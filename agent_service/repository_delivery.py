"""Append a separately verified local Word task to a completed GitHub reading."""
from copy import deepcopy
from pathlib import Path

from .computer import ComputerError, write_new


def deliver_repository_document(reading, *, stop=lambda: False, progress=lambda value: None,
                                computer_base=None):
    from .repository_document import prepare_repository_document, execute_repository_document
    result = deepcopy(reading)
    options = {} if computer_base is None else {'computer_base': computer_base}
    try:
        if stop():
            raise ComputerError('repository_document_cancelled')
        progress({'op':'repository_document', 'message':'仓库已读取，正在本机整理 Word 并核验；完成后自动接到下一任务。'})
        plan = prepare_repository_document(reading['run'], **options)
        report = execute_repository_document(plan, approval=plan['sha256'], stop=stop, **options)
    except Exception as exc:
        report = {'ok':False, 'state':'stopped',
                  'code':str(exc) if isinstance(exc, ComputerError) else 'repository_document_failed',
                  'artifacts':[]}
    result['repository_report'] = report
    if report['ok']:
        result['artifacts'] += report['artifacts']
        result['code'] = 'repository_document_verified'
    else:
        result.update(ok=False, state='unknown' if report.get('state') == 'unknown' else 'partial',
                      code=report['code'])
    # The browser outcome remains unchanged; this records the delivery stage.
    try:
        write_new(Path(reading['run']) / 'repository-delivery.json', {
            'ok':result['ok'], 'code':result['code'], 'repository_report':report})
    except Exception:
        result.update(ok=False, state='unknown', code='repository_delivery_record_failed')
    return result
