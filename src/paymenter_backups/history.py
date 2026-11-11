"""Versioned cold history; replay keys remain outside expiring payloads."""
import calendar
import datetime
import math
import os
import re
import stat
import time

from .common import BackupError, atomic_json, digest, private_file, read_json

TERMINAL = frozenset({'complete', 'satisfied', 'cancelled', 'expired', 'skipped'})
OPEN = frozenset({'pending', 'deferred', 'launching', 'running'})
COLD_NAME = re.compile(r'([0-9]{20})-([A-Za-z0-9_-]{1,100})\.json')
INTENTS = ('delivery_intent', 'retrieve_intent', 'local_delete_intent',
           'remote_delete_intent', 'retention_delete_intent', 'staging_removal_intent', 'multipart')


def directory(path, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BackupError('private-state-permissions')
    return True


def layout(state, create=False):
    root = state.root / 'history'
    for path in (root, root / 'v1'):
        if not directory(path, create):
            return None
    root /= 'v1'
    for family in ('requests', 'runs', 'request-keys', 'run-keys'):
        if not directory(root / family, create):
            return None
    return root


def owned(state, path, family, identifier):
    private_file(path)
    item = read_json(path)
    key = 'request_id' if family == 'requests' else 'run_id'
    if not isinstance(item, dict) or item.get(key) != identifier or item.get('installation_uuid') != state.uuid:
        raise BackupError('history-identity-mismatch')
    return item


def replay(state, request_id):
    state.run_path(request_id)
    root = layout(state)
    if root is None:
        return None
    path = root / 'request-keys' / (request_id + '.json')
    if not path.exists() and not path.is_symlink():
        return None
    return owned(state, path, 'requests', request_id)


def cutoff(now, months):
    current = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    year, month = divmod(current.year * 12 + current.month - 1 - months, 12)
    return current.replace(year=year, month=month + 1,
                           day=min(current.day, calendar.monthrange(year, month + 1)[1])).timestamp()


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def recovered(item):
    receipt = item.get('recovery_acknowledged')
    return (item.get('service_recovered') is True or isinstance(receipt, dict)
            and timestamp(receipt.get('at')) and receipt.get('owner_uid') == os.getuid())


def unresolved(item):
    intent = item.get('delete_intent')
    return (not recovered(item) or any(key in item for key in INTENTS)
            or intent is not None and (not isinstance(intent, dict) or intent.get('completed') is not True)
            or bool(item.get('cleanup_error')))


def request_time(state, root, item, journals):
    if item.get('status') in OPEN:
        return None, None
    if item.get('status') not in TERMINAL or item.get('action') not in (
            'run', 'verify', 'deliver', 'retrieve', 'retention', 'destination-test', 'delete'):
        return None, 'request-outcome-uncertain'
    arguments, result = item.get('arguments') or {}, item.get('result') or {}
    if not isinstance(arguments, dict) or not isinstance(result, dict):
        return None, 'request-shape-unknown'
    references = {value for value in (arguments.get('run_id'), result.get('run_id'), item.get('covered_by')) if isinstance(value, str)}
    related = [row for row in journals if row.get('request_id') == item['request_id'] or row.get('run_id') in references]
    if any(unresolved(row) for row in related):
        return None, 'request-run-unresolved'
    known = {row['run_id'] for row in related}
    for run_id in references - known:
        state.run_path(run_id)
        path = root / 'run-keys' / (run_id + '.json')
        if path.exists() or path.is_symlink():
            owned(state, path, 'runs', run_id)
            known.add(run_id)
    if (item.get('action') == 'run' and item.get('status') == 'complete' and not (related or references & known)
            or references - known):
        return None, 'request-run-unknown'
    # Legacy records without a proven terminal timestamp remain active.
    values = [item[key] for key in ('finished_at', 'updated_at') if key in item]
    if not values or not all(timestamp(value) for value in values):
        return None, 'request-time-unknown'
    return max(values), None


def run_time(item, requests):
    if unresolved(item):
        return None, 'run-work-unresolved'
    if item.get('retained_deleted') is not True:
        return None, None
    if item.get('local_complete') is not False or item.get('remote_delivered') is not False:
        return None, 'run-copy-unknown'
    if any(row.get('status') not in TERMINAL and (row.get('arguments', {}).get('run_id') == item['run_id']
            or row.get('request_id') == item.get('request_id')) for row in requests):
        return None, 'run-request-open'
    deletion = item.get('deletion') or {}
    if not isinstance(deletion, dict):
        return None, 'run-time-unknown'
    values = [deletion.get('at'), item.get('updated_at')]
    if not all(timestamp(value) for value in values):
        return None, 'run-time-unknown'
    return max(values), None


def sync(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def move(source, target):
    # Both directories are on the private-state filesystem. Never copy/unlink.
    os.rename(source, target)
    sync(target.parent)
    sync(source.parent)


def archive(state, root, family, source, item, at):
    identifier = item['request_id' if family == 'requests' else 'run_id']
    name = f'{int(at * 1000000):020d}-{identifier}.json'
    key_family = 'request-keys' if family == 'requests' else 'run-keys'
    key_path = root / key_family / (identifier + '.json')
    checksum = digest(source)
    receipt = {key: item[key] for key in ('request_id', 'run_id', 'action', 'status', 'slot', 'created_at') if key in item}
    receipt.update(schema_version=1, installation_uuid=state.uuid, terminal_at=int(at * 1000000) / 1000000,
                   cold_name=name, sha256=checksum)
    if key_path.exists() or key_path.is_symlink():
        previous = owned(state, key_path, family, identifier)
        if previous != receipt:
            raise BackupError('history-key-conflict')
    else:
        # A crash can leave both a replay receipt and the active payload. The
        # active payload still wins reads; retry checks the exact same receipt.
        atomic_json(key_path, receipt)
    target = root / family / name
    if target.exists() or target.is_symlink():
        owned(state, target, family, identifier)
        if digest(target) != checksum:
            raise BackupError('history-payload-conflict')
        raise BackupError('history-active-conflict')
    move(source, target)
    if family == 'runs':
        source.parent.rmdir()
        sync(source.parent.parent)


def expire(state, root, before, report):
    for family, key_family in (('requests', 'request-keys'), ('runs', 'run-keys')):
        for path in sorted((root / family).iterdir()):
            match = COLD_NAME.fullmatch(path.name)
            if not match:
                report['damaged'].append({'family': family, 'id': None, 'code': 'history-name-invalid'})
                continue
            at, identifier = int(match[1]) / 1000000, match[2]
            if at >= before:
                continue
            try:
                owned(state, path, family, identifier)
                receipt = owned(state, root / key_family / (identifier + '.json'), family, identifier)
                if (receipt.get('cold_name') != path.name or receipt.get('terminal_at') != at
                        or receipt.get('sha256') != digest(path)):
                    raise BackupError('history-payload-conflict')
                path.unlink()
                sync(path.parent)
                report['deleted'] += 1
            except (BackupError, OSError) as error:
                report['damaged'].append({'family': family, 'id': identifier,
                                          'code': error.code if isinstance(error, BackupError) else 'history-io-failed'})


def maintain(state, now=None):
    """Take run then requests, or defer immediately when either is occupied."""
    if not state.initialized:
        return None
    now = time.time() if now is None else now
    try:
        with state.lock(), state.lock('requests.lock'):
            root = layout(state, create=True)
            report = {'schema_version': 1, 'at': now, 'archived': 0, 'deleted': 0,
                      'unclassified': [], 'damaged': []}
            journals, requests = [], []
            for family, paths, rows in (
                    ('runs', sorted((state.root / 'runs').glob('*/journal.json')), journals),
                    ('requests', sorted((state.root / 'requests').glob('*.json')), requests)):
                for path in paths:
                    identifier = path.parent.name if family == 'runs' else path.stem
                    try:
                        state.run_path(identifier)
                        directory(path.parent)
                        rows.append(owned(state, path, family, identifier))
                    except (BackupError, OSError) as error:
                        report['unclassified'].append({'family': family, 'id': identifier,
                            'code': error.code if isinstance(error, BackupError) else 'history-io-failed'})
            # An unreadable active request might reference any run. Keep all
            # runs when that relationship cannot be proved.
            unknown_requests = any(row['family'] == 'requests' for row in report['unclassified'])
            for family, rows in (('requests', requests), ('runs', journals)):
                for item in rows:
                    identifier = item['request_id' if family == 'requests' else 'run_id']
                    try:
                        at, reason = request_time(state, root, item, journals) if family == 'requests' else run_time(item, requests)
                    except (BackupError, TypeError, AttributeError, ValueError):
                        at, reason = None, 'history-classification-unknown'
                    if family == 'runs' and unknown_requests:
                        at, reason = None, 'run-requests-unknown'
                    if reason:
                        report['unclassified'].append({'family': family, 'id': identifier, 'code': reason})
                    if at is None or at >= cutoff(now, 13):
                        continue
                    source = (state.root / 'requests' / (identifier + '.json') if family == 'requests'
                              else state.run_path(identifier) / 'journal.json')
                    try:
                        if family == 'runs' and {path.name for path in source.parent.iterdir()} != {'journal.json'}:
                            raise BackupError('run-work-remains')
                        archive(state, root, family, source, item, at)
                        report['archived'] += 1
                    except (BackupError, OSError) as error:
                        report['unclassified'].append({'family': family, 'id': identifier,
                            'code': error.code if isinstance(error, BackupError) else 'history-io-failed'})
            expire(state, root, cutoff(now, 36), report)
            atomic_json(state.root / 'history-maintenance.json', report)
            return report
    except BackupError as error:
        if error.code != 'busy':
            raise
        return None
