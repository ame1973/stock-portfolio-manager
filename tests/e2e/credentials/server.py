"""Serve the actual API and built SPA using only a newly generated synthetic fixture root."""

import os
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from spm_api.config import Settings
from spm_api.main import create_app

root = Path(os.environ["SPM_E2E_CREDENTIAL_ROOT"])
assert root == root.resolve(strict=True)
assert root.parent == Path(tempfile.gettempdir()).resolve()
assert root.name.startswith("spm-credential-browser-")
assert root.stat().st_uid == os.geteuid() and stat.S_IMODE(root.stat().st_mode) == 0o700
assert os.environ["SPM_E2E_COMPOSE_PROJECT"] == "spm-e2e-credential-browser-fixture"
settings = Settings()
assert settings.app_env == "test"
for name, path in (
    ("data", settings.data_dir),
    ("backups", settings.backup_dir),
    ("operator", settings.provider_secret_dir),
    ("managed", settings.managed_provider_secret_dir),
):
    assert path == root / name and not path.exists()
    path.mkdir(mode=0o700)
origin = urlsplit(settings.public_origin)
assert origin.scheme == "http" and origin.hostname == "127.0.0.1" and origin.port
assert settings.public_origin == os.environ["SPM_E2E_BASE_URL"]
subprocess.run(["spm-manage", "migrate"], check=True, capture_output=True)
build = Path(__file__).resolve().parents[3] / "apps/web/dist"
assert (build / "index.html").is_file()
app = create_app(settings)
app.mount("/assets", StaticFiles(directory=build / "assets"), name="fixture-built-assets")


@app.get("/", include_in_schema=False)
@app.get("/settings/providers", include_in_schema=False)
@app.get("/settings/providers/{provider_id}", include_in_schema=False)
def spa() -> FileResponse:
    return FileResponse(build / "index.html", headers={"Cache-Control": "no-store"})


# No worker is started: this proves real credential UI/HTTP/files, not provider execution.
uvicorn.run(app, host="127.0.0.1", port=origin.port, access_log=False, log_config=None)
