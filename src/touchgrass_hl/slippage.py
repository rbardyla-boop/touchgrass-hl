"""Order-book walking. Never assumes a mid-price fill."""

from __future__ import annotations

from decimal import Decimal

from touchgrass_hl.models import Book, BookLevel, WalkResult
from touchgrass_hl.util import D


def _sort_book(book: Book) -> tuple[list[BookLevel], list[BookLevel]]:
    bids = sorted(book.bids, key=lambda level: level.px, reverse=True)
    asks = sorted(book.asks, key=lambda level: level.px)
    return bids, asks


def top_of_book(book: Book) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    bids, asks = _sort_book(book)
    bid = bids[0].px if bids else None
    ask = asks[0].px if asks else None
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return bid, ask, None, None
    mid = (bid + ask) / Decimal(2)
    spread_bps = (ask - bid) / mid * Decimal(10000)
    return bid, ask, mid, spread_bps


def walk_size(book: Book, size: Decimal, is_buy: bool) -> WalkResult:
    """Walk the opposite side until `size` base units are filled."""
    bids, asks = _sort_book(book)
    bid, ask, mid, spread_bps = top_of_book(book)
    side = asks if is_buy else bids
    remaining = D(size)
    if remaining < 0:
        remaining = Decimal(0)
    filled = Decimal(0)
    cost = Decimal(0)
    used = 0
    for level in side:
        if level.px <= 0 or level.sz <= 0:
            continue
        take = level.sz if level.sz < remaining else remaining
        if take <= 0:
            break
        cost += take * level.px
        filled += take
        remaining -= take
        used += 1
        if remaining <= 0:
            break
    fully = remaining <= 0 and filled > 0
    vwap = (cost / filled) if filled > 0 else None
    slip = None
    if vwap is not None and mid is not None and mid > 0:
        if is_buy:
            slip = (vwap - mid) / mid * Decimal(10000)
        else:
            slip = (mid - vwap) / mid * Decimal(10000)
    return WalkResult(
        vwap=vwap,
        filled_size=filled,
        filled_notional=cost,
        requested_size=D(size),
        requested_notional=None,
        fully_filled=fully,
        slippage_bps=slip,
        mid=mid,
        best_bid=bid,
        best_ask=ask,
        spread_bps=spread_bps,
        levels_used=used,
    )


def walk_notional(book: Book, notional: Decimal, is_buy: bool) -> WalkResult:
    """Spend up to `notional` quote units walking the book. VWAP is quote/base."""
    bids, asks = _sort_book(book)
    bid, ask, mid, spread_bps = top_of_book(book)
    side = asks if is_buy else bids
    remaining = D(notional)
    if remaining < 0:
        remaining = Decimal(0)
    filled = Decimal(0)
    cost = Decimal(0)
    used = 0
    for level in side:
        if level.px <= 0 or level.sz <= 0:
            continue
        level_notional = level.px * level.sz
        take_notional = level_notional if level_notional < remaining else remaining
        if take_notional <= 0:
            break
        take_sz = take_notional / level.px
        cost += take_notional
        filled += take_sz
        remaining -= take_notional
        used += 1
        if remaining <= 0:
            break
    fully = remaining <= 0 and filled > 0
    vwap = (cost / filled) if filled > 0 else None
    slip = None
    if vwap is not None and mid is not None and mid > 0:
        if is_buy:
            slip = (vwap - mid) / mid * Decimal(10000)
        else:
            slip = (mid - vwap) / mid * Decimal(10000)
    return WalkResult(
        vwap=vwap,
        filled_size=filled,
        filled_notional=cost,
        requested_size=None,
        requested_notional=D(notional),
        fully_filled=fully,
        slippage_bps=slip,
        mid=mid,
        best_bid=bid,
        best_ask=ask,
        spread_bps=spread_bps,
        levels_used=used,
    )
