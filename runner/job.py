#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions 任务入口：解析 Issue 正文 -> OPD 抓取 -> 写 status.json。

环境变量：
    ISSUE_NUMBER  Issue 编号（同时作为任务目录名）
    ISSUE_BODY    Issue 正文，内含 <!--OPD_JOB:<base64 JSON>-->
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

RUNNER_DIR = Path(__file__).resolve().parent
REPO_ROOT = RUNNER_DIR.parent
sys.path.insert(0, str(RUNNER_DIR))

from fetch import collect  # noqa: E402

SITE = "https://zzt00711.github.io/global-dossier"
ALLOWED_OFFICES = ("JP", "EP", "US", "KR")
MAX_JOBS_KEEP = 10
# 同 CN 号重复提交时直接复用上次抓取结果，避免重复请求 OPD
CACHE_TTL = 72 * 3600
MAX_CACHE_KEEP = 10
# workflow 上限 30 分钟，留 6 分钟给 checkout / 提交 / 回写 issue；
# 到点主动收尾，至少把已抓到的结果和 manifest 落盘。
JOB_BUDGET_SECONDS = 24 * 60


class _LogWriter(io.TextIOBase):
    """把日志同时写进内存、Actions 控制台和 status.json。

    Actions 超时取消时进程会被直接杀掉，finally 里的落盘和内存日志都不会
    留下。所以每写一行就刷一次真实 stdout，并回写 status.json。
    """

    def __init__(self, lines: list[str], on_line=None):
        self.lines = lines
        self.on_line = on_line

    def write(self, text: str) -> int:
        for line in text.splitlines():
            line = line.rstrip()
            if not line:
                continue
            self.lines.append(line)
            real = sys.__stdout__
            if real is not None:
                real.write(line + "\n")
                real.flush()
            if self.on_line is not None:
                self.on_line()
        return len(text)

    def flush(self) -> None:
        return None


def parse_payload(body: str) -> tuple[str, list[str], int]:
    m = re.search(r"<!--OPD_JOB:([A-Za-z0-9+/=]+)-->", body or "")
    if not m:
        raise ValueError("Issue 正文缺少 OPD_JOB 标记")
    try:
        data = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"OPD_JOB 参数解码失败: {exc}") from exc

    query = str(data.get("query", "")).strip().upper()
    if not re.fullmatch(r"CN\d{6,12}[A-Z]\d?", query):
        raise ValueError(
            f"CN 公开号格式不合法: {query!r}；请输入完整公开号（含 kind code），如 CN116887720A"
        )

    offices = [o for o in data.get("offices", []) if o in ALLOWED_OFFICES]
    if not offices:
        offices = list(ALLOWED_OFFICES)

    try:
        max_docs = int(data.get("max_docs", 4))
    except (TypeError, ValueError):
        max_docs = 4
    max_docs = max(1, min(5, max_docs))
    return query, offices, max_docs


def slim_result(manifest: dict, issue: int) -> dict:
    base = f"{SITE}/results/jobs/{issue}/{manifest['query'].upper()}"
    members = []
    for m in manifest.get("members", []):
        picked = []
        for p in m.get("picked", []):
            url = ""
            if p.get("file"):
                rel = str(p["file"]).replace("\\", "/")
                url = base + "/" + quote(rel, safe="/")
            picked.append(
                {
                    "name": p.get("name"),
                    "family": p.get("family"),
                    "date": p.get("date"),
                    "status": p.get("status"),
                    "bytes": p.get("bytes"),
                    "url": url,
                    "error": p.get("error"),
                }
            )
        members.append(
            {
                "country": m.get("country"),
                "app_num": m.get("app_num"),
                "status": m.get("status"),
                "doc_total": m.get("doc_total", 0),
                "picked": picked,
                "error": m.get("error"),
                "note": m.get("note"),
            }
        )
    return {
        "query": manifest.get("query"),
        "family_counts": manifest.get("family_counts", {}),
        "generated_at": manifest.get("generated_at"),
        "partial": bool(manifest.get("partial")),
        "members": members,
    }


def write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子写：Actions 超时杀进程时，status.json 不会停在半截 JSON 上
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def safe_write_status(path: Path, payload: dict) -> None:
    """单行日志触发的实时回写，失败不应打断抓取。"""
    try:
        write_status(path, payload)
    except OSError:
        pass


def cleanup_old_jobs(jobs_root: Path, current: int) -> None:
    if not jobs_root.is_dir():
        return
    dirs = [d for d in jobs_root.iterdir() if d.is_dir() and d.name.isdigit()]
    dirs.sort(key=lambda d: int(d.name), reverse=True)
    for d in dirs[MAX_JOBS_KEEP:]:
        if int(d.name) != current:
            shutil.rmtree(d, ignore_errors=True)


def _manifest_time(manifest: dict) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(manifest.get("generated_at", "")))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def load_cache_manifest(cache_dir: Path) -> dict | None:
    """读取缓存 manifest；缺失、损坏或超过 TTL 都视为无效。"""
    try:
        manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ts = _manifest_time(manifest)
    if ts is None:
        return None
    if (datetime.now(timezone.utc) - ts).total_seconds() > CACHE_TTL:
        return None
    return manifest


# 这些状态说明抓取本身失败，缓存再新也要重试；no_doc/bad_app_num 重试也不会变
# timeout 是时间预算用尽，成员还没抓到，同样不能被当成完整结果长期复用
RETRYABLE_STATUS = {"doc_list_failed", "download_failed", "timeout"}


def cache_covers(manifest: dict, offices: list[str], max_docs: int) -> bool:
    """缓存是否已覆盖本次请求的局别与篇数。"""
    if set(offices) - set(manifest.get("offices", [])):
        return False
    if int(manifest.get("max_docs_per_office", 0)) < max_docs:
        return False
    for member in manifest.get("members", []):
        if member.get("country") in offices and member.get("status") in RETRYABLE_STATUS:
            return False
    return True


def select_manifest(manifest: dict, offices: list[str], max_docs: int) -> dict:
    """从缓存 manifest 裁出本次请求的子集：局别过滤 + 篇数截断。"""
    out = json.loads(json.dumps(manifest))
    out["offices"] = list(offices)
    out["max_docs_per_office"] = max_docs
    for member in out.get("members", []):
        if member.get("country") not in offices:
            member["status"] = "skipped"
            member["picked"] = []
            member.pop("documents", None)
            continue
        picked = member.get("picked", [])[:max_docs]
        keep = {p.get("doc_id") for p in picked}
        member["picked"] = picked
        for doc in member.get("documents", []):
            doc["picked"] = doc.get("doc_id") in keep
    out.pop("manifest_path", None)
    return out


def copy_picked(manifest: dict, src_root: Path, dst_root: Path) -> None:
    """按 manifest 里被选中的文书复制 PDF，并写入任务目录的 manifest.json。"""
    for member in manifest.get("members", []):
        for item in member.get("picked", []):
            rel = item.get("file")
            if not rel:
                continue
            src = src_root / rel
            if not src.is_file():
                item["status"] = "failed"
                item["error"] = "缓存文件缺失"
                continue
            dst = dst_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    dst_root.mkdir(parents=True, exist_ok=True)
    write_status(dst_root / "manifest.json", manifest)


def cleanup_cache(cache_root: Path) -> None:
    if not cache_root.is_dir():
        return
    entries: list[tuple[datetime, Path]] = []
    for d in cache_root.iterdir():
        if not d.is_dir():
            continue
        manifest = load_cache_manifest(d)
        if manifest is None:
            shutil.rmtree(d, ignore_errors=True)
            continue
        ts = _manifest_time(manifest) or datetime.fromtimestamp(0, timezone.utc)
        entries.append((ts, d))
    # 按 manifest.generated_at 淘汰：checkout 会重置目录 mtime，mtime 不可信
    entries.sort(key=lambda item: item[0], reverse=True)
    for _, d in entries[MAX_CACHE_KEEP:]:
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    started = time.monotonic()
    issue = int(os.environ.get("ISSUE_NUMBER", "0"))
    body = os.environ.get("ISSUE_BODY", "")
    jobs_root = REPO_ROOT / "results" / "jobs"
    cache_root = REPO_ROOT / "results" / "cache"
    job_dir = jobs_root / str(issue)
    status_path = job_dir / "status.json"

    logs: list[str] = []
    status: dict = {
        "state": "running",
        "issue": issue,
        "site": SITE,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "logs": logs,
    }
    write_status(status_path, status)

    try:
        query, offices, max_docs = parse_payload(body)
        status.update({"query": query, "offices": offices, "max_docs": max_docs})
        write_status(status_path, status)
        print(f"开始：{query}  offices={','.join(offices)}  max_docs={max_docs}")

        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        job_dir.mkdir(parents=True, exist_ok=True)
        write_status(status_path, status)

        # 缓存按公开号归并，局别与篇数退化为"覆盖度"，同一号码的不同请求共用一份
        cache_dir = cache_root / query
        cached = load_cache_manifest(cache_dir)
        fetched = False

        def flush_status() -> None:
            status["logs"] = logs[-300:]
            safe_write_status(status_path, status)

        writer = _LogWriter(logs, flush_status)
        with contextlib.redirect_stdout(writer):
            if cached and cache_covers(cached, offices, max_docs):
                print(
                    f"命中缓存 {query}（{cached.get('max_docs_per_office')} 篇/局，"
                    f"{','.join(cached.get('offices', []))}），跳过 OPD 抓取"
                )
                status["cached"] = True
                manifest = select_manifest(cached, offices, max_docs)
            else:
                status["cached"] = False
                # 未覆盖时按并集补抓，成功后缓存同时服务其他局别/篇数
                fetch_offices = list(
                    dict.fromkeys(offices + list(cached.get("offices", []) if cached else []))
                )
                fetch_max = max(
                    max_docs, int(cached.get("max_docs_per_office", 0)) if cached else 0
                )
                print(f"未命中缓存，开始 OPD 抓取：{query}（{fetch_max} 篇/局）")
                shutil.rmtree(cache_dir, ignore_errors=True)
                full = collect(
                    query,
                    offices=fetch_offices,
                    max_docs=fetch_max,
                    out_root=cache_root,
                    download=True,
                    deadline=started + JOB_BUDGET_SECONDS,
                )
                manifest = select_manifest(full, offices, max_docs)
                fetched = True

            status["partial"] = bool(manifest.get("partial"))

            copy_picked(manifest, cache_dir, job_dir / query)
            if fetched and not any(
                p.get("status") == "ok"
                for m in manifest.get("members", [])
                for p in m.get("picked", [])
            ):
                # 全部失败的结果不值得缓存，避免下次直接复用空结果
                shutil.rmtree(cache_dir, ignore_errors=True)

        status["result"] = slim_result(manifest, issue)
        status["state"] = "done"
    except Exception as exc:
        status["state"] = "error"
        status["error"] = f"{type(exc).__name__}: {exc}"
        logs.append(traceback.format_exc())
        print(status["error"], file=sys.stderr)
    finally:
        status["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        status["logs"] = logs[-300:]
        write_status(status_path, status)
        cleanup_old_jobs(jobs_root, issue)
        cleanup_cache(cache_root)

    return 0 if status.get("state") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
