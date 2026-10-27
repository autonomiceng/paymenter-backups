"""Isolated host filesystem for the configured, root-owned scheduler path."""
import contextlib
import os
import pathlib
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


@contextlib.contextmanager
def guarded_host(path='/opt/paymenter/scheduler.php'):
    with tempfile.TemporaryDirectory(prefix='backups-guarded-host-') as temporary:
        host = pathlib.Path(temporary)
        script = host / path.lstrip('/')
        script.parent.mkdir(parents=True)
        script.write_text('<?php // fixture scheduler\n')
        script.chmod(0o644)
        fixture = SimpleNamespace(script=script, owner_uid=0)
        target = pathlib.Path(path)
        resolve, lstat = pathlib.Path.resolve, pathlib.Path.lstat

        def host_resolve(path, *arguments, **keywords):
            if path == target:
                canonical = resolve(script, *arguments, **keywords)
                return pathlib.Path('/') / canonical.relative_to(host)
            return resolve(path, *arguments, **keywords)

        def host_lstat(path, *arguments, **keywords):
            if path == target:
                info = list(lstat(script, *arguments, **keywords))
                info[4] = fixture.owner_uid
                return os.stat_result(info)
            return lstat(path, *arguments, **keywords)

        with patch.dict(os.environ, {'PAYMENTER_BACKUPS_GUARDED_SCHEDULER': str(target)}), \
                patch.object(pathlib.Path, 'resolve', host_resolve), patch.object(pathlib.Path, 'lstat', host_lstat):
            yield fixture
