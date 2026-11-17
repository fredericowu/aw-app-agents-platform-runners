"""This app is the first one to both PROVIDE the agent-contribution surface
and USE it.

``plugin.py::register_contributed_agents`` is the provider side of
aw-workspace's protocol — every other app's ``contributes.agents`` is seeded
by *this* app. Declaring agents here means core hands this app's own
declaration back to this app's own provider during its own activation. That
works (the registry looks the provider up by protocol, not by app id, and a
declaration arriving before a provider is held and replayed), but it is a
path nothing exercised until 2026-08-21 — so the shape is pinned here rather
than left to be rediscovered by whoever breaks it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]

#: This file only pins the Telegram family's own shape. The manifest has
#: since grown other agent families (see test_seed_migration.py) that don't
#: share this family's MCP-gateway contract — skill_slugs/agent_config_slug
#: are how the Telegram agents reach tools, not a rule every agent follows.
TELEGRAM_AGENT_SLUGS = {"telegram-sonnet", "telegram-opus", "telegram-haiku",
                        "telegram-fable", "telegram-gpt-6-1-sol",
                        "telegram-flash", "telegram-hydrogen", "telegram-argon",
                        "telegram-antigravity-sonnet", "telegram-antigravity-opus"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((APP_DIR / "aw-app.json").read_text())


@pytest.fixture(scope="module")
def spec(manifest) -> dict:
    return manifest["contributes"]["agents"]


def test_declares_the_capability_the_contribution_needs(manifest):
    # Core rejects contributes.agents without it — the install fails rather
    # than silently dropping the declaration. Easy to miss here because this
    # app already had six other permissions before it ever contributed one.
    assert "agents:contribute" in manifest["permissions"]


def test_ships_the_telegram_family(spec):
    """Five model variants of one role, same contract, chosen by what the
    conversation is worth — the same shape as the Coder and QA families.

    They were platform-seeded rows until now: a workspace that installed no
    apps still got them, and a workspace whose platform stopped seeding would
    have lost its entire Telegram channel with nothing to say why.
    """
    slugs = {a["slug"] for a in spec["agents"]}
    assert TELEGRAM_AGENT_SLUGS <= slugs


def test_declares_the_gpt_6_family_for_the_codex_runner(spec):
    models = {model["slug"]: model for model in spec["models"]}
    expected = {
        "codex-runner-gpt-6-1-sol": "gpt-6.1-sol",
        "codex-runner-gpt-6-astra": "gpt-6-astra",
        "codex-runner-gpt-6-1-terra": "gpt-6.1-terra",
        "codex-runner-gpt-6-sol": "gpt-6-sol",
        "codex-runner-gpt-6-luna": "gpt-6-luna",
    }
    for slug, model_id in expected.items():
        model = models[slug]
        assert model["provider"] == "runner"
        assert model["params"] == {
            "runner": "aw-codex",
            "cli": "codex",
            "model": model_id,
            "dangerous_skip_permissions": True,
            "timeout_s": 900,
        }


def test_model_slugs_use_antigravity_not_the_cli_binary_name(spec):
    models = {model["slug"]: model for model in spec["models"]}
    assert not any(slug.startswith("agy-") for slug in models)
    assert all(not model["model_id"].startswith("agy-") for model in models.values())


def test_gpt_5_6_runner_models_are_retired(spec):
    models = {model["slug"]: model for model in spec["models"]}
    assert not any(slug.startswith("codex-runner-gpt-5-6-") for slug in models)


def test_declares_antigravity_runner_models(spec):
    models = {model["slug"]: model for model in spec["models"]}
    assert "antigravity-gemini-3-8-flash-high" in models
    model = models["antigravity-gemini-3-8-flash-high"]
    assert model["provider"] == "runner"
    assert model["params"] == {
        "runner": "aw-agy",
        "cli": "agy",
        "model": "gemini-3.8-flash-high",
        "dangerous_skip_permissions": True,
        "timeout_s": 900,
    }
    assert "antigravity-gemini-flash" in models
    for slug, model_id in (
        ("antigravity-gemini-4-hydrogen-high", "gemini-4-hydrogen-high"),
        ("antigravity-gemini-4-hydrogen-xhigh", "gemini-4-hydrogen-xhigh"),
        ("antigravity-gemini-4-hydrogen-medium", "gemini-4-hydrogen-medium"),
        ("antigravity-gemini-4-hydrogen-low", "gemini-4-hydrogen-low"),
        ("antigravity-gemini-4-argon-high", "gemini-4-argon-high"),
        ("antigravity-gemini-4-argon-medium", "gemini-4-argon-medium"),
        ("antigravity-gemini-4-argon-low", "gemini-4-argon-low"),
    ):
        assert slug in models
        assert models[slug]["provider"] == "runner"
        assert models[slug]["params"]["cli"] == "agy"
        assert models[slug]["params"]["runner"] == "aw-agy"
        assert models[slug]["params"]["model"] == model_id


def test_telegram_agents_are_workspace_qualified(spec):
    """Every Telegram agent must carry `workspace_qualified: true` so
    aw-workspace's `qualify_workspace_agents()` gives each workspace its own
    `[<workspace>] Telegram - ...` row instead of colliding on one shared slug.
    """
    for agent in spec["agents"]:
        if agent["slug"] not in TELEGRAM_AGENT_SLUGS:
            continue
        assert agent.get("workspace_qualified") is True, agent["slug"]


def test_every_agent_uses_the_shared_contract_and_prompt(spec):
    """One prompt file for all five. The live rows were byte-identical across
    the family before this app adopted them, and the contract that actually
    matters is the aw-agent-telegram skill — the prompt is a pointer to it.
    """
    for agent in spec["agents"]:
        if agent["slug"] not in TELEGRAM_AGENT_SLUGS:
            continue
        assert agent["skill_slugs"] == ["aw-agent-telegram"], agent["slug"]
        assert agent["system_prompt_file"] == "prompts/telegram.md", agent["slug"]


def test_the_skill_every_declared_agent_names_is_shipped_by_this_app(manifest, spec):
    """No dependency hop for these: this app ships aw-agent-telegram itself,
    so the agents and their contract install or fail together.
    """
    shipped = {s["id"] for s in manifest["contributes"]["skills"]}
    for agent in spec["agents"]:
        for slug in agent.get("skill_slugs") or []:
            assert slug in shipped, f"{agent['slug']} names {slug!r}, unshipped"


def test_referenced_files_exist(spec):
    for agent in spec["agents"]:
        ref = agent.get("system_prompt_file")
        if ref is not None:
            assert (APP_DIR / ref).is_file(), agent["slug"]


def test_every_agent_names_a_config_this_app_declares(spec):
    """`agent-config-aw-full` is also declared by aw-app-mobile, and that
    duplication is deliberate rather than an accident to clean up.

    Seeding is create-if-absent by slug, so two apps declaring the same
    config is harmless — whichever installs first creates it. Declaring it
    here is what makes THIS app installable on its own: pointing at a slug
    only aw-app-mobile ships would mean a workspace that wants Telegram but
    not the mobile app gets four agents referencing a config that does not
    exist, and a dangling agent_config_slug does not error — it produces an
    agent with no MCP surface, which reads as a broken model.

    If the two ever need to differ, the answer is two slugs, not one shared
    row edited by hand.
    """
    configs = {c["slug"] for c in spec["agent_configs"]}
    assert "agent-config-aw-full" in configs
    for agent in spec["agents"]:
        if agent["slug"] not in TELEGRAM_AGENT_SLUGS:
            continue
        assert agent["agent_config_slug"] in configs, agent["slug"]


def test_qa_gpt_is_a_persistent_codex_qa_with_workspace_tools(spec):
    agents = {agent["slug"]: agent for agent in spec["agents"]}
    qa = agents["qa-gpt"]
    assert qa["model_slug"] == "codex-runner-gpt-6-1-sol"
    assert qa["agent_config_slug"] == "devteam-config"
    assert qa["group_slug"] == "qas"
    assert qa["skill_slugs"] == ["aw-agent-qa"]
    assert qa["disable_harness_tools"] is True

    configs = {config["slug"]: config for config in spec["agent_configs"]}
    config = configs["devteam-config"]
    assert config["mcp_servers"] == ["aw-gateway"]
    assert config["permissions"] == {
        "workspace_access": True,
        "share_network": True,
        "github": True,
        "docker": True,
        "tmp_access": True,
    }

    groups = {group["slug"]: group for group in spec["groups"]}
    assert groups["qas"]["instructions_file"] == "prompts/qa-group.md"


def test_the_config_is_declared_by_reference_not_by_url(spec):
    """`mcp_servers: ["aw-gateway"]` makes the provisioner resolve the live
    URL and token from this workspace's own .mcp.json at activation. An
    inline connection dict would commit a credential and freeze an address
    — see the mcp_url_overrides comment in plugin.py for the incident that
    rule came from.
    """
    cfg = spec["agent_configs"][0]
    assert cfg["mcp_servers"] == ["aw-gateway"]
    assert "mcp_config" not in cfg
    blob = json.dumps(cfg)
    assert "http" not in blob, "a config declared by reference names no URL"


def _walk(value, path=""):
    """Every (key-path, scalar) pair in a nested declaration."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, value


def test_no_declaration_ships_a_credential(spec):
    """Same guard the other contributing apps carry — a token pasted into a
    manifest is public the moment the repo is.

    Checks KEYS, not a substring of the whole blob. The first version of this
    test searched the serialized declaration for the word "token" and failed
    on the config's own description, which says the token is resolved at
    activation and deliberately not committed — i.e. it flagged the sentence
    explaining why there is no credential. Prose is where you explain a rule;
    a key named `token` is where you break it.
    """
    for key_path, value in _walk(spec):
        leaf = key_path.rsplit(".", 1)[-1].split("[")[0].lower()
        assert leaf not in ("token", "secret", "api_key", "apikey",
                            "password", "authorization"), key_path
        if isinstance(value, str):
            assert not value.lower().startswith("bearer "), key_path


def test_no_declaration_ships_a_key_nothing_reads(spec):
    """Guard against manifest keys that nothing reads (e.g. a key left behind
    after its consumer was removed). Note `workspace_qualified` IS read by
    aw-workspace's `src/apps/agents.py::qualify_workspace_agents`.
    """
    dead_keys: set[str] = set()
    for key_path, _ in _walk(spec):
        leaf = key_path.rsplit(".", 1)[-1].split("[")[0]
        assert leaf not in dead_keys, (
            f"{key_path}: no code anywhere reads this — either wire it up or "
            "drop it, but do not leave it looking implemented")
