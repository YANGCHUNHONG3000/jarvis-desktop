"""Read-only parent-to-child history navigation, separate from paid review."""
from copy import deepcopy
from queue import Empty, Queue
import threading
import time


class MailReviewNavigationPanel:
    def __init__(self, parent, *, ttk, base, status, get_context, navigate):
        self.base, self.status = base, status
        self.get_context, self.navigate = get_context, navigate
        self.messages = Queue()
        self.generation = 0
        self.parent_root = self.pending = None
        self.button = ttk.Button(parent, text='查看续接任务和成果', state='disabled',
                                 command=self.prepare)
        self.button.pack(side='left', padx=6)

    def invalidate(self):
        self.generation += 1
        self.parent_root = self.pending = None
        self.button.configure(state='disabled')

    def update(self, inspection):
        self.invalidate()
        context = self.get_context()
        preview = inspection.get('mail_review_resume', {})
        if (type(preview) is dict and preview.get('code') == 'mail_review_resume_already_claimed'
                and inspection.get('root') == context.get('task_root')
                and not context.get('busy', True)):
            self.parent_root = inspection['root']
            self.button.configure(state='normal')

    def prepare(self):
        context = self.get_context()
        if (self.pending or not self.parent_root or context.get('busy', True)
                or context.get('task_root') != self.parent_root):
            return
        self.generation += 1
        generation = self.generation
        parent = self.parent_root
        self.pending = (deepcopy(context), parent, time.monotonic() + 30)
        self.button.configure(state='disabled')
        self.status.set('正在只读核验关联续接任务；不重新生成、不调用模型，当前草稿保留。')

        def work():
            from .mail_review_navigation import resolve_continuation, describe_error
            try:
                result = resolve_continuation(parent, self.base)
                self.messages.put((generation, result, None))
            except Exception as exc:
                self.messages.put((generation, None, describe_error(exc)))
        try:
            threading.Thread(target=work, daemon=True, name='jarvis-mail-review-navigate').start()
        except RuntimeError:
            self.invalidate()
            self.status.set('关联核验未能启动；原选择保留，请重新选择原任务后再试。')

    def poll(self):
        if self.pending and time.monotonic() >= self.pending[2]:
            self.invalidate()
            self.status.set('关联核验超时；原选择保留，没有启动任务，请重新选择原任务后再试。')
        while True:
            try:
                generation, resolved, error = self.messages.get_nowait()
            except Empty:
                return
            if generation != self.generation or self.pending is None:
                continue
            context, parent, _deadline = self.pending
            self.pending = self.parent_root = None
            if self.get_context() != context:
                self.status.set('历史选择、草稿、资料或运行状态已变化；旧跳转已忽略，没有启动任务。')
                continue
            if error:
                self.status.set(error)
                continue
            try:
                from .mail_review_navigation import link_unchanged
                if (type(resolved) is not dict or resolved.get('parent_root') != parent
                        or not link_unchanged(resolved, self.base)):
                    raise ValueError('navigation_changed')
                self.navigate(resolved)
            except Exception:
                self.status.set('关联记录已变化或暂不可用；未启动任务，请重新选择原任务核验。')
