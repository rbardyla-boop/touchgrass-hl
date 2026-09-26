"""Smart-wallet convergence. Counts behavioral groups, not raw addresses."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from touchgrass_hl.models import ActionEvent
from touchgrass_hl.util import short_hash

QUALIFYING = {
    "LONG": frozenset({"OPEN_LONG", "ADD_LONG"}),
    "SHORT": frozenset({"OPEN_SHORT", "ADD_SHORT"}),
}


@dataclass(frozen=True)
class ClusterConfig:
    window_ms: int = 120_000
    min_groups: int = 3


class ClusterDetector:
    def __init__(self, cfg: ClusterConfig) -> None:
        self.cfg = cfg
        self.windows: dict[tuple[str, str], list[ActionEvent]] = {}
        self.anchors: dict[tuple[str, str], int | None] = {}

    def reset(self) -> None:
        self.windows.clear()
        self.anchors.clear()

    def on_action(
        self,
        action: ActionEvent,
        *,
        verified: set[str],
        groups: dict[str, str],
        scores: dict[str, Decimal] | None = None,
    ) -> dict[str, Any] | None:
        allowed = QUALIFYING.get(action.direction)
        if not allowed or action.action not in allowed:
            return None
        if action.wallet not in verified:
            return None
        key = (action.market_id, action.direction)
        window = self.windows.setdefault(key, [])
        window.append(action)
        cutoff = action.time_ms - self.cfg.window_ms
        window[:] = [item for item in window if item.time_ms >= cutoff]
        earliest: dict[str, ActionEvent] = {}
        for item in window:
            if item.wallet not in verified:
                continue
            if item.action not in allowed:
                continue
            prev = earliest.get(item.wallet)
            if prev is None or item.time_ms < prev.time_ms:
                earliest[item.wallet] = item
        by_group: dict[str, ActionEvent] = {}
        group_members: dict[str, list[str]] = {}
        for wallet, item in earliest.items():
            gid = groups.get(wallet) or f"solo:{wallet}"
            group_members.setdefault(gid, []).append(wallet)
            current = by_group.get(gid)
            if current is None or item.time_ms < current.time_ms:
                by_group[gid] = item
        if len(by_group) < self.cfg.min_groups:
            self.anchors[key] = None
            return None
        anchor = min(item.time_ms for item in by_group.values())
        if self.anchors.get(key) == anchor:
            return None
        self.anchors[key] = anchor
        group_ids = sorted(by_group)
        identity = "|".join([action.market_id, action.direction, str(anchor), ",".join(group_ids)])
        candidate_id = short_hash(identity, 32)
        participants = []
        for gid in group_ids:
            for wallet in sorted(group_members[gid]):
                item = earliest[wallet]
                score = None if not scores or wallet not in scores else format(scores[wallet], "f")
                participants.append(
                    {
                        "address": wallet,
                        "group_id": gid,
                        "action": item.action,
                        "time_ms": item.time_ms,
                        "price": format(item.price, "f"),
                        "size": format(item.size, "f"),
                        "score": score,
                        "prev_position": None
                        if item.prev_position is None
                        else format(item.prev_position, "f"),
                        "new_position": None
                        if item.new_position is None
                        else format(item.new_position, "f"),
                    }
                )
        first = min(by_group.values(), key=lambda item: item.time_ms)
        trigger = action
        movement = None
        if first.price > 0:
            movement = format((trigger.price - first.price) / first.price, "f")
        return {
            "candidate_id": candidate_id,
            "market_id": action.market_id,
            "coin": action.coin,
            "dex": action.dex,
            "direction": action.direction,
            "timestamp_first_wallet_ms": first.time_ms,
            "timestamp_threshold_ms": trigger.time_ms,
            "participating_addresses": sorted({row["address"] for row in participants}),
            "behavioral_group_ids": group_ids,
            "independent_group_count": len(group_ids),
            "wallet_actions": participants,
            "price_first": format(first.price, "f"),
            "price_trigger": format(trigger.price, "f"),
            "price_movement": movement,
            "relationship_type": "behavioral_group",
            "ownership_claim": False,
        }
