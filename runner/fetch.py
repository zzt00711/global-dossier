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
    family_by_publication,
    parse_member_num,
)

# 中国审查员关注点排序与配额。
# 顺序即轮转顺序：每轮各类各取 1 份，而不是把某类配额一次吃满。
# 这样 max_docs=4 时拿到的是「授权结论 + 审查意见 + 答复/修改 + 修改后权利要求」，
# 而不是被两份检索报告挤掉申请人的答复与修改。
# citation 放在 applicant / claims 之后：检索报告只是辅助佐证，
# 名额富余（max_docs>=6）时才会补入。
FAMILY_ORDER = ["final_grant", "rejection", "applicant", "claims", "citation", "other"]
FAMILY_QUOTA = {"final_grant": 1, "rejection": 2, "citation": 2, "applicant": 2, "claims": 1, "other": 0}

# 大族（如 CN117425921A 有 118 个成员、其中 US 69 个）如果逐个成员下载，
# 单次要抓 300+ 篇、40 分钟以上，必然撞上 job 的 30 分钟上限。
# 只取每局最早的若干个成员（通常就是原始申请 + 首个分案/续案），其余标记 skipped。
MAX_MEMBERS_PER_OFFICE = 2

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
    r"|\(electronic\)\s+receipt|matter\s+concerning\s+the\s+application"
    # 申请文件本身、审查请求、电子受理回执、年费地址变更：只是著录/程序性表格，
    # 不含任何审查意见或申请人争辩，对实审过程分析无用。
    r"|^\s*\[?patent\s+application\b|request\s+for\s+examination"
    r"|electronic\s+filing\s+system|maintenance\s+fee"
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
# 申请人提交的上诉/口审/审判请求类文书。
# 与官方侧分开判：口审传票、上诉委员会决定由官方发出，
# 而「对传票的答复」「口审请求」「上诉理由书」是申请人提交的，
# 两者名称里都含 oral proceedings / summons / board of appeal，只能靠动作词区分。
RE_APPEAL_APPLICANT = re.compile(
    r"appeal\s+brief|notice\s+of\s+appeal|statement\s+of\s+(the\s+)?grounds\s+of\s+appeal"
    r"|request\s+for\s+oral|reply\s+to|response\s+to|amendments?\s+filed"
    r"|審判請求|심판\s*청구",
    re.I,
)
# 官方侧：上诉委员会决定、口审传票/期日通知、审决。
# 日文实名是「口頭審理通知書」「審判口頭審理期日通知書」，不含「口審」二字，
# 故必须收 口頭審理；韩文「거절결정불복심판 청구서」中间有空格，故用 \s*。
RE_APPEAL_OFFICIAL = re.compile(
    r"board\s+of\s+appeal|decision\s+on\s+appeal|appeal\s+decision"
    r"|oral\s+proceedings|oral\s+hearing|summons"
    r"|審決|口頭審理|口審",
    re.I,
)
# 国际阶段（PCT）文书：ISA 国际检索报告、ISA 书面意见、国际初步审查报告、IPRP。
# 由国际检索/初审单位出具，属另一程序阶段，不是各国家局的审查通知书，
# 归 other（配额 0 且不参与补位）即不会被取用。
RE_WO_PHASE = re.compile(
    r"international\s+search\s+report|international\s+preliminary\s+(report|examination)"
    r"|written\s+opinion\s+of\s+the\s+international|\bISA\b|\bIPEA\b|43bis"
    # PCT 阶段表格（PCT/IB/318 优先权声明、PCT/ISA/210 检索报告、PCT/RO/101 受理等）
    # 由国际局/受理局出具，不是国家局审查文书；且 "PCT/IB/318 ... Priority Claims"
    # 含 claims 字样，早判为 other 才不会被 RE_CLAIMS 抢去占 claims 配额。
    r"|\bPCT/[A-Z]{2}/\d+\b"
    r"|国際調査|書面による見解|国際予備報告|国際予備審査"
    r"|국제조사|국제예비",
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
    # 日文检索报告：OPD 的日文名不含 "search report"，缺了这组只能靠 name_en 兜，
    # 一旦 name_en 缺失就会落进 other（配额 0）而永远取不到。
    # 注意只收本国局的「検索報告」，国际阶段的「国際調査報告」由 RE_WO_PHASE 拦下。
    r"|引用文献|先行技術|인용문헌|对比文件|检索报告|検索報告",
    re.I,
)
RE_CLAIMS = re.compile(r"\bclaims\b|claim\s+set|特許請求の範囲|請求の範囲|청구범위|权利要求", re.I)


def _is_office_issued(group: str) -> bool:
    """DOC_GRP 是否含 token "2"（特許庁/官方发出）。

    实测约 280 份文书（JP/EP/US/KR）无一例反证：
      拒絶理由通知書=2,21,101,102  拒絶査定=2,23,101  審査前置移管通知=2
      European search opinion=2,21  Decision to grant=2,23
      Notice of Allowance=2,23      Reminder period for payment...=2

    必须按逗号切分后精确比对 token，不能用子串：
    国際調査報告的 group 是 "4,101,102"，其中 102 含字符 '2'，
    子串判断会把它误判成官方文书。

    注意 "4" 只表示「出願人提出侧的書類」，国際調査報告 / IPRP / 優先権書類
    都带 4，所以不能用 4 反推「申请人实质答复」，这里只做官方侧的否决。
    """
    return "2" in {tok.strip() for tok in (group or "").split(",") if tok.strip()}


def classify(name: str, group: str = "") -> str:
    # group 含 token 2 = 官方发出，绝不可能是申请人答复。
    # 用它否决名称启发式的 applicant 判定，其余情况仍靠名称。
    office = _is_office_issued(group)
    if RE_FINAL_GRANT.search(name):
        return "final_grant"
    # 国际阶段文书（ISR / ISA 书面意见 / IPRP / 国际初审报告）不是国家局审查通知书，
    # 必须早于所有国家局分支判断，否则会被 written opinion / search report 抢走
    if RE_WO_PHASE.search(name):
        return "other"
    # Office Action Appendix 是审查意见的引用文献附录，归入对比文件
    if re.search(r"office\s+action\s+appendix", name, re.I):
        return "citation"
    # 审查员针对上诉理由书的答辩是官方实质意见，归审查意见侧。
    # 必须先判：名称里含 "Appeal Brief"，否则会被申请人侧的 appeal brief 抢走
    if re.search(r"examiner'?s\s+answer", name, re.I):
        return "rejection"
    # 纯程序性的上诉转送/延期通知，无实质内容
    if re.search(r"forwarding\s+of\s+appeal|appeal\s+forwarded|time\s+period\s+for\s+filing\s+appeal", name, re.I):
        return "other"
    # 上诉/口审：先判申请人侧，再判官方侧。顺序不能反——
    # "Reply to the summons to oral proceedings"、"Request for oral proceedings"
    # 同时含 summons / oral proceedings，若先判官方侧会把申请人答复误归 rejection
    if RE_APPEAL_APPLICANT.search(name) and not office:
        return "applicant"
    if RE_APPEAL_OFFICIAL.search(name):
        return "rejection"
    # EP 审查员主动发出的程序性通知（可能的修改/权利要求费、补正缺陷、
    # 维持申请声明邀请等）不是申请人答复。名称含 amendment / written opinion /
    # claims fee，会被下面的 RE_APPLICANT 抢走；且 EP 约半数文书 DOC_GRP 为
    # unknown，office 否决救不到，必须靠名称显式归 other。
    # 锚定 communication / invitation 前缀，不会误伤 "Reply to communication
    # from the Examining Division" 这类申请人答复（已在上面的 RE_APPEAL_APPLICANT 命中）。
    if re.search(
        r"communication\s+(regarding|concerning)"
        r"|invitation\s+to\s+(declare|correct|file|pay|submit|complete|comment)"
        r"|correction\s+of\s+deficiencies|correct\s+deficiencies"
        r"|deficiencies\s+in\s+the\s+written\s+opinion",
        name, re.I):
        return "other"
    # 答复类先于审查意见，避免 "Amendment after Non-Final Rejection" 被误判
    if RE_APPLICANT.search(name) and not office:
        return "applicant"
    if RE_REJECTION.search(name):
        return "rejection"
    # IDS 表格与外国对比文件清单只是著录表格，不占对比文件配额
    if re.search(r"information\s+disclosure\s+statement|\bids\b|foreign\s+reference", name, re.I):
        return "other"
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
    if family == "final_grant":
        # 授权通知只有一行著录信息，优先取带审查结论的 Notice of Allowance / 査定
        if "issue notification" in name or "egrant" in name:
            return 2
        return 0
    if family == "rejection":
        if "advisory action" in name:
            return 2
        # 口审传票/期日通知只是排期，不含任何审查理由，最低优先，
        # 否则会在 rejection 只有 2 个名额时挤掉实质审查意见
        if re.search(r"summons|oral\s+proceedings|oral\s+hearing|口頭審理|口審", name):
            return 2
        # 上诉委员会决定/审决是结论性文书，次于承载审查理由的通知书
        if re.search(r"board\s+of\s+appeal|decision\s+on\s+appeal|appeal\s+decision|審決", name):
            return 1
        if "final rejection" in name or "拒絶査定" in name:
            return 1
        return 0
    if family == "citation":
        # 国际阶段的 ISR 副本已由 RE_WO_PHASE 归入 other，这里只处理本国局检索文书：
        # 检索报告/检索意见的信息量远高于检索策略与引用清单
        if "search report" in name or "search opinion" in name:
            return 0
        if "search strategy" in name or "information on search" in name or "search information" in name:
            return 1
        return 2
    if family == "claims":
        return 0 if name.strip() == "claims" else 1
    return 0


def pick_docs(docs: list[dict[str, Any]], max_docs: int) -> list[dict[str, Any]]:
    """按类别配额轮转挑选；同类按日期倒序。

    每轮从每个类别各取 1 份，直到达到 max_docs 或所有配额用尽。
    这样在名额紧张（如 max_docs=4）时，每个有文书的类别都能分到至少 1 份，
    不会被排在前面的类别一次吃满。
    """
    for d in docs:
        d["family"] = classify(f"{d.get('name', '')} {d.get('name_en', '')}", d.get("group", ""))

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

    queues = {f: ordered(buckets[f], f) for f in FAMILY_ORDER}
    pos = {f: 0 for f in FAMILY_ORDER}
    quota = dict(FAMILY_QUOTA)

    def take(family: str) -> bool:
        """从该类别取下一份未用过的文书；取到返回 True。"""
        items = queues[family]
        while pos[family] < len(items):
            d = items[pos[family]]
            pos[family] += 1
            if key(d) in used:
                continue
            used.add(key(d))
            picked.append(d)
            quota[family] -= 1
            return True
        return False

    # 轮转：一轮内每个类别最多取 1 份
    while len(picked) < max_docs:
        progressed = False
        for family in FAMILY_ORDER:
            if len(picked) >= max_docs:
                break
            if quota[family] <= 0:
                continue
            if take(family):
                progressed = True
        if not progressed:
            break

    # 名额没取满时，只用其余实质文书（超出单类配额的审查意见/答复）补，
    # 绝不用 other 类凑数：max_docs 是上限而非目标，宁可少取。
    if len(picked) < max_docs:
        for d in ordered(docs, "other"):
            if len(picked) >= max_docs:
                break
            if d.get("family") == "other":
                continue
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


def _norm_num(value: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (value or "").upper())


def _num_core(value: str) -> str:
    """去掉末尾 kind code，只留国家+号码，便于 A/B 号互相匹配。"""
    return re.sub(r"[A-Z]\d?$", "", _norm_num(value))


def _app_serial(app_num: str) -> int:
    """申请号里的数字段；越小越早递交（分案/续案都在母案之后）。"""
    parts = (app_num or "").split(".")
    digits = re.sub(r"\D", "", parts[1] if len(parts) > 1 else "")
    return int(digits) if digits else 10**18


def _find_anchor(members: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """被查公开号自身所在的那一行，用来锁定代际。"""
    core = _num_core(query)
    for m in members:
        for pub in m.get("pub_nums", []):
            if _num_core(pub) == core:
                return m
    return None


def _first_cn_date(members: list[dict[str, Any]]) -> str:
    dates = [m.get("app_date") for m in members if m.get("country") == "CN" and m.get("app_date")]
    return min(dates) if dates else ""


def _collect_member(
    client: OpdClient,
    input_parts: dict[str, str],
    member: dict[str, Any],
    country: str,
    max_docs: int,
    out_dir: Path,
    download: bool,
    entry: dict[str, Any],
    deadline: float | None = None,
) -> bool:
    """抓单个同族成员的文书并下载；返回是否拿到了可用文书。"""
    app_parts = _member_app_parts(member)
    if not app_parts:
        entry["status"] = "bad_app_num"
        return False

    try:
        listing = client.doc_list(input_parts, app_parts, refr_type="publication")
    except OpdError as exc:
        entry["status"] = "doc_list_failed"
        entry["error"] = str(exc)
        return False

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
            # 保留 OPD 的 DOC_GRP，便于事后核对分类是否判错（含 token 2 = 官方发出）
            "group": d.get("group", ""),
            "excluded": is_excluded(f"{d.get('name', '')} {d.get('name_en', '')}"),
            "picked": d["doc_id"] in picked_keys,
        }
        for d in docs
    ]

    if not picked:
        entry["status"] = "no_doc"
        # 区分「真的没有文书」与「有文书但全是申请文件/程序性表格」，
        # 否则前端只显示一个 no_doc，看不出该局其实还没进入实审。
        entry["error"] = (
            f"该国 {len(docs)} 份文书均为申请文件或程序性表格，无实质审查文书可取"
            if docs
            else "该国文书列表为空"
        )
        return False

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
        # 时间预算用尽后不再发起新下载，已下的保留，剩下的明确标记
        if deadline is not None and time.monotonic() >= deadline:
            item.update({"status": "timeout", "error": "任务时间预算用尽，未下载"})
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
    if ok:
        entry["status"] = "ok"
    elif any(p.get("status") == "timeout" for p in entry["picked"]):
        entry["status"] = "timeout"
        entry["error"] = "任务时间预算用尽，未下载"
    else:
        entry["status"] = "download_failed"
    return bool(ok)


def collect(
    query: str,
    offices: list[str] | None = None,
    max_docs: int = 6,
    out_root: Path | None = None,
    download: bool = True,
    client: OpdClient | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """抓取并下载，返回 manifest。deadline 为 time.monotonic() 口径的截止时刻。"""
    offices = [o.upper() for o in (offices or ["JP", "EP", "US", "KR"])]
    client = client or OpdClient()
    out_dir = (out_root or Path(__file__).parent / "out") / query.upper()
    out_dir.mkdir(parents=True, exist_ok=True)

    input_parts, members = family_by_publication(client, query)
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

    # 用被查号自身那一行的申请日锁定代际：分案/续案会沿用母案申请日，
    # 所以同一代里既有美国 PCT 国家阶段（单独成组），也有其他局的对应件。
    anchor = _find_anchor(unique_members, query)
    anchor_date = (anchor or {}).get("app_date") or _first_cn_date(unique_members)
    manifest["anchor"] = {
        "app_num": (anchor or {}).get("app_num"),
        "app_date": anchor_date,
        "family_id": (anchor or {}).get("family_id"),
    }

    office_entries: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for member in unique_members:
        country = member.get("country", "")
        entry: dict[str, Any] = {
            "country": country,
            "app_num": member.get("app_num"),
            "app_date": member.get("app_date") or "",
            "family_id": member.get("family_id"),
            "pub_nums": member.get("pub_nums", []),
            "status": "skipped",
            "doc_total": 0,
            "picked": [],
        }
        manifest["members"].append(entry)
        if country in offices:
            office_entries.append((member, entry))

    covered: set[str] = set()
    covered_gen: dict[str, str] = {}
    for country in offices:
        total = manifest["family_counts"].get(country, 0)
        groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        undated: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for member, entry in office_entries:
            if member.get("country") != country:
                continue
            d = member.get("app_date") or ""
            if d:
                groups.setdefault(d, []).append((member, entry))
            else:
                undated.append((member, entry))

        dated = sorted(groups, reverse=True)
        prev_date = ""
        earliest_date = dated[-1] if dated else ""
        if anchor_date:
            # 先本代；本代全失败再逐代往回降级（上一代优先，其次更早，最后才试更新的代）
            earlier = [d for d in dated if d < anchor_date]
            prev_date = earlier[0] if earlier else ""
            order = ([anchor_date] if anchor_date in groups else []) + earlier
            order += sorted(d for d in dated if d > anchor_date)
        else:
            # 定位不到本代时，按「都抓不到就取最早」兜底：最早申请优先
            order = sorted(groups)
        if undated:
            order.append("")

        for d in order:
            batch = groups.get(d, []) if d else undated
            # 申请号序号升序：原申请在前，分案/续案在后
            batch.sort(key=lambda pair: _app_serial(pair[0].get("app_num") or ""))
            attempted = 0
            for member, entry in batch:
                if country in covered:
                    entry["status"] = "sibling" if d == anchor_date else "other_generation"
                    entry["note"] = (
                        f"该局同族成员 {total} 个，"
                        f"已取到 {covered_gen.get(country) or d or anchor_date} 代的文书，不再重复下载"
                    )
                    continue
                if attempted >= MAX_MEMBERS_PER_OFFICE:
                    entry["status"] = "member_limit"
                    entry["error"] = (
                        f"该局同族成员 {total} 个，"
                        f"{d or anchor_date} 这一代仅处理前 {MAX_MEMBERS_PER_OFFICE} 个"
                    )
                    continue
                attempted += 1
                if deadline is not None and time.monotonic() >= deadline:
                    # 预算用尽：不再开新成员，明确标记而不是伪装成 skipped
                    entry["status"] = "timeout"
                    entry["note"] = "任务时间预算用尽，本次未抓取"
                    manifest["partial"] = True
                    continue
                if _collect_member(
                    client,
                    input_parts,
                    member,
                    country,
                    max_docs,
                    out_dir,
                    download,
                    entry,
                    deadline=deadline,
                ):
                    covered.add(country)
                    covered_gen[country] = d
                    if not anchor_date:
                        entry["note"] = (
                            f"该局同族成员 {total} 个，未定位到本代，"
                            f"按最早申请优先取用（{d or '无申请日'}）"
                        )
                    elif d and d != anchor_date:
                        if d < anchor_date:
                            if d == prev_date:
                                which = "上一代"
                            elif d == earliest_date:
                                which = "最早申请（兜底）"
                            else:
                                which = "更早一代"
                            entry["note"] = (
                                f"该局同族成员 {total} 个，本代（{anchor_date}）未取到文书，"
                                f"已降级使用{which}（{d}）"
                            )
                        else:
                            entry["note"] = (
                                f"该局同族成员 {total} 个，本代（{anchor_date}）未取到文书，"
                                f"已改用更新一代（{d}）"
                            )
                    else:
                        entry["note"] = f"该局同族成员 {total} 个，使用本代原申请"

    if any(m.get("status") == "timeout" for m in manifest["members"]):
        manifest["partial"] = True

    manifest_path = out_dir / "manifest.json"
    # 原子写：进程被 30 分钟上限杀掉时，不会留下半截 manifest 让缓存误判
    tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(manifest_path)
    manifest["manifest_path"] = str(manifest_path)
    return manifest
