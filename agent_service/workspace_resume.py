"""Explicit save and guarded restore for desktop workspace state."""

from copy import deepcopy
import queue
import threading
import time

from . import workspace_state


class WorkspaceResume:
    def __init__(self, *, context, busy, on_busy, apply, status, store, task_base,
                 clock=time.monotonic, backend=workspace_state,
                 thread_name="workspace-restore", on_failure=None):
        self.context = context
        self.busy = busy
        self.on_busy = on_busy
        self.apply = apply
        self.status = status
        self.store = store
        self.task_base = task_base
        self.clock = clock
        self.backend = backend
        self.thread_name = thread_name
        self.on_failure = on_failure
        self.expected_id = None
        self.pending = False
        self.deadline = None
        self.generation = 0
        self.closed = False
        self.messages = queue.Queue()
        self._worker = None
        self._worker_generation = None
        try:
            current = self.backend.load(store)
            self.expected_id = current["id"] if current else None
        except workspace_state.WorkspaceError:
            # Keep CAS conservative; an explicit save will report the error.
            self.expected_id = None

    def save(self):
        if self.closed or self.pending or self._worker is not None or self.busy():
            self.status("工作区正忙，未保存。"); return False
        try:
            snapshot = self.context()
            if type(snapshot) is not dict or snapshot.get("unavailable"):
                reason = snapshot.get("blocked_reason") if type(snapshot) is dict else None
                self.status(reason if type(reason) is str and reason else
                            "工作区无法读取；当前内容未更改。")
                return False
            payload = deepcopy(snapshot["payload"])
            record = self.backend.save(self.store, payload, self.expected_id)
        except workspace_state.WorkspaceError as exc:
            message = ("已有更新的工作区，未覆盖；请重新打开后恢复。" if str(exc) == "workspace_conflict"
                       else "工作区无法保存；当前内容未更改。")
            self.status(message); return False
        except Exception:
            self.status("工作区无法读取；当前内容未更改。"); return False
        self.expected_id = record["id"]
        self.status("工作区已保存；不会自动执行任务。")
        return True

    def restore(self):
        if self.closed or self.pending or self._worker is not None or self.busy():
            self.status("工作区正忙，未恢复。"); return False
        try:
            current = deepcopy(self.context())
            if type(current) is not dict:
                self.status("当前工作区状态无法核验，未恢复。"); return False
            if current.get("unavailable"):
                reason = current.get("blocked_reason")
                self.status(reason if type(reason) is str and reason else
                            "当前工作区状态无法核验，未恢复。")
                return False
            saved = self.backend.load(self.store)
            if saved is None:
                self.status("没有已保存的工作区。"); return False
            payload = saved["payload"]
            cur = current["payload"]
            if type(cur) is not dict or set(cur) != set(payload):
                self.status("当前内容与已保存工作区不同，未覆盖。"); return False
            # Empty fields may be filled; existing nonempty values must match exactly.
            for field, value in cur.items():
                empty = value is None or value == "" or value == [] or value == {}
                if not empty and value != payload[field]:
                    self.status("当前内容与已保存工作区不同，未覆盖。"); return False
        except workspace_state.WorkspaceError:
            self.status("已保存工作区无法读取，当前内容未更改。"); return False
        except Exception:
            self.status("当前工作区状态无法核验，未恢复。"); return False
        self.generation += 1
        generation = self.generation
        self.pending = True
        self.deadline = self.clock() + 30
        self._captured_context = current
        self._captured_saved = deepcopy(saved)
        self._captured_id = saved["id"]
        self.on_busy(True)

        def work():
            try:
                result = self.backend.verify(payload, self.task_base)
                self.messages.put(("result", generation, result, None))
            except Exception as exc:
                self.messages.put(("result", generation, None, exc))
            finally:
                self.messages.put(("done", generation, None, None))
        try:
            self._worker = threading.Thread(target=work, daemon=True,
                                            name=self.thread_name)
            self._worker_generation = generation
            self._worker.start()
        except Exception:
            self._worker = None
            self._worker_generation = None
            self.pending = False; self.deadline = None
            self.on_busy(False)
            self.status("工作区核验无法启动，当前内容未更改。"); return False
        self.status("正在核验已保存工作区；不会启动任务。")
        return True

    def poll(self):
        changed = False
        if self.pending and self.deadline is not None and self.clock() >= self.deadline:
            self.cancel("工作区核验超时，当前内容未更改。")
            changed = True
        result = None
        while True:
            try:
                event, generation, verified, error = self.messages.get_nowait()
            except queue.Empty:
                break
            changed = True
            if event == "done" and generation == self._worker_generation:
                self._worker = None
                self._worker_generation = None
            elif event == "result":
                result = (generation, verified, error)
        if result is None:
            return changed
        generation, verified, error = result
        if generation != self.generation or not self.pending:
            return True
        self.pending = False; self.deadline = None
        self.on_busy(False)
        try:
            if deepcopy(self.context()) != self._captured_context:
                self.status("核验期间工作区内容已变化，未恢复；当前内容保留。"); return True
            if error is not None:
                message = None
                if self.on_failure is not None:
                    try:
                        message = self.on_failure(deepcopy(self._captured_saved), error,
                                                  deepcopy(self._captured_context))
                    except Exception:
                        message = None
                if type(message) is str and message:
                    self.status(message)
                else:
                    self.status("已保存成果无法核验，当前内容未更改。")
                return True
            if self.apply(deepcopy(verified)) is not True:
                self.status("工作区无法应用，当前内容未更改。"); return True
            self.expected_id = self._captured_id
            self.status("工作区已恢复为草稿和来源；没有执行任务。")
        except Exception:
            self.status("工作区无法应用，当前内容未更改。")
        return True

    def cancel(self, message="工作区恢复已取消，当前内容未更改。"):
        self.generation += 1
        was_pending = self.pending
        self.pending = False; self.deadline = None
        if was_pending:
            self.on_busy(False)
            self.status(message)
        return was_pending

    def close(self):
        self.closed = True
        return self.cancel("工作区恢复已取消，当前内容未更改。")
