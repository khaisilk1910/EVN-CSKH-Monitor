"""Pure calculations for EVN CSKH Monitor sensors and notifications."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any


def clamp_day(year: int, month: int, day: int) -> int:
    return min(max(1, day), monthrange(year, month)[1])


def month_shift(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def billing_period_for(day_start: int, today: date) -> tuple[date, date]:
    """Return current billing period start and nominal end date."""
    day_start = min(max(1, int(day_start)), 31)
    this_start_day = clamp_day(today.year, today.month, day_start)
    if today.day >= this_start_day:
        start = date(today.year, today.month, this_start_day)
    else:
        py, pm = month_shift(today.year, today.month, -1)
        start = date(py, pm, clamp_day(py, pm, day_start))
    ny, nm = month_shift(start.year, start.month, 1)
    next_start = date(ny, nm, clamp_day(ny, nm, day_start))
    return start, next_start - timedelta(days=1)


def previous_period(start: date, day_start: int) -> tuple[date, date]:
    py, pm = month_shift(start.year, start.month, -1)
    prev_start = date(py, pm, clamp_day(py, pm, day_start))
    return prev_start, start - timedelta(days=1)


def period_chain(day_start: int, today: date) -> list[tuple[date, date]]:
    current_start, current_end = billing_period_for(day_start, today)
    prev_start, prev_end = previous_period(current_start, day_start)
    prev2_start, prev2_end = previous_period(prev_start, day_start)
    return [(current_start, current_end), (prev_start, prev_end), (prev2_start, prev2_end)]


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def daily_map(snapshot: dict[str, Any]) -> dict[date, dict[str, Any]]:
    result: dict[date, dict[str, Any]] = {}
    for row in snapshot.get("daily", []):
        dt = parse_iso_date(row.get("date"))
        if dt:
            result[dt] = row
    return result


def nearest_reading(
    snapshot: dict[str, Any], target: date, *, direction: str
) -> tuple[float | None, date | None]:
    rows = []
    for row in snapshot.get("daily", []):
        dt = parse_iso_date(row.get("date"))
        reading = row.get("reading")
        if dt is None or reading is None:
            continue
        try:
            numeric = float(reading)
        except (TypeError, ValueError):
            continue
        rows.append((dt, numeric))
    if direction == "before":
        candidates = [item for item in rows if item[0] <= target]
        if not candidates:
            return None, None
        dt, reading = max(candidates, key=lambda item: item[0])
        return reading, dt
    candidates = [item for item in rows if item[0] >= target]
    if not candidates:
        return None, None
    dt, reading = min(candidates, key=lambda item: item[0])
    return reading, dt


def consumption_on(snapshot: dict[str, Any], target: date) -> float | None:
    row = daily_map(snapshot).get(target)
    if not row:
        return None
    value = row.get("consumption")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def period_rows(snapshot: dict[str, Any], start: date, end: date) -> list[dict[str, Any]]:
    rows = []
    for row in snapshot.get("daily", []):
        dt = parse_iso_date(row.get("date"))
        if dt and start <= dt <= end:
            rows.append(row)
    rows.sort(key=lambda item: item.get("date") or "")
    return rows


def period_consumption(snapshot: dict[str, Any], start: date, end: date) -> float | None:
    rows = period_rows(snapshot, start, end)
    total = 0.0
    count = 0
    for row in rows:
        value = row.get("consumption")
        if value is None:
            continue
        try:
            total += float(value)
            count += 1
        except (TypeError, ValueError):
            continue
    if count:
        return round(total, 3)
    # Fallback to meter-index delta when daily production is unavailable.
    before_start, _ = nearest_reading(snapshot, start - timedelta(days=1), direction="before")
    end_reading, _ = nearest_reading(snapshot, end, direction="before")
    if before_start is not None and end_reading is not None and end_reading >= before_start:
        return round(end_reading - before_start, 3)
    return None


def official_month(snapshot: dict[str, Any], month: int, year: int) -> dict[str, Any] | None:
    for row in snapshot.get("monthly", []):
        if int(row.get("month") or 0) == month and int(row.get("year") or 0) == year:
            return row
    return None


def official_cost_for_period(
    snapshot: dict[str, Any], start: date, end: date, billing_start_day: int
) -> float | None:
    """Only use an EVN invoice when the period is exactly a calendar month."""
    if billing_start_day != 1 or start.day != 1:
        return None
    if end != date(start.year, start.month, monthrange(start.year, start.month)[1]):
        return None
    row = official_month(snapshot, start.month, start.year)
    if not row or row.get("cost") is None:
        return None
    try:
        return float(row["cost"])
    except (TypeError, ValueError):
        return None


def estimate_electricity_cost(kwh: float | None) -> tuple[float | None, dict[str, Any]]:
    """Estimate residential tariff using the tariff table retained by the project.
    Official invoice amounts from EVN always take priority where available.
    """
    if kwh is None:
        return None, {
            "estimated": True,
            "pre_tax": None,
            "tax": None,
            "tiers": [],
            "reason": "no_consumption_data",
        }
    if kwh <= 0:
        return 0.0, {"estimated": True, "pre_tax": 0.0, "tax": 0.0, "tiers": []}
    tiers = [
        (50, 1984),
        (50, 2050),
        (100, 2380),
        (100, 2998),
        (100, 3350),
        (float("inf"), 3460),
    ]
    remaining = float(kwh)
    pre_tax = 0.0
    details = []
    for index, (limit, price) in enumerate(tiers, 1):
        used = min(remaining, limit)
        if used <= 0:
            break
        cost = used * price
        pre_tax += cost
        details.append({"tier": index, "price": price, "kwh": round(used, 3), "cost": round(cost)})
        remaining -= used
        if remaining <= 0:
            break
    tax = pre_tax * 0.08
    total = pre_tax + tax
    return round(total), {
        "estimated": True,
        "pre_tax": round(pre_tax),
        "tax": round(tax),
        "tiers": details,
    }


def period_cost(
    snapshot: dict[str, Any],
    start: date,
    end: date,
    billing_start_day: int,
) -> tuple[float | None, dict[str, Any]]:
    official = official_cost_for_period(snapshot, start, end, billing_start_day)
    if official is not None:
        return official, {"estimated": False, "source": "EVN invoice"}
    kwh = period_consumption(snapshot, start, end)
    total, details = estimate_electricity_cost(kwh)
    details["source"] = "local estimate"
    return total, details


def format_number(value: float | int | None, decimals: int = 2) -> int | float | str:
    if value is None:
        return "N/A"
    rounded = round(float(value), decimals)
    return int(rounded) if rounded.is_integer() else rounded


def future_outages(snapshot: dict[str, Any], today: date) -> list[dict[str, Any]]:
    rows = []
    for item in snapshot.get("outages", []):
        try:
            dt = datetime.strptime(item.get("start_date") or "", "%d-%m-%Y").date()
        except ValueError:
            continue
        if dt >= today:
            rows.append({**item, "_date": dt})
    rows.sort(key=lambda item: (item["_date"], item.get("start_time") or ""))
    return rows
