#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 CN 公开号抓取五局同族审查文书。

流程：
    wsh0701 族列表 -> 逐局 wsh0501 文书列表 -> 筛选 -> wsh0901 + PDF 下载

产物：
    out/<query>/manifest.json
    out/<query>/<CC>/*.pdf
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opd import (
    OpdClient,
    OpdError,
    family_with_kind_fallback,
    parse_member_num,
)

# 中国审查员关注点排序与配额（沿用 gd_helper.pick_exam_docs）
FAMILY_ORDER = ["final_grant", "rejection", "citation", "applicant", "claims", "other"]
FAMILY_QUOTA = {"final_grant": 1, "rejection": 2, "citation": 2, "applicant": 2, "claims": 1, "other": 0}

# 全文公报/优先权证明对实审分析价值低且体积大，直接排除
EXCLUDE_NAME = re.compile(
    r"priority\s+document|priority\s+certificate"
    r"|優先権証明書|優先権書類|우선권"
    r"|公開特許公報|特許公報|公表特許公報|再公表特許公報|実用新案公報|公報"
    r"|international\s+publication|pamphlet|gazette"
    r"|patent\s+application\s+publication"
    r"|courtesy\s+postcard|postcard"
    r"|claims\s+worksheet|index\s+of\s+claims|claims\s+listing"
    r"|letter\s+accompanying|communication\s+regarding\s+the\s+transmission"
    r"|notification\s+of\s+forthcoming\s+publication|request\s+for\s+entry\s+into\s+the\s+european\s+phase"
    r"|request\s+for\s+extension|extension\s+of\s+time\s+limit"
    r"|国際公開|公表",
    re.I,
)

RE_FINAL_GRANT = re.compile(
    r"notice\s+of\s+allowance|decision\s+to\s+grant|intention\s+to\s+grant"
    r"|text\s+intended\s+for\s+grant|issue\s+notification"
    r"|decision\s+(on|of|for)\s+registration|written\s+decision\s+on\s+registration|patented"
    r"|特許査定|登録査定|設定登録|등록결정|授权|授予",
    re.I,
)
RE_APPLICANT = re.compile(
    r"amend(ed|ment)?|remarks|written\s+opinion|argument|reply|response"
    r"|request\s+for\s+(continued\s+examination|reconsideration)"
    r"|意見書|補正書|手続補正書|答弁書|上申書|보정|의견서|陈述意见|修改",
    re.I,
)
RE_REJECTION = re.compile(
    r"final\s+rejection|non[- ]final|office\s+action|advisory\s+action"
    r"|reasons?\s+(for|of)\s+refusal|notification\s+of\s+reasons"
    r"|notice\s+of\s+examination\s+opinion|examination\s+opinion|search\s+opinion"
    r"|request\s+for\s+the\s+submission\s+of\s+an\s+opinion"
    r"|communication\s+(from|under|pursuant)|decision\s+(of|on)\s+rejection|revocat"
    r"|拒絶理由|拒絶査定|審査意見|意見照会|거절이유|거절결정|审查意见|驳回",
    re.I,
)
RE_CITATION = re.compile(
    r"list\s+of\s+references|search\s+report|search\s+strategy|search\s+information"
    r"|citation|information\s+disclosure\s+statement|\bids\b"
    r"|引用文献|先行技術|인용문헌|对比文件|检索报告",
    re.I,
)
RE_CLAIMS = re.compile(r"\bclaims\b|claim\s+set|特許請求の範囲|請求の範囲|청구범위|权利要求", re.I)


def classify(name: str) -> str:
    if RE_FINAL_GRANT.search(name):
        return "final_grant"
    # Office Action Appendix 是审查意见的引用文献附录，归入对比文件
    if re.search(r"office\s+action\s+appendix", name, re.I):
        return "citation"
    # 答复类先于审查意见，避免 "Amendment after Non-Final Rejection" 被误判
    if RE_APPLICANT.search(name):
        return "applicant"
    if RE_REJECTION.search(name):
        return "rejection"
    if RE_CITATION.search(name):
        return "citation"
    if RE_CLAIMS.search(name):
        return "claims"
    return "other"


def is_excluded(name: str) -> bool:
    return bool(EXCLUDE_NAME.search(name))


def _date_key(value: str) -> str:
    return (value or "").replace("-", "")


def _family_rank(family: str, doc: dict[str, Any]) -> int:
    """类别内部的偏好：数字越小越优先。"""
    name = f"{doc.get('name', '')} {doc.get('name_en', '')}".lower()
    if family == "rejection":
        if "advisory action" in name:
            return 2
        if "final rejection" in name or "拒絶査定" in name:
            return 1
        return 0
    if family == "claims":
        return 0 if name.strip() == "claims" else 1
    return 0


def pick_docs(docs: list[dict[str, Any]], max_docs: int) -> list[dict[str, Any]]:
    """按类别配额挑选；同类按日期倒序。"""
    for d in docs:
        d["family"] = classify(f"{d.get('name', '')} {d.get('name_en', '')}")

    picked: list[dict[str, Any]] = []
    used: set[str] = set()

    def ordered(items: list[dict[str, Any]], family: str) -> list[dict[str, Any]]:
        # 先按日期倒序，再按类别内偏好稳定排序
        arr = sorted(items, key=lambda d: _date_key(d.get("date", "")), reverse=True)
        return sorted(arr, key=lambda d: _family_rank(family, d))

    def key(d: dict[str, Any]) -> str:
        k = d.get("doc_id", "")
        return k[:-3] if k.endswith("_JP") else k

    buckets = {f: [] for f in FAMILY_ORDER}
    for d in docs:
        if not is_excluded(f"{d.get('name', '')} {d.get('name_en', '')}"):
            buckets[d["family"]].append(d)

    for family in FAMILY_ORDER:
        quota = FAMILY_QUOTA[family]
        if quota <= 0:
            continue
        for d in ordered(buckets[family], family):
            if len(picked) >= max_docs or quota <= 0:
                break
            if key(d) in used:
                continue
            used.add(key(d))
            picked.append(d)
            quota -= 1

    if len(picked) < max_docs:
        for d in ordered(docs, "other"):
            if len(picked) >= max_docs:
                break
            if is_excluded(f"{d.get('name', '')} {d.get('name_en', '')}") or key(d) in used:
                continue
            used.add(key(d))
            picked.append(d)

    return picked


def safe_filename(value: str, limit: int = 60) -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", value or "")
    s = re.sub(r"\s+", "_", s).strip("._")
    return (s or "document")[:limit]


def _member_app_parts(member: dict[str, Any]) -> dict[str, str] | None:
    try:
        return parse_member_num(member.get("app_num", ""))
    except ValueError:
        return None


def collect(
    query: str,
    offices: list[str] | None = None,
    max_docs: int = 2,
    out_root: Path | None = None,
    download: bool = True,
    client: OpdClient | None = None,
) -> dict[str, Any]:
    """抓取并下载，返回 manifest。"""
    offices = [o.upper() for o in (offices or ["JP", "EP", "US", "KR"])]
    client = client or OpdClient()
    out_dir = (out_root or Path(__file__).parent / "out") / query.upper()
    out_dir.mkdir(parents=True, exist_ok=True)

    input_parts, members = family_with_kind_fallback(client, query)
    # 同族成员按申请号去重
    seen: set[str] = set()
    unique_members = []
    for m in members:
        app = m.get("app_num") or ""
        if not app or app in seen:
            continue
        seen.add(app)
        unique_members.append(m)

    manifest: dict[str, Any] = {
        "query": query,
        "input": input_parts,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "offices": offices,
        "max_docs_per_office": max_docs,
        "download": download,
        "family_counts": {},
        "members": [],
    }
    for m in unique_members:
        manifest["family_counts"][m["country"]] = manifest["family_counts"].get(m["country"], 0) + 1

    for member in unique_members:
        country = member.get("country", "")
        entry: dict[str, Any] = {
            "country": country,
            "app_num": member.get("app_num"),
            "pub_nums": member.get("pub_nums", []),
            "status": "skipped",
            "doc_total": 0,
            "picked": [],
        }
        manifest["members"].append(entry)

        if country not in offices:
            continue
        app_parts = _member_app_parts(member)
        if not app_parts:
            entry["status"] = "bad_app_num"
            continue

        try:
            listing = client.doc_list(input_parts, app_parts, refr_type="publication")
        except OpdError as exc:
            entry["status"] = "doc_list_failed"
            entry["error"] = str(exc)
            continue

        docs = listing["docs"]
        entry["title"] = listing.get("title", "")
        entry["doc_total"] = len(docs)
        picked = pick_docs(docs, max_docs)
        picked_keys = {d["doc_id"] for d in picked}

        entry["documents"] = [
            {
                "doc_id": d.get("doc_id"),
                "name": d.get("name") or d.get("name_en"),
                "date": d.get("date"),
                "family": d.get("family"),
                "excluded": is_excluded(f"{d.get('name', '')} {d.get('name_en', '')}"),
                "picked": d["doc_id"] in picked_keys,
            }
            for d in docs
        ]

        if not picked:
            entry["status"] = "no_doc"
            continue

        cdir = out_dir / country
        cdir.mkdir(parents=True, exist_ok=True)
        for d in picked:
            item: dict[str, Any] = {
                "doc_id": d["doc_id"],
                "name": d.get("name") or d.get("name_en"),
                "name_en": d.get("name_en"),
                "date": d.get("date"),
                "family": d.get("family"),
            }
            if not download:
                item["status"] = "dry_run"
                entry["picked"].append(item)
                continue
            try:
                content = client.doc_content(input_parts, app_parts, d["doc_id"])
                item["tender_date"] = content.get("tender_date")
                fname = "_".join(
                    x
                    for x in (
                        safe_filename(content.get("tender_date") or d.get("date") or ""),
                        country,
                        safe_filename(d.get("name_en") or d.get("name") or "doc"),
                        safe_filename(d["doc_id"][-12:]),
                    )
                    if x
                ) + ".pdf"
                dest = cdir / fname
                size = client.download_pdf(content["doc_url"], dest)
                item.update({"file": str(dest.relative_to(out_dir)), "bytes": size, "status": "ok"})
                print(f"    OK  {country} {item['name']}  {size/1024:.0f} KB")
            except (OpdError, OSError, KeyError) as exc:
                item.update({"status": "failed", "error": str(exc)})
                print(f"    ERR {country} {item.get('name')}  {exc}")
            entry["picked"].append(item)
            time.sleep(0.3)

        ok = [p for p in entry["picked"] if p.get("status") in {"ok", "dry_run"}]
        entry["status"] = "ok" if ok else "download_failed"

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest["manifest_path"] = str(manifest_path)
    return manifest
