"""One bounded read/create/verify workflow with durable cancellation checkpoints."""
from contextlib import contextmanager
import os
from pathlib import Path

from .contracts import ActionRequest, digest, utc_now
from .policy import ActionPolicy


@contextmanager
def worker_lock(root):
    # Lock file remains on disk; OS releases ownership on close or process crash.
    with (Path(root) / "worker.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def render_document(job):
    # Escape markup and quote all external text. No source text is interpreted.
    def plain(value):
        escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return "".join("\\" + c if c in "\\`*_{}[]()#+-.!|" else c for c in escaped)

    title = " ".join(job["topic"].splitlines())
    lines = [f"# {plain(title)}", "", "来源摘录 / Source excerpts", "",
             "本文件汇集指定网页的原文摘录，未经模型归纳或独立事实核查。", "",
             f"Task: {job['id']}", ""]
    for index, obs in enumerate(job["observations"], 1):
        lines += [f"## [{index}] {plain(' '.join(obs['title'].splitlines()))}", "",
                  f"Source: <{obs['final_url']}>", f"Captured: {obs['captured_at']}",
                  f"Text SHA-256: {obs['sha256']}", ""]
        lines += ["> " + plain(line) for line in obs["text"].splitlines()]
        lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


class Worker:
    def __init__(self, store, browser):
        self.store = store
        self.browser = browser
        self.policy = ActionPolicy(store.root)

    def _checkpoint(self, db, job):
        if job["state"] == "cancelling":
            job.update(state="cancelled", reason="Cancelled; existing evidence and files retained")
            self.store._save(db, job, "cancelled")
            return False
        if job["pause_requested"]:
            job.update(state="paused", pause_requested=False, reason="Paused by user")
            self.store._save(db, job, "paused")
            return False
        return job["state"] == "running"

    def _begin_action(self, job_id, kind, target):
        with self.store.transaction() as db:
            job = self.store._load(db, job_id)
            if not self._checkpoint(db, job):
                return None
            action = ActionRequest(f"{job_id}:{kind}:{target}", kind, target)
            self.policy.check(job, action)
            job["current_action"] = kind
            self.store._save(db, job, "action_requested", {
                "kind": kind, "target": target, "operation_id": action.operation_id,
                "fingerprint": action.fingerprint,
            })
            return job

    def run_once(self):
        with worker_lock(self.store.root):
            job = self.store.recover_and_claim()
            if not job:
                return None
            try:
                self._run(job)
            except Exception as exc:
                with self.store.transaction() as db:
                    current = self.store._load(db, job["id"])
                    if self._checkpoint(db, current):
                        # Do not persist raw backend errors: URLs/errors may contain credentials.
                        code = getattr(exc, "safe_code", type(exc).__name__)
                        current.update(state="failed", reason=f"{current.get('current_action', 'job')} failed ({code}); partial evidence retained")
                        self.store._save(db, current, "failed", {"error_type": type(exc).__name__, "code": code})
            return self.store.get(job["id"])

    def _run(self, job):
        job_id = job["id"]
        observed = {obs["requested_url"] for obs in job["observations"]}
        for url in job["urls"]:
            if url in observed:
                continue
            if self._begin_action(job_id, "browser.read", url) is None:
                return
            observation = self.browser.read(url, should_stop=lambda: self._should_stop(job_id))
            if (observation.requested_url != url or observation.final_url != url
                    or not isinstance(observation.text, str) or not observation.text.strip()
                    or len(observation.text) > 12000 or len(observation.title) > 300):
                raise ValueError("Browser returned invalid or out-of-scope evidence")
            with self.store.transaction() as db:
                job = self.store._load(db, job_id)
                job["observations"].append(observation.to_dict())
                self.store._save(db, job, "observed", {"url": url, "sha256": job["observations"][-1]["sha256"]})
                if not self._checkpoint(db, job):
                    return

        job = self._begin_action(job_id, "document.create", job["output_name"])
        if job is None:
            return
        content = render_document(job)
        expected = digest(content)
        # Persist intent BEFORE the side effect, so a crash after xb is reconcilable.
        with self.store.transaction() as db:
            job = self.store._load(db, job_id)
            if not self._checkpoint(db, job):
                return
            if job["intent"] is not None and job["intent"]["sha256"] != expected:
                raise ValueError("Stored artifact intent no longer matches observations")
            prior_intent = job["intent"] is not None
            job["intent"] = {"sha256": expected, "bytes": len(content), "name": job["output_name"],
                             "path": str(self.policy.artifact_path(job["output_name"]))}
            self.store._save(db, job, "artifact_prepared", job["intent"])

        with self.store.transaction() as db:
            job = self.store._load(db, job_id)
            if not self._checkpoint(db, job):
                return
            path = self.policy.artifact_path(job["output_name"])
            path.parent.mkdir(exist_ok=True)
            if path.exists():
                if not prior_intent or self._read_digest(path, len(content)) != expected:
                    job.update(state="waiting_user", reason="Output already exists or differs; preserved without overwrite. Submit a new output name.")
                    self.store._save(db, job, "output_conflict")
                    return
            else:
                with path.open("xb") as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
            job["artifact"] = {"path": str(path), "sha256": expected, "verified": False}
            self.store._save(db, job, "artifact_created", job["artifact"])

        if self._begin_action(job_id, "document.verify", job["output_name"]) is None:
            return
        with self.store.transaction() as db:
            job = self.store._load(db, job_id)
            if not self._checkpoint(db, job):
                return
            path = self.policy.artifact_path(job["output_name"])
            actual = self._read_digest(path, len(content))
            if actual != expected:
                raise ValueError("Artifact readback mismatch")
            job["artifact"].update(verified=True, readback_sha256=actual, verified_at=utc_now())
            job.update(state="succeeded", reason="Source document created and read back; no model synthesis performed")
            self.store._save(db, job, "succeeded", job["artifact"])

    @staticmethod
    def _read_digest(path, size):
        with path.open("rb") as file:
            return digest(file.read(size + 1))

    def _should_stop(self, job_id):
        job = self.store.get(job_id)
        return job["state"] != "running" or job["pause_requested"]


def verify_artifact(store, job_id):
    job = store.get(job_id)
    if not job["artifact"] or not job["intent"]:
        return {"ok": False, "reason": "No recorded artifact"}
    path = ActionPolicy(store.root).artifact_path(job["output_name"])
    try:
        actual = Worker._read_digest(path, job["intent"]["bytes"])
    except OSError as exc:
        return {"ok": False, "reason": type(exc).__name__, "path": str(path)}
    return {"ok": actual == job["intent"]["sha256"], "path": str(path), "sha256": actual,
            "recorded_sha256": job["intent"]["sha256"]}
