"""Install the EVN current-debt hotfix without changing invoice-history files."""

from __future__ import annotations

from datetime import datetime
import logging
import math
from typing import Any

from .coordinator import EVNDataUpdateCoordinator
from .database import (
    EVNDatabase,
    _SQLITE_LOCK,
    _first_value,
    _parse_number,
    _to_int,
)
from .debt import extract_bill_rows, extract_current_debt

_LOGGER = logging.getLogger(__name__)
_DEBT_SEMANTICS_STATE_KEY = "current_debt_semantics_v2"
_PATCH_FLAG = "_evn_current_debt_hotfix_v2"

_ORIGINAL_COORDINATOR_INITIALIZE = EVNDataUpdateCoordinator.async_initialize


def _save_bills_without_debt(
    self: EVNDatabase, customer_id: str, bills: list[dict[str, Any]]
) -> None:
    """Persist official invoice history but NEVER derive current debt from it."""
    rows: list[tuple[Any, ...]] = []
    for bill in bills:
        month = _to_int(_first_value(bill, "THANG", "thang", "month"))
        year = _to_int(_first_value(bill, "NAM", "nam", "year"))
        if month is None or year is None:
            continue
        amount = _parse_number(
            _first_value(
                bill,
                "TONG_TIEN",
                "tong_tien",
                "TIEN_DIEN",
                "tien_dien",
                "SO_TIEN",
                "so_tien",
            )
        )
        consumption = _parse_number(
            _first_value(
                bill,
                "DIEN_TTHU",
                "dien_tthu",
                "SAN_LUONG",
                "san_luong",
            )
        )
        status = str(
            _first_value(
                bill,
                "TTRANG_TTOAN",
                "trang_thai",
                "status",
                "payment_status",
            )
            or ""
        ).strip()
        rows.append(
            (customer_id, month, year, amount, consumption, status, "invoice")
        )

    if not rows:
        return

    with _SQLITE_LOCK, self._connect() as conn:
        conn.executemany(
            """
            INSERT INTO monthly_bill(
                customer_id, thang, nam, tien_dien, san_luong_kwh,
                bill_status, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_id, thang, nam) DO UPDATE SET
                tien_dien=COALESCE(excluded.tien_dien, monthly_bill.tien_dien),
                san_luong_kwh=COALESCE(
                    excluded.san_luong_kwh,
                    monthly_bill.san_luong_kwh
                ),
                bill_status=COALESCE(
                    NULLIF(excluded.bill_status,''),
                    monthly_bill.bill_status
                ),
                source='invoice'
            """,
            rows,
        )
        conn.commit()


def _save_current_debt(
    self: EVNDatabase, customer_id: str, amount: float
) -> None:
    """Persist one authoritative CURRENT debt amount."""
    numeric = float(amount)
    if not math.isfinite(numeric):
        raise ValueError("Debt amount must be finite")
    numeric = max(0.0, numeric)
    now = datetime.now().astimezone().isoformat()
    with _SQLITE_LOCK, self._connect() as conn:
        conn.execute(
            """
            INSERT INTO debt(customer_id, amount, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(customer_id) DO UPDATE SET
                amount=excluded.amount,
                updated_at=excluded.updated_at
            """,
            (customer_id, numeric, now),
        )
        conn.commit()


def _clear_current_debt(self: EVNDatabase, customer_id: str) -> None:
    """Remove legacy/stale debt once when upgrading to the new semantics."""
    with _SQLITE_LOCK, self._connect() as conn:
        conn.execute("DELETE FROM debt WHERE customer_id=?", (customer_id,))
        conn.commit()


async def _patched_async_initialize(self: EVNDataUpdateCoordinator) -> None:
    """Load cache, then discard legacy debt until a new authoritative read."""
    await _ORIGINAL_COORDINATOR_INITIALIZE(self)

    marker = await self.hass.async_add_executor_job(
        self.database.get_state,
        self.customer_id,
        _DEBT_SEMANTICS_STATE_KEY,
    )
    if marker == "1":
        return

    # Old releases could derive debt from invoice-history rows. Do not expose
    # that cached value while waiting for the first authoritative debt response.
    await self.hass.async_add_executor_job(
        self.database.clear_current_debt,
        self.customer_id,
    )
    snapshot = await self.hass.async_add_executor_job(
        self.database.load_snapshot,
        self.customer_id,
    )
    self.data = self._decorate_snapshot(snapshot)


async def _patched_process_bill_result(
    self: EVNDataUpdateCoordinator,
    result: Any,
    errors: list[str],
) -> None:
    """Persist invoice history and current debt through independent paths."""
    if isinstance(result, Exception):
        errors.append(f"bill: {result}")
        return
    if result is None:
        errors.append("bill: no response")
        return

    await self._async_save_raw("bill", result)

    bills = extract_bill_rows(result)
    if bills:
        await self.hass.async_add_executor_job(
            self.database.save_bills,
            self.customer_id,
            bills,
        )

    authoritative, debt = extract_current_debt(result, self.api.region)
    if authoritative and debt is not None:
        await self.hass.async_add_executor_job(
            self.database.save_current_debt,
            self.customer_id,
            debt,
        )
        await self.hass.async_add_executor_job(
            self.database.set_state,
            self.customer_id,
            _DEBT_SEMANTICS_STATE_KEY,
            "1",
        )
        _LOGGER.debug(
            "Updated authoritative current debt for %s (%s): %.0f VND",
            self.customer_id,
            self.api.region,
            debt,
        )
    else:
        _LOGGER.debug(
            "EVN bill response for %s (%s) did not contain authoritative "
            "current-debt semantics; debt was not guessed from invoice totals",
            self.customer_id,
            self.api.region,
        )

    if bills:
        await self._async_extract_invoice_files(
            bills,
            source_hint="bill",
            resource_base_url=f"{self.api.base_url.rstrip('/')}/",
        )


def install_debt_fix() -> None:
    """Patch integration-owned classes exactly once."""
    if getattr(EVNDatabase, _PATCH_FLAG, False):
        return

    EVNDatabase.save_bills = _save_bills_without_debt
    EVNDatabase.save_current_debt = _save_current_debt
    EVNDatabase.clear_current_debt = _clear_current_debt

    EVNDataUpdateCoordinator.async_initialize = _patched_async_initialize
    EVNDataUpdateCoordinator._async_process_bill_result = _patched_process_bill_result

    setattr(EVNDatabase, _PATCH_FLAG, True)
    setattr(EVNDataUpdateCoordinator, _PATCH_FLAG, True)
