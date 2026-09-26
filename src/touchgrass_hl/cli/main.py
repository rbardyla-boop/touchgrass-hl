"""touchgrass-hl command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal

from sqlalchemy import func, select

from touchgrass_hl import __version__
from touchgrass_hl.config import Settings
from touchgrass_hl.db.repo import count_rows, ensure_paper_accounts, load_account, research_summary
from touchgrass_hl.db.schema import (
    AuditEventRow,
    CandidateRow,
    JevReviewRow,
    LaneDecisionRow,
    MarketRow,
    ScoreRow,
    WalletFillRow,
    WalletRow,
)
from touchgrass_hl.db.session import (
    init_db,
    integrity_ok,
    journal_mode,
    make_engine,
    session_factory,
    session_scope,
)
from touchgrass_hl.hyperliquid_client import HyperliquidREST
from touchgrass_hl.jev import JevClient
from touchgrass_hl.logging_setup import setup_logging
from touchgrass_hl.market_context import compact_jev_state
from touchgrass_hl.market_registry import MarketRegistry
from touchgrass_hl.paper import LANES, unrealized
from touchgrass_hl.rate_limit import RateLimiter
from touchgrass_hl.service import run_service
from touchgrass_hl.testnet_executor import TestnetExecutor, TestnetNotConfigured, smoke_order
from touchgrass_hl.util import D, loads, utc_now_ms


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="touchgrass-hl", description="Deterministic Hyperliquid research bot")
    parser.add_argument("--version", action="version", version=f"touchgrass-hl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the SQLite database")
    sub.add_parser("doctor", help="Check configuration, database, Hyperliquid, and optional Jev")
    sub.add_parser("discover-markets", help="List current core and HIP-3 perp markets")

    run_p = sub.add_parser("run", help="Run the research service")
    run_p.add_argument("--duration", type=float, default=None, help="Stop after N seconds")

    sub.add_parser("wallets", help="Show ranked wallet candidates")
    wallet_p = sub.add_parser("wallet", help="Show one wallet")
    wallet_p.add_argument("address")

    sub.add_parser("candidates", help="Show recent convergence candidates")
    sub.add_parser("paper-report", help="Show paper equity for both lanes")

    jev_p = sub.add_parser("jev-review", help="Run Jev on one candidate without overwriting prior reviews")
    jev_p.add_argument("candidate_id")

    sub.add_parser("testnet-smoke", help="Place, query, and cancel one tiny testnet order")

    args = parser.parse_args(argv)
    try:
        settings = Settings()
    except Exception as exc:
        print(f"[FAIL] configuration: {exc}")
        return 1
    handlers = {
        "init-db": cmd_init_db,
        "doctor": cmd_doctor,
        "discover-markets": cmd_discover,
        "run": cmd_run,
        "wallets": cmd_wallets,
        "wallet": cmd_wallet,
        "candidates": cmd_candidates,
        "paper-report": cmd_paper,
        "jev-review": cmd_jev,
        "testnet-smoke": cmd_testnet,
    }
    return handlers[args.command](settings, args)


def _engine(settings: Settings):
    engine = make_engine(settings.database_url)
    init_db(engine)
    return engine


def cmd_init_db(settings: Settings, _args) -> int:
    engine = _engine(settings)
    ok, detail = integrity_ok(engine)
    factory = session_factory(engine)
    with session_scope(factory) as session:
        ensure_paper_accounts(session, settings.paper_starting_equity)
    print(f"database={settings.database_url}")
    print(f"journal_mode={journal_mode(engine)}")
    print(detail)
    engine.dispose()
    return 0 if ok else 1


def cmd_doctor(settings: Settings, _args) -> int:
    checks: list[tuple[str, str, str]] = []
    checks.append(("configuration", "PASS", f"execution_mode={settings.execution_mode} jev_enabled={settings.jev_enabled}"))
    try:
        engine = _engine(settings)
        ok, detail = integrity_ok(engine)
        mode = journal_mode(engine)
        checks.append(("database", "PASS" if ok and mode.lower() == "wal" else "FAIL", f"{detail}"))
        factory = session_factory(engine)
        with session_scope(factory) as session:
            ensure_paper_accounts(session, settings.paper_starting_equity)
            summary = research_summary(session)
        engine.dispose()
    except Exception as exc:
        checks.append(("database", "FAIL", str(exc)[:300]))
        summary = None
    try:
        count = asyncio.run(_doctor_markets(settings))
        checks.append(("hyperliquid_mainnet_info", "PASS", f"markets={count}"))
    except Exception as exc:
        checks.append(("hyperliquid_mainnet_info", "FAIL", str(exc)[:300]))
    try:
        detail = asyncio.run(_doctor_ws(settings.hl_ws_url))
        checks.append(("hyperliquid_mainnet_ws", "PASS", detail))
    except Exception as exc:
        checks.append(("hyperliquid_mainnet_ws", "FAIL", str(exc)[:300]))
    if not settings.jev_api_key:
        checks.append(("jev", "NOT RUN", "JEV_API_KEY unset"))
    else:
        try:
            body = asyncio.run(_doctor_jev(settings))
            names = []
            if isinstance(body, dict):
                models = body.get("body", body).get("models") if isinstance(body.get("body", body), dict) else None
                if isinstance(models, list):
                    names = [str(item.get("name")) for item in models if isinstance(item, dict)]
            checks.append(("jev", "PASS", "models=" + ",".join(names[:8])))
        except Exception as exc:
            checks.append(("jev", "FAIL", type(exc).__name__))
    if not settings.testnet_agent_private_key:
        checks.append(("testnet", "NOT RUN", "TESTNET_AGENT_PRIVATE_KEY unset"))
    else:
        try:
            asyncio.run(_doctor_testnet_info(settings))
            checks.append(("testnet_info", "PASS", settings.testnet_api_url))
        except Exception as exc:
            checks.append(("testnet_info", "FAIL", type(exc).__name__))
    failed = False
    for name, status, detail in checks:
        print(f"[{status}] {name}: {detail}")
        if status == "FAIL":
            failed = True
    if summary is not None:
        print("research=" + json.dumps(summary, sort_keys=True))
    return 1 if failed else 0


async def _doctor_markets(settings: Settings) -> int:
    limiter = RateLimiter(settings.info_weight_per_minute, settings.info_window_seconds)
    client = HyperliquidREST(settings.hl_info_url, limiter, settings.http_timeout_s)
    registry = MarketRegistry(client)
    payload = await registry.fetch()
    return sum(len(rows) for _, rows in payload["collected"])


async def _doctor_ws(url: str) -> str:
    import websockets

    async with websockets.connect(url, ping_interval=None, open_timeout=20) as ws:
        await ws.send(json.dumps({"method": "ping"}))
        raw = await asyncio.wait_for(ws.recv(), timeout=20)
        message = json.loads(raw)
        channel = message.get("channel")
        if channel != "pong":
            raise RuntimeError(f"expected pong, got {channel}")
        return "pong"


async def _doctor_jev(settings: Settings) -> dict:
    client = JevClient(
        enabled=True,
        api_key=settings.jev_api_key,
        model=settings.jev_model,
        base_url=settings.jev_base_url,
        timeout_s=settings.jev_timeout_s,
    )
    return await client.list_models()


async def _doctor_testnet_info(settings: Settings) -> None:
    from touchgrass_hl.testnet_executor import assert_testnet_url

    url = assert_testnet_url(settings.testnet_api_url)
    limiter = RateLimiter(100, 60)
    client = HyperliquidREST(url, limiter, settings.http_timeout_s)
    payload = await client.perp_dexs()
    if not isinstance(payload, list):
        raise RuntimeError("testnet perpDexs failed")


def cmd_discover(settings: Settings, _args) -> int:
    engine = _engine(settings)
    factory = session_factory(engine)

    async def _go() -> int:
        limiter = RateLimiter(settings.info_weight_per_minute, settings.info_window_seconds)
        client = HyperliquidREST(settings.hl_info_url, limiter, settings.http_timeout_s)
        registry = MarketRegistry(client)
        payload = await registry.fetch()
        with session_scope(factory) as session:
            return registry.apply(session, payload)

    count = asyncio.run(_go())
    with session_scope(factory) as session:
        rows = session.scalars(select(MarketRow).order_by(MarketRow.dex, MarketRow.coin)).all()
        print(f"{'dex':<12} {'coin':<24} {'asset':>8} {'status':<10} {'mark':>14} {'volume':>16} market_id")
        for row in rows:
            if row.market_id.startswith("dexbaseline:"):
                continue
            print(
                f"{row.dex:<12} {row.coin:<24} {str(row.asset_id or ''):>8} {row.status:<10} "
                f"{str(row.mark_px or ''):>14} {str(row.day_ntl_vlm or ''):>16} {row.market_id}"
            )
    print(f"markets={count}")
    with session_scope(factory) as session:
        ensure_paper_accounts(session, settings.paper_starting_equity)
        print("research=" + json.dumps(research_summary(session), sort_keys=True))
    engine.dispose()
    return 0


def cmd_run(settings: Settings, args) -> int:
    stats = asyncio.run(run_service(settings, args.duration))
    print(json.dumps(stats, sort_keys=True))
    return 0


def cmd_wallets(settings: Settings, _args) -> int:
    engine = _engine(settings)
    factory = session_factory(engine)
    with session_scope(factory) as session:
        latest = (
            select(ScoreRow.address, func.max(ScoreRow.id).label("id")).group_by(ScoreRow.address).subquery()
        )
        rows = session.execute(
            select(ScoreRow, WalletRow)
            .join(latest, ScoreRow.id == latest.c.id)
            .join(WalletRow, WalletRow.address == ScoreRow.address)
            .order_by(ScoreRow.score.desc())
        ).all()
        if not rows:
            observed = count_rows(session, WalletRow)
            print(f"no scored wallets yet. observed_addresses={observed}")
            engine.dispose()
            return 0
        ranked = sorted(rows, key=lambda pair: D(pair[0].score), reverse=True)
        print(f"{'score':>8} {'verified':<9} {'trades':>8} {'notional':>14} address")
        for score, wallet in ranked:
            print(
                f"{score.score:>8} {str(score.verified):<9} {wallet.trade_count:>8} {wallet.notional:>14} {wallet.address}"
            )
    engine.dispose()
    return 0


def cmd_wallet(settings: Settings, args) -> int:
    address = args.address.strip().lower()
    engine = _engine(settings)
    factory = session_factory(engine)
    with session_scope(factory) as session:
        wallet = session.get(WalletRow, address)
        if wallet is None:
            print(f"address not observed: {address}")
            engine.dispose()
            return 1
        fills = count_rows_where(session, address)
        score = session.scalar(select(ScoreRow).where(ScoreRow.address == address).order_by(ScoreRow.id.desc()).limit(1))
        print(f"address={wallet.address}")
        print(f"trade_count={wallet.trade_count} notional={wallet.notional}")
        print(f"first_seen_ms={wallet.first_seen_ms} last_seen_ms={wallet.last_seen_ms}")
        print(f"trades_per_hour={wallet.trades_per_hour}")
        print(f"markets={wallet.markets_json}")
        print(f"hydration={wallet.hydration_status} completeness={wallet.completeness_label}")
        print(f"history_limited={wallet.history_limited} lifetime_complete={wallet.lifetime_complete}")
        print(f"stored_fills={fills} verified={wallet.verified}")
        if score is None:
            print("score=none")
        else:
            print(f"score={score.score} verified_at_score={score.verified}")
            print(score.breakdown_json)
    engine.dispose()
    return 0


def count_rows_where(session, address: str) -> int:
    return int(session.scalar(select(func.count()).select_from(WalletFillRow).where(WalletFillRow.address == address)) or 0)


def cmd_candidates(settings: Settings, _args) -> int:
    engine = _engine(settings)
    factory = session_factory(engine)
    with session_scope(factory) as session:
        rows = session.scalars(select(CandidateRow).order_by(CandidateRow.trigger_ms.desc()).limit(50)).all()
        if not rows:
            print("no candidates")
            engine.dispose()
            return 0
        for row in rows:
            lanes = session.scalars(select(LaneDecisionRow).where(LaneDecisionRow.candidate_id == row.candidate_id)).all()
            lane_txt = ", ".join(f"{item.lane}:{'ACCEPT' if item.accepted else item.reason}" for item in lanes) or "no lane decision"
            print(
                f"{row.candidate_id} {row.market_id} {row.direction} trigger_ms={row.trigger_ms} {lane_txt}"
            )
    engine.dispose()
    return 0


def cmd_paper(settings: Settings, _args) -> int:
    engine = _engine(settings)
    factory = session_factory(engine)
    with session_scope(factory) as session:
        ensure_paper_accounts(session, settings.paper_starting_equity)
        marks = {row.market_id: D(row.mark_px) for row in session.scalars(select(MarketRow)).all() if row.mark_px}
        for lane in LANES:
            account = load_account(session, lane)
            unreal = Decimal(0)
            for pos in account.open_positions():
                mark = marks.get(pos.market_id, pos.entry_px)
                unreal += unrealized(pos, mark)
            equity = account.cash + sum((p.entry_notional for p in account.open_positions()), Decimal(0)) + unreal
            peak = account.peak_equity
            dd = (peak - equity) / peak if peak > 0 else Decimal(0)
            closed = len([p for p in (account.positions or []) if p.status == "closed"])
            print(f"lane={lane}")
            print(f"  starting_equity={account.starting_equity}")
            print(f"  current_equity={equity}")
            print(f"  realized_pnl={account.realized_pnl}")
            print(f"  unrealized_pnl={unreal}")
            print(f"  fees={account.fees}")
            print(f"  funding={account.funding}")
            print(f"  wins={account.wins} losses={account.losses} closed_positions={closed} open={len(account.open_positions())}")
            print(f"  drawdown={dd}")
            print(f"  day_realized={account.day_realized}")
    engine.dispose()
    return 0


def cmd_jev(settings: Settings, args) -> int:
    if not settings.jev_enabled or not settings.jev_api_key:
        print("[NOT RUN] jev-review requires JEV_ENABLED=true and JEV_API_KEY")
        return 3
    engine = _engine(settings)
    factory = session_factory(engine)
    with session_scope(factory) as session:
        row = session.get(CandidateRow, args.candidate_id)
        if row is None:
            print(f"candidate not found: {args.candidate_id}")
            engine.dispose()
            return 1
        packet = loads(row.packet_json) or {}
        prior = int(
            session.scalar(
                select(func.count()).select_from(JevReviewRow).where(JevReviewRow.candidate_id == row.candidate_id)
            )
            or 0
        )
    state = compact_jev_state(packet)

    async def _call():
        client = JevClient(
            enabled=True,
            api_key=settings.jev_api_key,
            model=settings.jev_model,
            base_url=settings.jev_base_url,
            timeout_s=settings.jev_timeout_s,
        )
        return await client.evaluate(state)

    result = asyncio.run(_call())
    from touchgrass_hl.db.repo import insert_jev
    from touchgrass_hl.jev import QUESTIONS
    from touchgrass_hl.util import canon_hash

    with session_scope(factory) as session:
        review_id = insert_jev(session, args.candidate_id, state, canon_hash(state), QUESTIONS, result)
        session.add(
            AuditEventRow(
                kind="JEV_MANUAL_REVIEW",
                candidate_id=args.candidate_id,
                created_ms=utc_now_ms(),
                payload_json=json.dumps(
                    {"review_id": review_id, "status": result.status, "model": result.model_returned},
                    sort_keys=True,
                ),
            )
        )
    print(f"review_id={review_id} prior_reviews={prior} status={result.status} model={result.model_returned}")
    print(json.dumps(result.answers, sort_keys=True))
    if result.error:
        print(f"error={result.error}")
    engine.dispose()
    return 0 if result.status == "ok" else 1


def cmd_testnet(settings: Settings, _args) -> int:
    if settings.execution_mode != "testnet":
        print("[NOT RUN] testnet-smoke requires EXECUTION_MODE=testnet")
        return 3
    if not settings.testnet_agent_private_key:
        print("[NOT RUN] TESTNET_AGENT_PRIVATE_KEY unset")
        return 3
    if settings.trading_kill_switch:
        print("[FAIL] trading kill switch is active")
        return 1
    setup_logging(settings.log_dir, settings.log_level, settings.log_max_bytes, settings.log_backup_count)
    try:
        executor = TestnetExecutor(
            private_key=settings.testnet_agent_private_key,
            account_address=settings.testnet_account_address,
            base_url=settings.testnet_api_url,
            kill_switch=settings.trading_kill_switch,
        )
        result = smoke_order(executor)
    except TestnetNotConfigured as exc:
        print(f"[NOT RUN] {exc}")
        return 3
    except Exception as exc:
        print(f"[FAIL] testnet-smoke: {type(exc).__name__}: {exc}")
        return 1
    engine = _engine(settings)
    factory = session_factory(engine)
    from touchgrass_hl.db.repo import record_testnet_order

    with session_scope(factory) as session:
        record_testnet_order(
            session,
            {
                "purpose": "smoke",
                "coin": result.get("coin"),
                "is_buy": True,
                "size": result.get("size"),
                "limit_px": result.get("limit_px"),
                "reduce_only": False,
                "oid": result.get("oid"),
                "status": "cancelled" if result.get("cancel") else ("closed" if result.get("close") else "unknown"),
                "raw": {"place": result.get("place"), "cancel": result.get("cancel"), "query": result.get("query"), "close": result.get("close")},
            },
        )
    print(json.dumps({"coin": result["coin"], "oid": result["oid"], "size": result["size"], "limit_px": result["limit_px"], "base_url": result["base_url"]}, sort_keys=True))
    engine.dispose()
    if result.get("oid") is None:
        print("[FAIL] order was not acknowledged with an oid")
        return 1
    print("[PASS] testnet smoke order placed and cancel/close attempted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
