"""Explicit history action for reviewing an existing, unexecuted mail plan.

Preparation is read-only. The main UI owns process launch and result delivery.
"""
from copy import deepcopy
from queue import Queue, Empty
import re
import threading
import time


class MailReviewResumePanel:
    def __init__(self, parent, *, ttk, base, status, get_context, start):
        self.base, self.status = base, status
        self.get_context, self.start = get_context, start
        self.messages = Queue()
        self.generation = 0
        self.preview = self.pending = None
        self.button = ttk.Button(parent, text="继续邮件复核（云端）", state="disabled",
                                 command=self.prepare)
        self.button.pack(side="left", padx=6)

    def invalidate(self):
        self.generation += 1
        self.preview = self.pending = None
        self.button.configure(state="disabled")

    def update(self, inspection):
        self.invalidate()
        preview = inspection.get('mail_review_resume') if type(inspection) is dict else None
        context = self.get_context()
        if (type(preview) is dict and preview.get('eligible') is True
                and preview.get('task_root') == inspection.get('root') == context.get('task_root')
                and type(preview.get('token')) is str
                and re.fullmatch(r'[0-9a-f]{64}', preview['token'])
                and type(preview.get('instruction')) is str
                and 1 <= len(preview['instruction'].strip()) <= 4000
                and type(preview.get('source_ref')) is dict and not context.get('busy', True)):
            self.preview = deepcopy(preview)
            self.button.configure(state="normal")
            return True
        return False

    def prepare(self):
        context = self.get_context()
        if self.pending or not self.preview or context.get('busy', True):
            return
        preview = deepcopy(self.preview)
        if context.get('task_root') != preview['task_root']:
            self.invalidate()
            return
        self.generation += 1
        generation = self.generation
        self.pending = (deepcopy(context), preview, time.monotonic() + 30)
        self.button.configure(state="disabled")
        self.status.set("正在重新核验原邮件、已有候选与计划；通过后只调用语义复核，不重新生成。当前草稿保留。")

        def work():
            try:
                from .mail_review_resume import prepare_resume
                result = prepare_resume(preview['task_root'], self.base, preview['token'])
                self.messages.put((generation, result, None))
            except Exception:
                # Provider details, local paths and mail contents stay out of UI errors.
                self.messages.put((generation, None, 'mail_review_prepare_failed'))
        try:
            threading.Thread(target=work, daemon=True, name="jarvis-mail-review-prepare").start()
        except RuntimeError:
            self.invalidate()
            self.status.set("复核续接准备未能启动；尚未调用模型，请重新选择历史任务。")

    def poll(self):
        if self.pending and time.monotonic() >= self.pending[2]:
            self.invalidate()
            self.status.set("复核续接准备超时；未启动任务，请重新选择历史任务。")
        while True:
            try:
                generation, prepared, error = self.messages.get_nowait()
            except Empty:
                return
            if generation != self.generation or self.pending is None:
                continue
            context, preview, _deadline = self.pending
            self.pending = None
            self.preview = None
            if self.get_context() != context:
                self.status.set("历史选择、资料、草稿或运行状态已变化；旧复核准备已忽略，未启动任务。")
                continue
            if (error or type(prepared) is not dict
                    or any(prepared.get(k) != preview[k]
                           for k in ('task_root', 'token', 'instruction', 'source_ref'))):
                self.status.set("原邮件、候选或续接记录无法核验；未调用模型，请刷新历史后重新检查。")
                continue
            try:
                self.start(prepared)
            except Exception:
                self.status.set("复核续接启动未确认；请检查历史任务，勿重复执行。当前草稿保留。")
