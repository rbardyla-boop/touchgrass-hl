"""Append-only audit events. Decisions are never rewritten."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from touchgrass_hl.db.schema import AuditEventRow
from touchgrass_hl.util import dumps, utc_now_ms


def append_audit(
    session: Session,
    *,
    kind: str,
    payload: dict[str, Any],
    candidate_id: str | None = None,
    lane: str | None = None,
    created_ms: int | None = None,
) -> int:
    row = AuditEventRow(
        kind=kind,
        candidate_id=candidate_id,
        lane=lane,
        created_ms=created_ms if created_ms is not None else utc_now_ms(),
        payload_json=dumps(payload),
    )
    session.add(row)
    session.flush()
    return int(row.id)
