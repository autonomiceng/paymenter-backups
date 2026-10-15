"""Agent socket discovery and the optional non-AEAD GnuPG status field."""
import io
import pathlib
import tempfile
import time
import unittest

from paymenter_backups import encryption
from paymenter_backups.common import BackupError


class GnuPGCompatibility(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)

    def test_owned_agent_uses_reported_socket_instead_of_homedir_default(self):
        agent = self.root / 'gpg-agent'
        agent.write_text("#!/usr/bin/python3\nimport pathlib,socket,sys,time\np=pathlib.Path(sys.argv[sys.argv.index('--homedir')+1])/'agent.socket'\ns=socket.socket(socket.AF_UNIX);s.bind(str(p));time.sleep(30)\n")
        agent.chmod(0o700)
        config = self.root / 'gpgconf'
        config.write_text("#!/usr/bin/python3\nimport pathlib,sys\np=pathlib.Path(sys.argv[sys.argv.index('--homedir')+1])/'agent.socket'\nif '--list-dirs' in sys.argv: print(p)\n")
        config.chmod(0o700)
        with encryption.owned_home(str(agent), time.monotonic() + 1) as home:
            self.assertTrue((pathlib.Path(home) / 'agent.socket').is_socket())
            self.assertFalse((pathlib.Path(home) / 'S.gpg-agent').exists())

    def test_encrypt_status_allows_only_absent_or_zero_aead_field(self):
        (self.root / 'gpg-agent').symlink_to('/usr/bin/gpg-agent')
        (self.root / 'gpgconf').symlink_to('/usr/bin/gpgconf')
        binary = self.root / 'gpg'
        envelope = {'settings': {'capture_timeout': 5}, 'tools': {'gpg': str(binary)}}
        for profile, accepted in [('2 9', True), ('2 9 0', True), ('2 9 2', False), ('0 9 0', False), ('2 9 0 0', False)]:
            with self.subTest(profile=profile):
                binary.write_text("#!/usr/bin/python3\nimport os,sys\na=sys.argv;fd=int(a[a.index('--status-fd')+1]);sys.stdin.buffer.read()\nos.write(fd,b'[GNUPG:] BEGIN_ENCRYPTION " + profile + "\\n[GNUPG:] END_ENCRYPTION\\n');sys.stdout.buffer.write(b'ciphertext')\n")
                binary.chmod(0o700)
                if accepted:
                    self.assertEqual(b''.join(encryption.transform(envelope, io.BytesIO(b'fixture'), 'a' * 64)), b'ciphertext')
                else:
                    with self.assertRaises(BackupError):
                        b''.join(encryption.transform(envelope, io.BytesIO(b'fixture'), 'a' * 64))

    def test_readiness_names_missing_config_helper_before_encryption(self):
        (self.root / 'gpg').symlink_to('/usr/bin/gpg')
        agent = self.root / 'gpg-agent'
        agent.write_text('#!/bin/sh\nexit 0\n')
        agent.chmod(0o700)
        envelope = {'settings': {'s3_encryption': True}, 'tools': {'gpg': str(self.root / 'gpg')}}
        self.assertEqual(encryption.readiness(envelope), 'archive-encryption-tool-unusable')
