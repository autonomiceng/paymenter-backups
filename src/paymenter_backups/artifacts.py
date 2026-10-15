"""Owned artifact resolution shared by verify, download, retention and delete.

A run ID is the only input. Paths come from this installation's private
journal and must name exactly RUN.tar.gz and RUN.manifest.json inside one
canonical owner-only directory outside the app and every document root.
Files open with O_NOFOLLOW relative to that directory's descriptor.
"""
import contextlib
import hashlib
import json
import os
import pathlib
import re
import stat

from .common import BackupError, private_file, read_json

ARCHIVE_SUFFIX = ".tar.gz"
MANIFEST_SUFFIX = ".manifest.json"
MANIFEST_LIMIT = 1024 * 1024
CHUNK = 1024 * 1024
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


def journal(state, run_id):
    path = state.run_path(run_id) / "journal.json"
    try:
        private_file(path)
    except FileNotFoundError:
        raise BackupError("run-not-found") from None
    item = read_json(path)
    if item.get("run_id") != run_id or item.get("installation_uuid") != state.uuid:
        raise BackupError("artifact-identity-mismatch")
    return item


def locations(state, item):
    """Validate recorded locations without following links. Files may be absent.

    Returns the canonical directory, both file paths and the directory's
    (device, inode) so a later open can prove it reached the same directory.
    """
    run_id = item["run_id"]
    paths = []
    for key, suffix in (("archive_path", ARCHIVE_SUFFIX), ("manifest_path", MANIFEST_SUFFIX)):
        value = item.get(key)
        if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
            raise BackupError("artifact-location-invalid")
        path = pathlib.Path(value)
        if str(path) != os.path.normpath(value) or path.name != run_id + suffix:
            raise BackupError("artifact-location-invalid")
        paths.append(path)
    directory = paths[0].parent
    if paths[1].parent != directory:
        raise BackupError("artifact-location-invalid")
    try:
        info = os.lstat(directory)
        canonical = directory.resolve(strict=True)
    except (OSError, RuntimeError):
        raise BackupError("artifact-location-invalid") from None
    if canonical != directory or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BackupError("artifact-location-invalid")
    if directory == state.app or state.app in directory.parents:
        raise BackupError("artifact-location-invalid")
    state.guard_location(directory, "artifact-location-invalid")
    return Location(directory, paths[0], paths[1], (info.st_dev, info.st_ino))


class Location(tuple):
    """(directory, archive, manifest) with the checked directory identity."""
    def __new__(cls, directory, archive, manifest, key):
        value = super().__new__(cls, (directory, archive, manifest))
        value.key = key
        return value


def open_directory(directory):
    """Open a canonical directory one component at a time, never following links."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in pathlib.PurePosixPath(directory).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise BackupError("artifact-location-invalid") from None


@contextlib.contextmanager
def open_owned(location, name):
    """Yield (descriptor, stat) for an owner-only 0600 regular file with one link."""
    directory_fd = open_directory(location[0])
    descriptor = None
    try:
        info = os.fstat(directory_fd)
        if (info.st_dev, info.st_ino) != location.key or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BackupError("artifact-location-invalid")
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            raise BackupError("artifact-missing") from None
        except OSError:
            raise BackupError("artifact-unsafe") from None
        info = os.fstat(descriptor)
        if not owned_file(info):
            raise BackupError("artifact-unsafe")
        yield descriptor, info
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def read_owned(location, name, limit=MANIFEST_LIMIT, seen=None):
    """Read a small owned file through the verified directory descriptor.

    When given, seen[name] records the identity of the file that was read.
    """
    with open_owned(location, name) as (descriptor, info):
        if info.st_size > limit:
            raise BackupError("artifact-unsafe")
        data = b""
        while len(data) <= info.st_size:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            data += chunk
        if identity(os.fstat(descriptor)) != identity(info) or len(data) != info.st_size:
            raise BackupError("artifact-changed")
        if seen is not None:
            seen[name] = identity(info)
        return data


def digest_owned(location, name, seen=None):
    with open_owned(location, name) as (descriptor, info):
        hasher = hashlib.sha256()
        for chunk in iter(lambda: os.read(descriptor, CHUNK), b""):
            hasher.update(chunk)
        if identity(os.fstat(descriptor)) != identity(info):
            raise BackupError("artifact-changed")
        if seen is not None:
            seen[name] = identity(info)
        return info.st_size, hasher.hexdigest()


def stat_owned(location, name, seen):
    """Record the identity of an owned file without reading it."""
    with open_owned(location, name) as (_descriptor, info):
        seen[name] = identity(info)


def remove_owned(location, names, expected):
    """Unlink names inside the verified directory, never re-walking its path.

    A parent replaced after the location check makes the component-wise
    no-follow open fail, so nothing outside the owned directory is removed.
    Each name must still be the owned file checked earlier (expected maps
    name to its identity); a replaced or modified file is kept, including
    one that reuses the checked inode number. Absent names are skipped so an
    interrupted removal can resume.
    """
    directory_fd = open_directory(location[0])
    try:
        info = os.fstat(directory_fd)
        if (info.st_dev, info.st_ino) != location.key or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BackupError("artifact-location-invalid")
        for name in names:
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not owned_file(current) or expected.get(name) != identity(current):
                raise BackupError("artifact-changed")
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def owned_file(info):
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1


def identity(info):
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def read_manifest(state, item, location):
    data = read_owned(location, location[2].name)
    try:
        manifest = json.loads(data)
        archive = manifest["archive"]
        valid = (isinstance(archive["sha256"], str) and HEX_DIGEST.fullmatch(archive["sha256"]) is not None
                 and type(archive["size"]) is int and archive["size"] > 0)
    except (ValueError, KeyError, TypeError):
        raise BackupError("artifact-manifest-invalid") from None
    if not valid:
        raise BackupError("artifact-manifest-invalid")
    if manifest.get("installation_uuid") != state.uuid or manifest.get("run_id") != item["run_id"]:
        raise BackupError("artifact-identity-mismatch")
    return manifest


def deleted(item):
    return bool(item.get("retained_deleted") or item.get("delete_intent") or item.get("retention_delete_intent"))


class Artifact:
    def __init__(self, item, manifest, descriptor, info):
        self.item = item
        self.manifest = manifest
        self.descriptor = descriptor
        self.info = info

    def reader(self):
        """A buffered reader over the already-open descriptor, from offset zero."""
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        return os.fdopen(os.dup(self.descriptor), "rb", closefd=True)

    def digest(self):
        hasher = hashlib.sha256()
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        for chunk in iter(lambda: os.read(self.descriptor, CHUNK), b""):
            hasher.update(chunk)
        return hasher.hexdigest()

    def unchanged(self):
        return identity(os.fstat(self.descriptor)) == identity(self.info)


@contextlib.contextmanager
def open_archive(state, run_id, item=None):
    item = item or journal(state, run_id)
    if deleted(item):
        raise BackupError("artifact-deleted")
    if not item.get("local_complete"):
        raise BackupError("run-incomplete")
    location = locations(state, item)
    manifest = read_manifest(state, item, location)
    with open_owned(location, location[1].name) as (descriptor, info):
        if info.st_size != manifest["archive"]["size"]:
            raise BackupError("archive-checksum-mismatch")
        yield Artifact(item, manifest, descriptor, info)


def verification_current(artifact):
    """True only when a recorded full check matches this open descriptor."""
    record = artifact.item.get("verification")
    if not isinstance(record, dict):
        return False
    return (record.get("identity") == identity(artifact.info)
            and record.get("sha256") == artifact.manifest["archive"]["sha256"])


def download_state(state, item):
    """Cheap readiness for presentation; never hashes the archive."""
    try:
        with open_archive(state, item["run_id"], item) as artifact:
            return "ready" if verification_current(artifact) else "verify"
    except BackupError as error:
        return error.code


def stream(state, run_id, output):
    """Write a header line, then the verified archive bytes from one descriptor.

    The final chunk is withheld until the streamed SHA-256 and descriptor
    identity match the recorded verification, so a changed file never arrives
    complete.
    """
    with open_archive(state, run_id) as artifact:
        if not verification_current(artifact):
            raise BackupError("verification-required")
        expected = artifact.manifest["archive"]
        header = {"ok": True, "size": expected["size"], "sha256": expected["sha256"],
                  "filename": "paymenter-backup-" + run_id + ARCHIVE_SUFFIX}
        write_all(output, json.dumps(header, sort_keys=True).encode() + b"\n")
        hasher = hashlib.sha256()
        os.lseek(artifact.descriptor, 0, os.SEEK_SET)
        pending = b""
        total = 0
        for chunk in iter(lambda: os.read(artifact.descriptor, CHUNK), b""):
            if pending:
                write_all(output, pending)
            hasher.update(chunk)
            total += len(chunk)
            pending = chunk
        if total != expected["size"] or hasher.hexdigest() != expected["sha256"] or not artifact.unchanged():
            raise BackupError("archive-changed-during-download")
        write_all(output, pending)
        return {"run_id": run_id, "size": total}


def write_all(descriptor, data):
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]
