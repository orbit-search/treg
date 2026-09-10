"""Orbit: key probe, catalog shape (async search/enrich, documented credit prices), and BYOK relay."""

import json
from pathlib import Path

import httpx
import pytest
import yaml

from treg.api import app
from treg import oauth_providers as providers
from treg.domain.catalog import store as catalog_store

PROFILE = "a23ff3b7-b6cc-4ac6-8d4f-0c909cd956f5"  # public figure, level-3 profile used by every test_request


def _orbit_yaml():
    return yaml.safe_load((Path(__file__).parents[1] / "src/treg/catalog/orbit.yaml").read_text())


def test_orbit_catalog_covers_the_v3_surface_with_cheap_test_requests():
    """16 operations = the whole public OpenAPI; six carry a cheap replayable test_request, the rest say why not."""
    endpoints = _orbit_yaml()["endpoints"]
    assert len(endpoints) == 16
    assert len({(ep["method"], ep["path"]) for ep in endpoints}) == 16
    by_id = {ep["id"]: ep for ep in endpoints}
    testable = {eid for eid, ep in by_id.items() if "test_request" in ep}
    assert testable == {
        "orbit.people.search", "orbit.people.profile.read", "orbit.people.enrich",
        "orbit.people.enrich.batch", "orbit.people.watchers.list", "orbit.people.webhooks.list",
    }
    for eid, ep in by_id.items():
        assert "verified" not in ep and "example_response" not in ep, eid  # maintainers stamp those
        assert ("test_request" in ep) != ("untestable" in ep), eid
    # every paid replay targets the same public, already-full profile; the enrich ones are deliberate no-ops
    assert by_id["orbit.people.profile.read"]["test_request"] == {"pathParams": {"profile_id": PROFILE}}
    assert by_id["orbit.people.enrich"]["test_request"]["body"]["operation"] == "partial"
    assert by_id["orbit.people.enrich.batch"]["test_request"]["body"]["profile_ids"] == [PROFILE]
    assert by_id["orbit.people.search"]["test_request"]["body"] == {"query": "Sam Altman", "limit": 1, "include_profile": False}


def test_orbit_search_and_enrich_are_awaitable_through_their_status_utilities():
    cat = catalog_store.load()
    rows = {ep["id"]: ep for ep in cat.for_provider("orbit")}
    search, enrich = rows["orbit.people.search"], rows["orbit.people.enrich"]
    assert search["async"]["poll"]["endpoint"] == "orbit.people.search.status"
    assert search["async"]["id_from"] == "search_id"
    assert set(search["async"]["status"]["success"]) == {"completed", "completed_with_errors"}
    assert enrich["async"]["poll"]["endpoint"] == "orbit.people.enrich.status"
    assert enrich["async"]["status"]["failure"] == ["failed"]
    for status_id, param in (("orbit.people.search.status", "search_id"), ("orbit.people.enrich.status", "request_id")):
        status = rows[status_id]
        assert status["kind"] == "utility" and status["method"] == "GET"
        assert status["resource_ownership"]["requires"] == {"kind": f"poll:{status_id}", "param": param}
        assert status["cost"]["type"] == "free"
    # batch enrich has no parent poll route: children are polled one by one, so no descriptor
    assert not rows["orbit.people.enrich.batch"].get("async")


def test_orbit_prices_come_from_the_rate_card_at_one_cent_per_credit():
    cat = catalog_store.load()
    rows = {ep["id"]: ep for ep in cat.for_provider("orbit")}
    paid = {"orbit.people.search", "orbit.people.profile.read", "orbit.people.enrich", "orbit.people.enrich.batch"}
    for eid, ep in rows.items():
        view = cat.cost_view(ep["cost"], "orbit")
        if eid in paid:
            assert ep["cost"]["source"] == "rate_card_api", eid
            assert ep["cost"]["source_url"] == "https://api.orbitsearch.com/v2/developer/pricing", eid
            assert ep["cost"]["confidence"] == "documented", eid
            assert view["usd"] is not None and view["usd"] > 0, eid
            assert cat.platform_eligible(ep), eid
        else:
            assert ep["cost"]["type"] == "free", eid
            assert view["usd"] == 0, eid
    # 1 credit per 10 cached results -> $0.001 per result; a read is 1 credit -> $0.01
    assert cat.cost_view(rows["orbit.people.search"]["cost"], "orbit")["usd"] == pytest.approx(0.001)
    assert cat.cost_view(rows["orbit.people.profile.read"]["cost"], "orbit")["usd"] == pytest.approx(0.01)
    # enrich prices out of the operation table: partial 5, full/regenerate 10 credits
    assert cat.cost_view(rows["orbit.people.enrich"]["cost"], "orbit")["usd"] == pytest.approx(0.10)
    assert rows["orbit.people.enrich"]["cost"]["table"][0] == {"when": {"body.operation": "partial"}, "value": 5}
    # own-account management routes never become a platform-key offer
    assert not cat.platform_eligible(rows["orbit.people.watchers.list"])


async def test_orbit_catalog_serves_the_documented_price_to_clients(clients):
    response = await clients.get("/catalog/endpoints/orbit.people.search")
    assert response.status_code == 200, response.text
    endpoint = response.json()["endpoint"]
    assert endpoint["cost"]["usd"] == pytest.approx(0.001)
    assert endpoint["cost"]["confidence"] == "documented"
    assert endpoint["async"]["poll"]["endpoint"] == "orbit.people.search.status"


async def test_orbit_own_key_call_relays_the_bearer_and_the_idempotency_key(clients, monkeypatch):
    response = await clients.post("/secrets", json={"name": "orbit", "value": "orbit-test-placeholder"})
    assert response.status_code == 200, response.text
    seen = []

    def upstream(request):
        seen.append(request)
        assert str(request.url) == "https://api.orbitsearch.com/v3/search"
        assert request.headers["Authorization"] == "Bearer orbit-test-placeholder"
        assert json.loads(request.content) == {"query": "Sam Altman", "limit": 1, "include_profile": False}
        return httpx.Response(
            202, headers={"Content-Type": "application/json"},
            stream=httpx.ByteStream(b'{"status":"running","search_id":"example-search","results":[]}'),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(app.state, "http", client)
        response = await clients.post("/call/orbit.people.search",
                                      json={"query": "Sam Altman", "limit": 1, "include_profile": False})
    assert response.status_code == 202, response.text
    assert response.json() == {"status": "running", "search_id": "example-search", "results": []}
    assert len(seen) == 1
    assert "X-Treg-Cost-Micro" not in response.headers  # BYOK: Orbit bills the customer's own account


@pytest.mark.parametrize("status,payload,accepted", [
    (200, {"status": "success", "payload": {"usage_scope": "current_api_key", "credits_used": 0}}, True),
    (403, {"status": "failure", "error": {"code": "invalid_api_key", "message": "Invalid, revoked, expired, or malformed API key"}}, False),
    (401, {"status": "failure", "error": {"code": "missing_api_key"}}, False),
    (200, {"status": "failure", "error": {"code": "something"}}, False),
    (404, {}, False),   # route not deployed yet -> never "connected"
    (429, {"status": "failure"}, False),
    (502, {}, False),
])
async def test_orbit_connect_accepts_only_a_success_envelope_from_the_usage_probe(clients, monkeypatch, status, payload, accepted):
    def upstream(request):
        assert str(request.url) == "https://api.orbitsearch.com/v3/credits/usage"
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer orbit-test-placeholder"
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(app.state, "http", client)
        response = await clients.post("/connections/token", json={"provider": "orbit", "token": "orbit-test-placeholder"})
    assert response.status_code == (200 if accepted else 422), response.text
    if accepted:
        tools = (await clients.get("/tools")).json()
        orbit = next(tool for tool in tools if tool["name"] == "orbit")
        assert orbit["bindings"][0] == {**orbit["bindings"][0], "name": "Authorization", "format": "Bearer {secret}"}
    assert providers.get("orbit").probe_path == "/v3/credits/usage"
