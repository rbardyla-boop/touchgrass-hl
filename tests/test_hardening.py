"""Regression tests for v0.1.1 correctness hardening. No network."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import text

from touchgrass_hl.baseline_queue import plan_baseline_batch
from touchgrass_hl.config import official_perp_fee
from touchgrass_hl.db.repo import add_open_position, store_wallet_fills, wallet_fill_key
from touchgrass_hl.db.schema import (
    MarketRow,
    PaperFillRow,
    SchemaMeta,
    TradeRow,
    WalletFillRow,
    WalletRow,
)
from touchgrass_hl.db.session import (
    init_db,
    make_engine,
    schema_version,
    session_factory,
    session_scope,
)
from touchgrass_hl.hyperliquid_client import user_fills_reserve
from touchgrass_hl.independence import BehaviorEvent, IndependenceConfig, build_groups
from touchgrass_hl.market_registry import rows_from_meta
from touchgrass_hl.models import AccountView, Book, BookLevel, Episode, FillRecord
from touchgrass_hl.paper import LANES, AccountState, PaperPosition, marked_account
from touchgrass_hl.position_tracker import reconstruct
from touchgrass_hl.rate_limit import PRIORITY_DISCOVERY, RateLimiter
from touchgrass_hl.risk import RiskConfig, evaluate_risk
from touchgrass_hl.util import market_id
from touchgrass_hl.wallet_scoring import compute_metrics, score_population


def _fill(tid, start, side, size, px, closed, fee, time, coin="BTC"):
    return FillRecord(
        time_ms=time,
        coin=coin,
        market_id=market_id("", coin),
        price=Decimal(px),
        size=Decimal(size),
        side=side,
        start_position=Decimal(start),
        closed_pnl=Decimal(closed),
        fee=Decimal(fee),
        oid="1",
        tid=str(tid),
        crossed=True,
        direction_raw="",
    )


def _episode(pnl: str, notional: str, closed: bool = True) -> Episode:
    return Episode(
        market_id="perp:core:BTC",
        coin="BTC",
        direction="LONG",
        entry_time_ms=1,
        exit_time_ms=2 if closed else None,
        entry_price=Decimal(notional),
        exit_price=Decimal(notional) if closed else None,
        entry_size=Decimal(1),
        closed_pnl=Decimal(pnl),
        fees=Decimal(0),
        entry_notional=Decimal(notional),
        closed=closed,
    )


def test_opening_add_close_and_flip_keep_every_closed_pnl():
    fills = [
        _fill(1, "0", "B", "1", "100", "-0.05", "0.05", 1_000),
        _fill(2, "1", "B", "1", "100", "-0.02", "0.02", 2_000),
        _fill(3, "2", "A", "2", "110", "19.90", "0.10", 3_000),
    ]
    episodes, actions, unmatched = reconstruct(fills)
    assert [item.action for item in actions] == ["OPEN_LONG", "ADD_LONG", "CLOSE_LONG"]
    assert unmatched == 0
    closed = [ep for ep in episodes if ep.closed]
    assert len(closed) == 1
    assert closed[0].closed_pnl == Decimal("-0.05") + Decimal("-0.02") + Decimal("19.90")
    assert closed[0].fees == Decimal("0.17")
    metrics = compute_metrics(
        episodes,
        fills,
        copyability=None,
        completeness_label="partial",
        requested_window_fully_returned=False,
        unmatched_closes=unmatched,
    )
    assert metrics.pnl_reconciled
    assert metrics.reconstructed_net_pnl == metrics.hyperliquid_closed_pnl
    assert metrics.realized_pnl == Decimal("19.83")

    flip = [
        _fill(1, "0", "B", "1", "100", "-0.04", "0.04", 1_000),
        _fill(2, "1", "A", "2", "110", "5.00", "0.06", 2_000),
    ]
    flip_eps, flip_actions, flip_unmatched = reconstruct(flip)
    assert flip_actions[1].action == "FLIP_LONG_TO_SHORT"
    assert flip_unmatched == 0
    flip_metrics = compute_metrics(
        flip_eps,
        flip,
        copyability=None,
        completeness_label="partial",
        requested_window_fully_returned=False,
        unmatched_closes=flip_unmatched,
    )
    assert flip_metrics.reconstructed_net_pnl == Decimal("4.96")
    assert flip_metrics.pnl_reconciled
    short_open = [ep for ep in flip_eps if not ep.closed][0]
    assert short_open.direction == "SHORT"
    assert short_open.closed_pnl == Decimal("-0.03")


def test_short_open_add_reduce_close_retains_fees():
    fills = [
        _fill(1, "0", "A", "2", "50", "-0.04", "0.04", 1_000),
        _fill(2, "-2", "A", "1", "50", "-0.01", "0.01", 2_000),
        _fill(3, "-3", "B", "1", "40", "9.99", "0.02", 3_000),
        _fill(4, "-2", "B", "2", "40", "19.97", "0.03", 4_000),
    ]
    episodes, actions, unmatched = reconstruct(fills)
    assert [item.action for item in actions] == [
        "OPEN_SHORT",
        "ADD_SHORT",
        "REDUCE_SHORT",
        "CLOSE_SHORT",
    ]
    assert unmatched == 0
    total = sum((ep.closed_pnl for ep in episodes), Decimal(0))
    hl = sum((fill.closed_pnl for fill in fills), Decimal(0))
    assert total == hl
    assert total == Decimal("29.91")


def test_drawdown_before_positive_peak_is_not_zero():
    episode = _episode("-10", "100")
    fill = _fill(1, "0", "B", "1", "100", "-10", "0", 1_000)
    metrics = compute_metrics(
        [episode],
        [fill],
        copyability=None,
        completeness_label="partial",
        requested_window_fully_returned=True,
    )
    assert metrics.max_drawdown == Decimal("0.1")
    assert metrics.max_drawdown_usd == Decimal(10)
    assert metrics.pnl_reconciled


def test_verification_stages_require_copy_evidence():
    base = compute_metrics(
        [],
        [],
        copyability=None,
        completeness_label="partial",
        requested_window_fully_returned=True,
    )
    performance = base
    performance.realized_pnl = Decimal(50)
    performance.closed_trades = 40
    performance.win_rate = Decimal("0.6")
    performance.profit_factor = Decimal("1.8")
    performance.consistency = Decimal("0.6")
    performance.max_drawdown = Decimal("0.1")
    performance.profit_concentration = Decimal("0.2")
    performance.data_completeness = Decimal(1)
    performance.active_days = 20
    performance.copyability = None
    performance.copyability_known = False
    performance.copyability_observations = 0
    weights = {"profitability": Decimal(1)}
    scored = score_population(
        [("perf", performance)],
        weights,
        min_closed_trades=30,
        min_active_days=14,
        max_profit_concentration=Decimal("0.5"),
        allow_concentration_override=False,
        min_copyability_observations=10,
    )
    assert scored["perf"].performance_verified
    assert not scored["perf"].copy_verified
    assert scored["perf"].verification_stage == "performance"
    assert "MIN_COPYABILITY_OBSERVATIONS" in scored["perf"].copy_reasons

    copied = compute_metrics(
        [],
        [],
        copyability=Decimal("0.001"),
        completeness_label="partial",
        requested_window_fully_returned=True,
        copyability_observations=10,
    )
    copied.realized_pnl = Decimal(50)
    copied.closed_trades = 40
    copied.profit_factor = Decimal("1.8")
    copied.profit_concentration = Decimal("0.2")
    copied.active_days = 20
    copied.copyability_known = True
    ready = score_population(
        [("copy", copied)],
        weights,
        min_closed_trades=30,
        min_active_days=14,
        max_profit_concentration=Decimal("0.5"),
        allow_concentration_override=False,
    )
    assert ready["copy"].copy_verified
    assert ready["copy"].verification_stage == "copy"


def test_normalized_return_ignores_account_size():
    small = compute_metrics(
        [_episode("10", "100")],
        [_fill(1, "0", "B", "1", "100", "10", "0", 1_000)],
        copyability=None,
        completeness_label="partial",
        requested_window_fully_returned=True,
    )
    big = compute_metrics(
        [_episode("10000", "100000")],
        [_fill(1, "0", "B", "1", "100000", "10000", "0", 1_000)],
        copyability=None,
        completeness_label="partial",
        requested_window_fully_returned=True,
    )
    assert small.normalized_return == Decimal("0.1")
    assert big.normalized_return == small.normalized_return
    assert small.realized_pnl != big.realized_pnl
    scored = score_population(
        [("small", small), ("big", big)],
        {"profitability": Decimal(1)},
        min_closed_trades=1,
        min_active_days=0,
        max_profit_concentration=Decimal(1),
        allow_concentration_override=True,
    )
    small_value = scored["small"].components["profitability"]["value"]
    big_value = scored["big"].components["profitability"]["value"]
    assert small_value == big_value


def test_official_hip3_fee_scale():
    base = Decimal("0.00045")
    core, _ = official_perp_fee(
        base_rate=base,
        role="taker",
        deployer_fee_scale=None,
        growth_mode=False,
        hip3=False,
    )
    assert core == Decimal("0.00045")
    cases = {
        Decimal(0): Decimal("0.00045"),
        Decimal("0.5"): Decimal("0.000675"),
        Decimal(1): Decimal("0.0009"),
        Decimal(3): Decimal("0.0027"),
    }
    for scale, expected in cases.items():
        rate, inputs = official_perp_fee(
            base_rate=base,
            role="taker",
            deployer_fee_scale=scale,
            growth_mode=False,
            hip3=True,
        )
        assert rate == expected
        assert inputs["known"] is True
    growth, _ = official_perp_fee(
        base_rate=base,
        role="taker",
        deployer_fee_scale=Decimal(1),
        growth_mode=True,
        hip3=True,
    )
    assert growth == Decimal("0.00009")
    aligned, _ = official_perp_fee(
        base_rate=base,
        role="taker",
        deployer_fee_scale=Decimal(1),
        growth_mode=False,
        aligned_quote=True,
        hip3=True,
    )
    assert aligned == Decimal("0.00081")
    missing, info = official_perp_fee(
        base_rate=base,
        role="taker",
        deployer_fee_scale=None,
        growth_mode=False,
        hip3=True,
    )
    assert missing is None
    assert info["known"] is False
    assert user_fills_reserve() == 120


def test_fee_snapshot_does_not_change_after_scale_update(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/fee.db")
    init_db(engine)
    factory = session_factory(engine)
    frozen = {"rate": "0.0009", "deployer_fee_scale": "1", "growth_mode": False}
    pos = PaperPosition(
        lane=LANES[0],
        candidate_id="c1",
        market_id="perp:xyz:xyz:GOLD",
        coin="xyz:GOLD",
        direction="LONG",
        size=Decimal(1),
        entry_px=Decimal(10),
        entry_fee=Decimal("0.009"),
        entry_fee_assumption="official_hip3_taker_base",
        entry_notional=Decimal(10),
        funding_rate=Decimal(0),
        opened_ms=1,
        fee_inputs=frozen,
    )
    with session_scope(factory) as session:
        session.add(
            MarketRow(
                market_id="perp:xyz:xyz:GOLD",
                dex="xyz",
                coin="xyz:GOLD",
                sz_decimals=2,
                deployer_fee_scale="1",
                collateral_token=0,
                status="active",
                updated_ms=1,
            )
        )
        add_open_position(session, pos, "{}")
    with session_scope(factory) as session:
        market = session.get(MarketRow, "perp:xyz:xyz:GOLD")
        assert market is not None
        market.deployer_fee_scale = "3"
    with session_scope(factory) as session:
        fill = session.query(PaperFillRow).one()
        assert '"0.0009"' in fill.fee_inputs_json
        assert session.get(MarketRow, "perp:xyz:xyz:GOLD").deployer_fee_scale == "3"
    engine.dispose()


def _risk_cfg() -> RiskConfig:
    return RiskConfig(
        max_spread_bps=Decimal(50),
        max_slippage_bps=Decimal(100),
        max_price_move_bps=Decimal(100),
        stale_market_seconds=15,
        min_groups=1,
        max_positions=2,
        target_notional=Decimal(5),
        daily_loss_limit_usd=Decimal(5),
        max_drawdown_pct=Decimal("0.2"),
        sz_decimals=3,
        fee_rate=Decimal("0.00045"),
        fee_assumption="test",
    )


def _packet(**extra):
    packet = {
        "market_id": "perp:core:BTC",
        "dex": "core",
        "direction": "LONG",
        "behavioral_group_ids": ["g1"],
        "price_first": "100",
        "price_trigger": "100.1",
        "fee_known": True,
        "collateral_token": 0,
    }
    packet.update(extra)
    return packet


def _book():
    return Book(
        coin="BTC",
        time_ms=1,
        bids=[BookLevel(Decimal(100), Decimal(10), 1)],
        asks=[BookLevel(Decimal("100.1"), Decimal(10), 1)],
    )


def _view(**overrides) -> AccountView:
    base = dict(
        open_positions=0,
        open_markets={},
        recent_same_direction=False,
        daily_realized_pnl=Decimal(0),
        equity=Decimal(75),
        peak_equity=Decimal(75),
        free_cash=Decimal(75),
        kill_switch=False,
        marks_ok=True,
    )
    base.update(overrides)
    return AccountView(**base)


def _risk(packet, account):
    return evaluate_risk(
        packet=packet,
        book=_book(),
        account=account,
        cfg=_risk_cfg(),
        now_ms=1_000,
        ws_synced=True,
        market_status="active",
        context_time_ms=1_000,
        mark_px=Decimal(100),
        participating_verified=True,
    )


def test_unsupported_collateral_and_stale_mark_and_unrealized_drawdown():
    foreign = _risk(_packet(collateral_token=360, dex="km"), _view())
    assert "VETO_UNSUPPORTED_COLLATERAL" in foreign.vetoes
    assert foreign.allowed is False
    usdc = _risk(_packet(collateral_token=0, dex="core"), _view())
    assert "VETO_UNSUPPORTED_COLLATERAL" not in usdc.vetoes

    stale = _risk(_packet(), _view(marks_ok=False))
    assert "VETO_STALE_OPEN_MARK" in stale.vetoes
    assert stale.allowed is False

    account = AccountState(
        lane=LANES[0],
        starting_equity=Decimal(100),
        cash=Decimal(60),
        peak_equity=Decimal(100),
        positions=[
            PaperPosition(
                lane=LANES[0],
                candidate_id="open",
                market_id="perp:core:ETH",
                coin="ETH",
                direction="LONG",
                size=Decimal(1),
                entry_px=Decimal(100),
                entry_fee=Decimal(0),
                entry_fee_assumption="test",
                entry_notional=Decimal(40),
                funding_rate=Decimal(0),
                opened_ms=1,
            )
        ],
    )
    equity, peak, free = marked_account(account, {"perp:core:ETH": Decimal(50)}, True)
    assert equity == Decimal(50)
    assert peak == Decimal(100)
    assert free == Decimal(10)
    underwater = _risk(
        _packet(),
        _view(
            equity=equity,
            peak_equity=peak,
            free_cash=free,
            open_positions=1,
            open_markets={"perp:core:ETH": "LONG"},
        ),
    )
    assert "VETO_DRAWDOWN_KILL_SWITCH" in underwater.vetoes
    assert underwater.allowed is False
    flat = marked_account(account, {}, False)
    assert flat[2] == Decimal(0)


def test_baseline_rotation_passes_the_first_page():
    addresses = [f"0x{index:040x}" for index in range(25)]
    first, cursor = plan_baseline_batch(addresses, cursor=0, batch=10)
    second, cursor = plan_baseline_batch(addresses, cursor=cursor, batch=10)
    third, _cursor = plan_baseline_batch(addresses, cursor=cursor, batch=10)
    assert len(first) == 10
    assert set(first).isdisjoint(second)
    assert len(set(first) | set(second) | set(third)) == 25
    urgent = [addresses[-1]]
    batched, _next = plan_baseline_batch(addresses, cursor=0, batch=10, priority=urgent)
    assert batched[0] == addresses[-1]


def test_rate_limit_reserves_a_full_page_without_exceeding_budget():
    async def _go() -> None:
        limiter = RateLimiter(1000, 60)
        await limiter.acquire(950, PRIORITY_DISCOVERY)
        waiter = asyncio.create_task(limiter.acquire(120, PRIORITY_DISCOVERY))
        await asyncio.sleep(0.05)
        assert waiter.done() is False
        assert limiter.used() == 950
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)

        other = RateLimiter(1000, 60)
        await other.acquire(120, PRIORITY_DISCOVERY)
        other.refund(100)
        assert other.used() == 20

    asyncio.run(_go())


def test_wallet_fill_identity_keeps_same_tid_on_two_coins(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/fills.db")
    init_db(engine)
    factory = session_factory(engine)
    address = "0x" + "ab" * 20
    one = {
        "tid": "9",
        "coin": "BTC",
        "market_id": "perp:core:BTC",
        "time_ms": 1000,
        "price": "1",
        "size": "1",
        "side": "B",
        "closed_pnl": "0",
        "fee": "0",
    }
    two = dict(one)
    two["coin"] = "ETH"
    two["market_id"] = "perp:core:ETH"
    two["time_ms"] = 1001
    with session_scope(factory) as session:
        assert store_wallet_fills(session, address, [one, two]) == 2
        assert store_wallet_fills(session, address, [one]) == 0
        rows = session.query(WalletFillRow).all()
        assert {row.fill_key for row in rows} == {
            wallet_fill_key(1000, "BTC", "9"),
            wallet_fill_key(1001, "ETH", "9"),
        }
    engine.dispose()


def test_schema_migrates_v1_without_losing_rows(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/v1.db")
    TradeRow.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO trades (tid, idempotency_key, coin, market_id, side, aggressor, "
                "price, size, notional, hash, time_ms, buyer, seller, raw_json) "
                "VALUES ('tid:1:BTC:1', 'tid:1:BTC:1', 'BTC', 'perp:core:BTC', 'B', 'BUY', "
                "'1', '1', '1', '', 1, '0x11', '0x22', '{}')"
            )
        )
        conn.execute(
            text("CREATE TABLE schema_meta (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)")
        )
        conn.execute(text("INSERT INTO schema_meta (id, version) VALUES (1, 1)"))
        conn.execute(
            text(
                """
                CREATE TABLE wallet_fills (
                    id INTEGER PRIMARY KEY,
                    address VARCHAR(66),
                    tid VARCHAR(80),
                    coin VARCHAR(80),
                    market_id VARCHAR(160),
                    time_ms BIGINT,
                    price VARCHAR(64),
                    size VARCHAR(64),
                    side VARCHAR(8),
                    direction_raw VARCHAR(32),
                    start_position VARCHAR(64),
                    closed_pnl VARCHAR(64),
                    fee VARCHAR(64),
                    fee_token VARCHAR(16),
                    oid VARCHAR(40),
                    hash VARCHAR(80),
                    crossed INTEGER,
                    raw_json TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO wallet_fills (address, tid, coin, market_id, time_ms, price, size, "
                "side, direction_raw, start_position, closed_pnl, fee, fee_token, oid, hash, "
                "crossed, raw_json) VALUES ("
                "'0xabc', '9', 'BTC', 'perp:core:BTC', 1000, '1', '1', 'B', '', '0', '0', '0', "
                "'USDC', '', '', 1, '{}')"
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE wallets (
                    address VARCHAR(66) PRIMARY KEY,
                    trade_count INTEGER,
                    notional VARCHAR(64),
                    buy_notional VARCHAR(64),
                    sell_notional VARCHAR(64),
                    markets_json TEXT,
                    first_seen_ms BIGINT,
                    last_seen_ms BIGINT,
                    trades_per_hour VARCHAR(64),
                    hydration_status VARCHAR(16),
                    history_limited BOOLEAN,
                    completeness_label VARCHAR(80),
                    lifetime_complete BOOLEAN,
                    verified BOOLEAN,
                    tracked BOOLEAN,
                    updated_ms BIGINT
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO wallets (address, trade_count, notional, buy_notional, sell_notional, "
                "markets_json, first_seen_ms, last_seen_ms, trades_per_hour, hydration_status, "
                "history_limited, completeness_label, lifetime_complete, verified, tracked, "
                "updated_ms) VALUES ("
                "'0xabc', 1, '1', '1', '0', '[]', 1, 1, '0', 'done', 1, 'partial', 0, 1, 1, 1)"
            )
        )
    init_db(engine)
    assert schema_version(engine) == 2
    factory = session_factory(engine)
    with session_scope(factory) as session:
        assert session.query(TradeRow).count() == 1
        row = session.query(WalletFillRow).one()
        assert row.fill_key == "1000|BTC|9"
        wallet = session.get(WalletRow, "0xabc")
        assert wallet is not None
        assert wallet.verified is False
        assert wallet.performance_verified is True
        assert wallet.verification_stage == "performance"
        assert session.get(SchemaMeta, 1).version == 2
        added = store_wallet_fills(
            session,
            "0xabc",
            [
                {
                    "tid": "9",
                    "coin": "ETH",
                    "market_id": "perp:core:ETH",
                    "time_ms": 2000,
                    "price": "2",
                    "size": "1",
                    "side": "A",
                    "closed_pnl": "1",
                    "fee": "0",
                }
            ],
        )
        assert added == 1
    init_db(engine)
    with session_scope(factory) as session:
        assert session.query(WalletFillRow).count() == 2
        assert session.query(TradeRow).count() == 1
        assert schema_version(engine) == 2
    engine.dispose()


def test_market_metadata_keeps_fee_scale_and_collateral():
    payload = [
        {
            "universe": [
                {
                    "name": "xyz:GOLD",
                    "szDecimals": 3,
                    "maxLeverage": 10,
                    "deployerFeeScale": "1",
                    "growthMode": None,
                    "lastFeeScaleChangeTime": "2025-11-23T17:37:10.033211662",
                }
            ],
            "collateralToken": 0,
        },
        [{"markPx": "10", "dayNtlVlm": "5"}],
    ]
    rows = rows_from_meta(
        perp_dex_index=1,
        dex_name="xyz",
        payload=payload,
        dex_meta={"name": "xyz"},
        now_ms=1,
        exchange_halted=False,
    )
    assert rows[0]["deployer_fee_scale"] == "1"
    assert rows[0]["collateral_token"] == 0
    assert rows[0]["last_fee_scale_change_ms"] == 1763919430033
    assert rows[0]["growth_mode"] is None


def test_transitive_group_is_not_a_direct_link():
    events = []
    for offset in range(5):
        stamp = offset * 10_000
        events.append(BehaviorEvent("A", "perp:core:M1", "LONG", stamp, Decimal(1)))
        events.append(BehaviorEvent("B", "perp:core:M1", "LONG", stamp + 1, Decimal(1)))
        later = 100_000 + stamp
        events.append(BehaviorEvent("B", "perp:core:M1", "LONG", later, Decimal(1)))
        events.append(BehaviorEvent("C", "perp:core:M1", "LONG", later + 1, Decimal(1)))
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
    direct = {frozenset((edge["a"], edge["b"])) for edge in grouped["edges"]}
    assert frozenset(("A", "C")) not in direct
    assert frozenset(("A", "B")) in direct
    assert frozenset(("B", "C")) in direct
    group = next(item for item in grouped["groups"] if set(item["members"]) == {"A", "B", "C"})
    assert group["grouping"] == "transitive_union"
    pairs = {(item["a"], item["b"]) for item in group["transitive_pairs"]}
    assert ("A", "C") in pairs
    assert all(edge["link"] == "direct" for edge in grouped["edges"])
    assert all(edge["ownership_claim"] is False for edge in grouped["edges"])
