"""Help keeps known launcher precedence and rejects unsafe queue lock paths."""
import pathlib
import tempfile
import unittest

from paymenter_backups import discovery
from paymenter_backups.common import State
from guarded_scheduler_fixture import guarded_host


class HelpSuggestions(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='help-suggestions-')
        self.addCleanup(scratch.cleanup)
        self.base = pathlib.Path(scratch.name)
        app = self.base / 'app'
        app.mkdir()
        self.state = State(app, self.base / 'private')
        self.envelope = {'tools': {'php': '/usr/bin/php'}}

    def queue_item(self, lock):
        """Parse a real bounded crontab worker without authorizing its lock."""
        line = f'* * * * * /usr/bin/flock -n {lock} /usr/bin/php {self.state.app}/artisan queue:work --stop-when-empty --max-time=50'
        return discovery.record(line, self.state.app, 'crontab', 'fixture')

    def test_unsafe_queue_locks_keep_the_private_fallback(self):
        safe = self.base / 'safe.lock'
        safe.touch(mode=0o600)
        linked = self.base / 'linked.lock'
        linked.symlink_to(safe)
        public = self.base / 'public.lock'
        public.touch(mode=0o644)
        public.chmod(0o644)
        fallback = discovery.suggestions(self.state, self.envelope)['queue']
        for lock in (self.base / 'missing.lock', linked, public):
            with self.subTest(lock=lock.name):
                item = self.queue_item(lock)
                self.assertEqual(item['coordination'], 'coordinated')
                suggested = discovery.suggestions(self.state, self.envelope, {'items': [item]})['queue']
                self.assertEqual(suggested, fallback)

    def test_first_verified_queue_entry_wins_in_either_inventory_order(self):
        locks = [self.base / 'first.lock', self.base / 'second.lock']
        for lock in locks:
            lock.touch(mode=0o600)
        invalid = self.queue_item(self.base / 'missing.lock')
        for ordered in (locks, list(reversed(locks))):
            with self.subTest(first=ordered[0].name):
                items = [invalid, *(self.queue_item(lock) for lock in ordered), invalid]
                suggested = discovery.suggestions(self.state, self.envelope, {'items': items})['queue']
                self.assertEqual(discovery.parse(suggested, self.state.app)['lock'], str(ordered[0]))

    def test_first_guarded_scheduler_entry_wins_without_hiding_queue(self):
        self.state.app = pathlib.Path('/app')
        locks = [self.base / 'first.lock', self.base / 'second.lock']
        for lock in locks:
            lock.touch(mode=0o600)
        with guarded_host():
            items = [discovery.record(f'* * * * * /usr/bin/flock -n {lock} /usr/local/bin/php /opt/paymenter/scheduler.php',
                                      self.state.app, 'crontab', 'fixture') for lock in locks]
            items.append(self.queue_item(locks[1]))
            suggested = discovery.suggestions(self.state, self.envelope, {'items': items})
            self.assertEqual(discovery.parse(suggested['scheduler'], self.state.app)['lock'], str(locks[0]))
            self.assertEqual(discovery.parse(suggested['queue'], self.state.app)['lock'], str(locks[1]))
