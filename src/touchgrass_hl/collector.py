"""Apply public trades to the local database and the convergence detector."""

from __future__ import annotations

import logging
from collections import deque
from decimal import Decimal

from sqlalchemy.orm import Session

from touchgrass_hl.cluster_detector import ClusterDetector
from touchgrass_hl.db.repo import insert_action, insert_trades, observe_wallets, tracked_map
from touchgrass_hl.db.schema import TrackedPositionRow
from touchgrass_hl.hyperliquid_client import parse_all_dexs_asset_ctxs, parse_trades_message
from touchgrass_hl.logging_setup import log
from touchgrass_hl.market_registry import MarketRegistry
from touchgrass_hl.models import ActionEvent, Trade
from touchgrass_hl.position_tracker import OPEN_ADD, apply_trade_to_position, classify_action
from touchgrass_hl.util import D, split_market_id

logger = logging.getLogger("touchgrass.collector")


class TradeCollector:
    def __init__(
        self,
        registry: MarketRegistry,
        cluster: ClusterDetector,
        *,
        min_trades: int,
        min_notional: Decimal,
    ) -> None:
        self.registry = registry
        self.cluster = cluster
        self.min_trades = min_trades
        self.min_notional = min_notional
        self.buffer: list[Trade] = []
        self.ctx_updates: list[tuple[str, int, dict]] = []
        self.inserted = 0
        self.seen: deque[str] = deque()
        self.seen_set: set[str] = set()
        self.discovered_addresses = 0

    def on_message(self, message: dict) -> None:
        channel = message.get("channel")
        if channel == "trades":
            trades = parse_trades_message(message, self.registry.market_id_for_coin)
            for trade in trades:
                if trade.tid in self.seen_set:
                    continue
                self._remember(trade.tid)
                self.buffer.append(trade)
        elif channel == "allDexsAssetCtxs":
            self.ctx_updates.extend(parse_all_dexs_asset_ctxs(message))

    def _remember(self, tid: str) -> None:
        self.seen.append(tid)
        self.seen_set.add(tid)
        while len(self.seen) > 100_000:
            old = self.seen.popleft()
            self.seen_set.discard(old)

    def flush(
        self,
        session: Session,
        *,
        verified: set[str],
        groups: dict[str, str],
        scores: dict[str, Decimal],
        monitored: set[str] | None = None,
    ) -> list[dict]:
        trades = self.buffer
        ctxs = self.ctx_updates
        self.buffer = []
        self.ctx_updates = []
        try:
            candidates = self._persist(
                session,
                trades,
                ctxs,
                verified=verified,
                groups=groups,
                scores=scores,
                monitored=monitored,
            )
        except Exception:
            self.buffer = trades + self.buffer
            self.ctx_updates = ctxs + self.ctx_updates
            raise
        return candidates

    def _persist(
        self,
        session: Session,
        trades: list[Trade],
        ctxs: list[tuple[str, int, dict]],
        *,
        verified: set[str],
        groups: dict[str, str],
        scores: dict[str, Decimal],
        monitored: set[str] | None = None,
    ) -> list[dict]:
        self._apply_full_ctx_snapshots(session, ctxs)
        added = insert_trades(session, trades)
        queued = observe_wallets(
            session,
            trades,
            min_trades=self.min_trades,
            min_notional=self.min_notional,
        )
        self.inserted += added
        self.discovered_addresses += len({t.buyer for t in trades} | {t.seller for t in trades})
        candidates = []
        watch = set(verified if monitored is None else monitored) | set(verified)
        if watch:
            tracked = tracked_map(session, watch)
            for trade in trades:
                candidates.extend(
                    self._apply_participants(
                        session, trade, tracked, verified, groups, scores, monitored=watch
                    )
                )
        if added or queued:
            log(
                logger,
                logging.DEBUG,
                "flush",
                inserted=added,
                buffered=len(trades),
                hydration_queued=len(queued),
                candidates=len(candidates),
            )
        return candidates

    def _apply_full_ctx_snapshots(self, session: Session, ctxs: list[tuple[str, int, dict]]) -> None:
        """Apply allDexsAssetCtxs only when every universe index is present.

        A partial update is not index-aligned to the REST universe, so using it
        would write the wrong coin's mark. REST metaAndAssetCtxs remains authoritative.
        """
        latest: dict[tuple[str, int], dict] = {}
        for dex_name, index, ctx in ctxs:
            latest[(dex_name, index)] = ctx
        by_dex: dict[str, dict[int, dict]] = {}
        for (dex_name, index), ctx in latest.items():
            by_dex.setdefault(dex_name, {})[index] = ctx
        for dex_name, mapping in by_dex.items():
            expected = self.registry.order.get(dex_name) or []
            if not expected or set(mapping) != set(range(len(expected))):
                continue
            for index, ctx in mapping.items():
                self.registry.apply_ws_ctx(session, dex_name, index, ctx)

    def _apply_participants(
        self,
        session: Session,
        trade: Trade,
        tracked,
        verified: set[str],
        groups: dict[str, str],
        scores: dict[str, Decimal],
        monitored: set[str] | None = None,
    ) -> list[dict]:
        out = []
        watch = verified if monitored is None else monitored
        dex, _coin = split_market_id(trade.market_id)
        for address, wallet_side in ((trade.buyer, "B"), (trade.seller, "A")):
            if address not in watch:
                continue
            row = tracked.get((address, trade.market_id))
            baseline = tracked.get((address, f"dexbaseline:{dex}"))
            known = bool(row and row.known and row.signed_size is not None)
            inherited = False
            if not known and baseline is not None and baseline.known and trade.time_ms >= (baseline.baseline_time_ms or 0):
                known = True
                inherited = True
                prev = Decimal(0)
            else:
                prev = D(row.signed_size) if known else None
            if known and not inherited and trade.time_ms < (row.baseline_time_ms or 0):
                continue
            if inherited and row is None:
                row = TrackedPositionRow(
                    address=address,
                    market_id=trade.market_id,
                    coin=trade.coin,
                    signed_size="0",
                    known=True,
                    baseline_time_ms=baseline.baseline_time_ms if baseline is not None else trade.time_ms,
                    updated_ms=trade.time_ms,
                )
                session.add(row)
                tracked[(address, trade.market_id)] = row
                prev = Decimal(0)
            action = classify_action(prev, wallet_side, trade.size)
            new = apply_trade_to_position(prev, wallet_side, trade.size)
            if row is not None and known and new is not None:
                row.signed_size = format(new, "f")
                row.known = True
                row.updated_ms = trade.time_ms
            inserted = insert_action(
                session,
                address=address,
                market_id=trade.market_id,
                coin=trade.coin,
                action=action,
                direction="LONG" if "LONG" in action else ("SHORT" if "SHORT" in action else "NONE"),
                time_ms=trade.time_ms,
                price=format(trade.price, "f"),
                size=format(trade.size, "f"),
                prev_position=None if prev is None else format(prev, "f"),
                new_position=None if new is None else format(new, "f"),
                tid=trade.tid,
                source="live",
            )
            if not inserted or action not in OPEN_ADD:
                continue
            event = ActionEvent(
                wallet=address,
                market_id=trade.market_id,
                coin=trade.coin,
                dex=dex,
                direction="LONG" if action.endswith("LONG") or action in {"OPEN_LONG", "ADD_LONG"} else "SHORT",
                action=action,
                time_ms=trade.time_ms,
                price=trade.price,
                size=trade.size,
                prev_position=prev,
                new_position=new,
            )
            # OPEN_LONG endswith LONG, ADD_LONG endswith LONG, OPEN_SHORT endswith SHORT.
            if action in {"OPEN_LONG", "ADD_LONG"}:
                event.direction = "LONG"
            elif action in {"OPEN_SHORT", "ADD_SHORT"}:
                event.direction = "SHORT"
            if address not in verified:
                continue
            candidate = self.cluster.on_action(event, verified=verified, groups=groups, scores=scores)
            if candidate is not None:
                out.append(candidate)
        return out
