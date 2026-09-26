"""24/7 orchestration. Trading fails closed; collection keeps retrying."""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from touchgrass_hl.audit import append_audit
from touchgrass_hl.baseline_queue import plan_baseline_batch
from touchgrass_hl.cluster_detector import ClusterConfig, ClusterDetector
from touchgrass_hl.collector import TradeCollector
from touchgrass_hl.config import Settings, score_weights
from touchgrass_hl.db.repo import (
    add_open_position,
    baseline_priority,
    behavior_events,
    count_rows,
    current_group_map,
    ensure_paper_accounts,
    finish_hydration,
    get_checkpoint,
    insert_action,
    insert_candidate,
    insert_context,
    insert_jev,
    insert_lane,
    insert_risk,
    known_copyability_observations,
    lane_exists,
    latest_scores,
    load_account,
    load_wallet_fills,
    mark_position_closed,
    monitored_addresses,
    prune,
    recent_same_signal,
    replace_current_groups,
    replace_episodes,
    research_summary,
    reset_running_jobs,
    save_account,
    save_score,
    set_checkpoint,
    store_wallet_fills,
    upsert_tracked,
    verified_addresses,
)
from touchgrass_hl.db.schema import MarketRow, WalletFillRow, WalletRow
from touchgrass_hl.db.session import (
    init_db,
    integrity_ok,
    make_engine,
    session_factory,
    session_scope,
)
from touchgrass_hl.hyperliquid_client import HyperliquidREST, book_to_json, parse_book_payload
from touchgrass_hl.independence import BehaviorEvent, IndependenceConfig, build_groups
from touchgrass_hl.jev import QUESTIONS, JevClient, jev_policy_vetoes
from touchgrass_hl.logging_setup import log, setup_logging
from touchgrass_hl.market_context import compact_jev_state, enrich_packet, market_dict
from touchgrass_hl.market_registry import MarketRegistry
from touchgrass_hl.models import AccountView, Book
from touchgrass_hl.paper import (
    LANES,
    close_from_book,
    decide_lane_acceptance,
    exit_reason,
    marked_account,
)
from touchgrass_hl.position_tracker import OPEN_ADD, reconstruct
from touchgrass_hl.rate_limit import PRIORITY_CANDIDATE, RateLimiter
from touchgrass_hl.risk import RiskConfig, evaluate_risk
from touchgrass_hl.util import D, canon_hash, dumps, maybe_d, utc_now_ms
from touchgrass_hl.wallet_history import (
    download_fills,
    episode_copyability,
    refresh_baseline,
    row_to_fill,
)
from touchgrass_hl.wallet_scoring import compute_metrics, metrics_to_dict, score_population
from touchgrass_hl.websocket_manager import SubscriptionManager, WebsocketManager

logger = logging.getLogger("touchgrass.service")


class ResearchService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = make_engine(settings.database_url)
        init_db(self.engine)
        ok, detail = integrity_ok(self.engine)
        if not ok:
            raise RuntimeError(f"database integrity check failed: {detail}")
        log(logger, logging.INFO, "database_ready", detail=detail)
        self.factory = session_factory(self.engine)
        self.db_lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.limiter = RateLimiter(settings.info_weight_per_minute, settings.info_window_seconds)
        self.client = HyperliquidREST(settings.hl_info_url, self.limiter, settings.http_timeout_s)
        self.registry = MarketRegistry(self.client)
        self.cluster = ClusterDetector(
            ClusterConfig(
                window_ms=settings.cluster_window_seconds * 1000,
                min_groups=settings.cluster_min_groups,
            )
        )
        self.collector = TradeCollector(
            self.registry,
            self.cluster,
            min_trades=settings.min_trades_to_hydrate,
            min_notional=settings.min_notional_to_hydrate,
        )
        self.subs = SubscriptionManager(settings.max_trade_subscriptions)
        self.ws = WebsocketManager(settings.hl_ws_url, self.subs, self.collector.on_message)
        self.jev = JevClient(
            enabled=settings.jev_enabled,
            api_key=settings.jev_api_key,
            model=settings.jev_model,
            base_url=settings.jev_base_url,
            timeout_s=settings.jev_timeout_s,
        )
        self.verified: set[str] = set()
        self.monitored: set[str] = set()
        self.groups: dict[str, str] = {}
        self.scores: dict[str, Decimal] = {}
        self.hydrations_this_hour = 0
        self.hour_bucket = utc_now_ms() // 3_600_000
        self.candidates_seen = 0
        self._boot()

    def _boot(self) -> None:
        with self.db_lock:
            with session_scope(self.factory) as session:
                reset_running_jobs(session)
                ensure_paper_accounts(session, self.settings.paper_starting_equity)
                self.verified = verified_addresses(session)
                self.monitored = monitored_addresses(session)
                self.groups = current_group_map(session)
                self.scores = latest_scores(session)
                restored = self.registry.load_cached(session)
                coins = self.registry.ranked_coins(
                    session,
                    self.settings.allow_coins(),
                    self.settings.deny_coins(),
                )
                chosen = self.subs.set_universe(coins)
                set_checkpoint(
                    session,
                    "process_start",
                    {"ms": utc_now_ms(), "verified": len(self.verified), "markets": restored, "trade_subs": len(chosen)},
                )
        log(
            logger,
            logging.INFO,
            "startup",
            execution_mode=self.settings.execution_mode,
            jev_enabled=self.settings.jev_enabled,
            verified=len(self.verified),
            restored_markets=self.registry.count,
            trade_subs=len(self.subs.desired),
        )

    def kill_switch_active(self) -> bool:
        if self.settings.trading_kill_switch:
            return True
        return Path("data/KILL_SWITCH").exists()

    async def market_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                payload = await self.registry.fetch()
                with self.db_lock:
                    with session_scope(self.factory) as session:
                        self.registry.apply(session, payload)
                        coins = self.registry.ranked_coins(
                            session,
                            self.settings.allow_coins(),
                            self.settings.deny_coins(),
                        )
                        chosen = self.subs.set_universe(coins)
                        set_checkpoint(
                            session,
                            "markets",
                            {"count": self.registry.count, "subscribed": len(chosen), "ms": utc_now_ms()},
                        )
                log(logger, logging.INFO, "subscriptions_updated", trade_subs=len(chosen), markets=self.registry.count)
            except Exception as exc:
                log(logger, logging.WARNING, "market_refresh_failed", error=str(exc)[:300])
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.market_refresh_seconds)
            except asyncio.TimeoutError:
                continue

    async def flush_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
                break
            except asyncio.TimeoutError:
                timed_out = True
            else:
                timed_out = False
            if not timed_out:
                break
            with self.state_lock:
                verified = set(self.verified)
                monitored = set(self.monitored)
                groups = dict(self.groups)
                scores = dict(self.scores)
            if not self.collector.buffer and not self.collector.ctx_updates:
                continue
            try:
                with self.db_lock:
                    with session_scope(self.factory) as session:
                        candidates = self.collector.flush(
                            session,
                            verified=verified,
                            groups=groups,
                            scores=scores,
                            monitored=monitored,
                        )
            except Exception:
                logger.exception("flush_failed")
                continue
            for candidate in candidates:
                self.candidates_seen += 1
                asyncio.create_task(self._safe_candidate(candidate))

    async def _safe_candidate(self, structural: dict[str, Any]) -> None:
        try:
            await self.handle_candidate(structural)
        except Exception:
            logger.exception("candidate_failed", extra={"extra_fields": {"candidate_id": structural.get("candidate_id")}})

    async def handle_candidate(self, structural: dict[str, Any]) -> None:
        market_id = structural["market_id"]
        with self.db_lock:
            with session_scope(self.factory) as session:
                row = session.get(MarketRow, market_id)
                market = market_dict(row) if row is not None else {"coin": structural.get("coin"), "dex": structural.get("dex"), "status": "unknown", "updated_ms": None}
                coin = (row.coin if row is not None else structural.get("coin")) or ""
        book = None
        if coin:
            try:
                raw_book = await self.client.l2_book(coin, priority=PRIORITY_CANDIDATE)
                book = parse_book_payload(raw_book if isinstance(raw_book, dict) else {})
            except Exception as exc:
                log(logger, logging.WARNING, "l2_failed", coin=coin, error=type(exc).__name__)
        packet = enrich_packet(
            structural,
            market=market,
            book=book,
            target_notional=self.settings.paper_target_notional,
            ws_synced=self.ws.synced,
        )
        await self._decide(packet, book)

    async def _decide(self, packet: dict[str, Any], book: Book | None) -> None:
        settings = self.settings
        dex = str(packet.get("dex") or "core")
        fee_rate, fee_assumption, fee_inputs = settings.fee_for(
            dex=dex,
            growth_mode=packet.get("growth_mode"),
            role="taker",
            deployer_fee_scale=packet.get("deployer_fee_scale"),
            aligned_quote=False,
        )
        packet["fee_snapshot"] = fee_inputs
        packet["fee_known"] = fee_rate is not None
        if fee_rate is None:
            fee_rate = Decimal(0)
        clone = dict(packet)
        clone.pop("packet_hash", None)
        packet["packet_hash"] = canon_hash(clone)
        neutral = AccountView(
            open_positions=0,
            open_markets={},
            recent_same_direction=False,
            daily_realized_pnl=Decimal(0),
            equity=Decimal("1000000"),
            peak_equity=Decimal("1000000"),
            free_cash=Decimal("1000000"),
            kill_switch=False,
        )
        base_cfg = self._risk_cfg(fee_rate, fee_assumption, int(packet.get("sz_decimals") or 0))
        now = utc_now_ms()
        mark = maybe_d(packet.get("mark_px"))
        context_ms = packet.get("context_time_ms")
        market_risk = evaluate_risk(
            packet=packet,
            book=book,
            account=neutral,
            cfg=base_cfg,
            now_ms=now,
            ws_synced=bool(self.ws.synced and (self.ws.last_message_ms == 0 or now - self.ws.last_message_ms <= settings.ws_stale_seconds * 1000)),
            market_status=packet.get("market_status"),
            context_time_ms=int(context_ms) if context_ms else None,
            mark_px=mark,
            participating_verified=True,
        )
        jev_result = None
        state = compact_jev_state(packet)
        state_hash = canon_hash(state)
        if market_risk.allowed:
            jev_result = await self.jev.evaluate(state)
        with self.db_lock:
            with session_scope(self.factory) as session:
                if not insert_candidate(session, packet, packet.get("packet_hash") or state_hash):
                    return
                insert_context(session, packet["candidate_id"], packet["market_id"], packet)
                if jev_result is not None or not market_risk.allowed:
                    if jev_result is None:
                        from touchgrass_hl.models import JevResult

                        jev_result = JevResult(
                            status="not_requested",
                            model_requested=settings.jev_model,
                            model_returned=None,
                            answers={},
                            usage={},
                            error="skipped_market_veto",
                        )
                    insert_jev(session, packet["candidate_id"], state, state_hash, QUESTIONS, jev_result)
                for lane in LANES:
                    self._decide_lane(
                        session,
                        packet,
                        book,
                        lane,
                        fee_rate,
                        fee_assumption,
                        jev_result,
                        market_risk_allowed=market_risk.allowed,
                        now=now,
                    )
                append_audit(
                    session,
                    kind="CANDIDATE_DECISION",
                    candidate_id=packet["candidate_id"],
                    payload={
                        "packet_hash": packet.get("packet_hash"),
                        "market_vetoes": market_risk.vetoes,
                        "jev_status": None if jev_result is None else jev_result.status,
                        "independent_groups": packet.get("independent_group_count"),
                    },
                )
        log(
            logger,
            logging.INFO,
            "candidate",
            candidate_id=packet["candidate_id"],
            market=packet.get("market_id"),
            direction=packet.get("direction"),
            groups=packet.get("independent_group_count"),
            market_vetoes=market_risk.vetoes,
            jev_status=None if jev_result is None else jev_result.status,
        )

    def _risk_cfg(self, fee_rate: Decimal, fee_assumption: str, sz_decimals: int) -> RiskConfig:
        s = self.settings
        return RiskConfig(
            max_spread_bps=s.max_spread_bps,
            max_slippage_bps=s.max_slippage_bps,
            max_price_move_bps=s.max_price_move_since_first_bps,
            stale_market_seconds=s.stale_market_seconds,
            min_groups=s.cluster_min_groups,
            max_positions=s.paper_max_positions,
            target_notional=s.paper_target_notional,
            daily_loss_limit_usd=s.daily_loss_limit_usd,
            max_drawdown_pct=s.max_drawdown_pct,
            sz_decimals=sz_decimals,
            fee_rate=fee_rate,
            fee_assumption=fee_assumption,
            cooldown_seconds=s.signal_cooldown_seconds,
            supported_collateral=s.supported_collateral(),
        )

    def _decide_lane(
        self,
        session: Session,
        packet: dict[str, Any],
        book: Book | None,
        lane: str,
        fee_rate: Decimal,
        fee_assumption: str,
        jev_result,
        *,
        market_risk_allowed: bool,
        now: int,
    ) -> None:
        if lane_exists(session, packet["candidate_id"], lane):
            return
        account = load_account(session, lane)
        since = now - self.settings.signal_cooldown_seconds * 1000
        recent = recent_same_signal(session, packet["market_id"], packet["direction"], since, lane)
        opens = account.open_positions()
        marks: dict[str, Decimal] = {}
        marks_ok = True
        stale_ms = self.settings.stale_market_seconds * 1000
        for pos in opens:
            market = session.get(MarketRow, pos.market_id)
            fresh = (
                market is not None
                and market.mark_px not in (None, "")
                and now - int(market.updated_ms or 0) <= stale_ms
            )
            if not fresh:
                marks_ok = False
                continue
            marks[pos.market_id] = D(str(market.mark_px))
        equity, peak, free_cash = marked_account(account, marks, marks_ok)
        if marks_ok:
            save_account(session, account)
        view = AccountView(
            open_positions=len(opens),
            open_markets={p.market_id: p.direction for p in opens},
            recent_same_direction=recent,
            daily_realized_pnl=account.day_realized,
            equity=equity,
            peak_equity=peak,
            free_cash=free_cash,
            kill_switch=self.kill_switch_active(),
            marks_ok=marks_ok,
        )
        cfg = self._risk_cfg(fee_rate, fee_assumption, int(packet.get("sz_decimals") or 0))
        ws_ok = self.ws.synced and (
            self.ws.last_message_ms == 0 or now - self.ws.last_message_ms <= self.settings.ws_stale_seconds * 1000
        )
        risk = evaluate_risk(
            packet=packet,
            book=book,
            account=view,
            cfg=cfg,
            now_ms=now,
            ws_synced=bool(ws_ok),
            market_status=packet.get("market_status"),
            context_time_ms=int(packet["context_time_ms"]) if packet.get("context_time_ms") else None,
            mark_px=maybe_d(packet.get("mark_px")),
            participating_verified=True,
        )
        insert_risk(
            session,
            packet["candidate_id"],
            lane,
            risk.allowed,
            risk.vetoes,
            {"slippage_bps": None if risk.slippage_bps is None else format(risk.slippage_bps, "f"), **risk.details},
        )
        vetoes = list(risk.vetoes)
        if lane == "RULES_PLUS_JEV":
            if not market_risk_allowed:
                vetoes.append("JEV_NOT_REQUESTED")
            else:
                vetoes.extend(
                    jev_policy_vetoes(
                        jev_result,
                        min_cluster_quality=float(self.settings.jev_min_cluster_quality),
                        min_behavior_fit=float(self.settings.jev_min_behavior_fit),
                        max_contradiction=float(self.settings.jev_max_contradiction),
                        min_information_sufficient=float(self.settings.jev_min_information_sufficient),
                    )
                )
        accepted = decide_lane_acceptance(risk.allowed, lane, [v for v in vetoes if v.startswith("JEV_")])
        reason = "ACCEPTED" if accepted else (vetoes[0] if vetoes else "REJECTED")
        if accepted and risk.vwap is not None and risk.size is not None and risk.notional is not None and book is not None:
            from touchgrass_hl.paper import PaperPosition

            fee = risk.notional * fee_rate
            pos = PaperPosition(
                lane=lane,
                candidate_id=packet["candidate_id"],
                market_id=packet["market_id"],
                coin=str(packet.get("coin") or packet.get("market") or ""),
                direction=packet["direction"],
                size=risk.size,
                entry_px=risk.vwap,
                entry_fee=fee,
                entry_fee_assumption=fee_assumption,
                entry_notional=risk.notional,
                funding_rate=maybe_d(packet.get("funding")) or Decimal(0),
                opened_ms=now,
                entry_slippage_bps=risk.slippage_bps,
                fee_inputs=packet.get("fee_snapshot"),
            )
            # Lock notional inside apply by mutating a loaded account.
            if account.cash < pos.entry_notional + pos.entry_fee:
                accepted = False
                vetoes.append("VETO_INSUFFICIENT_PAPER_EQUITY")
                reason = "VETO_INSUFFICIENT_PAPER_EQUITY"
            else:
                account.cash -= pos.entry_notional + pos.entry_fee
                account.fees += pos.entry_fee
                account.positions = (account.positions or []) + [pos]
                save_account(session, account)
                add_open_position(session, pos, book_to_json(book))
                append_audit(
                    session,
                    kind="PAPER_ENTRY",
                    candidate_id=packet["candidate_id"],
                    lane=lane,
                    payload={
                        "price": format(pos.entry_px, "f"),
                        "size": format(pos.size, "f"),
                        "fee": format(pos.entry_fee, "f"),
                        "fee_assumption": fee_assumption,
                        "notional": format(pos.entry_notional, "f"),
                    },
                )
                log(logger, logging.INFO, "paper_entry", lane=lane, candidate_id=packet["candidate_id"], market=pos.market_id)
        insert_lane(session, packet["candidate_id"], lane, accepted, vetoes, reason)
        if not accepted:
            log(logger, logging.INFO, "lane_veto", lane=lane, candidate_id=packet["candidate_id"], vetoes=vetoes)

    async def hydrate_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            bucket = utc_now_ms() // 3_600_000
            if bucket != self.hour_bucket:
                self.hour_bucket = bucket
                self.hydrations_this_hour = 0
            if self.hydrations_this_hour >= self.settings.hydration_max_per_hour:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=30)
                    break
                except asyncio.TimeoutError:
                    continue
            address = None
            with self.db_lock:
                with session_scope(self.factory) as session:
                    from touchgrass_hl.db.repo import claim_hydration

                    address = claim_hydration(session, utc_now_ms())
            if not address:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                    break
                except asyncio.TimeoutError:
                    continue
            self.hydrations_this_hour += 1
            try:
                result = await download_fills(
                    self.client,
                    address,
                    self.registry.market_id_for_coin,
                    self.settings.hydration_lookback_days,
                )
                with self.db_lock:
                    with session_scope(self.factory) as session:
                        store_wallet_fills(session, address, result["fills"])
                        self._rebuild_wallet(session, address, result["label"], result["window_complete"])
                        finish_hydration(
                            session,
                            address,
                            ok=True,
                            error="",
                            label=result["label"],
                            limited=True,
                        )
                        if result.get("portfolio") is not None:
                            append_audit(
                                session,
                                kind="WALLET_PORTFOLIO",
                                payload={"address": address, "portfolio": result["portfolio"], "history": result["label"]},
                            )
                log(logger, logging.INFO, "hydration_done", address=address, fills=len(result["fills"]), label=result["label"])
            except Exception as exc:
                text = str(exc)
                log(logger, logging.WARNING, "hydration_failed", address=address, error=text[:300])
                if "429" in text:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=60)
                        break
                    except asyncio.TimeoutError:
                        log(logger, logging.INFO, "hydration_backoff_elapsed", address=address)
                with self.db_lock:
                    with session_scope(self.factory) as session:
                        finish_hydration(
                            session,
                            address,
                            ok=False,
                            error=str(exc),
                            label="hydration_error_history_unknown",
                            limited=True,
                        )

    def _rebuild_wallet(self, session: Session, address: str, label: str, window_complete: bool) -> None:
        rows = load_wallet_fills(session, address)
        records = [row_to_fill(row) for row in rows]
        episodes, actions, _unmatched = reconstruct(records)
        replace_episodes(
            session,
            address,
            [
                {
                    "market_id": ep.market_id,
                    "coin": ep.coin,
                    "direction": ep.direction,
                    "entry_time_ms": ep.entry_time_ms,
                    "exit_time_ms": ep.exit_time_ms,
                    "entry_price": format(ep.entry_price, "f"),
                    "exit_price": None if ep.exit_price is None else format(ep.exit_price, "f"),
                    "entry_size": format(ep.entry_size, "f"),
                    "closed_pnl": format(ep.closed_pnl, "f"),
                    "fees": format(ep.fees, "f"),
                    "entry_notional": format(ep.entry_notional, "f"),
                    "closed": ep.closed,
                }
                for ep in episodes
            ],
        )
        for action in actions:
            if action.action not in OPEN_ADD and not action.action.startswith("CLOSE") and "FLIP" not in action.action:
                continue
            insert_action(
                session,
                address=address,
                market_id=action.market_id,
                coin=action.coin,
                action=action.action,
                direction=action.direction,
                time_ms=action.time_ms,
                price=format(action.price, "f"),
                size=format(action.size, "f"),
                prev_position=None if action.prev_position is None else format(action.prev_position, "f"),
                new_position=None if action.new_position is None else format(action.new_position, "f"),
                tid=action.tid or f"hist:{action.time_ms}",
                source="history",
            )

    async def score_loop(self, stop: asyncio.Event) -> None:
        # First pass is soon so a long-running process is not idle, but hydration
        # still has to meet the activity threshold before there is anything to score.
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except asyncio.TimeoutError:
            timed_out = True
        else:
            return
        if not timed_out:
            return
        while not stop.is_set():
            try:
                await self.score_once()
            except Exception:
                logger.exception("scoring_failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.scoring_interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def score_once(self) -> None:
        with self.db_lock:
            with session_scope(self.factory) as session:
                addresses = list(
                    session.scalars(
                        select(WalletFillRow.address).distinct()
                    ).all()
                )
        built: list[tuple[str, Any]] = []
        metrics_json: dict[str, str] = {}
        for address in addresses:
            with self.db_lock:
                with session_scope(self.factory) as session:
                    rows = load_wallet_fills(session, address)
                    wallet = session.get(WalletRow, address)
                    label = wallet.completeness_label if wallet is not None else "partial_hyperliquid_recent_history"
                    window_complete = label.startswith("requested_window_returned")
                    records = [row_to_fill(row) for row in rows]
                    episodes, _actions, unmatched = reconstruct(records)
                    copy_value = episode_copyability(
                        session,
                        address,
                        [ep for ep in episodes if ep.closed][:50],
                        self.settings.copy_delay_list(),
                    )
                    observations = known_copyability_observations(session, address)
                    metrics = compute_metrics(
                        episodes,
                        records,
                        copyability=copy_value,
                        completeness_label=label,
                        requested_window_fully_returned=window_complete,
                        copyability_observations=observations,
                        unmatched_closes=unmatched,
                    )
                    built.append((address, metrics))
                    metrics_json[address] = dumps(metrics_to_dict(metrics))
        if not built:
            return
        scored = score_population(
            built,
            score_weights(self.settings),
            min_closed_trades=self.settings.min_closed_trades,
            min_active_days=self.settings.min_active_days,
            max_profit_concentration=self.settings.max_profit_concentration,
            allow_concentration_override=self.settings.allow_profit_concentration_override,
            min_copyability_observations=self.settings.min_copyability_observations,
        )
        metrics_by = dict(built)
        now = utc_now_ms()
        with self.db_lock:
            with session_scope(self.factory) as session:
                for address, breakdown in scored.items():
                    stage = breakdown.verification_stage
                    save_score(
                        session,
                        address,
                        metrics_json[address],
                        format(breakdown.score, "f"),
                        breakdown.copy_verified,
                        dumps(
                            {
                                "score": format(breakdown.score, "f"),
                                "components": breakdown.components,
                                "omitted": breakdown.omitted,
                                "verified": breakdown.verified,
                                "performance_verified": breakdown.performance_verified,
                                "copy_verified": breakdown.copy_verified,
                                "verification_stage": stage,
                                "verification_reasons": breakdown.verification_reasons,
                                "copy_reasons": breakdown.copy_reasons,
                            }
                        ),
                        now,
                        performance_verified=breakdown.performance_verified,
                        verification_stage=stage,
                        copy_observations=metrics_by[address].copyability_observations,
                    )
                since = now - self.settings.independence_window_days * 86_400_000
                events = []
                for row in behavior_events(session, since):
                    if row.direction not in {"LONG", "SHORT"}:
                        continue
                    events.append(
                        BehaviorEvent(
                            wallet=row.address,
                            market_id=row.market_id,
                            direction=row.direction,
                            time_ms=row.time_ms,
                            size=D(row.size),
                        )
                    )
                grouped = build_groups(
                    events,
                    IndependenceConfig(
                        jaccard_min=self.settings.independence_jaccard_min,
                        min_simultaneous=self.settings.independence_min_simultaneous,
                        proximity_ms=self.settings.independence_proximity_ms,
                        min_events=self.settings.independence_min_events,
                        max_wallets=self.settings.independence_max_wallets,
                    ),
                )
                replace_current_groups(session, grouped["groups"], now)
                verified = verified_addresses(session)
                monitored = monitored_addresses(session)
                groups = current_group_map(session)
                scores = latest_scores(session)
                set_checkpoint(
                    session,
                    "scoring",
                    {
                        "ms": now,
                        "wallets": len(scored),
                        "performance_verified": sum(
                            1 for item in scored.values() if item.performance_verified
                        ),
                        "copy_verified": len(verified),
                    },
                )
        with self.state_lock:
            self.verified = verified
            self.monitored = monitored
            self.groups = groups
            self.scores = scores
        log(
            logger,
            logging.INFO,
            "scoring_complete",
            wallets=len(scored),
            performance=sum(1 for item in scored.values() if item.performance_verified),
            copy_verified=len(verified),
        )
        await self._refresh_baselines_rotating()

    async def _refresh_baselines_rotating(self) -> None:
        with self.db_lock:
            with session_scope(self.factory) as session:
                addresses = sorted(verified_addresses(session))
                cursor_row = get_checkpoint(session, "baseline_cursor") or {}
                cursor = int(cursor_row.get("cursor") or 0)
                priority = baseline_priority(session, addresses, utc_now_ms())
                batch, new_cursor = plan_baseline_batch(
                    addresses,
                    cursor=cursor,
                    batch=self.settings.verified_baseline_batch,
                    priority=priority,
                )
                set_checkpoint(
                    session,
                    "baseline_cursor",
                    {"cursor": new_cursor, "last_batch": batch, "ms": utc_now_ms()},
                )
        await self._refresh_baselines(batch)

    async def _refresh_baselines(self, addresses: list[str]) -> None:
        dexes = list(self.registry.order.keys()) or [""]
        for address in addresses:
            try:
                positions = await refresh_baseline(self.client, address, dexes)
            except Exception as exc:
                log(logger, logging.WARNING, "baseline_failed", address=address, error=type(exc).__name__)
                continue
            with self.db_lock:
                with session_scope(self.factory) as session:
                    seen_dex = set()
                    for pos in positions:
                        dex = pos.get("dex") or ""
                        seen_dex.add(dex)
                        if pos.get("empty"):
                            continue
                        coin = pos["coin"]
                        mid = self.registry.market_id_for_coin(coin)
                        upsert_tracked(session, address, mid, coin, pos["szi"], True, int(pos["time_ms"]))
                    for dex in seen_dex:
                        when = utc_now_ms()
                        upsert_tracked(
                            session,
                            address,
                            f"dexbaseline:{dex or 'core'}",
                            dex or "core",
                            "0",
                            True,
                            when,
                        )

    async def paper_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._monitor_paper()
            except Exception:
                logger.exception("paper_monitor_failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.paper_monitor_seconds)
            except asyncio.TimeoutError:
                continue

    async def _monitor_paper(self) -> None:
        with self.db_lock:
            with session_scope(self.factory) as session:
                open_rows = []
                for lane in LANES:
                    account = load_account(session, lane)
                    opens = account.open_positions()
                    marks: dict[str, Decimal] = {}
                    marks_ok = True
                    stale_ms = self.settings.stale_market_seconds * 1000
                    now = utc_now_ms()
                    for pos in opens:
                        market = session.get(MarketRow, pos.market_id)
                        fresh = (
                            market is not None
                            and market.mark_px not in (None, "")
                            and now - int(market.updated_ms or 0) <= stale_ms
                        )
                        if not fresh:
                            marks_ok = False
                        elif market is not None and market.mark_px is not None:
                            marks[pos.market_id] = D(str(market.mark_px))
                        if market is None:
                            continue
                        open_rows.append(
                            {
                                "lane": lane,
                                "pos": pos,
                                "mark": market.mark_px,
                                "updated_ms": market.updated_ms,
                                "dex": market.dex,
                                "growth_mode": market.growth_mode,
                                "deployer_fee_scale": market.deployer_fee_scale,
                            }
                        )
                    if opens and marks_ok:
                        marked_account(account, marks, True)
                        save_account(session, account)
        for item in open_rows:
            pos = item["pos"]
            if item["mark"] is None:
                continue
            age = utc_now_ms() - int(item["updated_ms"] or 0)
            if age > self.settings.stale_exit_seconds * 1000:
                continue
            mark = D(item["mark"])
            reason = exit_reason(
                pos,
                mark,
                utc_now_ms(),
                stop_pct=self.settings.paper_stop_loss_pct,
                take_pct=self.settings.paper_take_profit_pct,
                max_hold_s=self.settings.paper_max_hold_seconds,
            )
            if reason is None:
                continue
            try:
                raw = await self.client.l2_book(pos.coin, priority=PRIORITY_CANDIDATE)
                book = parse_book_payload(raw if isinstance(raw, dict) else {})
            except Exception:
                book = None
            if book is None:
                log(logger, logging.INFO, "exit_deferred_missing_book", lane=item["lane"], market=pos.market_id, reason=reason)
                continue
            fee_rate, fee_assumption, fee_inputs = self.settings.fee_for(
                dex=item["dex"],
                growth_mode=item["growth_mode"],
                role="taker",
                deployer_fee_scale=item.get("deployer_fee_scale"),
                aligned_quote=False,
            )
            if fee_rate is None:
                log(
                    logger,
                    logging.INFO,
                    "exit_deferred_unknown_fee",
                    lane=item["lane"],
                    market=pos.market_id,
                )
                continue
            with self.db_lock:
                with session_scope(self.factory) as session:
                    account = load_account(session, item["lane"])
                    current = next((p for p in account.open_positions() if p.candidate_id == pos.candidate_id), None)
                    if current is None:
                        continue
                    closed = close_from_book(
                        account,
                        current,
                        book,
                        reason=reason,
                        now_ms=utc_now_ms(),
                        fee_rate=fee_rate,
                        fee_assumption=fee_assumption,
                        fee_inputs=fee_inputs,
                    )
                    if closed is None:
                        log(logger, logging.INFO, "exit_deferred_book_walk", lane=item["lane"], market=pos.market_id)
                        continue
                    save_account(session, account)
                    mark_position_closed(session, closed, book_to_json(book))
                    append_audit(
                        session,
                        kind="PAPER_EXIT",
                        candidate_id=closed.candidate_id,
                        lane=item["lane"],
                        payload={
                            "reason": reason,
                            "exit_px": None if closed.exit_px is None else format(closed.exit_px, "f"),
                            "entry_fee": format(closed.entry_fee, "f"),
                            "exit_fee": None if closed.exit_fee is None else format(closed.exit_fee, "f"),
                            "funding_pnl": None if closed.funding_pnl is None else format(closed.funding_pnl, "f"),
                            "realized_pnl": None if closed.realized_pnl is None else format(closed.realized_pnl, "f"),
                            "fee_assumption": fee_assumption,
                        },
                    )
            log(logger, logging.INFO, "paper_exit", lane=item["lane"], market=pos.market_id, reason=reason)

    async def retention_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                now = utc_now_ms()
                with self.db_lock:
                    with session_scope(self.factory) as session:
                        result = prune(
                            session,
                            trade_cutoff_ms=now - self.settings.trade_retention_days * 86_400_000,
                            snapshot_cutoff_ms=now - self.settings.snapshot_retention_hours * 3_600_000,
                        )
                if result["trades"] or result["snapshots"]:
                    log(logger, logging.INFO, "retention", **result)
            except Exception:
                logger.exception("retention_failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.retention_interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def status_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self._log_status()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.status_interval_seconds)
            except asyncio.TimeoutError:
                continue

    def _log_status(self) -> None:
        try:
            with self.db_lock:
                with session_scope(self.factory) as session:
                    wallets = count_rows(session, WalletRow)
                    trades = count_rows(session, self._trade_model())
        except Exception:
            wallets = -1
            trades = -1
        log(
            logger,
            logging.INFO,
            "status",
            ws_synced=self.ws.synced,
            ws_connects=self.ws.connects,
            ws_messages=self.ws.messages,
            subscriptions=len(self.subs.desired),
            markets=self.registry.count,
            trades_stored=self.collector.inserted,
            trades_table=trades,
            wallets=wallets,
            candidates=self.candidates_seen,
            verified=len(self.verified),
            monitored=len(self.monitored),
            rate=self.limiter.snapshot(),
        )

    def _trade_model(self):
        from touchgrass_hl.db.schema import TradeRow

        return TradeRow

    def snapshot(self) -> dict[str, Any]:
        with self.db_lock:
            with session_scope(self.factory) as session:
                summary = research_summary(session)
        summary["ws_messages"] = self.ws.messages
        summary["ws_connects"] = self.ws.connects
        summary["trades_inserted"] = self.collector.inserted
        summary["http_429"] = self.limiter.http_429
        summary["copy_verified_loaded"] = len(self.verified)
        summary["monitored_loaded"] = len(self.monitored)
        return summary


def _install_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())


async def run_service(settings: Settings, duration_s: float | None = None) -> dict[str, Any]:
    setup_logging(settings.log_dir, settings.log_level, settings.log_max_bytes, settings.log_backup_count)
    service = ResearchService(settings)
    stop = asyncio.Event()
    _install_signals(stop)

    async def _timer() -> None:
        assert duration_s is not None
        await asyncio.sleep(duration_s)
        stop.set()

    tasks = [
        asyncio.create_task(service.ws.run(stop), name="ws"),
        asyncio.create_task(service.market_loop(stop), name="markets"),
        asyncio.create_task(service.flush_loop(stop), name="flush"),
        asyncio.create_task(service.hydrate_loop(stop), name="hydrate"),
        asyncio.create_task(service.score_loop(stop), name="score"),
        asyncio.create_task(service.paper_loop(stop), name="paper"),
        asyncio.create_task(service.retention_loop(stop), name="retention"),
        asyncio.create_task(service.status_loop(stop), name="status"),
    ]
    if duration_s is not None:
        tasks.append(asyncio.create_task(_timer(), name="duration"))
    await stop.wait()
    service.ws.request_stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # Final flush.
    with service.state_lock:
        verified = set(service.verified)
        monitored = set(service.monitored)
        groups = dict(service.groups)
        scores = dict(service.scores)
    with service.db_lock:
        with session_scope(service.factory) as session:
            service.collector.flush(
                session,
                verified=verified,
                groups=groups,
                scores=scores,
                monitored=monitored,
            )
    stats = service.snapshot()
    with service.db_lock:
        with session_scope(service.factory) as session:
            set_checkpoint(session, "shutdown", stats)
    log(logger, logging.INFO, "shutdown", **{k: v for k, v in stats.items()})
    service.engine.dispose()
    return stats
