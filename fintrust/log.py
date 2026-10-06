"""Structured JSON logging.

Every log line is ONE JSON object written with print(). Lambda sends anything
printed to CloudWatch Logs automatically, and CloudWatch Logs Insights can then
filter on the JSON fields, for example:

    fields @timestamp, message, transaction_id
    | filter level = "ERROR" and service = "ingest"

POPIA rule for this project: never log customer names, ID numbers or full
account numbers. Log identifiers such as transaction_id instead.
"""

import json
import os
from datetime import datetime, timezone

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class Logger:
    """A tiny logger that always emits JSON.

    `service` is the module doing the logging (ingest, score, ...).
    `context` holds fields repeated on every line, such as the request id.
    """

    def __init__(self, service, **context):
        self.service = service
        self.context = context

    def bind(self, **context):
        """Return a new Logger that adds these fields to every line."""
        merged = {**self.context, **context}
        return Logger(self.service, **merged)

    def _write(self, level, message, fields):
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": level,
            "service": self.service,
            "env": os.environ.get("FINTRUST_ENV", "dev"),
            "message": message,
        }
        record.update(self.context)
        record.update(fields)
        # default=str turns Decimal, datetime and exceptions into text instead
        # of crashing the log call.
        print(json.dumps(record, default=str))

    def debug(self, message, **fields):
        if os.environ.get("FINTRUST_LOG_LEVEL", "INFO").upper() == "DEBUG":
            self._write("DEBUG", message, fields)

    def info(self, message, **fields):
        self._write("INFO", message, fields)

    def warning(self, message, **fields):
        self._write("WARNING", message, fields)

    def error(self, message, **fields):
        self._write("ERROR", message, fields)


def get_logger(service, **context):
    """Create a logger for one module, e.g. get_logger("ingest")."""
    return Logger(service, **context)
