from pathlib import Path

import pytest

from agents_platform_runners_app.model_provenance import codex_config_model


@pytest.mark.parametrize("contents", [b'model = ["test-secret"', b'# test-secret\xff'])
def test_malformed_codex_config_warns_without_exposing_details(tmp_path, caplog, contents):
    config_path = tmp_path / "private-config.toml"
    config_path.write_bytes(contents)

    assert codex_config_model(config_path, tmp_path) is None
    assert caplog.messages == ["Omitting Codex model attribution: malformed TOML config"]
    assert caplog.records[0].exc_info is None


def test_unreadable_codex_config_warns_without_exposing_details(tmp_path, caplog, monkeypatch):
    config_path = tmp_path / "private-config.toml"

    def deny_open(path, *args, **kwargs):
        assert path == config_path
        raise PermissionError("test-secret", str(path))

    monkeypatch.setattr(Path, "open", deny_open)

    assert codex_config_model(config_path, tmp_path) is None
    assert caplog.messages == ["Omitting Codex model attribution: unreadable config"]
    assert caplog.records[0].exc_info is None


@pytest.mark.parametrize("contents", [None, 'other = "test-secret"', 'model = "sk-test-secret"'])
def test_unknown_codex_model_is_omitted_quietly(tmp_path, caplog, contents):
    config_path = tmp_path / "config.toml"
    if contents is not None:
        config_path.write_text(contents, encoding="utf-8")

    assert codex_config_model(config_path, tmp_path) is None
    assert caplog.messages == []


def test_valid_codex_model_is_attributed_quietly(tmp_path, caplog):
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "gpt-5.6-sol"', encoding="utf-8")

    assert codex_config_model(config_path, tmp_path) == "gpt-5.6-sol"
    assert caplog.messages == []
