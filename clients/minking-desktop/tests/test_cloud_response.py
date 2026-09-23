import json

from minking_desktop.cloud_response import collect_response


def event(payload):
    return "data: " + json.dumps(payload) + "\n\n"


def test_empty_terminal_keeps_actual_text_and_usage():
    item = {"type": "message", "content": [{"type": "output_text", "text": "你好"}]}
    stream = event({"type": "response.output_item.done", "output_index": 0, "item": item})
    stream += event({"type": "response.completed", "response": {"output": [], "usage": {"total_tokens": 7}}})
    status, result = collect_response(stream)
    assert status == 200
    assert result["output"] == [item]
    assert result["usage"]["total_tokens"] == 7


def test_failed_and_truncated_streams_are_not_success():
    status, result = collect_response(event({"type": "response.failed", "response": {"error": {"message": "Input must be a list"}}}))
    assert status == 502
    assert result["error"]["message"] == "Input must be a list"
    assert collect_response(event({"type": "response.created"}))[0] == 502
    assert collect_response("data: broken\n\n")[0] == 502


def test_terminal_output_is_not_duplicated():
    item = {"type": "message", "content": []}
    status, result = collect_response(event({"type": "response.output_item.done", "item": item}) + event({"type": "response.completed", "response": {"output": [item]}}))
    assert status == 200 and result["output"] == [item]
