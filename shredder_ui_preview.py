#!/usr/bin/env python3
"""
File Shredder - local UI + HTTP API.

Serves the Material 3 interface on http://127.0.0.1:<port>/ and exposes a JSON
API that mirrors the widget set of file_shredder.py.

    GET  /api/state                current queue, mode, trim flag, job status
    GET  /api/home_dir_contents    entries in the user's home directory
    GET  /api/list_dir?path=...    entries in any directory
    GET  /api/ssds                 SSD drive letters + filesystem info
    GET  /api/modes                shredding modes and their pass counts
    POST /api/add                  queue paths (files and/or folders)
    POST /api/remove               drop queue entries by index
    POST /api/clear                empty the queue
    POST /api/load                 read a .txt path list and queue the valid entries
    POST /api/save                 write the queue to a .txt file
    POST /api/open                 open selected entries with the OS handler
    POST /api/shred                permanently shred selected entries
    POST /api/cancel               abort a running shred job

Usage:
    python shredder_ui_preview.py
    python shredder_ui_preview.py --port 9000 --no-browser

Security posture
----------------
This process performs irreversible deletions, so it is deliberately hard to
abuse from the network:

  * Binds to 127.0.0.1 only. A non-loopback bind is refused unless
    --allow-remote is passed, which prints a loud warning.
  * Every mutating request must carry the per-process CSRF token in the
    X-Shredder-Token header. The token is minted at startup and inlined into
    the page, so a third-party site cannot read it.
  * The Host header must be a loopback name (DNS-rebinding defence) and, when
    present, Origin must match the serving origin (CSRF defence).
  * Request bodies are size-capped.
  * A protected-path denylist blocks system directories, drive roots, the home
    directory itself and this script.

Requires only the Python standard library.
"""

import argparse
import http.server
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

DEFAULT_PORT = 8777
DEFAULT_HOST = "127.0.0.1"
MAX_BODY_BYTES = 8 * 1024 * 1024
CHUNK_SIZE = 1 << 20  # 1 MiB
SESSION_TTL_SECONDS = 3600

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = ("index.html", "app.css", "app.js")

SHRED_MODES = {
    "basic": {
        "label": "Basic Mode",
        "passes": 1,
        "pattern": "null",
        "hint": "Single pass, overwrite with null bytes",
    },
    "normal1": {
        "label": "Normal Mode (1 Pass)",
        "passes": 1,
        "pattern": "random",
        "hint": "One pass of random data",
    },
    "normal3": {
        "label": "Normal Mode (3 Pass)",
        "passes": 3,
        "pattern": "random",
        "hint": "Three passes of random data (DoD 5220.22-M like)",
    },
    "expert": {
        "label": "Expert Mode (10 Pass)",
        "passes": 10,
        "pattern": "random",
        "hint": "Ten passes of random data for the highest assurance",
    },
}


class ApiError(Exception):
    """Raised inside handlers to produce a structured JSON error response."""

    def __init__(self, message, status=400, detail=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #

def normalize_path(raw):
    """Expand, absolutise and normalise a user-supplied path."""
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError("path must be a non-empty string")
    expanded = os.path.expanduser(os.path.expandvars(raw.strip()))
    return os.path.normpath(os.path.abspath(expanded))


def protected_prefixes():
    """Directories that may never be shredded or opened."""
    here = os.path.dirname(os.path.abspath(__file__))
    prefixes = {here}
    if sys.executable:
        prefixes.add(os.path.abspath(sys.executable))
    if platform.system() == "Windows":
        system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\/")
        # set.update() takes iterables: a bare string would be split into
        # single characters, so each entry must be a one-element iterable.
        prefixes.update([
            os.environ.get("SystemRoot", "C:\\Windows"),
            os.environ.get("ProgramFiles", "C:\\Program Files"),
            os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
            os.environ.get("ProgramData", "C:\\ProgramData"),
            system_drive + "\\PerfLogs",
            system_drive + "\\System Volume Information",
            system_drive + "\\$Recycle.Bin",
            system_drive + "\\Recovery",
            system_drive + "\\Windows\\Boot",
        ])
    else:
        prefixes.update(["/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys"])

    resolved = set()
    for prefix in prefixes:
        if not prefix:
            continue
        try:
            resolved.add(os.path.normcase(os.path.abspath(prefix)))
        except (OSError, ValueError):
            continue
    return resolved


PROTECTED = protected_prefixes()


def is_protected(path):
    """Reason this path may not be shredded, or None if it is allowed.

    This gate applies to shredding only. Browsing and queueing a protected
    path are harmless and stay permitted, so the caller can inspect and
    stage anything; only the destructive step is refused.
    """
    norm = os.path.normcase(os.path.abspath(path))

    drive, _ = os.path.splitdrive(norm)
    if drive and norm.rstrip("\\/") == drive.rstrip("\\/"):
        return "drive root"

    home = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
    if norm.rstrip("\\/") == home.rstrip("\\/"):
        return "home directory"

    for prefix in PROTECTED:
        if norm == prefix:
            return "protected: %s" % prefix
        if norm.startswith(prefix + os.sep):
            return "protected: %s" % prefix

    return None


def split_by_protection(paths):
    """Partition paths into (shreddable, refused). Refused carries the reason."""
    allowed, refused = [], []
    for path in paths:
        reason = is_protected(path)
        if reason:
            refused.append({"path": path, "reason": reason})
        else:
            allowed.append(path)
    return allowed, refused


def describe(path):
    """Metadata describing a queued path."""
    try:
        stat = os.stat(path)
        is_dir = os.path.isdir(path)
        size = None if is_dir else stat.st_size
        return {
            "path": path,
            "name": os.path.basename(path) or path,
            "is_dir": is_dir,
            "exists": True,
            "size": size,
            "mtime": stat.st_mtime,
        }
    except OSError as exc:
        return {
            "path": path,
            "name": os.path.basename(path) or path,
            "is_dir": False,
            "exists": False,
            "size": None,
            "mtime": None,
            "error": exc.strerror or str(exc),
        }


MAX_COUNTED_FILES = 2_000_000


def count_files(path):
    """File count for progress, capped so setup cannot walk a whole volume."""
    if os.path.isfile(path):
        return 1
    total = 0
    for _root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        total += len(files)
        if total >= MAX_COUNTED_FILES:
            break
    return total


def list_directory(path):
    """Entries of a directory, directories first, hidden files included.

    Nothing here restricts access. The protected flag is advisory only: it
    tells the UI which entries the shredder would refuse, without preventing
    the user from navigating to them or staging them.
    """
    try:
        names = os.listdir(path)
    except PermissionError:
        raise ApiError("permission denied", 403)
    except OSError as exc:
        raise ApiError("cannot read directory: %s" % (exc.strerror or exc), 400)

    entries = []
    for name in names:
        full = os.path.join(path, name)
        is_dir = os.path.isdir(full)
        entry = {
            "name": name,
            "path": full,
            "is_dir": is_dir,
            "hidden": name.startswith("."),
            "shreddable": is_protected(full) is None,
        }
        try:
            entry["mtime"] = os.path.getmtime(full)
        except OSError:
            entry["mtime"] = None
        if is_dir:
            entry["size"] = None
        else:
            try:
                entry["size"] = os.path.getsize(full)
            except OSError:
                entry["size"] = None
        entries.append(entry)

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


# --------------------------------------------------------------------------- #
# Drive / SSD helpers
# --------------------------------------------------------------------------- #

def detect_ssd_drives():
    """Drive letters reporting a solid-state physical disk."""
    if platform.system() != "Windows":
        return {}

    script = (
        "Get-Volume | Where-Object { $_.DriveLetter } | ForEach-Object { "
        "  $l = $_.DriveLetter; "
        "  try { $pd = Get-Partition -DriveLetter $l | Get-Disk | Get-PhysicalDisk; "
        "    if ($pd.MediaType -eq 'SSD') { \"$l`t$($pd.MediaType)\" } } catch { } }"
    )
    result = {}
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return result

    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            match = re.fullmatch(r"([A-Za-z])\s*\t?(\S+)", line.strip())
            if match:
                result[match.group(1).upper()] = match.group(2)
    return result


def run_trim(letter):
    """Issue a TRIM/retrim to a drive. Returns (ok, message)."""
    script = (
        'try { Optimize-Volume -DriveLetter %s -ReTrim -ErrorAction Stop; "OK" } '
        'catch { "ERR: " + $_.Exception.Message; exit 1 }' % letter
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode == 0 and "OK" in proc.stdout:
            return True, "Optimize-Volume"
    except (OSError, subprocess.SubprocessError) as exc:
        detail = str(exc)
    else:
        detail = proc.stderr.strip() or proc.stdout.strip()

    try:
        proc = subprocess.run(["defrag.exe", "%s:" % letter, "/L"],
                              capture_output=True, text=True, timeout=900)
        if proc.returncode == 0:
            return True, "defrag /L"
        detail = proc.stderr.strip() or proc.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        detail = str(exc)

    return False, detail or "unknown error"


def drive_of(path):
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if len(drive) == 2 and drive.endswith(":"):
        return drive[0].upper()
    return None


# --------------------------------------------------------------------------- #
# Application state
# --------------------------------------------------------------------------- #

class State:
    """Queue, options and the currently running shred job."""

    def __init__(self):
        self.lock = threading.RLock()
        self.queue = []
        self.mode = "expert"
        self.trim = True
        self.job = None
        self.token = secrets.token_urlsafe(32)
        self.started = time.time()
        self.notes = []

    # -- queue ----------------------------------------------------------- #
    def add(self, paths):
        """Stage paths for shredding. Protected locations may be queued; the
        shred step is what refuses them."""
        added, skipped = [], []
        with self.lock:
            for raw in paths:
                try:
                    path = normalize_path(raw)
                except ApiError as exc:
                    skipped.append({"path": str(raw)[:200], "reason": exc.message})
                    continue
                if not os.path.exists(path):
                    skipped.append({"path": path, "reason": "not found"})
                    continue
                if path in self.queue:
                    skipped.append({"path": path, "reason": "already queued"})
                    continue
                self.queue.append(path)
                added.append(path)
        return added, skipped

    def remove(self, indices):
        removed = []
        with self.lock:
            for index in sorted(set(indices), reverse=True):
                if 0 <= index < len(self.queue):
                    removed.append(self.queue.pop(index))
        return removed

    def clear(self):
        with self.lock:
            count = len(self.queue)
            self.queue.clear()
        return count

    def selected(self, indices):
        with self.lock:
            if indices is None:
                return list(self.queue)
            return [self.queue[i] for i in indices
                    if isinstance(i, int) and 0 <= i < len(self.queue)]

    def drop(self, paths):
        with self.lock:
            before = len(self.queue)
            self.queue = [p for p in self.queue if p not in set(paths)]
            return before - len(self.queue)

    # -- serialisation ---------------------------------------------------- #
    def snapshot(self):
        with self.lock:
            mode = SHRED_MODES[self.mode]
            total = 0
            entries = []
            for index, path in enumerate(self.queue):
                info = describe(path)
                info["index"] = index
                info["passes"] = mode["passes"]
                reason = is_protected(path)
                info["shreddable"] = reason is None
                info["block_reason"] = reason
                # Only regular files contribute a known size. Walking a
                # directory tree here would make every state poll scan the
                # disk, so folder entries report a null size instead.
                if info["is_dir"]:
                    info["size"] = None
                    info["bytes_known"] = False
                else:
                    info["bytes_known"] = info.get("size") is not None
                    if info.get("size"):
                        total += info["size"] * mode["passes"]
                entries.append(info)

            return {
                "queue": entries,
                "count": len(entries),
                "total_bytes": total,
                "mode": self.mode,
                "mode_label": mode["label"],
                "mode_passes": mode["passes"],
                "trim": self.trim,
                "job": self.job.public() if self.job else None,
                "busy": bool(self.job and self.job.running),
                "home": os.path.expanduser("~"),
                "platform": platform.system(),
            }


class Job:
    """Progress record for one shred run."""

    def __init__(self, targets, passes, trim, pattern):
        self.id = secrets.token_hex(8)
        self.targets = list(targets)
        self.passes = passes
        self.trim = trim
        self.pattern = pattern
        self.running = True
        self.cancelled = False
        self.total_files = 0
        self.files_done = 0
        self.bytes_done = 0
        self.item_total = 0
        self.item_done = 0
        self.item_name = "Preparing..."
        self.started = time.time()
        self.finished = None
        self.succeeded = []
        self.failed = []
        self.trimmed = []
        self.trim_failed = []
        self._lock = threading.Lock()
        self._cancel = threading.Event()

    def cancel(self):
        self.cancelled = True
        self._cancel.set()

    @property
    def cancelled_now(self):
        return self._cancel.is_set()

    def update(self, **fields):
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def add_bytes(self, count):
        with self._lock:
            self.bytes_done += count
            self.item_done += count

    def begin_item(self, name, size):
        with self._lock:
            self.item_name = name
            self.item_total = max(size * self.passes, 1)
            self.item_done = 0

    def finish_item(self):
        with self._lock:
            self.files_done += 1

    def public(self):
        with self._lock:
            total_work = max(self.total_files, 1)
            pct = min(100.0, 100.0 * self.files_done / total_work)
            return {
                "id": self.id,
                "running": self.running,
                "cancelled": self.cancelled,
                "targets": list(self.targets),
                "passes": self.passes,
                "trim": self.trim,
                "total_files": self.total_files,
                "files_done": self.files_done,
                "bytes_done": self.bytes_done,
                "item_name": self.item_name,
                "item_done": self.item_done,
                "item_total": self.item_total,
                "overall_pct": round(pct, 2),
                "item_pct": round(
                    min(100.0, 100.0 * self.item_done / max(self.item_total, 1)), 2
                ),
                "succeeded": list(self.succeeded),
                "failed": list(self.failed),
                "trimmed": list(self.trimmed),
                "trim_failed": list(self.trim_failed),
                "elapsed": round((self.finished or time.time()) - self.started, 1),
            }


STATE = State()


# --------------------------------------------------------------------------- #
# Shredding worker
# --------------------------------------------------------------------------- #

def shred_file(job, path, passes, pattern):
    """Overwrite a file's bytes, then delete it. Returns bytes overwritten."""
    size = os.path.getsize(path)
    if size == 0:
        job.begin_item(os.path.basename(path), 0)
        job.finish_item()
        os.remove(path)
        return 0

    written = 0
    with open(path, "r+b") as handle:
        for _pass in range(passes):
            if job.cancelled_now:
                raise InterruptedError("cancelled")
            handle.seek(0)
            remaining = size
            while remaining > 0:
                if job.cancelled_now:
                    raise InterruptedError("cancelled")
                block = min(CHUNK_SIZE, remaining)
                if pattern == "null":
                    data = b"\0" * block
                else:
                    data = os.urandom(block)
                handle.write(data)
                remaining -= block
                written += block
                job.add_bytes(block)
            handle.flush()
            os.fsync(handle.fileno())
    return written


def shred_worker(job):
    """Background thread body for a shred job."""
    try:
        total = sum(count_files(path) for path in job.targets)
        job.update(total_files=total)

        for target in job.targets:
            if job.cancelled_now:
                break
            try:
                if os.path.isfile(target):
                    job.begin_item(os.path.basename(target),
                                   os.path.getsize(target))
                    shred_file(job, target, job.passes, job.pattern)
                    os.remove(target)
                    job.finish_item()
                    job.succeeded.append(target)

                elif os.path.isdir(target):
                    job.update(item_name="Preparing " + os.path.basename(target))
                    for root, _dirs, files in os.walk(target, topdown=False):
                        for name in files:
                            if job.cancelled_now:
                                raise InterruptedError("cancelled")
                            file_path = os.path.join(root, name)
                            reason = is_protected(file_path)
                            if reason:
                                # Report rather than silently skip, so the user
                                # can see exactly what the shredder refused.
                                job.failed.append([file_path, reason])
                                continue
                            try:
                                size = os.path.getsize(file_path)
                                job.begin_item(name, size)
                                shred_file(job, file_path, job.passes,
                                           job.pattern)
                                os.remove(file_path)
                                job.finish_item()
                                job.succeeded.append(file_path)
                            except (OSError, InterruptedError) as exc:
                                job.failed.append([file_path, str(exc)])
                    shutil.rmtree(target, ignore_errors=False)
                    job.succeeded.append(target)

            except InterruptedError:
                job.update(cancelled=True)
                break
            except (OSError, shutil.Error) as exc:
                job.failed.append([target, str(exc)])

        if job.succeeded and job.trim and not job.cancelled_now:
            drives = sorted({d for d in (drive_of(p) for p in job.succeeded) if d})
            ssds = detect_ssd_drives()
            for letter in drives:
                if letter in ssds:
                    ok, detail = run_trim(letter)
                    (job.trimmed if ok else job.trim_failed).append(
                        "%s: (%s)" % (letter, detail))

    except Exception as exc:  # pragma: no cover - defensive
        job.failed.append(["<job>", str(exc)])
    finally:
        job.update(running=False, finished=time.time())
        STATE.drop(job.succeeded)


# --------------------------------------------------------------------------- #
# Request handler
# --------------------------------------------------------------------------- #

class ShredderHandler(http.server.BaseHTTPRequestHandler):
    server_version = "FileShredder/1.3"
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------- #
    def log_message(self, fmt, *args):
        if "--verbose" in sys.argv:
            sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status=200):
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _html(self, markup, status=200):
        self._send(status, markup.encode("utf-8"), "text/html; charset=utf-8")

    def _error(self, message, status=400, detail=None):
        self._json({"ok": False, "error": message, "detail": detail}, status)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("invalid Content-Length", 400)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError("request body too large", 413)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError("body must be valid JSON", 400)
        if not isinstance(data, dict):
            raise ApiError("body must be a JSON object", 400)
        return data

    # -- security --------------------------------------------------------- #
    def _host_is_loopback(self):
        host = (self.headers.get("Host") or "").lower()
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host
        hostname = hostname.strip("[]")
        return hostname in ("127.0.0.1", "localhost", "::1", "")

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True  # same-origin fetches may omit it
        parsed = urllib.parse.urlparse(origin)
        return parsed.hostname in ("127.0.0.1", "localhost", "::1")

    def _authorised(self, mutating):
        if not self._host_is_loopback():
            self._error("non-loopback Host header rejected", 403)
            return False
        if not self._origin_ok():
            self._error("cross-origin request rejected", 403)
            return False
        if mutating:
            supplied = self.headers.get("X-Shredder-Token", "")
            if not secrets.compare_digest(supplied, STATE.token):
                self._error("missing or invalid CSRF token", 403)
                return False
        return True

    # -- routing ---------------------------------------------------------- #
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        mutating = route.startswith("/api/")
        if not self._authorised(mutating):
            return

        handler = None
        if method == "GET":
            handler = {
                "/": self.page_index,
                "/healthz": self.page_health,
                "/api/state": self.api_state,
                "/api/modes": self.api_modes,
                "/api/home_dir_contents": self.api_home_contents,
                "/api/list_dir": self.api_list_dir,
                "/api/ssds": self.api_ssds,
            }.get(route)
            if handler is None and route.startswith("/static/"):
                self.page_static(route[len("/static/"):])
                return
        elif method == "POST":
            handler = {
                "/api/add": self.api_add,
                "/api/remove": self.api_remove,
                "/api/clear": self.api_clear,
                "/api/load": self.api_load,
                "/api/save": self.api_save,
                "/api/open": self.api_open,
                "/api/shred": self.api_shred,
                "/api/cancel": self.api_cancel,
                "/api/options": self.api_options,
            }.get(route)

        if handler is None:
            self._error("no such endpoint: %s" % route, 404)
            return

        try:
            handler(query)
        except ApiError as exc:
            self._error(exc.message, exc.status, exc.detail)
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            self._error("internal error: %s" % exc, 500)

    # -- static pages ----------------------------------------------------- #
    def page_index(self, _query):
        markup = read_static("index.html")
        markup = markup.replace("__CSRF_TOKEN__", STATE.token)
        markup = markup.replace("__PORT__", str(self.server.server_address[1]))
        self._html(markup)

    def page_health(self, _query):
        self._json({"ok": True, "uptime": round(time.time() - STATE.started, 1)})

    def page_static(self, relative):
        """Serve an allowlisted asset from static/."""
        if relative not in STATIC_FILES:
            self._error("no such asset: %s" % relative, 404)
            return
        try:
            body = open(os.path.join(STATIC_DIR, relative), "rb").read()
        except OSError as exc:
            self._error("asset unavailable: %s" % (exc.strerror or exc), 500)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(relative)[1], "application/octet-stream")
        self._send(200, body, ctype)

    # -- read APIs -------------------------------------------------------- #
    def api_state(self, _query):
        self._json({"ok": True, **STATE.snapshot()})

    def api_modes(self, _query):
        self._json({"ok": True, "modes": SHRED_MODES, "active": STATE.mode})

    def api_home_contents(self, _query):
        home = os.path.expanduser("~")
        entries = list_directory(home)
        self._json({
            "ok": True,
            "path": home,
            "parent": os.path.dirname(home.rstrip("\\/")) or None,
            "entries": entries,
        })

    def api_list_dir(self, query):
        raw = (query.get("path") or [None])[0]
        path = normalize_path(raw or os.path.expanduser("~"))
        if not os.path.isdir(path):
            raise ApiError("not a directory: %s" % path, 400)
        parent = os.path.dirname(path.rstrip("\\/"))
        if parent == path:
            parent = None
        self._json({
            "ok": True,
            "path": path,
            "parent": parent,
            "entries": list_directory(path),
        })

    def api_ssds(self, _query):
        self._json({"ok": True, "ssds": detect_ssd_drives()})

    # -- queue APIs ------------------------------------------------------- #
    def api_options(self, _query):
        data = self._read_body()
        with STATE.lock:
            if "mode" in data:
                mode = data["mode"]
                if mode not in SHRED_MODES:
                    raise ApiError("unknown shredding mode", 400)
                STATE.mode = mode
            if "trim" in data:
                STATE.trim = bool(data["trim"])
        self._json({"ok": True, **STATE.snapshot()})

    def api_add(self, _query):
        data = self._read_body()
        paths = data.get("paths")
        if not isinstance(paths, list):
            raise ApiError("'paths' must be a list of strings", 400)
        if len(paths) > 5000:
            raise ApiError("too many paths in one request", 413)
        added, skipped = STATE.add(paths)
        self._json({
            "ok": True,
            "added": added,
            "skipped": skipped,
            **STATE.snapshot(),
        })

    def api_remove(self, _query):
        data = self._read_body()
        indices = data.get("indices")
        if indices is None:
            paths = data.get("paths")
            if not isinstance(paths, list):
                raise ApiError("provide 'indices' or 'paths'", 400)
            with STATE.lock:
                indices = [i for i, p in enumerate(STATE.queue)
                           if p in set(paths)]
        if not isinstance(indices, list):
            raise ApiError("'indices' must be a list of integers", 400)
        removed = STATE.remove(indices)
        self._json({"ok": True, "removed": removed, **STATE.snapshot()})

    def api_clear(self, _query):
        self._read_body()
        removed = STATE.clear()
        self._json({"ok": True, "cleared": removed, **STATE.snapshot()})

    def api_load(self, _query):
        data = self._read_body()
        path = normalize_path(data.get("path") or "")
        if os.path.isdir(path):
            raise ApiError("pick a .txt list file, not a folder", 400)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = [line.strip() for line in handle if line.strip()]
        except OSError as exc:
            raise ApiError("cannot read list: %s" % (exc.strerror or exc), 400)

        added, skipped = STATE.add(lines)
        self._json({
            "ok": True,
            "source": path,
            "read": len(lines),
            "added": added,
            "skipped": skipped,
            **STATE.snapshot(),
        })

    def api_save(self, _query):
        data = self._read_body()
        path = normalize_path(data.get("path") or "")
        # Saving is a plain file write, so OS permissions are the real gate.
        # Only guard against clobbering this tool's own files.
        own = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
        if os.path.normcase(os.path.dirname(path)) == own:
            raise ApiError("refusing to write into the application directory", 403)
        if os.path.isdir(path):
            raise ApiError("path is a folder", 400)
        if not path.lower().endswith(".txt"):
            path += ".txt"
        with STATE.lock:
            entries = list(STATE.queue)
        if not entries:
            raise ApiError("queue is empty", 400)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(entries) + "\n")
        except OSError as exc:
            raise ApiError("cannot write list: %s" % (exc.strerror or exc), 400)
        self._json({"ok": True, "saved": path, "count": len(entries)})

    def api_open(self, _query):
        data = self._read_body()
        targets = STATE.selected(data.get("indices"))
        if not targets:
            raise ApiError("nothing selected to open", 400)
        opened, failed = [], []
        for path in targets:
            if not os.path.isfile(path):
                failed.append([path, "not a file"])
                continue
            try:
                if platform.system() == "Windows":
                    os.startfile(path)  # noqa: S606 - user-requested
                elif platform.system() == "Darwin":
                    subprocess.run(["open", path], check=True)
                else:
                    subprocess.run(["xdg-open", path], check=True)
                opened.append(path)
            except (OSError, subprocess.SubprocessError) as exc:
                failed.append([path, str(exc)])
        self._json({"ok": True, "opened": opened, "failed": failed})

    # -- destructive ------------------------------------------------------ #
    def api_shred(self, _query):
        data = self._read_body()
        if not data.get("confirm"):
            raise ApiError("shred requires confirm=true", 400)

        with STATE.lock:
            if STATE.job and STATE.job.running:
                raise ApiError("a shred job is already running", 409)
            targets = STATE.selected(data.get("indices"))
        if not targets:
            raise ApiError("nothing selected to shred", 400)

        # This is the only gate on protected locations. Browsing and queueing
        # stay open; destroying system files is what we refuse.
        allowed, refused = split_by_protection(targets)
        if not allowed:
            raise ApiError(
                "every selected item is in a protected location and will not "
                "be shredded",
                403,
                {"refused": refused, "shreddable": []},
            )
        if refused and not data.get("allow_partial"):
            raise ApiError(
                "%d of %d selected items are in protected locations"
                % (len(refused), len(targets)),
                403,
                {"refused": refused, "shreddable": allowed},
            )

        with STATE.lock:
            mode = SHRED_MODES[STATE.mode]
            job = Job(allowed, mode["passes"], STATE.trim, mode["pattern"])
            STATE.job = job

        threading.Thread(target=shred_worker, args=(job,), daemon=True).start()
        self._json({
            "ok": True,
            "job": job.public(),
            "refused": refused,
            **STATE.snapshot(),
        })

    def api_cancel(self, _query):
        self._read_body()
        with STATE.lock:
            job = STATE.job
        if not job or not job.running:
            raise ApiError("no running job", 409)
        job.cancel()
        self._json({"ok": True, "job": job.public()})


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #

def read_static(name):
    with open(os.path.join(STATIC_DIR, name), "r", encoding="utf-8") as handle:
        return handle.read()


def build_server(host, port, allow_remote):
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not allow_remote:
        raise SystemExit(
            "refusing to bind %s: this API deletes files.\n"
            "Pass --allow-remote if you genuinely need remote access, and "
            "put it behind a firewall or an SSH tunnel." % host
        )

    errors = []
    for candidate in (port, 0):
        try:
            httpd = http.server.ThreadingHTTPServer(
                (host, candidate), ShredderHandler
            )
        except OSError as exc:
            errors.append("%s:%d (%s)" % (host, candidate, exc))
            continue
        httpd.daemon_threads = True
        return httpd, httpd.server_address[1]
    raise SystemExit("could not bind a port; tried: " + ", ".join(errors))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="File Shredder UI and JSON API (loopback only).",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="preferred port (default: %d)" % DEFAULT_PORT)
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="bind address (default: %s)" % DEFAULT_HOST)
    parser.add_argument("--no-browser", "--no-open", dest="open_browser",
                        action="store_false",
                        help="do not launch a web browser")
    parser.add_argument("--allow-remote", action="store_true",
                        help="permit a non-loopback bind (dangerous)")
    parser.add_argument("--verbose", action="store_true",
                        help="log every request to stderr")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if not os.path.isdir(STATIC_DIR):
        raise SystemExit("missing static/ directory next to this script")

    httpd, port = build_server(args.host, args.port, args.allow_remote)
    host = httpd.server_address[0]
    url = "http://%s:%d/" % (host, port)

    print("File Shredder 1.3")
    print("  url      %s" % url)
    print("  binding  %s:%d" % (host, port))
    print("  home     %s" % os.path.expanduser("~"))
    print("  mode     %s (%d passes)" % (
        SHRED_MODES[STATE.mode]["label"], SHRED_MODES[STATE.mode]["passes"]))
    print("  trim     %s" % ("on" if STATE.trim else "off"))
    print("  ssds     %s" % (", ".join(sorted(detect_ssd_drives())) or "none detected"))
    print("  csrf     enabled (X-Shredder-Token)")
    print("  stop     Ctrl+C")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("\n  !! WARNING: bound to a public interface. Anyone who can reach")
        print("  !! this port can delete files on this machine.")
    print()

    if args.open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        with STATE.lock:
            if STATE.job and STATE.job.running:
                STATE.job.cancel()
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())