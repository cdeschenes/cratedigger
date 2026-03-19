"""
webapp/runner.py — subprocess execution and SSE log streaming.

Job IDs are fixed strings: "missing" and "discover".
Each job runs the corresponding Python script as a subprocess, captures stdout+stderr,
and fans the output out to any waiting SSE consumers.
"""
import asyncio
import sys
from asyncio.subprocess import PIPE, STDOUT
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Literal

SCRIPTS_DIR = Path(__file__).parent.parent  # webapp/ → Scripts/missing_popular_albums/

SCRIPT_MAP: dict[str, Path] = {
    "missing": SCRIPTS_DIR / "missing_popular_albums.py",
    "discover": SCRIPTS_DIR / "discover_similar_artists.py",
}

JobStatus = Literal["idle", "running", "succeeded", "failed"]


@dataclass
class JobState:
    status: JobStatus = "idle"
    pid: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=2000))
    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)


# Global state — one entry per job ID, lives for the lifetime of the process.
_jobs: dict[str, JobState] = {
    "missing": JobState(),
    "discover": JobState(),
}


def get_status(job_id: str) -> dict:
    """Return a JSON-serialisable snapshot of a job's state."""
    state = _jobs[job_id]
    return {
        "job_id": job_id,
        "status": state.status,
        "pid": state.pid,
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "finished_at": state.finished_at.isoformat() if state.finished_at else None,
        "exit_code": state.exit_code,
    }


def get_all_status() -> dict[str, dict]:
    return {job_id: get_status(job_id) for job_id in _jobs}


async def run_job(job_id: str, no_cache: bool = False, workers: int | None = None) -> None:
    """
    Launch the script subprocess. Raises RuntimeError if already running.
    Returns immediately; the subprocess continues in the background.
    """
    state = _jobs[job_id]
    if state.status == "running":
        raise RuntimeError(f"Job '{job_id}' is already running.")

    cmd = [sys.executable, str(SCRIPT_MAP[job_id])]
    if no_cache:
        cmd.append("--no-cache")
    if workers is not None:
        cmd.extend(["--workers", str(int(workers))])

    state.status = "running"
    state.pid = None
    state.started_at = datetime.now(tz=timezone.utc)
    state.finished_at = None
    state.exit_code = None
    state.log_buffer.clear()

    asyncio.create_task(_run_and_capture(job_id, cmd))


async def _run_and_capture(job_id: str, cmd: list[str]) -> None:
    state = _jobs[job_id]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=PIPE,
            stderr=STDOUT,
            cwd=str(SCRIPTS_DIR),
        )
        state.pid = proc.pid
        assert proc.stdout is not None

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if not line.strip():
                continue
            state.log_buffer.append(line)
            _broadcast(job_id, line)

        exit_code = await proc.wait()
        state.exit_code = exit_code
        state.status = "succeeded" if exit_code == 0 else "failed"
    except Exception as exc:
        state.status = "failed"
        state.exit_code = -1
        _broadcast(job_id, f"[webapp] Error launching job: {exc}")
    finally:
        state.finished_at = datetime.now(tz=timezone.utc)
        # Signal all SSE consumers that the stream is done
        for q in list(state._subscribers):
            try:
                q.put_nowait(None)  # sentinel
            except asyncio.QueueFull:
                pass
        state._subscribers.clear()


def _broadcast(job_id: str, line: str) -> None:
    state = _jobs[job_id]
    dead: list[asyncio.Queue] = []
    for q in state._subscribers:
        try:
            q.put_nowait(line)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            state._subscribers.remove(q)
        except ValueError:
            pass


async def stream_logs(job_id: str) -> AsyncIterator[str]:
    """
    Async generator yielding SSE-formatted events.
    Replays the existing log buffer for late-joiners, then streams live lines.
    Sends a keepalive comment every 30 seconds to prevent proxy timeouts.
    Ends with a 'done' custom event when the job finishes.
    """
    state = _jobs[job_id]

    # Replay buffered lines first (handles page reload mid-run)
    for line in list(state.log_buffer):
        yield f"data: {line}\n\n"

    # If the job already finished, nothing more to stream
    if state.status != "running":
        yield "event: done\ndata: end\n\n"
        return

    q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=500)
    state._subscribers.append(q)
    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue

            if item is None:  # sentinel — job finished
                yield "event: done\ndata: end\n\n"
                return

            yield f"data: {item}\n\n"
    finally:
        try:
            state._subscribers.remove(q)
        except ValueError:
            pass
