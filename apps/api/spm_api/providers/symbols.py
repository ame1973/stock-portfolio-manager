"""Resolve explicit provider identities for an entire inclusive calendar window."""

from datetime import date, timedelta
from enum import StrEnum
from typing import cast
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import Connection, text

from spm_api.decimal_types import IsoDate
from spm_api.errors import Model
from spm_api.master_models import ProviderCode, ProviderId
from spm_api.providers.base import Market, registry


class FetchWindow(Model):
    model_config = ConfigDict(frozen=True)

    start: IsoDate
    end: IsoDate

    @model_validator(mode="after")
    def ordered(self) -> "FetchWindow":
        if self.end < self.start:
            raise ValueError("Provider window end precedes its start")
        return self


class AliasPeriod(Model):
    model_config = ConfigDict(frozen=True)

    id: UUID
    security_id: UUID
    provider_id: ProviderId
    provider_symbol: ProviderCode
    effective_from: IsoDate
    effective_to: IsoDate | None = None
    verified: bool = Field(default=False, strict=True)
    version: int = Field(default=1, strict=True, ge=1)

    @model_validator(mode="after")
    def ordered(self) -> "AliasPeriod":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("Provider alias end precedes its start")
        return self


class AliasSegment(Model):
    model_config = ConfigDict(frozen=True)

    alias: AliasPeriod
    window: FetchWindow
    market: Market | None = None

    @model_validator(mode="after")
    def within_identity(self) -> "AliasSegment":
        if self.window.start < self.alias.effective_from or (
            self.alias.effective_to is not None and self.window.end > self.alias.effective_to
        ):
            raise ValueError("Fetch segment must remain inside the explicit alias period")
        return self


class ResolutionState(StrEnum):
    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"


class SymbolResolution(Model):
    model_config = ConfigDict(frozen=True)

    state: ResolutionState
    segments: tuple[AliasSegment, ...] = ()
    reason: str | None = None
    warnings: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def complete_only(self) -> "SymbolResolution":
        if bool(self.segments) != (self.state == ResolutionState.RESOLVED):
            raise ValueError("Only a complete resolution may expose provider fetch segments")
        return self


def overlaps(period: AliasPeriod, window: FetchWindow) -> bool:
    return period.effective_from <= window.end and (
        period.effective_to is None or period.effective_to >= window.start
    )


def resolve_periods(
    security_id: UUID,
    provider_id: ProviderId,
    market: Market,
    window: FetchWindow,
    aliases: list[AliasPeriod],
) -> SymbolResolution:
    """Mapping validity is not a claim of upstream coverage or verified dividend terms."""
    if market not in registry()[provider_id].markets:
        return SymbolResolution(state=ResolutionState.UNSUPPORTED, reason="UNSUPPORTED_MARKET")
    own = sorted(
        (
            item
            for item in aliases
            if item.security_id == security_id
            and item.provider_id == provider_id
            and overlaps(item, window)
        ),
        key=lambda item: (item.effective_from, str(item.id)),
    )
    if not own:
        return SymbolResolution(state=ResolutionState.NOT_FOUND, reason="NO_PROVIDER_ALIAS")
    cursor: date | None = date.fromisoformat(window.start)
    end = date.fromisoformat(window.end)
    segments = []
    for item in own:
        first = max(date.fromisoformat(item.effective_from), date.fromisoformat(window.start))
        last = min(date.fromisoformat(item.effective_to) if item.effective_to else end, end)
        if cursor is None or first < cursor:
            return SymbolResolution(state=ResolutionState.AMBIGUOUS, reason="OVERLAPPING_ALIASES")
        if first > cursor:
            return SymbolResolution(state=ResolutionState.NOT_FOUND, reason="ALIAS_WINDOW_GAP")
        clipped = FetchWindow(start=first.isoformat(), end=last.isoformat())
        if any(
            other.provider_id == provider_id
            and other.security_id != security_id
            and other.provider_symbol == item.provider_symbol
            and overlaps(other, clipped)
            for other in aliases
        ):
            return SymbolResolution(
                state=ResolutionState.AMBIGUOUS, reason="ALIAS_IDENTITY_COLLISION"
            )
        segments.append(AliasSegment(alias=item, window=clipped, market=market))
        cursor = None if last == end else last + timedelta(days=1)
    if cursor is not None:
        return SymbolResolution(state=ResolutionState.NOT_FOUND, reason="ALIAS_WINDOW_GAP")
    return SymbolResolution(
        state=ResolutionState.RESOLVED,
        segments=tuple(segments),
        warnings=frozenset({"UNVERIFIED_PROVIDER_ALIAS"})
        if any(not item.verified for item in own)
        else frozenset(),
    )


def resolve_symbol(
    con: Connection, security_id: UUID, provider_id: ProviderId, window: FetchWindow
) -> SymbolResolution:
    """Read catalogue mappings only; the caller owns the transaction and any later HTTP."""
    security = (
        con.execute(
            text(
                "SELECT e.market FROM securities s JOIN exchanges e ON e.code=s.exchange_code "
                "WHERE s.id=:id"
            ),
            {"id": str(security_id)},
        )
        .mappings()
        .first()
    )
    if security is None:
        return SymbolResolution(state=ResolutionState.NOT_FOUND, reason="SECURITY_NOT_FOUND")
    market = cast(Market, security["market"])
    if market not in registry()[provider_id].markets:
        return SymbolResolution(state=ResolutionState.UNSUPPORTED, reason="UNSUPPORTED_MARKET")
    rows = con.execute(
        text("""
        SELECT DISTINCT candidate.id,candidate.security_id,candidate.provider_id,
            candidate.provider_symbol,candidate.effective_from,candidate.effective_to,
            candidate.verified,candidate.version
        FROM provider_symbols own JOIN provider_symbols candidate
          ON candidate.provider_id=own.provider_id
          AND (candidate.security_id=own.security_id
               OR candidate.provider_symbol=own.provider_symbol)
        WHERE own.security_id=:id AND own.provider_id=:provider
          AND own.effective_from<=:end AND (own.effective_to IS NULL OR own.effective_to>=:start)
          AND candidate.effective_from<=:end
          AND (candidate.effective_to IS NULL OR candidate.effective_to>=:start)
    """),
        {"id": str(security_id), "provider": provider_id, "start": window.start, "end": window.end},
    ).mappings()
    aliases = [
        AliasPeriod.model_validate({**row, "verified": row["verified"] == 1}) for row in rows
    ]
    return resolve_periods(security_id, provider_id, market, window, aliases)
