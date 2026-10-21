#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""J-PlatPat OPD 三接口客户端。

只负责协议层：
    wsh0701  族列表
    wsh0501  文书列表
    wsh0901  文书 PDF 直链

不做业务筛选，不写报告。
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any, Iterable

import requests

BASE = "https://www.j-platpat.inpit.go.jp"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
API_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": BASE,
    "Referer": BASE + "/h0200",
    "User-Agent": UA,
}
PDF_HEADERS = {
    "Accept": "application/pdf,*/*",
    "Referer": BASE + "/h0400",
    "User-Agent": UA,
}

# 需要退避重试的 HTTP 状态；429/503 优先使用服务端 Retry-After
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF = 60.0

# J-PlatPat 在临时无响应/限流时也会返回 STATUS_CD_A=1 + 这句话，
# 与「号码确实没有数据」无法区分，只能靠重试兜住前一种情况。
TRANSIENT_MSG = "情報が取得できませんでした"
# 入口同族查询的重试间隔（秒）；该接口失败即整单失败，值得等久一点。
FAMILY_RETRY_DELAYS = (3.0, 20.0, 45.0, 90.0)


def _retry_delay(resp: requests.Response | None, attempt: int) -> float:
    """优先采用 Retry-After，否则指数退避并加入抖动。"""
    if resp is not None:
        raw = (resp.headers.get("Retry-After") or "").strip()
        if raw:
            try:
                return max(0.0, min(MAX_BACKOFF, float(raw)))
            except ValueError:
                pass
    return min(MAX_BACKOFF, 2.0 ** attempt + random.random())


class OpdError(RuntimeError):
    """OPD 业务错误或网络错误。status 为 STATUS_CD_A 或 NETWORK。"""

    def __init__(self, status: str, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


def _is_transient(exc: "OpdError") -> bool:
    """判断是否为 J-PlatPat 侧临时不可用（可重试）。"""
    return exc.status == "1" and TRANSIENT_MSG in exc.message


def parse_cn_publication(raw: str) -> dict[str, str]:
    """把 CN116887720A 解析为 OPD 号段。

    OPD 已不支持模糊匹配，kind code 必填；缺失时直接报错而不是猜 A/B/U。
    """
    s = re.sub(r"[\s\-]", "", (raw or "").strip().upper())
    m = re.fullmatch(r"(?:CN)?(\d{6,12})([A-Z]\d?)", s)
    if not m:
        raise ValueError(
            f"无法识别的 CN 公开号: {raw!r}；请输入完整公开号（含 kind code），如 CN116887720A"
        )
    return {"CNTRY_CD_PART": "CN", "NUM_PART": m.group(1), "KIND_CD_PART": m.group(2)}


def parse_member_num(value: str) -> dict[str, str]:
    """JP.2023554942.A -> 三段号段；容忍缺 kind 的写法。"""
    m = re.fullmatch(r"([A-Z]{2})\.([^.]+)\.(.+)", (value or "").strip().upper())
    if m:
        return {"CNTRY_CD_PART": m.group(1), "NUM_PART": m.group(2), "KIND_CD_PART": m.group(3)}
    m = re.fullmatch(r"([A-Z]{2})\.(.+)", (value or "").strip().upper())
    if m:
        return {"CNTRY_CD_PART": m.group(1), "NUM_PART": m.group(2), "KIND_CD_PART": ""}
    raise ValueError(f"无法识别的申请号: {value!r}")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        # 兼容 {"XXX": [...]} 的单层包装
        for v in value.values():
            if isinstance(v, list):
                return v
        return [value]
    return [value]


class OpdClient:
    def __init__(self, min_gap: float = 1.2, jitter: float = 1.0, retries: int = 3):
        self.min_gap = min_gap
        self.jitter = jitter
        self.retries = retries
        self.session = self._new_session()
        self._last = 0.0

    @staticmethod
    def _new_session() -> requests.Session:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        return s

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = max(0.0, self._last + self.min_gap + random.random() * self.jitter - now)
        self._last = now + wait
        if wait:
            time.sleep(wait)

    def _post(self, path: str, body: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                r = self.session.post(
                    BASE + path,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers=API_HEADERS,
                    timeout=timeout,
                )
                r.raise_for_status()
                data = r.json()
                self._check(data)
                return data
            except OpdError:
                raise
            except requests.exceptions.HTTPError as exc:
                last = exc
                resp = exc.response
                if attempt < self.retries and resp is not None and resp.status_code in RETRY_STATUS:
                    wait = _retry_delay(resp, attempt)
                    print(f"    OPD HTTP {resp.status_code}，{wait:.0f}s 后重试（{attempt}/{self.retries}）")
                    time.sleep(wait)
                    continue
            except Exception as exc:  # 网络、SSL、JSON 解析
                last = exc
                if attempt < self.retries:
                    time.sleep(min(MAX_BACKOFF, 1.2 * attempt + random.random()))
                    if isinstance(
                        exc,
                        (
                            requests.exceptions.SSLError,
                            requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout,
                        ),
                    ):
                        self.session = self._new_session()
        raise OpdError("NETWORK", f"{path} 请求失败: {last}")

    @staticmethod
    def _check(data: dict[str, Any]) -> None:
        info = data.get("RSLT_INFO") or {}
        code = str(info.get("STATUS_CD_A", "")).strip()
        if code == "0":
            return
        messages: list[str] = []
        for item in _as_list(info.get("MSG_LIST")):
            if isinstance(item, dict):
                text = item.get("MSG") or item.get("MESSAGE") or item.get("MSG_TXT") or item
                messages.append(str(text))
            else:
                messages.append(str(item))
        raise OpdError(code or "UNKNOWN", " / ".join(messages) or "OPD 返回失败")

    def _post_with_retry(
        self, path: str, body: dict[str, Any], timeout: float = 60
    ) -> dict[str, Any]:
        """入口查询专用：对 J-PlatPat 临时无响应做多次退避重试。

        仅重试 STATUS_CD_A=1 且提示「情報が取得できませんでした」的情况，
        参数错误、号码无数据等永久错误立即抛出。
        """
        attempt = 0
        while True:
            try:
                return self._post(path, body, timeout=timeout)
            except OpdError as exc:
                if not _is_transient(exc) or attempt >= len(FAMILY_RETRY_DELAYS):
                    raise
                wait = FAMILY_RETRY_DELAYS[attempt] + random.random() * 3
                print(
                    f"    OPD 暂未返回数据，{wait:.0f}s 后重试"
                    f"（{attempt + 1}/{len(FAMILY_RETRY_DELAYS)}）"
                )
                time.sleep(wait)
                attempt += 1

    # ── wsh0701 ──────────────────────────────────────────────
    def family(self, inqry: dict[str, str], bul_type: str = "publication") -> list[dict[str, Any]]:
        body = {
            "DISP_ID": "H0200",
            "SVC": "jpp",
            "BIZ_MENU": "family-list",
            "BUL_TYPE": bul_type,
            "INQRY_NUM": inqry,
            "OTID": None,
        }
        data = self._post_with_retry("/app/opdgw/wsh0701", body)
        respond = ((data.get("RSLT_INFO") or {}).get("FAMILY_INFO_RESPOND") or {})
        lst = (respond.get("FAMILY_INFO_LST") or {}).get("FAMILY_LST_LST")
        members: list[dict[str, Any]] = []
        for row in _as_list(lst):
            if not isinstance(row, dict):
                continue
            app_info = row.get("APP_NUM_INFO") or {}
            pubs = []
            for pub in _as_list((row.get("PUBLI_NUM_INFO_LST") or {}).get("PUBLI_NUM_INFO")):
                if isinstance(pub, dict) and pub.get("PUBLI_NUM"):
                    pubs.append(pub["PUBLI_NUM"])
            members.append(
                {
                    "family_id": row.get("FAMILY_ID"),
                    "country": (row.get("CNTRY_CD") or "").upper(),
                    "app_num": app_info.get("APP_NUM") or "",
                    "app_date": app_info.get("APP_DATE") or "",
                    "pub_nums": pubs,
                    "info_exist": str(row.get("INFO_EXIST_FLG") or ""),
                    "raw": row,
                }
            )
        return members

    # ── wsh0501 ──────────────────────────────────────────────
    def doc_list(
        self,
        input_num: dict[str, str],
        app_num: dict[str, str],
        refr_type: str = "publication",
    ) -> dict[str, Any]:
        body = {
            "DISP_ID": "H0200",
            "SVC": "jpp",
            "BIZ_MENU": "document-list",
            "REFR_TYPE": refr_type,
            "INPUT": input_num,
            "APP_NUM": app_num,
            "OTID": None,
        }
        data = self._post("/app/opdgw/wsh0501", body)
        respond = ((data.get("RSLT_INFO") or {}).get("DOC_LST_INFO_RESPOND") or {})
        bbl = respond.get("BBL_INFO") or {}
        docs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in _as_list((respond.get("DOC_INFO_LST") or {}).get("DOC_INFO")):
            if not isinstance(row, dict):
                continue
            orig = row.get("ORIG_TEXT_DOC_INFO") or {}
            rndr = row.get("RNDRNG_DOC_INFO") or {}
            doc_id = orig.get("DOC_ID") or ""
            if not doc_id or doc_id in seen:
                continue
            seen.add(doc_id)
            docs.append(
                {
                    "doc_id": doc_id,
                    "name": orig.get("DOC_NAME") or "",
                    "doc_id_en": rndr.get("DOC_ID") or "",
                    "name_en": rndr.get("DOC_NAME") or "",
                    "date": row.get("TENDER_DATE") or "",
                    "group": row.get("DOC_GRP") or "",
                    "raw": row,
                }
            )
        return {
            "bbl": bbl,
            "docs": docs,
            "title": ((bbl.get("RNDRNG_BBL_INFO") or {}).get("INVEN_NAME") or (bbl.get("ORIG_TEXT_BBL_INFO") or {}).get("INVEN_NAME") or ""),
        }

    # ── wsh0901 ──────────────────────────────────────────────
    def doc_content(
        self,
        input_num: dict[str, str],
        app_num: dict[str, str],
        doc_id: str,
        file_type: str = "PDF",
        refr_type: str = "publication",
    ) -> dict[str, Any]:
        body = {
            "DISP_ID": "H0400",
            "SVC": "jpp",
            "BIZ_MENU": "document-content",
            "REFR_TYPE": refr_type,
            "INPUT": input_num,
            "APP_NUM": app_num,
            "DOC_ID": doc_id,
            "FILE_TYPE": file_type,
            "OTID": None,
        }
        data = self._post("/app/opdgw/wsh0901", body)
        respond = ((data.get("RSLT_INFO") or {}).get("DOC_ENTTY_INFO_RESPOND") or {})
        info = respond.get("DOC_INFO") or {}
        url = info.get("DOC_URL") or ""
        if not url:
            raise OpdError("NO_URL", f"DOC_ID={doc_id} 未返回 DOC_URL")
        return {
            "doc_url": url,
            "absolute_url": url if url.startswith("http") else BASE + url,
            "app_num": (respond.get("APP_NUM_INFO") or {}).get("APP_NUM") or "",
            "tender_date": info.get("TENDER_DATE") or "",
            "name": (info.get("ORIG_TEXT_DOC_INFO") or {}).get("DOC_NAME") or "",
            "name_en": (info.get("RNDRNG_DOC_INFO") or {}).get("DOC_NAME") or "",
        }

    # ── PDF 下载 ─────────────────────────────────────────────
    def download_pdf(self, doc_url: str, dest: Path, timeout: tuple[float, float] = (10, 300)) -> int:
        """流式下载 PDF，返回字节数。DOC_URL 有时效，必须拿到后立即调用。"""
        url = doc_url if doc_url.startswith("http") else BASE + doc_url
        dest.parent.mkdir(parents=True, exist_ok=True)
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with self.session.get(url, headers=PDF_HEADERS, stream=True, timeout=timeout) as r:
                    r.raise_for_status()
                    first = b""
                    size = 0
                    with dest.open("wb") as fh:
                        for chunk in r.iter_content(64 * 1024):
                            if not chunk:
                                continue
                            if not first:
                                first = chunk[:5]
                                if not first.startswith(b"%PDF"):
                                    raise OpdError("NOT_PDF", f"响应不是 PDF: {first!r}")
                            fh.write(chunk)
                            size += len(chunk)
                    return size
            except OpdError:
                raise
            except requests.exceptions.HTTPError as exc:
                last = exc
                resp = exc.response
                if attempt < self.retries and resp is not None and resp.status_code in RETRY_STATUS:
                    wait = _retry_delay(resp, attempt)
                    print(f"    PDF HTTP {resp.status_code}，{wait:.0f}s 后重试（{attempt}/{self.retries}）")
                    time.sleep(wait)
                    continue
            except Exception as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(min(MAX_BACKOFF, 1.2 * attempt + random.random()))
        raise OpdError("DOWNLOAD", f"PDF 下载失败: {last}")


def family_by_publication(
    client: OpdClient, raw: str, bul_type: str = "publication"
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """按完整公开号（含 kind code）查询同族，不做 kind 猜测。"""
    parts = parse_cn_publication(raw)
    try:
        members = client.family(parts, bul_type=bul_type)
    except OpdError as exc:
        if _is_transient(exc):
            raise OpdError(
                exc.status,
                f"J-PlatPat 暂未返回 {raw} 的同族信息，已重试 "
                f"{len(FAMILY_RETRY_DELAYS)} 次仍失败，请稍后重新提交",
            ) from exc
        raise
    if not members:
        raise OpdError("EMPTY", f"号段 {parts} 未返回同族成员")
    return parts, members
