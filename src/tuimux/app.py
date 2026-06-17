#!/usr/bin/env python3
"""tuimux — a Textual dashboard for tmux sessions across your tailnet.

The heavy lifting (Tailscale discovery, tmux/ssh probing, terminal tab spawning,
keep-awake, claude/state detection) lives in the bundled `engine.sh`; this is
purely the front-end, calling it for data and actions.
"""

import os
import re
import subprocess
import time
from importlib.resources import files

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Header, Footer, DataTable, Label, Input, OptionList
from textual.widgets.option_list import Option

from .cli import tuimux_bin

ENGINE = str(files("tuimux").joinpath("engine.sh"))
# TUIMUX_BIN tells the engine its own absolute path, so sessions it spawns into
# new terminal tabs re-invoke tuimux even when it isn't on the tab's PATH
# (e.g. installed in a conda env / venv that isn't activated by default).
_ENV = {**os.environ, "TUIMUX_BIN": tuimux_bin()}
REFRESH = float(os.environ.get("TUIMUX_REFRESH", "3") or 3)
# Hold off the periodic refresh until you've stopped pressing keys for this long,
# so navigating the list stays fluid (no probe/render churn mid-keystroke). The
# refresh still runs on every tick once you're idle, and immediately on returning
# from a dialog.
QUIET = float(os.environ.get("TUIMUX_QUIET", "1.2") or 1.2)
AWAKE = "keep-awake"
SHELLS = {
    "zsh",
    "bash",
    "sh",
    "fish",
    "-zsh",
    "-bash",
    "dash",
    "ksh",
    "tcsh",
    "login",
    "",
}

LOCAL = "#34d8b1"  # teal   (this machine)
REMOTE = "#7aa2f7"  # blue   (other machines)
CYAN = "#56cfe1"  # folders
VIOLET = "#b08cff"  # agent
AMBER = "#e0af68"  # awake / waiting / no-ssh
GREEN = "#9ece6a"  # active session / running / working

# Per-session STATE word → colour (weight stays plain; colour carries meaning).
_STATE_STYLE = {"waiting": AMBER, "working": GREEN, "running": GREEN, "idle": "dim"}

# Stable per-machine accent: this machine is always teal; each remote hashes its
# name into this palette. engine.sh `host_color` mirrors this exactly (same
# palette + hash) so a session's tmux status bar matches its colour here.
HOST_PALETTE = (
    "#7aa2f7",
    "#b08cff",
    "#9ece6a",
    "#56cfe1",
    "#f7768e",
    "#ff9e64",
    "#c678dd",
)


def _host_color(host, is_local):
    if is_local:
        return LOCAL
    h = 5381  # djb2 — spreads similar names across the palette better than a sum
    for b in host.encode():
        h = (h * 33 + b) & 0xFFFFFFFF
    return HOST_PALETTE[h % len(HOST_PALETTE)]


# A probe that can't finish in this many seconds is treated as unreachable, so
# one slow/contended host can't stall (or back up) the whole refresh.
PROBE_TIMEOUT = float(os.environ.get("TUIMUX_PROBE_TIMEOUT", "10") or 10)
# Mutating actions (detach/close/rename/awake) run off the UI thread; cap them
# too so a slow host can't leave a worker hanging forever.
ACTION_TIMEOUT = float(os.environ.get("TUIMUX_ACTION_TIMEOUT", "15") or 15)
# Host discovery (tailscale status) — bounded so a wedged tailscaled can't hang
# the host-scan worker indefinitely.
HOSTS_TIMEOUT = float(os.environ.get("TUIMUX_HOSTS_TIMEOUT", "8") or 8)
# Terminal window/tab scan (AppleScript) used to show where a session is open.
WINDOWS_TIMEOUT = float(os.environ.get("TUIMUX_WINDOWS_TIMEOUT", "5") or 5)


def _run(args, timeout=None):
    try:
        return subprocess.run(
            ["bash", ENGINE, *args],
            capture_output=True,
            text=True,
            env=_ENV,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # Distinct marker so callers can tell "too slow to answer" apart from a
        # genuine SSH failure (engine prints UNREACHABLE for the latter).
        return subprocess.CompletedProcess(args, 124, "__TIMEOUT__\n", "")


def fetch_hosts():
    res = []
    for ln in _run(["__hosts"], timeout=HOSTS_TIMEOUT).stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        name = parts[0]
        is_local = parts[1].strip() == "1"
        status = parts[2].strip() if len(parts) > 2 else "online"
        lastseen = parts[3].strip() if len(parts) > 3 else ""
        res.append((name, is_local, status, lastseen))
    return res


def _abbrev(path):
    return re.sub(r"^/home/[^/]+", "~", re.sub(r"^/Users/[^/]+", "~", path or ""))


def _uptime(created):
    try:
        secs = int(time.time()) - int(created)
    except (TypeError, ValueError):
        return ""
    secs = max(secs, 0)
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _title_parts(title):
    """Split a window/tab title into comparable components.

    Ghostty titles are a single string ("<session> · <window>"); Terminal.app
    decorates them as "<login> — <session> · <window> — <process> — <WxH>".
    Splitting on the em-dash yields one component per terminal that we can match
    exactly, so Terminal's decorations don't defeat the lookup."""
    return [p.strip() for p in title.split("—")]


def _map_term(t):
    tl = (t or "").lower()
    for k in ("ghostty", "kitty", "alacritty", "wezterm"):
        if k in tl:
            return k
    if tl.startswith(("screen", "tmux")):
        return "tmux"
    if tl.startswith(("xterm", "vt")) or tl == "linux":
        return "term"
    return t or "-"


def _auto_name(name, dird, is_agent, cmd):
    if not name.isdigit():  # docker/manual name → keep it
        return name or "?"
    if dird not in ("~", "", "/"):  # bare tmux number in a real folder → folder
        return os.path.basename(dird)
    if is_agent:
        return "claude"
    if cmd and cmd not in SHELLS:
        return cmd
    return name


def probe(host):
    info = {
        "reachable": False,
        "busy": False,
        "notmux": False,
        "awake": False,
        "sessions": [],
    }
    lines = _run(["__probe", host], timeout=PROBE_TIMEOUT).stdout.splitlines()
    if lines and lines[0].strip() == "__TIMEOUT__":
        # SSH is fine, the host is just too loaded to answer within PROBE_TIMEOUT.
        info["busy"] = True
        return info
    if not lines or lines[0].strip() != "OK":
        return info
    info["reachable"] = True
    S, W, L, A, C = {}, {}, {}, {}, set()
    for ln in lines[1:]:
        if ln == "__NOTMUX__":
            info["notmux"] = True
            continue
        if "|" not in ln:
            continue
        p = ln.split("|")
        t = p[0]
        try:
            if t == "S":
                S[p[1]] = {
                    "attached": p[2] == "1",
                    "windows": int(p[3] or 0),
                    "dir": p[4] if len(p) > 4 else "",
                    "cmd": p[5] if len(p) > 5 else "",
                    "created": p[6] if len(p) > 6 else "",
                }
            elif t == "W":
                W.setdefault(p[1], []).append((p[2], p[3], p[4] == "1"))
            elif t == "L":
                L[p[1]] = p[2]
            elif t == "A":
                A[p[1]] = p[2]
            elif t == "C":
                C.add(p[1])
        except IndexError:
            continue
    info["awake"] = AWAKE in S
    for name, s in S.items():
        if name == AWAKE:
            continue
        is_agent = name in C
        dird = _abbrev(s["dir"]) or "~"
        wins = W.get(name, [])
        count = len(wins) or s["windows"] or 1
        active = next(
            (w[1] for w in wins if w[2]), wins[0][1] if wins else (s["cmd"] or "?")
        )
        if is_agent and re.match(r"^[0-9][0-9.]*$", active or ""):
            active = "claude"
        client = L.get(name)
        state = (
            (A.get(name) or "running")
            if is_agent
            else ("idle" if s["cmd"] in SHELLS else "running")
        )
        info["sessions"].append(
            {
                "name": name,
                "auto": _auto_name(name, dird, is_agent, s["cmd"]),
                "attached": s["attached"],
                "dir": dird,
                "tabs": f"{count}  {active}",
                "open_in": _map_term(client)
                if (s["attached"] and client)
                else "detached",
                "state": state,
                "uptime": _uptime(s["created"]),
                "agent": is_agent,
            }
        )
    info["sessions"].sort(key=lambda x: x["auto"].lower())
    return info


def _probe_or_offline(h):
    name, _is_local, status, lastseen = h
    if status == "offline":
        # Tailscale already reports it down — skip the SSH probe (and its timeout).
        info = {
            "reachable": False,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [],
            "lastseen": lastseen,
        }
        return (h, info)
    return (h, probe(name))


# Table columns in display order. A "cell" is a tuple of (text, style) segments
# so one cell can mix styles (e.g. a dim marker + a bold host name); an empty
# tuple renders blank. _row() builds a (cells, meta) pair, filling only the
# columns you name and leaving the rest empty — keeps _view readable.
_COLS = ("name", "status", "state", "uptime", "folder", "tabs", "open_in", "agent")


def _row(meta, **cells):
    return (tuple(cells.get(c, ()) for c in _COLS), meta)


class Confirm(ModalScreen[bool]):
    BINDINGS = [("escape", "no"), ("n", "no"), ("y", "yes")]

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message, id="dialog-msg")
            yield OptionList(
                Option("↩   cancel", id="cancel"),
                Option("✕   yes, shut it down", id="ok"),
                id="menu-list",
            )

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, e: OptionList.OptionSelected) -> None:
        self.dismiss(e.option.id == "ok")

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class Ask(ModalScreen[str]):
    BINDINGS = [("escape", "cancel")]

    def __init__(self, prompt, default=""):
        super().__init__()
        self.prompt = prompt
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt, id="dialog-msg")
            yield Input(value=self.default, id="dialog-input")

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, e: Input.Submitted) -> None:
        self.dismiss(e.value.strip())

    def action_cancel(self):
        self.dismiss("")


class Menu(ModalScreen[str]):
    BINDINGS = [("escape", "cancel")]

    def __init__(self, meta):
        super().__init__()
        self.meta = meta

    def compose(self) -> ComposeResult:
        attach = self.meta["action"] == "attach"
        title = self.meta["session"] if attach else "new session"
        if attach:
            opened = self.meta.get("open")
            opts = [
                ("↵  go to its tab" if opened else "↵  open in a new tab", "open"),
                ("⊕  open in a new window", "window"),
                ("✎  rename", "rename"),
            ]
            if opened:  # only meaningful when it's actually open
                opts.append(("⏏  detach (close its tab, keep running)", "detach"))
            opts.append(("✕  close (kill session)", "close"))
        else:
            opts = [
                ("↵  new tab", "open"),
                ("⊕  new window", "window"),
                ("✎  name & create", "rename"),
            ]
        with Vertical(id="dialog"):
            yield Label(title, id="dialog-msg")
            yield OptionList(*[Option(t, id=i) for t, i in opts], id="menu-list")

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, e: OptionList.OptionSelected) -> None:
        self.dismiss(e.option.id)

    def action_cancel(self):
        self.dismiss(None)


class SessionTable(DataTable):
    # DataTable already binds enter→select_cursor (hidden) which posts RowSelected;
    # we just make it visible on the footer as "open tab". Doing it here — on the
    # focused widget — instead of an app-level priority binding is crucial: a
    # priority binding fires app-wide and would hijack Enter inside the modal
    # menus (selecting a menu option, submitting a rename), breaking them.
    # `right` (drill into the row → actions menu) is also bound here because the
    # table's own column-nav binding would otherwise shadow an app-level one.
    BINDINGS = [
        Binding("enter", "select_cursor", "open tab", key_display="enter"),
        Binding("right", "app.menu", "menu", show=False),  # app.* → App.action_menu
    ]


class Tuimux(App):
    COMMAND_PALETTE_BINDING = "p"  # open the command palette with p (not ctrl+p)
    CSS = """
    Screen { background: $surface; }
    #table-wrap {
        border: round $primary 40%;
        border-title-color: $primary;
        border-title-style: bold;
        border-title-align: center;
        padding: 1 2;
        margin: 1 2;
        height: 1fr;
    }
    DataTable { height: 1fr; background: $surface; }
    DataTable > .datatable--cursor { background: $primary 20%; color: $text; }
    DataTable > .datatable--header {
        color: $text-muted; text-style: bold; background: $surface;
    }
    #dialog {
        width: 56; height: auto; padding: 1 2; margin: 1;
        background: $panel; border: round $primary;
    }
    #dialog-msg { width: 1fr; padding: 0 0 1 0; content-align: center middle; }
    #dialog-input { margin-top: 1; }
    #menu-list {
        height: auto; max-height: 14; border: none;
        background: $panel; padding: 0;
    }
    #menu-list > .option-list--option { padding: 0 1; }
    #menu-list > .option-list--option-highlighted { background: $primary 25%; }
    ModalScreen { align: center middle; background: $background 70%; }
    """

    # Footer order: enter (shown by SessionTable) space w n d x t c r a q
    BINDINGS = [
        Binding("space", "menu", "menu"),
        Binding("w", "window", "new window"),
        Binding("n", "rename", "rename"),
        Binding("d", "detach", "detach"),
        Binding("x", "close", "close"),
        Binding("t", "tmux", "tmux tree"),
        Binding("c", "console", "tailscale"),
        Binding("r", "reload", "refresh"),
        Binding("a", "awake", "keep-awake"),
        Binding("q", "quit", "quit"),
        # hidden alias: m also opens the menu (right is bound on SessionTable)
        Binding("m", "menu", "menu", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.row_meta = []
        # [(name, is_local, status, lastseen)] from the last host scan
        self._hosts = []
        # host -> probe info, filled in incrementally as probes land
        self._results = {}
        # hosts with a probe currently in flight (avoid double-probing)
        self._probing = set()
        self._table = None  # cached DataTable handle (avoids repeated DOM queries)
        # last paint, for cheap diffing: overall signature, per-row keys, cells
        self._last_sig = None
        self._last_keys = []
        self._last_cells = []
        # terminal layout: [(win_index, tab_title)] and the window holding us
        self._windows = []
        self._self_win = None
        self._last_input = 0.0  # monotonic time of the last keypress (idle debounce)
        self._last_refresh = 0.0  # monotonic time of the last actual refresh

    def on_key(self, event):
        # Record activity so the periodic refresh can hold off while you navigate.
        # We don't consume the event — bindings and cursor movement still happen.
        self._last_input = time.monotonic()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="table-wrap") as w:
            w.border_title = "✦ tuimux"
            yield SessionTable(cursor_type="row", zebra_stripes=False)
        yield Footer()

    def on_mount(self):
        self.title = "tuimux"
        self.sub_title = os.environ.get("TERM_PROGRAM", "terminal").lower()
        self._load_subtitle()  # fill in the login name off the UI thread
        for theme in ("tokyo-night", "catppuccin-mocha", "nord"):
            try:
                self.theme = theme
                break
            except Exception:
                continue
        t = self._table = self.query_one(SessionTable)
        for col, key, w in (
            ("NAME", "name", 22),
            ("STATUS", "status", 9),
            ("STATE", "state", 8),
            ("UPTIME", "uptime", 7),
            ("FOLDER", "folder", 20),
            ("TABS", "tabs", 12),
            ("OPEN IN", "open", 13),
            ("AGENT", "agent", 10),
        ):
            t.add_column(col, key=key, width=w)
        t.focus()
        self.reload()
        # Heartbeat faster than REFRESH so we notice you've gone quiet promptly;
        # reload() itself enforces the REFRESH cadence and the idle debounce.
        self.set_interval(min(1.0, REFRESH), self.reload)

    @work(thread=True)
    def _load_subtitle(self):
        login = _run(["__login"], timeout=HOSTS_TIMEOUT).stdout.strip()
        login = login or os.environ.get("USER", "")
        term = os.environ.get("TERM_PROGRAM", "terminal").lower()
        self.call_from_thread(
            setattr, self, "sub_title", f"{login} · {term}" if login else term
        )

    # ---- data ----
    # Refresh is incremental and per-host. We fetch the host list, paint every
    # machine immediately (from cached results, or a "checking…" placeholder),
    # then probe each host in its OWN background worker and re-render as each
    # result lands. A slow host (e.g. one pinned at high load) only delays its
    # own row — fast hosts and the whole UI never wait on it. A host already
    # being probed is never re-probed, so refreshes can't pile up.
    def _modal_open(self):
        # A dialog (Menu/Confirm/Ask) is on top. Pause background refresh while
        # it is, so probe/scan subprocesses and table repaints don't compete with
        # the dialog for the main loop — that churn is what makes menus feel laggy.
        return len(self.screen_stack) > 1

    def reload(self):
        # Called on every heartbeat. Refresh only when it makes sense:
        #   • not before the table exists, and never while a dialog is up;
        #   • not while you're actively pressing keys (defer < QUIET since the
        #     last keystroke) — keeps navigation fluid;
        #   • no more often than REFRESH, so an idle dashboard still updates
        #     on its normal cadence rather than every heartbeat.
        if self._table is None or self._modal_open():
            return
        now = time.monotonic()
        if now - self._last_input < QUIET or now - self._last_refresh < REFRESH:
            return
        self._last_refresh = now
        self._load_hosts()

    @work(exclusive=True, thread=True, group="hosts")
    def _load_hosts(self):
        hosts = fetch_hosts()
        self.call_from_thread(self._kick, hosts)

    def _kick(self, hosts):
        self._hosts = hosts
        # Tell the engine which host is us, so per-host probes skip the tailscale
        # lookups self_host would otherwise run on every call.
        for name, is_local, _status, _seen in hosts:
            if is_local:
                _ENV["TUIMUX_SELF_HOST"] = name
                break
        self._render()  # show machines right away, before any probe returns
        for h in hosts:
            name = h[0]
            if name not in self._probing:
                self._probing.add(name)
                self._probe_one(h)
        self._scan_windows()  # refresh where sessions are open (local, off-thread)

    @work(thread=True, group="probe")
    def _probe_one(self, h):
        _host, info = _probe_or_offline(h)
        self.call_from_thread(self._got, h[0], info)

    def _got(self, name, info):
        self._results[name] = info
        self._probing.discard(name)
        self._render()

    @work(exclusive=True, thread=True, group="windows")
    def _scan_windows(self):
        # Read the terminal's window/tab layout so sessions can show where they're
        # open. Local + read-only (no focus stealing); falls back to nothing.
        out = _run(["__windows"], timeout=WINDOWS_TIMEOUT).stdout
        wins, self_win = [], None
        for ln in out.splitlines():
            idx, sep, title = ln.partition("|")
            if not sep:
                continue
            idx, title = idx.strip(), title.strip()
            wins.append((idx, title))
            # the dashboard's own window — its title is "tuimux" (one of the
            # em-dash components on Terminal.app, the whole title on Ghostty)
            if any(p.lower() == "tuimux" for p in _title_parts(title)):
                self_win = idx
        self.call_from_thread(self._set_windows, wins, self_win)

    def _set_windows(self, wins, self_win):
        self._windows = wins
        self._self_win = self_win
        self._render()

    def _window_label(self, session_name):
        """Where this session's Ghostty tab lives, or None if not found locally.

        tuimux-opened surfaces are titled "<session> · <window>" (tmux set-titles),
        so we match the part before " · " against the session name — per title
        component, so Terminal.app's "<login> — …" decorations don't get in the way."""
        for idx, title in self._windows:
            if any(p.split(" · ", 1)[0].strip() == session_name
                   for p in _title_parts(title)):
                if self._self_win is not None and idx == self._self_win:
                    return "this window"
                return "other window"
        return None

    def _open_in_cell(self, s):
        """OPEN IN segment: which window the session is open in, else the
        attaching terminal type, else detached."""
        if s["open_in"] == "detached":
            return ("detached", "dim")
        loc = self._window_label(s["name"])
        if loc:
            return (loc, LOCAL if loc == "this window" else REMOTE)
        return (s["open_in"], REMOTE)  # attached, but tab not found locally

    # Build the table as plain data — a list of (cells, meta) rows — so we can
    # diff cheaply and only repaint when something actually changed.
    #
    # Emphasis convention (keep consistent when editing styles here):
    #   bold  → identifiers only: an online machine's NAME + STATUS word, and an
    #           attached session's NAME. Nothing else is ever bold.
    #   dim   → inactive/transient (offline & "checking…" machines, idle
    #           sessions), plain numeric metrics (uptime, tabs), and hints.
    #   plain → markers, STATE values, and colour-coded fields (folder, open-in,
    #           agent) — meaning carried by colour, not weight.
    def _view(self):
        rows = []
        for host, is_local, status, _lastseen in self._hosts:
            info = self._results.get(host)
            machine = {"host": host, "session": None, "action": "machine"}
            dead = {"host": host, "session": None, "action": "none"}
            if info is None:
                # not probed yet this session — neutral placeholder, no waiting
                rows.append(
                    _row(
                        machine,
                        name=(("● ", "dim"), (host, "dim")),
                        status=(("checking…", "dim italic"),),
                    )
                )
            elif not info["reachable"] and status == "offline":
                seen = info.get("lastseen", "")
                rows.append(
                    _row(
                        dead,
                        name=(("○ ", "dim"), (host, "dim")),
                        status=(("offline", "dim italic"),),
                        state=((seen, "dim"),) if seen else (),
                    )
                )
            elif not info["reachable"] and info.get("busy"):
                # SSH works, the host is just too loaded to answer in time.
                rows.append(
                    _row(
                        dead,
                        name=(("◐ ", AMBER), (host, f"bold {AMBER}")),
                        status=(("busy", f"bold {AMBER}"),),
                        folder=(("high load — slow to respond", "dim"),),
                    )
                )
            elif not info["reachable"]:
                # online on the tailnet, but tuimux can't log in — usually
                # Tailscale SSH isn't enabled/granted on that machine
                rows.append(
                    _row(
                        dead,
                        name=(("◐ ", AMBER), (host, f"bold {AMBER}")),
                        status=(("no ssh", f"bold {AMBER}"),),
                        folder=(("tailscale up --ssh", "dim"),),
                    )
                )
            else:
                # reachable — machine header, tinted with the machine's accent
                # (local = teal; each remote its own colour). STATUS word matches.
                color = _host_color(host, is_local)
                word = ("local" if is_local else "ssh", f"bold {color}")
                rows.append(
                    _row(
                        machine,
                        name=(("● ", color), (host, f"bold {color}")),
                        status=(word,),
                        state=(("☕ awake", AMBER),) if info["awake"] else (),
                    )
                )
                for s in info["sessions"]:  # its sessions, indented beneath it
                    rows.append(
                        _row(
                            {
                                "host": host,
                                "session": s["name"],
                                "action": "attach",
                                "open": s["open_in"] != "detached",
                            },
                            name=(
                                ("  ", ""),
                                (s["auto"], f"bold {GREEN}" if s["attached"] else ""),
                            ),
                            state=((s["state"], _STATE_STYLE.get(s["state"], "")),),
                            uptime=((s["uptime"], "dim"),),
                            folder=((s["dir"], CYAN),),
                            tabs=((s["tabs"], "dim"),),
                            open_in=(self._open_in_cell(s),),
                            agent=(("claude", VIOLET),) if s["agent"] else (),
                        )
                    )
                rows.append(
                    _row(
                        {"host": host, "session": "__NEW__", "action": "new"},
                        name=(("  ＋ new session", "dim italic"),),
                    )
                )
        return rows

    @staticmethod
    def _cell(segments):
        txt = Text()
        for text, style in segments:
            txt.append(text, style=style or None)
        return txt

    def _render(self):
        # Skip painting the (hidden) table while a dialog is up — keeps menus
        # responsive; the table refreshes when the dialog closes.
        if self._modal_open():
            return
        # Three tiers, cheapest first:
        #   1. nothing changed            → do nothing
        #   2. same rows, cells differ    → patch only the changed cells in place
        #      (no clear, no cursor reset, no flicker)
        #   3. rows added/removed/moved   → rebuild once inside a batch
        rows = self._view()
        cells_only = [cells for cells, _ in rows]
        sig = tuple(cells_only)
        if sig == self._last_sig:
            return
        keys = [(m["host"], m["session"]) for _, m in rows]
        t = self._table

        if keys == self._last_keys and t.row_count == len(rows):
            for r, cells in enumerate(cells_only):
                old = self._last_cells[r]
                for c, cell in enumerate(cells):
                    if cell != old[c]:
                        t.update_cell_at(Coordinate(r, c), self._cell(cell))
        else:
            prev = t.cursor_row
            sel = self._cur()
            sel_id = (sel["host"], sel["session"]) if sel else None
            with self.batch_update():
                t.clear()
                t.add_rows(tuple(self._cell(c) for c in cells) for cells in cells_only)
            if rows:
                # keep the cursor on the same logical row, not the same index —
                # rows shift as machines resolve and sessions come and go.
                idx = next((i for i, k in enumerate(keys) if k == sel_id), None)
                t.move_cursor(row=idx if idx is not None else min(prev, len(rows) - 1))

        self.row_meta = [meta for _, meta in rows]
        self._last_sig = sig
        self._last_keys = keys
        self._last_cells = cells_only

    def _cur(self):
        t = self._table
        if t is not None and 0 <= t.cursor_row < len(self.row_meta):
            return self.row_meta[t.cursor_row]
        return None

    def _spawn(self, args):
        subprocess.Popen(["bash", ENGINE, *args], env=_ENV)

    @work(thread=True)
    def _act(self, args):
        # Run a mutating engine command (detach/kill/rename/awake) off the UI
        # thread so a slow host can't freeze the TUI, then refresh once it's done.
        _run(args, timeout=ACTION_TIMEOUT)
        self.call_from_thread(self.reload)

    # ---- actions ----
    def on_data_table_row_selected(self, _):
        self.action_open()

    def action_menu(self):
        m = self._cur()
        if not m or m["action"] not in ("attach", "new"):
            return
        handlers = {
            "window": self.action_window,
            "rename": self.action_rename,
            "detach": self.action_detach,
            "close": self.action_close,
            "open": self.action_open,
        }

        def chosen(act):
            if act in handlers:
                handlers[act]()

        self.push_screen(Menu(m), chosen)

    def action_open(self):
        m = self._cur()
        if m and m["action"] in ("attach", "new"):
            self._spawn(["__open", "auto", m["host"], m["session"], m["action"]])

    def action_window(self):
        m = self._cur()
        if m and m["action"] in ("attach", "new"):
            self._spawn(["__open", "window", m["host"], m["session"], m["action"]])

    def action_detach(self):
        m = self._cur()
        if m and m["action"] == "attach":
            self._act(["__detach", m["host"], m["session"]])

    def action_close(self):
        m = self._cur()
        if not m or m["action"] != "attach":
            return

        def done(ok):
            if ok:
                self._act(["__killraw", m["host"], m["session"]])

        self.push_screen(
            Confirm(
                f"Shut down “{m['session']}” on {m['host']}?\nEverything running in it is killed."
            ),
            done,
        )

    def action_rename(self):
        m = self._cur()
        if not m or m["action"] not in ("attach", "new"):
            return
        if m["action"] == "new":

            def made(name):
                if name:
                    self._spawn(["__open", "tab", m["host"], name, "new"])

            self.push_screen(Ask("Name for the new session:"), made)
        else:

            def renamed(name):
                if name:
                    self._act(["__renameto", m["host"], m["session"], name])

            self.push_screen(Ask(f"Rename “{m['session']}” to:", m["session"]), renamed)

    def action_tmux(self):
        m = self._cur()
        if m and m["host"]:
            self._spawn(["__openbrowse", m["host"]])

    def action_console(self):
        # open the Tailscale admin console in the browser — no row needed
        self._spawn(["__console"])

    def action_awake(self):
        m = self._cur()
        if m and m["host"]:
            self._act(["__awaketoggle", m["host"]])


def run():
    """Entry point: refuse to nest inside tmux, then launch the dashboard."""
    if os.environ.get("TMUX"):
        raise SystemExit(
            "Don't run tuimux inside tmux — open it in a plain terminal tab."
        )
    Tuimux().run()


if __name__ == "__main__":
    run()
