"""Hard deterministic vetoes. No model output can override these."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from touchgrass_hl.models import AccountView, Book, RiskResult
from touchgrass_hl.slippage import walk_notional, walk_size
from touchgrass_hl.util import quantize_size


@dataclass(frozen=True)
class RiskConfig:
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    max_price_move_bps: Decimal
    stale_market_seconds: int
    min_groups: int
    max_positions: int
    target_notional: Decimal
    daily_loss_limit_usd: Decimal
    max_drawdown_pct: Decimal
    sz_decimals: int
    fee_rate: Decimal
    fee_assumption: str
    cooldown_seconds: int = 900
    supported_collateral: frozenset[int] = field(default_factory=lambda: frozenset({0}))


def evaluate_risk(
    *,
    packet: dict[str, Any],
    book: Book | None,
    account: AccountView,
    cfg: RiskConfig,
    now_ms: int,
    ws_synced: bool,
    market_status: str | None,
    context_time_ms: int | None,
    mark_px: Decimal | None,
    participating_verified: bool,
) -> RiskResult:
    vetoes: list[str] = []
    details: dict[str, Any] = {}
    try:
        return _evaluate(
            packet=packet,
            book=book,
            account=account,
            cfg=cfg,
            now_ms=now_ms,
            ws_synced=ws_synced,
            market_status=market_status,
            context_time_ms=context_time_ms,
            mark_px=mark_px,
            participating_verified=participating_verified,
            vetoes=vetoes,
            details=details,
        )
    except Exception as exc:  # fail closed
        vetoes.append("VETO_RISK_ENGINE_ERROR")
        details["error"] = type(exc).__name__
        return RiskResult(
            allowed=False,
            vetoes=vetoes,
            slippage_bps=None,
            spread_bps=None,
            vwap=None,
            size=None,
            notional=None,
            fee_rate=cfg.fee_rate,
            fee_assumption=cfg.fee_assumption,
            details=details,
        )


def _evaluate(
    *,
    packet: dict[str, Any],
    book: Book | None,
    account: AccountView,
    cfg: RiskConfig,
    now_ms: int,
    ws_synced: bool,
    market_status: str | None,
    context_time_ms: int | None,
    mark_px: Decimal | None,
    participating_verified: bool,
    vetoes: list[str],
    details: dict[str, Any],
) -> RiskResult:
    if account.kill_switch:
        vetoes.append("VETO_DRAWDOWN_KILL_SWITCH")
    if account.marks_ok is False:
        vetoes.append("VETO_STALE_OPEN_MARK")
    if packet.get("fee_known") is False:
        vetoes.append("VETO_UNKNOWN_FEE_SCALE")
    if "collateral_token" in packet:
        raw_token = packet.get("collateral_token")
        dex = str(packet.get("dex") or "core")
        if raw_token is None:
            if dex not in ("", "core"):
                vetoes.append("VETO_UNSUPPORTED_COLLATERAL")
        else:
            try:
                token = int(raw_token)
            except (TypeError, ValueError):
                vetoes.append("VETO_UNSUPPORTED_COLLATERAL")
            else:
                if token not in cfg.supported_collateral:
                    vetoes.append("VETO_UNSUPPORTED_COLLATERAL")
    if not ws_synced:
        vetoes.append("VETO_MARKET_DATA_UNSYNCHRONIZED")
    if context_time_ms is None or mark_px is None:
        vetoes.append("VETO_MISSING_MARKET_CONTEXT")
    elif now_ms - context_time_ms > cfg.stale_market_seconds * 1000:
        vetoes.append("VETO_STALE_MARKET_DATA")
    if market_status != "active":
        vetoes.append("VETO_MARKET_HALTED")
    groups = packet.get("behavioral_group_ids") or []
    if len(groups) < cfg.min_groups:
        vetoes.append("VETO_INSUFFICIENT_INDEPENDENT_GROUPS")
    if not participating_verified:
        vetoes.append("VETO_WALLET_DATA_QUALITY")
    market_id = str(packet.get("market_id") or "")
    direction = str(packet.get("direction") or "")
    open_dir = account.open_markets.get(market_id)
    if open_dir:
        if open_dir == direction:
            vetoes.append("VETO_DUPLICATE_SIGNAL")
        else:
            vetoes.append("VETO_EXISTING_CONFLICTING_POSITION")
    if account.recent_same_direction:
        if "VETO_DUPLICATE_SIGNAL" not in vetoes:
            vetoes.append("VETO_DUPLICATE_SIGNAL")
    if account.open_positions >= cfg.max_positions:
        vetoes.append("VETO_MAX_CONCURRENT_POSITIONS")
    if account.daily_realized_pnl <= -cfg.daily_loss_limit_usd:
        vetoes.append("VETO_DAILY_LOSS_LIMIT")
    if account.peak_equity > 0:
        dd = (account.peak_equity - account.equity) / account.peak_equity
        details["drawdown"] = format(dd, "f")
        if dd >= cfg.max_drawdown_pct:
            vetoes.append("VETO_DRAWDOWN_KILL_SWITCH")
    first = Decimal(str(packet.get("price_first") or "0"))
    trigger = Decimal(str(packet.get("price_trigger") or "0"))
    if first > 0 and trigger > 0:
        move_bps = abs(trigger - first) / first * Decimal(10000)
        details["price_move_bps"] = format(move_bps, "f")
        if move_bps > cfg.max_price_move_bps:
            vetoes.append("VETO_PRICE_MOVED_TOO_FAR")
    else:
        vetoes.append("VETO_MISSING_MARKET_CONTEXT")

    slippage_bps = None
    spread_bps = None
    vwap = None
    size = None
    notional = None
    is_buy = direction == "LONG"
    if book is None or (not book.bids and not book.asks) or (is_buy and not book.asks) or (
        not is_buy and not book.bids
    ):
        vetoes.append("VETO_MISSING_ORDER_BOOK")
    else:
        rough = walk_notional(book, cfg.target_notional, is_buy)
        spread_bps = rough.spread_bps
        details["rough_slippage_bps"] = None if rough.slippage_bps is None else format(rough.slippage_bps, "f")
        if spread_bps is None:
            vetoes.append("VETO_MISSING_ORDER_BOOK")
        elif spread_bps > cfg.max_spread_bps:
            vetoes.append("VETO_SPREAD_TOO_WIDE")
        if not rough.fully_filled or rough.vwap is None or rough.filled_size <= 0:
            vetoes.append("VETO_INADEQUATE_LIQUIDITY")
        else:
            size = quantize_size(rough.filled_size, cfg.sz_decimals)
            if size <= 0:
                vetoes.append("VETO_SIZE_ROUNDED_TO_ZERO")
            else:
                actual = walk_size(book, size, is_buy)
                vwap = actual.vwap
                notional = actual.filled_notional
                slippage_bps = actual.slippage_bps
                if not actual.fully_filled or vwap is None:
                    vetoes.append("VETO_INADEQUATE_LIQUIDITY")
                if slippage_bps is not None and slippage_bps > cfg.max_slippage_bps:
                    vetoes.append("VETO_SLIPPAGE_TOO_HIGH")
                fee = (notional or Decimal(0)) * cfg.fee_rate
                if account.free_cash < (notional or Decimal(0)) + fee:
                    vetoes.append("VETO_INSUFFICIENT_PAPER_EQUITY")
    # Deduplicate while preserving order.
    seen = set()
    unique = []
    for code in vetoes:
        if code not in seen:
            seen.add(code)
            unique.append(code)
    return RiskResult(
        allowed=len(unique) == 0,
        vetoes=unique,
        slippage_bps=slippage_bps,
        spread_bps=spread_bps,
        vwap=vwap,
        size=size,
        notional=notional,
        fee_rate=cfg.fee_rate,
        fee_assumption=cfg.fee_assumption,
        details=details,
    )
