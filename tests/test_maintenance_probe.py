"""Real HTTP/TLS requests prove the Basic guard without weakening native503 proof."""
import base64
import contextlib
import http.server
import json
import os
import pathlib
import shutil
import signal
import time
import ssl
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

from paymenter_backups.common import BackupError
from paymenter_backups.config import redact
from paymenter_backups.maintenance import probe, probe_authorization, probe_origin, probe_request


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = pathlib.Path(cls.temporary.name)
        cls.cert = cls.root / 'cert.pem'
        cls.key = cls.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(cls.key), '-out', str(cls.cert), '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = pathlib.Path(self.temp.name)
        framework = self.app / 'storage/framework'
        framework.mkdir(parents=True)
        (framework / 'down').write_text(json.dumps({'status': 503, 'template': 'EXACT NATIVE TEMPLATE', 'except': [], 'secret': None, 'redirect': None}))
        (framework / 'maintenance.php').write_text('<?php // early native fixture')
        self.calls = []
        self.status = 503
        self.body = b'EXACT NATIVE TEMPLATE'
        self.location = None
        self.drip = False
        self.require_auth = True
        self.auth = 'Basic ' + base64.b64encode('USER-CANARY:p:a:ss-CANARY'.encode()).decode()

    @contextlib.contextmanager
    def server(self, tls=True):
        fixture = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.calls.append((self.path, dict(self.headers)))
                status = fixture.status if not fixture.require_auth or self.headers.get('Authorization') == fixture.auth else 401
                self.send_response(status)
                self.send_header('Set-Cookie', 'maintenance-bypass=forbidden')
                if fixture.location:
                    self.send_header('Location', fixture.location)
                self.end_headers()
                if fixture.drip:
                    try:
                        for byte in fixture.body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.05)
                    except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
                        pass
                else:
                    self.wfile.write(fixture.body)
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.cert, self.key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield ('https' if tls else 'http') + '://localhost:' + str(server.server_port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def envelope(self, url, credentials=True):
        value = {'app': str(self.app), 'framework': {'app_url': url + '/billing'}}
        if credentials:
            value['maintenance_probe_credentials'] = {'origin': url, 'username': 'USER-CANARY', 'password': 'p:a:ss-CANARY'}
        return value

    def trusted(self):
        # Only replace the compiled system CA path in this isolated test. The
        # production interface has no user-selectable trust override.
        return patch('paymenter_backups.maintenance.ssl.get_default_verify_paths', return_value=SimpleNamespace(openssl_cafile=str(self.cert), openssl_capath='/nonexistent-fixture-ca'))

    def assert_fails(self, envelope, code='maintenance-probe-failed'):
        with self.assertRaises(BackupError) as error:
            probe(envelope)
        self.assertEqual(error.exception.code, code)
        self.assertNotIn('CANARY', str(error.exception))

    def test_real_tls_basic_exact503_no_cookie_or_redirect(self):
        with self.server() as url, self.trusted():
            envelope = self.envelope(url)
            self.assert_fails(self.envelope(url, False))
            probe(envelope)
            probe(envelope)
            self.assertNotEqual(self.calls[1][0], self.calls[2][0])
            self.assertTrue(self.calls[1][0].startswith('/billing/__backup_probe_'))
            self.assertEqual(self.calls[1][1]['Authorization'], self.auth)
            self.assertTrue(all('Cookie' not in headers for _, headers in self.calls))
            for status, body in [(401, self.body), (200, self.body), (503, b'WRONG'), (503, self.body + b'x'), (503, self.body[:-1]), (404, self.body)]:
                self.status, self.body = status, body
                self.assert_fails(envelope)

    def test_redirects_never_follow_even_same_origin(self):
        with self.server() as url, self.server(tls=False) as sink, self.trusted():
            for location in [url + '/bypass', sink + '/steal', '/relative']:
                for status in [301, 302, 303, 307, 308]:
                    self.status, self.location = status, location
                    before = len(self.calls)
                    self.assert_fails(self.envelope(url))
                    self.assertEqual(len(self.calls), before + 1)

    def test_http_anonymous_supported_but_credentials_refused_before_request(self):
        with self.server(tls=False) as url:
            self.require_auth = False
            probe(self.envelope(url, False))
            before = len(self.calls)
            self.assert_fails(self.envelope(url), 'maintenance-probe-credentials-invalid')
            self.assertEqual(len(self.calls), before)

    def test_slow_drip_total_deadline_and_oversized_native_template(self):
        with self.server() as url, self.trusted(), patch('paymenter_backups.maintenance.PROBE_TIMEOUT', 0.15):
            self.drip = True
            started = time.monotonic()
            handler = signal.getsignal(signal.SIGALRM)
            self.assert_fails(self.envelope(url))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(signal.getsignal(signal.SIGALRM), handler)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
            (self.app / 'storage/framework/down').write_text(json.dumps({'status': 503, 'template': 'x'*65537}))
            before = len(self.calls)
            self.assert_fails(self.envelope(url), 'maintenance-unsafe')
            self.assertEqual(len(self.calls), before)

    def test_existing_alarm_is_preserved_and_proof_refuses(self):
        with self.server() as url, self.trusted():
            handler = signal.getsignal(signal.SIGALRM)
            signal.setitimer(signal.ITIMER_REAL, 20)
            try:
                self.assert_fails(self.envelope(url))
                self.assertEqual(signal.getsignal(signal.SIGALRM), handler)
                self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 19)
                self.assertFalse(self.calls)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)

    def test_utf8_credentials_are_sent_as_utf8_basic(self):
        with self.server() as url, self.trusted():
            envelope = self.envelope(url)
            envelope['maintenance_probe_credentials'].update(username='usér', password='päss:秘密')
            self.auth = 'Basic ' + base64.b64encode('usér:päss:秘密'.encode('utf-8')).decode()
            probe(envelope)
            self.assertEqual(self.calls[-1][1]['Authorization'], self.auth)

    def test_origin_mismatch_rejected_before_network(self):
        with self.server() as url, self.trusted():
            envelope = self.envelope(url)
            for origin in ['https://other.invalid', 'https://localhost', url.replace('localhost', '127.0.0.1')]:
                envelope['maintenance_probe_credentials']['origin'] = origin
                self.assert_fails(envelope, 'maintenance-probe-origin-mismatch')
            self.assertFalse(self.calls)

    def test_transport_request_uses_canonical_authority_and_effective_port(self):
        cases = [('https://EXAMPLE.com:443/base/', 'example.com'), ('https://EXAMPLE.com:8443/base', 'example.com:8443'), ('http://EXAMPLE.com:443/base', 'example.com:443'), ('http://EXAMPLE.com/base', 'example.com'), ('http://EXAMPLE.com:80/base', 'example.com:80'), ('https://[0:0:0:0:0:0:0:1]:443/base', '[::1]')]
        with self.trusted():
            for url, authority in cases:
                with self.subTest(url=url):
                    response = SimpleNamespace(status=404)
                    opener = Mock()
                    opener.open.return_value = response
                    with patch('paymenter_backups.maintenance.urllib.request.build_opener', return_value=opener):
                        probe_request({'framework': {'app_url': url}})
                    request = opener.open.call_args.args[0]
                    self.assertEqual(request.host, authority)
                    self.assertTrue(request.selector.startswith('/base/__backup_probe_'))

    def test_malformed_http_and_unavailable_system_trust_refuse_safely(self):
        with self.server() as url, self.trusted():
            self.status = 0
            self.assert_fails(self.envelope(url))
        self.calls.clear()
        no_trust = SimpleNamespace(openssl_cafile='/missing-fixture-ca', openssl_capath='/missing-fixture-cadir')
        with self.server() as url, patch('paymenter_backups.maintenance.ssl.get_default_verify_paths', return_value=no_trust):
            self.assert_fails(self.envelope(url))
            self.assertFalse(self.calls)

    def test_tls_wrong_hostname_refuses_even_trusted_fixture_ca(self):
        with self.server() as url, self.trusted():
            self.assert_fails(self.envelope(url.replace('localhost', '127.0.0.1')))
            self.assertFalse(self.calls)

    def test_hostile_environment_cannot_supply_ca_proxy_or_tls_log(self):
        with self.server() as url:
            keylog = self.app / 'tls-keylog'
            hostile = {'SSL_CERT_FILE': str(self.cert), 'SSL_CERT_DIR': str(self.root), 'SSLKEYLOGFILE': str(keylog), 'HTTPS_PROXY': 'http://127.0.0.1:1', 'http_proxy': 'http://127.0.0.1:1', 'REQUESTS_CA_BUNDLE': str(self.cert), 'OPENSSL_CONF': '/hostile', 'PYTHONPATH': '/hostile', 'AWS_CA_BUNDLE': str(self.cert)}
            with patch.dict(os.environ, hostile):
                self.assert_fails(self.envelope(url))  # fixture self-signed CA remains untrusted
                self.assertFalse(self.calls)
                with self.trusted():
                    probe(self.envelope(url))
                self.assertFalse(keylog.exists())
                self.assertFalse(any(name in os.environ for name in hostile))

    def test_diagnostic_transport_never_claims_maintenance(self):
        with self.server() as url, self.trusted():
            self.status = 404
            with probe_request(self.envelope(url)) as response:
                self.assertEqual(response.status, 404)
            self.assert_fails(self.envelope(url))

    def test_adversarial_credentials_and_urls_refuse_safely(self):
        creds = {'origin': 'https://example.com', 'username': 'user', 'password': 'secret'}
        for key, values in [('username', ['', 'u:s', 'u\r\nCookie: bypass', '\x00', '\x7f', '\ud800', 'a'*8193, 12]), ('password', ['', '\n', '\x00', '\x7f', '\ud800', 'a'*8193, None]), ('origin', ['http://example.com', 'https://user:pass@example.com', 'https://example.com#', 'https://example.com?', 'https://example.com:', 'https://example.com:0', 'https://example.com:65536', 'https://example.com/path', 'https://[::1]junk', 'https://good.-bad.example', 'https://good.bad-.example', 'https://example.com\\@evil.invalid', 'https://example.com/\ud800'])]:
            for value in values:
                with self.subTest(key=key, value=repr(value)), self.assertRaises(BackupError):
                    probe_authorization(dict(creds, **{key: value}), 'https://example.com')
        self.assertEqual(probe_origin('https://EXAMPLE.com:443/base'), 'https://example.com')
        self.assertEqual(probe_origin('https://[0:0:0:0:0:0:0:1]:443/'), 'https://[::1]')
        self.assertNotIn('CANARY', json.dumps(redact(self.envelope('https://example.com'))))

    @unittest.skipUnless(shutil.which('php'), 'PHP needed for independent parser parity')
    def test_php_python_origin_validation_parity(self):
        validator = pathlib.Path(__file__).resolve().parents[1] / 'extension/Backups/ProbeCredentials.php'
        program = 'require $argv[1]; try {echo Paymenter\\Extensions\\Others\\Backups\\ProbeCredentials::origin(json_decode(stream_get_contents(STDIN),true),true);}catch(Throwable $e){echo "REFUSED";}'
        for url in ['https://EXAMPLE.com:443/', 'https://example.com:8443', 'https://[0:0:0:0:0:0:0:1]:443/', 'https://example.com#', 'https://example.com?', 'https://example.com:', 'https://[::1]junk', 'https://example.com/path', 'https://good.-bad.example', 'https://example.com\\@evil.invalid', 'http://example.com', 'https://example.com:0', 'https://[::ffff:127.0.0.1]', 'https://[::ffff:7f00:1]']:
            with self.subTest(url=url):
                try:
                    expected = probe_origin(url, origin_only=True)
                except BackupError:
                    expected = 'REFUSED'
                result = subprocess.run(['php', '-r', program, str(validator)], input=json.dumps(url).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                self.assertEqual(result.stdout.decode(), expected, result.stderr.decode())
