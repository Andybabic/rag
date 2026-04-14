"""Tests for structured JSON logging."""

import json
import logging

import pytest
from shared.logging import (
    JSONFormatter,
    get_logger,
    log_extra,
    request_id_ctx,
    service_name_ctx,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _reset_logging():
    """Reset logging state after each test."""
    yield
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)


def _capture_line(capfd, logger: logging.Logger, msg: str, **kwargs) -> dict:
    """Emit one log line and return the parsed JSON."""
    if kwargs:
        log_extra(logger, logging.INFO, msg, **kwargs)
    else:
        logger.info(msg)
    out = capfd.readouterr().out.strip()
    return json.loads(out)


def test_json_format_basic(capfd):
    service_name_ctx.set("test-service")
    request_id_ctx.set("")
    setup_logging("test-service", "INFO")

    entry = _capture_line(capfd, get_logger("test"), "hello world")

    assert entry["level"] == "INFO"
    assert entry["service"] == "test-service"
    assert entry["message"] == "hello world"
    assert entry["request_id"] is None
    assert entry["extra"] == {}
    assert "timestamp" in entry


def test_request_id_in_log(capfd):
    setup_logging("test-service", "INFO")
    request_id_ctx.set("req-abc-123")

    entry = _capture_line(capfd, get_logger("test"), "with id")
    assert entry["request_id"] == "req-abc-123"


def test_extra_fields(capfd):
    setup_logging("test-service", "INFO")
    request_id_ctx.set("")
    logger = get_logger("test")

    entry = _capture_line(
        capfd, logger, "processed file", file_name="doc.pdf", duration_ms=42
    )
    assert entry["extra"]["file_name"] == "doc.pdf"
    assert entry["extra"]["duration_ms"] == 42


def test_exception_in_extra(capfd):
    setup_logging("test-service", "INFO")
    request_id_ctx.set("")
    logger = get_logger("test")

    try:
        raise ValueError("boom")
    except ValueError:
        logger.error("failed", exc_info=True, extra={"extra_data": {}})

    out = capfd.readouterr().out.strip()
    entry = json.loads(out)
    assert entry["level"] == "ERROR"
    assert "ValueError: boom" in entry["extra"]["exception"]


def test_setup_logging_sets_level():
    setup_logging("svc", "WARNING")
    root = logging.getLogger()
    assert root.level == logging.WARNING


def test_get_logger_returns_named_logger():
    logger = get_logger("my.module")
    assert logger.name == "my.module"


def test_json_formatter_standalone():
    service_name_ctx.set("fmt-test")
    request_id_ctx.set("rid-1")
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    line = formatter.format(record)
    entry = json.loads(line)
    assert entry["message"] == "hello world"
    assert entry["service"] == "fmt-test"
    assert entry["request_id"] == "rid-1"
