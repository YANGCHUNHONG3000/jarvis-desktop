"""Open an existing XLSX with the installed WPS spreadsheet executable."""
import os
from pathlib import Path
import subprocess

from .word_open import WordOpenError, _registered_app_paths


def resolve_workbook_application():
    # WPS installs et.exe alongside its registered wps.exe. Resolve both paths
    # and reject redirects; never change associations or fall back to the shell.
    for filename, _, _, raw in _registered_app_paths():
        if filename.casefold() != 'wps.exe' or type(raw) is not str or '\x00' in raw:
            continue
        value = os.path.expandvars(raw.strip())
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        original = Path(value)
        candidate = original.parent / 'et.exe'
        if (not original.is_absolute() or str(original).startswith(('\\\\', '//'))
                or original.name.casefold() != 'wps.exe' or '"' in value):
            continue
        try:
            if (not original.is_file() or original.is_symlink()
                    or not candidate.is_file() or candidate.is_symlink()):
                continue
            source, executable = original.resolve(strict=True), candidate.resolve(strict=True)
            if (not source.is_absolute() or str(source).startswith(('\\\\', '//'))
                    or source.name.casefold() != 'wps.exe'
                    or not executable.is_absolute() or str(executable).startswith(('\\\\', '//'))
                    or executable.name.casefold() != 'et.exe'
                    or executable.parent != source.parent):
                continue
        except OSError:
            continue
        return {'name': 'WPS 表格', 'executable': str(executable)}
    raise WordOpenError('未找到已安装的WPS表格，文件仍保留。')


def request_open_workbook(path):
    """Request only; callers must separately verify the exact open workbook."""
    from .windows_file_dialog import snapshot_file
    approved = snapshot_file(path)
    if Path(approved['path']).suffix.casefold() != '.xlsx':
        raise WordOpenError('请选择已有的XLSX工作簿。')
    application = resolve_workbook_application()
    try:
        subprocess.Popen([application['executable'], approved['path']], shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, ValueError) as exc:
        raise WordOpenError('WPS表格启动失败；工作簿保留，没有重新生成。') from exc
    return application['name']
