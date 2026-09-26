"""Deterministic wallet metrics, copyability summary, and percentile score."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from touchgrass_hl.models import Episode, FillRecord, Metrics, ScoreBreakdown
from touchgrass_hl.util import D, iso_week, median, percentile_rank, utc_day


def summarize_copyability(adverse_moves: list[Decimal | None]) -> Decimal | None:
    """Mean adverse price move. None if every observation is unknown.

    Positive means the follower price was worse than the leader price.
    """
    known = [D(item) for item in adverse_moves if item is not None]
    if not known:
        return None
    return sum(known, Decimal(0)) / Decimal(len(known))


def copyability_observations(
    *,
    leader_px: Decimal,
    direction: str,
    fill_time_ms: int,
    later_trades: list[tuple[int, Decimal]],
    delays_s: list[int],
    exit_px: Decimal | None,
) -> list[dict[str, Any]]:
    """Estimate follower prices from recorded prints. Depth is not invented.

    later_trades is (time_ms, price) at or after the leader fill, ascending.
    If no print exists at-or-after the delay, that delay is unknown.
    """
    ordered = sorted(later_trades, key=lambda item: item[0])
    out = []
    for delay in delays_s:
        target = fill_time_ms + int(delay) * 1000
        follower = next((item for item in ordered if item[0] >= target), None)
        if follower is None or leader_px <= 0:
            out.append(
                {
                    "delay_s": delay,
                    "status": "unknown",
                    "leader_px": format(leader_px, "f"),
                    "follower_px": None,
                    "adverse_move": None,
                    "available_depth": None,
                    "simulated_slippage": None,
                    "subsequent_result": None,
                    "reason": "no_market_print_at_delay",
                }
            )
            continue
        follower_px = follower[1]
        raw = (follower_px - leader_px) / leader_px
        adverse = raw if direction == "LONG" else -raw
        subsequent = None
        if exit_px is not None and follower_px > 0:
            if direction == "LONG":
                subsequent = (exit_px - follower_px) / follower_px
            else:
                subsequent = (follower_px - exit_px) / follower_px
        out.append(
            {
                "delay_s": delay,
                "status": "known",
                "leader_px": format(leader_px, "f"),
                "follower_px": format(follower_px, "f"),
                "follower_time_ms": follower[0],
                "adverse_move": format(adverse, "f"),
                "available_depth": None,
                "simulated_slippage": None,
                "subsequent_result": None if subsequent is None else format(subsequent, "f"),
                "reason": "trade_print_only_depth_unknown",
            }
        )
    return out


def _herfindahl(notionals: list[Decimal]) -> Decimal:
    total = sum(notionals, Decimal(0))
    if total <= 0:
        return Decimal(0)
    return sum((n / total) ** 2 for n in notionals)


def _return_drawdown(closed: list[Episode]) -> Decimal:
    """Peak-to-trough drawdown of a normalized equity curve that starts at 1.

    Each closed episode multiplies equity by (1 + closed_pnl / entry_notional).
    The starting basis is 1, so the denominator is never zero. Episodes with no
    entry notional are skipped rather than invented.
    """
    ordered = sorted(closed, key=lambda ep: (ep.exit_time_ms or 0, ep.entry_time_ms))
    equity = Decimal(1)
    peak = Decimal(1)
    max_dd = Decimal(0)
    for ep in ordered:
        if ep.entry_notional <= 0:
            continue
        equity = equity * (Decimal(1) + ep.closed_pnl / ep.entry_notional)
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _account_value_drawdown(values: list[Decimal]) -> Decimal | None:
    if len(values) < 2 or values[0] <= 0:
        return None
    peak = values[0]
    max_dd = Decimal(0)
    for value in values:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _normalized_return(
    closed: list[Episode],
    account_values: list[Decimal] | None,
    account_pnl: list[Decimal] | None,
) -> Decimal:
    """Size-normalized performance. Raw USD PnL is not this number.

    Prefer Hyperliquid account history when it is usable: the change in
    reported pnl divided by the average account value, which requires at least
    two strictly positive account-value points and two pnl points. Otherwise
    use sum(episode closed_pnl) / sum(episode entry_notional) over closed
    episodes that have a positive entry notional. Equal percentage results
    therefore score the same regardless of account size.
    """
    if account_values and account_pnl and len(account_values) >= 2 and len(account_pnl) >= 2:
        if all(value > 0 for value in account_values):
            average = sum(account_values, Decimal(0)) / Decimal(len(account_values))
            if average > 0:
                return (account_pnl[-1] - account_pnl[0]) / average
    numerator = Decimal(0)
    denominator = Decimal(0)
    for ep in closed:
        if ep.entry_notional <= 0:
            continue
        numerator += ep.closed_pnl
        denominator += ep.entry_notional
    if denominator <= 0:
        return Decimal(0)
    return numerator / denominator


def _p90(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = int((len(ordered) - 1) * Decimal("0.9"))
    return ordered[idx]


def compute_metrics(
    episodes: list[Episode],
    fills: list[FillRecord],
    *,
    copyability: Decimal | None,
    completeness_label: str,
    requested_window_fully_returned: bool,
    copyability_observations: int = 0,
    unmatched_closes: int = 0,
    account_values: list[Decimal] | None = None,
    account_pnl: list[Decimal] | None = None,
) -> Metrics:
    closed = [ep for ep in episodes if ep.closed]
    pnls = [ep.closed_pnl for ep in closed]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    breakeven = sum(1 for p in pnls if p == 0)
    gross_win = sum((p for p in pnls if p > 0), Decimal(0))
    gross_loss = -sum((p for p in pnls if p < 0), Decimal(0))
    capped = False
    if gross_loss == 0:
        profit_factor = Decimal("999") if gross_win > 0 else Decimal(0)
        capped = gross_win > 0
    else:
        profit_factor = gross_win / gross_loss
    returns = []
    holds = []
    for ep in closed:
        if ep.entry_notional > 0:
            returns.append(ep.closed_pnl / ep.entry_notional)
        if ep.exit_time_ms is not None:
            holds.append(Decimal(max(0, ep.exit_time_ms - ep.entry_time_ms)) / Decimal(1000))
    equity_usd = Decimal(0)
    peak_usd = Decimal(0)
    max_dd_usd = Decimal(0)
    for pnl in pnls:
        equity_usd += pnl
        if equity_usd > peak_usd:
            peak_usd = equity_usd
        dd_usd = peak_usd - equity_usd
        if dd_usd > max_dd_usd:
            max_dd_usd = dd_usd
    account_dd = _account_value_drawdown(list(account_values or []))
    max_dd = account_dd if account_dd is not None else _return_drawdown(closed)
    day_pnl: dict[str, Decimal] = {}
    week_pnl: dict[str, Decimal] = {}
    active_days: set[str] = set()
    for fill in fills:
        active_days.add(utc_day(fill.time_ms))
        if fill.closed_pnl != 0:
            day = utc_day(fill.time_ms)
            week = iso_week(fill.time_ms)
            day_pnl[day] = day_pnl.get(day, Decimal(0)) + fill.closed_pnl
            week_pnl[week] = week_pnl.get(week, Decimal(0)) + fill.closed_pnl
    profitable_days = sum(1 for value in day_pnl.values() if value > 0)
    profitable_weeks = sum(1 for value in week_pnl.values() if value > 0)
    active_day_count = len(active_days)
    consistency = (
        Decimal(profitable_days) / Decimal(active_day_count) if active_day_count else Decimal(0)
    )
    best = max(pnls) if pnls else Decimal(0)
    worst = min(pnls) if pnls else Decimal(0)
    concentration = (best / gross_win) if gross_win > 0 and best > 0 else Decimal(0)
    markets = {ep.market_id for ep in closed} or {f.market_id for f in fills}
    long_eps = [ep for ep in closed if ep.direction == "LONG"]
    short_eps = [ep for ep in closed if ep.direction == "SHORT"]

    def _wr(group: list[Episode]) -> Decimal:
        if not group:
            return Decimal(0)
        return Decimal(sum(1 for ep in group if ep.closed_pnl > 0)) / Decimal(len(group))

    maker = 0
    taker = 0
    known_liq = 0
    for fill in fills:
        if fill.crossed is True:
            taker += 1
            known_liq += 1
        elif fill.crossed is False:
            maker += 1
            known_liq += 1
    maker_ratio = Decimal(maker) / Decimal(known_liq) if known_liq else Decimal(0)
    taker_ratio = Decimal(taker) / Decimal(known_liq) if known_liq else Decimal(0)
    notionals = [ep.entry_notional for ep in closed]
    sample = Decimal(len(closed)) / Decimal(30) if len(closed) < 30 else Decimal(1)
    window_factor = Decimal(1) if requested_window_fully_returned else Decimal("0.5")
    completeness = sample * window_factor
    if completeness > 1:
        completeness = Decimal(1)
    hl_sum = sum((f.closed_pnl for f in fills), Decimal(0))
    realized = sum(pnls, Decimal(0))
    reconstructed = sum((ep.closed_pnl for ep in episodes), Decimal(0))
    reconciled = unmatched_closes == 0 and abs(reconstructed - hl_sum) <= Decimal("0.00000001")
    normalized = _normalized_return(closed, account_values, account_pnl)
    return Metrics(
        realized_pnl=realized,
        hyperliquid_closed_pnl=hl_sum,
        closed_trades=len(closed),
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        win_rate=(Decimal(wins) / Decimal(len(closed))) if closed else Decimal(0),
        profit_factor=profit_factor,
        profit_factor_capped=capped,
        average_return=(sum(returns, Decimal(0)) / Decimal(len(returns))) if returns else Decimal(0),
        median_return=median(returns),
        average_holding_seconds=(sum(holds, Decimal(0)) / Decimal(len(holds))) if holds else Decimal(0),
        median_holding_seconds=median(holds),
        max_drawdown=max_dd,
        max_drawdown_usd=max_dd_usd,
        active_days=active_day_count,
        profitable_days=profitable_days,
        profitable_weeks=profitable_weeks,
        active_weeks=len(week_pnl),
        consistency=consistency,
        best_trade=best,
        worst_trade=worst,
        profit_concentration=concentration,
        markets_traded=len(markets),
        herfindahl=_herfindahl(notionals),
        long_pnl=sum((ep.closed_pnl for ep in long_eps), Decimal(0)),
        short_pnl=sum((ep.closed_pnl for ep in short_eps), Decimal(0)),
        long_win_rate=_wr(long_eps),
        short_win_rate=_wr(short_eps),
        maker_ratio=maker_ratio,
        taker_ratio=taker_ratio,
        average_position_notional=(sum(notionals, Decimal(0)) / Decimal(len(notionals)))
        if notionals
        else Decimal(0),
        size_p50=median(notionals),
        size_p90=_p90(notionals),
        data_completeness=completeness,
        lifetime_complete=False,
        completeness_label=completeness_label,
        copyability=copyability,
        copyability_known=copyability is not None,
        copyability_observations=int(copyability_observations),
        unmatched_closes=int(unmatched_closes),
        normalized_return=normalized,
        reconstructed_net_pnl=reconstructed,
        pnl_reconciled=reconciled,
        data_quality_degraded=not reconciled,
    )


def metrics_to_dict(metrics: Metrics) -> dict[str, Any]:
    raw = asdict(metrics)
    out = {}
    for key, value in raw.items():
        if isinstance(value, Decimal):
            out[key] = format(value, "f")
        else:
            out[key] = value
    return out


def _component_value(metrics: Metrics, name: str) -> Decimal | None:
    if name == "profitability":
        return metrics.normalized_return
    if name == "profit_factor":
        return metrics.profit_factor
    if name == "consistency":
        return metrics.consistency
    if name == "copyability":
        if not metrics.copyability_known or metrics.copyability is None:
            return None
        return -metrics.copyability
    if name == "win_rate":
        return metrics.win_rate
    if name == "inverse_drawdown":
        return -metrics.max_drawdown
    if name == "inverse_concentration":
        return -metrics.profit_concentration
    if name == "data_quality":
        return metrics.data_completeness
    raise KeyError(name)


def verify_wallet(
    metrics: Metrics,
    *,
    min_closed_trades: int,
    min_active_days: int,
    max_profit_concentration: Decimal,
    allow_concentration_override: bool,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if metrics.closed_trades < min_closed_trades:
        reasons.append("MIN_CLOSED_TRADES")
    if metrics.active_days < min_active_days:
        reasons.append("MIN_ACTIVE_DAYS")
    if metrics.realized_pnl <= 0:
        reasons.append("NON_POSITIVE_PNL")
    if metrics.profit_factor <= 1:
        reasons.append("PROFIT_FACTOR")
    if metrics.profit_concentration > max_profit_concentration and not allow_concentration_override:
        reasons.append("PROFIT_CONCENTRATION")
    if metrics.lifetime_complete:
        reasons.append("COMPLETENESS_MISLABELLED")
    if not metrics.pnl_reconciled:
        reasons.append("PNL_NOT_RECONCILED")
    return (len(reasons) == 0), reasons


def score_population(
    rows: list[tuple[str, Metrics]],
    weights: dict[str, Decimal],
    *,
    min_closed_trades: int,
    min_active_days: int,
    max_profit_concentration: Decimal,
    allow_concentration_override: bool,
    min_copyability_observations: int = 10,
) -> dict[str, ScoreBreakdown]:
    populations: dict[str, list[Decimal]] = {name: [] for name in weights}
    values: dict[str, dict[str, Decimal | None]] = {}
    for address, metrics in rows:
        values[address] = {}
        for name in weights:
            value = _component_value(metrics, name)
            values[address][name] = value
            if value is not None:
                populations[name].append(value)
    out: dict[str, ScoreBreakdown] = {}
    for address, metrics in rows:
        components: dict[str, Any] = {}
        omitted: list[str] = []
        used_weight = Decimal(0)
        weighted = Decimal(0)
        for name, weight in weights.items():
            value = values[address][name]
            if value is None:
                omitted.append(name)
                components[name] = {
                    "value": None,
                    "percentile": None,
                    "weight": format(weight, "f"),
                    "weighted": None,
                }
                continue
            pct = percentile_rank(value, populations[name])
            used_weight += weight
            weighted += pct * weight
            components[name] = {
                "value": format(value, "f"),
                "percentile": format(pct, "f"),
                "weight": format(weight, "f"),
                "weighted": format(pct * weight, "f"),
            }
        score = (weighted / used_weight) if used_weight > 0 else Decimal(0)
        performance, reasons = verify_wallet(
            metrics,
            min_closed_trades=min_closed_trades,
            min_active_days=min_active_days,
            max_profit_concentration=max_profit_concentration,
            allow_concentration_override=allow_concentration_override,
        )
        copy_reasons = list(reasons)
        enough_copy = (
            metrics.copyability_known
            and metrics.copyability_observations >= min_copyability_observations
        )
        if not enough_copy:
            copy_reasons.append("MIN_COPYABILITY_OBSERVATIONS")
        copy_ok = performance and enough_copy
        if copy_ok:
            stage = "copy"
        elif performance:
            stage = "performance"
        else:
            stage = "none"
        out[address] = ScoreBreakdown(
            score=score,
            components=components,
            omitted=omitted,
            verified=performance,
            verification_reasons=reasons,
            performance_verified=performance,
            copy_verified=copy_ok,
            verification_stage=stage,
            copy_reasons=copy_reasons,
        )
    return out
