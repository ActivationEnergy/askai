"""Environment or explicitly saved API credentials, isolated from ordinary config."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .core import AskError, api_key_env_name, config_path

MAX_STORE_BYTES = 1024 * 1024


def store_path():
    return config_path().parent / 'credentials.json'


def _scope(provider):
    base = provider.get('base_url', '')
    if not isinstance(base, str):
        raise AskError('API 地址必须为字符串')
    text = json.dumps([base.rstrip('/'), api_key_env_name(provider)], ensure_ascii=True)
    return hashlib.sha256(text.encode()).hexdigest()


def validate_key(value):
    if not isinstance(value, str) or not 0 < len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise AskError('密钥须为不含空格、换行或控制字符的非空文本，最多 4096 字符')
    return value


def _check_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise AskError('密钥文件须为本人拥有、无链接且仅本人可读写的普通文件')


def _read():
    path = store_path()
    if os.name != 'posix':
        if path.exists():
            raise AskError('此平台暂不支持本地密钥存储，请使用环境变量')
        return {'format': 'askai-api-credentials', 'version': 1, 'keys': {}}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'format': 'askai-api-credentials', 'version': 1, 'keys': {}}
    except OSError:
        raise AskError('无法安全读取密钥文件，请检查文件权限与链接') from None
    try:
        _check_file(fd)
        from .startup import inspect_metadata
        inspect_metadata(path, require_write=False)
        with os.fdopen(os.dup(fd), 'rb') as source:
            raw = source.read(MAX_STORE_BYTES + 1)
        if len(raw) > MAX_STORE_BYTES:
            raise AskError('密钥文件过大，未读取')
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('format') != 'askai-api-credentials'
                or type(data.get('version')) is not int or data.get('version') != 1
                or not isinstance(data.get('keys'), dict)):
            raise ValueError('invalid store')
        for scope, record in data['keys'].items():
            if not isinstance(scope, str) or len(scope) != 64 or any(c not in '0123456789abcdef' for c in scope):
                raise ValueError('invalid scope')
            validate_key(record)
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise AskError('密钥文件格式无效，未使用或覆盖其中的内容') from None
    finally:
        os.close(fd)


def _record(provider):
    if provider.get('type') != 'api' or not provider.get('base_url'):
        return None
    return _read()['keys'].get(_scope(provider))


def stored_key(provider):
    record = _record(provider)
    return record or ''


def api_key(provider):
    """Never export a pasted key into the process environment or child argv."""
    record = _record(provider)
    if record:
        return record
    return os.environ.get(api_key_env_name(provider), '')


def source_label(provider):
    record = _record(provider)
    if record:
        return '本地保存的密钥'
    name = api_key_env_name(provider)
    if name and os.environ.get(name):
        return f'环境变量 {name}'
    return f'未配置可用密钥（环境变量 {name or "未指定"} 未设置）'


@contextmanager
def _mutation_lock():
    if os.name != 'posix':
        raise AskError('此平台暂不支持本地密钥存储，请使用环境变量')
    import fcntl
    parent = store_path().parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise AskError('密钥目录须归本人所有，且不能允许其他用户写入')
    fd = os.open(parent / 'credentials.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _check_file(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AskError('另一设置进程正在保存密钥，请稍后重试') from None
        yield
    finally:
        os.close(fd)


def save_key(provider, value):
    """Replace/activate one key, or delete it with None; never back it up."""
    _update(provider, value)


def _update(provider, value):
    if provider.get('type') != 'api':
        raise AskError('此功能仅用于 API 后端')
    if value is not None:
        validate_key(value)
    # Reuse the request validator before accepting a credential for an endpoint.
    from .providers import api_base_url
    api_base_url(provider)
    from .startup import inspect_metadata
    path = store_path()
    with _mutation_lock():
        data = _read()
        if path.exists():
            inspect_metadata(path)
        scope = _scope(provider)
        if value is None:
            if scope not in data['keys']:
                return
            del data['keys'][scope]
        else:
            data['keys'][scope] = value
        raw = (json.dumps(data, ensure_ascii=True) + '\n').encode()
        if len(raw) > MAX_STORE_BYTES:
            raise AskError('密钥文件过大，未保存')
        fd, name = tempfile.mkstemp(prefix='.credentials-', suffix='.tmp', dir=path.parent)
        temporary = Path(name)
        try:
            # Reject inherited ACLs before writing any secret bytes.
            inspect_metadata(temporary)
            with os.fdopen(fd, 'wb') as output:
                fd = None
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if fd is not None:
                os.close(fd)
            temporary.unlink(missing_ok=True)
