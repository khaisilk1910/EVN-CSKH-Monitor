"""Helpers for persisted Zalo recipient configuration."""

from __future__ import annotations

from typing import Any

from .const import (
    CONF_ZALO_ACCOUNT_SELECTION,
    CONF_ZALO_RECIPIENT_ENABLED,
    CONF_ZALO_RECIPIENT_NAME,
    CONF_ZALO_RECIPIENTS,
    CONF_ZALO_SEND_DAILY,
    CONF_ZALO_SEND_INVOICE,
    CONF_ZALO_SEND_OUTAGE,
    CONF_ZALO_THREAD_ID,
    CONF_ZALO_TYPE,
    DEFAULT_ZALO_ACCOUNT_SELECTION,
    DEFAULT_ZALO_SEND_DAILY,
    DEFAULT_ZALO_SEND_INVOICE,
    DEFAULT_ZALO_SEND_OUTAGE,
    DEFAULT_ZALO_THREAD_ID,
    DEFAULT_ZALO_TYPE,
)


def normalize_zalo_recipients(options: dict[str, Any]) -> list[dict[str, Any]]:
    """Return validated recipient dictionaries, migrating the old single route.

    The previous release stored one account_selection/thread_id pair directly in
    entry.options. Keeping this compatibility layer lets users upgrade without
    losing the existing Zalo destination. Once the Zalo manager is used, the
    options flow persists the new list format.
    """
    raw = options.get(CONF_ZALO_RECIPIENTS)
    recipients: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for index, item in enumerate(raw, start=1):
            normalized = _normalize_recipient(item, index)
            if normalized is not None:
                recipients.append(normalized)
        return recipients

    account = str(options.get(CONF_ZALO_ACCOUNT_SELECTION, "")).strip()
    thread = str(options.get(CONF_ZALO_THREAD_ID, "")).strip()
    if not account or not thread:
        return []

    return [
        {
            "id": "legacy",
            "name": "Zalo mặc định",
            "enabled": True,
            "type": _normalize_type(options.get(CONF_ZALO_TYPE, DEFAULT_ZALO_TYPE)),
            "account_selection": account,
            "thread_id": thread,
            "send_invoice": bool(
                options.get(CONF_ZALO_SEND_INVOICE, DEFAULT_ZALO_SEND_INVOICE)
            ),
            "send_daily": bool(
                options.get(CONF_ZALO_SEND_DAILY, DEFAULT_ZALO_SEND_DAILY)
            ),
            "send_outage": bool(
                options.get(CONF_ZALO_SEND_OUTAGE, DEFAULT_ZALO_SEND_OUTAGE)
            ),
        }
    ]


def recipient_from_form(
    form: dict[str, Any], recipient_id: str, *, default_name: str
) -> dict[str, Any]:
    """Convert one Home Assistant options form into a persisted recipient."""
    return {
        "id": recipient_id,
        "name": str(form.get(CONF_ZALO_RECIPIENT_NAME, default_name)).strip()
        or default_name,
        "enabled": bool(form.get(CONF_ZALO_RECIPIENT_ENABLED, True)),
        "type": _normalize_type(form.get(CONF_ZALO_TYPE, DEFAULT_ZALO_TYPE)),
        "account_selection": str(
            form.get(CONF_ZALO_ACCOUNT_SELECTION, DEFAULT_ZALO_ACCOUNT_SELECTION)
        ).strip(),
        "thread_id": str(form.get(CONF_ZALO_THREAD_ID, DEFAULT_ZALO_THREAD_ID)).strip(),
        "send_invoice": bool(
            form.get(CONF_ZALO_SEND_INVOICE, DEFAULT_ZALO_SEND_INVOICE)
        ),
        "send_daily": bool(form.get(CONF_ZALO_SEND_DAILY, DEFAULT_ZALO_SEND_DAILY)),
        "send_outage": bool(
            form.get(CONF_ZALO_SEND_OUTAGE, DEFAULT_ZALO_SEND_OUTAGE)
        ),
    }


def form_defaults(recipient: dict[str, Any]) -> dict[str, Any]:
    """Return field defaults used by add/edit forms."""
    return {
        CONF_ZALO_RECIPIENT_NAME: str(recipient.get("name") or ""),
        CONF_ZALO_RECIPIENT_ENABLED: bool(recipient.get("enabled", True)),
        CONF_ZALO_TYPE: str(_normalize_type(recipient.get("type", DEFAULT_ZALO_TYPE))),
        CONF_ZALO_ACCOUNT_SELECTION: str(recipient.get("account_selection") or ""),
        CONF_ZALO_THREAD_ID: str(recipient.get("thread_id") or ""),
        CONF_ZALO_SEND_INVOICE: bool(recipient.get("send_invoice", False)),
        CONF_ZALO_SEND_DAILY: bool(recipient.get("send_daily", False)),
        CONF_ZALO_SEND_OUTAGE: bool(recipient.get("send_outage", False)),
    }


def without_legacy_zalo_options(options: dict[str, Any]) -> dict[str, Any]:
    """Drop superseded single-recipient option keys."""
    result = dict(options)
    for key in (
        CONF_ZALO_TYPE,
        CONF_ZALO_ACCOUNT_SELECTION,
        CONF_ZALO_THREAD_ID,
        CONF_ZALO_SEND_INVOICE,
        CONF_ZALO_SEND_DAILY,
        CONF_ZALO_SEND_OUTAGE,
    ):
        result.pop(key, None)
    return result


def _normalize_recipient(item: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    account = str(item.get("account_selection") or "").strip()
    thread = str(item.get("thread_id") or "").strip()
    recipient_id = str(item.get("id") or f"recipient-{index}").strip()
    if not recipient_id or not account or not thread:
        return None
    return {
        "id": recipient_id,
        "name": str(item.get("name") or f"Zalo {index}").strip() or f"Zalo {index}",
        "enabled": bool(item.get("enabled", True)),
        "type": _normalize_type(item.get("type", DEFAULT_ZALO_TYPE)),
        "account_selection": account,
        "thread_id": thread,
        "send_invoice": bool(item.get("send_invoice", False)),
        "send_daily": bool(item.get("send_daily", False)),
        "send_outage": bool(item.get("send_outage", False)),
    }


def _normalize_type(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ZALO_TYPE
    return 1 if parsed == 1 else 0
