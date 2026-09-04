"""Async API client for EVN CSKH Monitor.

This is the complete runtime client used by the integration.  It intentionally
keeps all network I/O asynchronous and bounded so Home Assistant startup is not
blocked by a slow EVN gateway.
"""

from __future__ import annotations

import asyncio
from calendar import monthrange
from datetime import datetime, timedelta
import json
import logging
from time import monotonic
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    REQUEST_CONNECT_TIMEOUT_SECONDS,
    REQUEST_READ_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
)
from .invoice import (
    decode_base64_payload,
    detect_invoice_type,
    extract_invoice_links_from_html,
    iter_attachment_candidates,
)

_LOGGER = logging.getLogger(__name__)

EVN_REGIONS = {
    "HN": "https://gwkong.evnhanoi.vn",
    "NPC": "https://apicskhevn.npc.com.vn",
    "CPC": "https://cskh-api.cpc.vn",
    "SPC": "https://api.cskh.evnspc.vn",
    "HCMC": "https://cskh.evnhcmc.vn",
}
LOGIN_URL = "https://cskh.evn.com.vn/cskh/v1/auth/login"
SPC_LOGIN_URL = "https://api.cskh.evnspc.vn/api/user/authenticate"
NOTIFICATION_URL = "https://cskh.evn.com.vn/cskh/v1/notification/getAllByUser"
_EVN_AUTH_SUFFIXES = (
    ".evn.com.vn",
    ".npc.com.vn",
    ".cpc.vn",
    ".evnspc.vn",
    ".evnhanoi.vn",
    ".evnhcmc.vn",
)


class EVNAPI:
    """EVN API client supporting HN/NPC/CPC/SPC/HCMC."""

    def __init__(self, hass, region: str, username: str, password: str, customer_id: str):
        self.hass = hass
        self.region = region.upper()
        self.username = username
        self.password = password
        self.customer_id = customer_id
        self.base_url = EVN_REGIONS.get(self.region)
        if not self.base_url:
            raise ValueError(f"Invalid region: {region}")
        self.access_token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(
            total=REQUEST_TIMEOUT_SECONDS,
            connect=REQUEST_CONNECT_TIMEOUT_SECONDS,
            sock_connect=REQUEST_CONNECT_TIMEOUT_SECONDS,
            sock_read=REQUEST_READ_TIMEOUT_SECONDS,
        )
        self._login_lock = asyncio.Lock()
        self._transport_log_times: dict[str, float] = {}
        self.last_login_auth_failed = False
        self.last_login_error: str | None = None
        self.ma_dviqly: Optional[str] = None
        self.ma_ddo: Optional[str] = None
        self.ma_khang: Optional[str] = None
        self.ten_khang: Optional[str] = None
        self.dien_thoai: Optional[str] = None
        self.dia_chi: Optional[str] = None
        self.hcmc_session: Optional[str] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = async_get_clientsession(self.hass)
        return self._session

    async def close(self) -> None:
        self._session = None

    def _log_transport_failure(self, operation: str, err: BaseException) -> None:
        now = monotonic()
        last = self._transport_log_times.get(operation, 0.0)
        self._transport_log_times[operation] = now
        if isinstance(err, TimeoutError):
            detail = f"timed out (total limit {REQUEST_TIMEOUT_SECONDS}s)"
        else:
            detail = str(err).strip() or err.__class__.__name__
        if now - last >= 15 * 60:
            _LOGGER.debug(
                "%s transport unavailable for %s (%s, %s): %s; cached data will be kept",
                operation,
                self.customer_id,
                self.region,
                self.base_url,
                detail,
            )
        _LOGGER.debug("Full %s transport exception", operation, exc_info=True)

    def _get_ma_dviqly_and_ma_ddo(self) -> tuple[str, str]:
        if self.region in {"HN", "NPC", "CPC"}:
            return (
                self.customer_id[:6] if self.customer_id else "",
                f"{self.customer_id}001" if self.customer_id else "",
            )
        if self.ma_dviqly and self.ma_ddo:
            return self.ma_dviqly, self.ma_ddo
        return (
            self.customer_id[:6] if self.customer_id else "",
            self.customer_id if self.customer_id else "",
        )

    async def login(self) -> bool:
        async with self._login_lock:
            self.last_login_auth_failed = False
            self.last_login_error = None
            self.access_token = None
            return await self._login_unlocked()

    async def _login_unlocked(self) -> bool:
        if self.region == "SPC":
            return await self._login_spc()
        try:
            session = await self._get_session()
            payload = {
                "username": self.username,
                "password": self.password,
                "deviceInfo": {
                    "deviceId": f"ha-{self.customer_id}",
                    "deviceType": "Android/HomeAssistant",
                },
            }
            headers = {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "user-agent": "okhttp/4.12.0",
                "connection": "Keep-Alive",
            }
            async with session.post(
                LOGIN_URL, json=payload, headers=headers, timeout=self._timeout
            ) as resp:
                if resp.status != 200:
                    self.last_login_auth_failed = resp.status in (401, 403)
                    self.last_login_error = f"HTTP {resp.status}"
                    return False
                data = await resp.json(content_type=None)
                if not isinstance(data, dict) or not data.get("success") or "data" not in data:
                    self.last_login_auth_failed = True
                    self.last_login_error = "EVN rejected the supplied credentials/account"
                    return False
                envelope = data.get("data") or {}
                access_token = envelope.get("accessToken")
                if not access_token:
                    self.last_login_auth_failed = True
                    self.last_login_error = "No access token returned"
                    return False
                user_data = envelope.get("data") or {}
                ma_kh_login = str(user_data.get("maKhang") or "")
                self.ma_khang = ma_kh_login
                self.ten_khang = user_data.get("tenKhang")
                self.dien_thoai = user_data.get("dthoai")
                self.dia_chi = user_data.get("diaChi")
                if self.region == "HN":
                    self.ma_dviqly = None
                    self.ma_ddo = None
                elif ma_kh_login:
                    self.ma_dviqly = ma_kh_login[:6]
                    self.ma_ddo = ma_kh_login
                else:
                    self.ma_dviqly = self.customer_id[:6]
                    self.ma_ddo = self.customer_id
                if ma_kh_login and ma_kh_login != self.customer_id:
                    if not await self._switch_account(access_token):
                        return False
                else:
                    self.access_token = access_token
                if self.region == "HCMC" and not await self._login_hcmc_session():
                    return False
                return True
        except (TimeoutError, aiohttp.ClientError) as err:
            self.last_login_error = (
                f"EVN login timed out after {REQUEST_TIMEOUT_SECONDS}s"
                if isinstance(err, TimeoutError)
                else f"EVN login transport error: {err}"
            )
            self._log_transport_failure("login", err)
            return False
        except Exception as err:  # noqa: BLE001
            self.last_login_error = str(err)
            _LOGGER.error("EVN login request failed: %s", err, exc_info=True)
            return False

    async def _login_spc(self) -> bool:
        try:
            session = await self._get_session()
            payload = {
                "strUsername": self.username,
                "strPassword": self.password,
                "strDeviceID": self.customer_id,
            }
            headers = {
                "accept": "application/json",
                "content-type": "application/json",
                "user-agent": "evnapp/59 CFNetwork/1240.0.4 Darwin/20.6.0",
                "accept-language": "vi-vn",
                "connection": "keep-alive",
            }
            async with session.post(
                SPC_LOGIN_URL, json=payload, headers=headers, timeout=self._timeout
            ) as resp:
                if resp.status != 200:
                    self.last_login_auth_failed = resp.status in (401, 403)
                    self.last_login_error = f"SPC HTTP {resp.status}"
                    return False
                data = await resp.json(content_type=None)
                token = data.get("token") if isinstance(data, dict) else None
                ma_kh_login = data.get("maKH", "") if isinstance(data, dict) else ""
                if not token or not ma_kh_login:
                    self.last_login_auth_failed = True
                    self.last_login_error = "SPC rejected the supplied credentials/account"
                    return False
                self.access_token = token
                self.ma_khang = ma_kh_login
                self.ma_dviqly = data.get("maDonVi") or ma_kh_login[:6]
                self.ma_ddo = ma_kh_login
                self.ten_khang = data.get("tenKH")
                self.dien_thoai = data.get("dienThoai")
                return True
        except (TimeoutError, aiohttp.ClientError) as err:
            self.last_login_error = (
                f"SPC login timed out after {REQUEST_TIMEOUT_SECONDS}s"
                if isinstance(err, TimeoutError)
                else f"SPC login transport error: {err}"
            )
            self._log_transport_failure("SPC login", err)
            return False
        except Exception as err:  # noqa: BLE001
            self.last_login_error = str(err)
            _LOGGER.error("SPC login request failed: %s", err, exc_info=True)
            return False

    async def _login_hcmc_session(self) -> bool:
        try:
            session = await self._get_session()
            url = "https://cskh.evnhcmc.vn/Dangnhap/checkLG"
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/104 Safari/537.36",
                "Accept": "application/json",
                "Connection": "keep-alive",
            }
            async with session.post(
                url,
                data={"u": self.username, "p": self.password},
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                if resp.status != 200:
                    self.last_login_auth_failed = resp.status in (401, 403)
                    self.last_login_error = f"HCMC HTTP {resp.status}"
                    return False
                # Prefer the aiohttp cookie jar, fall back to raw Set-Cookie.
                cookie = resp.cookies.get("evn_session")
                if cookie is not None:
                    self.hcmc_session = cookie.value
                if not self.hcmc_session:
                    raw = resp.headers.get("Set-Cookie", "")
                    marker = "evn_session="
                    if marker in raw:
                        self.hcmc_session = raw.split(marker, 1)[1].split(";", 1)[0].strip()
                if not self.hcmc_session:
                    self.last_login_error = "HCMC session cookie was not returned"
                    return False
                return True
        except (TimeoutError, aiohttp.ClientError) as err:
            self.last_login_error = str(err)
            self._log_transport_failure("HCMC login", err)
            return False
        except Exception as err:  # noqa: BLE001
            self.last_login_error = str(err)
            _LOGGER.error("HCMC login request failed: %s", err, exc_info=True)
            return False

    async def _switch_account(self, token: str) -> bool:
        try:
            session = await self._get_session()
            url = f"https://cskh.evn.com.vn/cskh/v1/user/switch/{self.customer_id}"
            headers = {
                "accept": "application/json, text/plain, */*",
                "connection": "Keep-Alive",
                "user-agent": "okhttp/4.12.0",
                "authorization": f"Bearer {token}",
            }
            async with session.get(url, headers=headers, timeout=self._timeout) as resp:
                if resp.status != 200:
                    self.last_login_auth_failed = resp.status in (401, 403)
                    self.last_login_error = f"Switch account HTTP {resp.status}"
                    return False
                data = await resp.json(content_type=None)
                if not isinstance(data, dict) or not data.get("success") or "data" not in data:
                    self.last_login_error = "EVN account switch was rejected"
                    return False
                envelope = data["data"]
                new_token = envelope.get("accessToken")
                if not new_token:
                    self.last_login_error = "No access token returned by account switch"
                    return False
                self.access_token = new_token
                user_data = envelope.get("data") or {}
                self.ma_khang = str(user_data.get("maKhang") or "")
                self.ten_khang = user_data.get("tenKhang")
                self.dien_thoai = user_data.get("dthoai")
                self.dia_chi = user_data.get("diaChi")
                if self.region == "HN":
                    self.ma_dviqly = None
                    self.ma_ddo = None
                elif self.ma_khang:
                    self.ma_dviqly = self.ma_khang[:6]
                    self.ma_ddo = self.ma_khang
                else:
                    self.ma_dviqly = self.customer_id[:6]
                    self.ma_ddo = self.customer_id
                return True
        except (TimeoutError, aiohttp.ClientError) as err:
            self.last_login_error = str(err)
            self._log_transport_failure("account switch", err)
            return False
        except Exception as err:  # noqa: BLE001
            self.last_login_error = str(err)
            _LOGGER.error("EVN account switch request failed: %s", err, exc_info=True)
            return False

    @staticmethod
    def _convert_spc_to_standard_format(records: list) -> list:
        converted: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            row = dict(record)
            if "strTime" in record:
                row["NGAY"] = record["strTime"]
            if record.get("dGiaoBT"):
                row["CHISO_MOI"] = record["dGiaoBT"]
                row["CHISO"] = record["dGiaoBT"]
            if "dSanLuongBT" in record:
                row["DIEN_TIEU_THU"] = record["dSanLuongBT"]
                row["SAN_LUONG"] = record["dSanLuongBT"]
            converted.append(row)
        return converted

    @staticmethod
    def _convert_spc_outage_to_standard_format(records: list) -> list:
        converted: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            row = dict(record)
            for source, date_key, time_key in (
                ("strTuNgay", "NGAY_BAT_DAU", "THOI_GIAN_BAT_DAU"),
                ("strDenNgay", "NGAY_KET_THUC", "THOI_GIAN_KET_THUC"),
            ):
                value = str(record.get(source) or "").strip()
                if "ngày" in value:
                    time_part, date_part = value.split("ngày", 1)
                    row[time_key] = time_part.strip()
                    row[date_key] = date_part.strip()
            if "strLyDoMatDien" in record:
                row["LY_DO"] = record["strLyDoMatDien"]
                row["ly_do"] = record["strLyDoMatDien"]
            if "strDiaChi" in record:
                row["DIA_CHI"] = record["strDiaChi"]
                row["KHU_VUC"] = record["strDiaChi"]
            converted.append(row)
        return converted

    @staticmethod
    def _convert_hcmc_to_standard_format(records: list) -> list:
        converted: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            row = dict(record)
            if record.get("ngayFull"):
                row["NGAY"] = record["ngayFull"]
            reading = _optional_float(record.get("tong_p_giao"))
            if reading is not None:
                row["CHISO_MOI"] = reading
                row["CHISO"] = reading
            consumption = _optional_float(record.get("Tong"))
            if consumption is not None:
                row["DIEN_TIEU_THU"] = consumption
                row["SAN_LUONG"] = consumption
            converted.append(row)
        return converted

    @staticmethod
    def _convert_cpc_outage_to_standard_format(records: list) -> list:
        converted: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            row = dict(record)
            # CPC responses vary by gateway version; keep original keys and add
            # aliases consumed by the coordinator/database when known.
            row.setdefault("NGAY_BAT_DAU", record.get("NGAY_BDAU") or record.get("ngay_bat_dau"))
            row.setdefault("NGAY_KET_THUC", record.get("NGAY_KTHUC") or record.get("ngay_ket_thuc"))
            row.setdefault("THOI_GIAN_BAT_DAU", record.get("GIO_BDAU") or record.get("thoi_gian_bat_dau"))
            row.setdefault("THOI_GIAN_KET_THUC", record.get("GIO_KTHUC") or record.get("thoi_gian_ket_thuc"))
            row.setdefault("LY_DO", record.get("LY_DO") or record.get("ly_do"))
            row.setdefault("KHU_VUC", record.get("KHU_VUC") or record.get("DIA_CHI") or record.get("khu_vuc"))
            converted.append(row)
        return converted

    async def get_chisongay(self, from_date: str, to_date: str) -> Optional[Dict[str, Any]]:
        if not self.access_token and not await self.login():
            return None
        try:
            session = await self._get_session()
            if self.region == "HCMC":
                if not self.hcmc_session and not await self.login():
                    return None
                url = f"{self.base_url}/Tracuu/ajax_dienNangTieuThuTheoNgay"
                headers = {
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/104 Safari/537.36",
                    "Accept": "application/json",
                    "Cookie": f"evn_session={self.hcmc_session}",
                }
                payload = {
                    "input_makh": self.customer_id,
                    "input_tungay": from_date,
                    "input_denngay": to_date,
                }
                async with session.post(url, data=payload, headers=headers, timeout=self._timeout) as resp:
                    if resp.status != 200:
                        return None
                    data = json.loads(await resp.text(encoding="utf-8", errors="replace"))
                    if data.get("state") == "success" and isinstance(data.get("data"), dict):
                        records = data["data"].get("sanluong_tungngay", [])
                        if isinstance(records, list):
                            return {"data": self._convert_hcmc_to_standard_format(records)}
                    return None
            if self.region == "SPC":
                start = datetime.strptime(from_date, "%d/%m/%Y") - timedelta(days=1)
                end = datetime.strptime(to_date, "%d/%m/%Y")
                url = f"{self.base_url}/api/NghiepVu/LayThongTinSanLuongTheoNgay_v2"
                params = {
                    "strMaDiemDo": f"{self.customer_id}001",
                    "strFromDate": start.strftime("%Y%m%d"),
                    "strToDate": end.strftime("%Y%m%d"),
                }
                headers = {
                    "accept": "application/json, text/plain, */*",
                    "user-agent": "okhttp/4.12.0",
                    "authorization": f"Bearer {self.access_token}",
                }
                data = await self._request_json_with_reauth("GET", url, headers=headers, params=params)
                if isinstance(data, list):
                    return {"data": self._convert_spc_to_standard_format(data)}
                return data
            url = f"{self.base_url}/api/evn/tracuu/chisongay"
            ma_dviqly, ma_ddo = self._get_ma_dviqly_and_ma_ddo()
            payload = {
                "MA_DVIQLY": ma_dviqly,
                "MA_DDO": ma_ddo,
                "TU_NGAY": from_date,
                "DEN_NGAY": to_date,
            }
            headers = {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "user-agent": "okhttp/4.12.0",
                "authorization": f"Bearer {self.access_token}",
            }
            return await self._request_json_with_reauth("POST", url, headers=headers, json_body=payload)
        except (TimeoutError, aiohttp.ClientError) as err:
            self._log_transport_failure("get_chisongay", err)
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("get_chisongay error: %s", err, exc_info=True)
            return None

    async def get_chisothang(self, month: int, year: int) -> Optional[Dict[str, Any]]:
        if not self.access_token and not await self.login():
            return None
        try:
            if self.region in {"HCMC", "SPC"}:
                month_start = datetime(year, month, 1)
                last_day = monthrange(year, month)[1]
                month_end = datetime(year, month, last_day)
                from_date = (
                    month_start.strftime("%d/%m/%Y")
                    if self.region == "SPC"
                    else (month_start - timedelta(days=1)).strftime("%d/%m/%Y")
                )
                daily = await self.get_chisongay(from_date, month_end.strftime("%d/%m/%Y"))
                rows = daily.get("data") if isinstance(daily, dict) else None
                if not isinstance(rows, list) or not rows:
                    return None
                dated: list[tuple[datetime, dict[str, Any]]] = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    dt = _daily_record_date(row)
                    if dt is not None:
                        dated.append((dt, row))
                dated.sort(key=lambda item: item[0])
                daily_values: dict[Any, float] = {}
                for row_date, row in dated:
                    if row_date.year != year or row_date.month != month:
                        continue
                    value = next(
                        (
                            row.get(key)
                            for key in ("DIEN_TIEU_THU", "SAN_LUONG", "dSanLuongBT", "Tong")
                            if row.get(key) is not None
                        ),
                        None,
                    )
                    parsed = _optional_float(value)
                    if parsed is not None and parsed >= 0:
                        daily_values[row_date.date()] = parsed
                if daily_values:
                    consumption = round(sum(daily_values.values()), 6)
                    in_month = [(d, r) for d, r in dated if d.year == year and d.month == month]
                    first_record = in_month[0][1] if in_month else dated[0][1]
                    last_record = in_month[-1][1] if in_month else dated[-1][1]
                    chi_so_cu = _optional_float(first_record.get("CHISO_MOI") or first_record.get("CHISO") or first_record.get("dGiaoBT"))
                    chi_so_moi = _optional_float(last_record.get("CHISO_MOI") or last_record.get("CHISO") or last_record.get("dGiaoBT"))
                    start_date = min(daily_values)
                    end_date = max(daily_values)
                else:
                    first_record = dated[0][1]
                    last_record = dated[-1][1]
                    chi_so_cu = _optional_float(first_record.get("CHISO_MOI") or first_record.get("CHISO") or first_record.get("dGiaoBT"))
                    chi_so_moi = _optional_float(last_record.get("CHISO_MOI") or last_record.get("CHISO") or last_record.get("dGiaoBT"))
                    if chi_so_cu is None or chi_so_moi is None or chi_so_moi < chi_so_cu:
                        return None
                    consumption = round(chi_so_moi - chi_so_cu, 6)
                    start_date = month_start.date()
                    end_date = month_end.date()
                return {
                    "data": [
                        {
                            "THANG": month,
                            "NAM": year,
                            "DIEN_TTHU": consumption,
                            "CHISO_CU": chi_so_cu,
                            "CHISO_MOI": chi_so_moi,
                            "TU_NGAY": start_date.strftime("%d/%m/%Y"),
                            "DEN_NGAY": end_date.strftime("%d/%m/%Y"),
                        }
                    ]
                }
            url = f"{self.base_url}/api/evn/tracuu/chisothang"
            ma_dviqly, ma_ddo = self._get_ma_dviqly_and_ma_ddo()
            thang_nam = f"{month:02d}/{year}"
            payload = {
                "MA_DVIQLY": ma_dviqly,
                "MA_DDO": ma_ddo,
                "TU_THANG_NAM": thang_nam,
                "DEN_THANG_NAM": thang_nam,
            }
            headers = {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "user-agent": "okhttp/4.12.0",
                "authorization": f"Bearer {self.access_token}",
            }
            return await self._request_json_with_reauth("POST", url, headers=headers, json_body=payload)
        except (TimeoutError, aiohttp.ClientError) as err:
            self._log_transport_failure("get_chisothang", err)
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("get_chisothang error: %s", err, exc_info=True)
            return None

    async def get_hoadon(self, month: int | None = None, year: int | None = None) -> Optional[Dict[str, Any]]:
        """Get bills. Historical lookups never represent current debt."""
        if (month is None) != (year is None):
            raise ValueError("month and year must be supplied together")
        if month is not None and (not 1 <= month <= 12 or year is None or not 2000 <= year <= 2100):
            raise ValueError("invalid invoice month/year")
        if month is not None and self.region in {"HCMC", "SPC"}:
            return None
        if not self.access_token and not await self.login():
            return None
        try:
            session = await self._get_session()
            if self.region == "HCMC":
                if not self.hcmc_session and not await self.login():
                    return None
                url = f"{self.base_url}/Tracuu/kiemTraNo"
                headers = {
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/104 Safari/537.36",
                    "Accept": "application/json",
                    "Cookie": f"evn_session={self.hcmc_session}",
                }
                async with session.post(
                    url,
                    data={"input_makh": self.customer_id},
                    headers=headers,
                    timeout=self._timeout,
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = json.loads(await resp.text(encoding="utf-8", errors="replace"))
                    if data.get("state") == "success" and isinstance(data.get("data"), dict):
                        body = data["data"]
                        if body.get("isNo") == 1 and isinstance(body.get("info_no"), list):
                            return {"data": body["info_no"]}
                        return {"data": []}
                    return None
            if self.region == "SPC":
                url = f"{self.base_url}/api/NghiepVu/TraCuuNoHoaDon"
                headers = {
                    "User-Agent": "evnapp/59 CFNetwork/1240.0.4 Darwin/20.6.0",
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/json",
                    "Accept-Language": "vi-vn",
                }
                data = await self._request_json_with_reauth(
                    "GET", url, headers=headers, params={"strMaKH": self.customer_id}
                )
                if isinstance(data, list):
                    return {"data": data}
                return data
            url = f"{self.base_url}/api/evn/tracuu/hoadon"
            headers = {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "user-agent": "okhttp/4.12.0",
                "authorization": f"Bearer {self.access_token}",
            }
            payload: dict[str, Any] | None = None
            if month is not None and year is not None:
                ma_dviqly, ma_ddo = self._get_ma_dviqly_and_ma_ddo()
                thang_nam = f"{month:02d}/{year}"
                payload = {
                    "MA_DVIQLY": ma_dviqly,
                    "MA_DDO": ma_ddo,
                    "TU_THANG_NAM": thang_nam,
                    "DEN_THANG_NAM": thang_nam,
                }
            return await self._request_json_with_reauth("POST", url, headers=headers, json_body=payload)
        except (TimeoutError, aiohttp.ClientError) as err:
            self._log_transport_failure("get_hoadon", err)
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("get_hoadon error: %s", err, exc_info=True)
            return None

    async def get_ngungcapdien(self, from_date: str, to_date: str) -> Optional[Dict[str, Any]]:
        if not self.access_token and not await self.login():
            return None
        try:
            if self.region == "SPC":
                url = f"{self.base_url}/api/NghiepVu/TraCuuLichNgungGiamCungCapDien"
                headers = {
                    "User-Agent": "evnapp/59 CFNetwork/1240.0.4 Darwin/20.6.0",
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/json",
                    "Accept-Language": "vi-vn",
                }
                data = await self._request_json_with_reauth(
                    "GET", url, headers=headers, params={"strMaKH": self.customer_id}
                )
                if isinstance(data, list):
                    return {"data": self._convert_spc_outage_to_standard_format(data)}
                return data
            url = f"{self.base_url}/api/evn/tracuu/ngungcapdien"
            headers = {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "user-agent": "okhttp/4.12.0",
                "authorization": f"Bearer {self.access_token}",
            }
            data = await self._request_json_with_reauth(
                "POST",
                url,
                headers=headers,
                json_body={"TU_NGAY": from_date, "DEN_NGAY": to_date},
            )
            if self.region == "CPC" and isinstance(data, dict) and isinstance(data.get("data"), list):
                return {"data": self._convert_cpc_outage_to_standard_format(data["data"])}
            return data
        except (TimeoutError, aiohttp.ClientError) as err:
            self._log_transport_failure("get_ngungcapdien", err)
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("get_ngungcapdien error: %s", err, exc_info=True)
            return None

    async def get_thongbao(self) -> Optional[list]:
        if not self.access_token and not await self.login():
            return None
        try:
            if self.region == "SPC":
                url = f"{self.base_url}/api/NghiepVu/LayDanhSachThongBaoKhachHang"
                headers = {
                    "accept": "application/json",
                    "authorization": f"Bearer {self.access_token}",
                    "user-agent": "evnapp/59 CFNetwork/1240.0.4 Darwin/20.6.0",
                }
                data = await self._request_json_with_reauth(
                    "GET",
                    url,
                    headers=headers,
                    params={"strMaKh": self.customer_id, "strRedId": ""},
                )
                rows = data.get("data") if isinstance(data, dict) else data
                if not isinstance(rows, list):
                    return [] if rows is not None else None
                normalized: list[dict[str, Any]] = []
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    row = dict(item)
                    title = str(item.get("strTieuDe") or item.get("TieuDe") or item.get("title") or "")
                    summary = str(item.get("strNoiDung") or item.get("noiDung") or item.get("summary") or "")
                    folded = f"{title} {summary}".casefold()
                    if "ngừng" in folded or "ngung" in folded or "mất điện" in folded or "mat dien" in folded:
                        category = "NGUNGCAP_DIEN"
                    elif "hóa đơn" in folded or "hoa don" in folded or "tiền điện" in folded or "tien dien" in folded:
                        category = "HOADON"
                    else:
                        category = "KHAC"
                    row.setdefault("title", title)
                    row.setdefault("summary", summary)
                    row.setdefault("notificationType", category)
                    row.setdefault("createdDate", item.get("strNgayThongBao") or item.get("ngayTao") or item.get("NgayTao"))
                    normalized.append(row)
                return normalized
            headers = {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "user-agent": "okhttp/4.12.0",
                "authorization": f"Bearer {self.access_token}",
            }
            data = await self._request_json_with_reauth(
                "POST", NOTIFICATION_URL, headers=headers, json_body={}
            )
            return data.get("data") if isinstance(data, dict) else None
        except (TimeoutError, aiohttp.ClientError) as err:
            self._log_transport_failure("get_thongbao", err)
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("get_thongbao error: %s", err, exc_info=True)
            return None

    async def _request_json_with_reauth(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any | None:
        session = await self._get_session()
        transient_retries = 0
        auth_refreshed = False
        while True:
            try:
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self._timeout,
                ) as resp:
                    if resp.status in (401, 403) and not auth_refreshed:
                        auth_refreshed = True
                        if not await self.login():
                            if self.last_login_auth_failed or self.hass.is_stopping:
                                return None
                            await asyncio.sleep(0.75)
                            if not await self.login():
                                return None
                        token_key = "Authorization" if "Authorization" in headers else "authorization"
                        headers[token_key] = f"Bearer {self.access_token}"
                        continue
                    if resp.status in {408, 429, 500, 502, 503, 504} and transient_retries < 1 and not self.hass.is_stopping:
                        transient_retries += 1
                        delay = 0.75
                        if resp.headers.get("Retry-After"):
                            try:
                                delay = min(max(float(resp.headers["Retry-After"]), 0.25), 2.0)
                            except ValueError:
                                pass
                        resp.release()
                        await asyncio.sleep(delay)
                        continue
                    if resp.status != 200:
                        text = await resp.text()
                        _LOGGER.debug("EVN request failed %s %s: HTTP %s %s", method, url, resp.status, text[:300])
                        return None
                    try:
                        return await resp.json(content_type=None)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        text = await resp.text()
                        try:
                            return json.loads(text)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            return None
            except asyncio.CancelledError:
                raise
            except (TimeoutError, aiohttp.ClientError):
                if transient_retries >= 1 or self.hass.is_stopping:
                    raise
                transient_retries += 1
                await asyncio.sleep(0.75)

    @property
    def invoice_resource_base_url(self) -> str:
        if self.region == "HCMC":
            return f"{self.base_url}/Tracuu/kiemTraNo"
        if self.region == "SPC":
            return f"{self.base_url}/api/NghiepVu/TraCuuNoHoaDon"
        return f"{self.base_url}/api/evn/tracuu/hoadon"

    @property
    def monthly_resource_base_url(self) -> str:
        if self.region in {"HCMC", "SPC"}:
            return f"{self.base_url.rstrip('/')}/"
        return f"{self.base_url}/api/evn/tracuu/chisothang"

    @property
    def notification_resource_base_url(self) -> str:
        if self.region == "SPC":
            return f"{self.base_url}/api/NghiepVu/LayDanhSachThongBaoKhachHang"
        return NOTIFICATION_URL

    async def download_file(self, url: str, *, base_url: str | None = None) -> bytes | None:
        if not url:
            return None
        raw_url = str(url).strip()
        if not raw_url:
            return None
        if raw_url.lower().startswith(("http://", "https://")):
            start_urls = [raw_url]
        else:
            source_base = base_url or f"{self.base_url.rstrip('/')}/"
            start_urls = [urljoin(source_base, raw_url)]
            parsed = urlsplit(source_base)
            if parsed.scheme and parsed.netloc:
                root_url = urljoin(f"{parsed.scheme}://{parsed.netloc}/", raw_url)
                if root_url not in start_urls:
                    start_urls.append(root_url)
        fallback: bytes | None = None
        visited: set[str] = set()
        for start_url in start_urls:
            content = await self._download_file_url(start_url, visited=visited, depth=0)
            if detect_invoice_type(content) is not None:
                return content
            if content and fallback is None:
                fallback = content
        return fallback

    async def _download_file_url(self, url: str, *, visited: set[str], depth: int) -> bytes | None:
        if depth > 3 or url in visited:
            return None
        visited.add(url)
        session = await self._get_session()
        headers = {
            "user-agent": "Mozilla/5.0 (Linux; Android 13) EVN-CSKH-Monitor/1.0",
            "accept": "application/pdf,image/png,image/*,application/octet-stream,text/html;q=0.8,*/*;q=0.5",
            "referer": f"{self.base_url}/",
        }
        host = (urlsplit(url).hostname or "").lower()
        safe_auth_host = any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in _EVN_AUTH_SUFFIXES)
        if safe_auth_host and self.access_token:
            headers["authorization"] = f"Bearer {self.access_token}"
        if safe_auth_host and self.region == "HCMC" and self.hcmc_session:
            headers["Cookie"] = f"evn_session={self.hcmc_session}"
        for attempt in range(2):
            try:
                async with session.get(url, headers=headers, timeout=self._timeout, allow_redirects=False) as resp:
                    if resp.status in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("Location")
                        return (
                            await self._download_file_url(
                                urljoin(str(resp.url), location), visited=visited, depth=depth + 1
                            )
                            if location
                            else None
                        )
                    if resp.status in (401, 403) and attempt == 0 and safe_auth_host:
                        if not await self.login():
                            return None
                        if self.access_token:
                            headers["authorization"] = f"Bearer {self.access_token}"
                        if self.region == "HCMC" and self.hcmc_session:
                            headers["Cookie"] = f"evn_session={self.hcmc_session}"
                        continue
                    if resp.status != 200:
                        return None
                    if resp.content_length is not None and resp.content_length > 20 * 1024 * 1024:
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > 20 * 1024 * 1024:
                            return None
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if not data:
                        return None
                    content_type = (resp.headers.get("Content-Type") or "").lower()
                    stripped = data[:256].lstrip()
                    if "application/json" in content_type or stripped.startswith((b"{", b"[")):
                        try:
                            envelope = json.loads(data.decode("utf-8", errors="replace"))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            envelope = None
                        if envelope is not None:
                            for kind, candidate in iter_attachment_candidates(envelope):
                                if kind == "base64":
                                    embedded = decode_base64_payload(candidate)
                                    if detect_invoice_type(embedded) is not None:
                                        return embedded
                                elif kind == "url":
                                    nested = await self._download_file_url(
                                        urljoin(str(resp.url), candidate), visited=visited, depth=depth + 1
                                    )
                                    if nested:
                                        return nested
                    if "text/html" in content_type or stripped.lower().startswith((b"<!doctype html", b"<html")):
                        for linked in extract_invoice_links_from_html(data, str(resp.url)):
                            nested = await self._download_file_url(linked, visited=visited, depth=depth + 1)
                            if nested:
                                return nested
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.debug("Invoice download failed %s: %s", url, err)
                return None
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Invoice download failed %s: %s", url, err)
                return None
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        try:
            return float(text.replace(".", "").replace(",", "."))
        except ValueError:
            return None


def _daily_record_date(record: dict[str, Any]) -> datetime | None:
    for key in ("NGAY", "ngay", "ngayFull", "strTime"):
        raw = record.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if "-" in text and "/" in text and text.count("/") >= 4:
            text = text.split("-")[-1]
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                pass
    return None
