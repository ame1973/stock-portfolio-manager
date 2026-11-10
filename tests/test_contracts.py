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
    extensions = {
        ("/api/v1/trades/{id}/audit-origin", "get"): "get_trade_audit_origin",
        ("/api/v1/trades/{id}/audit-origin/recover", "post"): "recover_trade_audit_origin",
        ("/api/v1/trades/{id}/history-review", "get"): "get_trade_history_review",
        ("/api/v1/trades/{id}/history-review", "post"): "save_trade_history_review",
        ("/api/v1/trades/{id}/history-review/revisions", "get"): "list_trade_history_reviews",
        ("/api/v1/dividends/candidates", "get"): "list_dividend_candidates",
        ("/api/v1/dividends/candidates", "post"): "create_dividend_candidate",
        ("/api/v1/dividends/candidates/{id}", "get"): "get_dividend_candidate",
        ("/api/v1/dividends/candidates/{id}", "patch"): "revise_dividend_candidate",
        ("/api/v1/dividends/candidates/{id}/revisions", "get"): "list_dividend_candidate_revisions",
        ("/api/v1/dividends/basis-assertions", "post"): "create_dividend_basis_assertion",
        ("/api/v1/dividends/basis-assertions", "get"): "list_dividend_basis_assertions",
        ("/api/v1/dividends/basis-assertions/{id}", "get"): "get_dividend_basis_assertion",
        ("/api/v1/providers/cited-observations", "post"): "create_cited_source_observation",
        ("/api/v1/provider-profiles", "get"): "list_provider_profiles",
        ("/api/v1/provider-profiles", "post"): "create_provider_profile",
        ("/api/v1/provider-profiles/{provider_id}", "get"): "get_provider_profile",
        ("/api/v1/provider-profiles/{provider_id}", "patch"): "update_provider_profile",
        (
            "/api/v1/provider-profiles/{provider_id}/credential",
            "post",
        ): "import_provider_credential",
        (
            "/api/v1/provider-profiles/{provider_id}/revisions",
            "get",
        ): "list_provider_profile_revisions",
        ("/api/v1/source-schedules", "get"): "list_source_schedules",
        ("/api/v1/source-schedules", "post"): "create_source_schedule",
        ("/api/v1/source-schedules/{id}", "get"): "get_source_schedule",
        ("/api/v1/source-schedules/{id}", "patch"): "update_source_schedule",
        ("/api/v1/source-schedules/{id}/occurrences", "get"): "list_source_schedule_occurrences",
        ("/api/v1/providers/split-observations", "get"): "list_split_observations",
        ("/api/v1/jobs/{id}/items", "get"): "list_job_items",
        ("/api/v1/auth/reauth", "post"): "reauthenticate",
        ("/api/v1/exchanges", "get"): "list_exchanges",
        ("/api/v1/cash-entries/{id}", "get"): "get_cash_entry",
        ("/api/v1/expenses/{id}", "get"): "get_expense",
        ("/api/v1/accounts/{id}/cash-balances", "get"): "get_cash_balances",
        ("/api/v1/accounts/{id}/opening-cash", "get"): "get_opening_cash",
        ("/api/v1/accounts/{id}/opening-cash", "patch"): "correct_opening_cash",
        ("/api/v1/corporate-actions/{id}", "get"): "get_corporate_action",
        ("/api/v1/securities/{id}/identifiers/{mapping_id}", "patch"): "correct_identifier_period",
        (
            "/api/v1/securities/{id}/provider-symbols/{mapping_id}",
            "patch",
        ): "correct_provider_symbol_period",
    }
    for resource, singular in (
        ("cash-transfers", "cash_transfer"),
        ("security-transfers", "security_transfer"),
    ):
        extensions[(f"/api/v1/{resource}", "get")] = f"list_{singular}s"
        extensions[(f"/api/v1/{resource}/{{id}}", "get")] = f"get_{singular}"
        extensions[(f"/api/v1/{resource}/{{id}}", "patch")] = f"revise_{singular}"
        extensions[(f"/api/v1/{resource}/{{id}}", "delete")] = f"void_{singular}"
    extensions[("/api/v1/cash-transfers", "post")] = "create_cash_transfer"
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


def test_authentication_and_write_security_are_declared_in_the_contract() -> None:
    public = {
        ("/api/v1/setup/status", "get"),
        ("/api/v1/setup/initialize", "post"),
        ("/api/v1/auth/login", "post"),
        ("/api/v1/health/live", "get"),
        ("/api/v1/health/ready", "get"),
    }
    for route, operation in operations(create_app().openapi()).items():
        if route in public:
            assert not operation.get("security")
            continue
        assert operation["security"] == [{"cookieAuth": []}]
        if route[1] != "get":
            headers = {
                item["name"]: item for item in operation["parameters"] if item["in"] == "header"
            }
            for name in ("X-CSRF-Token", "Idempotency-Key"):
                assert headers[name]["required"] is True


def test_compose_trusts_only_the_reserved_gateway_ip() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = compose["services"]
    address = services["web"]["networks"]["app"]["ipv4_address"]
    assert services["api"]["environment"]["FORWARDED_ALLOW_IPS"] == address
    assert "*" not in address
    assert not services["api"].get("ports")
    assert not services["worker"].get("ports")
    assert services["web"]["ports"][0].startswith("127.0.0.1:")
    assert compose["networks"]["app"]["ipam"]["config"][0]["subnet"]


def test_reference_baseline_has_expected_scope() -> None:
    reference = yaml.safe_load((ROOT / "packages/contracts/reference.openapi.yaml").read_text())
    assert len(operations(reference)) == 91
    assert len(reference["components"]["schemas"]) == 74
    assert len(reference["paths"]) == 67
