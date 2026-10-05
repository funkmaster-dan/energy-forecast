"""One bounded subprocess worker; durable queue and recovery, no training in requests."""

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import timedelta

from .composition import dt
from .store import now, stamp

KINDS = {
    "weather",
    "forecast",
    "train",
    "calibrate",
    "weather_history",
    "weather_run",
    "export_history",
    "bias",
    "adapt",
}


def enqueue(store, kind, options=None, scheduled=False):
    if kind not in KINDS:
        raise ValueError("Unknown job kind")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT payload FROM jobs WHERE kind=? AND status IN ('queued','running')", (kind,)
        ).fetchone()
        if existing:
            existing_job = json.loads(existing[0])
            if existing_job["options"] == (options or {}):
                return existing_job
            if kind not in ("weather_history", "weather_run"):
                raise ValueError("A different job of this kind is already queued")
        queued = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
        ).fetchone()[0]
        if queued >= 20:
            raise ValueError("Job queue is full")
        identifier = uuid.uuid4().hex
        job = {
            "id": identifier,
            "kind": kind,
            "status": "queued",
            "created_at": stamp(),
            "updated_at": stamp(),
            "options": options or {},
            "scheduled": scheduled,
            "progress": 0,
        }
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?)",
            (
                identifier,
                kind,
                job["status"],
                job["created_at"],
                job["updated_at"],
                json.dumps(job),
            ),
        )
    return job


def update(store, identifier, **changes):
    with store.connect() as db:
        row = db.execute("SELECT payload FROM jobs WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Unknown job")
        payload = json.loads(row[0])
        if payload["status"] == "cancelled":
            return payload
        payload.update(changes, updated_at=stamp())
        db.execute(
            "UPDATE jobs SET status=?,updated=?,payload=? WHERE id=?",
            (payload["status"], payload["updated_at"], json.dumps(payload), identifier),
        )
    return payload


class Worker:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.process = None
        self.current = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="energy-worker")

    def start(self):
        # A single uvicorn worker is required. Interrupted jobs resume with saved checkpoints.
        for job in self.store.documents("jobs", 1000):
            if job["status"] == "running":
                update(self.store, job["id"], status="queued", reason="resuming_after_restart")
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.thread.join(timeout=10)

    def cancel(self, identifier):
        job = update(
            self.store, identifier, status="cancelled", reason="cancelled_by_administrator"
        )
        if self.current == identifier and self.process and self.process.poll() is None:
            self.process.terminate()
        return job

    def schedule(self):
        config = self.store.configuration()
        if not config:
            return
        periods = {
            "weather": 1800,
            "forecast": 60,
            "calibrate": 3600,
            "train": 604800,
            "bias": 3600,
            "adapt": 86400,
        }
        for kind, seconds in periods.items():
            if kind in ("calibrate", "train", "bias", "adapt") and config["learning_paused"]:
                continue
            last = self.store.meta(f"schedule_{kind}")
            if last is None or dt(last) + timedelta(seconds=seconds) <= now():
                enqueue(self.store, kind, scheduled=True)
                self.store.meta(f"schedule_{kind}", stamp())

    def run(self):
        while not self.stop.is_set():
            try:
                self.schedule()
                with self.store.connect() as db:
                    row = db.execute(
                        "SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
                    ).fetchone()
                if row:
                    self.current = row[0]
                    update(self.store, self.current, status="running", progress=0.05)
                    env = {
                        **os.environ,
                        "OMP_NUM_THREADS": "2",
                        "MKL_NUM_THREADS": "2",
                        "OPENBLAS_NUM_THREADS": "2",
                    }
                    log = self.store.root / "worker.log"
                    if log.exists() and log.stat().st_size > 2_000_000:
                        log.unlink()
                    with log.open("a") as output:
                        self.process = subprocess.Popen(
                            [
                                sys.executable,
                                "-m",
                                "energy_forecast.worker",
                                str(self.store.root),
                                self.current,
                            ],
                            env=env,
                            stdout=output,
                            stderr=output,
                        )
                        started = now()
                        while self.process.poll() is None and not self.stop.wait(0.5):
                            if (now() - started).total_seconds() > 300:
                                self.process.kill()
                                update(
                                    self.store,
                                    self.current,
                                    status="failed",
                                    reason="worker_time_limit_checkpoint_retained",
                                )
                                break
                        if self.stop.is_set():
                            self.process.terminate()
                            try:
                                self.process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                self.process.kill()
                            update(
                                self.store,
                                self.current,
                                status="queued",
                                reason="interrupted_checkpoint_retained",
                            )
                        elif self.process.returncode:
                            update(
                                self.store,
                                self.current,
                                status="failed",
                                reason="worker_failed; see local worker.log",
                            )
                    self.current = None
            except Exception as error:
                self.store.audit("worker_error", {"type": type(error).__name__})
            self.stop.wait(1)
