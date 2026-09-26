"""Unit tests for deterministic research logic. No network."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from touchgrass_hl.cluster_detector import ClusterConfig, ClusterDetector
from touchgrass_hl.config import Settings
from touchgrass_hl.db.repo import insert_trades
from touchgrass_hl.db.schema import TradeRow
from touchgrass_hl.db.session import (
    init_db,
    journal_mode,
    make_engine,
    session_factory,
    session_scope,
)
from touchgrass_hl.hyperliquid_client import (
    parse_book_payload,
    parse_trades_message,
    trade_idempotency_key,
    user_fills_by_time_weight,
)
from touchgrass_hl.independence import BehaviorEvent, IndependenceConfig, build_groups
from touchgrass_hl.jev import build_systemone_request, jev_policy_vetoes, parse_systemone_response
from touchgrass_hl.models import (
    AccountView,
    ActionEvent,
    Book,
    BookLevel,
    Episode,
    FillRecord,
    JevResult,
    Metrics,
)
from touchgrass_hl.paper import (
    AccountState,
    apply_open,
    close_from_book,
    decide_lane_acceptance,
    exit_reason,
    net_pnl,
    open_from_book,
)
from touchgrass_hl.position_tracker import classify_action, reconstruct
from touchgrass_hl.risk import RiskConfig, evaluate_risk
from touchgrass_hl.slippage import walk_notional, walk_size
from touchgrass_hl.testnet_executor import RefusingMainnetError, assert_testnet_url
from touchgrass_hl.util import asset_id_for, backoff_delay, market_id
from touchgrass_hl.wallet_scoring import (
    compute_metrics,
    copyability_observations,
    score_population,
    summarize_copyability,
    verify_wallet,
)
from touchgrass_hl.websocket_manager import SubscriptionManager


def _trade_msg(tid, users, side="B", coin="BTC", px="100", sz="1", time=1_700_000_000_000):
    return {
        "channel": "trades",
        "data": [
            {
                "coin": coin,
                "side": side,
                "px": px,
                "sz": sz,
                "hash": "0xabc",
                "time": time,
                "tid": tid,
                "users": users,
            }
        ],
    }


def test_trade_parse_and_idempotency_key():
    msg = _trade_msg(99, ["0xABC", "0xDEF"])
    trades = parse_trades_message(msg, lambda coin: market_id("", coin))
    assert len(trades) == 1
    trade = trades[0]
    assert trade.buyer == "0xabc"
    assert trade.seller == "0xdef"
    assert trade.aggressor == "BUY"
    assert trade.market_id == "perp:core:BTC"
    assert trade.idempotency_key == "tid:1700000000000:BTC:99"
    assert trade.tid == trade.idempotency_key
    assert trade_idempotency_key("99", {}) == "tid:99"
    assert user_fills_by_time_weight(0) == (20, 0)
    assert user_fills_by_time_weight(19) == (20, 0)
    assert user_fills_by_time_weight(2000) == (20, 100)


def test_duplicate_trade_rejection(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/dup.db")
    init_db(engine)
    factory = session_factory(engine)
    trades = parse_trades_message(_trade_msg(7, ["0x" + "11" * 20, "0x" + "22" * 20]), lambda c: market_id("", c))
    with session_scope(factory) as session:
        assert insert_trades(session, trades) == 1
    with session_scope(factory) as session:
        assert insert_trades(session, trades) == 0
        assert session.query(TradeRow).count() == 1
    other = parse_trades_message(
        _trade_msg(7, ["0x" + "11" * 20, "0x" + "22" * 20], coin="ETH"),
        lambda c: market_id("", c),
    )
    with session_scope(factory) as session:
        assert insert_trades(session, other) == 1
        assert session.query(TradeRow).count() == 2
    engine.dispose()


def test_market_normalization_no_collision():
    core = market_id("", "ABC")
    hip = market_id("xyz", "xyz:ABC")
    other = market_id("abcdex", "abcdex:ABC")
    assert core != hip != other
    assert asset_id_for(0, 0) == 0
    assert asset_id_for(1, 0) == 110_000
    assert asset_id_for(2, 3) == 100_000 + 20_000 + 3


def test_classify_actions():
    z = Decimal(0)
    one = Decimal(1)
    two = Decimal(2)
    assert classify_action(None, "B", one) == "UNKNOWN"
    assert classify_action(z, "B", one) == "OPEN_LONG"
    assert classify_action(z, "A", one) == "OPEN_SHORT"
    assert classify_action(one, "B", one) == "ADD_LONG"
    assert classify_action(two, "A", one) == "REDUCE_LONG"
    assert classify_action(one, "A", one) == "CLOSE_LONG"
    assert classify_action(one, "A", two) == "FLIP_LONG_TO_SHORT"
    assert classify_action(Decimal(-1), "A", one) == "ADD_SHORT"
    assert classify_action(Decimal(-2), "B", one) == "REDUCE_SHORT"
    assert classify_action(Decimal(-1), "B", one) == "CLOSE_SHORT"
    assert classify_action(Decimal(-1), "B", two) == "FLIP_SHORT_TO_LONG"


def _fill(tid, start, side, size, px, closed, time, coin="BTC"):
    return FillRecord(
        time_ms=time,
        coin=coin,
        market_id=market_id("", coin),
        price=Decimal(px),
        size=Decimal(size),
        side=side,
        start_position=None if start is None else Decimal(start),
        closed_pnl=Decimal(closed),
        fee=Decimal("0.01"),
        oid="1",
        tid=str(tid),
        crossed=True,
        direction_raw="",
    )


def test_reconstruct_round_trip_and_flip():
    fills = [
        _fill(1, "0", "B", "1", "100", "0", 1_000),
        _fill(2, "1", "A", "1", "110", "10", 5_000),
    ]
    episodes, actions, unmatched = reconstruct(fills)
    closed = [ep for ep in episodes if ep.closed]
    assert unmatched == 0
    assert actions[0].action == "OPEN_LONG"
    assert actions[1].action == "CLOSE_LONG"
    assert len(closed) == 1
    assert closed[0].closed_pnl == Decimal(10)
    flip = [
        _fill(1, "0", "B", "1", "100", "0", 1_000),
        _fill(2, "1", "A", "2", "90", "-10", 2_000),
    ]
    episodes, actions, _ = reconstruct(flip)
    assert actions[1].action == "FLIP_LONG_TO_SHORT"
    assert sum(1 for ep in episodes if ep.closed) == 1
    assert [ep for ep in episodes if not ep.closed][0].direction == "SHORT"


def test_metrics_and_concentration():
    episodes = []
    fills = []
    for i, pnl in enumerate([10, 10, 10, -5]):
        episodes.append(
            Episode(
                market_id="perp:core:BTC",
                coin="BTC",
                direction="LONG",
                entry_time_ms=i * 86_400_000,
                exit_time_ms=i * 86_400_000 + 60_000,
                entry_price=Decimal(100),
                exit_price=Decimal(100),
                entry_size=Decimal(1),
                closed_pnl=Decimal(pnl),
                fees=Decimal(0),
                entry_notional=Decimal(100),
                closed=True,
            )
        )
        fills.append(_fill(i, "0", "B", "1", "100", str(pnl), i * 86_400_000))
    metrics = compute_metrics(
        episodes,
        fills,
        copyability=None,
        completeness_label="requested_window_returned_but_not_lifetime_complete",
        requested_window_fully_returned=True,
    )
    assert metrics.closed_trades == 4
    assert metrics.wins == 3
    assert metrics.losses == 1
    assert metrics.profit_factor == Decimal(30) / Decimal(5)
    assert metrics.profit_concentration == Decimal(10) / Decimal(30)
    assert metrics.lifetime_complete is False
    assert metrics.copyability_known is False
    # One winner dominates.
    concentrated = [
        Episode(
            market_id="perp:core:BTC",
            coin="BTC",
            direction="LONG",
            entry_time_ms=1,
            exit_time_ms=2,
            entry_price=Decimal(100),
            exit_price=Decimal(100),
            entry_size=Decimal(1),
            closed_pnl=Decimal(90 if i == 0 else 10),
            fees=Decimal(0),
            entry_notional=Decimal(100),
            closed=True,
        )
        for i in range(2)
    ]
    conc = compute_metrics(concentrated, [], copyability=None, completeness_label="partial", requested_window_fully_returned=False)
    assert conc.profit_concentration > Decimal("0.5")
    ok, reasons = verify_wallet(
        conc,
        min_closed_trades=1,
        min_active_days=0,
        max_profit_concentration=Decimal("0.5"),
        allow_concentration_override=False,
    )
    assert not ok
    assert "PROFIT_CONCENTRATION" in reasons


def _metrics(**overrides) -> Metrics:
    base = compute_metrics([], [], copyability=None, completeness_label="partial", requested_window_fully_returned=True)
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_score_percentiles_and_verification():
    good = _metrics(
        realized_pnl=Decimal(50),
        closed_trades=40,
        wins=24,
        losses=16,
        win_rate=Decimal("0.6"),
        profit_factor=Decimal("1.8"),
        consistency=Decimal("0.6"),
        max_drawdown=Decimal("0.1"),
        profit_concentration=Decimal("0.2"),
        data_completeness=Decimal("1"),
        active_days=20,
        copyability=Decimal("0.001"),
        copyability_known=True,
    )
    weak = _metrics(
        realized_pnl=Decimal(-5),
        closed_trades=40,
        wins=10,
        losses=30,
        win_rate=Decimal("0.25"),
        profit_factor=Decimal("0.4"),
        consistency=Decimal("0.2"),
        max_drawdown=Decimal("0.8"),
        profit_concentration=Decimal("0.9"),
        data_completeness=Decimal("0.4"),
        active_days=20,
        copyability=Decimal("0.02"),
        copyability_known=True,
    )
    weights = {
        "profitability": Decimal("0.20"),
        "profit_factor": Decimal("0.15"),
        "consistency": Decimal("0.15"),
        "copyability": Decimal("0.15"),
        "win_rate": Decimal("0.10"),
        "inverse_drawdown": Decimal("0.10"),
        "inverse_concentration": Decimal("0.10"),
        "data_quality": Decimal("0.05"),
    }
    scored = score_population(
        [("good", good), ("weak", weak)],
        weights,
        min_closed_trades=30,
        min_active_days=14,
        max_profit_concentration=Decimal("0.50"),
        allow_concentration_override=False,
    )
    assert scored["good"].score > scored["weak"].score
    assert scored["good"].verified
    assert not scored["weak"].verified
    unknown = _metrics(
        realized_pnl=Decimal(10),
        closed_trades=40,
        win_rate=Decimal("0.6"),
        profit_factor=Decimal("1.5"),
        consistency=Decimal("0.5"),
        max_drawdown=Decimal("0.1"),
        profit_concentration=Decimal("0.2"),
        data_completeness=Decimal(1),
        active_days=20,
        copyability=None,
        copyability_known=False,
    )
    scored2 = score_population(
        [("u", unknown)],
        weights,
        min_closed_trades=30,
        min_active_days=14,
        max_profit_concentration=Decimal("0.5"),
        allow_concentration_override=False,
    )
    assert "copyability" in scored2["u"].omitted


def test_copyability_unknown_without_prints():
    obs = copyability_observations(
        leader_px=Decimal(100),
        direction="LONG",
        fill_time_ms=1_000,
        later_trades=[],
        delays_s=[2, 5],
        exit_px=Decimal(110),
    )
    assert all(item["status"] == "unknown" for item in obs)
    assert summarize_copyability([None, None]) is None
    known = copyability_observations(
        leader_px=Decimal(100),
        direction="LONG",
        fill_time_ms=1_000,
        later_trades=[(4_000, Decimal(101))],
        delays_s=[2],
        exit_px=Decimal(110),
    )
    assert known[0]["status"] == "known"
    assert Decimal(known[0]["adverse_move"]) == Decimal("0.01")
    assert known[0]["available_depth"] is None


def test_independence_and_cluster_counts_groups():
    events = []
    for i in range(6):
        t = i * 10_000
        events.append(BehaviorEvent("0xa", "perp:core:BTC", "LONG", t, Decimal(1)))
        events.append(BehaviorEvent("0xb", "perp:core:BTC", "LONG", t + 100, Decimal(1)))
        events.append(BehaviorEvent("0xc", "perp:core:ETH", "SHORT", t, Decimal(1)))
        events.append(BehaviorEvent("0xd", "perp:core:SOL", "LONG", t + 50_000, Decimal(2)))
    grouped = build_groups(
        events,
        IndependenceConfig(
            jaccard_min=Decimal("0.75"),
            min_simultaneous=4,
            proximity_ms=3000,
            min_events=5,
            max_wallets=50,
        ),
    )
    assert grouped["group_of"]["0xa"] == grouped["group_of"]["0xb"]
    assert grouped["group_of"]["0xc"] != grouped["group_of"]["0xa"]
    assert grouped["ownership_claim"] is False
    groups = grouped["group_of"]
    det = ClusterDetector(ClusterConfig(window_ms=120_000, min_groups=3))
    verified = {"0xa", "0xb", "0xc", "0xd"}
    base = 10_000_000
    actions = [
        ("0xa", "OPEN_LONG", "LONG"),
        ("0xb", "ADD_LONG", "LONG"),
        ("0xc", "OPEN_LONG", "LONG"),
    ]
    result = None
    for i, (wallet, action, direction) in enumerate(actions):
        result = det.on_action(
            ActionEvent(wallet, "perp:core:BTC", "BTC", "core", direction, action, base + i * 1000, Decimal(100), Decimal(1)),
            verified=verified,
            groups=groups,
            scores={},
        )
    assert result is None  # a and b share a group, c is a different group: only 2
    result = det.on_action(
        ActionEvent("0xd", "perp:core:BTC", "BTC", "core", "LONG", "OPEN_LONG", base + 2000, Decimal(101), Decimal(1)),
        verified=verified,
        groups=groups,
        scores={},
    )
    assert result is not None
    assert result["direction"] == "LONG"
    assert result["independent_group_count"] == 3
    assert len(result["behavioral_group_ids"]) == 3


def test_short_cluster_and_unknown_ignored():
    det = ClusterDetector(ClusterConfig(window_ms=120_000, min_groups=3))
    verified = {"0xa", "0xb", "0xc"}
    groups = {w: f"solo:{w}" for w in verified}
    out = None
    for i, wallet in enumerate(["0xa", "0xb", "0xc"]):
        out = det.on_action(
            ActionEvent(wallet, "perp:core:ETH", "ETH", "core", "SHORT", "OPEN_SHORT", 5_000 + i, Decimal(10), Decimal(1)),
            verified=verified,
            groups=groups,
            scores={},
        )
    assert out is not None
    assert out["direction"] == "SHORT"
    ignored = det.on_action(
        ActionEvent("0xa", "perp:core:ETH", "ETH", "core", "SHORT", "UNKNOWN", 9_000, Decimal(10), Decimal(1)),
        verified=verified,
        groups=groups,
        scores={},
    )
    assert ignored is None


def _book():
    return Book(
        coin="BTC",
        time_ms=1,
        bids=[BookLevel(Decimal(100), Decimal(2), 1), BookLevel(Decimal(99), Decimal(2), 1)],
        asks=[BookLevel(Decimal(101), Decimal(1), 1), BookLevel(Decimal(102), Decimal(2), 1)],
    )


def test_order_book_walk_and_slippage():
    book = _book()
    walked = walk_notional(book, Decimal(150), True)
    # 1 @ 101 (=101) then 49/102 of the next level
    assert walked.fully_filled
    assert walked.filled_notional == Decimal(150)
    assert walked.vwap is not None
    assert walked.vwap > Decimal(101)
    size_walk = walk_size(book, Decimal(1), True)
    assert size_walk.vwap == Decimal(101)
    assert size_walk.fully_filled
    parsed = parse_book_payload(
        {"coin": "BTC", "time": 5, "levels": [[{"px": "1", "sz": "2", "n": 1}], [{"px": "2", "sz": "3", "n": 1}]]}
    )
    assert parsed is not None
    assert parsed.bids[0].px == Decimal(1)


def _risk_cfg() -> RiskConfig:
    return RiskConfig(
        max_spread_bps=Decimal(50),
        max_slippage_bps=Decimal(100),
        max_price_move_bps=Decimal(100),
        stale_market_seconds=15,
        min_groups=3,
        max_positions=2,
        target_notional=Decimal(5),
        daily_loss_limit_usd=Decimal(5),
        max_drawdown_pct=Decimal("0.2"),
        sz_decimals=3,
        fee_rate=Decimal("0.00045"),
        fee_assumption="test",
    )


def _packet():
    return {
        "market_id": "perp:core:BTC",
        "direction": "LONG",
        "behavioral_group_ids": ["g1", "g2", "g3"],
        "price_first": "100",
        "price_trigger": "100.1",
    }


def _account(**overrides) -> AccountView:
    base = dict(
        open_positions=0,
        open_markets={},
        recent_same_direction=False,
        daily_realized_pnl=Decimal(0),
        equity=Decimal(75),
        peak_equity=Decimal(75),
        free_cash=Decimal(75),
        kill_switch=False,
    )
    base.update(overrides)
    return AccountView(**base)


def test_risk_vetoes_stale_spread_and_daily_loss():
    cfg = _risk_cfg()
    wide = Book(
        coin="BTC",
        time_ms=1,
        bids=[BookLevel(Decimal(100), Decimal(10), 1)],
        asks=[BookLevel(Decimal(102), Decimal(10), 1)],
    )
    result = evaluate_risk(
        packet=_packet(),
        book=wide,
        account=_account(),
        cfg=cfg,
        now_ms=100_000,
        ws_synced=True,
        market_status="active",
        context_time_ms=100_000,
        mark_px=Decimal(101),
        participating_verified=True,
    )
    assert "VETO_SPREAD_TOO_WIDE" in result.vetoes
    stale = evaluate_risk(
        packet=_packet(),
        book=_book(),
        account=_account(),
        cfg=cfg,
        now_ms=100_000,
        ws_synced=True,
        market_status="active",
        context_time_ms=1,
        mark_px=Decimal(100),
        participating_verified=True,
    )
    assert "VETO_STALE_MARKET_DATA" in stale.vetoes
    loss = evaluate_risk(
        packet=_packet(),
        book=_book(),
        account=_account(daily_realized_pnl=Decimal(-5)),
        cfg=cfg,
        now_ms=10_000,
        ws_synced=True,
        market_status="active",
        context_time_ms=10_000,
        mark_px=Decimal(100),
        participating_verified=True,
    )
    assert "VETO_DAILY_LOSS_LIMIT" in loss.vetoes
    assert loss.allowed is False


def _deep_book(bid, ask, size=100):
    return Book(
        coin="BTC",
        time_ms=1,
        bids=[BookLevel(Decimal(bid), Decimal(size), 1)],
        asks=[BookLevel(Decimal(ask), Decimal(size), 1)],
    )


def test_paper_long_short_fees_exits():
    fee = Decimal("0.00045")
    entry_book = _deep_book("99.9", "100")
    pos = open_from_book(
        lane="RULES_ONLY",
        candidate_id="c1",
        market_id="perp:core:BTC",
        coin="BTC",
        direction="LONG",
        book=entry_book,
        target_notional=Decimal(5),
        sz_decimals=3,
        fee_rate=fee,
        fee_assumption="configured_tier0_taker_no_discounts",
        funding_rate=Decimal(0),
        now_ms=0,
    )
    assert pos is not None
    assert pos.entry_fee > 0
    account = AccountState("RULES_ONLY", Decimal(75), Decimal(75), Decimal(75))
    assert apply_open(account, pos)
    start_cash = account.cash
    exit_book = _deep_book("102", "102.1", size=100)
    closed = close_from_book(account, pos, exit_book, reason="TAKE_PROFIT", now_ms=1_000, fee_rate=fee, fee_assumption="fee")
    assert closed is not None
    assert closed.realized_pnl == net_pnl("LONG", closed.entry_px, closed.exit_px, closed.size, closed.entry_fee, closed.exit_fee, Decimal(0))
    assert account.cash == start_cash + closed.entry_notional + (closed.realized_pnl + closed.entry_fee)
    # cash change from the pre-close cash equals price pnl - exit fee + funding, because entry fee already left cash.
    assert closed.exit_fee > 0
    assert exit_reason(closed, Decimal(100), 0, stop_pct=Decimal("0.01"), take_pct=Decimal("0.02"), max_hold_s=60) is None

    short = open_from_book(
        lane="RULES_ONLY",
        candidate_id="c2",
        market_id="perp:core:ETH",
        coin="ETH",
        direction="SHORT",
        book=_deep_book("100", "100.1"),
        target_notional=Decimal(5),
        sz_decimals=3,
        fee_rate=fee,
        fee_assumption="configured_tier0_taker_no_discounts",
        funding_rate=Decimal("0.0001"),
        now_ms=0,
    )
    assert short is not None
    short_account = AccountState("RULES_PLUS_JEV", Decimal(75), Decimal(75), Decimal(75))
    assert apply_open(short_account, short)
    # Independent of the long account.
    assert account.cash != short_account.cash or account.lane != short_account.lane
    stopped = short
    assert exit_reason(stopped, short.entry_px * Decimal("1.02"), 10, stop_pct=Decimal("0.01"), take_pct=Decimal("0.02"), max_hold_s=3600) == "STOP_LOSS"
    assert exit_reason(stopped, short.entry_px * Decimal("0.97"), 10, stop_pct=Decimal("0.01"), take_pct=Decimal("0.02"), max_hold_s=3600) == "TAKE_PROFIT"
    assert exit_reason(stopped, short.entry_px, 3_600_000, stop_pct=Decimal("0.01"), take_pct=Decimal("0.02"), max_hold_s=3600) == "TIME_EXIT"
    cover = _deep_book("98", "98.1")
    closed_short = close_from_book(
        short_account, short, cover, reason="TAKE_PROFIT", now_ms=1_000, fee_rate=fee, fee_assumption="fee"
    )
    assert closed_short.realized_pnl > 0
    assert short_account.wins == 1
    # Long account untouched.
    assert account.wins == 1


def test_lane_separation_and_jev_unavailable():
    assert decide_lane_acceptance(True, "RULES_ONLY", ["JEV_UNAVAILABLE"]) is True
    assert decide_lane_acceptance(True, "RULES_PLUS_JEV", ["JEV_UNAVAILABLE"]) is False
    assert decide_lane_acceptance(False, "RULES_ONLY", []) is False
    request = build_systemone_request("jev-latest", {"candidate_id": "abc"})
    assert request["questions"]["cluster_quality"]["type"] == "score"
    assert request["questions"]["behavior_fit"]["type"] == "noul"
    assert request["questions"]["regime"]["type"] == "choice"
    assert len(request["questions"]["cluster_quality"]["criteria"]) == 5
    parsed = parse_systemone_response(
        {
            "model": "jev-1.13.0",
            "answers": {
                "cluster_quality": {"type": "score", "score": 3.0, "confidence": 0.4, "probabilities": {"3": 0.5}},
                "behavior_fit": {"type": "noul", "noul": 0.8},
                "regime": {"type": "choice", "choice": "RANGE", "confidence": 0.3, "probabilities": {"RANGE": 0.4}},
                "contradiction": {"type": "noul", "noul": 0.1},
                "information_sufficient": {"type": "noul", "noul": 0.9},
            },
            "usage": {"input_tokens": 10, "output_tokens": 4},
        },
        model_requested="jev-latest",
    )
    assert parsed.status == "ok"
    assert parsed.model_returned == "jev-1.13.0"
    assert jev_policy_vetoes(parsed, min_cluster_quality=2, min_behavior_fit=0.55, max_contradiction=0.45, min_information_sufficient=0.55) == []
    bad = JevResult("unavailable", "jev-latest", None, {}, {}, "JEV_UNAVAILABLE")
    assert jev_policy_vetoes(bad, min_cluster_quality=2, min_behavior_fit=0.5, max_contradiction=0.5, min_information_sufficient=0.5) == ["JEV_UNAVAILABLE"]


def test_database_restart(tmp_path: Path):
    url = f"sqlite:///{tmp_path}/restart.db"
    engine = make_engine(url)
    init_db(engine)
    factory = session_factory(engine)
    trades = parse_trades_message(_trade_msg(42, ["0x" + "aa" * 20, "0x" + "bb" * 20]), lambda c: market_id("", c))
    with session_scope(factory) as session:
        insert_trades(session, trades)
    assert journal_mode(engine).lower() == "wal"
    engine.dispose()
    engine2 = make_engine(url)
    init_db(engine2)
    factory2 = session_factory(engine2)
    with session_scope(factory2) as session:
        assert session.get(TradeRow, trades[0].tid) is not None
        assert trades[0].tid == "tid:1700000000000:BTC:42"
    engine2.dispose()


def test_mainnet_execution_rejected(monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "mainnet")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    with pytest.raises(RefusingMainnetError):
        assert_testnet_url("https://api.hyperliquid.xyz")


def test_subscription_cap_and_backoff():
    mgr = SubscriptionManager(2)
    chosen = mgr.set_universe(["AAA", "BBB", "CCC"])
    assert chosen == ["AAA", "BBB"]
    assert "trades:CCC" not in mgr.desired
    assert mgr.desired["allDexsAssetCtxs"]["type"] == "allDexsAssetCtxs"
    subs, _ = mgr.diff()
    assert len(subs) == 3
    assert backoff_delay(0, cap_s=60, jitter_unit=1) == 1
    assert backoff_delay(3, cap_s=60, jitter_unit=1) == 8
    assert backoff_delay(10, cap_s=60, jitter_unit=1) == 60


def test_no_placeholders():
    root = Path(__file__).resolve().parents[1] / "src"
    for path in root.rglob("*.py"):
        text = path.read_text()
        for token in ("TODO", "FIXME"):
            assert token not in text, f"{path} contains {token}"
        assert "raise NotImplementedError" not in text
        for line in text.splitlines():
            stripped = line.strip()
            assert stripped != "pass"
            assert "placeholder" not in stripped.lower()
