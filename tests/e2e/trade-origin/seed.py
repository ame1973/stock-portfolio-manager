"""Add synthetic legacy drafts only to the isolated browser fixture database."""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from spm_api import trade_service
from spm_api.audit import audit, stamp
from spm_api.config import Settings
from spm_api.db import make_engine, unit_of_work
from spm_api.trade_codec import as_trade

root = Path(os.environ["SPM_E2E_CREDENTIAL_ROOT"])
assert root == root.resolve(strict=True)
assert root.parent == Path(tempfile.gettempdir()).resolve()
assert root.name.startswith("spm-credential-browser-")
assert root.stat().st_uid == os.geteuid() and stat.S_IMODE(root.stat().st_mode) == 0o700
assert os.environ["SPM_E2E_COMPOSE_PROJECT"] == "spm-e2e-credential-browser-fixture"
settings = Settings()
assert settings.app_env == "test" and settings.data_dir == root / "data"
assert settings.database_path.is_file() and not settings.database_path.is_symlink()
identifiers = {key: str(UUID(value)) for key, value in json.load(sys.stdin).items()}
assert set(identifiers) == {"owner", "portfolio", "account", "security"}
engine = make_engine(settings.database_path)


def insert(con: Connection, table: str, values: dict[str, Any]) -> None:
    fields = ",".join(values)
    parameters = ",".join(":" + name for name in values)
    con.execute(text(f"INSERT INTO {table}({fields}) VALUES ({parameters})"), values)


originals = []
with unit_of_work(engine, write=True) as con:
    assert con.exec_driver_sql("SELECT count(*) FROM owners").scalar_one() == 1
    assert con.exec_driver_sql("SELECT id FROM owners").scalar_one() == identifiers["owner"]
    assert con.exec_driver_sql("SELECT count(*) FROM trade_audit_origins").scalar_one() == 1
    for sequence, day in ((9007199254740993, "2998-01-02"), (9223372036854775807, "2999-01-02")):
        first, second, at = str(uuid4()), str(uuid4()), stamp()
        con.exec_driver_sql(
            "INSERT INTO ledger_sequences(sequence,kind,fact_id) VALUES (?,'TRADE',?)",
            (sequence, first),
        )
        values = {
            "id": first,
            "root_id": first,
            "created_sequence": sequence,
            "portfolio_id": identifiers["portfolio"],
            "account_id": identifiers["account"],
            "security_id": identifiers["security"],
            "side": "BUY",
            "trade_date": day,
            "quantity_e8": 1000000000,
            "price_e8": 1000000000,
            "currency": "USD",
            "gross_micro": 100000000,
            "settlement_micro": 100000000,
            "status": "DRAFT_SCHEDULED",
            "created_at": at,
            "updated_at": at,
        }
        insert(con, "trades", values)
        original = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (first,)).mappings().one()
        retained = as_trade(original)
        audit(
            con,
            identifiers["owner"],
            "trade",
            first,
            "CREATE",
            request_id=str(uuid4()),
            after=retained.model_dump(mode="json"),
        )
        trade_service._retire(con, original, "SUPERSEDED")
        revised_at = stamp()
        insert(
            con,
            "trades",
            {
                **values,
                "id": second,
                "revision": 2,
                "supersedes_id": first,
                "quantity_e8": 1100000000,
                "gross_micro": 110000000,
                "settlement_micro": 110000000,
                "created_at": revised_at,
                "updated_at": revised_at,
            },
        )
        successor = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (second,)).mappings().one()
        )
        audit(
            con,
            identifiers["owner"],
            "trade",
            second,
            "REVISE",
            request_id=str(uuid4()),
            before=dict(original),
            after=as_trade(successor).model_dump(mode="json"),
            reason="Correct the synthetic historical draft",
        )
        originals.append(retained.model_dump_json())
engine.dispose()
for original_json in originals:
    print(original_json)
