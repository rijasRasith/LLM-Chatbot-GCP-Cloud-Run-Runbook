import contextvars
import json
import logging
import sys

request_id_var: contextvars.ContextVar = contextvars.ContextVar("request_id", default=None)
trace_var: contextvars.ContextVar = contextvars.ContextVar("trace", default=None)


class CloudJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "request_id": request_id_var.get(),
        }
        trace = trace_var.get()
        if trace:
            payload["logging.googleapis.com/trace"] = trace
        extra_fields = getattr(record, "fields", None)
        if extra_fields:
            payload.update(extra_fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudJsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    logger.log(level, event, extra={"fields": {"event": event, **fields}})
