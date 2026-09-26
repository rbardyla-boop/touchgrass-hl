"""Immutable candidate packet enrichment from registry state and an L2 book."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from touchgrass_hl.hyperliquid_client import book_to_json
from touchgrass_hl.models import Book
from touchgrass_hl.slippage import top_of_book, walk_notional
from touchgrass_hl.util import canon_hash


def enrich_packet(
    structural: dict[str, Any],
    *,
    market: dict[str, Any],
    book: Book | None,
    target_notional: Decimal,
    ws_synced: bool,
) -> dict[str, Any]:
    packet = dict(structural)
    packet["market"] = market.get("coin")
    packet["dex"] = market.get("dex") or packet.get("dex")
    packet["market_status"] = market.get("status")
    packet["open_interest"] = market.get("open_interest")
    packet["funding"] = market.get("funding")
    packet["mark_px"] = market.get("mark_px")
    packet["oracle_px"] = market.get("oracle_px")
    packet["volume"] = market.get("day_ntl_vlm")
    packet["context_time_ms"] = market.get("updated_ms")
    packet["ws_synced"] = ws_synced
    packet["sz_decimals"] = market.get("sz_decimals")
    packet["max_leverage"] = market.get("max_leverage")
    packet["growth_mode"] = market.get("growth_mode")
    packet["deployer_fee_scale"] = market.get("deployer_fee_scale")
    packet["last_fee_scale_change_ms"] = market.get("last_fee_scale_change_ms")
    packet["collateral_token"] = market.get("collateral_token")
    packet["asset_id"] = market.get("asset_id")
    bbo = None
    spread = None
    depth = None
    slip = None
    if book is not None:
        bid, ask, mid, spread_bps = top_of_book(book)
        bbo = {
            "bid": None if bid is None else format(bid, "f"),
            "ask": None if ask is None else format(ask, "f"),
            "mid": None if mid is None else format(mid, "f"),
            "time_ms": book.time_ms,
        }
        spread = None if spread_bps is None else format(spread_bps, "f")
        is_buy = packet.get("direction") == "LONG"
        walked = walk_notional(book, target_notional, is_buy)
        slip = None if walked.slippage_bps is None else format(walked.slippage_bps, "f")
        depth = {
            "fully_filled": walked.fully_filled,
            "filled_notional": format(walked.filled_notional, "f"),
            "levels_used": walked.levels_used,
            "book": loads_book(book),
        }
    packet["bbo"] = bbo
    packet["spread_bps"] = spread
    packet["l2_depth"] = depth
    packet["estimated_slippage_bps"] = slip
    packet["position_context"] = [
        {
            "address": row.get("address"),
            "prev_position": row.get("prev_position"),
            "new_position": row.get("new_position"),
            "action": row.get("action"),
        }
        for row in packet.get("wallet_actions") or []
    ]
    packet["packet_hash"] = canon_hash(_hashable(packet))
    return packet


def loads_book(book: Book) -> dict[str, Any]:
    import json

    return json.loads(book_to_json(book))


def _hashable(packet: dict[str, Any]) -> dict[str, Any]:
    clone = dict(packet)
    clone.pop("packet_hash", None)
    # dumps via canon_hash already sorts. Ensure no raw objects remain.
    return clone


def market_dict(row) -> dict[str, Any]:
    return {
        "market_id": row.market_id,
        "dex": row.dex,
        "coin": row.coin,
        "asset_id": row.asset_id,
        "sz_decimals": row.sz_decimals,
        "max_leverage": row.max_leverage,
        "growth_mode": row.growth_mode,
        "deployer_fee_scale": row.deployer_fee_scale,
        "last_fee_scale_change_ms": row.last_fee_scale_change_ms,
        "collateral_token": row.collateral_token,
        "status": row.status,
        "mark_px": row.mark_px,
        "oracle_px": row.oracle_px,
        "funding": row.funding,
        "open_interest": row.open_interest,
        "day_ntl_vlm": row.day_ntl_vlm,
        "updated_ms": row.updated_ms,
    }


def compact_jev_state(packet: dict[str, Any]) -> dict[str, Any]:
    """Small state for one SystemOne request. Not raw trade history."""
    depth = packet.get("l2_depth") or {}
    book = depth.get("book") or {}
    bids = (book.get("bids") or [])[:5]
    asks = (book.get("asks") or [])[:5]
    return {
        "candidate_id": packet.get("candidate_id"),
        "market_id": packet.get("market_id"),
        "dex": packet.get("dex"),
        "direction": packet.get("direction"),
        "independent_group_count": packet.get("independent_group_count"),
        "behavioral_group_ids": packet.get("behavioral_group_ids"),
        "timestamp_first_wallet_ms": packet.get("timestamp_first_wallet_ms"),
        "timestamp_threshold_ms": packet.get("timestamp_threshold_ms"),
        "price_first": packet.get("price_first"),
        "price_trigger": packet.get("price_trigger"),
        "price_movement": packet.get("price_movement"),
        "mark_px": packet.get("mark_px"),
        "oracle_px": packet.get("oracle_px"),
        "funding": packet.get("funding"),
        "open_interest": packet.get("open_interest"),
        "volume": packet.get("volume"),
        "spread_bps": packet.get("spread_bps"),
        "estimated_slippage_bps": packet.get("estimated_slippage_bps"),
        "market_status": packet.get("market_status"),
        "bbo": packet.get("bbo"),
        "top_bids": bids,
        "top_asks": asks,
        "wallets": [
            {
                "address": row.get("address"),
                "group_id": row.get("group_id"),
                "action": row.get("action"),
                "score": row.get("score"),
                "price": row.get("price"),
                "size": row.get("size"),
            }
            for row in packet.get("wallet_actions") or []
        ],
        "note": "Behavioral groups are not an ownership claim. History may be partial.",
    }
