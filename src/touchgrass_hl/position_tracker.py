"""Signed-position classification and episode reconstruction.

Hyperliquid startPosition is the position before the fill. Buyer increases the
signed position; seller decreases it. Unknown prior position is UNKNOWN and is
not treated as a new bullish or bearish entry.
"""

from __future__ import annotations

from decimal import Decimal

from touchgrass_hl.models import ClassifiedAction, Episode, FillRecord

OPEN_ADD = frozenset({"OPEN_LONG", "ADD_LONG", "OPEN_SHORT", "ADD_SHORT"})
LONG_ACTIONS = frozenset({"OPEN_LONG", "ADD_LONG", "REDUCE_LONG", "CLOSE_LONG", "FLIP_LONG_TO_SHORT"})
SHORT_ACTIONS = frozenset(
    {"OPEN_SHORT", "ADD_SHORT", "REDUCE_SHORT", "CLOSE_SHORT", "FLIP_SHORT_TO_LONG"}
)


def aggressor_from_side(side: str) -> str:
    token = (side or "").strip().upper()
    if token in {"B", "BUY", "BID"}:
        return "BUY"
    if token in {"A", "S", "SELL", "ASK"}:
        return "SELL"
    return "UNKNOWN"


def signed_delta(side: str, size: Decimal) -> Decimal:
    aggressor = aggressor_from_side(side)
    if aggressor == "BUY":
        return size
    if aggressor == "SELL":
        return -size
    raise ValueError(f"unknown side {side!r}")


def direction_of(action: str) -> str:
    if action in LONG_ACTIONS:
        return "LONG"
    if action in SHORT_ACTIONS:
        return "SHORT"
    return "NONE"


def classify_action(prev: Decimal | None, side: str, size: Decimal) -> str:
    if prev is None:
        return "UNKNOWN"
    if size is None or size <= 0:
        return "UNKNOWN"
    if aggressor_from_side(side) == "UNKNOWN":
        return "UNKNOWN"
    new = prev + signed_delta(side, size)
    if prev == 0 and new > 0:
        return "OPEN_LONG"
    if prev == 0 and new < 0:
        return "OPEN_SHORT"
    if prev > 0 and new > prev:
        return "ADD_LONG"
    if prev > 0 and new == 0:
        return "CLOSE_LONG"
    if prev > 0 and new > 0:
        return "REDUCE_LONG"
    if prev > 0 and new < 0:
        return "FLIP_LONG_TO_SHORT"
    if prev < 0 and new < prev:
        return "ADD_SHORT"
    if prev < 0 and new == 0:
        return "CLOSE_SHORT"
    if prev < 0 and new < 0:
        return "REDUCE_SHORT"
    if prev < 0 and new > 0:
        return "FLIP_SHORT_TO_LONG"
    return "UNKNOWN"


def apply_trade_to_position(prev: Decimal | None, side: str, size: Decimal) -> Decimal | None:
    """Update a known signed position. Unknown stays unknown."""
    if prev is None:
        return None
    if aggressor_from_side(side) == "UNKNOWN" or size <= 0:
        return prev
    return prev + signed_delta(side, size)


def _empty_episode(fill: FillRecord, direction: str, size: Decimal) -> Episode:
    return Episode(
        market_id=fill.market_id,
        coin=fill.coin,
        direction=direction,
        entry_time_ms=fill.time_ms,
        exit_time_ms=None,
        entry_price=fill.price,
        exit_price=None,
        entry_size=size,
        closed_pnl=fill.closed_pnl,
        fees=fill.fee,
        entry_notional=fill.price * size,
        closed=False,
        taker_fills=1 if fill.crossed is True else 0,
        maker_fills=1 if fill.crossed is False else 0,
        fill_count=1,
    )


def _add_entry(ep: Episode, fill: FillRecord, add_size: Decimal) -> None:
    notional = ep.entry_price * ep.entry_size + fill.price * add_size
    ep.entry_size += add_size
    if ep.entry_size > 0:
        ep.entry_price = notional / ep.entry_size
        ep.entry_notional = ep.entry_price * ep.entry_size
    ep.fees += fill.fee
    ep.closed_pnl += fill.closed_pnl
    ep.fill_count += 1
    if fill.crossed is True:
        ep.taker_fills += 1
    elif fill.crossed is False:
        ep.maker_fills += 1


def _note_exit(ep: Episode, fill: FillRecord, exit_size: Decimal) -> None:
    already = getattr(ep, "_exit_size", Decimal(0))
    new_size = already + exit_size
    new_notional = (ep.exit_price or Decimal(0)) * already + fill.price * exit_size
    ep.exit_price = new_notional / new_size if new_size > 0 else fill.price
    setattr(ep, "_exit_size", new_size)
    ep.exit_time_ms = fill.time_ms


def _account_fill(ep: Episode, fill: FillRecord, pnl: Decimal, fee: Decimal, exit_size: Decimal | None) -> None:
    """Add one fill's closedPnl exactly once. Exit VWAP is optional."""
    if exit_size is not None:
        _note_exit(ep, fill, exit_size)
    ep.closed_pnl += pnl
    ep.fees += fee
    ep.fill_count += 1
    if fill.crossed is True:
        ep.taker_fills += 1
    elif fill.crossed is False:
        ep.maker_fills += 1


def allocate_flip(fill: FillRecord, closed_size: Decimal, opened_size: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Split one flip fill so the two legs sum to that fill's closedPnl and fee.

    Hyperliquid puts the fee inside closedPnl. The newly opened size is charged
    its share of the fee (a positive fee is a negative closedPnl, matching an
    opening fill). The closing leg keeps the rest, which is price PnL plus the
    closing share of the fee.
    """
    total = closed_size + opened_size
    if total <= 0:
        return fill.closed_pnl, Decimal(0), fill.fee, Decimal(0)
    open_fee = fill.fee * (opened_size / total)
    close_fee = fill.fee - open_fee
    open_pnl = -open_fee
    close_pnl = fill.closed_pnl - open_pnl
    return close_pnl, open_pnl, close_fee, open_fee


def _close_episode(ep: Episode, fill: FillRecord, exit_size: Decimal) -> None:
    _account_fill(ep, fill, fill.closed_pnl, fill.fee, exit_size)


def reconstruct(fills: list[FillRecord]) -> tuple[list[Episode], list[ClassifiedAction], int]:
    """Rebuild closed and open episodes from fills sorted per market.

    Returns episodes (open and closed), classified actions, and unmatched close count.
    Realized episode PnL is the sum of Hyperliquid closedPnl on every fill
    attached to the episode, including opens and adds. An opening fill's
    closedPnl is the fee. A closing fill's closedPnl is the fee plus realized
    price PnL. Each fill is applied once.
    """
    grouped: dict[str, list[FillRecord]] = {}
    for fill in fills:
        grouped.setdefault(fill.market_id, []).append(fill)
    episodes: list[Episode] = []
    actions: list[ClassifiedAction] = []
    unmatched = 0
    for market_id, items in grouped.items():
        items.sort(key=lambda f: (f.time_ms, f.tid))
        current: Episode | None = None
        for fill in items:
            prev = fill.start_position
            action = classify_action(prev, fill.side, fill.size)
            new = None
            if prev is not None and aggressor_from_side(fill.side) != "UNKNOWN" and fill.size > 0:
                new = prev + signed_delta(fill.side, fill.size)
            actions.append(
                ClassifiedAction(
                    market_id=market_id,
                    coin=fill.coin,
                    action=action,
                    direction=direction_of(action),
                    time_ms=fill.time_ms,
                    price=fill.price,
                    size=fill.size,
                    prev_position=prev,
                    new_position=new,
                    closed_pnl=fill.closed_pnl,
                    tid=fill.tid,
                )
            )
            if action in {"OPEN_LONG", "OPEN_SHORT"}:
                if current is not None and not current.closed:
                    current = None
                direction = "LONG" if action == "OPEN_LONG" else "SHORT"
                current = _empty_episode(fill, direction, fill.size)
                episodes.append(current)
            elif action in {"ADD_LONG", "ADD_SHORT"} and current is not None and not current.closed:
                _add_entry(current, fill, fill.size)
            elif action in {"REDUCE_LONG", "REDUCE_SHORT"} and current is not None and not current.closed:
                reduced = abs((new or Decimal(0)) - (prev or Decimal(0)))
                _close_episode(current, fill, reduced if reduced > 0 else fill.size)
            elif action in {"CLOSE_LONG", "CLOSE_SHORT"} and current is not None and not current.closed:
                _close_episode(current, fill, fill.size)
                current.closed = True
                current = None
            elif action in {"FLIP_LONG_TO_SHORT", "FLIP_SHORT_TO_LONG"}:
                if prev is None or new is None:
                    unmatched += 1
                    current = None
                    continue
                closed_size = abs(prev)
                opened = abs(new)
                close_pnl, open_pnl, close_fee, open_fee = allocate_flip(fill, closed_size, opened)
                if current is not None and not current.closed:
                    _account_fill(current, fill, close_pnl, close_fee, closed_size)
                    current.closed = True
                else:
                    unmatched += 1
                direction = "SHORT" if action == "FLIP_LONG_TO_SHORT" else "LONG"
                nxt = _empty_episode(fill, direction, opened)
                nxt.closed_pnl = open_pnl
                nxt.fees = open_fee
                episodes.append(nxt)
                current = nxt
            elif action in {"CLOSE_LONG", "CLOSE_SHORT", "REDUCE_LONG", "REDUCE_SHORT"}:
                unmatched += 1
            elif action in {"FLIP_LONG_TO_SHORT", "FLIP_SHORT_TO_LONG"}:
                unmatched += 1
            else:
                if current is not None and action == "UNKNOWN":
                    current = None
    return episodes, actions, unmatched


def closed_episodes(episodes: list[Episode]) -> list[Episode]:
    return [ep for ep in episodes if ep.closed]
