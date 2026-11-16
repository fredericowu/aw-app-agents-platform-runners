"""Vendor catalog verified with authenticated `agy 1.5.9 models` in its image."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agents_platform_runners_app.agent_provisioner import AgentProvisioner


MODEL_IDS = [
    f"claude-{family}-5-5-{effort}"
    for family in ("opus", "sonnet")
    for effort in ("low", "medium", "high")
] + ["gpt-oss-120b-medium"]


@pytest.mark.parametrize("model_id", MODEL_IDS)
def test_vendor_model_is_discoverable_through_provisioning(model_id):
    manifest = json.loads((Path(__file__).parents[1] / "aw-app.json").read_text())
    models = manifest["contributes"]["agents"]["models"]
    slug = f"agy-runner-{model_id}"
    declared = next((m for m in models if m["slug"] == slug), None)
    assert declared is not None, f"Missing Antigravity model: {model_id}"
    posted = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        posted.append(json.loads(request.content))
        return httpx.Response(201, json={})

    provisioner = AgentProvisioner(
        base="http://platform.test", token="test-token",
        transport=httpx.MockTransport(respond),
    )
    assert provisioner.seed("agents-platform-runners", {"models": [declared]}) == {"models": 1}
    assert len(posted) == 1
    model = posted[0]
    assert model["slug"] == slug
    assert model["provider"] == "runner"
    assert model["params"]["runner"] == "aw-agy"
    assert model["params"]["cli"] == "agy"
    assert model["params"]["model"] == model_id
    if model_id.startswith("gpt"):
        assert "GPT-OSS 120B" in model["display_name"]
