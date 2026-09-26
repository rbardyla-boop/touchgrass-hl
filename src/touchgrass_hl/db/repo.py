"""Persistence helpers. Audit rows are insert-only."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from touchgrass_hl.db.schema import (
    CandidateRow,
    CheckpointRow,
    ContextSnapshotRow,
    CopyabilityRow,
    EpisodeRow,
    GroupRow,
    HydrationJob,
    JevReviewRow,
    LaneDecisionRow,
    MarketRow,
    MetricRow,
    PaperAccountRow,
    PaperFillRow,
    PaperPositionRow,
    RiskDecisionRow,
    ScoreRow,
    TestnetOrderRow,
    TrackedPositionRow,
    TradeRow,
    WalletActionRow,
    WalletFillRow,
    WalletRow,
)
from touchgrass_hl.models import Trade
from touchgrass_hl.paper import LANES, AccountState, PaperPosition
from touchgrass_hl.util import D, dumps, loads, utc_day, utc_now_ms


def insert_trades(session: Session, trades: list[Trade]) -> int:
    if not trades:
        return 0
    tids = [trade.tid for trade in trades]
    existing = set(session.scalars(select(TradeRow.tid).where(TradeRow.tid.in_(tids))).all())
    added = 0
    for trade in trades:
        if trade.tid in existing:
            continue
        existing.add(trade.tid)
        session.add(
            TradeRow(
                tid=trade.tid,
                idempotency_key=trade.idempotency_key,
                coin=trade.coin,
                market_id=trade.market_id,
                side=trade.side,
                aggressor=trade.aggressor,
                price=format(trade.price, "f"),
                size=format(trade.size, "f"),
                notional=format(trade.notional, "f"),
                hash=trade.hash,
                time_ms=trade.time_ms,
                buyer=trade.buyer,
                seller=trade.seller,
                raw_json=dumps(trade.raw),
            )
        )
        added += 1
    return added


def observe_wallets(
    session: Session,
    trades: list[Trade],
    *,
    min_trades: int,
    min_notional: Decimal,
) -> list[str]:
    """Update cheap local observations. Returns addresses newly eligible to hydrate."""
    touched: dict[str, list[tuple[Trade, str]]] = {}
    for trade in trades:
        touched.setdefault(trade.buyer, []).append((trade, "buy"))
        touched.setdefault(trade.seller, []).append((trade, "sell"))
    if not touched:
        return []
    rows = {
        row.address: row
        for row in session.scalars(select(WalletRow).where(WalletRow.address.in_(list(touched)))).all()
    }
    queued: list[str] = []
    now = utc_now_ms()
    for address, events in touched.items():
        row = rows.get(address)
        if row is None:
            row = WalletRow(
                address=address,
                trade_count=0,
                notional="0",
                buy_notional="0",
                sell_notional="0",
                markets_json="[]",
                first_seen_ms=events[0][0].time_ms,
                last_seen_ms=0,
                trades_per_hour="0",
                hydration_status="none",
                history_limited=True,
                completeness_label="unknown",
                lifetime_complete=False,
                verified=False,
                tracked=False,
                updated_ms=now,
            )
            session.add(row)
            rows[address] = row
        markets = set(loads(row.markets_json) or [])
        notional = D(row.notional or "0")
        buy = D(row.buy_notional or "0")
        sell = D(row.sell_notional or "0")
        row.trade_count = int(row.trade_count or 0)
        row.first_seen_ms = int(row.first_seen_ms or 0)
        row.last_seen_ms = int(row.last_seen_ms or 0)
        row.hydration_status = row.hydration_status or "none"
        for trade, side in events:
            row.trade_count += 1
            notional += trade.notional
            if side == "buy":
                buy += trade.notional
            else:
                sell += trade.notional
            markets.add(trade.market_id)
            if row.first_seen_ms == 0 or trade.time_ms < row.first_seen_ms:
                row.first_seen_ms = trade.time_ms
            if trade.time_ms > row.last_seen_ms:
                row.last_seen_ms = trade.time_ms
        row.notional = format(notional, "f")
        row.buy_notional = format(buy, "f")
        row.sell_notional = format(sell, "f")
        row.markets_json = dumps(sorted(markets))
        span_h = Decimal(max(1, row.last_seen_ms - row.first_seen_ms)) / Decimal(3_600_000)
        row.trades_per_hour = format(Decimal(row.trade_count) / span_h, "f")
        row.updated_ms = now
        if (
            row.hydration_status == "none"
            and row.trade_count >= min_trades
            and notional >= min_notional
        ):
            row.hydration_status = "queued"
            session.merge(
                HydrationJob(
                    address=address,
                    status="queued",
                    attempts=0,
                    next_attempt_ms=now,
                    last_error="",
                    updated_ms=now,
                )
            )
            queued.append(address)
    return queued


def claim_hydration(session: Session, now_ms: int) -> str | None:
    job = session.scalar(
        select(HydrationJob)
        .where(HydrationJob.status == "queued", HydrationJob.next_attempt_ms <= now_ms)
        .order_by(HydrationJob.next_attempt_ms)
        .limit(1)
    )
    if job is None:
        return None
    job.status = "running"
    job.attempts += 1
    job.updated_ms = now_ms
    return job.address


def finish_hydration(session: Session, address: str, *, ok: bool, error: str, label: str, limited: bool) -> None:
    now = utc_now_ms()
    job = session.get(HydrationJob, address)
    wallet = session.get(WalletRow, address)
    if job is not None:
        if ok:
            job.status = "done"
            job.last_error = ""
        else:
            job.status = "queued"
            job.last_error = error[:500]
            job.next_attempt_ms = now + min(3_600_000, 30_000 * max(1, job.attempts))
        job.updated_ms = now
    if wallet is not None:
        wallet.hydration_status = "done" if ok else "error"
        wallet.completeness_label = label
        wallet.history_limited = limited
        wallet.lifetime_complete = False
        wallet.updated_ms = now


def reset_running_jobs(session: Session) -> int:
    result = session.execute(
        update(HydrationJob)
        .where(HydrationJob.status == "running")
        .values(status="queued", updated_ms=utc_now_ms())
    )
    return int(result.rowcount or 0)


def wallet_fill_key(time_ms: int, coin: str, tid: str) -> str:
    return f"{int(time_ms)}|{coin}|{tid}"


def store_wallet_fills(session: Session, address: str, fills: list[dict[str, Any]]) -> int:
    added = 0
    for fill in fills:
        key = wallet_fill_key(int(fill["time_ms"]), str(fill["coin"]), str(fill["tid"]))
        exists = session.scalar(
            select(WalletFillRow.id).where(
                WalletFillRow.address == address, WalletFillRow.fill_key == key
            )
        )
        if exists is not None:
            continue
        crossed = fill.get("crossed")
        session.add(
            WalletFillRow(
                address=address,
                tid=str(fill["tid"]),
                coin=fill["coin"],
                market_id=fill["market_id"],
                time_ms=int(fill["time_ms"]),
                price=str(fill["price"]),
                size=str(fill["size"]),
                side=str(fill["side"]),
                direction_raw=str(fill.get("direction_raw") or ""),
                start_position=fill.get("start_position"),
                closed_pnl=str(fill.get("closed_pnl") or "0"),
                fee=str(fill.get("fee") or "0"),
                fee_token=str(fill.get("fee_token") or ""),
                oid=str(fill.get("oid") or ""),
                hash=str(fill.get("hash") or ""),
                crossed=None if crossed is None else (1 if crossed else 0),
                raw_json=dumps(fill.get("raw") or {}),
                fill_key=key,
            )
        )
        added += 1
    return added


def load_wallet_fills(session: Session, address: str) -> list[WalletFillRow]:
    return list(
        session.scalars(
            select(WalletFillRow).where(WalletFillRow.address == address).order_by(WalletFillRow.time_ms)
        ).all()
    )


def replace_episodes(session: Session, address: str, episodes: list[dict[str, Any]]) -> None:
    session.execute(delete(EpisodeRow).where(EpisodeRow.address == address))
    for ep in episodes:
        session.add(EpisodeRow(address=address, **ep))


def save_score(
    session: Session,
    address: str,
    metrics_json: str,
    score: str,
    verified: bool,
    breakdown_json: str,
    now_ms: int,
    *,
    performance_verified: bool = False,
    verification_stage: str = "none",
    copy_observations: int = 0,
) -> None:
    """`verified` is COPY_VERIFIED. Performance-only wallets stay monitored."""
    session.add(MetricRow(address=address, computed_ms=now_ms, metrics_json=metrics_json))
    session.add(
        ScoreRow(
            address=address,
            computed_ms=now_ms,
            score=score,
            verified=verified,
            breakdown_json=breakdown_json,
        )
    )
    wallet = session.get(WalletRow, address)
    if wallet is not None:
        wallet.verified = verified
        wallet.performance_verified = performance_verified
        wallet.verification_stage = verification_stage
        wallet.copy_observations = int(copy_observations)
        wallet.tracked = bool(performance_verified or verified)
        wallet.updated_ms = now_ms


def replace_current_groups(session: Session, records: list[dict[str, Any]], now_ms: int) -> None:
    session.execute(update(GroupRow).where(GroupRow.is_current.is_(True)).values(is_current=False))
    for record in records:
        session.add(
            GroupRow(
                group_id=record["group_id"],
                computed_ms=now_ms,
                is_current=True,
                members_json=dumps(record["members"]),
                why_json=dumps(record),
            )
        )


def current_group_map(session: Session) -> dict[str, str]:
    rows = session.scalars(select(GroupRow).where(GroupRow.is_current.is_(True))).all()
    mapping: dict[str, str] = {}
    for row in rows:
        for member in loads(row.members_json) or []:
            mapping[str(member)] = row.group_id
    return mapping


def verified_addresses(session: Session) -> set[str]:
    """COPY_VERIFIED wallets. These are the only cluster participants."""
    return set(session.scalars(select(WalletRow.address).where(WalletRow.verified.is_(True))).all())


def monitored_addresses(session: Session) -> set[str]:
    """PERFORMANCE_VERIFIED and COPY_VERIFIED wallets. Still watched for evidence."""
    return set(
        session.scalars(
            select(WalletRow.address).where(WalletRow.verification_stage.in_(("performance", "copy")))
        ).all()
    )


def known_copyability_observations(session: Session, address: str) -> int:
    value = session.scalar(
        select(func.count())
        .select_from(CopyabilityRow)
        .where(CopyabilityRow.address == address, CopyabilityRow.status == "known")
    )
    return int(value or 0)


def latest_scores(session: Session) -> dict[str, Decimal]:
    # Latest score per address via max id.
    sub = (
        select(ScoreRow.address, func.max(ScoreRow.id).label("id"))
        .group_by(ScoreRow.address)
        .subquery()
    )
    rows = session.execute(select(ScoreRow.address, ScoreRow.score).join(sub, ScoreRow.id == sub.c.id)).all()
    return {address: D(score) for address, score in rows}


def upsert_tracked(
    session: Session,
    address: str,
    market_id: str,
    coin: str,
    signed_size: str | None,
    known: bool,
    baseline_time_ms: int,
) -> None:
    row = session.scalar(
        select(TrackedPositionRow).where(
            TrackedPositionRow.address == address, TrackedPositionRow.market_id == market_id
        )
    )
    now = utc_now_ms()
    if row is None:
        session.add(
            TrackedPositionRow(
                address=address,
                market_id=market_id,
                coin=coin,
                signed_size=signed_size,
                known=known,
                baseline_time_ms=baseline_time_ms,
                updated_ms=now,
            )
        )
    else:
        row.signed_size = signed_size
        row.known = known
        row.baseline_time_ms = baseline_time_ms
        row.coin = coin
        row.updated_ms = now


def tracked_map(session: Session, addresses: set[str]) -> dict[tuple[str, str], TrackedPositionRow]:
    if not addresses:
        return {}
    rows = session.scalars(select(TrackedPositionRow).where(TrackedPositionRow.address.in_(addresses))).all()
    return {(row.address, row.market_id): row for row in rows}


def insert_action(session: Session, **kwargs: Any) -> bool:
    exists = session.scalar(
        select(WalletActionRow.id).where(
            WalletActionRow.address == kwargs["address"],
            WalletActionRow.tid == kwargs.get("tid", ""),
            WalletActionRow.action == kwargs["action"],
        )
    )
    if exists is not None:
        return False
    session.add(WalletActionRow(**kwargs))
    return True


def behavior_events(session: Session, since_ms: int) -> list[WalletActionRow]:
    actions = ("OPEN_LONG", "ADD_LONG", "OPEN_SHORT", "ADD_SHORT")
    return list(
        session.scalars(
            select(WalletActionRow).where(
                WalletActionRow.time_ms >= since_ms,
                WalletActionRow.action.in_(actions),
            )
        ).all()
    )


def insert_candidate(session: Session, packet: dict[str, Any], packet_hash: str) -> bool:
    if session.get(CandidateRow, packet["candidate_id"]) is not None:
        return False
    session.add(
        CandidateRow(
            candidate_id=packet["candidate_id"],
            market_id=packet["market_id"],
            coin=packet.get("coin") or "",
            dex=packet.get("dex") or "",
            direction=packet["direction"],
            first_ms=int(packet["timestamp_first_wallet_ms"]),
            trigger_ms=int(packet["timestamp_threshold_ms"]),
            packet_json=dumps(packet),
            packet_hash=packet_hash,
        )
    )
    return True


def recent_same_signal(session: Session, market_id: str, direction: str, since_ms: int, lane: str) -> bool:
    row = session.scalar(
        select(LaneDecisionRow.id)
        .join(CandidateRow, CandidateRow.candidate_id == LaneDecisionRow.candidate_id)
        .where(
            CandidateRow.market_id == market_id,
            CandidateRow.direction == direction,
            CandidateRow.trigger_ms >= since_ms,
            LaneDecisionRow.lane == lane,
            LaneDecisionRow.accepted.is_(True),
        )
        .limit(1)
    )
    return row is not None


def ensure_paper_accounts(session: Session, starting: Decimal) -> None:
    today = utc_day(utc_now_ms())
    for lane in LANES:
        row = session.get(PaperAccountRow, lane)
        if row is None:
            session.add(
                PaperAccountRow(
                    lane=lane,
                    starting_equity=format(starting, "f"),
                    cash=format(starting, "f"),
                    peak_equity=format(starting, "f"),
                    realized_pnl="0",
                    fees="0",
                    funding="0",
                    day_utc=today,
                    day_realized="0",
                    wins=0,
                    losses=0,
                )
            )


def load_account(session: Session, lane: str) -> AccountState:
    row = session.get(PaperAccountRow, lane)
    if row is None:
        raise RuntimeError(f"paper account missing: {lane}")
    today = utc_day(utc_now_ms())
    day_realized = D(row.day_realized or "0")
    day = row.day_utc
    if day != today:
        day_realized = Decimal(0)
        day = today
    positions = []
    for pos in session.scalars(select(PaperPositionRow).where(PaperPositionRow.lane == lane)).all():
        positions.append(_pos_from_row(pos))
    return AccountState(
        lane=lane,
        starting_equity=D(row.starting_equity),
        cash=D(row.cash),
        peak_equity=D(row.peak_equity),
        realized_pnl=D(row.realized_pnl or "0"),
        fees=D(row.fees or "0"),
        funding=D(row.funding or "0"),
        day_utc=day,
        day_realized=day_realized,
        wins=row.wins,
        losses=row.losses,
        positions=positions,
    )


def save_account(session: Session, account: AccountState) -> None:
    row = session.get(PaperAccountRow, account.lane)
    if row is None:
        return
    row.cash = format(account.cash, "f")
    row.peak_equity = format(account.peak_equity, "f")
    row.realized_pnl = format(account.realized_pnl, "f")
    row.fees = format(account.fees, "f")
    row.funding = format(account.funding, "f")
    row.day_utc = account.day_utc
    row.day_realized = format(account.day_realized, "f")
    row.wins = account.wins
    row.losses = account.losses


def add_open_position(session: Session, pos: PaperPosition, book_json: str) -> int:
    row = PaperPositionRow(
        lane=pos.lane,
        candidate_id=pos.candidate_id,
        market_id=pos.market_id,
        coin=pos.coin,
        direction=pos.direction,
        size=format(pos.size, "f"),
        entry_px=format(pos.entry_px, "f"),
        entry_fee=format(pos.entry_fee, "f"),
        entry_fee_assumption=pos.entry_fee_assumption,
        entry_notional=format(pos.entry_notional, "f"),
        funding_rate=format(pos.funding_rate, "f"),
        opened_ms=pos.opened_ms,
        status="open",
        entry_slippage_bps=None if pos.entry_slippage_bps is None else format(pos.entry_slippage_bps, "f"),
    )
    session.add(row)
    session.flush()
    session.add(
        PaperFillRow(
            position_id=row.id,
            lane=pos.lane,
            candidate_id=pos.candidate_id,
            kind="entry",
            direction=pos.direction,
            price=format(pos.entry_px, "f"),
            size=format(pos.size, "f"),
            notional=format(pos.entry_notional, "f"),
            fee=format(pos.entry_fee, "f"),
            fee_assumption=pos.entry_fee_assumption,
            slippage_bps=None if pos.entry_slippage_bps is None else format(pos.entry_slippage_bps, "f"),
            time_ms=pos.opened_ms,
            book_json=book_json,
            fee_inputs_json=dumps(pos.fee_inputs or {}),
        )
    )
    return int(row.id)


def mark_position_closed(session: Session, pos: PaperPosition, book_json: str) -> None:
    row = session.scalar(
        select(PaperPositionRow).where(
            PaperPositionRow.lane == pos.lane,
            PaperPositionRow.candidate_id == pos.candidate_id,
            PaperPositionRow.status == "open",
        )
    )
    if row is None:
        return
    row.status = "closed"
    row.exit_px = None if pos.exit_px is None else format(pos.exit_px, "f")
    row.exit_fee = None if pos.exit_fee is None else format(pos.exit_fee, "f")
    row.exit_fee_assumption = pos.exit_fee_assumption
    row.exit_reason = pos.exit_reason
    row.closed_ms = pos.closed_ms
    row.realized_pnl = None if pos.realized_pnl is None else format(pos.realized_pnl, "f")
    row.funding_pnl = None if pos.funding_pnl is None else format(pos.funding_pnl, "f")
    row.exit_slippage_bps = None if pos.exit_slippage_bps is None else format(pos.exit_slippage_bps, "f")
    session.add(
        PaperFillRow(
            position_id=row.id,
            lane=pos.lane,
            candidate_id=pos.candidate_id,
            kind="exit",
            direction=pos.direction,
            price=row.exit_px or "0",
            size=row.size,
            notional=format((pos.exit_px or Decimal(0)) * pos.size, "f"),
            fee=row.exit_fee or "0",
            fee_assumption=pos.exit_fee_assumption or "",
            slippage_bps=row.exit_slippage_bps,
            time_ms=pos.closed_ms or utc_now_ms(),
            book_json=book_json,
            fee_inputs_json=dumps(pos.exit_fee_inputs or {}),
        )
    )


def lane_exists(session: Session, candidate_id: str, lane: str) -> bool:
    found = session.scalar(
        select(LaneDecisionRow.id).where(
            LaneDecisionRow.candidate_id == candidate_id, LaneDecisionRow.lane == lane
        )
    )
    return found is not None


def insert_lane(session: Session, candidate_id: str, lane: str, accepted: bool, vetoes: list[str], reason: str) -> None:
    session.add(
        LaneDecisionRow(
            candidate_id=candidate_id,
            lane=lane,
            accepted=accepted,
            vetoes_json=dumps(vetoes),
            reason=reason,
            created_ms=utc_now_ms(),
        )
    )


def insert_risk(session: Session, candidate_id: str, lane: str, allowed: bool, vetoes: list[str], details: dict) -> None:
    session.add(
        RiskDecisionRow(
            candidate_id=candidate_id,
            lane=lane,
            allowed=allowed,
            vetoes_json=dumps(vetoes),
            details_json=dumps(details),
            created_ms=utc_now_ms(),
        )
    )


def insert_jev(session: Session, candidate_id: str, state: dict, state_hash: str, questions: dict, result) -> int:
    row = JevReviewRow(
        candidate_id=candidate_id,
        state_hash=state_hash,
        state_json=dumps(state),
        questions_json=dumps(questions),
        model_requested=result.model_requested,
        model_returned=result.model_returned,
        status=result.status,
        answers_json=dumps(result.answers),
        usage_json=dumps(result.usage),
        error=result.error or "",
        created_ms=utc_now_ms(),
    )
    session.add(row)
    session.flush()
    return int(row.id)


def insert_context(session: Session, candidate_id: str | None, market_id: str, snapshot: dict) -> None:
    session.add(
        ContextSnapshotRow(
            candidate_id=candidate_id,
            market_id=market_id,
            time_ms=utc_now_ms(),
            snapshot_json=dumps(snapshot),
        )
    )


def set_checkpoint(session: Session, key: str, value: dict) -> None:
    row = session.get(CheckpointRow, key)
    payload = dumps(value)
    now = utc_now_ms()
    if row is None:
        session.add(CheckpointRow(key=key, value_json=payload, updated_ms=now))
    else:
        row.value_json = payload
        row.updated_ms = now


def get_checkpoint(session: Session, key: str) -> dict | None:
    row = session.get(CheckpointRow, key)
    if row is None:
        return None
    return loads(row.value_json)


def save_copy_obs(session: Session, address: str, market_id: str, tid: str, obs: list[dict]) -> None:
    for item in obs:
        exists = session.scalar(
            select(CopyabilityRow.id).where(
                CopyabilityRow.address == address,
                CopyabilityRow.fill_tid == tid,
                CopyabilityRow.delay_s == int(item["delay_s"]),
            )
        )
        if exists is not None:
            continue
        session.add(
            CopyabilityRow(
                address=address,
                market_id=market_id,
                fill_tid=tid,
                delay_s=int(item["delay_s"]),
                status=str(item.get("status") or "unknown"),
                payload_json=dumps(item),
            )
        )


def record_testnet_order(session: Session, payload: dict[str, Any]) -> None:
    session.add(
        TestnetOrderRow(
            purpose=str(payload.get("purpose") or "smoke"),
            coin=str(payload.get("coin") or ""),
            is_buy=bool(payload.get("is_buy", True)),
            size=str(payload.get("size") or ""),
            limit_px=str(payload.get("limit_px") or ""),
            reduce_only=bool(payload.get("reduce_only", False)),
            oid=None if payload.get("oid") is None else str(payload.get("oid")),
            status=str(payload.get("status") or ""),
            raw_response=dumps(payload.get("raw") or {}),
            error=str(payload.get("error") or ""),
            created_ms=utc_now_ms(),
        )
    )


def prune(session: Session, *, trade_cutoff_ms: int, snapshot_cutoff_ms: int) -> dict[str, int]:
    trades = session.execute(delete(TradeRow).where(TradeRow.time_ms < trade_cutoff_ms))
    snaps = session.execute(
        delete(ContextSnapshotRow).where(
            ContextSnapshotRow.time_ms < snapshot_cutoff_ms,
            ContextSnapshotRow.candidate_id.is_(None),
        )
    )
    return {"trades": int(trades.rowcount or 0), "snapshots": int(snaps.rowcount or 0)}


def _pos_from_row(row: PaperPositionRow) -> PaperPosition:
    return PaperPosition(
        lane=row.lane,
        candidate_id=row.candidate_id,
        market_id=row.market_id,
        coin=row.coin,
        direction=row.direction,
        size=D(row.size),
        entry_px=D(row.entry_px),
        entry_fee=D(row.entry_fee),
        entry_fee_assumption=row.entry_fee_assumption,
        entry_notional=D(row.entry_notional),
        funding_rate=D(row.funding_rate or "0"),
        opened_ms=row.opened_ms,
        status=row.status,
        exit_px=None if row.exit_px is None else D(row.exit_px),
        exit_fee=None if row.exit_fee is None else D(row.exit_fee),
        exit_fee_assumption=row.exit_fee_assumption,
        exit_reason=row.exit_reason,
        closed_ms=row.closed_ms,
        realized_pnl=None if row.realized_pnl is None else D(row.realized_pnl),
        funding_pnl=None if row.funding_pnl is None else D(row.funding_pnl),
        entry_slippage_bps=None if row.entry_slippage_bps is None else D(row.entry_slippage_bps),
        exit_slippage_bps=None if row.exit_slippage_bps is None else D(row.exit_slippage_bps),
    )


def count_rows(session: Session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def baseline_priority(session: Session, addresses: list[str], now_ms: int, fresh_ms: int = 900_000) -> list[str]:
    """Live-but-stale wallets, then wallets with no baseline. The ring does the rest."""
    wanted = set(addresses)
    if not wanted:
        return []
    recent = set(
        session.scalars(
            select(WalletActionRow.address).where(
                WalletActionRow.source == "live",
                WalletActionRow.time_ms >= now_ms - 3_600_000,
                WalletActionRow.address.in_(wanted),
            )
        ).all()
    )
    baselines: dict[str, int] = {}
    rows = session.scalars(
        select(TrackedPositionRow).where(
            TrackedPositionRow.address.in_(wanted),
            TrackedPositionRow.market_id.like("dexbaseline:%"),
        )
    ).all()
    for row in rows:
        baselines[row.address] = max(baselines.get(row.address, 0), int(row.baseline_time_ms or 0))
    live_stale = sorted(addr for addr in recent if now_ms - baselines.get(addr, 0) > fresh_ms)
    missing = sorted(addr for addr in wanted if addr not in baselines and addr not in live_stale)
    return live_stale + missing


def research_summary(session: Session, now_ms: int | None = None) -> dict[str, Any]:
    now = utc_now_ms() if now_ms is None else now_ms
    markets = list(session.scalars(select(MarketRow)).all())
    tradable = [row for row in markets if not str(row.market_id).startswith("dexbaseline:")]
    active = [row for row in tradable if row.status == "active" and not row.is_delisted]
    hip3 = [row for row in active if row.dex not in ("", "core")]
    scales: dict[str, int] = {}
    collateral: dict[str, int] = {}
    for row in active:
        if row.dex in ("", "core"):
            scale_key = "core"
        else:
            scale_key = "missing" if row.deployer_fee_scale in (None, "") else str(row.deployer_fee_scale)
            growth = "growth" if str(row.growth_mode or "").lower() == "enabled" else "base"
            scale_key = f"{scale_key}:{growth}"
        scales[scale_key] = scales.get(scale_key, 0) + 1
        token = "missing" if row.collateral_token is None else str(row.collateral_token)
        collateral[token] = collateral.get(token, 0) + 1
    copy_rows = list(session.scalars(select(WalletRow).where(WalletRow.verified.is_(True))).all())
    copy_addresses = {row.address for row in copy_rows}
    baseline_times: dict[str, int] = {}
    if copy_addresses:
        tracked = session.scalars(
            select(TrackedPositionRow).where(
                TrackedPositionRow.address.in_(copy_addresses),
                TrackedPositionRow.market_id.like("dexbaseline:%"),
            )
        ).all()
        for row in tracked:
            baseline_times[row.address] = max(
                baseline_times.get(row.address, 0), int(row.baseline_time_ms or 0)
            )
    ages = [now - ts for ts in baseline_times.values() if ts > 0]
    vetoes: dict[str, int] = {}
    for raw in session.scalars(select(RiskDecisionRow.vetoes_json)).all():
        for code in loads(raw) or []:
            vetoes[str(code)] = vetoes.get(str(code), 0) + 1
    lanes = {}
    for lane in LANES:
        account = load_account(session, lane)
        lanes[lane] = {
            "cash": format(account.cash, "f"),
            "peak_equity": format(account.peak_equity, "f"),
            "realized_pnl": format(account.realized_pnl, "f"),
            "open_positions": len(account.open_positions()),
        }
    hydrated = int(
        session.scalar(
            select(func.count()).select_from(WalletRow).where(WalletRow.hydration_status == "done")
        )
        or 0
    )
    performance = int(
        session.scalar(
            select(func.count())
            .select_from(WalletRow)
            .where(WalletRow.verification_stage == "performance")
        )
        or 0
    )
    return {
        "markets_total": len(tradable),
        "active_markets": len(active),
        "active_hip3": len(hip3),
        "fee_scale_distribution": scales,
        "collateral_distribution": collateral,
        "trades": count_rows(session, TradeRow),
        "wallets": count_rows(session, WalletRow),
        "hydrated_wallets": hydrated,
        "performance_verified": performance,
        "copy_verified": len(copy_addresses),
        "baseline_coverage": f"{len(baseline_times)}/{len(copy_addresses)}",
        "baseline_wallets": len(baseline_times),
        "actionable_wallets": len(copy_addresses),
        "oldest_baseline_age_ms": None if not ages else max(ages),
        "candidates": count_rows(session, CandidateRow),
        "hard_vetoes": vetoes,
        "paper_lanes": lanes,
    }
