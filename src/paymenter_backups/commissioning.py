"""Installation readiness only; no capture, delivery, writer locks or host changes."""
import os
import pathlib
import stat
import tempfile
import time

from .common import BackupError, TRANSIENT_ERRORS, private_file, read_json


def chosen_schedules(original, proposed, changes):
    """Gate explicit schedule activation/edits, while leaving setup repairs and Off usable."""
    if proposed.get('settings_version', 1) == 1:
        return [('full' if proposed['mode'] == 'full-quiesced' else 'database')] if proposed['enabled'] and (
            not original.get('enabled') or any(key.startswith('schedule_') or key in ('enabled', 'mode') for key in changes)) else []
    result = []
    for kind in ('full', 'database'):
        fields = {kind + suffix for suffix in ('_schedule', '_time', '_weekdays')}
        if kind == 'database':
            fields.update(('database_every_hours', 'database_minute'))
        if proposed[kind + '_schedule'] != 'off' and (original.get(kind + '_schedule', 'off') == 'off'
                or fields.intersection(changes) or 'schedule_timezone' in changes):
            result.append(kind)
    return result


def recent(path, field, now, maximum, installation_uuid=None):
    try:
        private_file(path)
        value = read_json(path)
        if not isinstance(value, dict):
            return False
        stamp = value.get(field)
        if installation_uuid is not None and (value.get('schema_version') != 1
                or value.get('installation_uuid') != installation_uuid
                or not (isinstance(value.get('active'), dict) or value.get('active') == [])
                or 'observer' in value['active']):
            return False
        return type(stamp) in (int, float) and 0 <= now - stamp <= maximum
    except (OSError, BackupError, TypeError, ValueError):
        return False


def local_storage(state, envelope, prepare=False):
    """Probe only our own temporary file, never change permissions on existing storage."""
    from .engine import destination, space
    if not state.initialized:
        raise BackupError('installation-not-initialized')
    if prepare:
        path = destination(state, envelope)
    else:
        value = envelope['settings'].get('local_path')
        path = pathlib.Path(value) if value else (state.root / 'backups'
            if envelope['settings'].get('settings_version', 1) == 2 else state.default_destination())
        if path.is_symlink() or path.resolve() == state.app or state.app in path.resolve().parents:
            raise BackupError('destination-unsafe')
        state.guard_location(path)
        if not path.exists():
            raise BackupError('local-storage-not-ready')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BackupError('destination-unsafe')
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise BackupError('local-storage-not-ready')
    space(state, envelope['settings'])
    if prepare:
        # NamedTemporaryFile deletes only its own exclusive file, even on failure.
        with tempfile.NamedTemporaryFile(prefix='.readiness-', dir=path) as probe:
            probe.write(b'backups readiness\n')
            probe.flush()
            os.fsync(probe.fileno())
            probe.seek(0)
            if probe.read() != b'backups readiness\n':
                raise BackupError('local-storage-not-ready')


def check(state, envelope, prepare=False, inventory=None, now=None, mode_readiness=None):
    from . import discovery, tools
    from .engine import plan
    from .presentation import destination_test_binding, destination_test_current
    from .requests import list_requests
    now = time.time() if now is None else now
    settings = envelope['settings']
    state.apply_policy(settings)
    reports = envelope.get('tool_checks')
    if not isinstance(reports, dict):
        reports = tools.checks(envelope, capabilities=True)
    envelope = dict(envelope, tool_checks=reports)
    checks = {}

    def item(name, code=None, pending=False):
        checks[name] = {'state': 'pending' if pending else 'missing' if code else 'ready', 'code': code}

    failed = next((report for name, report in reports.items() if name != 'gpg' and not report['ok']), None)
    item('tools', (failed.get('code') or 'tool-unavailable') if failed else None)
    item('private_state', None if state.initialized else 'installation-not-initialized')
    if inventory is None:
        inventory = discovery.discover(state, envelope, now=now) if prepare else discovery.cached(state, now)
    scheduler_code = None
    if inventory['state'] != 'ok':
        scheduler_code = inventory.get('error') or ('job-discovery-stale' if inventory['state'] == 'stale' else 'job-discovery-unknown')
    elif not any(job['kind'] == 'scheduler' and job['coordination'] != 'unsupported' for job in inventory['items']):
        scheduler_code = 'scheduler-missing'
    elif not recent(state.root / 'scheduler.json', 'last_tick_at', now, 300):
        scheduler_code = 'scheduler-not-running'
    item('scheduler', scheduler_code)
    observer_path = state.root / 'incidents.json'
    observer_code = None if recent(observer_path, 'checked_at', now, 900, state.uuid) else (
        'observer-stale' if observer_path.exists() else 'observer-not-run')
    item('observer', observer_code)
    try:
        local_storage(state, envelope, prepare)
        item('local_storage')
    except (OSError, BackupError) as failure:
        item('local_storage', failure.code if isinstance(failure, BackupError) else 'local-storage-not-ready')
    offsite_code = None
    if settings.get('upload_s3', settings['destination'] == 's3'):
        from . import encryption
        offsite_code = encryption.readiness(envelope)
        if not offsite_code and not ((envelope.get('credentials') or {}).get('access_key') and (envelope.get('credentials') or {}).get('secret_key')):
            offsite_code = 's3-credentials-required'
        binding = destination_test_binding(state, envelope)
        if not offsite_code and not any(destination_test_current(state, envelope, request, request.get('result') or {}, binding)
                for request in list_requests(state) if request.get('action') == 'destination-test'):
            offsite_code = 'storage-untested'
        item('offsite', offsite_code, pending=bool(offsite_code))
    else:
        item('offsite', pending=True)
    shared = [value['code'] for value in checks.values() if value['code']]
    schedules = {}
    for kind, mode in (('full', 'full-quiesced'), ('database', 'database-online')):
        try:
            proposed = dict(envelope, settings=dict(settings, mode=mode))
            if kind == 'database' and state.initialized:
                from .maintenance import database_coordination_paths
                database_coordination_paths(state, proposed, inventory)
            items = (mode_readiness[mode]['items'] if mode_readiness is not None else
                     plan(state, proposed, inventory, probe_locks=False)['readiness_items'])
            codes = [entry['code'] for entry in items if entry['code'] not in TRANSIENT_ERRORS]
        except BackupError as failure:
            codes = [failure.code]
        blockers = list(dict.fromkeys(shared + codes))
        schedules[kind] = {'ready': not blockers, 'blockers': blockers}
    return {'schema_version': 1, 'checks': checks, 'schedules': schedules}


def require_ready(state, envelope, kinds):
    result = check(state, envelope, prepare=True)
    for kind in kinds:
        blockers = result['schedules'][kind]['blockers']
        if blockers:
            raise BackupError(blockers[0], detail={'schedule_kind': kind})
