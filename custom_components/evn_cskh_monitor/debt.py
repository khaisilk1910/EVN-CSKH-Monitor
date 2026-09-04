"""Pure helpers for deriving CURRENT EVN debt from heterogeneous responses.

Invoice history and current debt are intentionally separate concepts. Historical
invoice totals must never be summed as debt unless EVN explicitly marks them
unpaid or the endpoint itself is a debt-only endpoint.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

# Strong envelope-level indicators. TTIEN_TTOAN_SO is EVN's documented total
# amount currently payable. Generic TONG_TIEN is deliberately excluded because
# it is also the historical invoice total.
_TOTAL_DEBT_KEYS = {
    "ttien_ttoan_so",
    "ttienttoanso",
    "tong_tien_thanh_toan",
    "tongtienthanhtoan",
    "tong_tien_no",
    "tongtienno",
    "tong_no",
    "tongno",
    "tong_cong_no",
    "tongcongno",
    "so_tien_no",
    "sotienno",
    "no_hien_tai",
    "nohientai",
    "current_debt",
    "currentdebt",
}

_LINE_DEBT_TOTAL_KEYS = {
    "tong_tien_no",
    "tongtienno",
    "so_tien_no",
    "sotienno",
    "con_no",
    "conno",
    "so_tien_con_no",
    "sotienconno",
    "remaining_amount",
    "remainingamount",
    "amount_due",
    "amountdue",
}

_PRINCIPAL_DEBT_KEYS = {"tien_no", "tienno"}
_TAX_DEBT_KEYS = {"thue_no", "thueno"}

_STATUS_KEYS = {
    "ttrang_ttoan",
    "trang_thai",
    "trangthai",
    "status",
    "payment_status",
    "paymentstatus",
    "tinh_trang",
    "tinhtrang",
}

_INVOICE_AMOUNT_KEYS = {
    "tong_tien",
    "tongtien",
    "tien_dien",
    "tiendien",
    "so_tien",
    "sotien",
    "thanh_tien",
    "thanhtien",
    "amount",
    "total_amount",
    "totalamount",
}

_BILL_LIST_KEYS = {
    "data",
    "hoa_don",
    "hoadon",
    "hoa_don_no",
    "hoadonno",
    "info_no",
    "invoices",
    "bills",
}

_UNPAID_TOKENS = {
    "chuatt",
    "chuathanhtoan",
    "unpaid",
    "conno",
    "chuadong",
    "chuanop",
    "notpaid",
    "pendingpayment",
}
_PAID_TOKENS = {
    "datt",
    "dathanhtoan",
    "paid",
    "hethanhtoan",
    "hoantat",
    "completed",
    "settled",
}

# These API methods are explicitly debt/current-liability lookups in the current
# integration, so a successful empty list means zero debt and a returned row is
# an outstanding row even when it has no textual payment status.
_DEBT_ONLY_REGIONS = {"HCMC", "SPC"}


def _norm_key(value: Any) -> str:
    text = str(value or "").strip().lower().replace("đ", "d")
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9_]", "", text)


def _norm_status(value: Any) -> str:
    text = str(value or "").strip().lower().replace("đ", "d")
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9]", "", text)


def _parse_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None

    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    text = re.sub(r"[^\d,.\-+]", "", text)
    if not text or text in {"+", "-"}:
        return None

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts) == 2 and 1 <= len(parts[-1]) <= 2:
            text = parts[0] + "." + parts[1]
        else:
            text = "".join(parts)
    elif "." in text:
        parts = text.split(".")
        # 5.658.248 is a thousands-formatted integer.
        if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
            text = "".join(parts)

    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _first_numeric(mapping: dict[str, Any], keys: set[str]) -> float | None:
    for key, value in mapping.items():
        if _norm_key(key) not in keys:
            continue
        number = _parse_number(value)
        if number is not None:
            return number
    return None


def _explicit_total_debt(value: Any, *, depth: int = 0) -> float | None:
    """Find a strong total-current-debt field, preferring outer envelopes."""
    if depth > 5:
        return None
    if isinstance(value, dict):
        found = _first_numeric(value, _TOTAL_DEBT_KEYS)
        if found is not None:
            return max(0.0, found)
        for child in value.values():
            if isinstance(child, (dict, list)):
                found = _explicit_total_debt(child, depth=depth + 1)
                if found is not None:
                    return found
    elif isinstance(value, list):
        for child in value:
            if isinstance(child, (dict, list)):
                found = _explicit_total_debt(child, depth=depth + 1)
                if found is not None:
                    return found
    return None


def extract_bill_rows(response: Any) -> list[dict[str, Any]]:
    """Return the most plausible bill row list without flattening random data."""
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if not isinstance(response, dict):
        return []

    # Preserve the integration's common {"data": [...]} contract first.
    data = response.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    # Some EVN services return {"data": {"HOA_DON": [...]}} or HCMC info_no.
    containers = [response]
    if isinstance(data, dict):
        containers.insert(0, data)
    for container in containers:
        for key, value in container.items():
            if _norm_key(key) in _BILL_LIST_KEYS and isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _status_state(row: dict[str, Any]) -> bool | None:
    """Return True for unpaid, False for paid, None for unknown."""
    raw = None
    for key, value in row.items():
        if _norm_key(key) in _STATUS_KEYS:
            raw = value
            break
    if raw is None:
        return None

    status = _norm_status(raw)
    if not status:
        return None

    # Check unpaid first because "chua thanh toan" contains "thanh toan".
    if status in _UNPAID_TOKENS or any(token in status for token in _UNPAID_TOKENS):
        return True
    if status in _PAID_TOKENS or any(token in status for token in _PAID_TOKENS):
        return False
    return None


def _line_debt(row: dict[str, Any], *, debt_only_endpoint: bool) -> tuple[bool, float]:
    """Return (known, amount_due) for one invoice/debt row."""
    direct = _first_numeric(row, _LINE_DEBT_TOTAL_KEYS)
    if direct is not None:
        return True, max(0.0, direct)

    principal_present = False
    tax_present = False
    principal = 0.0
    tax = 0.0
    for key, value in row.items():
        norm = _norm_key(key)
        if norm in _PRINCIPAL_DEBT_KEYS:
            parsed = _parse_number(value)
            if parsed is not None:
                principal_present = True
                principal = max(0.0, parsed)
        elif norm in _TAX_DEBT_KEYS:
            parsed = _parse_number(value)
            if parsed is not None:
                tax_present = True
                tax = max(0.0, parsed)
    if principal_present or tax_present:
        return True, principal + tax

    status = _status_state(row)
    if status is False:
        return True, 0.0

    amount = _first_numeric(row, _INVOICE_AMOUNT_KEYS)
    if status is True:
        return (amount is not None), max(0.0, amount or 0.0)

    if debt_only_endpoint and amount is not None:
        return True, max(0.0, amount)

    return False, 0.0


def extract_current_debt(response: Any, region: str) -> tuple[bool, float | None]:
    """Derive current debt without ever summing paid historical invoices.

    Returns:
        (authoritative, amount)
        authoritative=False means the response did not contain enough current
        debt semantics, so callers should preserve/leave debt unknown rather than
        invent a value.
    """
    explicit = _explicit_total_debt(response)
    if explicit is not None:
        return True, explicit

    rows = extract_bill_rows(response)
    debt_only_endpoint = str(region or "").upper() in _DEBT_ONLY_REGIONS

    if debt_only_endpoint and not rows:
        # HCMC kiemTraNo and SPC TraCuuNoHoaDon are current-debt endpoints.
        # A valid successful empty list therefore means no current debt.
        return True, 0.0

    if not rows:
        return False, None

    total = 0.0
    all_known = True
    any_known = False
    for row in rows:
        known, amount = _line_debt(row, debt_only_endpoint=debt_only_endpoint)
        if known:
            any_known = True
            total += amount
        else:
            all_known = False

    if debt_only_endpoint and any_known:
        # On a debt-only endpoint every returned row is outstanding. Unknown
        # rows are only tolerated if all known rows still cover the endpoint
        # representation; otherwise preserve the old value.
        return (all_known, round(total, 2) if all_known else None)

    if all_known and any_known:
        return True, round(total, 2)

    return False, None
