"""SQLite schema. Audit, candidates, and paper results are not retention-pruned."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for the research database."""


class SchemaMeta(Base):
    __tablename__ = "schema_meta"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class MarketRow(Base):
    __tablename__ = "markets"
    market_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    dex: Mapped[str] = mapped_column(String(64), index=True)
    coin: Mapped[str] = mapped_column(String(80), index=True)
    asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sz_decimals: Mapped[int] = mapped_column(Integer, default=0)
    max_leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    margin_table_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    margin_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    only_isolated: Mapped[bool] = mapped_column(Boolean, default=False)
    growth_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deployer_fee_scale: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_fee_scale_change_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    collateral_token: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_delisted: Mapped[bool] = mapped_column(Boolean, default=False)
    mark_px: Mapped[str | None] = mapped_column(String(64), nullable=True)
    oracle_px: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mid_px: Mapped[str | None] = mapped_column(String(64), nullable=True)
    funding: Mapped[str | None] = mapped_column(String(64), nullable=True)
    open_interest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    day_ntl_vlm: Mapped[str | None] = mapped_column(String(64), nullable=True)
    premium: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prev_day_px: Mapped[str | None] = mapped_column(String(64), nullable=True)
    impact_pxs: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    hip3_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class TradeRow(Base):
    __tablename__ = "trades"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_trade_idem"),)
    tid: Mapped[str] = mapped_column(String(200), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    coin: Mapped[str] = mapped_column(String(80), index=True)
    market_id: Mapped[str] = mapped_column(String(160), index=True)
    side: Mapped[str] = mapped_column(String(8))
    aggressor: Mapped[str] = mapped_column(String(8))
    price: Mapped[str] = mapped_column(String(64))
    size: Mapped[str] = mapped_column(String(64))
    notional: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(80), default="")
    time_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    buyer: Mapped[str] = mapped_column(String(66), index=True)
    seller: Mapped[str] = mapped_column(String(66), index=True)
    raw_json: Mapped[str] = mapped_column(Text, default="")


class WalletRow(Base):
    __tablename__ = "wallets"
    address: Mapped[str] = mapped_column(String(66), primary_key=True)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    notional: Mapped[str] = mapped_column(String(64), default="0")
    buy_notional: Mapped[str] = mapped_column(String(64), default="0")
    sell_notional: Mapped[str] = mapped_column(String(64), default="0")
    markets_json: Mapped[str] = mapped_column(Text, default="[]")
    first_seen_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    last_seen_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    trades_per_hour: Mapped[str] = mapped_column(String(64), default="0")
    hydration_status: Mapped[str] = mapped_column(String(16), default="none")
    history_limited: Mapped[bool] = mapped_column(Boolean, default=True)
    completeness_label: Mapped[str] = mapped_column(String(80), default="unknown")
    lifetime_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    performance_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_stage: Mapped[str] = mapped_column(String(16), default="none")
    copy_observations: Mapped[int] = mapped_column(Integer, default=0)
    tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class HydrationJob(Base):
    __tablename__ = "hydration_jobs"
    address: Mapped[str] = mapped_column(String(66), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class WalletFillRow(Base):
    __tablename__ = "wallet_fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(66), index=True)
    tid: Mapped[str] = mapped_column(String(80), index=True)
    coin: Mapped[str] = mapped_column(String(80))
    market_id: Mapped[str] = mapped_column(String(160), index=True)
    time_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    price: Mapped[str] = mapped_column(String(64))
    size: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))
    direction_raw: Mapped[str] = mapped_column(String(32), default="")
    start_position: Mapped[str | None] = mapped_column(String(64), nullable=True)
    closed_pnl: Mapped[str] = mapped_column(String(64), default="0")
    fee: Mapped[str] = mapped_column(String(64), default="0")
    fee_token: Mapped[str] = mapped_column(String(16), default="")
    oid: Mapped[str] = mapped_column(String(40), default="")
    hash: Mapped[str] = mapped_column(String(80), default="")
    crossed: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1 taker, 0 maker, null unknown
    raw_json: Mapped[str] = mapped_column(Text, default="")
    fill_key: Mapped[str] = mapped_column(String(220), default="")
    __table_args__ = (UniqueConstraint("address", "fill_key", name="uq_wallet_fill_key"),)


class EpisodeRow(Base):
    __tablename__ = "wallet_episodes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(66), index=True)
    market_id: Mapped[str] = mapped_column(String(160))
    coin: Mapped[str] = mapped_column(String(80))
    direction: Mapped[str] = mapped_column(String(8))
    entry_time_ms: Mapped[int] = mapped_column(BigInteger)
    exit_time_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    entry_price: Mapped[str] = mapped_column(String(64))
    exit_price: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_size: Mapped[str] = mapped_column(String(64))
    closed_pnl: Mapped[str] = mapped_column(String(64), default="0")
    fees: Mapped[str] = mapped_column(String(64), default="0")
    entry_notional: Mapped[str] = mapped_column(String(64), default="0")
    closed: Mapped[bool] = mapped_column(Boolean, default=False)


class MetricRow(Base):
    __tablename__ = "wallet_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(66), index=True)
    computed_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    metrics_json: Mapped[str] = mapped_column(Text)


class ScoreRow(Base):
    __tablename__ = "wallet_scores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(66), index=True)
    computed_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    score: Mapped[str] = mapped_column(String(32))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    breakdown_json: Mapped[str] = mapped_column(Text)


class GroupRow(Base):
    __tablename__ = "behavioral_groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(64), index=True)
    computed_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    members_json: Mapped[str] = mapped_column(Text)
    why_json: Mapped[str] = mapped_column(Text)


class TrackedPositionRow(Base):
    __tablename__ = "tracked_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(66), index=True)
    market_id: Mapped[str] = mapped_column(String(160), index=True)
    coin: Mapped[str] = mapped_column(String(80))
    signed_size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    known: Mapped[bool] = mapped_column(Boolean, default=False)
    baseline_time_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (UniqueConstraint("address", "market_id", name="uq_tracked_pos"),)


class WalletActionRow(Base):
    __tablename__ = "wallet_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(66), index=True)
    market_id: Mapped[str] = mapped_column(String(160), index=True)
    coin: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    time_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    price: Mapped[str] = mapped_column(String(64))
    size: Mapped[str] = mapped_column(String(64))
    prev_position: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_position: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tid: Mapped[str] = mapped_column(String(80), default="")
    source: Mapped[str] = mapped_column(String(16), default="live")
    __table_args__ = (UniqueConstraint("address", "tid", "action", name="uq_wallet_action"),)


class CandidateRow(Base):
    __tablename__ = "convergence_candidates"
    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_id: Mapped[str] = mapped_column(String(160), index=True)
    coin: Mapped[str] = mapped_column(String(80))
    dex: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8), index=True)
    first_ms: Mapped[int] = mapped_column(BigInteger)
    trigger_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    packet_json: Mapped[str] = mapped_column(Text)
    packet_hash: Mapped[str] = mapped_column(String(64))


class ContextSnapshotRow(Base):
    __tablename__ = "market_context_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    market_id: Mapped[str] = mapped_column(String(160), index=True)
    time_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    snapshot_json: Mapped[str] = mapped_column(Text)


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    lane: Mapped[str] = mapped_column(String(32))
    allowed: Mapped[bool] = mapped_column(Boolean)
    vetoes_json: Mapped[str] = mapped_column(Text)
    details_json: Mapped[str] = mapped_column(Text)
    created_ms: Mapped[int] = mapped_column(BigInteger)


class JevReviewRow(Base):
    __tablename__ = "jev_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    state_hash: Mapped[str] = mapped_column(String(64))
    state_json: Mapped[str] = mapped_column(Text)
    questions_json: Mapped[str] = mapped_column(Text)
    model_requested: Mapped[str] = mapped_column(String(64))
    model_returned: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    answers_json: Mapped[str] = mapped_column(Text)
    usage_json: Mapped[str] = mapped_column(Text)
    error: Mapped[str] = mapped_column(Text, default="")
    created_ms: Mapped[int] = mapped_column(BigInteger, index=True)


class LaneDecisionRow(Base):
    __tablename__ = "lane_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    lane: Mapped[str] = mapped_column(String(32), index=True)
    accepted: Mapped[bool] = mapped_column(Boolean)
    vetoes_json: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(64), default="")
    created_ms: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("candidate_id", "lane", name="uq_lane_decision"),)


class PaperAccountRow(Base):
    __tablename__ = "paper_accounts"
    lane: Mapped[str] = mapped_column(String(32), primary_key=True)
    starting_equity: Mapped[str] = mapped_column(String(64))
    cash: Mapped[str] = mapped_column(String(64))
    peak_equity: Mapped[str] = mapped_column(String(64))
    realized_pnl: Mapped[str] = mapped_column(String(64), default="0")
    fees: Mapped[str] = mapped_column(String(64), default="0")
    funding: Mapped[str] = mapped_column(String(64), default="0")
    day_utc: Mapped[str] = mapped_column(String(16), default="")
    day_realized: Mapped[str] = mapped_column(String(64), default="0")
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)


class PaperPositionRow(Base):
    __tablename__ = "paper_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lane: Mapped[str] = mapped_column(String(32), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True)
    market_id: Mapped[str] = mapped_column(String(160), index=True)
    coin: Mapped[str] = mapped_column(String(80))
    direction: Mapped[str] = mapped_column(String(8))
    size: Mapped[str] = mapped_column(String(64))
    entry_px: Mapped[str] = mapped_column(String(64))
    entry_fee: Mapped[str] = mapped_column(String(64))
    entry_fee_assumption: Mapped[str] = mapped_column(String(80))
    entry_notional: Mapped[str] = mapped_column(String(64))
    funding_rate: Mapped[str] = mapped_column(String(64), default="0")
    opened_ms: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), index=True, default="open")
    exit_px: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_fee: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_fee_assumption: Mapped[str | None] = mapped_column(String(80), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    closed_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    realized_pnl: Mapped[str | None] = mapped_column(String(64), nullable=True)
    funding_pnl: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_slippage_bps: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exit_slippage_bps: Mapped[str | None] = mapped_column(String(32), nullable=True)


class PaperFillRow(Base):
    __tablename__ = "paper_fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(Integer, index=True)
    lane: Mapped[str] = mapped_column(String(32), index=True)
    candidate_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(8))
    direction: Mapped[str] = mapped_column(String(8))
    price: Mapped[str] = mapped_column(String(64))
    size: Mapped[str] = mapped_column(String(64))
    notional: Mapped[str] = mapped_column(String(64))
    fee: Mapped[str] = mapped_column(String(64))
    fee_assumption: Mapped[str] = mapped_column(String(80))
    slippage_bps: Mapped[str | None] = mapped_column(String(32), nullable=True)
    time_ms: Mapped[int] = mapped_column(BigInteger)
    book_json: Mapped[str] = mapped_column(Text, default="")
    fee_inputs_json: Mapped[str] = mapped_column(Text, default="")


class TestnetOrderRow(Base):
    __tablename__ = "testnet_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    purpose: Mapped[str] = mapped_column(String(32))
    coin: Mapped[str] = mapped_column(String(80))
    is_buy: Mapped[bool] = mapped_column(Boolean)
    size: Mapped[str] = mapped_column(String(64))
    limit_px: Mapped[str] = mapped_column(String(64))
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    oid: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="")
    raw_response: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_ms: Mapped[int] = mapped_column(BigInteger, index=True)


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lane: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class CheckpointRow(Base):
    __tablename__ = "service_checkpoints"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    updated_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class CopyabilityRow(Base):
    __tablename__ = "copyability_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(66), index=True)
    market_id: Mapped[str] = mapped_column(String(160))
    fill_tid: Mapped[str] = mapped_column(String(80))
    delay_s: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    payload_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("address", "fill_tid", "delay_s", name="uq_copy_obs"),)
