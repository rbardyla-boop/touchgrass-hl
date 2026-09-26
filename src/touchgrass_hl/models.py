"""Domain records shared by pure logic. Persistence lives in db/."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Trade:
    tid: str
    coin: str
    market_id: str
    side: str
    aggressor: str
    price: Decimal
    size: Decimal
    notional: Decimal
    hash: str
    time_ms: int
    buyer: str
    seller: str
    idempotency_key: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class BookLevel:
    px: Decimal
    sz: Decimal
    n: int = 1


@dataclass(frozen=True)
class Book:
    coin: str
    time_ms: int | None
    bids: list[BookLevel]
    asks: list[BookLevel]


@dataclass(frozen=True)
class WalkResult:
    vwap: Decimal | None
    filled_size: Decimal
    filled_notional: Decimal
    requested_size: Decimal | None
    requested_notional: Decimal | None
    fully_filled: bool
    slippage_bps: Decimal | None
    mid: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread_bps: Decimal | None
    levels_used: int


@dataclass
class FillRecord:
    time_ms: int
    coin: str
    market_id: str
    price: Decimal
    size: Decimal
    side: str
    start_position: Decimal | None
    closed_pnl: Decimal
    fee: Decimal
    oid: str
    tid: str
    crossed: bool | None
    direction_raw: str
    hash: str = ""


@dataclass
class ClassifiedAction:
    market_id: str
    coin: str
    action: str
    direction: str
    time_ms: int
    price: Decimal
    size: Decimal
    prev_position: Decimal | None
    new_position: Decimal | None
    closed_pnl: Decimal
    tid: str


@dataclass
class Episode:
    market_id: str
    coin: str
    direction: str
    entry_time_ms: int
    exit_time_ms: int | None
    entry_price: Decimal
    exit_price: Decimal | None
    entry_size: Decimal
    closed_pnl: Decimal
    fees: Decimal
    entry_notional: Decimal
    closed: bool
    taker_fills: int = 0
    maker_fills: int = 0
    fill_count: int = 0


@dataclass
class Metrics:
    realized_pnl: Decimal
    hyperliquid_closed_pnl: Decimal
    closed_trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate: Decimal
    profit_factor: Decimal
    profit_factor_capped: bool
    average_return: Decimal
    median_return: Decimal
    average_holding_seconds: Decimal
    median_holding_seconds: Decimal
    max_drawdown: Decimal
    max_drawdown_usd: Decimal
    active_days: int
    profitable_days: int
    profitable_weeks: int
    active_weeks: int
    consistency: Decimal
    best_trade: Decimal
    worst_trade: Decimal
    profit_concentration: Decimal
    markets_traded: int
    herfindahl: Decimal
    long_pnl: Decimal
    short_pnl: Decimal
    long_win_rate: Decimal
    short_win_rate: Decimal
    maker_ratio: Decimal
    taker_ratio: Decimal
    average_position_notional: Decimal
    size_p50: Decimal
    size_p90: Decimal
    data_completeness: Decimal
    lifetime_complete: bool
    completeness_label: str
    copyability: Decimal | None
    copyability_known: bool
    unmatched_closes: int


@dataclass
class ScoreBreakdown:
    score: Decimal
    components: dict[str, Any]
    omitted: list[str]
    verified: bool
    verification_reasons: list[str]


@dataclass
class ActionEvent:
    wallet: str
    market_id: str
    coin: str
    dex: str
    direction: str
    action: str
    time_ms: int
    price: Decimal
    size: Decimal
    prev_position: Decimal | None = None
    new_position: Decimal | None = None


@dataclass
class AccountView:
    open_positions: int
    open_markets: dict[str, str]
    recent_same_direction: bool
    daily_realized_pnl: Decimal
    equity: Decimal
    peak_equity: Decimal
    free_cash: Decimal
    kill_switch: bool


@dataclass
class RiskResult:
    allowed: bool
    vetoes: list[str]
    slippage_bps: Decimal | None
    spread_bps: Decimal | None
    vwap: Decimal | None
    size: Decimal | None
    notional: Decimal | None
    fee_rate: Decimal | None
    fee_assumption: str | None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class JevResult:
    status: str
    model_requested: str
    model_returned: str | None
    answers: dict[str, Any]
    usage: dict[str, Any]
    error: str | None
    raw: dict[str, Any] | None = None
