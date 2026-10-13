import io
import json
import logging

from app.logging_config import (
    RAW_PAYLOAD_REDACTED,
    REDACTED,
    SafeError,
    configure_logging,
    correlation_id,
    get_correlation_id,
    redact,
)

FIXTURE_SECRET = "fixture-secret-do-not-log"


def test_recursive_redaction_covers_sensitive_keys_urls_headers_and_raw_payload() -> None:
    value = {
        "api_key": FIXTURE_SECRET,
        "headers": {"Authorization": f"Bearer {FIXTURE_SECRET}"},
        "url": f"https://example.invalid/rpc?api-key={FIXTURE_SECRET}&safe=yes",
        "raw_payload": {"nested": FIXTURE_SECRET},
        "items": [{"password": FIXTURE_SECRET}],
        "safe": "ordinary-value",
    }

    redacted = redact(value)
    encoded = json.dumps(redacted)

    assert FIXTURE_SECRET not in encoded
    assert redacted["api_key"] == REDACTED
    assert redacted["headers"]["Authorization"] == REDACTED
    assert redacted["raw_payload"] == RAW_PAYLOAD_REDACTED
    assert "safe=yes" in redacted["url"]
    assert redacted["safe"] == "ordinary-value"


def test_explicit_secret_values_are_removed_from_free_text() -> None:
    assert FIXTURE_SECRET not in redact(
        f"provider call failed with value={FIXTURE_SECRET}", secrets=[FIXTURE_SECRET]
    )


def test_correlation_id_is_scoped_and_restored() -> None:
    assert get_correlation_id() is None
    with correlation_id("request-123") as current:
        assert current == "request-123"
        assert get_correlation_id() == "request-123"
    assert get_correlation_id() is None


def test_safe_error_does_not_retain_original_exception_text() -> None:
    try:
        raise RuntimeError(FIXTURE_SECRET)
    except RuntimeError as original:
        error = SafeError.from_exception("provider_timeout", original)

    assert FIXTURE_SECRET not in str(error)
    assert error.as_log_fields() == {
        "error_code": "provider_timeout",
        "error_message": "operation failed",
        "retryable": False,
    }


def test_json_logs_never_contain_fixture_secret_or_raw_payload() -> None:
    output = io.StringIO()
    logger = logging.getLogger("test.t004")
    configure_logging(level=logging.INFO, secrets=[FIXTURE_SECRET], logger=logger, stream=output)

    with correlation_id("request-456"):
        logger.info(
            "provider request %s",
            FIXTURE_SECRET,
            extra={
                "log_fields": {
                    "authorization": FIXTURE_SECRET,
                    "raw_payload": {"secret": FIXTURE_SECRET},
                    "slot": 42,
                }
            },
        )

    record = json.loads(output.getvalue())
    assert FIXTURE_SECRET not in output.getvalue()
    assert record["correlation_id"] == "request-456"
    assert record["message"] == "provider request [REDACTED]"
    assert record["authorization"] == REDACTED
    assert record["raw_payload"] == RAW_PAYLOAD_REDACTED
    assert record["slot"] == 42


def test_configured_json_logging_is_safe_for_exceptions() -> None:
    output = io.StringIO()
    logger = logging.getLogger("test.t004.exception")
    configure_logging(level=logging.INFO, logger=logger, stream=output)
    try:
        raise ValueError(FIXTURE_SECRET)
    except ValueError:
        logger.exception("provider operation failed")

    record = json.loads(output.getvalue())
    assert FIXTURE_SECRET not in output.getvalue()
    assert record["exception_type"] == "ValueError"
