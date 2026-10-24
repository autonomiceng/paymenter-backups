"""Environment boundary for disposable native fixtures, never live apps."""
import os
import pathlib
import stat
import tempfile

# These are controlled defaults, not copies of the invoking shell environment.
# In particular Dotenv must never see inherited APP_*/DB_* configuration.
DEFAULT_ENVIRONMENT = {
    'PATH': '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
    'LANG': 'C.UTF-8',
    'LC_ALL': 'C.UTF-8',
    'TZ': 'UTC',
}


def child_environment(overrides=None):
    result = dict(DEFAULT_ENVIRONMENT)
    if overrides:
        unknown = set(overrides) - set(DEFAULT_ENVIRONMENT)
        if unknown:
            raise ValueError('fixture child environment accepts only PATH/LANG/LC_ALL/TZ')
        result.update(overrides)
    return result


def private_work_directory(prefix, parent=None):
    """Create a random 0700 fixture directory under a checked temporary parent."""
    nested = parent is not None
    parent = pathlib.Path(parent if nested else os.environ.get('TMPDIR', tempfile.gettempdir()))
    try:
        parent = parent.resolve(strict=True)
        info = parent.stat()
    except OSError:
        raise RuntimeError('temporary directory is unavailable') from None
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError('temporary parent is not a directory')
    if info.st_mode & stat.S_IWOTH and not info.st_mode & stat.S_ISVTX:
        raise RuntimeError('temporary parent is world-writable without sticky bit')
    if nested and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise RuntimeError('nested temporary parent is not owner-private')
    path = pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    path.chmod(0o700)
    return path


def require_private_workroot(path, prefix, parent_prefix):
    """Validate a fixture workroot and its private random base under TMPDIR."""
    work = pathlib.Path(path).absolute()
    base = work.parent
    temporary = pathlib.Path(os.environ.get('TMPDIR', tempfile.gettempdir())).resolve(strict=True)
    temporary_info = temporary.stat()
    if (not stat.S_ISDIR(temporary_info.st_mode)
            or temporary_info.st_mode & stat.S_IWOTH and not temporary_info.st_mode & stat.S_ISVTX):
        raise RuntimeError('temporary parent is world-writable without sticky bit')
    if (not work.name.startswith(prefix) or not base.name.startswith(parent_prefix)
            or base.resolve(strict=True).parent != temporary):
        raise RuntimeError('fixture workroot is outside its private temporary base')
    for directory in (base, work):
        try:
            info = directory.lstat()
        except OSError:
            raise RuntimeError('fixture workroot is unavailable') from None
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise RuntimeError('fixture workroot is not owner-private')
    return work.resolve(strict=True)
