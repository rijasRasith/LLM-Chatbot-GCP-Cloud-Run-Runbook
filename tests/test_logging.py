import json
import logging

from app.logging_config import CloudJsonFormatter


def test_json_formatter_has_severity_and_custom_fields():
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, "llm_retry", None, None)
    record.fields = {"event": "llm_retry", "attempt": 2}
    out = json.loads(CloudJsonFormatter().format(record))
    assert out["severity"] == "WARNING"
    assert out["event"] == "llm_retry" and out["attempt"] == 2
