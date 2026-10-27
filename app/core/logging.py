"""Logging setup.

    logs/app/YYYY-MM-DD.log       application log (UAE + IST time, token usage, errors)
    logs/results/YYYY-MM-DD.jsonl one JSON line per request with the full result

Files are named by the UAE date and roll over at UAE midnight.
"""

import json
import logging
from pathlib import Path
from typing import Optional, TextIO

from app.core.timezones import IST_TZ, UAE_TZ, format_timestamp, now_ist, now_uae

RESULTS_LOGGER_NAME = "cheque.results"

APP_LOG_FORMAT = (
    "%(uae_time)s UAE | %(ist_time)s IST | %(levelname)-8s | %(name)s | %(message)s"
)


class DualTimezoneFormatter(logging.Formatter):
    """Adds `uae_time` and `ist_time` to every record."""

    def format(self, record: logging.LogRecord) -> str:
        record.uae_time = format_timestamp(record.created, UAE_TZ)
        record.ist_time = format_timestamp(record.created, IST_TZ)
        return super().format(record)


class DailyFileHandler(logging.Handler):
    """Writes to `<directory>/<UAE date><suffix>`, switching files at UAE midnight."""

    def __init__(self, directory: Path, suffix: str) -> None:
        super().__init__()
        self.directory = directory
        self.suffix = suffix
        self.directory.mkdir(parents=True, exist_ok=True)
        self._date: Optional[str] = None
        self._stream: Optional[TextIO] = None

    def _get_stream(self) -> TextIO:
        today = now_uae().strftime("%Y-%m-%d")
        if today != self._date or self._stream is None:
            if self._stream:
                self._stream.close()
            path = self.directory / f"{today}{self.suffix}"
            self._stream = open(path, "a", encoding="utf-8")
            self._date = today
        return self._stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = self._get_stream()
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with self.lock:
            if self._stream:
                self._stream.close()
                self._stream = None
        super().close()


def setup_logging(level: str = "INFO", logs_dir: str = "logs") -> None:
    base = Path(logs_dir)
    formatter = DualTimezoneFormatter(APP_LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    app_file = DailyFileHandler(base / "app", ".log")
    app_file.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers = [console, app_file]

    results = logging.getLogger(RESULTS_LOGGER_NAME)
    results_file = DailyFileHandler(base / "results", ".jsonl")
    results_file.setFormatter(logging.Formatter("%(message)s"))
    results.handlers = [results_file]
    results.setLevel(logging.INFO)
    results.propagate = False

    # Keep third-party HTTP noise out of the app log
    for noisy in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_result(payload: dict) -> None:
    """Append one request's result as a JSON line to today's results log."""
    record = {
        "timestamp_uae": now_uae().isoformat(timespec="seconds"),
        "timestamp_ist": now_ist().isoformat(timespec="seconds"),
        **payload,
    }
    logging.getLogger(RESULTS_LOGGER_NAME).info(
        json.dumps(record, ensure_ascii=False, default=str)
    )
