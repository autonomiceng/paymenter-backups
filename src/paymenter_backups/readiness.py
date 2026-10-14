"""Automatic full-state checks shared by plan and capture."""
import re

STORE_RULES = (
    ('maintenance', 'maintenance_driver', 'APP_MAINTENANCE_DRIVER', 'file', ('file',)),
    ('cache', 'cache_driver', 'CACHE_STORE', 'file', ('file', 'database', 'array')),
    ('session', 'session_driver', 'SESSION_DRIVER', 'file', ('file', 'database')),
    ('queue', 'queue_driver', 'QUEUE_CONNECTION', 'database', ('database', 'sync', 'null')),
    ('filesystem', 'filesystem_driver', 'FILESYSTEM_DISK', 'local', ('local', 'public')),
)


def stores(envelope, legacy_policy=False):
    framework = envelope.get('framework', {})
    resolved = framework.get('stores', {})
    result = []
    for kind, key, env, suggest, allowed in STORE_RULES:
        data = resolved.get(kind, {})
        driver = framework.get(key) if legacy_policy else data.get('driver', framework.get(key))
        driver = driver if isinstance(driver, str) and re.fullmatch(r'[A-Za-z0-9_.\-]{1,64}', driver) else 'unknown'
        supported = driver in allowed if legacy_policy else data.get('supported')
        if type(supported) is not bool:
            # v1 envelopes predate resolved stores. Preserve their allowlist.
            supported = driver in allowed if envelope['settings'].get('settings_version', 1) == 1 or kind == 'maintenance' else None
        result.append({'kind': kind, 'driver': driver, 'env': env, 'supported': supported,
                       'suggest': None if supported else suggest})
    return result


def state_for(items):
    codes = {item['code'] for item in items}
    if 'recovery-required' in codes:
        return 'needs-recovery'
    if not codes:
        return 'ready'
    if codes <= {'busy', 'billing-guard-stale', 'billing-window-protected', 'job-lock-busy'}:
        return 'waiting'
    return 'needs-setup'
