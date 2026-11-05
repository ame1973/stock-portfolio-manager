"""Atomic action revisions, explicit display-only symbol effects, and review provenance."""

from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, RowMapping, text

from spm_api.action_models import CorporateAction, CorporateActionCreate
from spm_api.audit import audit, canonical, now, stamp
from spm_api.errors import ApiError, Success
from spm_api.ledger_service import allocate_sequence, validate_action_history
from spm_api.master_service import invalidate_portfolio, security_row
from spm_api.mutations import check_version
from spm_api.security import Auth

ACTION_SQL = """
    SELECT c.*,EXISTS(SELECT 1 FROM review_items r
        WHERE json_extract(r.detail_json,'$.action_root_id')=c.root_id
          AND r.status IN ('OPEN','ACKNOWLEDGED')) AS review_required
    FROM corporate_actions c WHERE 1=1
"""


def action_row(con: Connection, identifier: str) -> RowMapping:
    row = con.execute(text(ACTION_SQL + " AND c.id=:id"), {"id": identifier}).mappings().first()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Corporate action not found")
    return row


def as_action(row: RowMapping) -> CorporateAction:
    values = {key: row[key] for key in CorporateAction.model_fields if key in row}
    values["ratio_numerator"], values["ratio_denominator"] = row["ratio_num"], row["ratio_den"]
    values["is_current"] = bool(row["is_current"])
    values["review_required"] = bool(row["review_required"])
    values["lifecycle_status"] = (
        "SUPERSEDED" if not row["is_current"] else "VOID" if row["status"] == "VOID" else "CURRENT"
    )
    scope = "REVIEW_ONLY"
    if row["status"] == "VOID":
        scope = "NONE"
    elif row["status"] == "CONFIRMED":
        if row["action_type"] in {"SPLIT", "REVERSE_SPLIT"}:
            scope = "QUANTITY_TERMS"
        elif row["action_type"] == "SYMBOL_CHANGE" and row["previous_symbol"] is not None:
            scope = "DISPLAY_ONLY"
    values["effect_scope"] = scope
    return CorporateAction.model_validate(values)


def _current(row: RowMapping) -> None:
    if not row["is_current"] or row["status"] == "VOID":
        raise ApiError(409, "INACTIVE_FACT", "Use the current non-void action revision")


def _set_symbol(
    con: Connection, auth: Auth, security: RowMapping, ticker: str, reason: str, request_id: str
) -> None:
    if security["display_ticker"] == ticker:
        return
    result = con.execute(
        text(
            "UPDATE securities SET display_ticker=:ticker,version=version+1 "
            "WHERE id=:id AND version=:version"
        ),
        {"ticker": ticker, "id": security["id"], "version": security["version"]},
    )
    if result.rowcount != 1:
        raise ApiError(
            412, "PRECONDITION_FAILED", "Security identity changed; reload before editing"
        )
    audit(
        con,
        str(auth.owner.id),
        "security",
        security["id"],
        "SYMBOL_DISPLAY_CHANGE",
        request_id=request_id,
        before={"display_ticker": security["display_ticker"], "version": security["version"]},
        after={"display_ticker": ticker, "version": security["version"] + 1},
        reason=reason,
    )


def _later_symbol(con: Connection, security: str, day: str, root: str | None = None) -> bool:
    return (
        con.execute(
            text("""
        SELECT 1 FROM corporate_actions WHERE security_id=:security AND action_type='SYMBOL_CHANGE'
        AND status='CONFIRMED' AND is_current=1 AND effective_date>:day
        AND (:root IS NULL OR root_id!=:root) LIMIT 1
    """),
            {"security": security, "day": day, "root": root},
        ).first()
        is not None
    )


def _undo_symbol(
    con: Connection, auth: Auth, row: RowMapping, reason: str, request_id: str
) -> None:
    if (
        row["action_type"] != "SYMBOL_CHANGE"
        or row["status"] != "CONFIRMED"
        or row["previous_symbol"] is None
    ):
        return
    if _later_symbol(con, row["security_id"], row["effective_date"], row["root_id"]):
        raise ApiError(
            409,
            "SYMBOL_HISTORY_DEPENDENCY",
            "Resolve later confirmed display changes before correcting this one",
        )
    security = security_row(con, row["security_id"])
    if security["display_ticker"] != row["new_symbol"]:
        raise ApiError(
            409, "NEEDS_REVIEW", "The current display no longer matches this action's effect"
        )
    _set_symbol(con, auth, security, row["previous_symbol"], reason, request_id)


def _prepare(
    con: Connection, auth: Auth, body: CorporateActionCreate, request_id: str
) -> dict[str, Any]:
    security = security_row(con, str(body.security_id))
    previous = None
    if body.action_type == "SYMBOL_CHANGE" and body.status == "CONFIRMED":
        timezone = con.execute(
            text("SELECT timezone FROM exchanges WHERE code=:code"),
            {"code": security["exchange_code"]},
        ).scalar_one()
        if body.effective_date > now().astimezone(ZoneInfo(timezone)).date().isoformat():
            raise ApiError(
                422,
                "FUTURE_SYMBOL_CHANGE",
                "Keep a future display change in review; confirm after its effective date",
            )
        if _later_symbol(con, str(body.security_id), body.effective_date):
            raise ApiError(
                409,
                "SYMBOL_HISTORY_DEPENDENCY",
                "Do not overwrite a later confirmed display change",
            )
        previous = security["display_ticker"]
        assert body.new_symbol is not None
        _set_symbol(con, auth, security, body.new_symbol, body.reason, request_id)
    return {
        "security_id": str(body.security_id),
        "action_type": body.action_type,
        "effective_date": body.effective_date,
        "ratio_num": body.ratio_numerator,
        "ratio_den": body.ratio_denominator,
        "new_symbol": body.new_symbol,
        "previous_symbol": previous,
        "evidence_url": body.evidence_url,
        "status": body.status,
        "reason": body.reason,
        "note": body.note,
    }


def _insert(con: Connection, values: dict[str, Any]) -> None:
    con.execute(
        text(
            f"INSERT INTO corporate_actions({','.join(values)}) "
            f"VALUES ({','.join(':' + key for key in values)})"
        ),
        values,
    )


def _portfolios(con: Connection, security: str) -> list[str]:
    return list(
        con.execute(
            text("""
        SELECT portfolio_id FROM trades WHERE security_id=:security
        UNION SELECT portfolio_id FROM security_transfers WHERE security_id=:security
    """),
            {"security": security},
        ).scalars()
    )


def _close_reviews(con: Connection, auth: Auth, root: str, reason: str, request_id: str) -> None:
    reviews = (
        con.execute(
            text("""
        SELECT id,status FROM review_items WHERE json_extract(detail_json,'$.action_root_id')=:root
        AND status IN ('OPEN','ACKNOWLEDGED')
    """),
            {"root": root},
        )
        .mappings()
        .all()
    )
    for review in reviews:
        con.execute(
            text("UPDATE review_items SET status='RESOLVED',resolved_at=:now WHERE id=:id"),
            {"id": review["id"], "now": stamp()},
        )
        audit(
            con,
            str(auth.owner.id),
            "review_item",
            review["id"],
            "RESOLVE_ACTION_REVISION",
            request_id=request_id,
            before=dict(review),
            after={"status": "RESOLVED"},
            reason=reason,
        )


def _finish(
    con: Connection, auth: Auth, row: RowMapping, start: str, request_id: str, *, void: bool = False
) -> None:
    portfolios = _portfolios(con, row["security_id"])
    _close_reviews(con, auth, row["root_id"], row["reason"], request_id)
    review = row["status"] == "NEEDS_REVIEW" or row["action_type"] == "SYMBOL_CHANGE"
    for portfolio in portfolios:
        pending = validate_action_history(con, portfolio, row["security_id"])
        if not void and (review or pending):
            _review(con, row, portfolio)
        invalidate_portfolio(con, portfolio, row["id"], "CORPORATE_ACTION_CHANGED", start)
    if not void and review and not portfolios:
        _review(con, row, None)


def _review(con: Connection, row: RowMapping, portfolio: str | None) -> None:
    symbol = row["action_type"] == "SYMBOL_CHANGE"
    detail = {
        "action_id": row["id"],
        "action_root_id": row["root_id"],
        "security_id": row["security_id"],
        "revision": row["revision"],
        "scope": "DISPLAY_ONLY_NO_ALIAS_GUESS" if symbol else "UNALLOCATED_TERMS_OR_HISTORY",
        "reason": row["reason"],
    }
    con.execute(
        text("""
        INSERT INTO review_items(id,portfolio_id,kind,severity,reason_code,detail_json,created_at)
        VALUES (:id,:portfolio,'CORPORATE_ACTION','WARNING',:code,:detail,:now)
    """),
        {
            "id": str(uuid4()),
            "portfolio": portfolio,
            "code": "IDENTITY_MAPPING_REVIEW" if symbol else "ACTION_ALLOCATION_REVIEW",
            "detail": canonical(detail),
            "now": stamp(),
        },
    )


def create_action(
    con: Connection, auth: Auth, body: CorporateActionCreate, request_id: str
) -> CorporateAction:
    values = _prepare(con, auth, body, request_id)
    identifier = str(uuid4())
    values.update(
        id=identifier,
        root_id=identifier,
        created_sequence=allocate_sequence(con, "ACTION", identifier),
        created_at=stamp(),
    )
    _insert(con, values)
    row = action_row(con, identifier)
    _finish(con, auth, row, body.effective_date, request_id)
    result = as_action(action_row(con, identifier))
    audit(
        con,
        str(auth.owner.id),
        "corporate_action",
        identifier,
        "CREATE",
        request_id=request_id,
        after=result.model_dump(mode="json"),
        reason=body.reason,
    )
    return result


def revise_action(
    con: Connection,
    auth: Auth,
    identifier: str,
    version: int,
    body: CorporateActionCreate,
    request_id: str,
) -> CorporateAction:
    before = action_row(con, identifier)
    check_version(before["version"], version)
    _current(before)
    if str(body.security_id) != before["security_id"]:
        raise ApiError(
            409, "IMMUTABLE_IDENTITY", "An action revision cannot move to another security"
        )
    _undo_symbol(con, auth, before, body.reason, request_id)
    con.execute(
        text(
            "UPDATE corporate_actions SET is_current=0,version=version+1 "
            "WHERE id=:id AND version=:version"
        ),
        {"id": identifier, "version": version},
    )
    values = _prepare(con, auth, body, request_id)
    revised = str(uuid4())
    values.update(
        id=revised,
        root_id=before["root_id"],
        supersedes_id=identifier,
        revision=before["revision"] + 1,
        created_sequence=before["created_sequence"],
        created_at=stamp(),
    )
    _insert(con, values)
    row = action_row(con, revised)
    _finish(con, auth, row, min(before["effective_date"], body.effective_date), request_id)
    result = as_action(action_row(con, revised))
    audit(
        con,
        str(auth.owner.id),
        "corporate_action",
        revised,
        "REVISE",
        request_id=request_id,
        before=dict(before),
        after=result.model_dump(mode="json"),
        reason=body.reason,
    )
    return result


def void_action(
    con: Connection, auth: Auth, identifier: str, version: int, reason: str, request_id: str
) -> Success:
    before = action_row(con, identifier)
    check_version(before["version"], version)
    _current(before)
    _undo_symbol(con, auth, before, reason, request_id)
    con.execute(
        text(
            "UPDATE corporate_actions SET status='VOID',version=version+1 "
            "WHERE id=:id AND version=:version"
        ),
        {"id": identifier, "version": version},
    )
    row = action_row(con, identifier)
    _finish(con, auth, row, before["effective_date"], request_id, void=True)
    audit(
        con,
        str(auth.owner.id),
        "corporate_action",
        identifier,
        "VOID",
        request_id=request_id,
        before=dict(before),
        after={"status": "VOID", "version": version + 1},
        reason=reason,
    )
    return Success(message="Corporate action voided; original terms and audit history retained")
