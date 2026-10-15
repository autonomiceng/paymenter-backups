#!/usr/bin/env python3
"""Exact-request native UI worker with a persistent, loopback-only S3 fixture.

The product binding remains valid HTTPS. Only this isolated worker substitutes
the SDK transport, so this proves protocol/UI workflows, not provider or TLS.
"""
import hashlib
import json
import os
import pathlib
import stat
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def admitted_request(argv):
    """Admit only the launcher's exact native request command and fixture binding."""
    if (len(argv) != 11 or [argv[index] for index in (0, 2, 4, 6)] != ['--app', '--private-root', '--bridge', '--php']
            or argv[8] != 'request-worker' or argv[10] != '--execute'):
        raise RuntimeError('loopback S3 requires exactly request-worker ID --execute')
    from native_ui_fixture import controlled_metadata
    app = pathlib.Path(argv[1])
    work, metadata = controlled_metadata(app.parent)
    expected = ['--app', metadata['app'], '--private-root', metadata['private_root'],
                '--bridge', str(pathlib.Path(metadata['app']) / 'extensions/Others/Backups/bridge.php'),
                '--php', metadata['php'], 'request-worker', argv[9], '--execute']
    if argv != expected or metadata.get('fixture_only') is not True or metadata.get('native_scheduler_started') is not False:
        raise RuntimeError('loopback S3 requires the exact disposable browser fixture binding')
    runtime = app / 'extensions/Others/Backups/runtime'
    sys.path[:0] = [str(runtime / 'sdk'), str(runtime / 'src')]
    from paymenter_backups import requests
    from paymenter_backups.common import State, private_file, read_json
    state = State(metadata['app'], metadata['private_root'], initialize=False)
    with state.lock('requests.lock', create=False):
        target = requests.path(state, argv[9])
        private_file(target)
        item = read_json(target)
        if (item.get('request_id') != argv[9] or item.get('installation_uuid') != state.uuid
                or item.get('status') != 'launching' or item.get('action') not in requests.ACTIONS):
            raise RuntimeError('loopback S3 requires an owned launching request')
        arguments = item.get('arguments')
        if (item['action'] == 'run'
                and (not isinstance(arguments, dict) or arguments.get('mode') != 'database-online')):
            raise RuntimeError('loopback S3 worker refuses non-database capture')
    return work, metadata


def object_directory(work):
    objects = work / 'loopback-s3'
    objects.mkdir(mode=0o700, exist_ok=True)
    info = objects.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or objects.resolve(strict=True) != objects):
        raise RuntimeError('loopback S3 object directory must be canonical and owner-private')
    return objects


def main():
    os.umask(0o077)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    work, metadata = admitted_request(sys.argv[1:])
    from paymenter_backups import storage
    from paymenter_backups.cli import main as native_main
    objects = object_directory(work)
    parts = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def target(self):
            parsed = urllib.parse.urlsplit(self.path)
            with (objects / 'trace.jsonl').open('a') as trace:
                trace.write(json.dumps({'method': self.command, 'path': parsed.path}) + '\n')
            return objects / hashlib.sha256(parsed.path.encode()).hexdigest(), urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        def answer(self, code, body=b'', headers=None):
            self.send_response(code)
            self.send_header('Content-Length', str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def data(self):
            return self.rfile.read(int(self.headers.get('Content-Length', 0)))

        def do_PUT(self):
            target, query = self.target()
            data = self.data()
            if 'uploadId' in query:
                parts[(query['uploadId'][0], int(query['partNumber'][0]))] = data
            else:
                target.write_bytes(data)
            self.answer(200, headers={'ETag': '"' + hashlib.md5(data).hexdigest() + '"'})

        def do_POST(self):
            target, query = self.target()
            self.data()
            if 'uploads' in query:
                self.answer(200, b'<InitiateMultipartUploadResult><Bucket>fake-browser-bucket</Bucket><Key>fixture</Key><UploadId>fixture-upload</UploadId></InitiateMultipartUploadResult>')
            else:
                upload = query['uploadId'][0]
                target.write_bytes(b''.join(parts[key] for key in sorted(parts) if key[0] == upload))
                self.answer(200, b'<CompleteMultipartUploadResult><Location>fixture</Location><Bucket>fake-browser-bucket</Bucket><Key>fixture</Key><ETag>fixture</ETag></CompleteMultipartUploadResult>')

        def do_GET(self):
            target, _ = self.target()
            self.answer(200, target.read_bytes()) if target.exists() else self.answer(404, b'<Error><Code>NoSuchKey</Code></Error>')

        def do_HEAD(self):
            target, _ = self.target()
            self.answer(200, target.read_bytes()) if target.exists() else self.answer(404)

        def do_DELETE(self):
            target, _ = self.target()
            target.unlink(missing_ok=True)
            self.answer(204)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_client = storage.client

    def fixture_client(envelope):
        settings = envelope['settings']
        credentials = envelope.get('credentials', {})
        if (settings['s3_endpoint'] != 'https://browser-s3.invalid'
                or settings['s3_bucket'] != 'fake-browser-bucket'
                or credentials.get('access_key') != 'fake-browser-access'
                or credentials.get('secret_key') != 'fake-browser-secret'):
            raise RuntimeError('loopback S3 refuses non-fixture binding or credentials')
        transport = dict(envelope, settings=dict(settings, s3_endpoint='http://127.0.0.1:' + str(server.server_port), s3_path_style=True))
        return original_client(transport)

    storage.client = fixture_client
    try:
        return native_main(sys.argv[1:])
    finally:
        storage.client = original_client
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    raise SystemExit(main())
