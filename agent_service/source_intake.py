"""Select a lossless bounded workbook intake before creating a source snapshot."""
from pathlib import Path
import time
import xml.etree.ElementTree as ET


def needs_workbook_source(path):
    from .office_source import _package_parts, _S
    path = Path(path)
    if path.suffix.casefold() != '.xlsx':
        return False
    parts = _package_parts(path)
    workbook = ET.fromstring(parts['xl/workbook.xml'])
    sheets = workbook.findall(f'{{{_S}}}sheets/{{{_S}}}sheet')
    return (len(sheets) != 1 or any(s.get('state','visible') != 'visible' for s in sheets)
            or any(ET.fromstring(content).find('.//{'+_S+'}f') is not None
                   for name,content in parts.items() if name.startswith('xl/worksheets/') and name.endswith('.xml')))


def snapshot_selected_source(path, task_root, *, stop=lambda:False, deadline=None):
    from .office_source import snapshot_source, SourceError
    if stop() or deadline is not None and time.monotonic() >= deadline:
        raise SourceError('source_cancelled')
    if needs_workbook_source(path):
        from .workbook_source import snapshot_workbook_source
        result = snapshot_workbook_source(path, task_root, stop=stop, deadline=deadline)
    else:
        result = snapshot_source(path, task_root)
    if stop() or deadline is not None and time.monotonic() >= deadline:
        raise SourceError('source_cancelled')
    return result
