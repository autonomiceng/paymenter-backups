"""Private state, atomic journals and deliberately small public errors."""
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import stat
import time
import uuid


class BackupError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(path, check=None):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if check:
                check()
            result.update(chunk)
    return result.hexdigest()


def read_json(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        raise BackupError("state-unreadable") from None


def atomic_json(path, value):
    path = pathlib.Path(path)
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex)
    data = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def private_file(path):
    info = pathlib.Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BackupError("private-state-permissions")


def identity(path):
    """Include inode and ctime: equal bytes never confer ownership."""
    path = pathlib.Path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise BackupError("maintenance-unsafe")
        hasher = hashlib.sha256()
        for chunk in iter(lambda: os.read(descriptor, 65536), b""):
            hasher.update(chunk)
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_uid")
        if any(getattr(before, key) != getattr(after, key) for key in stable):
            raise BackupError("maintenance-changed")
        return {"device": after.st_dev, "inode": after.st_ino, "size": after.st_size,
                "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns,
                "sha256": hasher.hexdigest()}
    finally:
        os.close(descriptor)


class State:
    def __init__(self, app, root, initialize=True, extra_docroots=None):
        os.umask(0o077)
        self.app = pathlib.Path(app).resolve(strict=True)
        self.root = pathlib.Path(root).absolute()
        if self.root.is_symlink():
            raise BackupError("private-root-unsafe")
        canonical = self.root.resolve()
        if canonical == self.app or self.app in canonical.parents:
            raise BackupError("private-root-inside-app")
        self.document_roots = [(self.app / "public").resolve()] + [pathlib.Path(path).resolve() for path in (extra_docroots or [])]
        self.root = canonical
        self.initialized = False
        self.installation = None
        self.uuid = None
        self.binding = self.root / "installation.json"
        self.guard_location(self.root, "private-root-inside-docroot")
        if not initialize and not self.root.exists():
            return
        if initialize:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = self.root.resolve(strict=True)
        if self.root == self.app or self.app in self.root.parents:
            raise BackupError("private-root-inside-app")
        info = self.root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or self.app.stat().st_uid != os.getuid():
            raise BackupError("owner-mismatch")
        self.binding = self.root / "installation.json"
        if initialize:
            with self.lock("identity.lock"):
                if not self.binding.exists():
                    atomic_json(self.binding, {"schema_version": 1, "app": str(self.app),
                                "owner_uid": os.getuid(), "installation_uuid": str(uuid.uuid4())})
        if not self.binding.exists():
            return
        private_file(self.binding)
        self.installation = read_json(self.binding)
        if self.installation.get("app") != str(self.app) or self.installation.get("owner_uid") != os.getuid():
            raise BackupError("installation-mismatch")
        self.uuid = self.installation["installation_uuid"]
        self.initialized = True
        policy = self.root / "policy.json"
        if policy.exists():
            private_file(policy)
            self.document_roots += [pathlib.Path(path).resolve() for path in read_json(policy).get("extra_docroots", [])]
            self.guard_location(self.root, "private-root-inside-docroot")
        for name in ("runs", "requests"):
            directory = self.root / name
            if initialize:
                directory.mkdir(mode=0o700, exist_ok=True)
            if not directory.exists():
                continue
            if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
                raise BackupError("private-state-permissions")

    def guard_location(self, path, code="destination-inside-docroot"):
        canonical = pathlib.Path(path).resolve()
        if any(canonical == root or root in canonical.parents for root in self.document_roots):
            raise BackupError(code)

    def apply_policy(self, settings, write=False):
        roots = [pathlib.Path(path).resolve() for path in settings.get("extra_docroots", [])]
        self.document_roots = [(self.app / "public").resolve()] + roots
        self.guard_location(self.root, "private-root-inside-docroot")
        if write:
            atomic_json(self.root / "policy.json", {"schema_version": 1, "extra_docroots": [str(path) for path in roots]})

    @contextlib.contextmanager
    def lock(self, name="run.lock", blocking=False, create=True):
        if not create and not (self.root / name).exists():
            yield None
            return
        flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
        descriptor = os.open(self.root / name, flags | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BackupError("private-state-permissions")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise BackupError("busy") from None
            yield descriptor
        finally:
            os.close(descriptor)

    def run_path(self, run):
        if not isinstance(run, str) or len(run) > 100 or not run or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run):
            raise BackupError("invalid-run-id")
        path = self.root / "runs" / run
        if path.is_symlink():
            raise BackupError("private-state-permissions")
        return path

    def journals(self):
        return [read_json(path) for path in sorted((self.root / "runs").glob("*/journal.json"))]

    def save(self, journal):
        journal["updated_at"] = time.time()
        atomic_json(self.run_path(journal["run_id"]) / "journal.json", journal)
