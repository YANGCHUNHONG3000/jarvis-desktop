"""Open an existing DOCX in a registered local editor, without shell association."""
import os
from pathlib import Path
import subprocess


class WordOpenError(ValueError):
    pass


def _registered_app_paths():
    """Read App Paths only; never modify default apps or execute registry commands."""
    try:
        import winreg
    except ImportError as exc:
        raise WordOpenError('当前系统不支持直接打开 Word。') from exc
    # Word first when both editors are installed. WPS is used on this machine.
    for filename, label, flags in (
        ('WINWORD.EXE', 'Microsoft Word', ()),
        ('wps.exe', 'WPS Office', ('/wps',)),
    ):
        key_name = r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths' + '\\' + filename
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                        value, kind = winreg.QueryValueEx(key, '')
                    if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                        yield filename, label, flags, value
                except OSError:
                    continue


def resolve_word_application():
    """Return a fixed executable/argument recipe from a validated App Paths entry."""
    for filename, label, flags, value in _registered_app_paths():
        if not isinstance(value, str) or '\x00' in value:
            continue
        value = os.path.expandvars(value.strip())
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        path = Path(value)
        # App Paths must be one local executable, never a command line, URI,
        # UNC share, relative program name, or an executable found via PATH.
        if (not path.is_absolute() or value.startswith(('\\\\', '//'))
                or path.name.casefold() != filename.casefold() or '"' in value):
            continue
        try:
            if not path.is_file() or path.is_symlink():
                continue
            executable = path.resolve(strict=True)
            if (not executable.is_absolute() or str(executable).startswith(('\\\\', '//'))
                    or executable.name.casefold() != filename.casefold()):
                continue
        except OSError:
            continue
        return {'name': label, 'executable': str(executable), 'flags': flags}
    raise WordOpenError('未找到可直接打开 Word 的已安装程序，请检查 Word 或 WPS 安装。')


def request_open_word(path):
    """Launch only. A successful request is not proof that a window has opened."""
    try:
        document = Path(path)
        if document.suffix.lower() != '.docx' or not document.is_file():
            raise WordOpenError('Word 成果文件不存在或格式不受支持。')
        document = document.resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise WordOpenError('Word 成果文件暂不可读取。') from exc
    application = resolve_word_application()
    try:
        subprocess.Popen([application['executable'], *application['flags'], str(document)],
                         shell=False, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, ValueError) as exc:
        raise WordOpenError('办公程序启动失败；文件保留，没有重新执行任务。') from exc
    return application['name']
