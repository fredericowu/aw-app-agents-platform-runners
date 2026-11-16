import json

from agents_platform_runners_app.codex_usage import CodexTurnUsageCapture


def _row(kind, payload):
    return json.dumps({"type": kind, "payload": payload}) + "\n"


def test_capture_reads_only_appended_response_usage_and_terminal_error(tmp_path):
    session_id = "019abc"
    rollout = tmp_path / "sessions" / "2026" / "11" / f"rollout-now-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(_row("token_usage_record", {"usage": {"input_tokens": 999}}))

    capture = CodexTurnUsageCapture(tmp_path, session_id)
    with rollout.open("a") as fh:
        fh.write(_row("token_usage_record", {"usage": {
            "input_tokens": 120, "cached_input_tokens": 80,
            "output_tokens": 7, "reasoning_output_tokens": 3,
        }}))
        fh.write(_row("token_usage_record", {"usage": {
            "input_tokens": 140, "cached_input_tokens": 100,
            "output_tokens": 9, "reasoning_output_tokens": 4,
        }}))
        fh.write(_row("event_msg", {"type": "task_complete", "error": {
            "message": "You have 0 weighted tokens left",
            "codex_error_info": "usage_limit_exceeded",
        }}))

    event = capture.event()
    assert event["type"] == "turn.usage.delta"
    assert event["usage_records"] == 2
    assert event["usage"]["input_tokens"] == 260
    assert event["usage"]["cached_input_tokens"] == 180
    assert event["usage"]["output_tokens"] == 16
    assert event["context_tokens"] == 140
    assert event["error"]["codex_error_info"] == "usage_limit_exceeded"


def test_capture_learns_new_thread_from_public_stream(tmp_path):
    capture = CodexTurnUsageCapture(tmp_path)
    capture.observe_cli_line(json.dumps({"type": "thread.started", "thread_id": "fresh-id"}))
    rollout = tmp_path / "sessions" / "rollout-now-fresh-id.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(_row("token_usage_record", {"usage": {
        "input_tokens": 42, "output_tokens": 2,
    }}))

    event = capture.event()
    assert event["usage"]["input_tokens"] == 42
    assert event["context_tokens"] == 42


def test_capture_returns_none_without_new_records(tmp_path):
    rollout = tmp_path / "sessions" / "rollout-now-existing.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(_row("token_usage_record", {"usage": {"input_tokens": 10}}))
    capture = CodexTurnUsageCapture(tmp_path, "existing")

    assert capture.event() is None
