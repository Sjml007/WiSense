"""Unit tests for structured logging setup and formatters."""

import io
import json
import logging

import pytest

from wisense_backend.config.app_config import AppConfig
from wisense_backend.logging_setup import JsonFormatter, TextFormatter, configure_logging


class TestLoggingSetup:
    """Validate configure_logging behavior and formatters."""

    def test_log_level_configuration(self) -> None:
        """Verify that logger level is updated according to AppConfig."""
        for level_str, expected_level in [
            ("DEBUG", logging.DEBUG),
            ("INFO", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
        ]:
            cfg = AppConfig(log_level=level_str)
            logger = configure_logging(cfg)
            assert logger.level == expected_level

    def test_text_formatting(self) -> None:
        """Verify standard text formatting format."""
        formatter = TextFormatter()
        record = logging.LogRecord(
            name="wisense_backend.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="System started successfully",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        assert "[INFO]" in output
        assert "[wisense_backend.test]" in output
        assert "System started successfully" in output

    def test_json_formatting_valid_json(self) -> None:
        """Verify JSON formatter produces valid JSON with required fields."""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="wisense_backend.pipeline",
            level=logging.WARNING,
            pathname="pipeline.py",
            lineno=42,
            msg="Subcarrier variance spike detected: %d",
            args=(105,),
            exc_info=None,
        )
        # Add custom extra attribute
        record.metric_id = "var_sc_12"  # type: ignore

        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "WARNING"
        assert parsed["logger"] == "wisense_backend.pipeline"
        assert parsed["message"] == "Subcarrier variance spike detected: 105"
        assert "timestamp" in parsed
        assert parsed["metric_id"] == "var_sc_12"

    def test_json_formatting_with_exception(self) -> None:
        """Verify JSON formatter handles exception tracebacks."""
        formatter = JsonFormatter()
        try:
            raise ValueError("Test error for logging")
        except ValueError:
            import sys

            record = logging.LogRecord(
                name="wisense_backend.error",
                level=logging.ERROR,
                pathname="error.py",
                lineno=99,
                msg="Operation failed",
                args=(),
                exc_info=sys.exc_info(),
            )

        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "ERROR"
        assert "exception" in parsed
        assert "ValueError: Test error for logging" in parsed["exception"]

    def test_configure_logging_idempotence(self) -> None:
        """Verify calling configure_logging repeatedly does not accumulate duplicate handlers."""
        cfg_text = AppConfig(log_format="text")
        cfg_json = AppConfig(log_format="json")

        logger = configure_logging(cfg_text)
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, TextFormatter)

        logger = configure_logging(cfg_json)
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, JsonFormatter)

    def test_stream_output_capture(self) -> None:
        """Verify that logged messages can be written to a stream handler."""
        stream = io.StringIO()
        cfg = AppConfig(log_format="json", log_level="INFO")
        logger = configure_logging(cfg)

        # Replace stream with capture stream
        logger.handlers[0].stream = stream
        logger.info("Heartbeat check", extra={"pulse_count": 5})

        log_content = stream.getvalue().strip()
        assert log_content
        parsed = json.loads(log_content)
        assert parsed["message"] == "Heartbeat check"
        assert parsed["pulse_count"] == 5
