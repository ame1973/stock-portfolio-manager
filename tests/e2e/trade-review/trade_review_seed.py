"""Insert one new auditless synthetic draft in the exclusive browser fixture."""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from spm_api.audit import stamp
from spm_api.config import Settings
from spm_api.db import make_engine, unit_of_work
from spm_api.ledger_service import allocate_sequence

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
identifier = str(uuid4())
with unit_of_work(engine, write=True) as con:
    assert con.exec_driver_sql("SELECT count(*) FROM owners").scalar_one() == 1
    assert con.exec_driver_sql("SELECT id FROM owners").scalar_one() == identifiers["owner"]
    assert (
        con.execute(
            text("""
        SELECT count(*) FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id
        JOIN brokers b ON b.id=a.broker_id JOIN securities s ON s.id=:security
        WHERE a.id=:account AND p.id=:portfolio AND p.owner_id=:owner AND b.owner_id=:owner
    """),
            identifiers,
        ).scalar_one()
        == 1
    )
    sequence, at = allocate_sequence(con, "TRADE", identifier), stamp()
    con.execute(
        text("""
        INSERT INTO trades(id,root_id,created_sequence,portfolio_id,account_id,security_id,
            side,trade_date,quantity_e8,price_e8,currency,gross_micro,settlement_micro,
            status,created_at,updated_at)
        VALUES (:id,:id,:sequence,:portfolio,:account,:security,'BUY','2997-01-02',
            1000000000,1000000000,'USD',100000000,100000000,'DRAFT_SCHEDULED',:at,:at)
    """),
        {**identifiers, "id": identifier, "sequence": sequence, "at": at},
    )
engine.dispose()
print(json.dumps({"id": identifier}))
