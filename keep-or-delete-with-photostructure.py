#!/usr/bin/env python3
"""Evaluate chosen photos against a local PhotoStructure library."""

from __future__ import annotations

import http.client
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import ctypes
    from ctypes import wintypes
except ImportError:
    ctypes = None  # type: ignore[assignment]
    wintypes = None  # type: ignore[assignment]

HASH_RE = re.compile(r"^[A-Za-z0-9+/=]+$")
CMD_CHAR_LIMIT = 30_000
INFO_BATCH_FILES = 40
SCAN_PROGRESS_S = 5.0
DEFAULT_PORT = 1787
SKIP_DIR_NAMES = {".photostructure"}
SKIP_FILE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
MODE_ROTATED = "Match rotated"
MODE_EXACT = "Match exact only"
FILTER_ALL = "All"
FILTER_MATCHES = "Matches"
FILTER_NONE = "No match"
FILTER_ERRORS = "Errors"
STATUS_PENDING = "pending"
STATUS_MATCH = "match"
STATUS_NO_MATCH = "no match"
STATUS_ERROR = "error"
STATUS_DELETED = "deleted"

HASH_KEYS = ("pHash", "rHash", "wHash")
HASH0_KEYS = ("pHash0", "rHash0", "wHash0")


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def is_valid_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and HASH_RE.fullmatch(value) is not None


def normalize_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def same_file(a: str, b: str) -> bool:
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return normalize_path(a) == normalize_path(b)


def extract_hash_fields(obj: dict[str, Any]) -> dict[str, Optional[str]]:
    out: dict[str, Optional[str]] = {}
    for key in HASH_KEYS + HASH0_KEYS:
        val = obj.get(key)
        if val is None and "imageHash" in obj and isinstance(obj["imageHash"], dict):
            val = obj["imageHash"].get(key)
        if isinstance(val, str) and val:
            out[key] = val
        else:
            out[key] = None
    return out


def canonical_rotated(hashes: dict[str, Optional[str]]) -> Optional[tuple[str, str, str]]:
    zeros = [hashes.get(k) for k in HASH0_KEYS]
    plains = [hashes.get(k) for k in HASH_KEYS]
    if all(is_valid_hash(v) for v in zeros):
        return zeros[0], zeros[1], zeros[2]  # type: ignore[return-value]
    if all(is_valid_hash(v) for v in plains):
        return plains[0], plains[1], plains[2]  # type: ignore[return-value]
    return None


def dump_matches_mode(obj: dict[str, Any], mode: str, target: dict[str, Optional[str]]) -> bool:
    dumped = extract_hash_fields(obj)
    if mode == MODE_EXACT:
        return all(dumped.get(k) == target.get(k) and is_valid_hash(target.get(k)) for k in HASH_KEYS)
    canon = canonical_rotated(target)
    if canon is None:
        return False
    p, r, w = canon
    z = [dumped.get(k) for k in HASH0_KEYS]
    if z == [p, r, w]:
        return True
    if all(v is None for v in z) and [dumped.get(k) for k in HASH_KEYS] == [p, r, w]:
        return True
    return False


def where_group(mode: str, hashes: dict[str, Optional[str]]) -> Optional[str]:
    if mode == MODE_EXACT:
        if not all(is_valid_hash(hashes.get(k)) for k in HASH_KEYS):
            return None
        parts = [f"AssetFile.{k} = {sql_quote(hashes[k])}" for k in HASH_KEYS]  # type: ignore[arg-type]
        return "(" + " AND ".join(parts) + ")"
    canon = canonical_rotated(hashes)
    if canon is None:
        return None
    p, r, w = canon
    rotated = (
        f"(AssetFile.pHash0 = {sql_quote(p)} AND AssetFile.rHash0 = {sql_quote(r)} "
        f"AND AssetFile.wHash0 = {sql_quote(w)})"
    )
    upright = (
        "(AssetFile.pHash0 IS NULL AND AssetFile.rHash0 IS NULL AND AssetFile.wHash0 IS NULL "
        f"AND AssetFile.pHash = {sql_quote(p)} AND AssetFile.rHash = {sql_quote(r)} "
        f"AND AssetFile.wHash = {sql_quote(w)})"
    )
    return f"({rotated} OR {upright})"


def parse_json_stream(text: str) -> list[Any]:
    start = None
    for idx, ch in enumerate(text):
        if ch in "{[":
            start = idx
            break
    if start is None:
        return []
    text = text[start:].strip()
    if not text:
        return []
    try:
        val = json.loads(text)
        if isinstance(val, list):
            return val
        return [val]
    except json.JSONDecodeError:
        pass
    items: list[Any] = []
    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        items.append(obj)
        idx = end
    if items:
        return items
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def flatten_info_records(parsed: list[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict) and any(str(k).isdigit() for k in item.keys()) and not item.get("nativePath"):
            numeric_keys = sorted((k for k in item if str(k).isdigit()), key=lambda k: int(k))
            if numeric_keys:
                for k in numeric_keys:
                    val = item[k]
                    if isinstance(val, dict):
                        records.append(val)
                continue
        if isinstance(item, dict):
            records.append(item)
        elif isinstance(item, list):
            records.extend(x for x in item if isinstance(x, dict))
    return records


def record_path(rec: dict[str, Any]) -> Optional[str]:
    for key in ("nativePath", "path", "filename", "file"):
        val = rec.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def newest_photostructure_js() -> Optional[Path]:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "npm-cache" / "_npx"
    if not root.is_dir():
        return None
    newest: Optional[Path] = None
    newest_mtime = -1.0
    try:
        for path in root.glob("*/node_modules/photostructure/photostructure.js"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest = path
                newest_mtime = mtime
    except OSError:
        return newest
    return newest


@dataclass
class ResolvedCmd:
    argv_prefix: list[str]
    js_path: Optional[Path] = None
    display: str = ""


def resolve_photostructure() -> Optional[ResolvedCmd]:
    env = os.environ.get("PHOTOSTRUCTURE")
    if env:
        path = Path(env)
        if path.is_file():
            if path.suffix.lower() == ".js":
                node = shutil.which("node") or shutil.which("node.exe")
                if node:
                    return ResolvedCmd([node, str(path)], path, str(path))
            return ResolvedCmd([str(path)], None, str(path))
    which = shutil.which("photostructure") or shutil.which("photostructure.cmd")
    if which:
        return ResolvedCmd([which], None, which)
    js = newest_photostructure_js()
    if js:
        node = shutil.which("node") or shutil.which("node.exe")
        if node:
            return ResolvedCmd([node, str(js)], js, str(js))
    return None


def settings_toml_path() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "PhotoStructure" / "settings.toml"


def read_settings_value(key: str) -> Optional[str]:
    settings = settings_toml_path()
    if not settings.is_file():
        return None
    try:
        text = settings.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*(.+)$")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = pattern.match(stripped)
        if not match:
            continue
        raw = match.group(1).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            return raw[1:-1]
        return raw
    return None


def read_http_port() -> int:
    value = read_settings_value("httpPort")
    if value and value.isdigit():
        return int(value)
    return DEFAULT_PORT


def library_dir() -> Optional[Path]:
    value = read_settings_value("libraryDir")
    if not value:
        return None
    path = Path(value)
    return path if path.is_dir() else None


def base_url() -> str:
    return f"http://127.0.0.1:{read_http_port()}/"


def activity_url() -> str:
    return urllib.parse.urljoin(base_url(), "activity")


def _hidden_photostructure_path(path: str) -> bool:
    return any(part.lower() == ".photostructure" for part in Path(path).parts)


def _entry_path(entry: dict[str, Any]) -> str:
    for key in ("path", "filename", "file", "dir", "directory"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _entry_reason(entry: dict[str, Any]) -> str:
    for key in ("details", "reason", "why", "message", "detail"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


PROBLEM_FILE_STATES = {
    "excluded",
    "excludeddir",
    "rejected",
    "unavailable",
    "blocklisted",
    "failed",
    "warning",
}


def _is_problem_entry(entry: dict[str, Any]) -> bool:
    state = str(entry.get("state") or entry.get("status") or "").lower()
    return state in PROBLEM_FILE_STATES


def _entry_is_dir(entry: dict[str, Any], path: str) -> bool:
    kind = str(entry.get("type") or "").lower()
    if kind in {"dir", "directory", "folder"}:
        return True
    if kind == "file":
        return False
    if entry.get("isDirectory") or entry.get("directory"):
        return True
    return path.endswith(("/", "\\")) or os.path.isdir(path)


TOTAL_FILES_RE = re.compile(r"—\s*(?P<total>\d+)\s+total files", re.I)


def _iter_report_entries(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry


def _is_imports_complete(details: str) -> bool:
    return "file imports complete:" in details.lower()


def _imports_complete_is_clean(details: str) -> bool:
    lower = details.lower()
    return "no files skipped" in lower and "no errors" in lower


def _total_file_count(details: str) -> Optional[int]:
    match = TOTAL_FILES_RE.search(details)
    if match is None:
        return None
    return int(match.group("total"))


def _collect_excluded_from_text(
    text: str,
    seen: Optional[set[str]] = None,
    found: Optional[list[tuple[str, str, bool]]] = None,
) -> list[tuple[str, str, bool]]:
    if seen is None:
        seen = set()
    if found is None:
        found = []
    for entry in _iter_report_entries(text):
        if not _is_problem_entry(entry):
            continue
        path = _entry_path(entry)
        if not path or _hidden_photostructure_path(path):
            continue
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            continue
        seen.add(key)
        found.append((path, _entry_reason(entry), _entry_is_dir(entry, path)))
    return found


def sync_report_files() -> list[Path]:
    lib = library_dir()
    if lib is None:
        return []
    root = lib / ".photostructure" / "sync-reports"
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*-sync-report.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def latest_imports_complete() -> Optional[tuple[int, str, dict[str, Any]]]:
    """Newest SyncDirectory 'File imports complete' note for a folder that had files."""
    best: Optional[tuple[int, str, dict[str, Any]]] = None
    for report in sync_report_files()[:40]:
        try:
            text = report.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for entry in _iter_report_entries(text):
            if str(entry.get("from") or "") != "SyncDirectory":
                continue
            details = str(entry.get("details") or "")
            if not _is_imports_complete(details):
                continue
            total = _total_file_count(details)
            if total == 0:
                continue
            ts = entry.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            stamp = int(ts)
            if best is None or stamp > best[0]:
                best = (stamp, text, entry)
    return best


def latest_excluded_items() -> list[tuple[str, str, bool]]:
    """Problem files from the newest scan-path 'File imports complete' report."""
    complete = latest_imports_complete()
    if complete is None:
        return []
    _ts, text, entry = complete
    details = str(entry.get("details") or "")
    if _imports_complete_is_clean(details):
        return []
    found = _collect_excluded_from_text(text)
    if found:
        return found
    path = _entry_path(entry) or "scan"
    return [(path, details, True)]


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def http_get(url: str, timeout: float = 10.0) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


def http_post(url: str, timeout: float = 10.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


def popen_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {"shell": False}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


def start_argv() -> Optional[list[str]]:
    js = newest_photostructure_js()
    node = shutil.which("node") or shutil.which("node.exe")
    if js is None or node is None:
        return None
    return [node, str(js)]


def local_photostructure_version() -> Optional[str]:
    argv = start_argv()
    if argv is None:
        which = shutil.which("photostructure") or shutil.which("photostructure.cmd")
        if not which:
            return None
        argv = [which]
    try:
        proc = subprocess.run(
            argv + ["--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            **popen_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    match = re.search(r"\d{4}\.\d+(?:\.\d+)?", text)
    return match.group(0) if match else None


def parse_version(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", text)
    return tuple(int(p) for p in parts) if parts else ()


def version_newer(remote: str, local: str) -> bool:
    a = parse_version(remote)
    b = parse_version(local)
    if not a or not b:
        return False
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a > b


_VERSION_CACHE: dict[str, Any] = {"t": 0.0, "local": None, "remote": None}


def cached_versions(*, force: bool = False) -> tuple[Optional[str], Optional[str]]:
    now = time.monotonic()
    if not force and now - float(_VERSION_CACHE["t"]) < 60:
        return _VERSION_CACHE["local"], _VERSION_CACHE["remote"]
    _VERSION_CACHE["local"] = local_photostructure_version()
    _VERSION_CACHE["remote"] = npm_stable_version()
    _VERSION_CACHE["t"] = now
    return _VERSION_CACHE["local"], _VERSION_CACHE["remote"]


def npm_stable_version() -> Optional[str]:
    url = "https://registry.npmjs.org/photostructure/stable"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    ver = data.get("version") if isinstance(data, dict) else None
    return ver if isinstance(ver, str) and ver else None


def stable_update_available(*, force: bool = False) -> bool:
    local, remote = cached_versions(force=force)
    if not local or not remote:
        return False
    return version_newer(remote, local)


def set_enabled(widget: ttk.Button, enabled: bool) -> None:
    widget.state(["!disabled"] if enabled else ["disabled"])


def link_label(text: str) -> str:
    return "↗ " + text


def make_scrolled(parent: tk.Widget, factory: Any, *, x: bool = False, y: bool = True, **kwargs: Any) -> tuple[Any, ttk.Frame]:
    frame = ttk.Frame(parent)
    widget = factory(frame, **kwargs)
    widget.grid(row=0, column=0, sticky="nsew")
    if y:
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=0, column=1, sticky="ns")
    if x:
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=widget.xview)
        widget.configure(xscrollcommand=xscroll.set)
        xscroll.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return widget, frame


def progress_states_from_sse_obj(obj: Any) -> Optional[list[str]]:
    if not isinstance(obj, dict):
        return None
    payload: Any = obj
    channel = obj.get("channel")
    if channel is not None and channel != "progress":
        return None
    if channel == "progress" and isinstance(obj.get("data"), dict):
        payload = obj["data"]
    states = payload.get("states") if isinstance(payload, dict) else None
    if not isinstance(states, list):
        return None
    names: list[str] = []
    for item in states:
        if isinstance(item, dict):
            names.append(str(item.get("state") or item.get("status") or ""))
        else:
            names.append(str(item))
    return names


class ProgressSse:
    """Keep one /sse/all?channels=progress stream open and parse snapshots."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.conn: Optional[http.client.HTTPConnection] = None
        self.resp: Optional[http.client.HTTPResponse] = None
        self.buf = ""
        self.connected = False
        self.last_states: Optional[list[str]] = None

    def close(self) -> None:
        try:
            if self.conn is not None:
                self.conn.close()
        except OSError:
            pass
        self.conn = None
        self.resp = None

    def _connect(self) -> None:
        self.close()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=2.0)
        conn.request(
            "GET",
            "/sse/all?channels=progress",
            headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
        )
        self.resp = conn.getresponse()
        self.conn = conn
        if self.resp.status == 200:
            self.connected = True

    def pump(self) -> None:
        if self.resp is None:
            try:
                self._connect()
            except (OSError, http.client.HTTPException, TimeoutError):
                return
        assert self.resp is not None
        try:
            chunk = self.resp.read(4096)
        except socket.timeout:
            return
        except (OSError, http.client.HTTPException, TimeoutError):
            self.close()
            return
        if not chunk:
            return
        self.buf += chunk.decode("utf-8", errors="replace")
        while True:
            split = re.split(r"\r?\n\r?\n", self.buf, maxsplit=1)
            if len(split) < 2:
                break
            block, self.buf = split
            if ": connected" in block or block.strip() == ": connected":
                self.connected = True
            data_lines = [
                line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
            ]
            if not data_lines:
                continue
            try:
                obj = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                continue
            states = progress_states_from_sse_obj(obj)
            if states is not None:
                self.connected = True
                self.last_states = states


def recycle_files(paths: list[str]) -> tuple[bool, str]:
    if not paths:
        return True, ""
    if ctypes is None:
        return False, "Recycle Bin is unavailable on this Python build."
    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40
    FOF_NOCONFIRMATION = 0x10
    FOF_SILENT = 0x04

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.USHORT),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    joined = "\0".join(paths) + "\0\0"
    from_buf = ctypes.create_unicode_buffer(joined)
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(from_buf, wintypes.LPCWSTR)
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted:
        return False, f"Recycle Bin returned {result}"
    return True, ""


def should_skip_file(path: Path) -> bool:
    return path.name.lower() in SKIP_FILE_NAMES


def iter_files_from_root(root: Path):
    if root.is_file():
        if not should_skip_file(root):
            try:
                yield str(root.resolve())
            except OSError:
                return
        return
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIR_NAMES]
        for name in filenames:
            path = Path(dirpath) / name
            if should_skip_file(path):
                continue
            try:
                if path.is_file():
                    yield str(path.resolve())
            except OSError:
                continue


def collect_files_from_root(root: Path) -> list[str]:
    return list(iter_files_from_root(root))


def argv_char_len(argv: list[str]) -> int:
    return sum(len(a) + 1 for a in argv)


def batch_paths(
    prefix: list[str],
    paths: list[str],
    max_items: Optional[int] = None,
) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    for path in paths:
        trial = prefix + current + [path]
        over_chars = current and argv_char_len(trial) > CMD_CHAR_LIMIT
        over_count = bool(max_items and current and len(current) >= max_items)
        if over_chars or over_count:
            batches.append(current)
            current = [path]
        else:
            current.append(path)
    if current:
        batches.append(current)
    return batches


@dataclass
class FileResult:
    path: str
    status: str = STATUS_PENDING
    exit_code: Optional[int] = None
    elapsed: Optional[float] = None
    matches: list[str] = field(default_factory=list)
    hashes: dict[str, Optional[str]] = field(default_factory=dict)
    info_stdout: str = ""
    info_stderr: str = ""
    list_stdout: str = ""
    list_stderr: str = ""
    command: str = ""
    error: str = ""
    exists: bool = True
    listed_self: bool = False

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def parent(self) -> str:
        return os.path.dirname(self.path)


class KeepOrDeleteApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Keep or Delete with PhotoStructure")
        self.root.geometry("1480x920")
        self.root.minsize(1100, 880)
        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.busy = False
        self.roots: list[str] = []
        self.results: dict[str, FileResult] = {}
        self.order: list[str] = []
        self.filter_var = tk.StringVar(value=FILTER_ALL)
        self.mode_var = tk.StringVar(value=MODE_ROTATED)
        self.library_wait_var = tk.BooleanVar(value=True)
        self.stop_on_close_var = tk.BooleanVar(value=True)
        self.preview_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Add files or a folder, then Ask PhotoStructure.")
        self.summary_var = tk.StringVar(value="0 files · 0 with a match · 0 with no match · 0 error")
        self.log_lines: list[str] = []
        self.selected_path: Optional[str] = None
        self.row_ids: dict[str, str] = {}
        self.sort_column = "status"
        self.sort_reverse = False
        self.ps_running = False
        self.update_available = False
        self.update_prompt = threading.Event()
        self.update_prompt_yes = False
        self._boot_lock = threading.Lock()
        self._boot_thread: Optional[threading.Thread] = None
        self._boot_ready = threading.Event()
        self._boot_started_ps = False
        self._exclude_scanned = False
        self._exclude_text = ""
        self._preview_photo: Any = None
        self._preview_seq = 0
        self._preview_path: Optional[str] = None
        self._preview_resize_job: Optional[str] = None
        self._preview_size: tuple[int, int] = (0, 0)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._drain_queue)
        self.root.after(200, self._poll_photostructure_state)

    def _build(self) -> None:
        top = ttk.LabelFrame(self.root, text="PhotoStructure Local Web Server", padding=8)
        self.ps_frame = top
        top.pack(fill="x", padx=8, pady=(8, 0))
        self.start_btn = ttk.Button(top, text="Start and open", command=self.start_and_open)
        self.start_btn.pack(side="left", padx=2)
        self.stop_btn = ttk.Button(top, text="Stop", command=self.stop_photostructure)
        self.stop_btn.pack(side="left", padx=2)
        self.sync_btn = ttk.Button(top, text="Sync", command=self.sync_photostructure)
        self.sync_btn.pack(side="left", padx=2)
        self.update_btn = ttk.Button(top, text="Update", command=self.update_photostructure)
        self.update_btn.pack(side="left", padx=2)
        ttk.Checkbutton(
            top,
            text="Stop web server when closing this tool",
            variable=self.stop_on_close_var,
        ).pack(side="left", padx=(16, 2))

        self.exclude_frame = tk.Frame(self.root, bg="#fff3cd", highlightbackground="#e0c36a", highlightthickness=1)
        self.exclude_label = tk.Label(
            self.exclude_frame,
            text="",
            bg="#fff3cd",
            fg="#6b4f00",
            justify="left",
            anchor="w",
            wraplength=1200,
            cursor="hand2",
            font=("Segoe UI", 9),
        )
        self.exclude_label.pack(fill="x", padx=10, pady=8)
        self.exclude_label.bind("<Button-1>", self._open_activity)
        self.exclude_frame.bind("<Button-1>", self._open_activity)
        self.exclude_frame.bind("<Configure>", self._resize_exclude_label)

        setup = ttk.LabelFrame(self.root, text="Pick files", padding=8)
        setup.pack(fill="x", padx=8, pady=(8, 0))
        picker = ttk.Frame(setup)
        picker.pack(fill="x")
        ttk.Button(picker, text="Add folders", command=self.add_folders).pack(side="left")
        ttk.Button(picker, text="Add files", command=self.add_files).pack(side="left", padx=(6, 0))
        ttk.Button(picker, text="Remove selected", command=self.remove_root).pack(side="left", padx=6)
        ttk.Label(picker, text="Compare:").pack(side="left", padx=(16, 4))
        ttk.Combobox(
            picker,
            textvariable=self.mode_var,
            values=(MODE_ROTATED, MODE_EXACT),
            state="readonly",
            width=20,
        ).pack(side="left")
        ttk.Checkbutton(
            picker,
            text="Sync library before asking",
            variable=self.library_wait_var,
        ).pack(side="left", padx=12)
        ttk.Button(picker, text="Ask PhotoStructure", command=self.evaluate).pack(side="left")
        self.cancel_btn = ttk.Button(picker, text="Cancel", command=self.cancel)
        self.cancel_btn.pack(side="left", padx=4)
        ttk.Label(setup, text="Selected roots").pack(anchor="w", pady=(8, 2))
        self.roots_list, roots_scroll = make_scrolled(
            setup,
            tk.Listbox,
            x=True,
            y=True,
            height=4,
            exportselection=False,
            activestyle="none",
        )
        roots_scroll.pack(fill="x")
        self.roots_list.bind("<Double-1>", self.on_root_activate)
        progress_row = ttk.Frame(setup)
        progress_row.pack(fill="x", pady=(8, 0))
        self.eval_progress = ttk.Progressbar(progress_row, mode="determinate")
        self.eval_progress.pack(fill="x")
        self.roots_list.bind("<Motion>", self._roots_cursor)

        paned = ttk.Panedwindow(self.root, orient="horizontal")

        left = ttk.LabelFrame(paned, text="Results: Evaluate keep or delete", padding=4)
        summary = ttk.Frame(left)
        summary.pack(fill="x", pady=(0, 4))
        ttk.Label(summary, textvariable=self.summary_var).pack(side="left")
        ttk.Label(summary, text="Filter:").pack(side="left", padx=(16, 4))
        ttk.Combobox(
            summary,
            textvariable=self.filter_var,
            values=(FILTER_ALL, FILTER_MATCHES, FILTER_NONE, FILTER_ERRORS),
            state="readonly",
            width=12,
        ).pack(side="left")
        ttk.Checkbutton(
            summary,
            text="Preview selected image",
            variable=self.preview_var,
            command=self._on_preview_toggle,
        ).pack(side="left", padx=(16, 0))
        cols = ("status", "name", "matches", "folder")
        self.tree, tree_scroll = make_scrolled(
            left, ttk.Treeview, x=True, y=True, columns=cols, show="headings", selectmode="extended"
        )
        self.tree.heading("status", text="Status", command=lambda: self.sort_by("status"))
        self.tree.heading("name", text="File; double-click to open", command=lambda: self.sort_by("name"))
        self.tree.heading("matches", text="Matches", command=lambda: self.sort_by("matches"))
        self.tree.heading("folder", text="Folder; double-click to show in Explorer", command=lambda: self.sort_by("folder"))
        self.tree.column("status", width=90, minwidth=70, stretch=False)
        self.tree.column("name", width=360, minwidth=160, stretch=False)
        self.tree.column("matches", width=80, minwidth=60, stretch=False, anchor="center")
        self.tree.column("folder", width=720, minwidth=200, stretch=False)
        tree_scroll.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select_row)
        self.tree.bind("<Double-1>", self.on_result_click)
        self.tree.bind("<Motion>", lambda e: self._link_cursor(self.tree, e, {"#2", "#4"}))
        self.tree.bind("<Shift-MouseWheel>", self._on_tree_shift_wheel)
        paned.add(left, weight=1)

        self.right_paned = ttk.Panedwindow(paned, orient="vertical")
        paned.add(self.right_paned, weight=1)
        self.detail_frame = ttk.LabelFrame(self.right_paned, text="Selected result file", padding=4)
        self.preview_frame = tk.Frame(self.detail_frame, bg="#1e1e1e", height=260)
        self.preview_frame.pack_propagate(False)
        self.preview_label = tk.Label(
            self.preview_frame,
            text="",
            bg="#1e1e1e",
            fg="#c8c8c8",
            anchor="center",
        )
        self.preview_label.pack(fill="both", expand=True)
        self.preview_frame.bind("<Configure>", self._on_preview_configure)
        self.detail, detail_scroll = make_scrolled(self.detail_frame, tk.Text, x=True, y=True, wrap="none", height=18)
        self._detail_scroll = detail_scroll
        detail_scroll.pack(fill="both", expand=True)
        self.right_paned.add(self.detail_frame, weight=3)

        self.match_frame = ttk.LabelFrame(
            self.right_paned, text="Library matches for selected result file", padding=4
        )
        self.match_tree, match_scroll = make_scrolled(
            self.match_frame,
            ttk.Treeview,
            x=True,
            y=True,
            columns=("file", "folder"),
            show="headings",
            selectmode="browse",
            height=8,
        )
        self.match_tree.heading("file", text="File; double-click to open")
        self.match_tree.heading("folder", text="Folder; double-click to show in Explorer")
        self.match_tree.column("file", width=280, minwidth=120, stretch=False)
        self.match_tree.column("folder", width=720, minwidth=200, stretch=False)
        match_scroll.pack(fill="both", expand=True)
        self.match_tree.bind("<Double-1>", self.on_match_click)
        self.match_tree.bind("<Motion>", lambda e: self._link_cursor(self.match_tree, e, {"#1", "#2"}))
        self.match_tree.bind("<Shift-MouseWheel>", self._on_tree_shift_wheel)
        self.match_paths: dict[str, str] = {}
        self._match_pane_shown = False

        log_frame = ttk.LabelFrame(self.root, text="Status log", padding=4)
        log_frame.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        log_frame.pack_propagate(False)
        log_frame.configure(height=160)
        self.log, log_scroll = make_scrolled(log_frame, tk.Text, x=True, y=True, height=8, wrap="none")
        log_scroll.pack(fill="both", expand=True)

        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(side="bottom", fill="x")
        self.bulk_btn = ttk.Button(bottom, text="Delete all matched files", command=self.delete_all_matched)
        self.bulk_btn.pack(side="left")
        self.delete_selected_btn = ttk.Button(
            bottom, text="Delete selected matched files", command=self.delete_selected_matched
        )
        self.delete_selected_btn.pack(side="left", padx=8)
        self.retry_errors_btn = ttk.Button(
            bottom, text="Retry files with errors", command=self.retry_errors
        )
        self.retry_errors_btn.pack(side="left")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", padx=12)

        paned.pack(fill="both", expand=True, padx=8, pady=8)
        self._update_action_buttons()
        self.filter_var.trace_add("write", lambda *_: self.refresh_table())
        self._ensure_boot_started()
        threading.Thread(target=self._queue_excluded_warning, daemon=True).start()

    def on_close(self) -> None:
        running = http_ok(base_url())
        if self.stop_on_close_var.get():
            if running:
                try:
                    http_post(base_url() + "admin/shutdown", timeout=5)
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    self.log_status(f"Shutdown request failed: {exc}")
        elif running:
            messagebox.showinfo(
                "PhotoStructure is still running",
                "PhotoStructure is still running. You can stop it from the webpage sidebar using Shut down.",
            )
        self.root.destroy()

    def log_status(self, message: str) -> None:
        self.status_var.set(message)
        self.log_lines.append(message)
        self.log.insert("end", message + "\n")
        self.log.see("end")

    def _reset_eval_progress(self, total: int) -> None:
        self.eval_progress.stop()
        self.eval_progress.configure(mode="determinate", maximum=max(total, 1), value=0)

    def _queue_eval_progress(self, value: int, total: int, text: str) -> None:
        self.ui_queue.put(("progress", {"value": value, "max": max(total, 1), "text": text}))

    def _apply_eval_progress(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "")
        if text:
            self.status_var.set(text)
        if str(self.eval_progress.cget("mode")) == "indeterminate":
            return
        total = max(int(payload.get("max") or 1), 1)
        value = max(0, min(int(payload.get("value") or 0), total))
        self.eval_progress.configure(maximum=total, value=value)

    def _append_roots(self, added: list[str]) -> None:
        for path in added:
            if path not in self.roots:
                self.roots.append(path)
                self.roots_list.insert("end", link_label(path))

    def _ensure_boot_started(self) -> None:
        with self._boot_lock:
            if self._boot_thread is not None and self._boot_thread.is_alive():
                return
            if self._boot_ready.is_set() and http_ok(base_url()):
                return
            self._boot_ready.clear()
            self._boot_thread = threading.Thread(target=self._background_boot, daemon=True)
            self._boot_thread.start()

    def _start_photostructure_process(self) -> bool:
        argv = start_argv()
        if argv is None:
            self.ui_queue.put(("status", "PhotoStructure was not found. Use Update."))
            return False
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen_kwargs())
        except OSError as exc:
            self.ui_queue.put(("status", f"Could not start PhotoStructure: {exc}"))
            return False
        self._boot_started_ps = True
        return True

    def _wait_until_healthy(self, cancel: Optional[threading.Event] = None) -> bool:
        health = base_url() + "api/health/checks"
        while True:
            if cancel is not None and cancel.is_set():
                return False
            if http_ok(health, timeout=3.0):
                return True
            time.sleep(0.5)

    def _background_boot(self) -> None:
        try:
            url = base_url()
            if not http_ok(url):
                self.ui_queue.put(("status", "Preparing PhotoStructure…"))
                if not self._start_photostructure_process():
                    return
                if not self._wait_until_healthy():
                    return
            self.ui_queue.put(("status", "PhotoStructure is ready."))
            self._queue_excluded_warning()
        except Exception as exc:
            self.ui_queue.put(("status", f"Could not prepare PhotoStructure: {exc}"))
        finally:
            self._boot_ready.set()

    def add_files(self) -> None:
        files = filedialog.askopenfilenames(title="Choose files")
        self._append_roots([os.path.abspath(p) for p in files])

    def add_folders(self) -> None:
        folder = filedialog.askdirectory(title="Choose folder")
        if folder:
            self._append_roots([os.path.abspath(folder)])

    def remove_root(self) -> None:
        sel = list(self.roots_list.curselection())
        sel.reverse()
        for idx in sel:
            self.roots_list.delete(idx)
            del self.roots[idx]

    def queued_files(self) -> list[str]:
        return self._scan_queued_files(report_progress=False)

    def _scan_queued_files(self, report_progress: bool = True) -> list[str]:
        seen: set[str] = set()
        files: list[str] = []
        started = time.monotonic()
        last_report = started
        announced = False
        if report_progress:
            self.ui_queue.put(("status", "Looking for files to evaluate…"))
            self._queue_eval_progress(0, 1, "Looking for files to evaluate…")
        for root in self.roots:
            for path in iter_files_from_root(Path(root)):
                if self.cancel_event.is_set():
                    return files
                key = normalize_path(path)
                if key in seen:
                    continue
                seen.add(key)
                files.append(path)
                now = time.monotonic()
                if report_progress and now - started >= SCAN_PROGRESS_S and now - last_report >= SCAN_PROGRESS_S:
                    announced = True
                    last_report = now
                    message = f"Found {len(files)} file(s) so far…"
                    self.ui_queue.put(("status", message))
                    self._queue_eval_progress(len(files), max(len(files), 1), message)
        if report_progress:
            if files:
                self.ui_queue.put(
                    ("status", f"Found {len(files)} file(s) to evaluate.")
                )
            elif announced:
                self.ui_queue.put(("status", "No files found in the selected roots."))
        return files

    def evaluate(self) -> None:
        if self.busy:
            messagebox.showinfo("Ask PhotoStructure", "A run is already in progress.")
            return
        if not self.roots:
            messagebox.showinfo("Ask PhotoStructure", "Add files or a folder first.")
            return
        resolved = resolve_photostructure()
        if resolved is None:
            self.log_status("PhotoStructure was not found. Use Update.")
            return
        mode = self.mode_var.get()
        wait_library = bool(self.library_wait_var.get())
        self.cancel_event.clear()
        self.update_prompt.clear()
        self.update_prompt_yes = False
        self.busy = True
        self.eval_progress.stop()
        self.eval_progress.configure(mode="indeterminate")
        self.eval_progress.start(12)
        self.status_var.set("Looking for files to evaluate…")
        self._update_action_buttons()
        self.worker = threading.Thread(
            target=self._run_evaluate,
            args=(resolved, mode, wait_library),
            daemon=True,
        )
        self.worker.start()

    def cancel(self) -> None:
        if not self.busy:
            return
        self.cancel_event.set()
        self.ui_queue.put(("status", "Cancel requested…"))

    def _run_evaluate(self, resolved: ResolvedCmd, mode: str, wait_library: bool) -> None:
        finish = True
        try:
            files = self._scan_queued_files()
            if self.cancel_event.is_set():
                self.ui_queue.put(("status", "Cancelled."))
                return
            if not files:
                self.ui_queue.put(("no_files", None))
                finish = False
                return
            self.ui_queue.put(("files_ready", files))
            if self._software_update_available():
                self.update_prompt.clear()
                self.ui_queue.put(("ask_update_before_eval", None))
                while not self.update_prompt.wait(timeout=0.2):
                    if self.cancel_event.is_set():
                        self.ui_queue.put(("status", "Cancelled."))
                        return
                if self.update_prompt_yes:
                    if not self._install_update(stop_if_running=True):
                        self.ui_queue.put(("status", "Update did not finish; evaluation continues."))
                    resolved = resolve_photostructure() or resolved
            if wait_library:
                wait = self._wait_for_library(resolved)
                if wait == "failed":
                    self.ui_queue.put(("ask_failed", True))
                    finish = False
                    return
                if wait is not True:
                    return
            else:
                self._ensure_boot_started()
                boot = self._boot_thread
                if boot is not None:
                    while boot.is_alive():
                        if self.cancel_event.is_set():
                            self.ui_queue.put(("status", "Cancelled."))
                            return
                        boot.join(0.2)
                self._queue_excluded_warning()
            if self.cancel_event.is_set():
                self.ui_queue.put(("status", "Cancelled."))
                return
            self.ui_queue.put(("status", f"Hashing {len(files)} file(s)…"))
            self._run_hash_check(resolved, files, mode)
        except Exception as exc:
            self.ui_queue.put(("status", f"Evaluation failed: {exc}"))
        finally:
            if finish:
                self.ui_queue.put(("done", None))

    def _wait_for_library(self, resolved: ResolvedCmd) -> Any:
        url = base_url()
        self._ensure_boot_started()
        boot = self._boot_thread
        if boot is not None:
            while boot.is_alive():
                if self.cancel_event.is_set():
                    self.ui_queue.put(("status", "Cancelled."))
                    return False
                boot.join(0.2)
        started_here = self._boot_started_ps
        if not http_ok(url):
            self.ui_queue.put(("status", "Starting PhotoStructure…"))
            if not self._start_photostructure_process():
                return False
            started_here = True
            if not self._wait_until_healthy(self.cancel_event):
                self.ui_queue.put(("status", "Cancelled."))
                return False
        if self.cancel_event.is_set():
            self.ui_queue.put(("status", "Cancelled."))
            return False
        self.ui_queue.put(("status", "PhotoStructure is syncing the library…"))
        if not self._post_library_sync():
            return False
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or read_http_port()
        sse = ProgressSse(host, port)
        seen_nonempty = False
        ignored_first_empty = False
        seen_processing = False
        connected_at: Optional[float] = None
        sync_requested_at = time.monotonic()
        last_names: list[str] = []
        try:
            while not self.cancel_event.is_set():
                sse.pump()
                if sse.connected and connected_at is None:
                    connected_at = time.monotonic()
                snapshot = sse.last_states
                if snapshot is not None:
                    names = [s.lower() for s in snapshot]
                    if started_here and not names and not seen_nonempty and not ignored_first_empty:
                        ignored_first_empty = True
                        sse.last_states = None
                        time.sleep(0.2)
                        continue
                    last_names = names
                    if names:
                        seen_nonempty = True
                else:
                    names = last_names
                processing = any(s == "processing" for s in names)
                paused = any(s == "paused" for s in names)
                unfinished = [s for s in names if s not in {"done", ""}]
                if processing:
                    seen_processing = True
                if paused and not processing:
                    self.ui_queue.put(("status", "PhotoStructure sync is paused"))
                    time.sleep(0.3)
                    continue
                if processing:
                    self.ui_queue.put(("status", "PhotoStructure is syncing the library…"))
                    time.sleep(0.3)
                    continue
                waiting_for_sync_to_start = (
                    not seen_processing and time.monotonic() - sync_requested_at < 20
                )
                idle_ready = False
                if waiting_for_sync_to_start:
                    idle_ready = False
                elif seen_nonempty and not processing and not paused:
                    only_failed = unfinished and all(s == "failed" for s in unfinished)
                    if only_failed:
                        return "failed"
                    idle_ready = True
                elif snapshot is None and connected_at is not None and not processing and not paused:
                    idle_ready = True
                if idle_ready:
                    self.ui_queue.put(("status", "Library sync finished"))
                    self._queue_excluded_warning()
                    return True
                time.sleep(0.2)
        finally:
            sse.close()
        self.ui_queue.put(("status", "Cancelled."))
        return False

    def _list_dump_for_groups(
        self, resolved: ResolvedCmd, groups: list[tuple[str, str]]
    ) -> tuple[int, list[dict[str, Any]], str, str, str]:
        dump_objs: list[dict[str, Any]] = []
        list_outs: list[str] = []
        list_errs: list[str] = []
        list_displays: list[str] = []
        list_code = 0
        list_prefix = resolved.argv_prefix + ["list", "--dump", "--where"]
        where_batches: list[list[str]] = []
        current_groups: list[str] = []
        for _path, group in groups:
            trial = " OR ".join(current_groups + [group])
            if current_groups and argv_char_len(list_prefix + [trial]) > CMD_CHAR_LIMIT:
                where_batches.append(current_groups)
                current_groups = [group]
            else:
                current_groups.append(group)
        if current_groups:
            where_batches.append(current_groups)
        for batch in where_batches:
            if self.cancel_event.is_set():
                return 1, dump_objs, "\n".join(list_outs), "Cancelled", "\n\n".join(list_displays)
            where = " OR ".join(batch)
            list_argv = list_prefix + [where]
            list_displays.append(subprocess.list2cmdline(list_argv))
            code, out, err = self._run_cmd(list_argv)
            list_outs.append(out)
            list_errs.append(err)
            if code != 0:
                list_code = code
            for item in parse_json_stream(out):
                if isinstance(item, dict):
                    dump_objs.append(item)
                elif isinstance(item, list):
                    dump_objs.extend(x for x in item if isinstance(x, dict))
        return (
            list_code,
            dump_objs,
            "\n".join(list_outs),
            "\n".join(list_errs),
            "\n\n".join(list_displays),
        )

    def _run_hash_check(self, resolved: ResolvedCmd, files: list[str], mode: str) -> None:
        info_prefix = resolved.argv_prefix + [
            "info",
            "--filter", "pHash",
            "--filter", "rHash",
            "--filter", "wHash",
            "--filter", "pHash0",
            "--filter", "rHash0",
            "--filter", "wHash0",
            "--no-color",
        ]
        t0 = time.perf_counter()
        info_batches = batch_paths(info_prefix, files, max_items=INFO_BATCH_FILES)
        total = len(files)
        work: queue.Queue[Optional[dict[str, Any]]] = queue.Queue(maxsize=1)
        progress = {"hashing": (0, 0), "matching": (0, 0), "done": 0}
        lock = threading.Lock()

        def publish_progress() -> None:
            with lock:
                hashing = progress["hashing"]
                matching = progress["matching"]
                done = progress["done"]
            parts: list[str] = []
            if matching[1]:
                parts.append(f"matching {matching[0]}–{matching[1]}")
            if hashing[1]:
                parts.append(f"hashing {hashing[0]}–{hashing[1]}")
            if not parts:
                parts.append(f"evaluated {done}")
            self._queue_eval_progress(done, total, f"{'; '.join(parts)} of {total}…")

        def hasher() -> None:
            hashed = 0
            try:
                for batch in info_batches:
                    if self.cancel_event.is_set():
                        break
                    lo = hashed + 1
                    hi = hashed + len(batch)
                    with lock:
                        progress["hashing"] = (lo, hi)
                    publish_progress()
                    info_code, out, err = self._run_cmd(info_prefix + batch)
                    records_by_path: dict[str, dict[str, Any]] = {}
                    for rec in flatten_info_records(parse_json_stream(out)):
                        path = record_path(rec)
                        if path:
                            records_by_path[normalize_path(path)] = rec
                    file_hashes: dict[str, dict[str, Optional[str]]] = {}
                    groups: list[tuple[str, str]] = []
                    info_cmd = subprocess.list2cmdline(info_prefix + ["<paths>"])
                    for path in batch:
                        rec = records_by_path.get(normalize_path(path))
                        hashes = extract_hash_fields(rec) if rec else {}
                        file_hashes[path] = hashes
                        if rec is None or not all(is_valid_hash(hashes.get(k)) for k in HASH_KEYS):
                            self.ui_queue.put(
                                (
                                    "result",
                                    FileResult(
                                        path=path,
                                        status=STATUS_ERROR,
                                        exit_code=info_code if rec is None else 1,
                                        elapsed=time.perf_counter() - t0,
                                        hashes=hashes,
                                        info_stdout=json.dumps(rec, indent=2) if rec else out,
                                        info_stderr=err,
                                        command=info_cmd,
                                        error="Missing or invalid pHash, rHash, or wHash",
                                    ),
                                )
                            )
                            continue
                        group = where_group(mode, hashes)
                        if group is None:
                            self.ui_queue.put(
                                (
                                    "result",
                                    FileResult(
                                        path=path,
                                        status=STATUS_ERROR,
                                        exit_code=1,
                                        elapsed=time.perf_counter() - t0,
                                        hashes=hashes,
                                        info_stdout=json.dumps(rec, indent=2),
                                        info_stderr=err,
                                        command=info_cmd,
                                        error="Could not build a WHERE group for this file",
                                    ),
                                )
                            )
                            continue
                        groups.append((path, group))
                    item = {
                        "lo": lo,
                        "hi": hi,
                        "size": len(batch),
                        "groups": groups,
                        "file_hashes": file_hashes,
                        "records_by_path": records_by_path,
                        "info_code": info_code,
                        "info_err": err,
                    }
                    while True:
                        if self.cancel_event.is_set():
                            return
                        try:
                            work.put(item, timeout=0.2)
                            break
                        except queue.Full:
                            continue
                    hashed += len(batch)
            finally:
                with lock:
                    progress["hashing"] = (0, 0)
                try:
                    work.put(None, timeout=30)
                except queue.Full:
                    pass

        def matcher() -> None:
            while True:
                try:
                    item = work.get(timeout=0.2)
                except queue.Empty:
                    if self.cancel_event.is_set():
                        return
                    continue
                if item is None:
                    return
                lo = int(item["lo"])
                hi = int(item["hi"])
                groups: list[tuple[str, str]] = item["groups"]
                with lock:
                    progress["matching"] = (lo, hi)
                publish_progress()
                dump_objs: list[dict[str, Any]] = []
                list_out = ""
                list_err = ""
                list_display = ""
                list_code = 0
                if groups:
                    if self.cancel_event.is_set():
                        return
                    list_code, dump_objs, list_out, list_err, list_display = self._list_dump_for_groups(
                        resolved, groups
                    )
                elapsed = time.perf_counter() - t0
                file_hashes = item["file_hashes"]
                records_by_path = item["records_by_path"]
                info_code = item["info_code"]
                info_err = item["info_err"]
                for path, _group in groups:
                    hashes = file_hashes[path]
                    rec = records_by_path.get(normalize_path(path))
                    matches: list[str] = []
                    listed_self = False
                    for obj in dump_objs:
                        if not dump_matches_mode(obj, mode, hashes):
                            continue
                        dump_path = record_path(obj)
                        if not dump_path:
                            continue
                        if same_file(path, dump_path):
                            listed_self = True
                            continue
                        if dump_path not in matches:
                            matches.append(dump_path)
                    ok = list_code == 0 and info_code == 0
                    if not ok:
                        status = STATUS_ERROR
                        error = "info or list exited with an error"
                    elif matches:
                        status = STATUS_MATCH
                        error = ""
                    else:
                        status = STATUS_NO_MATCH
                        error = ""
                    self.ui_queue.put(
                        (
                            "result",
                            FileResult(
                                path=path,
                                status=status,
                                exit_code=list_code if ok else (info_code or list_code),
                                elapsed=elapsed,
                                matches=matches,
                                hashes=hashes,
                                info_stdout=json.dumps(rec, indent=2) if rec else "",
                                info_stderr=info_err,
                                list_stdout=list_out,
                                list_stderr=list_err,
                                command=list_display,
                                error=error,
                                listed_self=listed_self,
                            ),
                        )
                    )
                with lock:
                    progress["done"] += int(item["size"])
                    progress["matching"] = (0, 0)
                publish_progress()

        hash_thread = threading.Thread(target=hasher, daemon=True)
        match_thread = threading.Thread(target=matcher, daemon=True)
        hash_thread.start()
        match_thread.start()
        hash_thread.join()
        match_thread.join()
        if self.cancel_event.is_set():
            self.ui_queue.put(("status", "Cancelled."))

    def _run_cmd(self, argv: list[str]) -> tuple[int, str, str]:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_kwargs(),
            )
        except OSError as exc:
            return 1, "", str(exc)
        out_box: list[str] = []
        err_box: list[str] = []

        def _read(stream: Any, dest: list[str]) -> None:
            dest.append(stream.read() or "")

        t_out = threading.Thread(target=_read, args=(proc.stdout, out_box), daemon=True)
        t_err = threading.Thread(target=_read, args=(proc.stderr, err_box), daemon=True)
        t_out.start()
        t_err.start()
        while proc.poll() is None:
            if self.cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                t_out.join(timeout=2)
                t_err.join(timeout=2)
                return 1, "".join(out_box), "Cancelled"
            time.sleep(0.1)
        t_out.join()
        t_err.join()
        return proc.returncode or 0, "".join(out_box), "".join(err_box)

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "status":
                    self.log_status(str(payload))
                elif kind == "result":
                    result: FileResult = payload
                    self.results[result.path] = result
                    self.refresh_table()
                    self._update_summary()
                    if self.selected_path == result.path:
                        self.show_detail(result.path)
                elif kind == "ask_failed":
                    if messagebox.askyesno(
                        "Library errors",
                        "PhotoStructure reported failed library work. Ask anyway?",
                    ):
                        resolved = resolve_photostructure()
                        files = list(self.order)
                        mode = self.mode_var.get()
                        if resolved and files:
                            self.busy = True
                            self.cancel_event.clear()
                            self.worker = threading.Thread(
                                target=self._run_hash_only,
                                args=(resolved, files, mode),
                                daemon=True,
                            )
                            self.worker.start()
                    else:
                        self.busy = False
                        self.log_status("Evaluation skipped because library work failed.")
                elif kind == "progress":
                    self._apply_eval_progress(payload)
                elif kind == "preview":
                    self._apply_preview(payload)
                elif kind == "files_ready":
                    files = list(payload)
                    self.results = {p: FileResult(path=p) for p in files}
                    self.order = files
                    self._reset_eval_progress(len(files))
                    self.refresh_table()
                    self._update_summary()
                elif kind == "no_files":
                    self.busy = False
                    self.eval_progress.stop()
                    self.eval_progress.configure(mode="determinate", value=0)
                    self._update_action_buttons()
                    messagebox.showinfo("Ask PhotoStructure", "No files were found in the selected roots.")
                elif kind == "done":
                    self.busy = False
                    self.log_status("Evaluation complete.")
                    self._apply_eval_progress(
                        {"value": 1, "max": 1, "text": "Evaluation complete."}
                    )
                    self.refresh_table()
                    self._select_default_row()
                    self._update_action_buttons()
                elif kind == "ps_state":
                    running, available = payload
                    self.ps_running = bool(running)
                    self.update_available = bool(available)
                    self._update_action_buttons()
                elif kind == "ask_update_before_eval":
                    self.update_prompt_yes = bool(
                        messagebox.askyesno(
                            "Update PhotoStructure",
                            "A PhotoStructure update is available. Update before asking?",
                        )
                    )
                    self.update_prompt.set()
                elif kind == "open_browser":
                    webbrowser.open(base_url())
                elif kind == "exclude_warn":
                    self._show_excluded_warning(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def error_paths(self) -> list[str]:
        return [p for p in self.order if self.results.get(p) and self.results[p].status == STATUS_ERROR]

    def retry_errors(self) -> None:
        if self.busy:
            messagebox.showinfo("Retry", "A run is already in progress.")
            return
        paths = [p for p in self.error_paths() if os.path.exists(p)]
        missing = len(self.error_paths()) - len(paths)
        if not paths:
            messagebox.showinfo("Retry", "There are no remaining error files to retry.")
            return
        resolved = resolve_photostructure()
        if resolved is None:
            self.log_status("PhotoStructure was not found. Use Update.")
            return
        self.cancel_event.clear()
        self.busy = True
        self._reset_eval_progress(len(paths))
        for path in paths:
            self.results[path] = FileResult(path=path)
        self.refresh_table()
        self._update_summary()
        self._update_action_buttons()
        if missing:
            self.log_status(f"Skipping {missing} missing error file(s).")
        self.log_status(f"Retrying {len(paths)} file(s) with errors…")
        self.worker = threading.Thread(
            target=self._run_hash_only,
            args=(resolved, paths, self.mode_var.get()),
            daemon=True,
        )
        self.worker.start()

    def _run_hash_only(self, resolved: ResolvedCmd, files: list[str], mode: str) -> None:
        try:
            self.ui_queue.put(("status", f"Hashing {len(files)} file(s)…"))
            self._run_hash_check(resolved, files, mode)
        except Exception as exc:
            self.ui_queue.put(("status", f"Evaluation failed: {exc}"))
        finally:
            self.ui_queue.put(("done", None))

    def _visible_paths(self) -> list[str]:
        filt = self.filter_var.get()
        paths: list[str] = []
        for path in self.order:
            result = self.results[path]
            if filt == FILTER_MATCHES and result.status != STATUS_MATCH:
                continue
            if filt == FILTER_NONE and result.status != STATUS_NO_MATCH:
                continue
            if filt == FILTER_ERRORS and result.status != STATUS_ERROR:
                continue
            paths.append(path)
        return self._sorted_paths(paths)

    def _sort_key(self, path: str) -> Any:
        result = self.results[path]
        if self.sort_column == "status":
            return result.status.lower()
        if self.sort_column == "name":
            return result.name.lower()
        if self.sort_column == "matches":
            if result.status == STATUS_PENDING:
                return -1
            return len(result.matches)
        if self.sort_column == "folder":
            return result.parent.lower()
        return result.name.lower()

    def _sorted_paths(self, paths: list[str]) -> list[str]:
        return sorted(paths, key=self._sort_key, reverse=self.sort_reverse)

    def sort_by(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        self.refresh_table()

    def refresh_table(self) -> None:
        previously = set(self.selected_paths())
        self.tree.delete(*self.tree.get_children())
        self.row_ids = {}
        for index, path in enumerate(self._visible_paths()):
            result = self.results[path]
            iid = f"row-{index}"
            self.row_ids[iid] = path
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    result.status,
                    link_label(result.name),
                    "—" if result.status == STATUS_PENDING else len(result.matches),
                    link_label(result.parent),
                ),
            )
        to_select = [iid for iid, path in self.row_ids.items() if path in previously]
        if to_select:
            self.tree.selection_set(to_select)
            self.tree.see(to_select[0])
            self.selected_path = self.row_ids[to_select[0]]
        self._update_action_buttons()

    def _select_default_row(self) -> None:
        preferred: Optional[str] = None
        for path in self._visible_paths():
            status = self.results[path].status
            if status in {STATUS_MATCH, STATUS_ERROR}:
                preferred = path
                break
        if preferred is None:
            visible = self._visible_paths()
            preferred = visible[0] if visible else None
        if preferred is None:
            return
        for iid, path in self.row_ids.items():
            if path == preferred:
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.see(iid)
                self.selected_path = path
                self.show_detail(path)
                break

    def _update_summary(self) -> None:
        total = len(self.order)
        matches = sum(1 for p in self.order if self.results[p].status == STATUS_MATCH)
        none = sum(1 for p in self.order if self.results[p].status == STATUS_NO_MATCH)
        errors = sum(1 for p in self.order if self.results[p].status == STATUS_ERROR)
        self.summary_var.set(f"{total} files · {matches} with a match · {none} with no match · {errors} error")
        self._update_action_buttons()

    def selected_paths(self) -> list[str]:
        return [self.row_ids[iid] for iid in self.tree.selection() if iid in self.row_ids]

    def on_select_row(self, _event: Any = None) -> None:
        paths = self.selected_paths()
        if not paths:
            self._update_action_buttons()
            return
        focus = self.tree.focus()
        path = self.row_ids.get(focus) if focus in self.row_ids else None
        if path not in paths:
            path = paths[0]
        self.selected_path = path
        self.show_detail(path)
        self._update_action_buttons()

    def show_detail(self, path: str) -> None:
        self._update_preview(path)
        result = self.results.get(path)
        self.detail.delete("1.0", "end")
        self.match_tree.delete(*self.match_tree.get_children())
        self.match_paths = {}
        if result is None:
            self._set_match_pane(False)
            return
        lines = [
            f"Path: {result.path}",
            f"Status: {result.status}",
            f"Exit code: {result.exit_code}",
            f"Elapsed: {result.elapsed:.2f}s" if result.elapsed is not None else "Elapsed:",
            "",
            "Hashes:",
        ]
        for key in HASH_KEYS + HASH0_KEYS:
            lines.append(f"  {key}: {result.hashes.get(key) or ''}")
        lines.append("")
        if result.matches:
            lines.append("Match paths:")
            for index, m in enumerate(result.matches):
                lines.append(f"  {m}")
                iid = f"match-{index}"
                self.match_paths[iid] = m
                self.match_tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(link_label(os.path.basename(m)), link_label(os.path.dirname(m))),
                )
        elif result.status == STATUS_NO_MATCH:
            lines.append("no library match")
        elif result.status == STATUS_DELETED:
            lines.append("This file was sent to the Recycle Bin.")
        if result.error:
            lines.append("")
            lines.append(f"Error: {result.error}")
        lines.extend(
            [
                "",
                "Shared list command:",
                result.command,
                "",
                "info result:",
                result.info_stdout or "(empty)",
                "",
                "info stderr:",
                result.info_stderr or "(empty)",
                "",
                "list stdout:",
                result.list_stdout or "(empty)",
                "",
                "list stderr:",
                result.list_stderr or "(empty)",
            ]
        )
        self.detail.insert("1.0", "\n".join(lines))
        self._set_match_pane(bool(result.matches))

    def _on_preview_toggle(self) -> None:
        if self.preview_var.get():
            if not self.preview_frame.winfo_ismapped():
                self.preview_frame.pack(fill="x", before=self._detail_scroll, pady=(0, 6))
            self._update_preview(self.selected_path)
        else:
            self._preview_seq += 1
            self._preview_photo = None
            self.preview_label.configure(image="", text="")
            self.preview_frame.pack_forget()

    def _on_preview_configure(self, _event: Any = None) -> None:
        if not self.preview_var.get() or not self._preview_path:
            return
        if self._preview_resize_job is not None:
            self.root.after_cancel(self._preview_resize_job)
        self._preview_resize_job = self.root.after(150, lambda: self._update_preview(self._preview_path))

    def _update_preview(self, path: Optional[str]) -> None:
        if not self.preview_var.get():
            return
        if not self.preview_frame.winfo_ismapped():
            self.preview_frame.pack(fill="x", before=self._detail_scroll, pady=(0, 6))
        width = max(self.preview_frame.winfo_width(), 320)
        height = max(self.preview_frame.winfo_height(), 200)
        if (
            path
            and path == self._preview_path
            and (width, height) == self._preview_size
            and self._preview_photo is not None
        ):
            return
        self._preview_seq += 1
        seq = self._preview_seq
        self._preview_path = path
        self._preview_size = (width, height)
        if not path:
            self._preview_photo = None
            self.preview_label.configure(image="", text="No file selected")
            return
        self.preview_label.configure(image="", text="Loading preview…")
        threading.Thread(
            target=self._decode_preview,
            args=(seq, path, width, height),
            daemon=True,
        ).start()

    def _decode_preview(self, seq: int, path: str, width: int, height: int) -> None:
        try:
            from PIL import Image, ImageOps

            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                image = image.convert("RGB")
                image.thumbnail((max(width - 8, 32), max(height - 8, 32)), Image.Resampling.LANCZOS)
                payload = (seq, path, image.copy(), "")
        except Exception as exc:
            payload = (seq, path, None, str(exc))
        self.ui_queue.put(("preview", payload))

    def _apply_preview(self, payload: tuple[Any, ...]) -> None:
        seq, path, image, error = payload
        if seq != self._preview_seq or path != self._preview_path or not self.preview_var.get():
            return
        if image is None:
            self._preview_photo = None
            self.preview_label.configure(image="", text=error or "Cannot preview this file")
            return
        try:
            from PIL import ImageTk
        except ImportError:
            self._preview_photo = None
            self.preview_label.configure(image="", text="Pillow is not installed")
            return
        photo = ImageTk.PhotoImage(image)
        self._preview_photo = photo
        self.preview_label.configure(image=photo, text="")

    def _set_match_pane(self, show: bool) -> None:
        panes = set(self.right_paned.panes())
        shown = str(self.match_frame) in panes
        if show and not shown:
            self.right_paned.add(self.match_frame, weight=2)
            self._match_pane_shown = True
        elif not show and shown:
            self.right_paned.forget(self.match_frame)
            self._match_pane_shown = False

    def current_result(self) -> Optional[FileResult]:
        if not self.selected_path:
            return None
        return self.results.get(self.selected_path)

    def _matched_existing(self, result: FileResult) -> bool:
        return result.status == STATUS_MATCH and bool(result.matches) and os.path.exists(result.path)

    def _update_action_buttons(self) -> None:
        any_matched = any(self._matched_existing(r) for r in self.results.values())
        set_enabled(self.bulk_btn, any_matched)
        selected_matched = any(
            self._matched_existing(self.results[p]) for p in self.selected_paths() if p in self.results
        )
        set_enabled(self.delete_selected_btn, selected_matched)
        set_enabled(self.retry_errors_btn, (not self.busy) and any(
            r.status == STATUS_ERROR for r in self.results.values()
        ))
        set_enabled(self.start_btn, True)
        set_enabled(self.stop_btn, self.ps_running)
        set_enabled(self.sync_btn, self.ps_running)
        set_enabled(self.update_btn, self.update_available)
        set_enabled(self.cancel_btn, self.busy)

    def _on_tree_shift_wheel(self, event: Any) -> str:
        widget = event.widget
        steps = int(-1 * (event.delta / 120)) if event.delta else 0
        if steps:
            widget.xview_scroll(steps, "units")
        return "break"

    def _link_cursor(self, tree: ttk.Treeview, event: Any, columns: set[str]) -> None:
        column = tree.identify_column(event.x)
        row = tree.identify_row(event.y)
        tree.configure(cursor="hand2" if row and column in columns else "")

    def _open_path(self, path: str) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("Open", str(exc))

    def on_result_click(self, event: Any) -> None:
        iid = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not iid:
            return
        path = self.row_ids.get(iid)
        if not path:
            return
        if column == "#2":
            self._open_path(path)
        elif column == "#4":
            self._reveal(path)

    def _roots_cursor(self, event: Any) -> None:
        index = self.roots_list.nearest(event.y)
        if 0 <= index < self.roots_list.size():
            self.roots_list.configure(cursor="hand2")
        else:
            self.roots_list.configure(cursor="")

    def on_root_activate(self, _event: Any = None) -> None:
        sel = self.roots_list.curselection()
        if not sel:
            return
        path = self.roots[sel[0]]
        if os.path.isdir(path):
            self._reveal(path)
        else:
            self._open_path(path)

    def on_match_click(self, event: Any) -> None:
        iid = self.match_tree.identify_row(event.y)
        column = self.match_tree.identify_column(event.x)
        if not iid:
            return
        path = self.match_paths.get(iid)
        if not path:
            return
        self.match_tree.selection_set(iid)
        if column == "#2":
            self._reveal(path)
            return
        self._open_path(path)

    def _reveal(self, path: str) -> None:
        native = os.path.normpath(path)
        if ctypes is not None:
            params = f'/select,"{native}"'
            rc = ctypes.windll.shell32.ShellExecuteW(None, "open", "explorer.exe", params, None, 1)
            if rc > 32:
                return
        try:
            subprocess.Popen(["explorer.exe", "/select,", native], **popen_kwargs())
        except OSError as exc:
            messagebox.showerror("Explorer", str(exc))

    def _listed_self(self, result: FileResult) -> bool:
        return result.listed_self

    def delete_selected_matched(self) -> None:
        paths = [
            p
            for p in self.selected_paths()
            if p in self.results and self._matched_existing(self.results[p])
        ]
        if not paths:
            return
        listed = [p for p in paths if self._listed_self(self.results[p])]
        extra = ""
        if listed:
            extra = (
                f"\n\n{len(listed)} of these paths were also printed by list, so they are library copies."
            )
        if not messagebox.askyesno(
            "Delete selected matched files",
            f"Send {len(paths)} evaluated file(s) to the Recycle Bin?{extra}",
        ):
            return
        self._delete_paths(paths)

    def delete_all_matched(self) -> None:
        paths = [
            r.path
            for r in self.results.values()
            if r.status == STATUS_MATCH and r.matches and os.path.exists(r.path)
        ]
        if not paths:
            return
        listed = [p for p in paths if self._listed_self(self.results[p])]
        extra = ""
        if listed:
            extra = f"\n\n{len(listed)} of these paths were also printed by list, so they are library copies."
        if not messagebox.askyesno(
            "Delete all matched files",
            f"Send {len(paths)} evaluated files to the Recycle Bin?{extra}",
        ):
            return
        self._delete_paths(paths)

    def _delete_paths(self, paths: list[str]) -> None:
        ok, err = recycle_files(paths)
        if not ok:
            messagebox.showerror("Delete", err or "Delete failed.")
            return
        for path in paths:
            result = self.results.get(path)
            if result is None:
                continue
            result.status = STATUS_DELETED
            result.matches = []
            result.exists = False
        self.refresh_table()
        self._update_summary()
        if self.selected_path:
            self.show_detail(self.selected_path)

    def _poll_photostructure_state(self) -> None:
        threading.Thread(target=self._refresh_ps_state, daemon=True).start()
        self.root.after(4000, self._poll_photostructure_state)

    def _refresh_ps_state(self) -> None:
        running = http_ok(base_url())
        self.ui_queue.put(("ps_state", (running, stable_update_available())))
        self._queue_excluded_warning()

    def _software_update_available(self) -> bool:
        return stable_update_available(force=True)

    def start_and_open(self) -> None:
        def work() -> None:
            if not http_ok(base_url()):
                argv = start_argv()
                if argv is None:
                    self.ui_queue.put(("status", "PhotoStructure was not found. Use Update."))
                    return
                self.ui_queue.put(("status", "Starting PhotoStructure…"))
                try:
                    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen_kwargs())
                except OSError as exc:
                    self.ui_queue.put(("status", f"Could not start PhotoStructure: {exc}"))
                    return
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if http_ok(base_url()):
                        break
                    time.sleep(0.4)
            self.ui_queue.put(("open_browser", None))

        threading.Thread(target=work, daemon=True).start()

    def _shutdown_photostructure(self) -> bool:
        url = base_url()
        try:
            http_post(url + "admin/shutdown", timeout=10)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.ui_queue.put(("status", f"Shutdown request failed: {exc}"))
            return False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if not http_ok(url, timeout=1.5):
                self.ui_queue.put(("status", "PhotoStructure has stopped."))
                return True
            time.sleep(0.5)
        self.ui_queue.put(("status", "PhotoStructure is still answering on the port."))
        return False

    def _invalidate_boot(self) -> None:
        self._boot_ready.clear()
        self._boot_started_ps = False
        self._exclude_scanned = False

    def _queue_excluded_warning(self) -> None:
        try:
            items = latest_excluded_items()
        except Exception:
            items = []
        self._exclude_scanned = True
        self.ui_queue.put(("exclude_warn", items))

    def _resize_exclude_label(self, event: Any) -> None:
        width = max(event.width - 24, 200)
        self.exclude_label.configure(wraplength=width)

    def _open_activity(self, _event: Any = None) -> None:
        webbrowser.open(activity_url())

    def _show_excluded_warning(self, items: list[tuple[str, str, bool]]) -> None:
        if not items:
            if self._exclude_text:
                self.exclude_frame.pack_forget()
                self._exclude_text = ""
            return
        n_files = sum(1 for _path, _reason, is_dir in items if not is_dir)
        n_dirs = sum(1 for _path, _reason, is_dir in items if is_dir)
        parts: list[str] = []
        if n_files:
            parts.append(f"{n_files} file" + ("s" if n_files != 1 else ""))
        if n_dirs:
            parts.append(f"{n_dirs} folder" + ("s" if n_dirs != 1 else ""))
        what = " and ".join(parts) if parts else "items"
        url = activity_url()
        text = (
            f"PhotoStructure skipped or failed {what} during library sync. "
            f"Open System Activity ({url}) for details."
        )
        if text == self._exclude_text:
            return
        self._exclude_text = text
        self.exclude_label.configure(text=text)
        self.exclude_frame.pack(fill="x", padx=8, pady=(8, 0), after=self.ps_frame)
        self.log_status(f"PhotoStructure skipped or failed {what}. Open {url} for details.")

    def stop_photostructure(self) -> None:
        def work() -> None:
            self._invalidate_boot()
            self._shutdown_photostructure()

        threading.Thread(target=work, daemon=True).start()

    def _post_library_sync(self) -> bool:
        url = base_url() + "admin/sync/force-restart"
        deadline = time.monotonic() + 90
        last_status: Optional[int] = None
        while time.monotonic() < deadline:
            if self.cancel_event.is_set():
                self.ui_queue.put(("status", "Cancelled."))
                return False
            try:
                status, _body = http_post(url, timeout=15)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_status = None
                self.ui_queue.put(("status", "Waiting for PhotoStructure before syncing…"))
                time.sleep(0.8)
                continue
            last_status = status
            if 200 <= status < 300:
                self.ui_queue.put(("status", "PhotoStructure is syncing all scanned folders."))
                return True
            if status in {502, 503, 504}:
                self.ui_queue.put(("status", "PhotoStructure is starting; waiting to sync…"))
                time.sleep(0.8)
                continue
            self.ui_queue.put(("status", f"Sync request returned HTTP {status}."))
            return False
        if last_status is None:
            self.ui_queue.put(("status", "Sync request failed: PhotoStructure did not become ready."))
        else:
            self.ui_queue.put(("status", f"Sync request failed: HTTP {last_status}"))
        return False

    def sync_photostructure(self) -> None:
        if not http_ok(base_url()):
            self.log_status("PhotoStructure is not running. Use Start and open, then Sync.")
            return

        def work() -> None:
            if self._post_library_sync():
                self._queue_excluded_warning()

        threading.Thread(target=work, daemon=True).start()

    def _install_update(self, stop_if_running: bool) -> bool:
        npx = shutil.which("npx.cmd") or shutil.which("npx")
        if not npx:
            self.ui_queue.put(("status", "npx.cmd was not found."))
            return False
        if stop_if_running and http_ok(base_url()):
            if not self._shutdown_photostructure():
                self.ui_queue.put(("status", "Update cancelled because PhotoStructure is still running."))
                return False
        argv = [npx, "--yes", "--prefer-online", "photostructure@stable"]
        self.ui_queue.put(("status", "Updating PhotoStructure…"))
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_kwargs(),
            )
        except OSError as exc:
            self.ui_queue.put(("status", f"Update failed: {exc}"))
            return False
        assert proc.stdout is not None
        for line in proc.stdout:
            if self.cancel_event.is_set():
                proc.terminate()
                self.ui_queue.put(("status", "Update cancelled."))
                return False
            self.ui_queue.put(("status", line.rstrip()))
        code = proc.wait()
        self.ui_queue.put(("status", f"Update finished with exit {code}"))
        return code == 0

    def update_photostructure(self) -> None:
        if not self.update_available:
            return
        running = http_ok(base_url())
        if running:
            if not messagebox.askyesno(
                "Update PhotoStructure",
                "PhotoStructure is running. Stop it so the update can replace files it is using?",
            ):
                return
        elif not messagebox.askyesno("Update PhotoStructure", "Download and install photostructure@stable?"):
            return

        def work() -> None:
            self._install_update(stop_if_running=running)

        threading.Thread(target=work, daemon=True).start()


def main() -> None:
    root = tk.Tk()
    KeepOrDeleteApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
