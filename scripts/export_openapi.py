"""Export the real executable API contract without opening a database."""

import argparse
import json
from pathlib import Path

from spm_api.main import create_app

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "packages/contracts/openapi.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not TARGET.is_file() or TARGET.read_text() != rendered:
            raise SystemExit("OpenAPI drift: run npm run generate:contracts")
        print("Executable OpenAPI is current.")
    else:
        TARGET.write_text(rendered)
        print("Generated packages/contracts/openapi.json")


if __name__ == "__main__":
    main()
