"""Local approval distinguishes identical names/content in different folders."""
from agent_service.computer import ComputerSession
from agent_service.computer_interaction import prepare, describe
from test_office_edit import word_fixture


def test_preview_disambiguates_identical_originals(tmp_path):
    paths = []
    for folder in ('first', 'second'):
        directory = tmp_path / folder
        directory.mkdir()
        path = directory / 'brief.docx'
        word_fixture(path)
        paths.append(path)
    session = ComputerSession.create(tmp_path / 'computer')
    plan = prepare(session, '修改两份简报', edits=[
        {'path': str(path), 'changes': [{'paragraph_index': 1, 'before': '预算待确认', 'after': '预算已确认'}]}
        for path in paths])
    shown = describe(plan)
    assert all(str(p.absolute()) in shown for p in paths)
