"""Small shared helpers. No I/O."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_ms() -> int:
    return int(utc_now().timestamp() * 1000)


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def iso_week(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def D(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise ValueError("cannot convert None to Decimal")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not a decimal: {value!r}") from exc


def maybe_d(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return D(value)


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, separators=(",", ":"), sort_keys=True)


def loads(raw: str | None) -> Any:
    if not raw:
        return None
    return json.loads(raw)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).isoformat()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def canon_hash(obj: Any) -> str:
    raw = dumps(obj).encode()
    return hashlib.sha256(raw).hexdigest()


def short_hash(text: str, n: int = 32) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def norm_address(addr: str) -> str:
    return (addr or "").strip().lower()


def is_address(addr: str) -> bool:
    a = norm_address(addr)
    if len(a) != 42 or not a.startswith("0x"):
        return False
    try:
        int(a[2:], 16)
    except ValueError:
        return False
    return True


def market_id(dex_name: str, coin: str) -> str:
    dex = dex_name if dex_name else "core"
    return f"perp:{dex}:{coin}"


def split_market_id(mid: str) -> tuple[str, str]:
    parts = (mid or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "perp":
        return "unknown", mid
    return parts[1], parts[2]


def asset_id_for(perp_dex_index: int, index_in_meta: int) -> int:
    """Official Hyperliquid asset id.

    Primary perp dex (index 0): index in meta universe.
    Builder-deployed: 100000 + perp_dex_index * 10000 + index_in_meta.
    Example: perp_dex_index=1, index_in_meta=0 -> 110000.
    """
    if perp_dex_index <= 0:
        return index_in_meta
    return 100_000 + perp_dex_index * 10_000 + index_in_meta


def median(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def percentile_rank(value: Decimal, population: list[Decimal]) -> Decimal:
    """Midpoint percentile rank in [0, 100]."""
    if not population:
        return Decimal("0")
    below = sum(1 for item in population if item < value)
    equal = sum(1 for item in population if item == value)
    return (Decimal(below) + Decimal(equal) / Decimal(2)) / Decimal(len(population)) * Decimal(100)


def quantize_size(size: Decimal, sz_decimals: int) -> Decimal:
    from decimal import ROUND_DOWN

    quantum = Decimal(10) ** -int(sz_decimals)
    if quantum == 1:
        return size.to_integral_value(rounding=ROUND_DOWN)
    return size.quantize(quantum, rounding=ROUND_DOWN)


def redact(obj: Any) -> Any:
    secret_tokens = ("key", "secret", "private", "authorization", "token", "password")
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            if any(tok in lowered for tok in secret_tokens):
                out[key] = "***" if value else ""
            else:
                out[key] = redact(value)
        return out
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    return obj


def backoff_delay(attempt: int, cap_s: float = 60.0, jitter_unit: float = 0.5) -> float:
    """Exponential backoff with equal jitter.

    delay = uniform(0, min(cap, 2**attempt)). `jitter_unit` in [0, 1] makes the
    function deterministic in tests; production passes a random unit.
    """
    if attempt < 0:
        attempt = 0
    ceiling = min(float(cap_s), float(2**attempt))
    unit = min(1.0, max(0.0, float(jitter_unit)))
    return ceiling * unit
