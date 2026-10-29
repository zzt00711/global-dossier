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

import hashlib
import json
import math
import random
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import requests

BASE = "https://www.j-platpat.inpit.go.jp"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# 必须与 UA 里的主版本号一致，否则「UA 说 124、sec-ch-ua 说别的版本」反而更可疑
CH_UA = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'

# 真实 Chrome 无论 XHR 还是页面导航都会带的那几项，单独抽出来共用。
# 不主动设置 Accept-Encoding：requests 默认的 gzip, deflate 不会触发
# 环境里没装 brotli 时的解码失败，而 Chrome 的 br/zstd 我们解不了。
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "sec-ch-ua": CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Connection": "keep-alive",
}

# XHR 型请求（wsh0701 / wsh0501 / wsh0901）
API_HEADERS = {
    **BROWSER_HEADERS,
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": BASE,
    "Referer": BASE + "/h0200",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Priority": "u=1, i",
}
# PDF 直链是地址栏导航，Accept 与 Sec-Fetch-* 要和「打开一个页面」对得上
PDF_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Referer": BASE + "/h0400",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# 需要退避重试的 HTTP 状态；429/503 优先使用服务端 Retry-After
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF = 60.0

# J-PlatPat 在临时无响应/限流时也会返回 STATUS_CD_A=1 + 这句话，
# 与「号码确实没有数据」无法区分，只能靠重试兜住前一种情况。
TRANSIENT_MSG = "情報が取得できませんでした"
# 入口同族查询的重试间隔（秒）；该接口失败即整单失败，值得等久一点。
# 实测 J-PlatPat 的软失败可持续 2~3 分钟以上，故把窗口拉到约 10 分钟，
# 仍在 job 的 30 分钟上限内；正常任务不会走到这里。
FAMILY_RETRY_DELAYS = (5.0, 30.0, 90.0, 180.0, 300.0)

# 2026-10 起 OPD 各接口都要求先做一次 Cookie 签名握手，否则网关直接回 C10131-E：
#   POST /app/docgw/wsc1101 {"SERVICE_ID": "WSH0701"}
# 返回 ECRL（7 位比特串，决定签名要拼哪些字段）与 ECKY，
# 再按 sha256(拼接串) 写入 Cookie Z，随业务请求一起发出。
HASH_URL = "/app/docgw/wsc1101"
COOKIE_UUID = "U"
COOKIE_LANG = "L"
COOKIE_PAGE_ID = "D"
COOKIE_REQUEST_TIME = "T"
COOKIE_SITE_ID = "S"
COOKIE_HASH = "Z"
SITE_ID = "www"
LANG = "ja"

# MSG_LIST 只有 ID 时，补一句人话，避免把 {'ID': 'C10131-E'} 直接怼到 issue 里
MSG_HINTS = {
    "C10129-E": "J-PlatPat 验证码已取消，请稍后重试",
    "C10130-E": "J-PlatPat 验证码校验未通过，请稍后重试",
    "C10131-E": "J-PlatPat 服务器繁忙（外部系统连携错误），请稍后重试",
    "C10132-E": "J-PlatPat 同一 IP 验证码次数超限，请稍后重试",
}

# 人类化节奏：按「上一个动作 → 下一个动作」分段取对数正态分布（秒）。
# 真实用户是「看一眼再点」，间隔天然右偏、偶尔很长，固定间隔反而像脚本。
DELAY_RANGES = {
    "family": (2.0, 4.5),      # 打开检索页 → 提交同族查询
    "doc_list": (1.5, 4.0),    # 同族结果 → 逐个局点开文书列表
    "doc_content": (1.5, 4.0),  # 文书列表 → 取某篇文书地址
    "pdf": (1.0, 3.0),         # 拿到地址 → 下载 PDF / 连下下一篇
}


def _human_delay(lo: float, hi: float) -> float:
    """对数正态采样并截断到 [lo, hi]，中位数取区间几何中点。"""
    mu = (math.log(lo) + math.log(hi)) / 2.0
    sigma = (math.log(hi) - math.log(lo)) / 3.92  # 约 95% 落在区间内
    return min(hi, max(lo, random.lognormvariate(mu, sigma)))



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


def _num_key(parts: dict[str, str]) -> str:
    """号段三元组拼成前端签名用的 CN.116887720.A 形式。"""
    return ".".join(
        [
            parts.get("CNTRY_CD_PART", ""),
            parts.get("NUM_PART", ""),
            parts.get("KIND_CD_PART", ""),
        ]
    )


def _is_transient(exc: "OpdError") -> bool:
    """判断是否为 J-PlatPat 侧临时不可用（可重试）。"""
    if exc.status != "1":
        return False
    return TRANSIENT_MSG in exc.message or "C10131-E" in exc.message


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
    def __init__(self, retries: int = 3):
        self.retries = retries
        self.session = self._new_session()
        self._last = 0.0
        # 官网前端用客户端自生成的 UUID 参与 Cookie 签名，这里同样生成一次
        self.uuid = str(uuid.uuid4())

    @staticmethod
    def _new_session() -> requests.Session:
        s = requests.Session()
        s.headers.update(BROWSER_HEADERS)
        return s

    def _throttle(self, kind: str) -> None:
        """按动作类型把两次请求的间隔拉到人类区间。首次请求不等待。"""
        lo, hi = DELAY_RANGES.get(kind, (1.5, 4.0))
        now = time.monotonic()
        wait = max(0.0, self._last + _human_delay(lo, hi) - now)
        self._last = now + wait
        if wait:
            time.sleep(wait)

    def _post(
        self, path: str, body: dict[str, Any], timeout: float = 60, kind: str = "doc_content"
    ) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle(kind)
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
                text = item.get("MSG") or item.get("MESSAGE") or item.get("MSG_TXT")
                if not text:
                    mid = str(item.get("ID") or "").strip()
                    text = f"{mid}：{MSG_HINTS[mid]}" if mid in MSG_HINTS else str(item)
                messages.append(str(text))
            else:
                messages.append(str(item))
        if not messages:
            mid = str(info.get("MSG_ID") or "").strip()
            if mid:
                messages.append(f"{mid}：{MSG_HINTS[mid]}" if mid in MSG_HINTS else mid)
        raise OpdError(code or "UNKNOWN", " / ".join(messages) or "OPD 返回失败")

    def _post_with_retry(
        self, path: str, body: dict[str, Any], timeout: float = 60, kind: str = "family"
    ) -> dict[str, Any]:
        """入口查询专用：对 J-PlatPat 临时无响应做多次退避重试。

        仅重试 STATUS_CD_A=1 且提示「情報が取得できませんでした」的情况，
        参数错误、号码无数据等永久错误立即抛出。
        """
        attempt = 0
        while True:
            try:
                return self._post(path, body, timeout=timeout, kind=kind)
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

    # ── Cookie 签名 ──────────────────────────────────────────
    def _cookie_hash(self, service_id: str, key: str, page_id: str) -> None:
        """按官网前端逻辑刷新 Cookie 签名 Z。

        ECRL 决定签名串拼哪些字段，顺序固定为
        号码 / UUID / 时刻 / 语言 / 页面 ID / 站点 ID / ECKY。
        """
        now = datetime.now()
        stamp = now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
        for name, value in (
            (COOKIE_UUID, self.uuid),
            (COOKIE_LANG, LANG),
            (COOKIE_PAGE_ID, page_id),
            (COOKIE_REQUEST_TIME, stamp),
            (COOKIE_SITE_ID, SITE_ID),
        ):
            self.session.cookies.set(name, value)
        try:
            r = self.session.post(
                BASE + HASH_URL,
                data=json.dumps({"SERVICE_ID": service_id}, ensure_ascii=False).encode("utf-8"),
                headers=API_HEADERS,
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise OpdError("AUTH", f"Cookie 签名握手失败: {exc}") from exc
        info = data.get("RSLT_INFO") or {}
        if str(info.get("RSLT_CD", "")).strip() != "0":
            mid = str(info.get("MSG_ID") or "").strip()
            raise OpdError("AUTH", MSG_HINTS.get(mid, mid) or "Cookie 签名握手失败")
        bits = bin(int(data.get("ECRL") or 0))[2:].zfill(7)
        parts = [
            key,
            self.uuid,
            stamp,
            LANG,
            page_id,
            SITE_ID,
            str(data.get("ECKY") or ""),
        ]
        joined = "".join(part for bit, part in zip(bits, parts) if bit == "1")
        self.session.cookies.set(
            COOKIE_HASH, hashlib.sha256(joined.encode("utf-8")).hexdigest()
        )

    # ── wsh0701 ──────────────────────────────────────────────
    def family(self, inqry: dict[str, str], bul_type: str = "publication") -> list[dict[str, Any]]:
        self._cookie_hash("WSH0701", _num_key(inqry), "H0200")
        body = {
            "DISP_ID": "H0200",
            "SVC": "jpp",
            "BIZ_MENU": "family-list",
            "BUL_TYPE": bul_type,
            "INQRY_NUM": inqry,
            "OTID": None,
        }
        data = self._post_with_retry("/app/opdgw/wsh0701", body, kind="family")
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
        self._cookie_hash("WSH0501", _num_key(app_num), "H0200")
        body = {
            "DISP_ID": "H0200",
            "SVC": "jpp",
            "BIZ_MENU": "document-list",
            "REFR_TYPE": refr_type,
            "INPUT": input_num,
            "APP_NUM": app_num,
            "OTID": None,
        }
        data = self._post("/app/opdgw/wsh0501", body, kind="doc_list")
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
        self._cookie_hash("WSH0901", _num_key(app_num), "H0400")
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
            self._throttle("pdf")
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
