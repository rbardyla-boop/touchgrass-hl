"""Hyperliquid public-data parsers and REST wrapper.

REST uses hyperliquid.api.API (official SDK HTTP client) so request bodies match
the SDK. WebSocket collection is owned by websocket_manager so reconnect policy
stays explicit. Order signing is only in testnet_executor.
"""

from __future__ import annotations

import logging
from typing import Any

from touchgrass_hl.models import Book, BookLevel, Trade
from touchgrass_hl.position_tracker import aggressor_from_side
from touchgrass_hl.rate_limit import PRIORITY_CANDIDATE, PRIORITY_LIVE, RateLimiter
from touchgrass_hl.util import D, dumps, market_id, norm_address

log = logging.getLogger("touchgrass.hl")


def trade_idempotency_key(tid: str | None, raw: dict[str, Any]) -> str:
    """Hyperliquid `tid` is a 50-bit hash of the two order ids, not a global id.

    The documented unique trade identity is (block time, coin, tid).
    """
    if tid is not None and str(tid) != "":
        coin = str(raw.get("coin") or "")
        when = raw.get("time")
        if coin and when is not None and str(when) != "":
            return f"tid:{when}:{coin}:{tid}"
        return f"tid:{tid}"
    users = raw.get("users") or ["", ""]
    buyer = users[0] if len(users) > 0 else ""
    seller = users[1] if len(users) > 1 else ""
    material = "|".join(
        [
            str(raw.get("hash") or ""),
            str(raw.get("time") or ""),
            str(raw.get("coin") or ""),
            str(raw.get("px") or ""),
            str(raw.get("sz") or ""),
            str(buyer),
            str(seller),
        ]
    )
    return "fallback:" + material


def parse_trade_object(raw: dict[str, Any], market_id_for_coin) -> Trade | None:
    if not isinstance(raw, dict):
        return None
    users = raw.get("users")
    if not isinstance(users, list) or len(users) < 2:
        return None
    buyer = norm_address(str(users[0]))
    seller = norm_address(str(users[1]))
    if not buyer or not seller:
        return None
    tid_raw = raw.get("tid")
    tid = str(tid_raw) if tid_raw is not None else ""
    key = trade_idempotency_key(tid or None, raw)
    # Row identity is the global key. The exchange tid remains inside raw.
    try:
        price = D(raw.get("px"))
        size = D(raw.get("sz"))
        time_ms = int(raw.get("time"))
    except (TypeError, ValueError):
        return None
    if size < 0 or price < 0:
        return None
    coin = str(raw.get("coin") or "")
    if not coin:
        return None
    side = str(raw.get("side") or "")
    mid = market_id_for_coin(coin)
    return Trade(
        tid=key,
        coin=coin,
        market_id=mid,
        side=side,
        aggressor=aggressor_from_side(side),
        price=price,
        size=size,
        notional=price * size,
        hash=str(raw.get("hash") or ""),
        time_ms=time_ms,
        buyer=buyer,
        seller=seller,
        idempotency_key=key,
        raw=raw,
    )


def parse_trades_message(message: dict[str, Any], market_id_for_coin) -> list[Trade]:
    if not isinstance(message, dict) or message.get("channel") != "trades":
        return []
    data = message.get("data")
    if isinstance(data, dict):
        rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        return []
    out = []
    for row in rows:
        trade = parse_trade_object(row, market_id_for_coin)
        if trade is not None:
            out.append(trade)
    return out


def parse_book_payload(payload: dict[str, Any]) -> Book | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    levels = data.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        return None
    bids = _levels(levels[0])
    asks = _levels(levels[1])
    time_ms = data.get("time")
    return Book(
        coin=str(data.get("coin") or ""),
        time_ms=int(time_ms) if time_ms is not None else None,
        bids=bids,
        asks=asks,
    )


def _levels(rows: Any) -> list[BookLevel]:
    out: list[BookLevel] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append(BookLevel(px=D(row.get("px")), sz=D(row.get("sz")), n=int(row.get("n") or 1)))
        except (TypeError, ValueError):
            continue
    return out


def parse_all_dexs_asset_ctxs(message: dict[str, Any]) -> list[tuple[str, int, dict[str, Any]]]:
    """Return (dex_name, index_in_universe, ctx). Empty list if the shape is unrecognized."""
    if not isinstance(message, dict) or message.get("channel") != "allDexsAssetCtxs":
        return []
    data = message.get("data")
    ctxs = None
    if isinstance(data, dict):
        ctxs = data.get("ctxs")
    elif isinstance(data, list):
        ctxs = data
    if not isinstance(ctxs, list):
        return []
    out: list[tuple[str, int, dict[str, Any]]] = []
    for entry in ctxs:
        dex_name = ""
        asset_ctxs = None
        if isinstance(entry, list) and len(entry) >= 2:
            dex_name = "" if entry[0] is None else str(entry[0])
            asset_ctxs = entry[1]
        elif isinstance(entry, dict):
            dex_name = str(entry.get("dex") or entry.get("name") or "")
            asset_ctxs = entry.get("assetCtxs") or entry.get("ctxs")
        if not isinstance(asset_ctxs, list):
            continue
        for index, ctx in enumerate(asset_ctxs):
            if isinstance(ctx, dict):
                out.append((dex_name, index, ctx))
    return out


def parse_user_fill(raw: dict[str, Any], coin_to_market) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        tid = str(raw.get("tid"))
        coin = str(raw.get("coin") or "")
        return {
            "tid": tid,
            "coin": coin,
            "market_id": coin_to_market(coin),
            "time_ms": int(raw["time"]),
            "price": str(raw.get("px")),
            "size": str(raw.get("sz")),
            "side": str(raw.get("side") or ""),
            "direction_raw": str(raw.get("dir") or ""),
            "start_position": None if raw.get("startPosition") is None else str(raw.get("startPosition")),
            "closed_pnl": str(raw.get("closedPnl") or "0"),
            "fee": str(raw.get("fee") or "0"),
            "fee_token": str(raw.get("feeToken") or ""),
            "oid": str(raw.get("oid") or ""),
            "hash": str(raw.get("hash") or ""),
            "crossed": None if raw.get("crossed") is None else bool(raw.get("crossed")),
            "raw": raw,
        }
    except (KeyError, TypeError, ValueError):
        return None


def user_fills_by_time_weight(n_items: int) -> tuple[int, int]:
    """Official weight: 20 plus 1 per 20 items returned."""
    extra = max(0, int(n_items)) // 20
    return 20, extra


class HyperliquidREST:
    """Rate-limited official info endpoint. Public data only."""

    def __init__(self, base_url: str, limiter: RateLimiter, timeout_s: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.limiter = limiter
        self.timeout_s = timeout_s
        self._api = None

    def _client(self):
        if self._api is None:
            from hyperliquid.api import API

            self._api = API(base_url=self.base_url, timeout=self.timeout_s)
        return self._api

    def post_info(self, payload: dict[str, Any], weight: int, priority: int) -> Any:
        # Synchronous on purpose: callers use asyncio.to_thread after acquire,
        # or call acquire themselves. This method does not sleep.
        _ = priority
        return self._client().post("/info", payload)

    async def info(self, payload: dict[str, Any], weight: int, priority: int = PRIORITY_LIVE) -> Any:
        await self.limiter.acquire(weight, priority)
        import asyncio

        try:
            return await asyncio.to_thread(self.post_info, payload, weight, priority)
        except Exception as exc:
            text = str(exc)
            if "429" in text or "rate" in text.lower():
                self.limiter.penalize(self.limiter.capacity)
            raise

    async def perp_dexs(self, priority: int = PRIORITY_LIVE) -> Any:
        return await self.info({"type": "perpDexs"}, 20, priority)

    async def meta_and_asset_ctxs(self, dex: str = "", priority: int = PRIORITY_LIVE) -> Any:
        payload: dict[str, Any] = {"type": "metaAndAssetCtxs"}
        if dex:
            payload["dex"] = dex
        return await self.info(payload, 20, priority)

    async def l2_book(self, coin: str, priority: int = PRIORITY_CANDIDATE) -> Any:
        return await self.info({"type": "l2Book", "coin": coin}, 2, priority)

    async def user_fills_by_time(
        self,
        address: str,
        start_ms: int,
        end_ms: int | None = None,
        priority: int = 3,
    ) -> Any:
        payload: dict[str, Any] = {
            "type": "userFillsByTime",
            "user": address,
            "startTime": start_ms,
            "aggregateByTime": False,
        }
        if end_ms is not None:
            payload["endTime"] = end_ms
        # Docs: weight 20 plus 1 per 20 returned items. The page size is unknown
        # until the response arrives, so the extra is recorded immediately after.
        result = await self.info(payload, user_fills_by_time_weight(0)[0], priority)
        extra = user_fills_by_time_weight(len(result) if isinstance(result, list) else 0)[1]
        if extra:
            self.limiter.penalize(extra)
        return result

    async def portfolio(self, address: str, priority: int = 3) -> Any:
        return await self.info({"type": "portfolio", "user": address}, 20, priority)

    async def clearinghouse_state(self, address: str, dex: str = "", priority: int = 2) -> Any:
        payload: dict[str, Any] = {"type": "clearinghouseState", "user": address}
        if dex:
            payload["dex"] = dex
        return await self.info(payload, 2, priority)

    async def exchange_status(self, priority: int = PRIORITY_LIVE) -> Any:
        return await self.info({"type": "exchangeStatus"}, 2, priority)


def default_market_id(coin: str) -> str:
    if ":" in coin:
        dex = coin.split(":", 1)[0]
        return market_id(dex, coin)
    return market_id("", coin)


def book_to_json(book: Book) -> str:
    return dumps(
        {
            "coin": book.coin,
            "time_ms": book.time_ms,
            "bids": [{"px": format(level.px, "f"), "sz": format(level.sz, "f"), "n": level.n} for level in book.bids],
            "asks": [{"px": format(level.px, "f"), "sz": format(level.sz, "f"), "n": level.n} for level in book.asks],
        }
    )
