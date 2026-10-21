import json
import logging

from patchmanager.core.logging import JsonFormatter


def test_json_formatter_includes_extra_fields() -> None:
    record = logging.makeLogRecord(
        {"msg": "hello %s", "args": ("world",), "levelname": "INFO", "request_id": "abc"}
    )
    out = json.loads(JsonFormatter().format(record))
    assert out["msg"] == "hello world"
    assert out["request_id"] == "abc"
    assert "args" not in out


def test_json_formatter_renders_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.makeLogRecord({"msg": "failed", "exc_info": sys.exc_info()})
    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exc"]
