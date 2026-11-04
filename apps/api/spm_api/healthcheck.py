"""Healthcheck entrypoint for container supervision without an owner session."""

import argparse
import json
from urllib.request import urlopen

from spm_api.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        from spm_worker.main import healthy

        if not healthy(Settings().data_dir):
            raise SystemExit(1)
    else:
        with urlopen("http://127.0.0.1:8000/api/v1/health/ready", timeout=3) as response:
            if response.status != 200 or json.load(response) != {"status": "ready"}:
                raise SystemExit(1)


if __name__ == "__main__":
    main()
