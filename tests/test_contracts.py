"""Executable contract drift and immutable specification baseline checks."""

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from spm_api.main import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_authoritative_fixture_hashes() -> None:
    expected = {
        "finance_golden.json": "caecd254d902b9493f98979dbf6c2e663a4ca608e5433ecba5e586950d555d2a",
        "dividend_provider_cases.json": (
            "400d5dbf7157e61e52e1e777336ea82d32887d891a2118d4c9db6b38ba6b2a57"
        ),
    }
    for name, digest in expected.items():
        data = (ROOT / "packages/finance-fixtures" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest


def operations(spec: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (path, method): operation
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if method in {"get", "post", "patch", "put", "delete"}
    }


def test_executable_openapi_is_generated_and_matches_implemented_baseline() -> None:
    generated = create_app().openapi()
    assert generated == json.loads((ROOT / "packages/contracts/openapi.json").read_text())
    reference = yaml.safe_load((ROOT / "packages/contracts/reference.openapi.yaml").read_text())
    baseline = operations(reference)
    actual = operations(generated)
    identifiers = [op["operationId"] for op in actual.values()]
    assert len(identifiers) == len(set(identifiers))
    extensions = {("/api/v1/auth/reauth", "post"): "reauthenticate"}
    for route, operation in actual.items():
        if route in extensions:
            assert operation["operationId"] == extensions[route]
            continue
        assert operation["operationId"] == baseline[route]["operationId"]
        assert {code for code in operation["responses"] if code.startswith("2")} <= set(
            baseline[route]["responses"]
        )
    for schema in generated["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)


def test_reference_baseline_has_expected_scope() -> None:
    reference = yaml.safe_load((ROOT / "packages/contracts/reference.openapi.yaml").read_text())
    assert len(operations(reference)) == 91
    assert len(reference["components"]["schemas"]) == 74
    assert len(reference["paths"]) == 67
