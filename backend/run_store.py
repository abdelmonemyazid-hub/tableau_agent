"""
In-memory store for pipeline run traces — exposed by the monitoring dashboard.
Each call to /generate-viz or /refine-viz creates one RunRecord.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import uuid

MAX_RUNS = 100   # keep the last N runs (circular)


@dataclass
class StepRecord:
    id:          str
    label:       str
    status:      str             # pending | in_progress | success | error
    duration_ms: Optional[float] = None
    error:       Optional[str]   = None
    input:       Optional[str]   = None
    output:      Optional[str]   = None


@dataclass
class RunRecord:
    run_id:         str
    run_type:       str           # "generate" | "refine"
    question:       str           # question text (or feedback for refine)
    session_id:     Optional[str]
    started_at:     datetime
    duration_ms:    Optional[float]
    status:         str           # "success" | "error"
    steps:          list[StepRecord]
    final_viz_type: Optional[str] = None
    reasoning_text: Optional[str] = None
    error_message:  Optional[str] = None
    attempts:       int           = 1


# ── Store ──────────────────────────────────────────────────────────────────

_runs: list[RunRecord] = []


def record_run(run: RunRecord) -> None:
    """Inserts a run at the front of the list. Trims to MAX_RUNS."""
    _runs.insert(0, run)
    if len(_runs) > MAX_RUNS:
        _runs.pop()


def get_all_runs() -> list[RunRecord]:
    return _runs


def get_run(run_id: str) -> Optional[RunRecord]:
    return next((r for r in _runs if r.run_id == run_id), None)


def make_run_id() -> str:
    return str(uuid.uuid4())[:8]
