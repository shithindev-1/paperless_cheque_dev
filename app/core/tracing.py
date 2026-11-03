"""Per-request trace: one JSON record per API hit with timestamps, timings, counts and outcome.

The request middleware creates the trace and keeps it in a context variable, so services
add details (files, stages, cheques, retries, errors) without passing it around. When the
request ends the whole record is written as ONE line to logs/results/<date>.jsonl and
summarised in the app log, both keyed by `request_id` (also returned as X-Request-ID).

No secrets or names are recorded here: URLs are redacted, the API key is a fingerprint,
and cheques are summarised by amount / date / issue codes only.
"""

import contextvars
import hashlib
import json
import logging
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from app.core.timezones import IST_TZ, UAE_TZ

logger = logging.getLogger(__name__)

_current: contextvars.ContextVar[Optional["RequestTrace"]] = contextvars.ContextVar(
    "request_trace", default=None
)


def current_trace() -> Optional["RequestTrace"]:
    return _current.get()


def _stamp(moment: datetime) -> dict:
    return {"uae": moment.astimezone(UAE_TZ).isoformat(timespec="milliseconds"),
            "ist": moment.astimezone(IST_TZ).isoformat(timespec="milliseconds")}


def key_fingerprint(api_key: str) -> str:
    """Identifies which configured key was used without logging the key itself."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:10]


class RequestTrace:
    def __init__(self, method: str, path: str, client_ip: Optional[str], user_agent: Optional[str],
                 app: dict) -> None:
        self.request_id = uuid.uuid4().hex
        self._t0 = time.perf_counter()
        self.record: dict[str, Any] = {
            "request_id": self.request_id,
            "endpoint": f"{method} {path}",
            "type": None,
            "started_at": _stamp(datetime.now(timezone.utc)),
            "finished_at": None,
            "total_ms": None,
            "status_code": None,
            "outcome": None,
            "client": {"ip": client_ip, "user_agent": user_agent, "api_key_id": None},
            "app": app,
            "counts": {},
            "stages_ms": {},
            "usage": None,
            "files": [],
            "cheques_processed": [],
            "payments": [],
            "issues_summary": {},
            "events": [],
            "errors": [],
        }

    # ── lifecycle ──

    def activate(self) -> contextvars.Token:
        return _current.set(self)

    @staticmethod
    def deactivate(token: contextvars.Token) -> None:
        _current.reset(token)

    def finish(self, status_code: int) -> dict:
        self.record["finished_at"] = _stamp(datetime.now(timezone.utc))
        self.record["total_ms"] = self.elapsed_ms()
        self.record["status_code"] = status_code
        self.record["outcome"] = ("success" if status_code < 400
                                  else "client_error" if status_code < 500 else "server_error")
        return self.record

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    # ── details added by services ──

    def set(self, **fields: Any) -> None:
        self.record.update(fields)

    def count(self, **counts: int) -> None:
        self.record["counts"].update(counts)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time a processing stage; repeated stages add up."""
        start = time.perf_counter()
        try:
            yield
        finally:
            stages = self.record["stages_ms"]
            stages[name] = stages.get(name, 0) + int((time.perf_counter() - start) * 1000)

    def new_file(self, entry: Optional[dict] = None, **fields: Any) -> dict:
        """Add a file entry (an existing dict is added as-is, so later updates show up)."""
        entry = entry if entry is not None else {}
        entry.update(index=len(self.record["files"]), **fields)
        self.record["files"].append(entry)
        return entry

    def cheque_processed(self, **fields: Any) -> None:
        self.record["cheques_processed"].append(fields)

    def event(self, kind: str, **details: Any) -> None:
        self.record["events"].append({"at_ms": self.elapsed_ms(), "event": kind, **details})

    def error(self, kind: str, message: str, **details: Any) -> None:
        self.record["errors"].append({"at_ms": self.elapsed_ms(), "type": kind,
                                      "message": message, **details})

    # ── output ──

    def summary_line(self) -> str:
        """Compact one-line summary for the app log (full record is in the results log)."""
        r = self.record
        usage = r["usage"] or {}
        return json.dumps({
            "endpoint": r["endpoint"], "status": r["status_code"], "total_ms": r["total_ms"],
            "stages_ms": r["stages_ms"], "counts": r["counts"],
            "tokens": usage.get("total_tokens"), "issues": r["issues_summary"],
            "errors": len(r["errors"]), "retries": sum(e["event"] == "model_retry" for e in r["events"]),
        }, ensure_ascii=False)


def trace_file(file: Any) -> dict:
    """The trace entry for an UploadedFile (created on first use)."""
    trace = current_trace()
    if trace is None:
        return {}
    if "index" not in file.trace:
        trace.new_file(file.trace, name=file.name, mime_type=file.mime_type,
                       size_kb=round(len(file.data) / 1024, 1))
    return file.trace
