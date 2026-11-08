"""A test model of OrcaSlicer's Python plugin audit hook.

OrcaSlicer 2.5 asks the user before a plugin does anything the host audits (opens
a file outside the data folder, opens a network connection, starts a thread...).
This module repeats that decision pipeline in Python so the tests can run real
Print Farm flows and list every call that would have shown a dialog. It follows
``src/slic3r/plugin/PluginAuditManager.cpp`` and ``PythonPluginBridge.cpp`` of
OrcaSlicer and the wiki page ``plugin_audit_hook.md``:

* event -> category table, ``open`` classified by its mode;
* targets (paths, ``str()`` of the address argument, a fixed ``thread`` target)
  and the empty target list of events that name nothing (``open`` of a file
  descriptor, ``socket.__new__``);
* denied filenames and path keywords (a refusal, never a dialog);
* the stdlib read exemption, the allowed roots (the data folder read/write) and
  the "operation root" frames that cover the events nested in a granted
  ``urlopen()``;
* granted targets: exact strings, ``<dir>/*`` trees for files, host matching for
  network grants (``scheme://host`` covers every port, path and raw socket of the
  host; a full URL grant covers that exact URL only);
* ``orca.request_permissions(...)`` declarations made while the plugin loads.

Plugin threads inherit the audit context in the host (a thread shim), so every
thread counts here too. Only events whose Python stack contains Print Farm code
are judged; emulators and the test runner are not part of the plugin.
"""

import os
import pathlib
import sys
import sysconfig
import threading

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent

FS_READ, FS_READWRITE, FS_CREATE, FS_DELETE = "FsRead", "FsReadWrite", "FsCreate", "FsDelete"
HTTP, SOCKET, PROCESS, PROCESS_REPLACE, THREADING = (
    "Http", "Socket", "ProcessCreate", "ProcessReplace", "Threading")
FS_CATEGORIES = (FS_READ, FS_READWRITE, FS_CREATE, FS_DELETE)
THREAD_TARGET = "thread"

_CATEGORIES = {
    FS_READ: ("glob.glob", "glob.glob/2", "os.fwalk", "os.getxattr", "os.listdir",
              "os.listdrives", "os.listmounts", "os.listvolumes", "os.listxattr",
              "os.scandir", "os.walk", "pathlib.Path.glob", "pathlib.Path.rglob"),
    FS_READWRITE: ("os.chflags", "os.chmod", "os.chown", "os.removexattr", "os.rename",
                   "os.setxattr", "os.truncate", "os.utime", "shutil.chown",
                   "shutil.copymode", "shutil.copystat", "shutil.copyfile",
                   "shutil.copytree", "shutil.make_archive", "shutil.move",
                   "shutil.unpack_archive"),
    FS_CREATE: ("os.link", "os.mkdir", "os.symlink", "tempfile.mkdtemp",
                "tempfile.mkstemp", "_winapi.CreateJunction"),
    FS_DELETE: ("os.remove", "os.rmdir", "shutil.rmtree"),
    HTTP: ("http.client.connect", "http.client.send", "urllib.Request"),
    SOCKET: ("socket.__new__", "socket.bind", "socket.connect", "socket.getaddrinfo",
             "socket.gethostbyaddr", "socket.gethostbyname", "socket.gethostname",
             "socket.getnameinfo", "socket.getservbyname", "socket.getservbyport",
             "socket.sendmsg", "socket.sendto"),
    PROCESS: ("os.fork", "os.forkpty", "os.posix_spawn", "os.spawn", "os.system",
              "os.startfile", "os.startfile/2", "pty.spawn", "subprocess.Popen",
              "_winapi.CreateProcess", "_posixsubprocess.fork_exec"),
    THREADING: ("_thread.start_new_thread",),
    PROCESS_REPLACE: ("os.exec",),
}
CATEGORY_OF = {event: category for category, events in _CATEGORIES.items() for event in events}

OPERATION_ROOT_EVENTS = frozenset((
    "urllib.Request", "http.client.connect", "os.walk", "os.fwalk", "glob.glob",
    "glob.glob/2", "pathlib.Path.glob", "pathlib.Path.rglob", "subprocess.Popen",
    "pty.spawn", "tempfile.mkstemp", "tempfile.mkdtemp", "shutil.copyfile",
    "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.chown",
    "shutil.move", "shutil.rmtree", "shutil.make_archive", "shutil.unpack_archive",
))
TWO_PATH_FS_EVENTS = frozenset((
    "os.rename", "os.link", "os.symlink", "shutil.copyfile", "shutil.copytree",
    "shutil.copymode", "shutil.copystat", "shutil.move", "shutil.unpack_archive",
    "_winapi.CreateJunction",
))
TARGET_ARG_INDICES = {
    "http.client.connect": (1,), "urllib.Request": (0,),
    "socket.connect": (1,), "socket.bind": (1,), "socket.getaddrinfo": (0,),
    "socket.gethostbyname": (0,), "socket.gethostbyaddr": (0,), "socket.getnameinfo": (0,),
    "socket.getservbyname": (0,), "socket.getservbyport": (0,), "socket.sendto": (1,),
    "socket.sendmsg": (1,),
    "os.system": (0,), "subprocess.Popen": (1, 0), "os.posix_spawn": (1, 0),
    "os.spawn": (2, 1), "os.startfile": (0,), "os.startfile/2": (0,), "pty.spawn": (0,),
    "_winapi.CreateProcess": (1, 0), "_posixsubprocess.fork_exec": (0,), "os.exec": (0,),
}
DENIED_KEYWORDS = frozenset(("secret", "secrets", "cert", "certs", "certificate",
                             "certificates", "conf", "config"))
DENIED_FILENAME_PREFIXES = ("orcaslicer.conf", "orcaslicergcodeviewer.conf",
                            "orcaslicer.ini", "orcaslicergcodeviewer.ini",
                            ".install_state.json")

_BUSY = threading.local()


# --------------------------------------------------------------------------- #
# Network identity (PluginAuditManager::normalize_network_url and friends)
# --------------------------------------------------------------------------- #
def _without_brackets(host):
    return host[1:-1] if len(host) >= 2 and host[0] == "[" and host[-1] == "]" else host


def _host_from_authority(authority):
    authority = authority.rsplit("@", 1)[-1]
    if not authority:
        return ""
    if authority[0] == "[":
        close = authority.find("]")
        return authority[:close + 1].lower() if close >= 0 else ""
    authority = authority.split(":", 1)[0]
    if any(char in authority for char in " \t\r\n/?#@"):
        return ""
    return authority.lower()


def normalize_network_url(url):
    """``scheme://host`` of an http(s) URL, or "" when it has none."""
    scheme_end = url.find("://")
    if scheme_end <= 0:
        return ""
    scheme = url[:scheme_end].lower()
    if scheme not in ("http", "https"):
        return ""
    rest = url[scheme_end + 3:]
    cut = min([i for i in (rest.find(c) for c in "/?#") if i >= 0] or [len(rest)])
    host = _host_from_authority(rest[:cut])
    return scheme + "://" + host if host else ""


def _network_host_from_target(target):
    value = target.strip()
    if not value:
        return ""
    if value[0] == "(":
        quote = next((i for i, char in enumerate(value) if char in "'\""), -1)
        if quote >= 0:
            end = value.find(value[quote], quote + 1)
            if end < 0:
                return ""
            value = value[quote + 1:end]
        else:
            value = value[1:].split(",", 1)[0]
    elif value[0] == "[":
        close = value.find("]")
        if close < 0:
            return ""
        value = value[:close + 1]
    else:
        colon = value.find(":")
        if colon >= 0 and value.find(":", colon + 1) < 0:
            value = value[:colon]
    value = value.strip()
    if not value or any(char in value for char in " \t\r\n/?#@"):
        return ""
    return value.lower()


def network_permission_matches(granted, target):
    if not target:
        return False
    target_is_url = "://" in target
    target_url = normalize_network_url(target)
    if target_is_url and not target_url:
        return target in granted
    target_host = "" if target_is_url else _without_brackets(_network_host_from_target(target))
    if not target_is_url and not target_host:
        return False
    for entry in granted:
        if entry == target:
            return True
        entry_url = normalize_network_url(entry)
        if target_is_url:
            if entry_url and entry_url == entry and entry_url == target_url:
                return True
            continue
        if entry_url:
            if entry.lower() == entry_url:
                if _without_brackets(entry_url.split("://", 1)[1]) == target_host:
                    return True
        elif _without_brackets(_network_host_from_target(entry)) == target_host:
            return True
    return False


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _inside(candidate, root):
    candidate, root = _canonical(candidate), _canonical(root)
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:
        return False


def _tree_root(grant):
    if len(grant) < 2 or grant[-1] != "*" or grant.find("*") != len(grant) - 1:
        return None
    parent, name = os.path.split(grant)
    if name != "*" or not parent or ".." in pathlib.PurePath(parent).parts:
        return None
    return parent


def _fs_grant_covers(grant, target):
    if grant == target:
        return True
    root = _tree_root(grant)
    return bool(target) and root is not None and _inside(target, root)


def _has_permission(category, by_event, granted, target):
    tree_ok = not by_event and category in (FS_READ, FS_READWRITE)
    return any(grant == target or (tree_ok and _fs_grant_covers(grant, target))
               for grant in granted)


def _fspath(value):
    try:
        path = os.fspath(value)
    except TypeError:
        return ""
    return path.decode("utf-8", "replace") if isinstance(path, bytes) else path


class Prompt:
    """One dialog the host would have shown."""

    def __init__(self, event, category, targets, by_event, thread, site, chain):
        self.event, self.category = event, category
        self.targets, self.by_event = list(targets), by_event
        self.thread, self.site, self.chain = thread, site, chain

    @property
    def key(self):
        return ("%s:%s" % (self.category, self.event)) if self.by_event else self.targets[0]

    def describe(self):
        shown = "<no target>" if self.by_event else ", ".join(self.targets)
        return "%s %s [%s] at %s (thread %s)" % (
            self.category, self.event, shown, self.site, self.thread)

    __repr__ = describe


class AuditSimulator:
    """Judges audited events like the host does and records every dialog."""

    def __init__(self, data_dir, *, persist=True, plugin_files=None, extra_roots=None):
        self.data_dir = str(data_dir)
        # The installed package lives below the data folder in the host; the source
        # tree the tests import it from stands in for that location.
        self.roots = [self.data_dir] + [str(r) for r in (
            [PLUGIN_DIR] if extra_roots is None else extra_roots)]
        self.persist = persist
        self.block_pending_network = False
        self.grants = {"fs_read": [], "fs_readwrite": [], "network_http": [],
                       "network_socket": [], "process": [], "threading": [], "events": []}
        self.prompts = []
        self.denied = []
        self.declaration_dialogs = []
        self.active = 0
        self._local = threading.local()
        self._lock = threading.Lock()
        paths = sysconfig.get_paths()
        self._stdlib = [_canonical(paths[k]) for k in ("stdlib", "platstdlib") if paths.get(k)]
        self._site = [_canonical(paths[k]) for k in ("purelib", "platlib") if paths.get(k)]
        self._verdicts = {}
        self._plugin_files = plugin_files

    # -- plugin load: orca.request_permissions ----------------------------
    def request_permissions(self, fs_read=(), fs_readwrite=(), network_http=(),
                            network_socket=(), process=(), threading=False, network=()):
        """The declaration call, validated like the binding in PythonPluginBridge.cpp."""
        for name, targets in (("fs_read", fs_read), ("fs_readwrite", fs_readwrite),
                              ("network_http", network_http),
                              ("network_socket", network_socket), ("process", process)):
            if any(not target for target in targets):
                raise ValueError("request_permissions(%s=...) does not accept empty values" % name)
        normalized = []
        for url in network:
            origin = normalize_network_url(url)
            if not origin:
                raise ValueError("request_permissions(network=...) requires an http:// "
                                 "or https:// URL with a host")
            if origin not in normalized:
                normalized.append(origin)
        pending = getattr(self, "_pending", None)
        if pending is None:
            raise ValueError("request_permissions() called outside plugin discovery context")
        for bucket, targets in (("fs_read", fs_read), ("fs_readwrite", fs_readwrite),
                                ("network_http", list(network_http) + normalized),
                                ("network_socket", network_socket), ("process", process)):
            for target in targets:
                if target not in pending[bucket]:
                    pending[bucket].append(target)
        if threading and THREAD_TARGET not in pending["threading"]:
            pending["threading"].append(THREAD_TARGET)

    def load(self, register):
        """Run ``register`` as the host runs register_capabilities(), then answer
        the one combined dialog with Yes. Returns the targets the dialog listed."""
        self._pending = {"fs_read": [], "fs_readwrite": [], "network_http": [],
                         "network_socket": [], "process": [], "threading": []}
        try:
            register()
            pending, self._pending = self._pending, None
        finally:
            self._pending = None
        shown = {}
        for bucket, targets in pending.items():
            missing = []
            for target in targets:
                if bucket in ("network_http", "network_socket"):
                    covered = (network_permission_matches(self.grants["network_http"], target)
                               or network_permission_matches(self.grants["network_socket"], target))
                else:
                    covered = _has_permission(
                        FS_READ if bucket == "fs_read" else FS_READWRITE if bucket == "fs_readwrite"
                        else PROCESS, False, self.grants[bucket], target)
                if not covered and target not in missing:
                    missing.append(target)
            if missing:
                shown[bucket] = missing
        if shown:
            self.declaration_dialogs.append(shown)
            for bucket, targets in shown.items():
                self.grants[bucket].extend(t for t in targets if t not in self.grants[bucket])
        return shown

    # -- context -----------------------------------------------------------
    def audited(self):
        return _Audited(self)

    # -- event pipeline ----------------------------------------------------
    def _is_stdlib_file(self, filename):
        if filename.startswith("<frozen "):
            return True
        if not filename or filename[0] == "<":
            return False
        verdict = self._verdicts.get(filename)
        if verdict is None:
            verdict = (any(_inside(filename, d) for d in self._stdlib)
                       and not any(_inside(filename, d) for d in self._site))
            self._verdicts[filename] = verdict
        return verdict

    def _is_plugin_file(self, filename):
        if self._plugin_files is not None:
            return filename in self._plugin_files
        if not filename or filename[0] == "<":
            return False
        directory, name = os.path.split(filename)
        return (name.endswith(".py") and not name.startswith("test_")
                and name != "orca_audit_sim.py"
                and os.path.normcase(os.path.abspath(directory)) == os.path.normcase(str(PLUGIN_DIR)))

    def _is_denied(self, path):
        name = os.path.basename(path).lower()
        if name and any(name.startswith(prefix) for prefix in DENIED_FILENAME_PREFIXES):
            return True
        canonical = _canonical(path)
        root = _canonical(self.data_dir)
        try:
            inside = os.path.commonpath([canonical, root]) == root
        except ValueError:
            inside = False
        parts = (pathlib.PurePath(os.path.relpath(canonical, root)).parts if inside
                 else pathlib.PurePath(canonical).parts)
        for part in parts:
            lowered = part.lower().split(":", 1)[0]
            if lowered in DENIED_KEYWORDS or lowered.endswith((".conf", ".ini")):
                return True
        return False

    def _path_allowed(self, path, is_write):
        if not path:
            return True
        if not is_write and self._is_stdlib_path(path):
            return True
        return any(_inside(path, root) for root in self.roots)

    def _is_stdlib_path(self, path):
        return (any(_inside(path, d) for d in self._stdlib)
                and not any(_inside(path, d) for d in self._site))

    def _targets(self, event, category, args):
        if category in FS_CATEGORIES:
            count = 2 if event in TWO_PATH_FS_EVENTS else 1
            targets = []
            for index in range(count):
                path = _fspath(args[index]) if index < len(args) else ""
                if path:
                    targets.append(path)
            return targets
        if category == THREADING:
            return [THREAD_TARGET]
        for index in TARGET_ARG_INDICES.get(event, ()):
            item = args[index] if index < len(args) else None
            if item is None:
                continue
            try:
                value = str(item)
            except Exception:
                value = ""
            if value:
                return [value]
        return []

    def _permission_list(self, category, by_event):
        if by_event:
            return self.grants["events"]
        return {FS_READ: self.grants["fs_read"], FS_READWRITE: self.grants["fs_readwrite"],
                HTTP: self.grants["network_http"], SOCKET: self.grants["network_socket"],
                PROCESS: self.grants["process"], PROCESS_REPLACE: self.grants["process"],
                THREADING: self.grants["threading"]}.get(category)

    def _network_granted(self, target):
        return (network_permission_matches(self.grants["network_http"], target)
                or network_permission_matches(self.grants["network_socket"], target))

    def _frames(self, frame):
        frames = []
        while frame is not None:
            frames.append(frame)
            frame = frame.f_back
        return frames

    def _root_frames(self):
        frames = getattr(self._local, "roots", None)
        if frames is None:
            frames = self._local.roots = []
        return frames

    def on_event(self, event, args, frame):
        if not self.active or getattr(_BUSY, "on", False):
            return
        category = CATEGORY_OF.get(event)
        if event == "open":
            mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
            category = FS_READ if mode and not any(c in mode for c in "wax+") else FS_READWRITE
        if category is None:
            return
        frames = self._frames(frame)
        if not any(self._is_plugin_file(f.f_code.co_filename) for f in frames):
            return
        _BUSY.on = True
        try:
            self._judge(event, category, args, frames)
        finally:
            _BUSY.on = False

    def _judge(self, event, category, args, frames):
        targets = self._targets(event, category, args)
        fs = category in FS_CATEGORIES
        if fs and any(self._is_denied(t) for t in targets):
            self.denied.append((event, targets))
            raise PermissionError("denied path: %s" % targets)
        roots = self._root_frames()
        # Root frames whose operation has returned are let go (suspended generators stay).
        stack = {id(f) for f in frames}
        roots[:] = [f for f in roots if id(f) in stack or (f.f_code.co_flags & 0x20)]
        if event != "urllib.Request" and roots:
            for f in frames:
                if any(f is r for r in roots):
                    return
                if not self._is_stdlib_file(f.f_code.co_filename):
                    break
        if fs and targets and all(
                self._path_allowed(t, category != FS_READ) for t in targets):
            return
        by_event = not targets
        keys = ["%s:%s" % (category, event)] if by_event else targets
        granted_list = self._permission_list(category, by_event)
        unresolved = list(keys)
        if granted_list is not None or category in (HTTP, SOCKET):
            unresolved = []
            for key in keys:
                network_ok = category in (HTTP, SOCKET) and (
                    bool(self.grants["network_http"] or self.grants["network_socket"])
                    if by_event else self._network_granted(key))
                listed = granted_list is not None and _has_permission(
                    category, by_event, granted_list, key)
                if not listed and not network_ok:
                    unresolved.append(key)
        if unresolved:
            plugin_frames = [f for f in frames if self._is_plugin_file(f.f_code.co_filename)]
            chain = ["%s:%d %s" % (os.path.basename(f.f_code.co_filename), f.f_lineno,
                                   f.f_code.co_name) for f in plugin_frames[:5]]
            self.prompts.append(Prompt(
                event, category, unresolved if not by_event else [], by_event,
                threading.current_thread().name, chain[0] if chain else "?", chain))
            if self.block_pending_network and category in (HTTP, SOCKET):
                raise PermissionError('Plugin attempted audited operation "%s" without permission' % event)
            if self.persist and granted_list is not None:
                for key in unresolved:
                    if key not in granted_list:
                        granted_list.append(key)
        if targets and event in OPERATION_ROOT_EVENTS:
            top = frames[0]
            if self._is_stdlib_file(top.f_code.co_filename) and not any(
                    top is r for r in roots):
                roots.append(top)

    # -- reporting ---------------------------------------------------------
    def report(self):
        lines = []
        for prompt in self.prompts:
            lines.append(prompt.describe() + "\n      via " + " <- ".join(prompt.chain))
        return "\n".join(lines)

    def reset(self):
        del self.prompts[:]
        del self.denied[:]


class _Audited:
    def __init__(self, simulator):
        self.simulator = simulator

    def __enter__(self):
        _install()
        _CURRENT.append(self.simulator)
        self.simulator.active += 1
        self.simulator._root_frames()[:] = []
        return self.simulator

    def __exit__(self, *_exc):
        self.simulator.active -= 1
        _CURRENT.remove(self.simulator)
        self.simulator._root_frames()[:] = []
        return False


_CURRENT = []
_INSTALLED = False


def _dispatch(event, args):
    # sys._getframe() is itself audited, so the guard goes up before it is called.
    if not _CURRENT or getattr(_BUSY, "on", False):
        return
    _BUSY.on = True
    try:
        frame = sys._getframe(1)
    finally:
        _BUSY.on = False
    for simulator in list(_CURRENT):
        simulator.on_event(event, args, frame)


def _install():
    global _INSTALLED
    if not _INSTALLED:
        sys.addaudithook(_dispatch)
        if sys.version_info >= (3, 13):
            # CPython 3.12, which OrcaSlicer embeds, raises "_thread.start_new_thread" for
            # every Thread.start(); 3.13 no longer does. Raise it here so the model
            # sees the same thread events on any interpreter.
            real_start = threading.Thread.start

            def start(thread):
                sys.audit("_thread.start_new_thread", thread.run, (), None)
                return real_start(thread)

            threading.Thread.start = start
        _INSTALLED = True
