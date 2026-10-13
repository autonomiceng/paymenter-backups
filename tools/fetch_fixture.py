#!/usr/bin/env python3
"""Fetch only the pinned official stock release, verify before extraction."""
import argparse
import hashlib
import pathlib
import tarfile
import urllib.request

URL = 'https://github.com/Paymenter/Paymenter/releases/download/v1.5.9/paymenter.tar.gz'
SHA256 = 'ed69a689338f112f163f8fff3254fd3a1fc6a947f5bdca15fa4a1d86b4344125'


def fetch(destination, archive=None):
    destination = pathlib.Path(destination).resolve()
    if destination.exists():
        raise RuntimeError('fixture destination must be new')
    archive = pathlib.Path(archive) if archive else destination.parent / 'paymenter-v1.5.9.tar.gz'
    if not archive.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL, timeout=120) as response, archive.open('wb') as out:
            while block := response.read(1024 * 1024):
                out.write(block)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError('official release SHA256 mismatch')
    destination.mkdir(mode=0o700)
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            path = destination / member.name
            if not path.resolve().is_relative_to(destination):
                raise RuntimeError('unsafe stock archive path')
            if member.issym() or member.islnk():
                raise RuntimeError('unexpected stock archive link')
        tar.extractall(destination)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination')
    parser.add_argument('--archive')
    args = parser.parse_args()
    fetch(args.destination, args.archive)
