"""Defensive parsing of the live gateway's responses (docs/LIVE.md) against the
configured field candidates in `market_source_config.polymarket_us.live.response_fields`.
Every field is optional; a record without the one field that identifies it (an order
id, a fill id) is dropped by the caller. Money arrives in `money_unit` (dollars by
default) and leaves as integer cents."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from host.exchange.adapters.base import parse_time
from host.exchange.adapters.polymarket_us import pick


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def text(value: Any) -> str | None:
    return None if value is None else str(value)


def iso(value: datetime) -> str:
    """UTC ISO 8601 with milliseconds and a Z suffix, the way requests send times."""
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def field(record: dict[str, Any], live: dict[str, Any], name: str) -> Any:
    """The first present candidate for a logical response field."""
    return pick(record, live["response_fields"][name])


def unwrap(payload: Any, live: dict[str, Any]) -> Any:
    """Descend through `{"order": {...}}`, `{"data": {...}}` style wrappers (at most 3)."""
    for _ in range(3):
        if not isinstance(payload, dict):
            break
        inner = next((payload[k] for k in live["wrapper_keys"] if isinstance(payload.get(k), dict)), None)
        if inner is None:
            break
        payload = inner
    return payload


def records(payload: Any, live: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The list of objects in a list payload or under one of `list_keys` (possibly
    inside a wrapper); None when the payload is not a list of anything."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in live["list_keys"]:
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
        if any(isinstance(payload.get(k), dict) for k in live["wrapper_keys"]):
            return records(unwrap(payload, live), live)
    return None


def cents(value: Any, live: dict[str, Any]) -> int | None:
    amount = number(value)
    if amount is None:
        return None
    if str(live.get("money_unit", "dollars")).lower() == "cents":
        return int(round(amount))
    return int(round(amount * 100))


def order_dict(record: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """An open-order record as the executor wants it (the record must carry an id)."""
    size = number(field(record, live, "size"))
    filled = number(field(record, live, "filled_size"))
    return {
        "exchange_order_id": str(field(record, live, "order_id")),
        "client_order_id": text(field(record, live, "client_order_id")),
        "market_ref": text(field(record, live, "market_id")),
        "price": number(field(record, live, "price")),
        "size": None if size is None else int(size),
        "filled_size": 0 if filled is None else int(filled),
        "status": text(field(record, live, "status")),
    }


def fill_dict(record: dict[str, Any], live: dict[str, Any]) -> dict[str, Any] | None:
    """A fill record for `orders.record_fill`; None without a fill id or a size."""
    size = number(field(record, live, "size"))
    fill_id = field(record, live, "fill_id")
    if fill_id is None or size is None:
        return None
    return {
        "exchange_fill_id": str(fill_id),
        "exchange_order_id": text(field(record, live, "order_id")),
        "client_order_id": text(field(record, live, "client_order_id")),
        "price": number(field(record, live, "price")),
        "size": int(size),
        "fee_cents": cents(field(record, live, "fee"), live) or 0,
        "ts": parse_time(field(record, live, "fill_time")),
    }


def since_query(since: datetime | None, spec: dict[str, Any]) -> dict[str, str]:
    """The `since` query parameter of the fills call in the configured format."""
    if since is None or not spec.get("since_param"):
        return {}
    fmt = str(spec.get("since_format") or "iso").lower()
    if fmt == "iso":
        value = iso(since)
    else:
        value = str(int(since.timestamp() * (1000 if fmt == "ms" else 1)))
    return {str(spec["since_param"]): value}
