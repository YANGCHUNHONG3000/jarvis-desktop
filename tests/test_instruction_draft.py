import tkinter as tk

import pytest

from agent_service.instruction_draft import InstructionDraft


@pytest.fixture(scope='module')
def root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def text(root):
    widget = tk.Text(root, undo=True, autoseparators=False)
    yield widget
    if widget.winfo_exists():
        widget.destroy()


def test_exact_snapshot_and_adopt(text):
    draft = InstructionDraft(text)
    assert draft.snapshot() == {'revision': 0, 'text': ''}
    first = draft.adopt('  original\n\n')
    assert first == {'revision': 1, 'text': '  original\n\n'}
    assert draft.adopt(first['text']) == first
    assert draft.unchanged(first)
    assert draft.adopt('different')['revision'] == 2
    assert not draft.unchanged(first)


@pytest.mark.parametrize('command', ['insert', 'delete', 'replace'])
def test_direct_successful_commands_increment_without_event_loop(text, command):
    draft = InstructionDraft(text)
    ticket = draft.adopt('ABC')
    if command == 'insert':
        text.insert('end-1c', 'D')
    elif command == 'delete':
        text.delete('1.0', '1.1')
    else:
        text.replace('1.0', 'end-1c', 'DEF')
    assert draft.snapshot()['revision'] == ticket['revision'] + 1
    assert not draft.unchanged(ticket)
    assert not draft.complete(ticket)


def test_same_loop_a_b_a_is_new_draft(text):
    draft = InstructionDraft(text)
    ticket = draft.adopt('A')
    text.replace('1.0', 'end-1c', 'B')
    text.replace('1.0', 'end-1c', 'A')
    assert draft.snapshot() == {'revision': ticket['revision'] + 2, 'text': 'A'}
    assert not draft.complete(ticket)
    assert text.get('1.0', 'end-1c') == 'A'


def test_same_loop_undo_redo_and_return_to_original(text):
    draft = InstructionDraft(text)
    ticket = draft.adopt('A')
    text.edit_separator()
    text.insert('end-1c', 'B')
    text.edit_separator()
    text.edit_undo()
    assert draft.snapshot()['text'] == 'A'
    assert not draft.unchanged(ticket)
    undone = draft.snapshot()
    text.edit_redo()
    assert draft.snapshot()['text'] == 'AB'
    assert draft.snapshot()['revision'] > undone['revision']
    text.edit_undo()
    assert draft.snapshot()['text'] == 'A'
    assert not draft.complete(ticket)


def test_failed_commands_and_read_flags_do_not_change_revision(text):
    draft = InstructionDraft(text)
    ticket = draft.snapshot()
    with pytest.raises(tk.TclError):
        text.edit_undo()
    with pytest.raises(tk.TclError):
        text.tk.call(str(text), 'replace', 'invalid-index', 'end', 'x')
    text.edit_modified(False)
    text.get('1.0', 'end')
    text.tag_add('marked', '1.0', 'end')
    assert draft.snapshot() == ticket


def test_widget_name_and_text_are_not_interpolated_as_tcl(text):
    widget = tk.Text(text.master, name='draft;[literal]$name')
    try:
        draft = InstructionDraft(widget)
        raw = '[set injected 1]; $variable {literal}\n'
        assert draft.adopt(raw)['text'] == raw
        draft.close()
        assert widget.get('1.0', 'end-1c') == raw
        widget.insert('end-1c', 'more')
        assert widget.get('1.0', 'end-1c') == raw + 'more'
        assert not widget.tk.call('info', 'exists', 'injected')
    finally:
        widget.destroy()


def test_noop_successful_edit_conservatively_invalidates(text):
    draft = InstructionDraft(text)
    ticket = draft.adopt('A')
    text.replace('1.0', 'end-1c', 'A')
    assert not draft.unchanged(ticket)
    assert draft.snapshot()['text'] == 'A'


def test_successful_tcl_abbreviated_edits_are_observed(text):
    draft = InstructionDraft(text)
    ticket = draft.adopt('A')
    text.tk.call(str(text), 'ins', 'end-1c', 'B')
    text.tk.call(str(text), 'del', '1.1', 'end-1c')
    text.tk.call(str(text), 'rep', '1.0', 'end-1c', 'A')
    assert draft.snapshot()['revision'] == ticket['revision'] + 3
    assert not draft.complete(ticket)


def test_paste_binding_traverses_proxy(text):
    draft = InstructionDraft(text)
    ticket = draft.adopt('A')
    text.mark_set('insert', 'end-1c')
    text.clipboard_clear()
    text.clipboard_append(' pasted\n')
    # Tk's actual paste binding, without a KeyRelease or update() call.
    text.event_generate('<<Paste>>')
    assert draft.snapshot()['text'] == 'A pasted\n'
    assert not draft.complete(ticket)


def test_complete_current_ticket_once_and_undo_is_new_draft(text):
    draft = InstructionDraft(text)
    ticket = draft.adopt(' task \n')
    text.edit_separator()
    assert draft.complete(ticket)
    completed = draft.snapshot()
    assert completed == {'revision': ticket['revision'] + 1, 'text': ''}
    assert not draft.complete(ticket)
    assert not draft.complete(completed)
    text.edit_undo()
    assert draft.snapshot()['text'] == ticket['text']
    assert not draft.unchanged(ticket)


@pytest.mark.parametrize('value', ['', ' \t\n'])
def test_blank_instruction_does_not_complete(text, value):
    draft = InstructionDraft(text)
    ticket = draft.adopt(value)
    assert not draft.complete(ticket)
    assert draft.snapshot() == ticket


@pytest.mark.parametrize('ticket', [None, {}, {'revision': True, 'text': 'A'},
                                  {'revision': 1, 'text': 'A', 'extra': 1},
                                  {'revision': -1, 'text': 'A'},
                                  {'revision': 1, 'text': None}])
def test_invalid_ticket_rejected(text, ticket):
    draft = InstructionDraft(text)
    draft.adopt('A')
    assert not draft.unchanged(ticket)
    assert not draft.complete(ticket)
    assert draft.snapshot()['text'] == 'A'


def test_disabled_widget_cannot_falsely_complete_or_adopt(text):
    draft = InstructionDraft(text)
    ticket = draft.adopt('A')
    text.configure(state='disabled')
    assert not draft.complete(ticket)
    assert draft.snapshot()['text'] == 'A'
    with pytest.raises(ValueError, match='replace_failed'):
        draft.adopt('B')
    assert draft.snapshot()['text'] == 'A'


def test_close_restores_widget_and_is_idempotent(text):
    draft = InstructionDraft(text)
    ticket = draft.adopt('A')
    original = draft._original
    draft.close()
    draft.close()
    assert not text.tk.call('info', 'commands', original)
    assert not text.tk.call('info', 'commands', draft._procedure)
    assert not text.tk.call('info', 'commands', draft._notification)
    assert text.tk.call('info', 'commands', str(text))
    text.insert('end-1c', 'B')
    assert text.get('1.0', 'end-1c') == 'AB'
    assert not draft.unchanged(ticket)
    assert not draft.complete(ticket)
    with pytest.raises(RuntimeError, match='closed'):
        draft.snapshot()
    with pytest.raises(RuntimeError, match='closed'):
        draft.adopt('C')
    replacement = InstructionDraft(text)
    assert replacement.snapshot() == {'revision': 0, 'text': 'AB'}


def test_destroy_releases_proxy_and_preserves_other_destroy_binding(text):
    events = []
    text.bind('<Destroy>', lambda event: events.append(event.widget), add='+')
    draft = InstructionDraft(text)
    ticket = draft.adopt('A')
    text.destroy()
    assert events == [text]
    assert not text.tk.call('info', 'commands', draft._original)
    assert not text.tk.call('info', 'commands', draft._procedure)
    assert not text.tk.call('info', 'commands', draft._notification)
    assert not text.tk.call('info', 'commands', str(text))
    assert not draft.unchanged(ticket)
    draft.close()


def test_reject_duplicate_tracker_and_nontext(text):
    draft = InstructionDraft(text)
    with pytest.raises(ValueError, match='already_tracked'):
        InstructionDraft(text)
    with pytest.raises(TypeError, match='requires_text'):
        InstructionDraft(text.master)
    assert draft.snapshot()['revision'] == 0


def test_selection_error_preserves_tcl_catch_semantics_and_mainloop(text):
    def caught(command):
        code = text.tk.call('catch', command, 'draft_error', 'draft_options')
        message = text.tk.getvar('draft_error')
        errorcode = text.tk.call('dict', 'get', text.tk.getvar('draft_options'), '-errorcode')
        return code, message, errorcode

    expected = caught((str(text), 'index', 'sel.first'))
    draft = InstructionDraft(text)
    ticket = draft.snapshot()
    assert expected[0] == 1
    assert 'sel' in expected[1]
    reached = []
    text.master.after_idle(lambda: reached.append(caught((str(text), 'index', 'sel.first'))))
    timer = text.master.after(30, text.master.quit)
    try:
        text.master.mainloop()
    finally:
        text.master.after_cancel(timer)
    assert reached == [expected]
    assert draft.snapshot() == ticket


def test_mainloop_paste_long_text_without_selection_does_not_leak_python_error(text):
    draft = InstructionDraft(text)
    errors, reached = [], []
    root = text.master
    previous = root.report_callback_exception
    root.report_callback_exception = lambda *error: errors.append(error)
    payload = ('long instruction [set injected 1]; $variable {literal} 中文\n' * 100)
    # Native virtual events require a mapped Text on Windows. Mapping must be
    # complete before the callback runs; this update precedes all error probes.
    text.pack()
    root.deiconify()
    root.update()
    text.clipboard_clear()
    text.clipboard_append(payload)
    try:
        def paste():
            assert not text.tag_ranges('sel')
            text.event_generate('<<Paste>>')
            reached.append('pasted')

        root.after_idle(paste)
        timer = root.after(30, lambda: (reached.append('alive'), root.quit()))
        try:
            root.mainloop()
        finally:
            root.after_cancel(timer)
        assert reached == ['pasted', 'alive']
        assert errors == []
        assert draft.snapshot()['text'] == payload
        assert draft.snapshot()['revision'] > 0
        assert not text.tk.call('info', 'exists', 'injected')
    finally:
        root.withdraw()
        root.report_callback_exception = previous
