#!/usr/bin/env python3
"""Build deterministic native uploader ZIP with isolated, hash-pinned SDK."""
import argparse
import hashlib
import json
import pathlib
import shutil
import re
import urllib.request
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def build(output, wheelhouse=None):
    with tempfile.TemporaryDirectory(prefix='paymenter-backups-build-') as temporary:
        stage = pathlib.Path(temporary) / 'Backups'
        shutil.copytree(ROOT / 'extension' / 'Backups', stage)
        shutil.copytree(ROOT / 'src', stage / 'runtime' / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('LICENSE', 'README.md', 'docs', 'NOTICES.md'):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, stage / name)
            else:
                shutil.copy2(source, stage / name)
        sdk = stage / 'runtime' / 'sdk'
        sdk.mkdir()
        lock = (ROOT / 'requirements-s3.lock').read_text().replace('\\\n', ' ')
        for line in lock.splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            match = re.match(r'([A-Za-z0-9_-]+)==([^ ]+)', line)
            if not match:
                raise RuntimeError('invalid SDK lock')
            name, version = match.groups()
            permitted = set(re.findall(r'--hash=sha256:([a-f0-9]{64})', line))
            with urllib.request.urlopen('https://pypi.org/pypi/' + name + '/' + version + '/json', timeout=30) as response:
                metadata = json.load(response)
            wheels = [entry for entry in metadata['urls'] if entry['filename'].endswith('-none-any.whl') and entry['digests']['sha256'] in permitted]
            if len(wheels) != 1:
                raise RuntimeError('one hash-locked portable wheel required: ' + name)
            entry = wheels[0]
            source = pathlib.Path(wheelhouse) / entry['filename'] if wheelhouse else None
            if source and source.exists():
                contents = source.read_bytes()
            else:
                with urllib.request.urlopen(entry['url'], timeout=120) as response:
                    contents = response.read(64 * 1024 * 1024 + 1)
            if len(contents) > 64 * 1024 * 1024 or hashlib.sha256(contents).hexdigest() not in permitted:
                raise RuntimeError('SDK wheel hash mismatch: ' + name)
            import io
            with zipfile.ZipFile(io.BytesIO(contents)) as wheel:
                for member in wheel.infolist():
                    if not (sdk / member.filename).resolve().is_relative_to(sdk):
                        raise RuntimeError('unsafe wheel member')
                wheel.extractall(sdk)
        # Explicit S3 clients need S3 models and shared endpoint/partition data.
        # Remove other AWS service models so native 10MiB upload remains usable.
        service_data = sdk / 'botocore' / 'data'
        removed_services = []
        for model in sorted(service_data.iterdir()):
            if model.is_dir() and model.name != 's3':
                removed_services.append(model.name)
                shutil.rmtree(model)
        for cache in stage.rglob('__pycache__'):
            shutil.rmtree(cache)
        licenses = stage / 'THIRD_PARTY_LICENSES'
        licenses.mkdir()
        records = []
        for distribution in sorted((stage / 'runtime' / 'sdk').glob('*.dist-info')):
            candidates = [p for p in distribution.rglob('*') if p.is_file() and any(word in p.name.lower() for word in ('license', 'copying', 'notice'))]
            if not candidates:
                raise RuntimeError('dependency has no shipped license: ' + distribution.name)
            target = licenses / distribution.name
            target.mkdir()
            for index, source in enumerate(sorted(candidates)):
                shutil.copy2(source, target / (str(index) + '-' + source.name))
            records.append(distribution.name)
        (stage / 'SDK-MANIFEST.json').write_text(json.dumps({'schema_version': 1, 'distributions': records, 'service_scope': ['s3'], 'removed_service_count': len(removed_services), 'requirements_sha256': hashlib.sha256((ROOT / 'requirements-s3.lock').read_bytes()).hexdigest()}, indent=2) + '\n')
        output = pathlib.Path(output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for source in sorted(stage.rglob('*')):
                if not source.is_file():
                    continue
                name = str(source.relative_to(stage.parent))
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, source.read_bytes())
        if output.stat().st_size > 10240 * 1024:
            raise RuntimeError('release exceeds native uploader 10MiB limit')
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        output.with_suffix(output.suffix + '.sha256').write_text(digest + '  ' + output.name + '\n')
        return output, digest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='dist/Backups-0.1.2.zip')
    parser.add_argument('--wheelhouse')
    args = parser.parse_args()
    artifact, digest = build(args.output, args.wheelhouse)
    print(json.dumps({'artifact': str(artifact), 'sha256': digest}))
