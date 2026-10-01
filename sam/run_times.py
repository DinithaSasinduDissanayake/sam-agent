"""Read-only authoritative current-run timestamps for status and dashboards."""

import json
import os
from datetime import datetime, timezone


def parse_timestamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        if not isinstance(value, str) or not value:
            return None
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def read_run_result(entry):
    try:
        with open(entry.get("result_path"), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if data.get("agent_id") and data["agent_id"] != entry.get("id"):
                return {}
            if data.get("run_id") is not None and data["run_id"] != entry.get("run_id", 1):
                return {}
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {}


def run_started_at(entry):
    result = read_run_result(entry)
    dt = parse_timestamp(result.get("started_at"))
    if dt is not None:
        return dt
    dt = parse_timestamp(entry.get("run_started_at"))
    if dt is not None:
        return dt
    # Original creation cannot date a later run whose start was never recorded.
    if entry.get("run_id", entry.get("run_count", 1)) == 1:
        return parse_timestamp(entry.get("created_at"))
    return None


def run_ended_at(entry):
    return (parse_timestamp(read_run_result(entry).get("ended_at"))
            or parse_timestamp(entry.get("ended_at"))
            or parse_timestamp(entry.get("completed_at")))


def file_mtime(path):
    """UTC datetime of a file's mtime; None when missing/unreadable."""
    try:
        return datetime.fromtimestamp(os.stat(path).st_mtime, timezone.utc)
    except (OSError, TypeError, ValueError, OverflowError):
        return None


def run_end_evidence(entry):
    """Newest mtime of the run's output.log / result.json, else None.

    Offline evidence of when a run that has no recorded end last did
    anything. Used by doctor (and kill) for dead runs; never used for live
    runs and never by status/TUI DONE columns.
    """
    stamps = [file_mtime(entry.get(k)) for k in ("log_path", "result_path")
              if entry.get(k)]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None
