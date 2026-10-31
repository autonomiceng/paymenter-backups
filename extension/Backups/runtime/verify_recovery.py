#!/usr/bin/env python3
"""Verify downloaded recovery objects using Python 3.10+ and the stdlib only.

This reads files and never decrypts or extracts them. Successfully complete GPG
decryption (including MDC validation) before passing --recovery and --archive.
Transport-only checks do not authenticate the public descriptor.
"""
import argparse
import contextlib
import gzip
import hashlib
import json
import math
import os
import re
import stat
import tarfile
import uuid

FORMAT = "openpgp-symmetric-aes256-seipd-v1"
DESCRIPTOR_LIMIT = 16384
DOCUMENT_LIMIT = 1024 * 1024 + 65536
MANIFEST_LIMIT = 1024 * 1024
CHUNK = 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}")
RUN = re.compile(r"[A-Za-z0-9_-]{1,100}")


class VerificationError(Exception):
    """Fixed safe failure code; input document contents are never echoed."""


def require(condition, code="remote-descriptor-invalid"):
    if not condition:
        raise VerificationError(code)


def exact(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys))


def integer(value, minimum=1):
    require(type(value) is int and minimum <= value <= 2**63 - 1)


def text(value, maximum=4096):
    require(isinstance(value, str) and len(value.encode("utf-8")) <= maximum and "\0" not in value)


def sha(value):
    require(isinstance(value, str) and HEX.fullmatch(value) is not None)


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextlib.contextmanager
def file_reader(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode), "recovery-file-unsafe")
        yield stream
        require(identity(before) == identity(os.fstat(stream.fileno())), "recovery-file-changed")


def decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result

    def invalid_constant(_value):
        raise VerificationError("remote-descriptor-invalid")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise VerificationError("remote-descriptor-invalid") from None


def read_document(path, limit):
    with file_reader(path) as stream:
        require(os.fstat(stream.fileno()).st_size <= limit)
        data = stream.read(limit + 1)
        require(len(data) <= limit)
        value = decode(data)
        require(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n" == data)
        return value


def blob(value):
    exact(value, ("sha256", "size"))
    sha(value["sha256"])
    integer(value["size"])


def document_identity(value):
    require(type(value["version"]) is int and value["version"] == 1)
    require(value["format"] == FORMAT, "remote-format-unsupported")
    require(isinstance(value["run_id"], str) and RUN.fullmatch(value["run_id"]) is not None)
    installation = value["installation_uuid"]
    require(isinstance(installation, str) and str(uuid.UUID(installation)) == installation)
    key = value["key"]
    exact(key, ("scheme", "salt", "iterations", "value"))
    require(key["scheme"] == "pbkdf2-hmac-sha256-v1")
    require(type(key["iterations"]) is int and key["iterations"] == 600000)
    sha(key["salt"])
    sha(key["value"])


def object_schema(value, installation, run_id, suffix):
    exact(value, ("name", "size", "sha256"))
    blob({field: value[field] for field in ("size", "sha256")})
    name = value["name"]
    text(name)
    require("\\" not in name and not any(ord(char) < 32 or ord(char) == 127 for char in name))
    parts = name.split("/")
    require(len(parts) >= 3 and all(part not in ("", ".", "..") for part in parts))
    require(parts[-2:] == [installation, run_id + suffix])
    return "/".join(parts[:-1]) + "/"


def descriptor_schema(value):
    exact(value, ("version", "format", "installation_uuid", "run_id", "key", "objects"))
    document_identity(value)
    exact(value["objects"], ("archive", "recovery"))
    prefixes = [object_schema(value["objects"][kind], value["installation_uuid"], value["run_id"], suffix)
                for kind, suffix in (("archive", ".tar.gz.gpg"), ("recovery", ".recovery.json.gpg"))]
    require(prefixes[0] == prefixes[1])


def recovery_schema(value, descriptor):
    exact(value, ("version", "format", "installation_uuid", "run_id", "key", "scope", "archive", "manifest"))
    document_identity(value)
    require(all(value[field] == descriptor[field] for field in
                ("version", "format", "installation_uuid", "run_id", "key")))
    prefix = object_schema(value["archive"], value["installation_uuid"], value["run_id"], ".tar.gz.gpg")
    require(value["archive"] == descriptor["objects"]["archive"])
    scope = value["scope"]
    exact(scope, ("endpoint", "region", "bucket", "prefix", "path_style"))
    for field in ("endpoint", "region", "bucket", "prefix"):
        text(scope[field])
    require(scope["prefix"] == prefix and bool(scope["region"]) and bool(scope["bucket"]))
    require(type(scope["path_style"]) is bool)
    manifest_schema(value["manifest"], value["run_id"], value["installation_uuid"])


def check_bytes(stream, expected):
    require(os.fstat(stream.fileno()).st_size == expected["size"], "recovery-object-mismatch")
    stream.seek(0)
    digest = hashlib.sha256()
    count = 0
    while chunk := stream.read(CHUNK):
        count += len(chunk)
        require(count <= expected["size"], "recovery-object-mismatch")
        digest.update(chunk)
    require(count == expected["size"] and digest.hexdigest() == expected["sha256"], "recovery-object-mismatch")


def manifest_schema(manifest, run_id, installation):
    exact(manifest, ("schema_version", "run_id", "installation_uuid", "mode", "created_at", "guarantee",
                     "database_inventory", "excluded_paths", "external_state", "payloads", "archive"))
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1)
    require(manifest["run_id"] == run_id and manifest["installation_uuid"] == installation)
    require(manifest["mode"] in ("full-quiesced", "database-online"))
    created = manifest["created_at"]
    require(type(created) in (int, float) and math.isfinite(created) and created >= 0)
    guarantees = ("database-and-full-app-checkpoint", "transactional-database-and-live-app-files") if manifest["mode"] == "full-quiesced" else ("transactional-database-only",)
    require(manifest["guarantee"] in guarantees)
    text(manifest["external_state"])
    exclusions = manifest["excluded_paths"]
    require(isinstance(exclusions, list) and len(exclusions) <= 16)
    for path in exclusions:
        text(path)
    inventory = manifest["database_inventory"]
    exact(inventory, ("state", "counts", "current_user_grants", "grants_redaction", "scope"))
    require(inventory["state"] == "visible-zero")
    exact(inventory["counts"], ("tables", "routines", "events", "triggers"))
    require(all(type(value) is int and value == 0 for value in inventory["counts"].values()))
    require(isinstance(inventory["current_user_grants"], list) and bool(inventory["current_user_grants"]))
    for grant in inventory["current_user_grants"]:
        text(grant, MANIFEST_LIMIT)
    for field in ("grants_redaction", "scope"):
        text(inventory[field])
    expected = {"database.sql", "app.tar"} if manifest["mode"] == "full-quiesced" else {"database.sql"}
    exact(manifest["payloads"], expected)
    for payload in manifest["payloads"].values():
        blob(payload)
    blob(manifest["archive"])
    require(len(json.dumps(manifest).encode()) <= MANIFEST_LIMIT)


class BoundedReader:
    def __init__(self, source, limit):
        self.source, self.limit, self.count = source, limit, 0

    def read(self, size):
        require(size >= 0, "archive-layout-invalid")
        data = self.source.read(min(size, self.limit - self.count + 1))
        self.count += len(data)
        require(self.count <= self.limit, "archive-layout-invalid")
        return data

    def block(self):
        data = self.read(512)
        require(len(data) == 512, "archive-layout-invalid")
        return data


def check_archive(stream, manifest):
    """Read every gzip byte and reject hidden tar members after the end marker."""
    stream.seek(0)
    expected = manifest["payloads"]
    seen = set()
    root_seen = False
    limit = sum(value["size"] for value in expected.values()) + 2 * MANIFEST_LIMIT
    with gzip.GzipFile(fileobj=stream, mode="rb") as compressed:
        reader = BoundedReader(compressed, limit)
        while True:
            header = reader.block()
            if not any(header):
                require(not any(reader.block()), "archive-layout-invalid")
                # GNU tar pads with zeros to its record size. Require all padding
                # to be zero, while draining gzip through its CRC/length trailer.
                while padding := reader.read(CHUNK):
                    require(not any(padding), "archive-layout-invalid")
                break
            member = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
            name = member.name[2:] if member.name.startswith("./") else member.name
            if member.isdir() and name in ("", "."):
                require(not root_seen and member.size == 0, "archive-layout-invalid")
                root_seen = True
                continue
            require(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE) and not member.linkname,
                    "archive-layout-invalid")
            require(name not in seen and name in set(expected) | {"manifest.json"}, "archive-layout-invalid")
            require(type(member.size) is int and member.size >= 0, "archive-layout-invalid")
            if name == "manifest.json":
                require(member.size <= MANIFEST_LIMIT, "archive-manifest-mismatch")
                data = reader.read(member.size)
                require(len(data) == member.size, "archive-manifest-mismatch")
                require(decode(data) == {key: value for key, value in manifest.items() if key != "archive"},
                        "archive-manifest-mismatch")
            else:
                require(member.size == expected[name]["size"], "archive-payload-mismatch")
                digest = hashlib.sha256()
                remaining = member.size
                while remaining:
                    data = reader.read(min(remaining, CHUNK))
                    require(bool(data), "archive-payload-mismatch")
                    remaining -= len(data)
                    digest.update(data)
                require(digest.hexdigest() == expected[name]["sha256"], "archive-payload-mismatch")
            padding_size = (-member.size) % 512
            padding = reader.read(padding_size)
            require(len(padding) == padding_size and not any(padding), "archive-layout-invalid")
            seen.add(name)
    require(seen == set(expected) | {"manifest.json"}, "archive-layout-invalid")


def verify(args):
    descriptor = read_document(args.descriptor, DESCRIPTOR_LIMIT)
    descriptor_schema(descriptor)
    for path, kind in ((args.archive_ciphertext, "archive"), (args.recovery_ciphertext, "recovery")):
        with file_reader(path) as stream:
            check_bytes(stream, descriptor["objects"][kind])
    result = {"run_id": descriptor["run_id"], "installation_uuid": descriptor["installation_uuid"],
              "key_fingerprint": descriptor["key"]["value"], "ciphertext_matches_descriptor": True,
              "verification": "untrusted-transport-only"}
    if args.recovery:
        recovery = read_document(args.recovery, DOCUMENT_LIMIT)
        recovery_schema(recovery, descriptor)
        result["verification"] = "recovery-document-bound"
        result["mode"] = recovery["manifest"]["mode"]
        result["gpg_completion_required"] = True
        if args.archive:
            with file_reader(args.archive) as stream:
                check_bytes(stream, recovery["manifest"]["archive"])
                check_archive(stream, recovery["manifest"])
            result["verification"] = "plaintext-content-verified"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--descriptor", required=True)
    parser.add_argument("--archive-ciphertext", required=True)
    parser.add_argument("--recovery-ciphertext", required=True)
    parser.add_argument("--recovery", help="recovery JSON after successful GPG decryption")
    parser.add_argument("--archive", help="tar.gz after successful GPG decryption")
    args = parser.parse_args(argv)
    if args.archive and not args.recovery:
        parser.error("--archive requires --recovery")
    try:
        result = verify(args)
    except VerificationError as error:
        print(json.dumps({"verified": False, "code": str(error)}))
        return 1
    except (OSError, EOFError, ValueError, UnicodeError, RecursionError, tarfile.TarError):
        print(json.dumps({"verified": False, "code": "recovery-unreadable"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
