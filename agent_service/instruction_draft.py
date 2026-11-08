"""Revision tickets for one Tk Text instruction, including synchronous edits.

All methods belong on the widget's Tk thread. A ticket is local draft identity,
not task approval or evidence of successful execution.
"""
import tkinter as tk


# Keep the original widget command entirely in Tcl. A Tcl error crossing a
# Python createcommand callback leaves a pending Python exception even when a
# Tk binding catches the Tcl error (for example, querying an empty selection).
_PROXY_BODY = r'''
    set result [$original {*}$args]
    set op [lindex $args 0]
    set subop [lindex $args 1]
    if {$op ne "" && (
        [string first $op insert] == 0 ||
        [string first $op delete] == 0 ||
        [string first $op replace] == 0 ||
        ([string first $op edit] == 0 && $subop ne "" && (
            [string first $subop undo] == 0 ||
            [string first $subop redo] == 0)))} {
        $notify
    }
    return $result
'''


class InstructionDraft:
    def __init__(self, widget):
        if not isinstance(widget, tk.Text):
            raise TypeError('instruction_draft_requires_text')
        previous = getattr(widget, '_instruction_draft_tracker', None)
        if previous is not None and not previous._closed:
            raise ValueError('instruction_draft_already_tracked')
        self.widget = widget
        self._revision = 0
        self._closed = False
        self._path = str(widget)
        self._original = self._path + '_instruction_draft_' + str(id(self))
        self._procedure = self._original + '_proxy'
        self._notification = self._original + '_notify'
        self._destroy_binding = None
        widget.tk.call('rename', self._path, self._original)
        try:
            widget.tk.createcommand(self._notification, self._changed)
            widget.tk.call('proc', self._procedure, ('original', 'notify', 'args'), _PROXY_BODY)
            widget.tk.call('interp', 'alias', '', self._path, '',
                           self._procedure, self._original, self._notification)
            self._destroy_binding = widget.bind('<Destroy>', self._destroy, add='+')
        except Exception:
            self.close()
            raise
        widget._instruction_draft_tracker = self

    def _changed(self):
        # No widget calls or error handling here: Tcl notifies only after a
        # successful mutation, including a command whose text is unchanged.
        self._revision += 1

    def snapshot(self):
        if self._closed:
            raise RuntimeError('instruction_draft_closed')
        return {'revision': self._revision,
                'text': self.widget.get('1.0', 'end-1c')}

    def unchanged(self, ticket):
        if (self._closed or type(ticket) is not dict or set(ticket) != {'revision', 'text'}
                or type(ticket['revision']) is not int or ticket['revision'] < 0
                or type(ticket['text']) is not str):
            return False
        try:
            return ticket == self.snapshot()
        except tk.TclError:
            return False

    def complete(self, ticket):
        """Clear only the still-current, nonblank submitted instruction."""
        if not self.unchanged(ticket) or not ticket['text'].strip():
            return False
        try:
            self.widget.replace('1.0', 'end-1c', '')
            return self.widget.get('1.0', 'end-1c') == ''
        except tk.TclError:
            return False

    def adopt(self, text):
        """Install a new instruction with one edit; identical text is a no-op."""
        if self._closed:
            raise RuntimeError('instruction_draft_closed')
        if type(text) is not str:
            raise TypeError('instruction_draft_text_required')
        if self.snapshot()['text'] != text:
            self.widget.replace('1.0', 'end-1c', text)
            if self.snapshot()['text'] != text:
                raise ValueError('instruction_draft_replace_failed')
        return self.snapshot()

    def _destroy(self, event):
        if event.widget is self.widget:
            self.close()

    def close(self):
        """Restore the original widget command; safe after widget destruction."""
        if self._closed:
            return
        self._closed = True
        if getattr(self.widget, '_instruction_draft_tracker', None) is self:
            del self.widget._instruction_draft_tracker
        if self._destroy_binding is not None:
            try:
                self.widget.unbind('<Destroy>', self._destroy_binding)
            except tk.TclError:
                pass
        for command in (self._path, self._procedure):
            try:
                self.widget.tk.call('rename', command, '')
            except tk.TclError:
                pass
        try:
            self.widget.tk.deletecommand(self._notification)
        except tk.TclError:
            pass
        try:
            self.widget.tk.call('rename', self._original, self._path)
        except tk.TclError:
            pass
