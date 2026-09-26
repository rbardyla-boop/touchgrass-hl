"""Behavioral groups. Grouping is not a claim of common ownership."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from touchgrass_hl.util import median, short_hash


@dataclass(frozen=True)
class BehaviorEvent:
    wallet: str
    market_id: str
    direction: str
    time_ms: int
    size: Decimal


@dataclass(frozen=True)
class IndependenceConfig:
    jaccard_min: Decimal = Decimal("0.75")
    min_simultaneous: int = 4
    proximity_ms: int = 3000
    min_events: int = 5
    max_wallets: int = 400


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


def _jaccard(a: set[tuple[str, str]], b: set[tuple[str, str]]) -> Decimal:
    if not a and not b:
        return Decimal(0)
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return Decimal(0)
    return Decimal(inter) / Decimal(union)


def _pair_stats(
    a_events: list[BehaviorEvent],
    b_events: list[BehaviorEvent],
    proximity_ms: int,
) -> dict[str, Any]:
    a_sorted = sorted(a_events, key=lambda ev: ev.time_ms)
    b_sorted = sorted(b_events, key=lambda ev: ev.time_ms)
    i = 0
    j = 0
    simultaneous = 0
    a_leads = 0
    b_leads = 0
    ratios: list[Decimal] = []
    while i < len(a_sorted) and j < len(b_sorted):
        ea, eb = a_sorted[i], b_sorted[j]
        key_a = (ea.market_id, ea.direction)
        key_b = (eb.market_id, eb.direction)
        if key_a == key_b and abs(ea.time_ms - eb.time_ms) <= proximity_ms:
            simultaneous += 1
            if ea.time_ms < eb.time_ms:
                a_leads += 1
            elif eb.time_ms < ea.time_ms:
                b_leads += 1
            if ea.size > 0 and eb.size > 0:
                hi = ea.size if ea.size > eb.size else eb.size
                lo = eb.size if ea.size > eb.size else ea.size
                ratios.append(hi / lo)
            i += 1
            j += 1
        elif ea.time_ms < eb.time_ms or (
            ea.time_ms == eb.time_ms and (ea.market_id, ea.direction) < (eb.market_id, eb.direction)
        ):
            i += 1
        else:
            j += 1
    return {
        "simultaneous": simultaneous,
        "a_leads": a_leads,
        "b_leads": b_leads,
        "median_size_ratio": None if not ratios else format(median(ratios), "f"),
    }


def build_groups(
    events: list[BehaviorEvent],
    cfg: IndependenceConfig,
) -> dict[str, Any]:
    """Union wallets with high market/direction overlap AND repeated near-simultaneous entries.

    The result is a behavioral group, never an ownership claim.
    """
    by_wallet: dict[str, list[BehaviorEvent]] = {}
    for event in events:
        if event.direction not in {"LONG", "SHORT"}:
            continue
        by_wallet.setdefault(event.wallet, []).append(event)
    ranked = sorted(by_wallet, key=lambda w: (-len(by_wallet[w]), w))
    truncated = len(ranked) > cfg.max_wallets
    considered = ranked[: cfg.max_wallets]
    eligible = [w for w in considered if len(by_wallet[w]) >= cfg.min_events]
    sets = {
        w: {(ev.market_id, ev.direction) for ev in by_wallet[w]}
        for w in eligible
    }
    uf = _UnionFind()
    for wallet in by_wallet:
        uf.add(wallet)
    edges = []
    for i, wa in enumerate(eligible):
        for wb in eligible[i + 1 :]:
            jacc = _jaccard(sets[wa], sets[wb])
            stats = _pair_stats(by_wallet[wa], by_wallet[wb], cfg.proximity_ms)
            if jacc >= cfg.jaccard_min and stats["simultaneous"] >= cfg.min_simultaneous:
                uf.union(wa, wb)
                edges.append(
                    {
                        "a": wa,
                        "b": wb,
                        "jaccard": format(jacc, "f"),
                        "simultaneous": stats["simultaneous"],
                        "a_leads": stats["a_leads"],
                        "b_leads": stats["b_leads"],
                        "median_size_ratio": stats["median_size_ratio"],
                        "rule": "jaccard_and_repeated_near_simultaneous_entries",
                        "relationship_type": "behavioral_group",
                        "ownership_claim": False,
                    }
                )
    members: dict[str, list[str]] = {}
    root_of = {}
    for wallet in by_wallet:
        root = uf.find(wallet)
        root_of[wallet] = root
        members.setdefault(root, []).append(wallet)
    group_of: dict[str, str] = {}
    records = []
    for root, wallets in members.items():
        ordered = sorted(wallets)
        gid = "bg_" + short_hash("|".join(ordered), 16)
        related = [edge for edge in edges if edge["a"] in ordered and edge["b"] in ordered]
        for wallet in ordered:
            group_of[wallet] = gid
        records.append(
            {
                "group_id": gid,
                "members": ordered,
                "size": len(ordered),
                "relationship_type": "behavioral_group",
                "ownership_claim": False,
                "edges": related,
                "why": (
                    "merged on Jaccard overlap of (market, direction) plus repeated "
                    "near-simultaneous same-direction entries"
                    if len(ordered) > 1
                    else "singleton; no wallet met the overlap rule with this address"
                ),
            }
        )
    return {
        "group_of": group_of,
        "groups": records,
        "edges": edges,
        "truncated": truncated,
        "wallets_considered": len(considered),
        "relationship_type": "behavioral_group",
        "ownership_claim": False,
        "thresholds": {
            "jaccard_min": format(cfg.jaccard_min, "f"),
            "min_simultaneous": cfg.min_simultaneous,
            "proximity_ms": cfg.proximity_ms,
            "min_events": cfg.min_events,
            "max_wallets": cfg.max_wallets,
        },
    }
