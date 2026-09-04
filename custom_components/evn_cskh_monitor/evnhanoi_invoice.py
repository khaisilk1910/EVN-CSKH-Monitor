"""Pure helpers for EVNHANOI's website invoice API.

The website uses a two-step flow for historical invoices:
1. GetThongTinHoaDon returns invoice metadata (including idHdon).
2. XemHoaDonByMaKhachHang returns the official PDF as base64 in ``data``.

This module has no Home Assistant dependencies so the parsing can be tested
against captured HAR fixtures without network access.
"""

from __future__ import annotations

import base64
from typing import Any

EVNHANOI_WEB_BASE = "https://evnhanoi.vn"
EVNHANOI_INVOICE_REFERER = (
    "https://evnhanoi.vn/dashboard/home/quan-ly-hoa-don/tra-cuu-hoa-don"
)


def contract_rows(response: Any) -> list[dict[str, Any]]:
    """Extract EVNHANOI contract rows without assuming a single account."""
    if not isinstance(response, dict) or response.get("isError") is True:
        return []
    data = response.get("data")
    if not isinstance(data, dict):
        return []
    rows = data.get("thongTinHopDongDtos")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def management_unit_candidates(response: Any, customer_id: str) -> list[str]:
    """Return HN management-unit candidates, exact customer matches first.

    EVNHANOI users may own more than one contract. The exact ``maKhachHang``
    match is authoritative; unique units from the remaining rows are retained
    only as controlled fallback candidates for legacy/shared-account schemas.
    """
    rows = contract_rows(response)
    wanted = str(customer_id or "").strip().upper()
    exact: list[str] = []
    fallback: list[str] = []

    for row in rows:
        current = str(
            row.get("maKhachHang")
            or row.get("maKhang")
            or row.get("userNameOld")
            or ""
        ).strip().upper()
        unit = str(
            row.get("maDonViQuanLy")
            or row.get("maDvql")
            or row.get("maDviqly")
            or ""
        ).strip().upper()
        if not unit:
            continue
        target = exact if current == wanted else fallback
        if unit not in target:
            target.append(unit)

    result = list(exact)
    for unit in fallback:
        if unit not in result:
            result.append(unit)
    return result


def find_management_unit(response: Any, customer_id: str) -> str | None:
    """Return maDonViQuanLy only for an exact customer match."""
    wanted = str(customer_id or "").strip().upper()
    for row in contract_rows(response):
        current = str(
            row.get("maKhachHang")
            or row.get("maKhang")
            or row.get("userNameOld")
            or ""
        ).strip().upper()
        if current != wanted:
            continue
        unit = str(
            row.get("maDonViQuanLy")
            or row.get("maDvql")
            or row.get("maDviqly")
            or ""
        ).strip().upper()
        if unit:
            return unit
    return None


def invoice_identity(
    row: dict[str, Any],
    *,
    fallback_customer_id: str,
    fallback_management_unit: str,
) -> tuple[str, str, int, str]:
    """Return the identifiers required by XemHoaDonByMaKhachHang.

    ``idHdon`` is unique per invoice and must always come from the current
    invoice row. The management unit/customer/type are likewise taken from that
    row when available instead of being inherited from a different meter.
    """
    unit = str(
        row.get("maDonViQuanLy")
        or row.get("maDvql")
        or fallback_management_unit
        or ""
    ).strip().upper()
    customer = str(
        row.get("maKhang")
        or row.get("maKhachHang")
        or row.get("maKhtt")
        or fallback_customer_id
        or ""
    ).strip().upper()
    try:
        invoice_id = int(row.get("idHdon"))
    except (TypeError, ValueError):
        invoice_id = 0
    invoice_type = str(row.get("loaiHdon") or row.get("loaiHoaDon") or "TD").strip() or "TD"
    return unit, customer, invoice_id, invoice_type


def invoice_rows(response: Any) -> list[dict[str, Any]]:
    """Extract dmThongTinHoaDonList from GetThongTinHoaDon."""
    if not isinstance(response, dict) or response.get("isError") is True:
        return []
    data = response.get("data")
    if not isinstance(data, dict):
        return []
    rows = data.get("dmThongTinHoaDonList")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def pdf_base64(response: Any) -> str | None:
    """Return only a base64 payload that decodes to a real PDF signature."""
    if not isinstance(response, dict) or response.get("isError") is True:
        return None
    value = response.get("data")
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        padded = text + "=" * (-len(text) % 4)
        head = base64.b64decode(padded[:128], validate=False)
    except (ValueError, TypeError):
        return None
    if not head.startswith(b"%PDF-"):
        return None
    return text


def normalize_invoice_row(row: dict[str, Any], pdf: str | None = None) -> dict[str, Any]:
    """Normalize website camelCase fields for the integration database/parser."""
    normalized = dict(row)
    # EVNHANOI's ``ky`` is the within-month billing cycle (normally 1), not
    # the calendar month. The generic invoice period parser treats a bare KY as
    # a possible month key, so preserve it under a namespaced field to prevent
    # August 2026 from being misidentified as January 2026.
    if "ky" in normalized:
        normalized["_evnhanoi_ky"] = normalized.pop("ky")
    normalized.update(
        {
            "THANG": row.get("thang"),
            "NAM": row.get("nam"),
            "TONG_TIEN": row.get("tongTien"),
            "TIEN_DIEN": row.get("tongTien"),
            "DIEN_TTHU": row.get("dienTthu"),
            "TTRANG_TTOAN": (
                "DA_TT" if row.get("isDaThanhToan") is True else
                "CHUA_TT" if row.get("isDaThanhToan") is False else ""
            ),
        }
    )
    if pdf:
        # The generic invoice extractor recognizes this attachment-looking key
        # and still magic-sniffs the decoded bytes before writing anything.
        normalized["_invoice_pdf_base64"] = pdf
    return normalized


def strip_binary_payloads(value: Any) -> Any:
    """Return JSON-safe metadata without embedded PDF bytes/base64 strings."""
    if isinstance(value, dict):
        return {
            key: strip_binary_payloads(child)
            for key, child in value.items()
            if key != "_invoice_pdf_base64"
        }
    if isinstance(value, list):
        return [strip_binary_payloads(child) for child in value]
    return value
