"""Persistent round-robin for verified-wallet position baselines.

Priority is applied first, but the cursor still advances so the same prefix
cannot occupy every batch forever.
"""

from __future__ import annotations


def plan_baseline_batch(
    addresses: list[str],
    *,
    cursor: int,
    batch: int,
    priority: list[str] | None = None,
) -> tuple[list[str], int]:
    """Return the next batch and the cursor to persist.

    ``priority`` is already ordered: live-but-stale, then newly verified, then
    oldest baseline. Remaining slots walk the full address ring from ``cursor``.
    """
    ordered = sorted(set(addresses))
    if batch <= 0 or not ordered:
        return [], 0
    chosen: list[str] = []
    for addr in priority or []:
        if addr in ordered and addr not in chosen:
            chosen.append(addr)
        if len(chosen) >= batch:
            return chosen, (int(cursor) + batch) % len(ordered)
    start = int(cursor) % len(ordered)
    stepped = 0
    while len(chosen) < batch and stepped < len(ordered):
        addr = ordered[(start + stepped) % len(ordered)]
        stepped += 1
        if addr not in chosen:
            chosen.append(addr)
    return chosen, (start + stepped) % len(ordered)
