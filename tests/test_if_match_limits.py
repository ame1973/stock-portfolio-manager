"""Version headers are bounded before parsing without weakening specialized checks."""

from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from spm_api.candidate_routes import _version as candidate_version
from spm_api.config import Settings
from spm_api.decimal_types import INT64_MAX
from spm_api.errors import ApiError
from spm_api.main import create_app
from spm_api.security import matching_version
from spm_api.trade_routes import _physical_trade_version
from tests.helpers import create, database_snapshot, initialize, write_headers


@pytest.mark.parametrize(
    "value",
    ['"0"', '"01"', '"-1"', "1", '"١"', '"' + "9" * 20 + '"', '"' + "9" * 5000 + '"'],
    ids=["zero", "leading-zero", "negative", "unquoted", "unicode", "20-digits", "5000-digits"],
)
def test_direct_parser_rejects_invalid_or_oversized_versions(value: str) -> None:
    with pytest.raises(ApiError) as caught:
        matching_version(value)
    assert caught.value.status == 422 and caught.value.code == "INVALID_VERSION"


def test_direct_parser_and_specialized_versions_preserve_their_bounds() -> None:
    assert matching_version('"1"') == 1
    assert matching_version(f'"{INT64_MAX}"') == INT64_MAX
    assert _physical_trade_version(f'"{INT64_MAX}"') == INT64_MAX
    with pytest.raises(ApiError) as trade:
        _physical_trade_version(f'"{INT64_MAX + 1}"')
    assert trade.value.code == "INVALID_TRADE_VERSION"
    assert candidate_version('"2147483647"') == 2_147_483_647
    with pytest.raises(ApiError) as candidate:
        candidate_version('"2147483648"')
    assert candidate.value.code == "INVALID_CANDIDATE_VERSION"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "header",
    ['"' + "9" * 20 + '"', '"' + "9" * 5000 + '"'],
    ids=["20-digits", "5000-digits"],
)
async def test_oversized_version_is_a_private_422_without_writes(
    client: httpx.AsyncClient, engine: Engine, header: str
) -> None:
    await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic portfolio"})
    before = database_snapshot(engine)
    response = await client.patch(
        f"/api/v1/portfolios/{portfolio['id']}",
        json={"name": "Synthetic update"},
        headers={**write_headers(client), "If-Match": header},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.headers["cache-control"] == "no-store"
    assert header not in response.text
    assert database_snapshot(engine) == before


@pytest.mark.anyio
async def test_valid_stale_long_version_remains_412_without_writes(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic portfolio"})
    before = database_snapshot(engine)
    response = await client.patch(
        f"/api/v1/portfolios/{portfolio['id']}",
        json={"name": "Synthetic update"},
        headers={**write_headers(client), "If-Match": f'"{INT64_MAX}"'},
    )
    assert response.status_code == 412 and response.json()["code"] == "PRECONDITION_FAILED"
    assert database_snapshot(engine) == before


def test_common_version_header_contract_is_bounded(settings: Settings) -> None:
    paths: dict[str, Any] = create_app(settings).openapi()["paths"]
    for path in (
        "/api/v1/portfolios/{id}",
        "/api/v1/trades/{id}",
        "/api/v1/accounts/{id}/opening-cash",
        "/api/v1/source-schedules/{id}",
        "/api/v1/provider-profiles/{provider_id}",
    ):
        parameters = paths[path]["patch"]["parameters"]
        header = next(item for item in parameters if item["name"] == "If-Match")
        assert header["required"] is True
        assert header["schema"]["pattern"] == r'^"[1-9][0-9]{0,18}"$'
        assert header["schema"]["maxLength"] == 21
