"""Discover core and HIP-3 perp markets. No coin is hard-coded."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from touchgrass_hl.db.schema import MarketRow
from touchgrass_hl.hyperliquid_client import HyperliquidREST
from touchgrass_hl.logging_setup import log
from touchgrass_hl.util import asset_id_for, dumps, market_id, maybe_d, utc_now_ms

logger = logging.getLogger("touchgrass.markets")


def dex_entries(payload: Any) -> list[tuple[int, str, dict[str, Any] | None]]:
    if not isinstance(payload, list):
        raise ValueError("perpDexs response is not a list")
    out = []
    for index, item in enumerate(payload):
        if item is None:
            out.append((index, "", None))
        elif isinstance(item, dict) and item.get("name"):
            out.append((index, str(item["name"]), item))
        else:
            continue
    if not any(name == "" for _, name, _ in out):
        out.insert(0, (0, "", None))
    return out


def rows_from_meta(
    *,
    perp_dex_index: int,
    dex_name: str,
    payload: Any,
    dex_meta: dict[str, Any] | None,
    now_ms: int,
    exchange_halted: bool,
) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or len(payload) < 2:
        raise ValueError(f"metaAndAssetCtxs malformed for dex={dex_name!r}")
    meta, ctxs = payload[0], payload[1]
    if not isinstance(meta, dict) or not isinstance(ctxs, list):
        raise ValueError(f"metaAndAssetCtxs types unexpected for dex={dex_name!r}")
    universe = meta.get("universe") or []
    margin_tables = {int(item[0]): item[1] for item in meta.get("marginTables") or [] if isinstance(item, list) and len(item) == 2}
    rows = []
    for index, asset in enumerate(universe):
        if not isinstance(asset, dict) or "name" not in asset:
            continue
        coin = str(asset["name"])
        ctx = ctxs[index] if index < len(ctxs) and isinstance(ctxs[index], dict) else {}
        delisted = bool(asset.get("isDelisted"))
        status = "delisted" if delisted else ("halted" if exchange_halted else "active")
        table_id = asset.get("marginTableId")
        table = margin_tables.get(int(table_id)) if table_id is not None else None
        hip3 = {
            "dex_meta": dex_meta,
            "growthMode": asset.get("growthMode"),
            "lastGrowthModeChangeTime": asset.get("lastGrowthModeChangeTime"),
            "marginMode": asset.get("marginMode"),
            "onlyIsolated": asset.get("onlyIsolated"),
            "marginTable": table,
            "collateralToken": meta.get("collateralToken"),
        }
        rows.append(
            {
                "market_id": market_id(dex_name, coin),
                "dex": dex_name or "core",
                "coin": coin,
                "asset_id": asset_id_for(perp_dex_index, index),
                "sz_decimals": int(asset.get("szDecimals") or 0),
                "max_leverage": asset.get("maxLeverage"),
                "margin_table_id": int(table_id) if table_id is not None else None,
                "margin_mode": asset.get("marginMode"),
                "only_isolated": bool(asset.get("onlyIsolated")),
                "growth_mode": asset.get("growthMode"),
                "is_delisted": delisted,
                "mark_px": _s(ctx.get("markPx")),
                "oracle_px": _s(ctx.get("oraclePx")),
                "mid_px": _s(ctx.get("midPx")),
                "funding": _s(ctx.get("funding")),
                "open_interest": _s(ctx.get("openInterest")),
                "day_ntl_vlm": _s(ctx.get("dayNtlVlm")),
                "premium": _s(ctx.get("premium")),
                "prev_day_px": _s(ctx.get("prevDayPx")),
                "impact_pxs": dumps(ctx.get("impactPxs")) if ctx.get("impactPxs") is not None else None,
                "status": status,
                "hip3_json": dumps(hip3),
                "updated_ms": now_ms,
            }
        )
    return rows


def _s(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


class MarketRegistry:
    def __init__(self, client: HyperliquidREST) -> None:
        self.client = client
        self.by_coin: dict[str, str] = {}
        self.order: dict[str, list[str]] = {}
        self.count = 0
        self.halted = False

    def market_id_for_coin(self, coin: str) -> str:
        found = self.by_coin.get(coin)
        if found:
            return found
        if ":" in coin:
            return market_id(coin.split(":", 1)[0], coin)
        return market_id("", coin)

    def load_cached(self, session: Session) -> int:
        """Restore coin→market map from SQLite so trade subs can start before REST refresh."""
        rows = session.scalars(select(MarketRow)).all()
        by_coin: dict[str, str] = {}
        for row in rows:
            if not row.coin or str(row.market_id).startswith("dexbaseline:"):
                continue
            by_coin[row.coin] = row.market_id
        self.by_coin = by_coin
        self.count = len(by_coin)
        return self.count

    def ranked_coins(self, session: Session, allow: set[str], deny: set[str]) -> list[str]:
        rows = session.scalars(select(MarketRow).where(MarketRow.status == "active")).all()
        filtered = []
        for row in rows:
            if allow and row.coin not in allow:
                continue
            if row.coin in deny:
                continue
            volume = maybe_d(row.day_ntl_vlm) or Decimal(0)
            filtered.append((volume, row.coin))
        filtered.sort(key=lambda item: (-item[0], item[1]))
        return [coin for _, coin in filtered]

    async def fetch(self) -> dict[str, Any]:
        """Network-only market discovery. apply() writes the database."""
        now = utc_now_ms()
        halted = False
        try:
            status = await self.client.exchange_status()
            special = status.get("specialStatuses") if isinstance(status, dict) else None
            if special:
                halted = True
        except Exception as exc:
            log(logger, logging.WARNING, "exchange_status_failed", error=type(exc).__name__)
        dexs = dex_entries(await self.client.perp_dexs())
        collected: list[tuple[str, list[dict[str, Any]]]] = []
        for index, name, meta in dexs:
            try:
                payload = await self.client.meta_and_asset_ctxs(name)
                rows = rows_from_meta(
                    perp_dex_index=index,
                    dex_name=name,
                    payload=payload,
                    dex_meta=meta,
                    now_ms=now,
                    exchange_halted=halted,
                )
            except Exception as exc:
                log(logger, logging.WARNING, "dex_refresh_failed", dex=name or "core", error=str(exc)[:300])
                continue
            collected.append((name, rows))
        return {"halted": halted, "dexs": len(dexs), "collected": collected}

    def apply(self, session: Session, payload: dict[str, Any]) -> int:
        self.halted = bool(payload["halted"])
        written = 0
        by_coin = dict(self.by_coin)
        order = dict(self.order)
        for name, rows in payload["collected"]:
            order[name] = [row["coin"] for row in rows]
            for row in rows:
                by_coin[row["coin"]] = row["market_id"]
                existing = session.get(MarketRow, row["market_id"])
                if existing is None:
                    session.add(MarketRow(**row))
                else:
                    for key, value in row.items():
                        setattr(existing, key, value)
                written += 1
        self.by_coin = by_coin
        self.order = order
        self.count = len(by_coin)
        log(
            logger,
            logging.INFO,
            "markets_refreshed",
            markets=self.count,
            written=written,
            dexs=payload["dexs"],
            halted=self.halted,
        )
        return self.count

    def apply_ws_ctx(self, session: Session, dex_name: str, index: int, ctx: dict[str, Any]) -> None:
        coins = self.order.get(dex_name)
        if not coins or index >= len(coins):
            return
        coin = coins[index]
        mid = self.by_coin.get(coin)
        if not mid:
            return
        row = session.get(MarketRow, mid)
        if row is None:
            return
        for src, dest in (
            ("markPx", "mark_px"),
            ("oraclePx", "oracle_px"),
            ("midPx", "mid_px"),
            ("funding", "funding"),
            ("openInterest", "open_interest"),
            ("dayNtlVlm", "day_ntl_vlm"),
            ("premium", "premium"),
            ("prevDayPx", "prev_day_px"),
        ):
            if ctx.get(src) is not None:
                setattr(row, dest, str(ctx[src]))
        row.updated_ms = utc_now_ms()
