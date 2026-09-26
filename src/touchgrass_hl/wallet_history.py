"""Candidate-wallet history. Partial Hyperliquid history is never called complete."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from touchgrass_hl.db.repo import save_copy_obs, store_wallet_fills, wallet_fill_key
from touchgrass_hl.db.schema import TradeRow, WalletFillRow
from touchgrass_hl.hyperliquid_client import HyperliquidREST, parse_user_fill
from touchgrass_hl.logging_setup import log
from touchgrass_hl.models import FillRecord
from touchgrass_hl.rate_limit import PRIORITY_DISCOVERY, PRIORITY_VERIFIED
from touchgrass_hl.util import D, maybe_d, utc_now_ms
from touchgrass_hl.wallet_scoring import copyability_observations, summarize_copyability

logger = logging.getLogger("touchgrass.history")

API_PAGE = 2000
API_RECENT_CAP = 10000


async def download_fills(
    client: HyperliquidREST,
    address: str,
    coin_to_market,
    lookback_days: int,
) -> dict[str, Any]:
    start = utc_now_ms() - lookback_days * 86_400_000
    cursor = start
    seen: set[str] = set()
    fills: list[dict[str, Any]] = []
    pages = 0
    window_complete = False
    hit_cap = False
    while pages < 6:
        batch = await client.user_fills_by_time(address, cursor, priority=PRIORITY_DISCOVERY)
        pages += 1
        if not isinstance(batch, list):
            raise RuntimeError("userFillsByTime did not return a list")
        if not batch:
            window_complete = True
            break
        stamped = [int(raw["time"]) for raw in batch if isinstance(raw, dict) and "time" in raw]
        if not stamped:
            window_complete = True
            break
        for raw in batch:
            parsed = parse_user_fill(raw, coin_to_market)
            if parsed is None:
                continue
            key = wallet_fill_key(int(parsed["time_ms"]), str(parsed["coin"]), str(parsed["tid"]))
            if key in seen:
                continue
            seen.add(key)
            fills.append(parsed)
        last_time = max(stamped)
        if len(batch) < API_PAGE:
            window_complete = True
            break
        if last_time + 1 <= cursor:
            hit_cap = True
            break
        cursor = last_time + 1
        if len(fills) >= API_RECENT_CAP:
            hit_cap = True
            break
    if hit_cap or len(fills) >= API_RECENT_CAP:
        label = "partial_hyperliquid_recent_history_cap"
        limited = True
        window_complete = False
    elif window_complete:
        label = "requested_window_returned_but_not_lifetime_complete"
        limited = True
    else:
        label = "partial_hyperliquid_recent_history"
        limited = True
    portfolio = None
    try:
        portfolio = await client.portfolio(address, priority=PRIORITY_DISCOVERY)
    except Exception as exc:
        log(logger, logging.WARNING, "portfolio_failed", address=address, error=type(exc).__name__)
    return {
        "fills": fills,
        "label": label,
        "limited": limited,
        "window_complete": window_complete and not hit_cap,
        "lifetime_complete": False,
        "portfolio": portfolio,
        "pages": pages,
    }


def row_to_fill(row: WalletFillRow) -> FillRecord:
    crossed = None if row.crossed is None else bool(row.crossed)
    return FillRecord(
        time_ms=row.time_ms,
        coin=row.coin,
        market_id=row.market_id,
        price=D(row.price),
        size=D(row.size),
        side=row.side,
        start_position=maybe_d(row.start_position),
        closed_pnl=D(row.closed_pnl or "0"),
        fee=D(row.fee or "0"),
        oid=row.oid,
        tid=row.tid,
        crossed=crossed,
        direction_raw=row.direction_raw,
        hash=row.hash,
    )


def episode_copyability(
    session: Session,
    address: str,
    episodes,
    delays: list[int],
) -> Decimal | None:
    observations: list[Decimal | None] = []
    for ep in episodes:
        if not ep.closed:
            continue
        end = ep.entry_time_ms + 30_000
        prints = session.execute(
            select(TradeRow.time_ms, TradeRow.price).where(
                TradeRow.market_id == ep.market_id,
                TradeRow.time_ms >= ep.entry_time_ms,
                TradeRow.time_ms <= end,
            )
        ).all()
        later = [(int(t), D(px)) for t, px in prints]
        obs = copyability_observations(
            leader_px=ep.entry_price,
            direction=ep.direction,
            fill_time_ms=ep.entry_time_ms,
            later_trades=later,
            delays_s=delays,
            exit_px=ep.exit_price,
        )
        save_copy_obs(session, address, ep.market_id, f"ep:{ep.entry_time_ms}:{ep.market_id}", obs)
        for item in obs:
            if item["adverse_move"] is None:
                observations.append(None)
            else:
                observations.append(D(item["adverse_move"]))
    return summarize_copyability(observations)


def store_downloaded_fills(session: Session, address: str, fills: list[dict[str, Any]]) -> int:
    return store_wallet_fills(session, address, fills)


async def refresh_baseline(
    client: HyperliquidREST,
    address: str,
    dexes: list[str],
) -> list[dict[str, Any]]:
    """Latest signed positions from clearinghouseState. One call per dex."""
    out = []
    for dex in dexes:
        state = await client.clearinghouse_state(address, dex, priority=PRIORITY_VERIFIED)
        if not isinstance(state, dict):
            continue
        when = int(state.get("time") or utc_now_ms())
        positions = state.get("assetPositions") or []
        seen = []
        for item in positions:
            pos = (item or {}).get("position") or {}
            coin = str(pos.get("coin") or "")
            if not coin:
                continue
            seen.append(
                {
                    "coin": coin,
                    "dex": dex,
                    "szi": str(pos.get("szi") or "0"),
                    "time_ms": when,
                }
            )
        out.extend(seen)
        if not seen:
            out.append({"coin": "", "dex": dex, "szi": None, "time_ms": when, "empty": True})
    return out
