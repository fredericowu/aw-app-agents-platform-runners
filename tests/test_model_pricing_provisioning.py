"""Contributed model pricing flows ONLY through AP-MT's defaults operation,
and only when the platform advertises the capability."""
from __future__ import annotations

import json

import httpx

from agents_platform_runners_app.agent_provisioner import ENDPOINTS, AgentProvisioner

PRICING = {"revision": 3, "input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}
MODEL = {"slug": "codex-x", "provider": "runner", "model_id": "codex-x", "pricing": PRICING}


class Platform:
    def __init__(self, capable=True, existing=()):
        self.capable, self.existing = capable, list(existing)
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body))
        if req.method == "GET" and req.url.path == "/api/models/capabilities":
            return httpx.Response(200, json={"model_pricing": 1}) if self.capable \
                else httpx.Response(405)
        if req.method == "GET" and req.url.path == "/api/models":
            return httpx.Response(200, json=[{"slug": s} for s in self.existing])
        if req.method == "GET":
            return httpx.Response(200, json={"slug": req.url.path.rsplit("/", 1)[-1]})
        return httpx.Response(200, json={})


def prov(p):
    return AgentProvisioner("http://ap", "t", transport=httpx.MockTransport(p))


def test_price_keys_are_not_generic_model_fields():
    allowed = ENDPOINTS["models"][1]
    assert "pricing" not in allowed and not any(k.startswith("price_") for k in allowed)


def test_create_sends_no_prices_in_post_then_applies_defaults():
    p = Platform()
    prov(p).seed("agents-platform-runners", {"models": [MODEL]})
    post = next(c for c in p.calls if c[0] == "POST" and c[1] == "/api/models")
    assert "pricing" not in post[2] and not any(k.startswith("price_") for k in post[2])
    put = next(c for c in p.calls if c[0] == "PUT")
    assert put[1] == "/api/models/codex-x/pricing-defaults"
    assert put[2] == {"app_id": "agents-platform-runners", "revision": 3,
                      "prices": {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}}


def test_existing_model_still_gets_defaults_not_a_generic_put():
    p = Platform(existing=["codex-x"])
    prov(p).seed("agents-platform-runners", {"models": [MODEL]})
    puts = [c for c in p.calls if c[0] == "PUT"]
    assert [c[1] for c in puts] == ["/api/models/codex-x/pricing-defaults"]


def test_old_platform_without_capability_gets_no_prices_and_reconcile_is_not_acked():
    p = Platform(capable=False)
    pr = prov(p)
    pr.seed("agents-platform-runners", {"models": [MODEL]})
    assert not [c for c in p.calls if c[0] == "PUT"]
    # reconcile returns False so the workspace does not advance its baseline
    assert pr.update("models", "codex-x", {"pricing": PRICING}) is False


def test_reconcile_update_routes_pricing_and_acks():
    p = Platform()
    assert prov(p).update("models", "codex-x", {"pricing": PRICING}) is True
    assert [c[1] for c in p.calls if c[0] == "PUT"] == ["/api/models/codex-x/pricing-defaults"]


def test_reconcile_with_other_fields_never_forwards_pricing_in_generic_put():
    p = Platform()
    assert prov(p).update("models", "codex-x", {"display_name": "N", "pricing": PRICING}) is True
    generic = next(c for c in p.calls if c[0] == "PUT" and c[1] == "/api/models/codex-x")
    assert generic[2] == {"display_name": "N"}


def test_manifest_codex_models_are_explicit_zero_and_others_unset():
    import pathlib
    d = json.loads((pathlib.Path(__file__).parent.parent / "aw-app.json").read_text())
    for m in d["contributes"]["agents"]["models"]:
        if m["params"].get("cli") == "codex":
            assert m["pricing"] == PRICING | {"revision": 1}, m["slug"]
        else:
            assert "pricing" not in m, m["slug"]  # no invented tariffs
