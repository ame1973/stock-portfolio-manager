"""Canonical timestamps and append-only, secret-free audit records."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Connection, text


def now() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime | None = None) -> str:
    return (value or now()).isoformat(timespec="microseconds").replace("+00:00", "Z")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def audit(
    con: Connection,
    owner_id: str,
    entity_type: str,
    entity_id: str,
    action: str,
    *,
    request_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str = "",
) -> None:
    """Callers pass public entity values only, never passwords, cookies, or provider keys."""
    con.execute(
        text("""
        INSERT INTO audit_events
          (id,owner_id,entity_type,entity_id,action,before_sha256,after_sha256,reason,request_id,created_at)
        VALUES (:id,:owner,:type,:entity,:action,:before,:after,:reason,:request,:now)
    """),
        {
            "id": str(uuid4()),
            "owner": owner_id,
            "type": entity_type,
            "entity": entity_id,
            "action": action,
            "before": digest(canonical(before)) if before is not None else None,
            "after": digest(canonical(after)) if after is not None else None,
            "reason": reason,
            "request": request_id,
            "now": stamp(),
        },
    )
