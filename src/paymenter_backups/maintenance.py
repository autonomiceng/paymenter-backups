import base64
import contextlib
import ipaddress
import http.client
import re
import signal
import threading
import urllib.parse
import fcntl
import json
import os
import pathlib
import stat
import shlex
import ssl
import time
import urllib.error
import urllib.request
import uuid

from .common import BackupError, identity, read_json
from .config import billing_fingerprint
from .process import app_workers, group_members, run


PROBE_TIMEOUT = 10
PROBE_BODY_LIMIT = 65536

MARKERS = ("storage/framework/down", "storage/framework/maintenance.php")


def strict_payload(app):
    try:
        value = read_json(app / MARKERS[0])
        identity(app / MARKERS[1])
    except (OSError, BackupError):
        raise BackupError("maintenance-unsafe") from None
    try:
        template_size = len(value.get("template", "").encode("utf-8"))
    except (AttributeError, UnicodeError):
        raise BackupError("maintenance-unsafe") from None
    if value.get("status") != 503 or value.get("except") or value.get("secret") or value.get("redirect") or not isinstance(value.get("template"), str) or not value["template"] or template_size > PROBE_BODY_LIMIT:
        raise BackupError("maintenance-unsafe")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def probe_origin(url, origin_only=False):
    """Canonical HTTPS origin; reject ambiguous authority and URL credentials."""
    try:
        if not isinstance(url, str) or not url or any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
            raise ValueError()
        url.encode("utf-8")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or "?" in url or "#" in url or not re.fullmatch(r"(?:\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::[0-9]{1,5})?", parsed.netloc):
            raise ValueError()
        if origin_only and parsed.path not in ("", "/"):
            raise ValueError()
        host = parsed.hostname.lower()
        if ":" in host:
            address = ipaddress.IPv6Address(host)
            if address.ipv4_mapped is not None:
                raise ValueError()
            host = "[" + str(address) + "]"
        elif len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label, re.ASCII) for label in host.split(".")):
            raise ValueError()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        return "https://" + host + (":" + str(port) if port not in (None, 443) else "")
    except (ValueError, TypeError, UnicodeError):
        raise BackupError("maintenance-probe-credentials-invalid") from None


def probe_authorization(credentials, app_url):
    if not isinstance(credentials, dict) or set(credentials) != {"origin", "username", "password"}:
        raise BackupError("maintenance-probe-credentials-invalid")
    for key in ("username", "password"):
        value = credentials[key]
        try:
            valid = isinstance(value, str) and value and len(value.encode("utf-8")) <= 8192 and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        except UnicodeError:
            valid = False
        if not valid:
            raise BackupError("maintenance-probe-credentials-invalid")
    if ":" in credentials["username"]:
        raise BackupError("maintenance-probe-credentials-invalid")
    if probe_origin(credentials["origin"], origin_only=True) != probe_origin(app_url):
        raise BackupError("maintenance-probe-origin-mismatch")
    value = (credentials["username"] + ":" + credentials["password"]).encode("utf-8")
    return "Basic " + base64.b64encode(value).decode("ascii")


@contextlib.contextmanager
def probe_deadline(seconds):
    """Bound all header/body waits, including peers that continuously drip data."""
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        raise BackupError("maintenance-probe-failed")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    # Do not replace another caller's active alarm or its handler. Refuse proof
    # safely while preserving that timer; ordinary capture owns no SIGALRM.
    if previous_timer[0] or previous_timer[1]:
        raise BackupError("maintenance-probe-failed")
    def expired(signum, frame):
        raise BackupError("maintenance-probe-failed")
    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def probe_read(response, limit=PROBE_BODY_LIMIT):
    """Read at most limit bytes within the GET's original total deadline."""
    if type(limit) is not int or not 0 <= limit <= PROBE_BODY_LIMIT + 1:
        raise BackupError("maintenance-probe-failed")
    try:
        with probe_deadline(response._backup_probe_deadline - time.monotonic()):
            return response.read(limit)
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        raise BackupError("maintenance-probe-failed") from None


def probe_request(envelope):
    """One nonce GET using the capture proof's bound auth/TLS/redirect policy.

    Returns the response for strict proof or private operator diagnostics. This
    transport cannot claim maintenance success; probe() owns that decision.
    """
    app_url = envelope["framework"].get("app_url", "")
    headers = {"Cache-Control": "no-cache"}
    credentials = envelope.get("maintenance_probe_credentials")
    if credentials:
        headers["Authorization"] = probe_authorization(credentials, app_url)
    try:
        # Anonymous HTTP supports ordinary native/local installations. Reuse
        # strict URL validation while credentials require HTTPS unchanged.
        if not isinstance(app_url, str) or not app_url.startswith(("https://", "http://")):
            raise ValueError()
        origin = probe_origin("https://" + app_url.split("://", 1)[1])
        if app_url.startswith("http://"):
            origin = "http://" + origin.removeprefix("https://")
            # HTTPS canonicalization omits443; anonymous HTTP must retain an
            # explicit443 because its default port is80.
            if urllib.parse.urlsplit(app_url).port == 443:
                origin += ":443"
        path = urllib.parse.urlsplit(app_url).path
    except (BackupError, ValueError, TypeError):
        raise BackupError("maintenance-probe-failed") from None
    url = origin + path.rstrip("/") + "/__backup_probe_" + uuid.uuid4().hex
    try:
        from .storage import clean_environment
        clean_environment()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        trust = ssl.get_default_verify_paths()
        # Compiled system trust paths exclude caller CA environment overrides.
        context.load_verify_locations(cafile=trust.openssl_cafile if os.path.isfile(trust.openssl_cafile) else None,
                                      capath=trust.openssl_capath if os.path.isdir(trust.openssl_capath) else None)
        context.keylog_filename = None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect)
        deadline = time.monotonic() + PROBE_TIMEOUT
        with probe_deadline(PROBE_TIMEOUT):
            try:
                response = opener.open(urllib.request.Request(url, headers=headers), timeout=PROBE_TIMEOUT)
            except urllib.error.HTTPError as error:
                response = error
        response._backup_probe_deadline = deadline
        return response
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError, TypeError, UnicodeError):
        raise BackupError("maintenance-probe-failed") from None


def probe(envelope):
    expected = strict_payload(pathlib.Path(envelope["app"]))["template"].encode()
    try:
        with probe_request(envelope) as response:
            if response.status != 503 or probe_read(response, len(expected) + 1) != expected:
                raise BackupError("maintenance-probe-failed")
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        raise BackupError("maintenance-probe-failed") from None


def billing_admission(state, envelope, now=None):
    now = time.time() if now is None else now
    settings = envelope["settings"]
    try:
        guard = read_json(state.root / "billing-guard.json")
        if not envelope.get("billing", {}).get("daily_time") or guard["fingerprint"] != billing_fingerprint(envelope["billing"]):
            raise ValueError()
        age = now - guard["generated_at"]
        if not 0 <= age <= settings["billing_guard_max_age"] or not guard["instants"] or max(guard["instants"]) < now + 86400:
            raise ValueError()
        protected = [float(item) for item in guard["instants"]]
    except (KeyError, TypeError, ValueError, BackupError):
        raise BackupError("billing-guard-stale") from None
    bound = settings["lock_timeout"] + settings["drain_seconds"] + settings["capture_timeout"] + 30
    margin = settings["billing_margin_seconds"]
    for instant in protected:
        if now <= instant + 60 + margin and now + bound >= instant - margin:
            raise BackupError("billing-window-protected")
    return {"admitted_at": now, "hold_deadline": now + bound,
            "protected_instants": protected, "effective_timezone": guard.get("timezone"),
            "expression": guard.get("expression")}


@contextlib.contextmanager
def coordination(state, envelope):
    paths = envelope["settings"]["job_lock_paths"]
    descriptors = []
    try:
        if not paths:
            raise BackupError("job-locks-unverified")
        crontab = run([envelope["tools"].get("crontab", "/usr/bin/crontab"), "-l"], 5).decode()
        jobs = [line for line in crontab.splitlines() if line.strip() and not line.lstrip().startswith("#") and any(word in line for word in ("schedule:run", "queue:work"))]
        verified = set()
        for line in jobs:
            verified.add(cron_lock(line, state.app, paths))
        if not jobs or verified != set(paths):
            raise BackupError("job-locks-unverified")
        deadline = time.monotonic() + envelope["settings"]["lock_timeout"]
        for path in sorted(set(paths)):
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise BackupError("job-locks-unverified")
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BackupError("job-lock-busy")
                    time.sleep(0.05)
        if app_workers(state.app):
            raise BackupError("job-locks-unverified")
        yield
    except OSError:
        raise BackupError("job-locks-unverified") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def cron_lock(line, app, paths):
    """Support explicit path-based flock invocations; ambiguous shell refuses."""
    try:
        tokens = shlex.split(line, comments=True)
        tokens = tokens[1:] if tokens[0].startswith("@") else tokens[5:]
        flock_index = next(i for i, value in enumerate(tokens) if value in ("flock", "/usr/bin/flock", "/bin/flock"))
        if tokens[:flock_index] not in ([], ["cd", str(app), "&&"]):
            raise ValueError()
        index = flock_index + 1
        while tokens[index].startswith("-"):
            if tokens[index] in ("-n", "--nonblock", "-x", "--exclusive", "-E", "--conflict-exit-code", "-w", "--timeout"):
                if tokens[index] in ("-E", "--conflict-exit-code", "-w", "--timeout"):
                    index += 1
                index += 1
            else:
                raise ValueError()
        lock = tokens[index]
        if lock not in paths or "--force" in tokens:
            raise ValueError()
        command = tokens[index + 1:]
        if len(command) < 3 or pathlib.Path(command[0]).name not in ("php", "php8.3", "php8.4", "php8.5"):
            raise ValueError()
        if command[1] not in (str(app / "artisan"), "artisan") or command[2] not in ("schedule:run", "queue:work"):
            raise ValueError()
        if not any(word in command for word in ("schedule:run", "queue:work")):
            raise ValueError()
        absolute = str(app / "artisan") in command
        relative = "artisan" in command and "cd" in tokens[:index] and str(app) in tokens[:index]
        if not absolute and not relative:
            raise ValueError()
        if any(word in command for word in ("-c", "--command", "eval", ";", "&&", "||", "|", "&", ">", "<")) or any(any(symbol in word for symbol in (";", "|", "&", "`", "$", ">", "<")) for word in command):
            raise ValueError()
        return lock
    except (ValueError, StopIteration, IndexError):
        raise BackupError("job-locks-unverified") from None


def cleanup(state, journal):
    """Shared release/recovery path. Down always comes before bootstrap stub."""
    if any(group_members(group) for group in journal.get("groups", [])):
        raise BackupError("process-state-uncertain")
    owned = journal.get("owned_markers", {})
    # Validate *all* extant markers before removing any: replacements stay closed.
    for relative in MARKERS:
        path = state.app / relative
        if relative in owned and path.exists():
            try:
                if identity(path) != owned[relative]:
                    raise BackupError("maintenance-ownership-uncertain")
            except OSError:
                raise BackupError("maintenance-ownership-uncertain") from None
        elif relative not in owned and relative in journal.get("initial_markers", {}):
            if not path.exists() or identity(path) != journal["initial_markers"][relative]:
                raise BackupError("maintenance-ownership-uncertain")
        elif relative not in owned and path.exists():
            raise BackupError("maintenance-ownership-uncertain")
    for relative in MARKERS:
        if relative in owned:
            path = state.app / relative
            if path.exists():
                if identity(path) != owned[relative]:
                    raise BackupError("maintenance-ownership-uncertain")
                path.unlink()
                directory = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
    journal["service_recovered"] = True
    journal["hold_ended_at"] = time.time()
    state.save(journal)


def recover(state, execute=False, auto=False, run_id=None, acknowledge=False):
    results = []
    if not state.initialized:
        return results
    with state.lock(create=execute or auto or acknowledge):
        for journal in state.journals():
            if run_id and journal["run_id"] != run_id:
                continue
            if journal.get("service_recovered") or journal.get("recovery_acknowledged"):
                continue
            item = {"run_id": journal["run_id"], "phase": journal["phase"], "action": "inspect"}
            if acknowledge:
                if not run_id:
                    raise BackupError("run-id-required")
                journal["recovery_acknowledged"] = {"at": time.time(), "owner_uid": os.getuid()}
                state.save(journal)
                item["action"] = "acknowledged"
            elif execute or auto:
                try:
                    cleanup(state, journal)
                    item["action"] = "recovered"
                except BackupError as error:
                    journal["recovery_error"] = error.code
                    state.save(journal)
                    item["error"] = error.code
            results.append(item)
    if run_id and not any(item["run_id"] == run_id for item in results) and acknowledge:
        raise BackupError("unresolved-run-not-found")
    return results


def schedule_admission(state, envelope):
    """Validate future backup slots against observed native billing instants."""
    from .requests import due_slots
    settings = envelope["settings"]
    if settings["mode"] != "full-quiesced" or not settings["enabled"]:
        return
    now = time.time()
    try:
        billing_admission(state, envelope, now)
    except BackupError as error:
        if error.code != "billing-window-protected":
            raise
    guard = read_json(state.root / "billing-guard.json")
    horizon = min(max(guard["instants"]), now + 8 * 86400)
    # due_slots enumerates at most two days; use consecutive windows, preserving
    # the source event's UTC billing instants rather than synthesizing billing.
    cursor = now
    seen = set()
    while cursor < horizon:
        cursor = min(cursor + 86400, horizon)
        window = dict(settings, request_max_age=86400)
        for slot, instant in due_slots(window, cursor):
            if instant < now or slot in seen:
                continue
            seen.add(slot)
            bound = settings["lock_timeout"] + settings["drain_seconds"] + settings["capture_timeout"] + 30
            margin = settings["billing_margin_seconds"]
            if any(instant <= billing + 60 + margin and instant + bound >= billing - margin for billing in guard["instants"]):
                raise BackupError("schedule-overlaps-billing")
