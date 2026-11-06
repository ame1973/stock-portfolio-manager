"""Seed source-only browser fixtures through real bounded adapters in a network-none test image."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic_ns
from typing import Any
from uuid import UUID, uuid4

import httpx

from spm_api.config import Settings
from spm_api.db import make_engine, unit_of_work
from spm_api.job_models import SourceOperation
from spm_api.providers.configuration import ProviderConfigurationStore, ProviderFile
from spm_api.providers.persistence import PreparedFetch, capture_page, prepare_fetch
from spm_api.providers.runtime import configured_adapter
from spm_api.providers.symbols import FetchWindow, resolve_symbol


async def main(security_id: UUID) -> None:
    if os.environ.get("SPM_SYNTHETIC_E2E") != "source-ui" or os.geteuid() != 10001:
        raise SystemExit("This fixture requires the isolated synthetic source-UI image.")
    settings = Settings()
    engine = make_engine(settings.database_path)

    def snapshot() -> dict[str, list[tuple[Any, ...]]]:
        with unit_of_work(engine) as con:
            names = con.exec_driver_sql(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
            ).scalars()
            return {
                name: [
                    tuple(row)
                    for row in con.exec_driver_sql(f'SELECT * FROM "{name}" ORDER BY rowid')
                ]
                for name in names
            }

    try:
        with unit_of_work(engine) as con:
            assert con.exec_driver_sql("SELECT count(*) FROM owners").scalar_one() == 1
            owner_id = UUID(con.exec_driver_sql("SELECT id FROM owners").scalar_one())
            row = con.exec_driver_sql(
                "SELECT name,display_ticker FROM securities WHERE id=?", (str(security_id),)
            ).one()
            assert tuple(row) == ("E2E source UI evidence", "SUI")
            assert (
                con.exec_driver_sql(
                    "SELECT count(*) FROM dividend_observations WHERE security_id=?",
                    (str(security_id),),
                ).scalar_one()
                == 0
            )
        before = snapshot()
        requests = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            assert request.url.host == "eodhd.com"
            assert request.url.params["api_token"] == "synthetic-browser-not-a-real-key"
            # Acquiring another writer proves that mocked I/O is outside the source writer UoW.
            with unit_of_work(engine, write=True):
                pass
            if request.url.path == "/api/div/SUI.US":
                rows = [
                    {"date": f"2026-01-{day:02}", "unadjustedValue": "0.9", "currency": "USD"}
                    for day in range(1, 27)
                ] + [
                    {
                        "date": "2026-01-27",
                        "unadjustedValue": "92233720368.54775807",
                        "currency": "EUR",
                    }
                ]
            else:
                assert request.url.path == "/api/splits/SUI.US"
                rows = [
                    {"date": "2026-01-08", "split": "9223372036854775807/9223372036854775806"},
                    {"date": "2026-01-09", "split": "1/3"},
                ]
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=httpx.ByteStream(json.dumps(rows).encode()),
            )

        # Private profiles exist only in this container's tmpfs, not the API/worker data directory.
        # The real deployment remains unconfigured and cannot send the synthetic key upstream.
        with TemporaryDirectory(prefix="spm-synthetic-source-") as directory:
            base = Path(directory)
            data = base / "data"
            data.mkdir(mode=0o700)
            profiles, keys = data / "provider-config", base / "keys"
            profiles.mkdir(mode=0o700)
            keys.mkdir(mode=0o700)
            profile = ProviderFile(
                provider_id="eodhd",
                version=1,
                enabled=True,
                terms_acknowledged=True,
                terms_reference="Synthetic network-disabled browser evidence",
                terms_reason="Synthetic mocked input, not live data or a data-rights approval.",
                requests_per_minute=100,
                requests_per_day=100,
                credential_source="LOCAL_FILE",
                credential_version=1,
            )
            profile_path, key_path = profiles / "eodhd.json", keys / "eodhd.1.key"
            profile_path.write_text(profile.model_dump_json())
            key_path.write_text("synthetic-browser-not-a-real-key\n")
            profile_path.chmod(0o400)
            key_path.chmod(0o400)
            local = settings.model_copy(update={"data_dir": data, "provider_secret_dir": keys})
            store = ProviderConfigurationStore(local)
            loaded = store.load("eodhd")
            assert loaded.error_code is None, loaded.health.reason
            with unit_of_work(engine) as con:
                resolution = resolve_symbol(
                    con, security_id, "eodhd", FetchWindow(start="2026-01-01", end="2026-01-31")
                )
            assert len(resolution.segments) == 1
            operations: tuple[SourceOperation, ...] = ("DIVIDENDS", "SPLITS")
            contexts: dict[SourceOperation, PreparedFetch] = {
                operation: prepare_fetch(owner_id, loaded, resolution.segments[0], operation)
                for operation in operations
            }
            sequence: tuple[SourceOperation, ...] = (*operations, "DIVIDENDS")
            for operation in sequence:
                prepared = contexts[operation]
                assert store.unchanged(loaded)
                started = datetime.now(UTC)
                clock = monotonic_ns()
                async with configured_adapter(
                    loaded, transport=httpx.MockTransport(respond)
                ) as adapter:
                    page = (
                        await adapter.fetch_dividends(prepared.segment)
                        if operation == "DIVIDENDS"
                        else await adapter.fetch_splits(prepared.segment)
                    )
                finished = datetime.now(UTC)
                assert store.unchanged(loaded) and page.complete and page.http_status == 200
                with unit_of_work(engine, write=True) as con:
                    capture_page(
                        con,
                        prepared,
                        page,
                        run_id=uuid4(),
                        started_at=started,
                        finished_at=finished,
                        duration_ms=(monotonic_ns() - clock) // 1_000_000,
                    )
        allowed = {
            "provider_fetch_contexts",
            "provider_runs",
            "dividend_observations",
            "split_observations",
            "provider_observation_sightings",
            "audit_events",
        }
        after = snapshot()
        assert {name: rows for name, rows in before.items() if name not in allowed} == {
            name: rows for name, rows in after.items() if name not in allowed
        }
        assert requests == 3
        assert len(after["dividend_observations"]) == 27
        assert len(after["split_observations"]) == 2
        assert not (settings.data_dir / "provider-config").exists()
        print(
            "Synthetic adapter evidence: 3 mocked requests, 27 dividends, 2 provider-only splits; "
            "financial facts unchanged."
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(UUID(sys.argv[1])))
