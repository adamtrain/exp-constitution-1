import json
from datetime import datetime

from reflective_stability.claude_cli import classify, reset_time
from reflective_stability.runtime import append_jsonl, read_jsonl

MODEL = "claude-haiku-5-5"


def cli_result(**fields):
    result = {
        "is_error": False,
        "subtype": "success",
        "result": "an answer",
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 5,
        },
        "modelUsage": {MODEL: {}},
        "total_cost_usd": 0.01,
        "duration_ms": 1000,
    }
    return json.dumps({**result, **fields})


def test_genuine_answer():
    kind, reply = classify(MODEL, 0, cli_result(), "")
    assert kind == "answer" and reply.text == "an answer" and reply.output_tokens == 5


def test_error_reported_as_success_subtype_is_not_an_answer():
    # Observed: an unknown model returns subtype "success" with is_error true and HTTP 404.
    stdout = cli_result(
        is_error=True, api_error_status=404, modelUsage={}, result="There's an issue with the selected model"
    )
    assert classify(MODEL, 1, stdout, "")[0] == "fatal"


def test_usage_limit_text_without_model_usage_is_a_limit():
    stdout = cli_result(
        modelUsage={}, usage={"output_tokens": 0}, result="You've hit your session limit · resets 3:45pm"
    )
    assert classify(MODEL, 1, stdout, "")[0] == "limit"
    assert classify(MODEL, 1, cli_result(is_error=True, api_error_status=429, result="x"), "")[0] == "limit"


def test_unparseable_output_is_transient():
    assert classify(MODEL, 1, "", "network down")[0] == "transient"
    assert (
        classify(MODEL, 1, cli_result(is_error=True, api_error_status=529, result="Overloaded"), "")[0] == "transient"
    )


def test_reset_time_parsing():
    now = datetime(2026, 10, 14, 16, 0)  # a Wednesday
    assert reset_time("resets 3:45pm", now) == datetime(2026, 10, 15, 15, 45)  # already past today
    assert reset_time("resets 5pm", now) == datetime(2026, 10, 14, 17, 0)
    assert reset_time("resets Mon 12:00am", now) == datetime(2026, 10, 19, 0, 0)
    assert reset_time("no time here", now) is None


def test_jsonl_survives_unicode_line_separators_and_a_torn_last_line(tmp_path):
    path = tmp_path / "records.jsonl"
    append_jsonl(path, {"text": "line separator\u0085"})
    with path.open("a") as f:
        f.write('{"torn": ')
    assert read_jsonl(path) == [{"text": "line separator\u0085"}]
    append_jsonl(path, {"next": 1})
    assert read_jsonl(path) == [{"text": "line separator\u0085"}, {"next": 1}]
