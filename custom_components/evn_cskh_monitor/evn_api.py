"""Async API client for EVN CSKH Monitor."""

import asyncio
from calendar import monthrange
from datetime import date, datetime, timedelta
import json
import logging
from typing import Any, Dict, Optional
from urllib.parse import urljoin
from urllib.parse import urlsplit

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import REQUEST_TIMEOUT_SECONDS
from .invoice import (
    decode_base64_payload,
    detect_invoice_type,
    extract_invoice_links_from_html,
    iter_attachment_candidates,
)

_LOGGER = logging.getLogger(__name__)

# Base URLs for different regions
EVN_REGIONS = {
    "HN": "https://gwkong.evnhanoi.vn",
    "NPC": "https://apicskhevn.npc.com.vn",
    "CPC": "https://cskh-api.cpc.vn",
    "SPC": "https://api.cskh.evnspc.vn",
    "HCMC": "https://cskh.evnhcmc.vn",  # HCMC dùng base URL riêng cho API data
}

# Common login URL
LOGIN_URL = "https://cskh.evn.com.vn/cskh/v1/auth/login"

# SPC có cổng đăng nhập riêng: tài khoản SPC không tồn tại trên cổng chung
# cskh.evn.com.vn nên phải xác thực thẳng ở api.cskh.evnspc.vn
SPC_LOGIN_URL = "https://api.cskh.evnspc.vn/api/user/authenticate"

# Feed "Thông báo" dùng chung của app EVN (cùng cổng đăng nhập cskh.evn.com.vn).
# Lịch ngừng cấp điện BÁO TRƯỚC chỉ xuất hiện ở đây; API tra cứu lịch ngừng cấp
# điện (ngungcapdien) trả về rỗng với các đợt cắt được thông báo. getAllByUser
# trả về mọi mã khách hàng của cùng số điện thoại nên phải lọc theo mã đang cấu
# hình (xem coordinator._save_notification_outages).
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
    """EVN API Client"""

    def __init__(self, hass, region: str, username: str, password: str, customer_id: str):
        """Initialize EVN API client."""
        self.hass = hass
        self.region = region.upper()
        self.username = username
        self.password = password
        self.customer_id = customer_id
        self.base_url = EVN_REGIONS.get(self.region)
        self.access_token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        self._login_lock = asyncio.Lock()
        self.last_login_auth_failed = False
        self.last_login_error: str | None = None
        self.ma_dviqly: Optional[str] = None  # Lưu từ login response
        self.ma_ddo: Optional[str] = None  # Lưu từ login response (maKhang hoặc maHdong)
        self.ma_khang: Optional[str] = None  # Lưu từ login response
        self.ten_khang: Optional[str] = None  # Tên khách hàng, từ login response
        self.dien_thoai: Optional[str] = None  # Số điện thoại, từ login response
        self.dia_chi: Optional[str] = None  # Địa chỉ, từ login response
        self.hcmc_session: Optional[str] = None  # Session cookie cho HCMC

        if not self.base_url:
            raise ValueError(f"Invalid region: {region}")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return Home Assistant's shared aiohttp session."""
        if self._session is None:
            self._session = async_get_clientsession(self.hass)
        return self._session

    async def close(self) -> None:
        """Release local references; Home Assistant owns the shared session."""
        self._session = None

    def _get_ma_dviqly_and_ma_ddo(self):
        """Get MA_DVIQLY and MA_DDO for API payload based on region.
        
        Returns:
            tuple: (MA_DVIQLY, MA_DDO)
        """
        if self.region == "HN":
            # HN: dùng customer_id[:6] và customer_id + "001"
            # (như nestup_evn)
            ma_dviqly = self.customer_id[:6] if self.customer_id else ""
            ma_ddo = f"{self.customer_id}001" if self.customer_id else ""
        elif self.region == "NPC":
            # NPC: dùng customer_id[:6] và customer_id + "001"
            # (như nestup_evn)
            ma_dviqly = self.customer_id[:6] if self.customer_id else ""
            ma_ddo = f"{self.customer_id}001" if self.customer_id else ""
        elif self.region == "CPC":
            # CPC: dùng customer_id[:6] và customer_id + "001"
            # (như test đã xác nhận)
            ma_dviqly = self.customer_id[:6] if self.customer_id else ""
            ma_ddo = f"{self.customer_id}001" if self.customer_id else ""
        elif self.region == "SPC":
            # SPC: dùng maKhang để extract (cho các API khác)
            # Nhưng với chisongay, SPC dùng endpoint riêng với strMaDiemDo
            if self.ma_dviqly and self.ma_ddo:
                ma_dviqly = self.ma_dviqly
                ma_ddo = self.ma_ddo
            else:
                # Fallback: extract từ customer_id
                ma_dviqly = self.customer_id[:6] if self.customer_id else ""
                ma_ddo = self.customer_id if self.customer_id else ""
        else:
            # Fallback: extract từ customer_id
            ma_dviqly = self.customer_id[:6] if self.customer_id else ""
            ma_ddo = self.customer_id if self.customer_id else ""
        
        return ma_dviqly, ma_ddo

    async def login(self) -> bool:
        """Login to EVN with serialized authentication attempts."""
        async with self._login_lock:
            self.last_login_auth_failed = False
            self.last_login_error = None
            self.access_token = None
            return await self._login_unlocked()

    async def _login_unlocked(self) -> bool:
        """Perform one EVN authentication attempt."""
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

            async with session.post(LOGIN_URL, json=payload, headers=headers, timeout=self._timeout) as resp:
                if resp.status != 200:
                    self.last_login_auth_failed = resp.status in (401, 403)
                    self.last_login_error = f"HTTP {resp.status}"
                    _LOGGER.error("EVN login failed with status %s", resp.status)
                    return False

                data = await resp.json()
                
                if not data.get("success") or "data" not in data:
                    self.last_login_auth_failed = True
                    self.last_login_error = "EVN rejected the supplied credentials/account"
                    _LOGGER.error("EVN login was rejected by the authentication service")
                    return False

                access_token = data["data"].get("accessToken")
                if not access_token:
                    self.last_login_auth_failed = True
                    self.last_login_error = "No access token returned"
                    _LOGGER.error("EVN login response did not contain an access token")
                    return False

                # Lưu maKhang từ login response
                user_data = data["data"].get("data", {})
                ma_kh_login = user_data.get("maKhang", "")
                self.ma_khang = ma_kh_login
                self.ten_khang = user_data.get("tenKhang")
                self.dien_thoai = user_data.get("dthoai")
                self.dia_chi = user_data.get("diaChi")
                
                # Với HN: không dùng maDviqly/maHdong từ login,
                # sẽ dùng customer_id trực tiếp
                # Với NPC/CPC/SPC: dùng maKhang để extract
                if self.region == "HN":
                    # HN: không lưu ma_dviqly/ma_ddo,
                    # sẽ dùng customer_id trực tiếp trong API calls
                    self.ma_dviqly = None
                    self.ma_ddo = None
                elif ma_kh_login:
                    # NPC/CPC/SPC: dùng maKhang để extract
                    self.ma_dviqly = ma_kh_login[:6]
                    self.ma_ddo = ma_kh_login
                else:
                    # Fallback: extract từ customer_id
                    self.ma_dviqly = (
                        self.customer_id[:6] if self.customer_id else ""
                    )
                    self.ma_ddo = self.customer_id if self.customer_id else ""

                if ma_kh_login and ma_kh_login != self.customer_id:
                    _LOGGER.info(f"Switching account from {ma_kh_login} to {self.customer_id}")
                    if not await self._switch_account(access_token):
                        return False
                    # Sau khi switch, maKhang mới đã được cập nhật trong _switch_account
                else:
                    self.access_token = access_token

                # HCMC cần lấy session cookie từ login endpoint riêng
                if self.region == "HCMC":
                    if not await self._login_hcmc_session():
                        _LOGGER.error("Failed to get HCMC session cookie")
                        return False

                if self.region == "HN":
                    _LOGGER.info(
                        f"Login successful for {self.customer_id} "
                        "(HN: will use customer_id[:6] and customer_id+'001')"
                    )
                elif self.region == "HCMC":
                    _LOGGER.info(
                        f"Login successful for {self.customer_id} "
                        f"(HCMC: session cookie obtained)"
                    )
                else:
                    _LOGGER.info(
                        f"Login successful for {self.customer_id}, "
                        f"maDviqly={self.ma_dviqly}, maDdo={self.ma_ddo}"
                    )
                return True

        except Exception as err:
            self.last_login_error = str(err)
            _LOGGER.error("EVN login request failed: %s", err, exc_info=True)
            return False

    async def _login_spc(self) -> bool:
        """Login to EVNSPC endpoint to get access token.

        SPC không dùng cổng đăng nhập chung cskh.evn.com.vn. Endpoint riêng
        trả về token cùng maKH/maDonVi của khách hàng.
        """
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
                    _LOGGER.error("SPC login failed with status %s", resp.status)
                    return False

                data = await resp.json(content_type=None)

                token = data.get("token") if isinstance(data, dict) else None
                ma_kh_login = data.get("maKH", "") if isinstance(data, dict) else ""

                # SPC trả về 200 với maKH rỗng khi sai tài khoản/mật khẩu
                if not token or not ma_kh_login:
                    self.last_login_auth_failed = True
                    self.last_login_error = "SPC rejected the supplied credentials/account"
                    _LOGGER.error("SPC login was rejected by the authentication service")
                    return False

                self.access_token = token
                self.ma_khang = ma_kh_login
                self.ma_dviqly = data.get("maDonVi") or ma_kh_login[:6]
                self.ma_ddo = ma_kh_login
                self.ten_khang = data.get("tenKH")
                self.dien_thoai = data.get("dienThoai")

                _LOGGER.info(
                    f"Login successful for {self.customer_id}, "
                    f"maDviqly={self.ma_dviqly}, maDdo={self.ma_ddo}"
                )
                return True

        except Exception as err:
            self.last_login_error = str(err)
            _LOGGER.error("SPC login request failed: %s", err, exc_info=True)
            return False

    async def _login_hcmc_session(self) -> bool:
        """Login to HCMC endpoint to get session cookie.
        
        HCMC requires a separate login to get session cookie for API calls.
        This is called after successful login with common base URL.
        """
        try:
            session = await self._get_session()
            hcmc_login_url = "https://cskh.evnhcmc.vn/Dangnhap/checkLG"
            
            payload = {"u": self.username, "p": self.password}
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            }
            
            async with session.post(hcmc_login_url, data=payload, headers=headers, timeout=self._timeout) as resp:
                if resp.status != 200:
                    self.last_login_auth_failed = resp.status in (401, 403)
                    self.last_login_error = f"HCMC HTTP {resp.status}"
                    _LOGGER.error("HCMC login failed with status %s", resp.status)
                    return False
                
                # Lấy session cookie từ HCMC login (giống test_hcmc.py)
                cookies = resp.headers.get("Set-Cookie", "")
                evn_session = None
                for cookie in cookies.split(";"):
                    if "evn_session=" in cookie:
                        evn_session = cookie.split("evn_session=")[-1].strip()
                        if ";" in evn_session:
                            evn_session = evn_session.split(";")[0]
                
                if evn_session:
                    self.hcmc_session = evn_session
                    _LOGGER.info("HCMC session cookie obtained successfully")
                    return True
                else:
                    self.last_login_error = "HCMC login response contained no session cookie"
                    _LOGGER.error("No evn_session cookie in HCMC login response")
                    return False
                    
        except Exception as err:
            self.last_login_error = str(err)
            _LOGGER.error("HCMC login request failed: %s", err, exc_info=True)
            return False

    async def _switch_account(self, token: str) -> bool:
        """Switch to different customer account."""
        try:
            session = await self._get_session()
            switch_url = f"https://cskh.evn.com.vn/cskh/v1/user/switch/{self.customer_id}"

            headers = {
                "accept": "application/json, text/plain, */*",
                "accept-encoding": "gzip",
                "connection": "Keep-Alive",
                "user-agent": "okhttp/4.12.0",
                "authorization": f"Bearer {token}",
            }

            async with session.get(switch_url, headers=headers, timeout=self._timeout) as resp:
                if resp.status != 200:
                    self.last_login_auth_failed = resp.status in (401, 403)
                    self.last_login_error = f"Switch account HTTP {resp.status}"
                    _LOGGER.error("EVN account switch failed with status %s", resp.status)
                    return False

                data = await resp.json()
                
                if not data.get("success") or "data" not in data:
                    self.last_login_error = "EVN account switch was rejected"
                    _LOGGER.error("EVN account switch was rejected")
                    return False

                new_token = data["data"].get("accessToken")
                if not new_token:
                    self.last_login_error = "No access token returned by account switch"
                    _LOGGER.error("No access token in EVN account-switch response")
                    return False

                self.access_token = new_token
                
                # Lấy lại maKhang từ switch response (chỉ để tham khảo)
                switch_user_data = data["data"].get("data", {})
                self.ma_khang = switch_user_data.get("maKhang", "")
                self.ten_khang = switch_user_data.get("tenKhang")
                self.dien_thoai = switch_user_data.get("dthoai")
                self.dia_chi = switch_user_data.get("diaChi")
                
                # Với HN: không lưu ma_dviqly/ma_ddo, sẽ dùng customer_id trực tiếp trong API calls
                # Với NPC/CPC/SPC: dùng maKhang để extract
                if self.region == "HN":
                    # HN: không lưu ma_dviqly/ma_ddo
                    self.ma_dviqly = None
                    self.ma_ddo = None
                elif self.ma_khang:
                    # NPC/CPC/SPC: dùng maKhang để extract MA_DVIQLY và MA_DDO
                    self.ma_dviqly = self.ma_khang[:6]
                    self.ma_ddo = self.ma_khang
                else:
                    # Fallback: extract từ customer_id
                    self.ma_dviqly = self.customer_id[:6] if self.customer_id else ""
                    self.ma_ddo = self.customer_id if self.customer_id else ""
                
                _LOGGER.info(f"Account switched successfully to {self.customer_id}, maDviqly={self.ma_dviqly}, maDdo={self.ma_ddo}")
                return True

        except Exception as err:
            self.last_login_error = str(err)
            _LOGGER.error("EVN account switch request failed: %s", err, exc_info=True)
            return False

    def _convert_spc_to_standard_format(self, records: list) -> list:
        """Convert SPC API response format to standard format.
        
        SPC format: {
            "strTime": "dd/mm/yyyy",
            "dGiaoBT": 1234.56,
            "dSanLuongBT": 10.5
        }
        
        Standard format: {
            "NGAY": "dd/mm/yyyy",
            "CHISO_MOI": 1234.56,
            "DIEN_TIEU_THU": 10.5
        }
        """
        converted = []
        for record in records:
            if not isinstance(record, dict):
                continue
            
            converted_record = {}
            # Copy all existing fields
            converted_record.update(record)
            
            # Convert strTime -> NGAY
            if "strTime" in record:
                converted_record["NGAY"] = record["strTime"]
            
            # Convert dGiaoBT -> CHISO_MOI and CHISO
            # Bản ghi gộp nhiều ngày (strTime dạng "08/10/2025-09/10/2025")
            # không có chỉ số chốt, dGiaoBT = 0: bỏ qua để không ghi đè
            # chuỗi chỉ số bằng số 0.
            if record.get("dGiaoBT"):
                converted_record["CHISO_MOI"] = record["dGiaoBT"]
                converted_record["CHISO"] = record["dGiaoBT"]
            
            # Convert dSanLuongBT -> DIEN_TIEU_THU and SAN_LUONG
            if "dSanLuongBT" in record:
                converted_record["DIEN_TIEU_THU"] = record["dSanLuongBT"]
                converted_record["SAN_LUONG"] = record["dSanLuongBT"]
            
            converted.append(converted_record)
        
        return converted

    def _convert_spc_outage_to_standard_format(self, records: list) -> list:
        """Convert SPC outage API response format to standard format.
        
        SPC format: {
            "strTuNgay": "08:00:00 ngày 01/02/2026",
            "strDenNgay": "08:15:00 ngày 01/02/2026",
            "strThoiGianMatDien": "từ 08:00:00 ngày 01/02/2026 đến 08:15:00 ngày 01/02/2026",
            "strLyDoMatDien": "Bảo trì, sửa chữa lưới điện",
            "strDiaChi": "14.AB/38-39/7.H/1.H-T473-KP Tân Trà..."
        }
        
        Standard format: {
            "NGAY_BAT_DAU": "01/02/2026",
            "NGAY_KET_THUC": "01/02/2026",
            "THOI_GIAN_BAT_DAU": "08:00:00",
            "THOI_GIAN_KET_THUC": "08:15:00",
            "LY_DO": "Bảo trì, sửa chữa lưới điện",
            "DIA_CHI": "14.AB/38-39/7.H/1.H-T473-KP Tân Trà..."
        }
        """
        converted = []
        for record in records:
            if not isinstance(record, dict):
                continue
            
            converted_record = {}
            # Copy all existing fields
            converted_record.update(record)
            
            # Parse strTuNgay: "08:00:00 ngày 01/02/2026" -> NGAY_BAT_DAU="01/02/2026", THOI_GIAN_BAT_DAU="08:00:00"
            if "strTuNgay" in record and record["strTuNgay"]:
                tu_ngay = str(record["strTuNgay"]).strip()
                # Extract time and date: "08:00:00 ngày 01/02/2026"
                if "ngày" in tu_ngay:
                    parts = tu_ngay.split("ngày")
                    if len(parts) == 2:
                        time_part = parts[0].strip()
                        date_part = parts[1].strip()
                        converted_record["THOI_GIAN_BAT_DAU"] = time_part
                        converted_record["NGAY_BAT_DAU"] = date_part
                        converted_record["NGAY"] = date_part  # Also set NGAY for compatibility
            
            # Parse strDenNgay: "08:15:00 ngày 01/02/2026" -> NGAY_KET_THUC="01/02/2026", THOI_GIAN_KET_THUC="08:15:00"
            if "strDenNgay" in record and record["strDenNgay"]:
                den_ngay = str(record["strDenNgay"]).strip()
                # Extract time and date: "08:15:00 ngày 01/02/2026"
                if "ngày" in den_ngay:
                    parts = den_ngay.split("ngày")
                    if len(parts) == 2:
                        time_part = parts[0].strip()
                        date_part = parts[1].strip()
                        converted_record["THOI_GIAN_KET_THUC"] = time_part
                        converted_record["NGAY_KET_THUC"] = date_part
            
            # Convert strLyDoMatDien -> LY_DO
            if "strLyDoMatDien" in record:
                converted_record["LY_DO"] = record["strLyDoMatDien"]
                converted_record["ly_do"] = record["strLyDoMatDien"]
            
            # Convert strDiaChi -> DIA_CHI and KHU_VUC
            if "strDiaChi" in record:
                converted_record["DIA_CHI"] = record["strDiaChi"]
                converted_record["dia_chi"] = record["strDiaChi"]
                converted_record["KHU_VUC"] = record["strDiaChi"]
                converted_record["khu_vuc"] = record["strDiaChi"]
            
            converted.append(converted_record)
        
        return converted

    def _convert_hcmc_to_standard_format(self, records: list) -> list:
        """Convert HCMC API response format to standard format.
        
        HCMC format (từ ajax_dienNangTieuThuTheoNgay):
        {
            "ngay": "01/12",
            "ngayFull": "01/12/2025",
            "TD": 0.09,
            "BT": 0.2,
            "CD": 0.07,
            "Tong": 0.36,
            "p_giao_bt": "8,568.63",
            "tong_p_giao": "15,248.39"
        }
        
        Standard format:
        {
            "NGAY": "01/12/2025",
            "CHISO_MOI": 15248.39,
            "CHISO": 15248.39,
            "DIEN_TIEU_THU": 0.36,
            "SAN_LUONG": 0.36
        }
        """
        converted = []
        for record in records:
            if not isinstance(record, dict):
                continue
            
            converted_record = {}
            # Copy all existing fields
            converted_record.update(record)
            
            # Convert ngayFull -> NGAY
            if "ngayFull" in record:
                converted_record["NGAY"] = record["ngayFull"]
            
            # Convert tong_p_giao -> CHISO_MOI and CHISO
            if "tong_p_giao" in record:
                try:
                    # Remove commas and convert to float
                    chi_so_str = str(record["tong_p_giao"]).replace(",", "").strip()
                    chi_so = float(chi_so_str)
                    converted_record["CHISO_MOI"] = chi_so
                    converted_record["CHISO"] = chi_so
                except (ValueError, TypeError):
                    pass
            
            # Convert Tong -> DIEN_TIEU_THU and SAN_LUONG
            if "Tong" in record:
                try:
                    tieu_thu = float(record["Tong"])
                    converted_record["DIEN_TIEU_THU"] = tieu_thu
                    converted_record["SAN_LUONG"] = tieu_thu
                except (ValueError, TypeError):
                    pass
            
            converted.append(converted_record)
        
        return converted

    def _convert_cpc_outage_to_standard_format(self, records: list) -> list:
        """Convert CPC outage API response format to standard format.
        
        CPC format: {
            "TGIAN_BDAU": "05/12/2025 05:30",
            "TGIAN_KTHUC": "05/12/2025 17:00",
            "LY_DO": "Đội QLĐ Cẩm Lệ...",
            "KHUVUCMATDIEN": "ĐZ 483HXU:..."
        }
        
        Standard format: {
            "NGAY_BAT_DAU": "05/12/2025",
            "NGAY_KET_THUC": "05/12/2025",
            "THOI_GIAN_BAT_DAU": "05:30",
            "THOI_GIAN_KET_THUC": "17:00",
            "LY_DO": "Đội QLĐ Cẩm Lệ...",
            "KHU_VUC": "ĐZ 483HXU:..."
        }
        """
        converted = []
        for record in records:
            if not isinstance(record, dict):
                continue
            
            converted_record = {}
            # Copy all existing fields
            converted_record.update(record)
            
            # Parse TGIAN_BDAU: "05/12/2025 05:30" -> NGAY_BAT_DAU="05/12/2025", THOI_GIAN_BAT_DAU="05:30"
            if "TGIAN_BDAU" in record and record["TGIAN_BDAU"]:
                tgian_bdau = str(record["TGIAN_BDAU"]).strip()
                # Extract date and time: "05/12/2025 05:30"
                if " " in tgian_bdau:
                    parts = tgian_bdau.split(" ", 1)
                    if len(parts) == 2:
                        date_part = parts[0].strip()
                        time_part = parts[1].strip()
                        converted_record["NGAY_BAT_DAU"] = date_part
                        converted_record["THOI_GIAN_BAT_DAU"] = time_part
                        converted_record["NGAY"] = date_part  # Also set NGAY for compatibility
            
            # Parse TGIAN_KTHUC: "05/12/2025 17:00" -> NGAY_KET_THUC="05/12/2025", THOI_GIAN_KET_THUC="17:00"
            if "TGIAN_KTHUC" in record and record["TGIAN_KTHUC"]:
                tgian_kthuc = str(record["TGIAN_KTHUC"]).strip()
                # Extract date and time: "05/12/2025 17:00"
                if " " in tgian_kthuc:
                    parts = tgian_kthuc.split(" ", 1)
                    if len(parts) == 2:
                        date_part = parts[0].strip()
                        time_part = parts[1].strip()
                        converted_record["NGAY_KET_THUC"] = date_part
                        converted_record["THOI_GIAN_KET_THUC"] = time_part
            
            # Convert LY_DO -> LY_DO (giữ nguyên vì đã đúng format)
            if "LY_DO" in record:
                converted_record["LY_DO"] = record["LY_DO"]
                converted_record["ly_do"] = record["LY_DO"]
            
            # Convert KHUVUCMATDIEN -> KHU_VUC and DIA_CHI
            if "KHUVUCMATDIEN" in record:
                converted_record["KHU_VUC"] = record["KHUVUCMATDIEN"]
                converted_record["khu_vuc"] = record["KHUVUCMATDIEN"]
                converted_record["DIA_CHI"] = record["KHUVUCMATDIEN"]
                converted_record["dia_chi"] = record["KHUVUCMATDIEN"]
            
            converted.append(converted_record)
        
        return converted

    async def get_chisongay(
        self, from_date: str, to_date: str
    ) -> Optional[Dict[str, Any]]:
        """Get daily consumption data.
        
        Args:
            from_date: Format dd/mm/yyyy
            to_date: Format dd/mm/yyyy
            
        Returns:
            Dict with data or None
        """
        if not self.access_token:
            if not await self.login():
                return None

        try:
            session = await self._get_session()
            
            # HCMC dùng endpoint và format riêng
            if self.region == "HCMC":
                if not self.hcmc_session:
                    if not await self.login():
                        return None
                
                url = f"{self.base_url}/Tracuu/ajax_dienNangTieuThuTheoNgay"
                
                headers = {
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36",
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Cookie": f"evn_session={self.hcmc_session}",
                }
                
                payload = {
                    "input_makh": self.customer_id,
                    "input_tungay": from_date,
                    "input_denngay": to_date,
                }
                
                _LOGGER.debug(f"get_chisongay (HCMC): URL={url}, payload={payload}, region={self.region}")
                
                async with session.post(url, data=payload, headers=headers, timeout=self._timeout) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        _LOGGER.error(f"get_chisongay failed with status {resp.status}, URL={url}, payload={payload}, response: {error_text[:500]}")
                        return None
                    
                    # HCMC API trả về JSON nhưng Content-Type là text/html
                    try:
                        response_text = await resp.text(encoding='utf-8', errors='replace')
                        data = json.loads(response_text)
                    except Exception as e:
                        _LOGGER.error(f"Failed to parse HCMC response: {e}")
                        return None
                    
                    # Xử lý response theo format HCMC
                    if data.get("state") == "success" and "data" in data:
                        records = data["data"].get("sanluong_tungngay", [])
                        if isinstance(records, list):
                            converted_data = self._convert_hcmc_to_standard_format(records)
                            return {"data": converted_data}
                    
                    _LOGGER.error(f"HCMC get_chisongay: Invalid response format: {data}")
                    return None
            
            # SPC dùng endpoint và format riêng
            elif self.region == "SPC":
                # Convert dd/mm/yyyy to YYYYMMDD format (như nestup_evn: from_date - 1 ngày)
                from_date_obj = datetime.strptime(from_date, "%d/%m/%Y") - timedelta(days=1)
                to_date_obj = datetime.strptime(to_date, "%d/%m/%Y")
                from_date_str = from_date_obj.strftime("%Y%m%d")
                to_date_str = to_date_obj.strftime("%Y%m%d")
                
                url = f"{self.base_url}/api/NghiepVu/LayThongTinSanLuongTheoNgay_v2"
                params = {
                    "strMaDiemDo": f"{self.customer_id}001",
                    "strFromDate": from_date_str,
                    "strToDate": to_date_str,
                }
                headers = {
                    "accept": "application/json, text/plain, */*",
                    "user-agent": "okhttp/4.12.0",
                    "authorization": f"Bearer {self.access_token}",
                }
                
                _LOGGER.debug(f"get_chisongay (SPC): URL={url}, params={params}, region={self.region}")
                
                async with session.get(url, params=params, headers=headers, timeout=self._timeout) as resp:
                    if resp.status == 401:
                        # Token expired, try login again
                        if await self.login():
                            headers["authorization"] = f"Bearer {self.access_token}"
                            async with session.get(url, params=params, headers=headers, timeout=self._timeout) as retry_resp:
                                if retry_resp.status != 200:
                                    error_text = await retry_resp.text()
                                    _LOGGER.error(f"get_chisongay failed with status {retry_resp.status}, response: {error_text[:500]}")
                                    return None
                                data = await retry_resp.json()
                                # SPC trả về list trực tiếp, chuyển đổi format và wrap vào dict với key "data"
                                if isinstance(data, list):
                                    converted_data = self._convert_spc_to_standard_format(data)
                                    return {"data": converted_data}
                                return data
                        return None

                    if resp.status != 200:
                        error_text = await resp.text()
                        _LOGGER.error(f"get_chisongay failed with status {resp.status}, URL={url}, params={params}, response: {error_text[:500]}")
                        return None

                    data = await resp.json()
                    # SPC trả về list trực tiếp, chuyển đổi format và wrap vào dict với key "data"
                    if isinstance(data, list):
                        converted_data = self._convert_spc_to_standard_format(data)
                        return {"data": converted_data}
                    return data
            else:
                # Các region khác dùng endpoint chung
                url = f"{self.base_url}/api/evn/tracuu/chisongay"

                # Lấy MA_DVIQLY và MA_DDO dựa trên region
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

                _LOGGER.debug(f"get_chisongay: URL={url}, payload={payload}, region={self.region}")

                async with session.post(url, json=payload, headers=headers, timeout=self._timeout) as resp:
                    if resp.status == 401:
                        # Token expired, try login again
                        if await self.login():
                            headers["authorization"] = f"Bearer {self.access_token}"
                            async with session.post(url, json=payload, headers=headers, timeout=self._timeout) as retry_resp:
                                if retry_resp.status != 200:
                                    error_text = await retry_resp.text()
                                    _LOGGER.error(f"get_chisongay failed with status {retry_resp.status}, response: {error_text[:500]}")
                                    return None
                                return await retry_resp.json()
                        return None

                    if resp.status != 200:
                        error_text = await resp.text()
                        _LOGGER.error(f"get_chisongay failed with status {resp.status}, URL={url}, payload={payload}, response: {error_text[:500]}")
                        return None

                    return await resp.json()

        except Exception as e:
            _LOGGER.error(f"get_chisongay error: {e}", exc_info=True)
            return None

    async def get_chisothang(
        self, month: int, year: int
    ) -> Optional[Dict[str, Any]]:
        """Get monthly consumption data.
        
        Args:
            month: Month (1-12)
            year: Year
            
        Returns:
            Dict with data or None
        """
        if not self.access_token:
            if not await self.login():
                return None

        try:
            # HCMC và SPC tính từ dữ liệu ngày (như nestup_evn)
            if self.region == "HCMC" or self.region == "SPC":
                
                # Lấy dữ liệu ngày cho cả tháng
                month_start = datetime(year, month, 1)
                _, last_day = monthrange(year, month)
                month_end = datetime(year, month, last_day)

                # Cần cả chỉ số ngày cuối tháng trước làm chỉ số đầu kỳ.
                # SPC: get_chisongay đã tự lùi 1 ngày, HCMC thì chưa.
                if self.region == "SPC":
                    from_date = month_start.strftime("%d/%m/%Y")
                else:
                    from_date = (month_start - timedelta(days=1)).strftime("%d/%m/%Y")
                to_date = month_end.strftime("%d/%m/%Y")
                
                daily_data = await self.get_chisongay(from_date, to_date)
                if not daily_data or not daily_data.get("data"):
                    _LOGGER.error(f"get_chisothang: Failed to get daily data for {self.region}")
                    return None
                
                records = daily_data["data"]
                if not isinstance(records, list) or len(records) == 0:
                    _LOGGER.error(f"get_chisothang: No daily records for {self.region}")
                    return None
                
                # Tính sản lượng tháng từ trường sản lượng ngày khi EVN cung
                # cấp. Đây là nguồn trực tiếp hơn so với tự suy ra bằng hiệu hai
                # chỉ số và tránh thiếu kWh nếu response không có ngày cuối tháng
                # trước. Bản ghi cùng ngày được khử trùng lặp.
                records = sorted(records, key=_daily_record_sort_key)
                first_record = records[0]
                last_record = records[-1]
                daily_values: dict[date, float] = {}
                for record in records:
                    record_date = _daily_record_date(record)
                    if (
                        record_date is None
                        or record_date.year != year
                        or record_date.month != month
                    ):
                        continue
                    raw_consumption = next(
                        (
                            record.get(key)
                            for key in (
                                "DIEN_TIEU_THU",
                                "SAN_LUONG",
                                "dSanLuongBT",
                                "Tong",
                            )
                            if record.get(key) is not None
                        ),
                        None,
                    )
                    consumption = _optional_float(raw_consumption)
                    if consumption is not None and consumption >= 0:
                        daily_values[record_date.date()] = consumption

                if daily_values:
                    chi_so_thang = round(sum(daily_values.values()), 6)
                    first_day = min(daily_values)
                    last_day = max(daily_values)
                    from_date_parsed = datetime.combine(first_day, datetime.min.time())
                    to_date_parsed = datetime.combine(last_day, datetime.min.time())
                    if self.region == "HCMC":
                        chi_so_cu = _optional_float(
                            first_record.get("CHISO_MOI")
                            if first_record.get("CHISO_MOI") is not None
                            else first_record.get("CHISO")
                        )
                        chi_so_moi = _optional_float(
                            last_record.get("CHISO_MOI")
                            if last_record.get("CHISO_MOI") is not None
                            else last_record.get("CHISO")
                        )
                    else:
                        chi_so_cu = _optional_float(first_record.get("dGiaoBT"))
                        chi_so_moi = _optional_float(last_record.get("dGiaoBT"))
                # Fallback: only derive from meter readings when direct daily
                # consumption is genuinely absent. Missing readings return None
                # rather than manufacturing a 0 kWh month.
                elif self.region == "HCMC":
                    chi_so_cu = _optional_float(
                        first_record.get("CHISO_MOI")
                        if first_record.get("CHISO_MOI") is not None
                        else first_record.get("CHISO")
                    )
                    chi_so_moi = _optional_float(
                        last_record.get("CHISO_MOI")
                        if last_record.get("CHISO_MOI") is not None
                        else last_record.get("CHISO")
                    )
                    if chi_so_cu is None or chi_so_moi is None or chi_so_moi < chi_so_cu:
                        _LOGGER.warning(
                            "get_chisothang: invalid/missing HCMC meter readings for %s/%s",
                            month,
                            year,
                        )
                        return None
                    chi_so_thang = round(chi_so_moi - chi_so_cu, 6)
                    ngay_dau = first_record.get("NGAY") or first_record.get("ngayFull")
                    ngay_cuoi = last_record.get("NGAY") or last_record.get("ngayFull")
                    from_date_parsed = (
                        datetime.strptime(str(ngay_dau), "%d/%m/%Y") + timedelta(days=1)
                        if ngay_dau
                        else month_start
                    )
                    to_date_parsed = (
                        datetime.strptime(str(ngay_cuoi), "%d/%m/%Y")
                        if ngay_cuoi
                        else month_end
                    )
                else:  # SPC fallback
                    d_giao_bt_old = _optional_float(first_record.get("dGiaoBT"))
                    d_giao_bt_new = _optional_float(last_record.get("dGiaoBT"))
                    if (
                        d_giao_bt_old is None
                        or d_giao_bt_new is None
                        or d_giao_bt_new < d_giao_bt_old
                    ):
                        _LOGGER.warning(
                            "get_chisothang: invalid/missing SPC meter readings for %s/%s",
                            month,
                            year,
                        )
                        return None
                    chi_so_thang = round(d_giao_bt_new - d_giao_bt_old, 6)
                    chi_so_cu = d_giao_bt_old
                    chi_so_moi = d_giao_bt_new
                    first_date = _daily_record_date(first_record)
                    last_date = _daily_record_date(last_record)
                    if first_date is None or last_date is None:
                        return None
                    from_date_parsed = first_date + timedelta(days=1)
                    to_date_parsed = last_date

                # Trả về đúng format của API chisothang chung: data là list
                # bản ghi với các key CHISO_CU / CHISO_MOI / DIEN_TTHU,
                # vì coordinator._save_monthly_data đọc theo format đó.
                return {
                    "data": [
                        {
                            "THANG": month,
                            "NAM": year,
                            "DIEN_TTHU": chi_so_thang,
                            "CHISO_CU": chi_so_cu,
                            "CHISO_MOI": chi_so_moi,
                            "TU_NGAY": from_date_parsed.strftime("%d/%m/%Y"),
                            "DEN_NGAY": to_date_parsed.strftime("%d/%m/%Y"),
                        }
                    ]
                }
            else:
                # Các region khác dùng endpoint chung
                session = await self._get_session()
                url = f"{self.base_url}/api/evn/tracuu/chisothang"

                # Lấy MA_DVIQLY và MA_DDO dựa trên region
                ma_dviqly, ma_ddo = self._get_ma_dviqly_and_ma_ddo()

                # Format: MM/YYYY
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

                async with session.post(url, json=payload, headers=headers, timeout=self._timeout) as resp:
                    if resp.status == 401:
                        if await self.login():
                            headers["authorization"] = f"Bearer {self.access_token}"
                            async with session.post(url, json=payload, headers=headers, timeout=self._timeout) as retry_resp:
                                if retry_resp.status != 200:
                                    _LOGGER.error(f"get_chisothang failed with status {retry_resp.status}")
                                    return None
                                return await retry_resp.json()
                        return None

                    if resp.status != 200:
                        _LOGGER.error(f"get_chisothang failed with status {resp.status}")
                        return None

                    return await resp.json()

        except Exception as e:
            _LOGGER.error(f"get_chisothang error: {e}", exc_info=True)
            return None

    async def get_hoadon(self) -> Optional[Dict[str, Any]]:
        """Get bill information.
        
        Returns:
            Dict with data or None
        """
        if not self.access_token:
            if not await self.login():
                return None

        try:
            session = await self._get_session()
            
            # HCMC dùng endpoint và format riêng
            if self.region == "HCMC":
                if not self.hcmc_session:
                    if not await self.login():
                        return None
                
                url = f"{self.base_url}/Tracuu/kiemTraNo"
                
                headers = {
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36",
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Cookie": f"evn_session={self.hcmc_session}",
                }
                
                payload = {
                    "input_makh": self.customer_id,
                }
                
                _LOGGER.debug(f"get_hoadon (HCMC): URL={url}, payload={payload}, region={self.region}")
                
                async with session.post(url, data=payload, headers=headers, timeout=self._timeout) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        _LOGGER.error(f"get_hoadon failed with status {resp.status}, URL={url}, payload={payload}, response: {error_text[:500]}")
                        return None
                    
                    # HCMC API trả về JSON nhưng Content-Type là text/html
                    try:
                        response_text = await resp.text(encoding='utf-8', errors='replace')
                        data = json.loads(response_text)
                    except Exception as e:
                        _LOGGER.error(f"Failed to parse HCMC response: {e}")
                        return None
                    
                    # Xử lý response theo format HCMC
                    # HCMC trả về {"state": "success", "data": {"isNo": 0/1, "info_no": [...]}}
                    # Nếu có nợ (isNo=1), info_no chứa danh sách hóa đơn
                    if data.get("state") == "success" and "data" in data:
                        hcmc_data = data["data"]
                        if hcmc_data.get("isNo") == 1 and "info_no" in hcmc_data:
                            # Có nợ, trả về danh sách hóa đơn
                            bills = hcmc_data["info_no"]
                            if isinstance(bills, list):
                                return {"data": bills}
                        else:
                            # Không có nợ, trả về empty list
                            return {"data": []}
                    
                    _LOGGER.error(f"HCMC get_hoadon: Invalid response format: {data}")
                    return None
            
            # SPC dùng endpoint và format riêng
            elif self.region == "SPC":
                url = f"{self.base_url}/api/NghiepVu/TraCuuNoHoaDon"
                params = {
                    # Phải là mã khách hàng đã cấu hình, không phải mã của tài
                    # khoản đăng nhập: một tài khoản SPC có thể gắn nhiều mã và
                    # API phân quyền theo tham số này.
                    "strMaKH": self.customer_id,
                }
                headers = {
                    "User-Agent": "evnapp/59 CFNetwork/1240.0.4 Darwin/20.6.0",
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Accept-Language": "vi-vn",
                    "Connection": "keep-alive",
                }
                
                _LOGGER.debug(f"get_hoadon (SPC): URL={url}, params={params}, region={self.region}")
                
                async with session.get(url, params=params, headers=headers, timeout=self._timeout) as resp:
                    if resp.status == 401:
                        if await self.login():
                            headers["Authorization"] = f"Bearer {self.access_token}"
                            async with session.get(url, params=params, headers=headers, timeout=self._timeout) as retry_resp:
                                if retry_resp.status != 200:
                                    error_text = await retry_resp.text()
                                    _LOGGER.error(f"get_hoadon failed with status {retry_resp.status}, response: {error_text[:500]}")
                                    return None
                                data = await retry_resp.json()
                                # SPC trả về list trực tiếp, wrap vào dict với key "data"
                                if isinstance(data, list):
                                    return {"data": data}
                                return data
                        return None

                    if resp.status != 200:
                        error_text = await resp.text()
                        _LOGGER.error(f"get_hoadon failed with status {resp.status}, URL={url}, params={params}, response: {error_text[:500]}")
                        return None

                    data = await resp.json()
                    # SPC trả về list trực tiếp, wrap vào dict với key "data"
                    if isinstance(data, list):
                        return {"data": data}
                    return data
            else:
                # Các region khác dùng endpoint chung
                url = f"{self.base_url}/api/evn/tracuu/hoadon"

                headers = {
                    "accept": "application/json, text/plain, */*",
                    "content-type": "application/json",
                    "user-agent": "okhttp/4.12.0",
                    "authorization": f"Bearer {self.access_token}",
                }

                async with session.post(url, headers=headers, timeout=self._timeout) as resp:
                    if resp.status == 401:
                        if await self.login():
                            headers["authorization"] = f"Bearer {self.access_token}"
                            async with session.post(url, headers=headers, timeout=self._timeout) as retry_resp:
                                if retry_resp.status != 200:
                                    _LOGGER.error(f"get_hoadon failed with status {retry_resp.status}")
                                    return None
                                return await retry_resp.json()
                        return None

                    if resp.status != 200:
                        _LOGGER.error(f"get_hoadon failed with status {resp.status}")
                        return None

                    return await resp.json()

        except Exception as e:
            _LOGGER.error(f"get_hoadon error: {e}", exc_info=True)
            return None

    async def get_ngungcapdien(
        self, from_date: str, to_date: str
    ) -> Optional[Dict[str, Any]]:
        """Get power outage schedule.
        
        Args:
            from_date: Format dd/mm/yyyy
            to_date: Format dd/mm/yyyy
            
        Returns:
            Dict with data or None
        """
        if not self.access_token:
            if not await self.login():
                return None

        try:
            session = await self._get_session()
            
            # SPC dùng endpoint và format riêng
            if self.region == "SPC":
                url = f"{self.base_url}/api/NghiepVu/TraCuuLichNgungGiamCungCapDien"
                params = {
                    # Phải là mã khách hàng đã cấu hình, không phải mã của tài
                    # khoản đăng nhập: một tài khoản SPC có thể gắn nhiều mã và
                    # API phân quyền theo tham số này.
                    "strMaKH": self.customer_id,
                }
                headers = {
                    "User-Agent": "evnapp/59 CFNetwork/1240.0.4 Darwin/20.6.0",
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Accept-Language": "vi-vn",
                    "Connection": "keep-alive",
                }
                
                _LOGGER.debug(f"get_ngungcapdien (SPC): URL={url}, params={params}, region={self.region}")
                
                async with session.get(url, params=params, headers=headers, timeout=self._timeout) as resp:
                    if resp.status == 401:
                        if await self.login():
                            headers["Authorization"] = f"Bearer {self.access_token}"
                            async with session.get(url, params=params, headers=headers, timeout=self._timeout) as retry_resp:
                                if retry_resp.status != 200:
                                    error_text = await retry_resp.text()
                                    _LOGGER.error(f"get_ngungcapdien failed with status {retry_resp.status}, response: {error_text[:500]}")
                                    return None
                                data = await retry_resp.json()
                                # SPC trả về list trực tiếp, chuyển đổi format và wrap vào dict với key "data"
                                if isinstance(data, list):
                                    converted_data = self._convert_spc_outage_to_standard_format(data)
                                    return {"data": converted_data}
                                return data
                        return None

                    if resp.status != 200:
                        error_text = await resp.text()
                        _LOGGER.error(f"get_ngungcapdien failed with status {resp.status}, URL={url}, params={params}, response: {error_text[:500]}")
                        return None

                    data = await resp.json()
                    # SPC trả về list trực tiếp, chuyển đổi format và wrap vào dict với key "data"
                    if isinstance(data, list):
                        converted_data = self._convert_spc_outage_to_standard_format(data)
                        return {"data": converted_data}
                    return data
            else:
                # Các region khác dùng endpoint chung
                url = f"{self.base_url}/api/evn/tracuu/ngungcapdien"

                payload = {
                    "TU_NGAY": from_date,
                    "DEN_NGAY": to_date,
                }

                headers = {
                    "accept": "application/json, text/plain, */*",
                    "content-type": "application/json",
                    "user-agent": "okhttp/4.12.0",
                    "authorization": f"Bearer {self.access_token}",
                }

                async with session.post(url, json=payload, headers=headers, timeout=self._timeout) as resp:
                    if resp.status == 401:
                        if await self.login():
                            headers["authorization"] = f"Bearer {self.access_token}"
                            async with session.post(url, json=payload, headers=headers, timeout=self._timeout) as retry_resp:
                                if retry_resp.status != 200:
                                    _LOGGER.error(f"get_ngungcapdien failed with status {retry_resp.status}")
                                    return None
                                data = await retry_resp.json()
                                # Chuyển đổi format cho CPC
                                if self.region == "CPC" and isinstance(data, dict) and data.get("data"):
                                    if isinstance(data["data"], list):
                                        converted_data = self._convert_cpc_outage_to_standard_format(data["data"])
                                        return {"data": converted_data}
                                return data
                        return None

                    if resp.status != 200:
                        _LOGGER.error(f"get_ngungcapdien failed with status {resp.status}")
                        return None

                    data = await resp.json()
                    # Chuyển đổi format cho CPC
                    if self.region == "CPC" and isinstance(data, dict) and data.get("data"):
                        if isinstance(data["data"], list):
                            converted_data = self._convert_cpc_outage_to_standard_format(data["data"])
                            return {"data": converted_data}
                    return data

        except Exception as e:
            _LOGGER.error(f"get_ngungcapdien error: {e}", exc_info=True)
            return None


    async def get_thongbao(self) -> Optional[list]:
        """Get EVN notifications and normalize SPC into the common envelope."""
        if not self.access_token:
            if not await self.login():
                return None

        try:
            session = await self._get_session()
            if self.region == "SPC":
                url = f"{self.base_url}/api/NghiepVu/LayDanhSachThongBaoKhachHang"
                params = {"strMaKh": self.customer_id, "strRedId": ""}
                headers = {
                    "accept": "application/json",
                    "authorization": f"Bearer {self.access_token}",
                    "user-agent": "evnapp/59 CFNetwork/1240.0.4 Darwin/20.6.0",
                }
                data = await self._request_json_with_reauth(
                    "GET", url, headers=headers, params=params
                )
                rows = data.get("data") if isinstance(data, dict) else data
                if not isinstance(rows, list):
                    return [] if rows is not None else None
                normalized: list[dict[str, Any]] = []
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    row = dict(item)
                    title = str(
                        item.get("strTieuDe")
                        or item.get("TieuDe")
                        or item.get("tieuDe")
                        or item.get("title")
                        or ""
                    )
                    summary = str(
                        item.get("strNoiDung")
                        or item.get("noiDung")
                        or item.get("summary")
                        or ""
                    )
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
                    row.setdefault(
                        "createdDate",
                        item.get("strNgayThongBao")
                        or item.get("ngayTao")
                        or item.get("NgayTao"),
                    )
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
        except Exception as err:
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
        """Issue a small JSON request and retry once after an auth refresh."""
        session = await self._get_session()
        for attempt in range(2):
            async with session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                if resp.status in (401, 403) and attempt == 0:
                    if not await self.login():
                        return None
                    token_key = "Authorization" if "Authorization" in headers else "authorization"
                    headers[token_key] = f"Bearer {self.access_token}"
                    continue
                if resp.status != 200:
                    text = await resp.text()
                    _LOGGER.debug("EVN request failed %s %s: HTTP %s %s", method, url, resp.status, text[:300])
                    return None
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    text = await resp.text()
                    try:
                        return json.loads(text)
                    except Exception:
                        return None
        return None

    async def download_file(self, url: str) -> bytes | None:
        """Download an official EVN invoice resource.

        Regional APIs often return an opaque download/viewer URL rather than a
        URL ending in ``.pdf`` or ``.png``.  The caller verifies file signatures;
        here we follow EVN redirects and one level of HTML viewer links.  EVN
        credentials are only attached to EVN-owned hosts.
        """
        if not url:
            return None
        start_url = urljoin(f"{self.base_url}/", str(url).strip())
        return await self._download_file_url(start_url, visited=set(), depth=0)

    async def _download_file_url(
        self, url: str, *, visited: set[str], depth: int
    ) -> bytes | None:
        if depth > 2 or url in visited:
            return None
        visited.add(url)
        session = await self._get_session()
        headers = {
            "user-agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 EVN-CSKH-Monitor/1.0",
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
                async with session.get(
                    url,
                    headers=headers,
                    timeout=self._timeout,
                    allow_redirects=False,
                ) as resp:
                    if resp.status in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("Location")
                        if not location:
                            return None
                        redirected = urljoin(str(resp.url), location)
                        # Re-enter the helper so Authorization/Cookie headers are
                        # recalculated for the destination host. This prevents a
                        # signed external invoice redirect from receiving EVN
                        # credentials by accident.
                        return await self._download_file_url(
                            redirected, visited=visited, depth=depth
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
                        _LOGGER.debug("Invoice download failed %s: HTTP %s", url, resp.status)
                        return None
                    # Cap invoice resources so a bad endpoint cannot consume
                    # unbounded memory. EVN invoices are normally far below 20 MB.
                    content_length = resp.content_length
                    if content_length is not None and content_length > 20 * 1024 * 1024:
                        _LOGGER.warning("Ignoring oversized EVN invoice resource: %s bytes", content_length)
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > 20 * 1024 * 1024:
                            _LOGGER.warning("Ignoring oversized EVN invoice stream from %s", url)
                            return None
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if not data:
                        return None
                    content_type = (resp.headers.get("Content-Type") or "").lower()
                    stripped = data[:256].lstrip()
                    if (
                        "application/json" in content_type
                        or stripped.startswith((b"{", b"["))
                    ):
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
                                    nested_url = urljoin(str(resp.url), candidate)
                                    nested = await self._download_file_url(
                                        nested_url, visited=visited, depth=depth + 1
                                    )
                                    if nested:
                                        return nested
                    if "text/html" in content_type or stripped.lower().startswith((b"<!doctype html", b"<html")):
                        for linked in extract_invoice_links_from_html(data, str(resp.url)):
                            nested = await self._download_file_url(
                                linked, visited=visited, depth=depth + 1
                            )
                            if nested:
                                return nested
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.debug("Invoice download failed %s: %s", url, err)
                return None
            except Exception as err:  # noqa: BLE001 - region servers vary widely
                _LOGGER.debug("Invoice download failed %s: %s", url, err)
                return None
        return None

def _optional_float(value: Any) -> float | None:
    """Parse a numeric API field without manufacturing a zero value."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    # EVN meter readings are normally integers/decimals; commas are commonly
    # thousands separators in the regional responses handled here.
    if "," in text and "." not in text:
        text = text.replace(",", "")
    elif "," in text and "." in text:
        text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _daily_record_date(record: Any) -> datetime | None:
    """Parse the date from an HCMC/SPC standardized daily record."""
    if not isinstance(record, dict):
        return None
    for key in ("NGAY", "ngayFull", "strTime"):
        raw = record.get(key)
        if not raw:
            continue
        text = str(raw).strip()
        # Some SPC records represent a range; attribute the aggregate to the
        # range end, matching the normalization used by the coordinator.
        if text.count("/") == 4 and "-" in text:
            text = text.rsplit("-", 1)[-1].strip()
        try:
            return datetime.strptime(text, "%d/%m/%Y")
        except ValueError:
            continue
    return None


def _daily_record_sort_key(record: Any) -> tuple[int, datetime]:
    """Sort HCMC/SPC daily records chronologically when a date is present."""
    parsed = _daily_record_date(record)
    return (0, parsed) if parsed is not None else (1, datetime.max)

