"""Deterministic paper accounting. Fills come from walked books, never from mids."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from touchgrass_hl.models import Book
from touchgrass_hl.slippage import walk_size
from touchgrass_hl.util import quantize_size

LANE_RULES_ONLY = "RULES_ONLY"
LANE_RULES_PLUS_JEV = "RULES_PLUS_JEV"
LANES = (LANE_RULES_ONLY, LANE_RULES_PLUS_JEV)


@dataclass
class PaperPosition:
    lane: str
    candidate_id: str
    market_id: str
    coin: str
    direction: str
    size: Decimal
    entry_px: Decimal
    entry_fee: Decimal
    entry_fee_assumption: str
    entry_notional: Decimal
    funding_rate: Decimal
    opened_ms: int
    status: str = "open"
    exit_px: Decimal | None = None
    exit_fee: Decimal | None = None
    exit_fee_assumption: str | None = None
    exit_reason: str | None = None
    closed_ms: int | None = None
    realized_pnl: Decimal | None = None
    funding_pnl: Decimal | None = None
    entry_slippage_bps: Decimal | None = None
    exit_slippage_bps: Decimal | None = None
    fee_inputs: dict | None = None
    exit_fee_inputs: dict | None = None


@dataclass
class AccountState:
    lane: str
    starting_equity: Decimal
    cash: Decimal
    peak_equity: Decimal
    realized_pnl: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    funding: Decimal = Decimal(0)
    day_utc: str = ""
    day_realized: Decimal = Decimal(0)
    wins: int = 0
    losses: int = 0
    positions: list[PaperPosition] | None = None

    def __post_init__(self) -> None:
        if self.positions is None:
            self.positions = []

    def open_positions(self) -> list[PaperPosition]:
        return [p for p in (self.positions or []) if p.status == "open"]

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        unreal = Decimal(0)
        locked = Decimal(0)
        for pos in self.open_positions():
            mark = marks.get(pos.market_id, pos.entry_px)
            unreal += _price_pnl(pos.direction, pos.entry_px, mark, pos.size)
            locked += pos.entry_notional
        return self.cash + locked + unreal

    def free_cash(self) -> Decimal:
        return self.cash


def _price_pnl(direction: str, entry: Decimal, exit_px: Decimal, size: Decimal) -> Decimal:
    if direction == "LONG":
        return (exit_px - entry) * size
    return (entry - exit_px) * size


def marked_account(
    account: AccountState,
    marks: dict[str, Decimal],
    marks_ok: bool,
) -> tuple[Decimal, Decimal, Decimal]:
    """Marked equity, peak, and free cash.

    Free cash is settled cash plus unrealized PnL. Locked entry notional is not
    spendable. Peak equity moves only when every open mark is fresh. A missing
    mark does not invent a flat position: free cash is reported as zero so a
    new entry cannot use a stale book, and the caller must fail closed.
    """
    locked = sum((pos.entry_notional for pos in account.open_positions()), Decimal(0))
    if not marks_ok:
        return account.equity({}), account.peak_equity, Decimal(0)
    equity = account.equity(marks)
    if equity > account.peak_equity:
        account.peak_equity = equity
    return equity, account.peak_equity, equity - locked


def funding_pnl(direction: str, notional: Decimal, funding_rate: Decimal, hold_seconds: int) -> Decimal:
    """Estimate funding from the entry snapshot rate.

    Positive Hyperliquid funding means longs pay shorts. This is an estimate,
    not an exchange funding settlement.
    """
    hours = Decimal(max(0, hold_seconds)) / Decimal(3600)
    payment = funding_rate * notional * hours
    if direction == "LONG":
        return -payment
    return payment


def net_pnl(
    direction: str,
    entry: Decimal,
    exit_px: Decimal,
    size: Decimal,
    entry_fee: Decimal,
    exit_fee: Decimal,
    funding: Decimal,
) -> Decimal:
    return _price_pnl(direction, entry, exit_px, size) - entry_fee - exit_fee + funding


def decide_lane_acceptance(risk_allowed: bool, lane: str, jev_vetoes: list[str]) -> bool:
    """RULES_ONLY ignores Jev. RULES_PLUS_JEV fails closed when any Jev veto is present."""
    if not risk_allowed:
        return False
    if lane == LANE_RULES_PLUS_JEV and jev_vetoes:
        return False
    return True


def exit_reason(
    pos: PaperPosition,
    mark: Decimal,
    now_ms: int,
    *,
    stop_pct: Decimal,
    take_pct: Decimal,
    max_hold_s: int,
) -> str | None:
    if pos.entry_px <= 0:
        return None
    if pos.direction == "LONG":
        if mark <= pos.entry_px * (Decimal(1) - stop_pct):
            return "STOP_LOSS"
        if mark >= pos.entry_px * (Decimal(1) + take_pct):
            return "TAKE_PROFIT"
    else:
        if mark >= pos.entry_px * (Decimal(1) + stop_pct):
            return "STOP_LOSS"
        if mark <= pos.entry_px * (Decimal(1) - take_pct):
            return "TAKE_PROFIT"
    if now_ms - pos.opened_ms >= max_hold_s * 1000:
        return "TIME_EXIT"
    return None


def open_from_book(
    *,
    lane: str,
    candidate_id: str,
    market_id: str,
    coin: str,
    direction: str,
    book: Book,
    target_notional: Decimal,
    sz_decimals: int,
    fee_rate: Decimal,
    fee_assumption: str,
    funding_rate: Decimal,
    now_ms: int,
) -> PaperPosition | None:
    is_buy = direction == "LONG"
    touch = book.asks[0].px if is_buy else book.bids[0].px
    if touch <= 0:
        return None
    raw_size = target_notional / touch
    size = quantize_size(raw_size, sz_decimals)
    if size <= 0:
        return None
    walked = walk_size(book, size, is_buy)
    if not walked.fully_filled or walked.vwap is None:
        return None
    notional = walked.filled_notional
    fee = notional * fee_rate
    return PaperPosition(
        lane=lane,
        candidate_id=candidate_id,
        market_id=market_id,
        coin=coin,
        direction=direction,
        size=walked.filled_size,
        entry_px=walked.vwap,
        entry_fee=fee,
        entry_fee_assumption=fee_assumption,
        entry_notional=notional,
        funding_rate=funding_rate,
        opened_ms=now_ms,
        entry_slippage_bps=walked.slippage_bps,
    )


def apply_open(account: AccountState, pos: PaperPosition) -> bool:
    required = pos.entry_notional + pos.entry_fee
    if account.cash < required:
        return False
    if any(p.market_id == pos.market_id and p.status == "open" for p in account.positions or []):
        return False
    account.cash -= pos.entry_fee
    account.fees += pos.entry_fee
    account.cash -= pos.entry_notional
    account.positions = (account.positions or []) + [pos]
    return True


def close_from_book(
    account: AccountState,
    pos: PaperPosition,
    book: Book,
    *,
    reason: str,
    now_ms: int,
    fee_rate: Decimal,
    fee_assumption: str,
    fee_inputs: dict | None = None,
) -> PaperPosition | None:
    if pos.status != "open":
        return None
    is_buy = pos.direction == "SHORT"
    walked = walk_size(book, pos.size, is_buy)
    if not walked.fully_filled or walked.vwap is None:
        return None
    exit_fee = walked.filled_notional * fee_rate
    hold = max(0, now_ms - pos.opened_ms) // 1000
    fund = funding_pnl(pos.direction, pos.entry_notional, pos.funding_rate, hold)
    realized = net_pnl(pos.direction, pos.entry_px, walked.vwap, pos.size, pos.entry_fee, exit_fee, fund)
    pos.status = "closed"
    pos.exit_px = walked.vwap
    pos.exit_fee = exit_fee
    pos.exit_fee_assumption = fee_assumption
    pos.exit_fee_inputs = fee_inputs
    pos.exit_reason = reason
    pos.closed_ms = now_ms
    pos.funding_pnl = fund
    pos.realized_pnl = realized
    pos.exit_slippage_bps = walked.slippage_bps
    account.cash += pos.entry_notional
    account.cash -= exit_fee
    account.cash += _price_pnl(pos.direction, pos.entry_px, walked.vwap, pos.size)
    account.cash += fund
    account.fees += exit_fee
    account.funding += fund
    account.realized_pnl += realized
    account.day_realized += realized
    if realized > 0:
        account.wins += 1
    elif realized < 0:
        account.losses += 1
    marked = account.cash
    for other in account.open_positions():
        marked += other.entry_notional
    if marked > account.peak_equity:
        account.peak_equity = marked
    return pos


def unrealized(pos: PaperPosition, mark: Decimal) -> Decimal:
    return _price_pnl(pos.direction, pos.entry_px, mark, pos.size)
