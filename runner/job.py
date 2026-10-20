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
MAX_CACHE_KEEP = 5


class _LogWriter(io.TextIOBase):
    def __init__(self, lines: list[str]):
        self.lines = lines

    def write(self, text: str) -> int:
        for line in text.splitlines():
            line = line.rstrip()
            if line:
                self.lines.append(line)
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
        max_docs = int(data.get("max_docs", 2))
    except (TypeError, ValueError):
        max_docs = 2
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
            }
        )
    return {
        "query": manifest.get("query"),
        "family_counts": manifest.get("family_counts", {}),
        "generated_at": manifest.get("generated_at"),
        "members": members,
    }


def write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cleanup_old_jobs(jobs_root: Path, current: int) -> None:
    if not jobs_root.is_dir():
        return
    dirs = [d for d in jobs_root.iterdir() if d.is_dir() and d.name.isdigit()]
    dirs.sort(key=lambda d: int(d.name), reverse=True)
    for d in dirs[MAX_JOBS_KEEP:]:
        if int(d.name) != current:
            shutil.rmtree(d, ignore_errors=True)


def cache_key(query: str, offices: list[str], max_docs: int) -> str:
    return f"{query}_{'-'.join(sorted(offices))}_{max_docs}"


def cache_valid(cache_dir: Path) -> bool:
    hits = list(cache_dir.glob("*/manifest.json"))
    if not hits:
        return False
    try:
        return time.time() - hits[0].stat().st_mtime <= CACHE_TTL
    except OSError:
        return False


def copy_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def load_cache(cache_dir: Path, job_dir: Path) -> dict | None:
    """命中缓存时复制到任务目录，并返回缓存中的 manifest。"""
    hits = list(cache_dir.glob("*/manifest.json"))
    if not hits:
        return None
    copy_tree(cache_dir, job_dir)
    try:
        return json.loads(hits[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cleanup_cache(cache_root: Path) -> None:
    if not cache_root.is_dir():
        return
    dirs = [d for d in cache_root.iterdir() if d.is_dir()]
    for d in dirs:
        if not cache_valid(d):
            shutil.rmtree(d, ignore_errors=True)
    dirs = [d for d in cache_root.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[MAX_CACHE_KEEP:]:
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
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

        key = cache_key(query, offices, max_docs)
        cache_dir = cache_root / key
        writer = _LogWriter(logs)
        with contextlib.redirect_stdout(writer):
            manifest = load_cache(cache_dir, job_dir) if cache_valid(cache_dir) else None
            if manifest:
                print(f"命中缓存 {key}，跳过 OPD 抓取")
                status["cached"] = True
            else:
                print(f"未命中缓存，开始 OPD 抓取：{key}")
                shutil.rmtree(cache_dir, ignore_errors=True)
                manifest = collect(
                    query,
                    offices=offices,
                    max_docs=max_docs,
                    out_root=cache_dir,
                    download=True,
                )
                copy_tree(cache_dir, job_dir)
                if any(
                    p.get("status") == "ok"
                    for m in manifest.get("members", [])
                    for p in m.get("picked", [])
                ):
                    os.utime(cache_dir, None)
                else:
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
