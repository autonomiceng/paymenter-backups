"""Environment boundary for disposable native fixtures, never live apps."""

# These are controlled defaults, not copies of the invoking shell environment.
# In particular Dotenv must never see inherited APP_*/DB_* configuration.
DEFAULT_ENVIRONMENT = {
    'PATH': '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
    'LANG': 'C.UTF-8',
    'LC_ALL': 'C.UTF-8',
    'TZ': 'UTC',
}


def child_environment(overrides=None):
    result = dict(DEFAULT_ENVIRONMENT)
    if overrides:
        unknown = set(overrides) - set(DEFAULT_ENVIRONMENT)
        if unknown:
            raise ValueError('fixture child environment accepts only PATH/LANG/LC_ALL/TZ')
        result.update(overrides)
    return result
