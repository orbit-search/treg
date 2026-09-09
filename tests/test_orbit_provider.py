"""Orbit's key probe, public catalog coverage, and fail-closed setup contract."""

import json
from pathlib import Path

import httpx
import pytest
import yaml

from treg.api import app
from treg import oauth_providers as providers
from treg.domain.catalog import store as catalog_store


BILLING_NOTICE = (
    "Orbit uses usage-based credits. Customers connect their own Orbit API key, "
    "and Orbit applies charges under their account plan."
)


def test_orbit_catalog_covers_v3_without_claiming_live_verification():
    """Publish all current operations without treating untested data as verified."""
    source = Path(__file__).parents[1] / "src/treg/catalog/orbit.yaml"
    catalog = yaml.safe_load(source.read_text())
    endpoints = catalog["endpoints"]
    assert len(endpoints) == 16
    assert len({(ep["method"], ep["path"]) for ep in endpoints}) == 16
    for ep in endpoints:
        assert ep["untestable"]
        assert "verified" not in ep
        assert "test_request" not in ep
    by_id = {ep["id"]: ep for ep in endpoints}
    assert by_id["orbit.people.profile.read"]["method"] == "GET"
    assert by_id["orbit.people.enrich"]["method"] == "POST"
    batch = by_id["orbit.people.enrich.batch"]["input"]["body"]["profile_ids"]
    assert (batch["minItems"], batch["maxItems"]) == (1, 20)
    assert by_id["orbit.people.watchers.create"]["scope"] == "own_account"
    assert by_id["orbit.people.webhooks.test"]["cost"]["value"] is None
    assert providers.get("orbit").setup_url == "https://developer.orbitsearch.com/"


def test_orbit_prices_are_unknown_not_free_or_platform_eligible():
    """Unknown Orbit charges must not become a zero-price shared-key offer."""
    catalog = catalog_store.load()
    endpoints = [ep for ep in catalog.endpoints if ep["provider"] == "orbit"]
    assert len(endpoints) == 16
    for endpoint in endpoints:
        cost = catalog.cost_view(endpoint["cost"], "orbit")
        assert cost["type"] != "free", endpoint["id"]
        assert cost["value"] is None, endpoint["id"]
        assert cost["usd"] is None, endpoint["id"]
        assert cost["confidence"] == "unknown", endpoint["id"]
        assert not catalog.platform_eligible(endpoint), endpoint["id"]
    assert BILLING_NOTICE in providers.get("orbit").setup_note


async def test_orbit_catalog_serves_unknown_price_to_clients(clients):
    """The published search detail must not advertise Orbit as free."""
    response = await clients.get("/catalog/endpoints/orbit.people.search")
    assert response.status_code == 200, response.text
    endpoint = response.json()["endpoint"]
    assert endpoint["cost"]["usd"] is None
    assert endpoint["cost"]["confidence"] == "unknown"
    assert endpoint["platform_eligible"] is False
    assert BILLING_NOTICE in endpoint["cost"]["note"]


async def test_orbit_own_key_calls_remain_available_without_a_treg_price(clients, monkeypatch):
    """An unknown catalog rate must not block BYOK or imply a free upstream call."""
    response = await clients.post("/secrets", json={
        "name": "orbit", "value": "orbit-test-placeholder",
    })
    assert response.status_code == 200, response.text
    seen = []

    def upstream(request):
        seen.append(request)
        assert str(request.url) == "https://api.orbitsearch.com/v3/search"
        assert request.headers["Authorization"] == "Bearer orbit-test-placeholder"
        assert json.loads(request.content) == {"query": "example", "limit": 1}
        return httpx.Response(
            200, headers={"Content-Type": "application/json"},
            stream=httpx.ByteStream(b'{"status":"running","search_id":"example-search"}'),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(app.state, "http", client)
        response = await clients.post("/call/orbit.people.search", json={"query": "example", "limit": 1})
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "running", "search_id": "example-search"}
    assert len(seen) == 1
    assert "X-Treg-Cost-Micro" not in response.headers


@pytest.mark.parametrize("status,payload,accepted", [
    (400, {"status": "failed", "error": {"code": "developer_deep_search_input_required"}}, True),
    (403, {"status": "failure", "error": {"code": "invalid_api_key"}}, False),
    (200, {"status": "failed"}, False),
    (400, {}, False),
    (404, {"status": "failed"}, False),
    (429, {"status": "failed"}, False),
    (500, {"status": "failed"}, False),
    (502, {}, False),
])
async def test_orbit_connect_accepts_only_the_validation_probe(clients, monkeypatch, status, payload, accepted):
    """A gateway response must not mark a customer credential as connected."""
    def upstream(request):
        assert str(request.url) == "https://api.orbitsearch.com/v3/search"
        assert request.method == "POST"
        assert json.loads(request.content) == {}
        assert request.headers["Authorization"] == "Bearer orbit-test-placeholder"
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(app.state, "http", client)
        response = await clients.post("/connections/token", json={
            "provider": "orbit", "token": "orbit-test-placeholder",
        })
    assert response.status_code == (200 if accepted else 422), response.text
    if accepted:
        tools = (await clients.get("/tools")).json()
        orbit = next(tool for tool in tools if tool["name"] == "orbit")
        assert orbit["bindings"][0]["format"] == "Bearer {secret}"
        assert orbit["bindings"][0]["name"] == "Authorization"
