"""Encrypted recovery proof confined to an already captured disposable fixture.

Run as a child so all engine and SDK imports come from the installed ZIP.
The HTTP service is a loopback protocol fixture, does not test live providers or TLS.
"""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', required=True)
    parser.add_argument('--php', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--gpg-writer', default='/usr/bin/gpg')
    parser.add_argument('--gpg-reader', default='/usr/bin/gpg')
    args = parser.parse_args()
    os.umask(0o077)
    work = pathlib.Path(args.work).resolve(strict=True)
    root = pathlib.Path(__file__).resolve().parents[1]
    if work.parent != root / '.native-work' or not work.name.startswith('native-integration-'):
        raise RuntimeError('disposable native integration fixture required')
    app, private = work / 'app', work / 'private'
    runtime = app / 'extensions/Others/Backups/runtime'
    sys.path[:0] = [str(runtime / 'src'), str(runtime / 'sdk')]
    from paymenter_backups import storage
    from paymenter_backups.common import State
    from paymenter_backups.config import load
    cli = [sys.executable, '-I', str(runtime / 'cli.py'), '--app', str(app),
           '--private-root', str(private), '--php', args.php]

    def command(arguments, payload=None):
        result = subprocess.run(cli + arguments, input=payload, capture_output=True, timeout=180)
        if result.returncode:
            raise RuntimeError('disposable encrypted recovery command failed')
        return json.loads(result.stdout)

    generated = command(['encryption-key', 'generate'])
    fingerprint = generated['fingerprint']
    command(['encryption-key', 'acknowledge'], json.dumps({'fingerprint': fingerprint}).encode())
    scratch = work / 'encrypted-recovery'
    scratch.mkdir(mode=0o700)
    command(['recovery-key', 'export', '--output', str(scratch / 'key.json')])
    exported = json.loads((scratch / 'key.json').read_text())
    objects, parts, trace = {}, {}, []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def target(self):
            parsed = urllib.parse.urlsplit(self.path)
            trace.append([self.command, parsed.path])
            return parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        def answer(self, code, body=b'', **headers):
            self.send_response(code)
            self.send_header('Content-Length', str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def do_PUT(self):
            name, query = self.target()
            data = self.rfile.read(int(self.headers['Content-Length']))
            if 'uploadId' in query:
                parts[int(query['partNumber'][0])] = data
            else:
                objects[name] = data
            self.answer(200, ETag='"' + hashlib.md5(data).hexdigest() + '"')

        def do_POST(self):
            name, query = self.target()
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if 'uploads' in query:
                self.answer(200, b'<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>fixture</Key><UploadId>owned</UploadId></InitiateMultipartUploadResult>')
            else:
                objects[name] = b''.join(parts[index] for index in sorted(parts))
                parts.clear()
                self.answer(200, b'<CompleteMultipartUploadResult><Location>fixture</Location><Bucket>fixture</Bucket><Key>fixture</Key><ETag>fixture</ETag></CompleteMultipartUploadResult>')

        def do_GET(self):
            name, _ = self.target()
            if name in objects:
                self.answer(200, objects[name])
            else:
                self.answer(404, b'<Error><Code>NoSuchKey</Code></Error>')

        do_HEAD = do_GET

        def do_DELETE(self):
            name, _ = self.target()
            objects.pop(name, None)
            self.answer(204)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    envelope = load(argparse.Namespace(app=str(app), private_root=str(private),
                                       bridge=str(app / 'extensions/Others/Backups/bridge.php'), php=args.php))
    envelope['settings'].update(destination='s3', upload_s3=True, keep_local=True, s3_encryption=True,
                                s3_endpoint='https://restore-fixture.invalid', s3_region='fixture',
                                s3_bucket='fixture', s3_prefix='restore', s3_path_style=True)
    envelope['credentials'] = {'access_key': 'fake-restore-access', 'secret_key': 'fake-restore-secret'}
    envelope['tools']['gpg'] = args.gpg_writer
    original_client = storage.client

    def fixture_client(value):
        if value['settings']['s3_endpoint'] != 'https://restore-fixture.invalid':
            raise RuntimeError('non-fixture S3 binding refused')
        transport = dict(value, settings=dict(value['settings'], s3_endpoint='http://127.0.0.1:' + str(server.server_port)))
        return original_client(transport)

    storage.client = fixture_client
    state = State(app, private, initialize=False)
    for index in range(5000):
        run_id = 'history-' + str(index)
        directory = state.run_path(run_id)
        directory.mkdir(mode=0o700)
        state.save({'run_id': run_id, 'installation_uuid': state.uuid, 'created_at': index + 1,
                    'retained_deleted': True, 'service_recovered': True})
    # Capture timeout deliberately cannot accommodate the GnuPG key derivation;
    # transfer/proof deadlines must be independent of capture/billing admission.
    envelope['settings']['capture_timeout'] = 0.001
    try:
        delivered = storage.deliver(state, envelope, args.run)
        item = next(item for item in state.journals() if item['run_id'] == args.run)
        remote = item['remote']
        provider = fixture_client(envelope)
        for field, name in [('archive_key', 'archive.gpg'), ('recovery_key', 'recovery.gpg'), ('manifest_key', 'descriptor.json')]:
            response = provider.get_object(Bucket='fixture', Key=remote[field])
            try:
                (scratch / name).write_bytes(response['Body'].read())
            finally:
                response['Body'].close()
        provider.close()
    finally:
        storage.client = original_client
        server.shutdown()
        server.server_close()
        thread.join()
    # Fresh-machine verifier and GnuPG receive only downloaded objects and the
    # separately exported secret. They never read the original private store.
    shutil.copyfile(runtime / 'verify_recovery.py', scratch / 'verify_recovery.py')
    home = scratch / 'gpg-home'
    home.mkdir(mode=0o700)
    statuses = {}
    try:
        for name in ('archive', 'recovery'):
            with (scratch / (name + '.status')).open('wb') as status:
                command_line = [args.gpg_reader, '--no-options', '--homedir', str(home), '--batch',
                                '--pinentry-mode', 'loopback', '--no-symkey-cache', '--rfc4880',
                                '--passphrase-fd', '0', '--status-fd', str(status.fileno()),
                                '--output', str(scratch / (name + '.partial')), '--decrypt', str(scratch / (name + '.gpg'))]
                result = subprocess.run(command_line, input=(exported['secret'] + '\n').encode(),
                                        pass_fds=(status.fileno(),), capture_output=True, timeout=180,
                                        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
            tokens = [line.split()[1:] for line in (scratch / (name + '.status')).read_bytes().splitlines()
                      if line.startswith(b'[GNUPG:] ')]
            names = {token[0] for token in tokens if token}
            infos = [token[1:] for token in tokens if token and token[0] == b'DECRYPTION_INFO']
            if (result.returncode or not {b'DECRYPTION_OKAY', b'END_DECRYPTION'} <= names
                    or {b'BADMDC', b'ERRMDC', b'NODATA'} & names or not infos
                    or any(info[:2] != [b'2', b'9'] or len(info) > 2 and info[2] != b'0' for info in infos)):
                raise RuntimeError('encrypted restore GnuPG profile/completion refused')
            target = scratch / ('backup.tar.gz' if name == 'archive' else 'recovery.json')
            (scratch / (name + '.partial')).rename(target)
            statuses[name] = {'pinned_profile': True, 'mdc_completed': True, 'exit_code': result.returncode}
    finally:
        gpgconf = str(pathlib.Path(args.gpg_reader).resolve().with_name('gpgconf'))
        subprocess.run([gpgconf, '--homedir', str(home), '--kill', 'gpg-agent'], capture_output=True, timeout=10, check=True)
        subprocess.run([gpgconf, '--homedir', str(home), '--remove-socketdir'], capture_output=True, timeout=10, check=True)
    verification = subprocess.run([sys.executable, '-I', str(scratch / 'verify_recovery.py'),
                                  '--descriptor', str(scratch / 'descriptor.json'),
                                  '--archive-ciphertext', str(scratch / 'archive.gpg'),
                                  '--recovery-ciphertext', str(scratch / 'recovery.gpg'),
                                  '--recovery', str(scratch / 'recovery.json'), '--archive', str(scratch / 'backup.tar.gz')],
                                 cwd=scratch, capture_output=True, timeout=180, env={'PATH': '/usr/bin:/bin'})
    if verification.returncode:
        raise RuntimeError('shipped standalone recovery verifier refused')
    verified = json.loads(verification.stdout)
    if verified['mode'] != 'full-quiesced' or verified['verification'] != 'plaintext-content-verified':
        raise RuntimeError('full encrypted restore proof required')
    original_digest = hashlib.sha256(pathlib.Path(item['archive_path']).read_bytes()).hexdigest()
    if hashlib.sha256((scratch / 'backup.tar.gz').read_bytes()).hexdigest() != original_digest:
        raise RuntimeError('encrypted full checkpoint changed')
    print(json.dumps({'installed_zip_runtime_and_sdk': True, 'loopback_s3_sdk_delivery': delivered['remote_delivered'],
                      'independent_downloads': True, 'gpg': statuses, 'standalone_verifier': verified,
                      'gpg_writer': subprocess.check_output([args.gpg_writer, '--version']).decode().splitlines()[0],
                      'gpg_reader': subprocess.check_output([args.gpg_reader, '--version']).decode().splitlines()[0],
                      'historical_journals': 5000, 'capture_timeout_independent': True,
                      'plaintext_archive_bytes': (scratch / 'backup.tar.gz').stat().st_size,
                      'original_checkpoint_sha256': original_digest,
                      'restore_archive': str(scratch / 'backup.tar.gz'), 'real_provider_or_tls': False}))


if __name__ == '__main__':
    main()
