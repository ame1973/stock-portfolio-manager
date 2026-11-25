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
        (
            "/api/v1/trades/{id}/unverified-corrections/scope",
            "get",
        ): "get_unverified_trade_correction_scope",
        (
            "/api/v1/trades/{id}/unverified-corrections/revise",
            "post",
        ): "revise_unverified_trade",
        ("/api/v1/trades/{id}/unverified-corrections/void", "post"): "void_unverified_trade",
        ("/api/v1/dividends/events/from-candidate", "post"): "create_dividend_event_from_candidate",
        (
            "/api/v1/dividends/events/{event_id}/from-candidate",
            "post",
        ): "revise_dividend_event_from_candidate",
        ("/api/v1/dividends/events/{event_id}/withdraw", "post"): "withdraw_dividend_event",
        ("/api/v1/dividends/events/{event_id}", "get"): "get_dividend_event",
        ("/api/v1/dividends/events/{event_id}", "patch"): "revise_dividend_event",
        (
            "/api/v1/dividends/events/{event_id}/revisions",
            "get",
        ): "list_dividend_event_revisions",
        (
            "/api/v1/dividends/events/{event_id}/rejection",
            "get",
        ): "get_dividend_rejection_state",
        (
            "/api/v1/dividends/events/{event_id}/rejections",
            "get",
        ): "list_dividend_rejections",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings",
            "get",
        ): "get_dividend_fee_estimate",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings",
            "post",
        ): "save_dividend_fee_estimate",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings/history",
            "get",
        ): "list_dividend_fee_estimate_history",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings",
            "get",
        ): "get_dividend_tax_estimate",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings",
            "post",
        ): "save_dividend_tax_estimate",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings/history",
            "get",
        ): "list_dividend_tax_estimate_history",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/projection-inputs",
            "get",
        ): "get_dividend_projection_inputs",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/projections",
            "get",
        ): "list_dividend_projection_history",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/projections",
            "post",
        ): "create_recorded_dividend_projection",
        (
            "/api/v1/dividends/events/{event_id}/revision-preview",
            "post",
        ): "preview_dividend_revision",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/reconciliation",
            "get",
        ): "get_recorded_dividend_reconciliation",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts",
            "get",
        ): "list_combined_recorded_dividend_receipts",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts/inputs",
            "get",
        ): "get_direct_recorded_dividend_receipt_inputs",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts/"
            "{receipt_id}/void-inputs",
            "get",
        ): "get_direct_recorded_dividend_receipt_void_inputs",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts",
            "get",
        ): "list_recorded_dividend_receipts",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts/inputs",
            "get",
        ): "get_recorded_dividend_receipt_inputs",
        (
            "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts/"
            "{receipt_id}/void-inputs",
            "get",
        ): "get_recorded_dividend_receipt_void_inputs",
        ("/api/v1/dividends/direct-receipts", "post"): "create_direct_dividend_receipt",
        ("/api/v1/dividends/direct-receipts/{id}/void", "post"): "void_direct_dividend_receipt",
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


def test_revision_preview_contract_is_private_bounded_and_unverified() -> None:
    spec = create_app().openapi()
    path = "/api/v1/dividends/events/{event_id}/revision-preview"
    operation = spec["paths"][path]["post"]
    assert operation["operationId"] == "preview_dividend_revision"
    assert operation["security"] == [{"cookieAuth": []}]
    headers = {value["name"]: value for value in operation["parameters"] if value["in"] == "header"}
    assert all(
        headers[name]["required"] for name in ("X-CSRF-Token", "Idempotency-Key", "If-Match")
    )
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/PreviewRequest"
    )
    schemas = spec["components"]["schemas"]
    request, page, account = (
        schemas[name] for name in ("PreviewRequest", "PreviewPage", "PreviewAccount")
    )
    assert request["additionalProperties"] is page["additionalProperties"] is False
    assert account["additionalProperties"] is False
    assert {"revision_id", "proposal", "after_account_id", "snapshot_token"} <= set(
        request["required"]
    )
    assert request["properties"]["limit"]["maximum"] == 100
    assert page["properties"]["items"]["maxItems"] == 100
    for name, expected in (
        ("scope", "HYPOTHETICAL_RECORDED_COMPARISON_ONLY"),
        ("history_completeness", "UNVERIFIED"),
        ("current_use_scope", "REVALIDATION_REQUIRED"),
        ("eligibility_scope", "NOT_ESTABLISHED"),
        ("payment_scope", "OWNER_RECORDED_NOT_VERIFIED"),
    ):
        assert page["properties"][name]["const"] == expected
    for name in (
        "account_version",
        "broker_version",
        "portfolio_version",
        "portfolio_data_revision",
        "active_receipts",
        "void_receipts",
        "unverified_origins",
        "unavailable_cash_links",
    ):
        assert account["properties"][name]["type"] == "string"
    for name in (
        "previous_expected",
        "expected_delta",
        "recorded_received",
        "recorded_outstanding",
    ):
        assert {value["type"] for value in account["properties"][name]["anyOf"]} == {
            "string",
            "null",
        }


def test_recorded_reconciliation_contract_preserves_exact_unverified_values() -> None:
    spec = create_app().openapi()
    path = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/reconciliation"
    operation = spec["paths"][path]["get"]
    assert operation["operationId"] == "get_recorded_dividend_reconciliation"
    assert operation["security"] == [{"cookieAuth": []}]
    assert {item["name"] for item in operation["parameters"] if item["in"] == "path"} == {
        "event_id",
        "account_id",
    }
    schema = spec["components"]["schemas"]["RecordedReconciliation"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    for name in (
        "active_receipts",
        "void_receipts",
        "unverified_origins",
        "unavailable_cash_links",
    ):
        assert schema["properties"][name]["type"] == "string"
    for name in ("projection_no", "recorded_expected", "recorded_received", "recorded_outstanding"):
        assert {value["type"] for value in schema["properties"][name]["anyOf"]} == {
            "string",
            "null",
        }
    for name, value in (
        ("scope", "UNVERIFIED_RECORDED_RECONCILIATION_ONLY"),
        ("history_completeness", "UNVERIFIED"),
        ("current_use_scope", "REVALIDATION_REQUIRED"),
        ("eligibility_scope", "NOT_ESTABLISHED"),
        ("payment_scope", "OWNER_RECORDED_NOT_VERIFIED"),
    ):
        assert schema["properties"][name]["const"] == value


def test_combined_receipt_history_contract_keeps_explicit_basis_and_exact_values() -> None:
    spec = create_app().openapi()
    path = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts"
    operation = spec["paths"][path]["get"]
    assert operation["operationId"] == "list_combined_recorded_dividend_receipts"
    assert operation["security"] == [{"cookieAuth": []}]
    schema = spec["components"]["schemas"]["CombinedRecordedReceipt"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    properties = schema["properties"]
    assert properties["receipt_basis"]["enum"] == ["LEGACY_ENTITLEMENT", "DIRECT_EVENT_ACCOUNT"]
    assert {item["type"] for item in properties["entitlement_id"]["anyOf"]} == {"string", "null"}
    assert properties["version"]["type"] == properties["recorded_amount"]["type"] == "string"
    for name in ("recorded_tax", "recorded_fee", "fx_rate_hkd", "received_hkd"):
        assert {item["type"] for item in properties[name]["anyOf"]} == {"string", "null"}
    for name, value in (
        ("scope", "UNVERIFIED_COMBINED_RECEIPT_HISTORY_ONLY"),
        ("history_completeness", "UNVERIFIED"),
        ("current_use_scope", "REVALIDATION_REQUIRED"),
        ("eligibility_scope", "NOT_ESTABLISHED"),
        ("payment_scope", "OWNER_RECORDED_NOT_VERIFIED"),
    ):
        assert properties[name]["const"] == value


def test_direct_receipt_inputs_contract_has_exact_pins_but_no_entitlement_or_cash() -> None:
    spec = create_app().openapi()
    path = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts/inputs"
    operation = spec["paths"][path]["get"]
    assert operation["operationId"] == "get_direct_recorded_dividend_receipt_inputs"
    assert operation["security"] == [{"cookieAuth": []}]
    assert {item["name"] for item in operation["parameters"]} == {"event_id", "account_id"}
    schema = spec["components"]["schemas"]["DirectReceiptInputs"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    properties = schema["properties"]
    assert not {"entitlement_id", "entitlements", "received_amount", "currency"} & properties.keys()
    for name in (
        "account_version",
        "portfolio_version",
        "broker_version",
        "portfolio_data_revision",
    ):
        assert properties[name]["type"] == "string"
    assert {value["type"] for value in properties["rejection_id"]["anyOf"]} == {"string", "null"}
    for name, value in (
        ("receipt_basis", "DIRECT_EVENT_ACCOUNT"),
        ("scope", "DIRECT_RECORDED_RECEIPT_INPUTS_ONLY"),
        ("history_completeness", "UNVERIFIED"),
        ("current_use_scope", "REVALIDATION_REQUIRED"),
        ("eligibility_scope", "NOT_ESTABLISHED"),
        ("payment_scope", "OWNER_RECORDED_NOT_VERIFIED"),
    ):
        assert properties[name]["const"] == value


def test_direct_receipt_void_inputs_reuse_exact_current_and_recorded_contracts() -> None:
    spec = create_app().openapi()
    path = (
        "/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts/"
        "{receipt_id}/void-inputs"
    )
    operation = spec["paths"][path]["get"]
    assert operation["operationId"] == "get_direct_recorded_dividend_receipt_void_inputs"
    assert operation["security"] == [{"cookieAuth": []}]
    assert {item["name"] for item in operation["parameters"]} == {
        "event_id",
        "account_id",
        "receipt_id",
    }
    schema = spec["components"]["schemas"]["DirectReceiptVoidInputs"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    properties = schema["properties"]
    assert set(properties) == {
        "current",
        "receipt",
        "cash_entry_id",
        "origin_portfolio_id",
        "scope",
    }
    assert properties["current"]["$ref"] == "#/components/schemas/DirectReceiptInputs"
    assert properties["receipt"]["$ref"] == "#/components/schemas/CombinedRecordedReceipt"
    for name in ("cash_entry_id", "origin_portfolio_id"):
        assert properties[name]["type"] == "string" and properties[name]["format"] == "uuid"
    assert properties["scope"]["const"] == "DIRECT_RECORDED_RECEIPT_VOID_INPUTS_ONLY"


def test_recorded_receipt_void_inputs_contract_retains_unverified_scope() -> None:
    spec = create_app().openapi()
    path = (
        "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts/"
        "{receipt_id}/void-inputs"
    )
    operation = spec["paths"][path]["get"]
    assert operation["operationId"] == "get_recorded_dividend_receipt_void_inputs"
    assert operation["security"] == [{"cookieAuth": []}]
    assert {item["name"] for item in operation["parameters"] if item["in"] == "path"} == {
        "event_id",
        "account_id",
        "receipt_id",
    }
    schema = spec["components"]["schemas"]["ReceiptVoidInputs"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    for name in (
        "account_version",
        "portfolio_version",
        "broker_version",
        "portfolio_data_revision",
        "receipt_version",
    ):
        assert schema["properties"][name]["type"] == "string"
    for name in (
        "cash_entry_id",
        "origin_portfolio_id",
        "tax",
        "fee",
        "fx_rate_hkd",
        "received_hkd",
        "voided_at",
    ):
        assert {value["type"] for value in schema["properties"][name]["anyOf"]} == {
            "string",
            "null",
        }
    for name, value in (
        ("scope", "RECORDED_RECEIPT_VOID_INPUTS_ONLY"),
        ("history_completeness", "UNVERIFIED"),
        ("current_use_scope", "REVALIDATION_REQUIRED"),
        ("eligibility_scope", "NOT_ESTABLISHED"),
        ("payment_scope", "OWNER_RECORDED_NOT_VERIFIED"),
    ):
        assert schema["properties"][name]["const"] == value


def test_recorded_receipt_void_contract_requires_explicit_scope_and_headers() -> None:
    spec = create_app().openapi()
    operation = spec["paths"]["/api/v1/dividends/receipts/{id}/void"]["post"]
    assert operation["operationId"] == "void_dividend_receipt"
    assert operation["security"] == [{"cookieAuth": []}]
    headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
    for name in ("If-Match", "X-CSRF-Token", "Idempotency-Key"):
        assert headers[name]["required"] is True
    assert headers["If-Match"]["schema"]["pattern"] == r'^"[1-9][0-9]{0,18}"$'
    schemas = spec["components"]["schemas"]
    request = schemas["ReceiptVoidWrite"]
    assert request["additionalProperties"] is False
    assert set(request["required"]) == set(request["properties"])
    assert request["properties"]["confirmation"]["const"] == "VOID_RECORDED_CASH"
    for name in (
        "account_version",
        "portfolio_version",
        "broker_version",
        "expected_data_revision",
    ):
        assert request["properties"][name]["type"] == "string"
    response = schemas["ReceiptVoided"]
    assert response["additionalProperties"] is False
    for name, value in (
        ("status", "VOID"),
        ("version", "2"),
        ("audit_scope", "RECORDED_AUDIT_MATCHED_ONLY"),
        ("scope", "HISTORICAL_RECEIPT_VOID_RESPONSE_ONLY"),
        ("eligibility_scope", "NOT_ESTABLISHED"),
        ("payment_scope", "OWNER_RECORDED_NOT_VERIFIED"),
    ):
        assert response["properties"][name]["const"] == value


def test_direct_receipt_void_contract_binds_exact_original_scope_without_entitlement() -> None:
    spec = create_app().openapi()
    operation = spec["paths"]["/api/v1/dividends/direct-receipts/{id}/void"]["post"]
    assert operation["operationId"] == "void_direct_dividend_receipt"
    assert operation["security"] == [{"cookieAuth": []}]
    headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
    assert all(
        headers[name]["required"] for name in ("If-Match", "X-CSRF-Token", "Idempotency-Key")
    )
    assert headers["If-Match"]["schema"]["pattern"] == r'^"[1-9][0-9]{0,18}"$'
    schemas = spec["components"]["schemas"]
    for name in ("DirectReceiptVoidWrite", "DirectReceiptVoided"):
        model = schemas[name]
        assert model["additionalProperties"] is False
        assert set(model["required"]) == set(model["properties"])
        properties = model["properties"]
        assert "entitlement_id" not in properties
        for field in ("account_version", "portfolio_version", "broker_version"):
            assert properties[field]["type"] == "string"
        assert properties["original_revision_no"]["type"] == "integer"
    assert (
        schemas["DirectReceiptVoidWrite"]["properties"]["confirmation"]["const"]
        == "VOID_RECORDED_CASH"
    )
    for name, value in (
        ("receipt_basis", "DIRECT_EVENT_ACCOUNT"),
        ("status", "VOID"),
        ("version", "2"),
        ("scope", "HISTORICAL_DIRECT_RECEIPT_VOID_RESPONSE_ONLY"),
        ("audit_scope", "RECORDED_AUDIT_MATCHED_ONLY"),
        ("history_completeness", "UNVERIFIED"),
        ("current_use_scope", "REVALIDATION_REQUIRED"),
        ("eligibility_scope", "NOT_ESTABLISHED"),
        ("payment_scope", "OWNER_RECORDED_NOT_VERIFIED"),
    ):
        assert schemas["DirectReceiptVoided"]["properties"][name]["const"] == value


def test_direct_receipt_creation_contract_requires_explicit_cash_and_fence() -> None:
    spec = create_app().openapi()
    operation = spec["paths"]["/api/v1/dividends/direct-receipts"]["post"]
    assert operation["operationId"] == "create_direct_dividend_receipt"
    assert operation["security"] == [{"cookieAuth": []}]
    headers = {item["name"] for item in operation["parameters"] if item["in"] == "header"}
    assert {"X-CSRF-Token", "Idempotency-Key", "If-Match"} <= headers
    for name in ("DirectReceiptCreate", "DirectReceiptCreated"):
        schema = spec["components"]["schemas"][name]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "entitlement_id" not in schema["properties"]
        for field in ("account_version", "portfolio_version", "broker_version", "received_amount"):
            assert schema["properties"][field]["type"] == "string"
        for field in ("tax", "fee", "fx_rate_hkd", "actual_hkd"):
            assert {value["type"] for value in schema["properties"][field]["anyOf"]} == {
                "string",
                "null",
            }
    request = spec["components"]["schemas"]["DirectReceiptCreate"]["properties"]
    assert request["confirm_revision_fence"]["type"] == "boolean"
    assert request["confirmation"]["const"] == "RECORDED_CASH_ONLY"
    response = spec["components"]["schemas"]["DirectReceiptCreated"]["properties"]
    assert response["receipt_basis"]["const"] == "DIRECT_EVENT_ACCOUNT"
    assert response["scope"]["const"] == "HISTORICAL_DIRECT_RECEIPT_CREATION_RESPONSE_ONLY"
    assert response["payment_scope"]["const"] == "OWNER_RECORDED_NOT_VERIFIED"
    assert response["eligibility_scope"]["const"] == "NOT_ESTABLISHED"


def test_recorded_receipt_creation_contract_requires_explicit_scope_and_cash() -> None:
    spec = create_app().openapi()
    operation = spec["paths"]["/api/v1/dividends/receipts"]["post"]
    assert operation["operationId"] == "create_dividend_receipt"
    assert operation["security"] == [{"cookieAuth": []}]
    headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
    for name in ("If-Match", "X-CSRF-Token", "Idempotency-Key"):
        assert headers[name]["required"] is True
    assert headers["If-Match"]["schema"]["pattern"] == r'^"[1-9][0-9]{0,9}"$'
    schemas = spec["components"]["schemas"]
    request = schemas["ReceiptCreate"]
    assert request["additionalProperties"] is False
    assert set(request["required"]) == set(request["properties"])
    assert request["properties"]["confirmation"]["const"] == "RECORDED_CASH_ONLY"
    for name in (
        "account_version",
        "portfolio_version",
        "broker_version",
        "expected_data_revision",
    ):
        assert request["properties"][name]["type"] == "string"
    for name in ("tax", "fee", "fx_rate_hkd", "fx_source", "actual_hkd", "actual_hkd_reason"):
        assert {option["type"] for option in request["properties"][name]["anyOf"]} == {
            "string",
            "null",
        }
    response = schemas["ReceiptCreated"]
    assert response["additionalProperties"] is False
    for name, value in (
        ("scope", "HISTORICAL_RECEIPT_CREATION_RESPONSE_ONLY"),
        ("eligibility_scope", "NOT_ESTABLISHED"),
        ("payment_scope", "OWNER_RECORDED_NOT_VERIFIED"),
    ):
        assert response["properties"][name]["const"] == value


def test_unverified_trade_correction_contract_requires_scope_and_labels() -> None:
    spec = create_app().openapi()
    for action in ("revise", "void"):
        operation = spec["paths"][f"/api/v1/trades/{{id}}/unverified-corrections/{action}"]["post"]
        headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
        assert headers["If-Match"]["required"] is True
        assert headers["If-Match"]["schema"]["pattern"] == r'^"[1-9][0-9]{0,18}"$'
    schemas = spec["components"]["schemas"]
    for name, change in (
        ("TradeUnverifiedRevisionWrite", "revision"),
        ("TradeUnverifiedVoidWrite", "reason"),
    ):
        schema = schemas[name]
        assert set(schema["required"]) == {"confirmation", "reviews", change}
        assert schema["properties"]["confirmation"]["const"] == "UNVERIFIED_TRADE_CORRECTION_ONLY"
        assert schema["properties"]["reviews"]["minItems"] == 1
        assert schema["properties"]["reviews"]["maxItems"] == 100
        assert schema["additionalProperties"] is False
    pin = schemas["TradeCorrectionReviewPin"]["properties"]
    assert pin["review_revision"]["type"] == "string"
    assert pin["review_revision"]["pattern"] == r"^[1-9][0-9]{0,18}$"
    assert pin["gap"]["const"] == "MISSING_CREATION_AUDIT"
    record = schemas["TradeUnverifiedCorrectionRecord"]
    assert set(record["required"]) == {"authorization_id", "trade", "scope", "history_completeness"}
    assert record["properties"]["scope"]["const"] == "UNVERIFIED_TRADE_CORRECTION_ONLY"
    assert record["properties"]["history_completeness"]["const"] == "UNVERIFIED"
    scope = schemas["TradeCorrectionScopeStatus"]
    assert set(scope["required"]) == {
        "trade_id",
        "root_id",
        "trade_version",
        "trade_status",
        "gaps",
        "scope",
        "history_completeness",
    }
    assert scope["properties"]["gaps"]["maxItems"] == 100
    assert scope["properties"]["scope"]["const"] == "UNVERIFIED_TRADE_CORRECTION_ONLY"
    assert scope["properties"]["history_completeness"]["const"] == "UNVERIFIED"
    gap = schemas["TradeCorrectionScopeGap"]
    assert set(gap["required"]) == {
        "trade_id",
        "trade_version",
        "review_revision",
        "current_gap",
        "state",
    }
    assert gap["properties"]["review_revision"]["type"] == "string"
    assert set(gap["properties"]["state"]["enum"]) == {
        "NOT_REVIEWED",
        "ACKNOWLEDGED_UNVERIFIED",
        "WITHDRAWN",
        "EVIDENCE_CHANGED",
    }


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
