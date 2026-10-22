"""Durable command queue and event stream, independent of old personal data."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid

from .contracts import TERMINAL, digest, utc_now
from .policy import output_name, source_url


class JobStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "jobs.sqlite3"
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS submissions (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, job_id TEXT NOT NULL)")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _load(db, job_id):
        row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown job: {job_id}")
        return json.loads(row[0])

    @staticmethod
    def _save(db, job, kind, data=None):
        job["updated_at"] = utc_now()
        db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)",
                   (job["id"], json.dumps(job, ensure_ascii=False)))
        db.execute("INSERT INTO events(job_id, at, kind, data) VALUES (?, ?, ?, ?)",
                   (job["id"], job["updated_at"], kind, json.dumps(data or {}, ensure_ascii=False)))

    def submit(self, topic, urls=(), name=None, request_id=None):
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 500:
            raise ValueError("Topic must contain 1 to 500 characters")
        urls = list(dict.fromkeys(source_url(u) for u in urls))
        if len(urls) > 8:
            raise ValueError("At most eight sources are supported per job")
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            raise ValueError("Request id must contain 1 to 128 characters")
        fingerprint = digest(json.dumps([topic.strip(), urls, name], ensure_ascii=False).encode("utf-8"))
        job_id = uuid.uuid4().hex
        job = dict(id=job_id, topic=topic.strip(), urls=urls,
                   output_name=output_name(name or f"research-{job_id}.md"),
                   state="queued" if urls else "waiting_user",
                   reason="" if urls else "Source URLs required; cloud research has no approved budget",
                   created_at=utc_now(), observations=[], artifact=None,
                   intent=None, pause_requested=False)
        with self.transaction() as db:
            if request_id is not None:
                prior = db.execute("SELECT fingerprint, job_id FROM submissions WHERE key=?", (request_id,)).fetchone()
                if prior:
                    if prior[0] != fingerprint:
                        raise ValueError("Request id was already used for different inputs")
                    return self._load(db, prior[1])
                db.execute("INSERT INTO submissions VALUES (?, ?, ?)", (request_id, fingerprint, job_id))
            self._save(db, job, "submitted", {"state": job["state"]})
        return job

    def get(self, job_id):
        with self.transaction() as db:
            return self._load(db, job_id)

    def list(self):
        with self.transaction() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM jobs ORDER BY rowid")]

    def events(self, job_id, after=0):
        with self.transaction() as db:
            self._load(db, job_id)
            return [dict(seq=row[0], at=row[1], kind=row[2], data=json.loads(row[3]))
                    for row in db.execute("SELECT seq, at, kind, data FROM events WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after))]

    def control(self, job_id, command):
        if command not in {"cancel", "pause", "resume"}:
            raise ValueError("Unknown control command")
        with self.transaction() as db:
            job = self._load(db, job_id)
            if job["state"] in TERMINAL:
                return job
            if command == "cancel":
                job["state"] = "cancelling" if job["state"] in {"running", "cancelling"} else "cancelled"
                job["reason"] = "Cancellation requested"
            elif command == "pause":
                if job["state"] == "running":
                    job["pause_requested"] = True
                elif job["state"] == "queued":
                    job["state"] = "paused"
            elif job["state"] == "paused":
                job.update(state="queued", pause_requested=False, reason="")
            else:
                raise ValueError("Only paused jobs can resume; submit a new job for changed inputs")
            self._save(db, job, command, {"state": job["state"]})
            return job

    def recover_and_claim(self):
        """Caller MUST hold WorkerLock. No other executor can own a running job."""
        with self.transaction() as db:
            jobs = [json.loads(row[0]) for row in db.execute("SELECT payload FROM jobs ORDER BY rowid")]
            for job in jobs:
                if job["state"] in {"running", "cancelling"}:
                    job["state"] = "cancelled" if job["state"] == "cancelling" else "paused"
                    job["reason"] = "Worker interrupted; retained evidence requires reconciliation"
                    self._save(db, job, "recovered", {"state": job["state"]})
            queued = next((job for job in jobs if job["state"] == "queued"), None)
            if queued:
                queued["state"] = "running"
                self._save(db, queued, "started")
            return queued
