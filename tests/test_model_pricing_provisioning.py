"""Contributed model pricing flows ONLY through AP-MT's defaults operation,
and only when the platform advertises the capability."""
from __future__ import annotations

import json

import httpx

from agents_platform_runners_app.agent_provisioner import ENDPOINTS, AgentProvisioner

PRICING = {"revision": 3, "input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}
MODEL = {"slug": "codex-runner-gpt-6-1-sol", "provider": "runner", "model_id": "codex-runner-gpt-6-1-sol", "pricing": PRICING}


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
    assert put[1] == "/api/models/codex-runner-gpt-6-1-sol/pricing-defaults"
    assert put[2] == {"app_id": "agents-platform-runners", "revision": 3,
                      "prices": {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}}


def test_existing_model_still_gets_defaults_not_a_generic_put():
    p = Platform(existing=["codex-runner-gpt-6-1-sol"])
    prov(p).seed("agents-platform-runners", {"models": [MODEL]})
    puts = [c for c in p.calls if c[0] == "PUT"]
    assert [c[1] for c in puts] == ["/api/models/codex-runner-gpt-6-1-sol/pricing-defaults"]


def test_old_platform_without_capability_gets_no_prices_and_reconcile_is_not_acked():
    p = Platform(capable=False)
    pr = prov(p)
    pr.seed("agents-platform-runners", {"models": [MODEL]})
    assert not [c for c in p.calls if c[0] == "PUT"]
    # reconcile returns False so the workspace does not advance its baseline
    assert pr.update("models", "codex-runner-gpt-6-1-sol", {"pricing": PRICING}) is False


def test_reconcile_update_routes_pricing_and_acks():
    p = Platform()
    assert prov(p).update("models", "codex-runner-gpt-6-1-sol", {"pricing": PRICING}) is True
    assert [c[1] for c in p.calls if c[0] == "PUT"] == ["/api/models/codex-runner-gpt-6-1-sol/pricing-defaults"]


def test_reconcile_with_other_fields_never_forwards_pricing_in_generic_put():
    p = Platform()
    assert prov(p).update("models", "codex-runner-gpt-6-1-sol", {"display_name": "N", "pricing": PRICING}) is True
    generic = next(c for c in p.calls if c[0] == "PUT" and c[1] == "/api/models/codex-runner-gpt-6-1-sol")
    assert generic[2] == {"display_name": "N"}


def test_manifest_codex_models_are_priced_and_others_unset():
    import pathlib
    from agents_platform_runners_app.agent_provisioner import effective_pricing
    d = json.loads((pathlib.Path(__file__).parent.parent / "aw-app.json").read_text())
    for m in d["contributes"]["agents"]["models"]:
        if m["params"].get("cli") == "codex" or m["params"].get("runner") == "aw-codex":
            assert effective_pricing(m)["revision"] >= 2, m["slug"]
        elif m["slug"] not in PRICED_NON_CODEX_FLAGGED:
            assert "pricing" not in m, m["slug"]  # no invented tariffs


# openai-provider Codex-family models priced explicitly (see test_manifest_list_prices_*).
PRICED_NON_CODEX_FLAGGED = {"gpt-6-sol", "gpt-6-1-sol", "gpt-6-1-terra", "gpt-6-luna", "gpt-6-astra",
                            "openai-gpt-5-6-sol", "openai-gpt-5-6-luna", "openai-gpt-5-6-terra"}


# ---- review round 1 ----
def test_pricing_failure_does_not_withhold_ack_for_fields_that_landed():
    class P(Platform):
        def __call__(self, req):
            if req.method == "PUT" and req.url.path.endswith("/pricing-defaults"):
                return httpx.Response(500)
            return super().__call__(req)
    p = P()
    assert prov(p).update("models", MODEL["slug"], {"display_name": "B", "pricing": PRICING}) is True
    assert any(c[0] == "PUT" and c[1] == f"/api/models/{MODEL['slug']}" for c in p.calls)
    # pricing-only failure is still not acked (nothing landed; retried)
    assert prov(P()).update("models", MODEL["slug"], {"pricing": PRICING}) is False


def test_other_apps_models_never_get_defaults_under_runners_identity():
    p = Platform()
    other = {**MODEL, "slug": "someone-elses-model"}
    prov(p).seed("other-app", {"models": [other]})
    assert not [c for c in p.calls if c[1].endswith("/pricing-defaults")]
    assert prov(p).update("models", "someone-elses-model", {"pricing": PRICING}) is True
    assert not [c for c in p.calls if c[1].endswith("/pricing-defaults")]


def test_invalid_revision_does_not_abort_the_seed_pass():
    p = Platform()
    bad = {**MODEL, "pricing": {**PRICING, "revision": None}}
    second = {"slug": "after-it", "provider": "runner", "model_id": "after-it"}
    created = prov(p).seed("agents-platform-runners", {"models": [bad, second]})
    assert created.get("models") == 2
    assert not [c for c in p.calls if c[1].endswith("/pricing-defaults")]


def test_redirected_pricing_put_is_not_acknowledged():
    class P(Platform):
        def __call__(self, req):
            if req.method == "PUT" and req.url.path.endswith("/pricing-defaults"):
                return httpx.Response(307, headers={"location": "http://elsewhere/x"})
            return super().__call__(req)
    assert prov(P()).update("models", MODEL["slug"], {"pricing": PRICING}) is False


# ---- structural guarantee: no Codex model is ever unpriced -------------------

def _manifest_models():
    from pathlib import Path
    m = json.loads((Path(__file__).resolve().parents[1] / "aw-app.json").read_text())
    return m["contributes"]["agents"]["models"]


def test_every_registered_codex_model_has_a_pricing_default():
    from agents_platform_runners_app.agent_provisioner import effective_pricing, is_codex_model
    codex = [m for m in _manifest_models() if is_codex_model(m)]
    assert codex, "expected at least one codex model in the manifest"
    for m in codex:
        block = effective_pricing(m)
        assert block is not None, m["slug"]
        for f in ("input", "output", "cache_read", "cache_write"):
            assert f in block, (m["slug"], f)


def test_unpriced_codex_entry_gets_default_zero_block():
    p = Platform()
    entry = {"slug": "codex-runner-gpt-6-1-sol", "provider": "runner",
             "model_id": "x", "params": {"cli": "codex", "runner": "aw-codex"}}
    prov(p).seed("agents-platform-runners", {"models": [entry]})
    put = next(c for c in p.calls if c[0] == "PUT")
    assert put[1] == "/api/models/codex-runner-gpt-6-1-sol/pricing-defaults"
    assert put[2]["prices"] == {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}


def test_non_codex_entry_stays_unpriced():
    p = Platform()
    entry = {"slug": "claude-runner-sonnet", "provider": "runner", "model_id": "x",
             "params": {"cli": "claude"}}
    prov(p).seed("agents-platform-runners", {"models": [entry]})
    assert not [c for c in p.calls if c[0] == "PUT"]


def test_manifest_list_prices_for_codex_models():
    from pathlib import Path
    manifest = json.loads((Path(__file__).resolve().parents[1] / "aw-app.json").read_text(encoding="utf-8"))
    by = {m["slug"]: m.get("pricing") for m in manifest["contributes"]["agents"]["models"]}

    def p(i, o, cr, cw):
        return {"revision": 2, "input": i, "output": o, "cache_read": cr, "cache_write": cw}
    g5 = p("1.25", "10", "0.125", "0")
    sol6, sol61 = p("2", "10", "0.2", "2.5"), p("2", "10", "0.1", "2.5")
    terra61, astra, luna = p("1", "5", "0.05", "1.25"), p("10", "50", "1", "12.5"), p("0.1", "0.5", "0.01", "0.125")
    expected = {"codex-runner-gpt-5": g5, "codex-cli-gpt-5": g5,
                "gpt-6-sol": sol6, "codex-runner-gpt-6-sol": sol6,
                "gpt-6-1-sol": sol61, "codex-runner-gpt-6-1-sol": sol61,
                "gpt-6-1-terra": terra61, "codex-runner-gpt-6-1-terra": terra61,
                "gpt-6-astra": astra, "codex-runner-gpt-6-astra": astra,
                "gpt-6-luna": luna, "codex-runner-gpt-6-luna": luna,
                "openai-gpt-5-6-sol": p("4", "20", "0.4", "5"),
                "openai-gpt-5-6-terra": p("2", "12", "0.2", "2.5"),
                "openai-gpt-5-6-luna": p("0.2", "1.2", "0.02", "0.25")}
    for slug, price in expected.items():
        assert by[slug] == price, slug
    # no Codex-flagged model may be left at the $0 placeholder
    from agents_platform_runners_app.agent_provisioner import is_codex_model
    for m in manifest["contributes"]["agents"]["models"]:
        if is_codex_model(m):
            assert m["pricing"]["output"] != "0", m["slug"]
