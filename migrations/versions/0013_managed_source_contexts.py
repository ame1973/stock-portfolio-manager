"""Extend immutable source contexts with exact owned policy provenance, without adopting access."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0013_managed_source_contexts"
down_revision = "0012_provider_profiles"
branch_labels = None
depends_on = None

_COMMON = (
    "provider_id",
    "version",
    "enabled",
    "terms_acknowledged",
    "terms_reference",
    "terms_reason",
    "requests_per_minute",
    "requests_per_day",
    "raw_payload_storage",
)
_LEGACY = (*_COMMON, "credential_source", "credential_version")
_MANAGED = (*_COMMON, "configuration_source", "revision_id", "owner_id", "credential_artifact_id")


def _names(values: tuple[str, ...]) -> str:
    return ",".join(f"'{name}'" for name in values)


def upgrade() -> None:
    con = op.get_bind()
    installed = con.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='trg_provider_context_insert'"
    ).scalar_one_or_none()
    fragment = "'credential_version','raw_payload_storage'"
    if not isinstance(installed, str) or installed.count(fragment) != 1:
        raise RuntimeError("Expected retained source context guard is unavailable")
    # Preserve every original alias/date/origin/policy guard; widen only its exact key whitelist.
    extended = installed.replace(
        fragment,
        fragment + ",'configuration_source','revision_id','owner_id','credential_artifact_id'",
    )
    con.exec_driver_sql("DROP TRIGGER trg_provider_context_insert")
    con.exec_driver_sql(extended)
    mirrors = " AND ".join(
        f"p.{name} IS json_extract(NEW.profile_json,'$.{name}')"
        for name in (*_COMMON, "credential_artifact_id")
    )
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_provider_configuration_context_insert
        BEFORE INSERT ON provider_fetch_contexts
        WHEN EXISTS(SELECT 1 FROM provider_fetch_contexts WHERE id=NEW.id)
          OR CASE WHEN json_type(NEW.profile_json,'$.configuration_source') IS NULL THEN
                EXISTS(SELECT 1 FROM json_each(NEW.profile_json)
                    WHERE key NOT IN ({_names(_LEGACY)}))
            ELSE json_type(NEW.profile_json,'$.configuration_source') IS NOT 'text'
              OR json_extract(NEW.profile_json,'$.configuration_source') IS NOT 'MANAGED_PROFILE'
              OR (SELECT count(*) FROM json_each(NEW.profile_json))!={len(_MANAGED)}
              OR (SELECT count(DISTINCT key) FROM json_each(NEW.profile_json))!={len(_MANAGED)}
              OR EXISTS(SELECT 1 FROM json_each(NEW.profile_json)
                    WHERE key NOT IN ({_names(_MANAGED)}))
              OR json_type(NEW.profile_json,'$.revision_id') IS NOT 'text'
              OR json_type(NEW.profile_json,'$.owner_id') IS NOT 'text'
              OR json_type(NEW.profile_json,'$.provider_id') IS NOT 'text'
              OR json_type(NEW.profile_json,'$.terms_reference') IS NOT 'text'
              OR json_type(NEW.profile_json,'$.terms_reason') IS NOT 'text'
              OR (json_type(NEW.profile_json,'$.credential_artifact_id') IS NOT 'text'
                    AND json_type(NEW.profile_json,'$.credential_artifact_id') IS NOT 'null')
              OR json_extract(NEW.profile_json,'$.owner_id') IS NOT NEW.owner_id
              OR NOT EXISTS(SELECT 1 FROM owners WHERE id=NEW.owner_id AND is_active=1)
              OR NOT EXISTS(
                    SELECT 1 FROM provider_profile_revisions p
                    WHERE p.id=json_extract(NEW.profile_json,'$.revision_id')
                      AND p.owner_id=NEW.owner_id AND p.provider_id=NEW.provider_id
                      AND p.enabled=1 AND p.terms_acknowledged=1 AND {mirrors}
                      AND p.version=(SELECT max(version) FROM provider_profile_revisions
                            WHERE owner_id=NEW.owner_id AND provider_id=NEW.provider_id))
            END
        BEGIN SELECT RAISE(ABORT,'INVALID_PROVIDER_CONTEXT'); END
    """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_MANAGED_SOURCE_CONTEXTS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; preserve source facts and require exact owned provenance",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of deleting source history")
