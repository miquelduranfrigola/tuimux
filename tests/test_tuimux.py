"""Unit tests for tuimux's pure logic — parsing, naming, and table rendering.

No network, SSH, or running Textual app required: the engine call is stubbed and
the dashboard's view-model (`_view`) is exercised directly. Run with `pytest`, or
standalone with `python tests/test_tuimux.py`.
"""

import contextlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tuimux import app  # noqa: E402
from tuimux.cli import tuimux_bin  # noqa: E402


@contextlib.contextmanager
def stub_engine(stdout):
    """Make app._run return canned engine output instead of spawning bash."""
    orig = app._run
    app._run = lambda args, timeout=None: SimpleNamespace(stdout=stdout)
    try:
        yield
    finally:
        app._run = orig


def _cell_text(cell):
    return "".join(text for text, _ in cell)


def _cell_styles(cell):
    return " ".join(style for _, style in cell if style)


# ---- small pure helpers ------------------------------------------------------
def test_abbrev_collapses_home():
    assert app._abbrev("/Users/alice/code") == "~/code"
    assert app._abbrev("/home/bob/x") == "~/x"
    assert app._abbrev("/etc/hosts") == "/etc/hosts"
    assert app._abbrev("") == ""


def test_uptime_buckets():
    assert app._uptime(None) == ""
    assert app._uptime("not-a-number") == ""
    now = int(app.time.time())
    assert app._uptime(now - 120).endswith("m")
    assert app._uptime(now - 7200).endswith("h")
    assert app._uptime(now - 200000).endswith("d")


def test_map_term():
    assert app._map_term("xterm-ghostty") == "ghostty"
    assert app._map_term("xterm-kitty") == "kitty"
    assert app._map_term("screen.xterm") == "tmux"
    assert app._map_term("xterm-256color") == "term"
    assert app._map_term("") == "-"


def test_auto_name():
    # a bare tmux number in a real folder → the folder's basename
    assert app._auto_name("0", "~/code/ersilia", False, "zsh") == "ersilia"
    # numeric session running an agent → claude
    assert app._auto_name("3", "~", True, "node") == "claude"
    # numeric session running a non-shell command → that command
    assert app._auto_name("3", "~", False, "vim") == "vim"
    # an explicitly named session is kept as-is
    assert app._auto_name("build", "~", False, "zsh") == "build"


def test_tuimux_bin_is_absolute_or_name():
    got = tuimux_bin()
    assert got == "tuimux" or got.endswith("tuimux")


# ---- CLI: attach / detach (replaced the old `here`) -------------------------
def test_here_command_is_gone_and_usage_advertises_attach_detach():
    r = subprocess.run(["bash", app.ENGINE, "here"], capture_output=True, text=True)
    assert r.returncode == 1  # unknown command → usage + non-zero
    out = r.stdout + r.stderr
    assert "tuimux attach" in out and "tuimux detach" in out
    assert "tuimux here" not in out


def test_detach_outside_tmux_is_friendly_noop():
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    r = subprocess.run(
        ["bash", app.ENGINE, "detach"], capture_output=True, text=True, env=env
    )
    assert r.returncode == 0  # not an error — just nothing to do
    assert "not inside a tmux session" in (r.stdout + r.stderr).lower()


# ---- CLI: autostart (auto-attach every new local terminal) ------------------
def _autostart(action, rc):
    r = subprocess.run(
        ["bash", app.ENGINE, "autostart", action],
        capture_output=True,
        text=True,
        env={**os.environ, "TUIMUX_RC": rc},
    )
    return r.stdout + r.stderr  # `note` reports on stderr


def test_autostart_on_off_status_and_idempotency():
    with tempfile.TemporaryDirectory() as d:
        rc = os.path.join(d, "rc")
        with open(rc, "w") as f:
            f.write("export FOO=1\nalias x=y\n")

        assert "off" in _autostart("status", rc)
        _autostart("on", rc)
        assert "on" in _autostart("status", rc)
        body = open(rc).read()
        assert "# >>> tuimux autostart >>>" in body and "export FOO=1" in body

        _autostart("on", rc)  # idempotent — exactly one block
        assert open(rc).read().count("# >>> tuimux autostart >>>") == 1

        _autostart("off", rc)
        after = open(rc).read()
        assert "tuimux autostart" not in after  # block removed
        assert "export FOO=1" in after and "alias x=y" in after  # rest untouched
        assert "off" in _autostart("status", rc)


def test_autostart_bash_links_login_profile():
    # bash login shells (macOS) read .bash_profile/.profile, not .bashrc — so
    # `autostart on` must also make the login profile source .bashrc, and `off`
    # must remove both blocks. Force SHELL=bash and point both files at temps.
    def run(action, bashrc, profile):
        return subprocess.run(
            ["bash", app.ENGINE, "autostart", action],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "SHELL": "/bin/bash",
                "TUIMUX_RC": bashrc,
                "TUIMUX_LOGIN_RC": profile,
            },
        )

    with tempfile.TemporaryDirectory() as d:
        bashrc = os.path.join(d, "bashrc")
        profile = os.path.join(d, "bash_profile")
        with open(profile, "w") as f:
            f.write("export PATH=/x\n")  # a login profile that does NOT source .bashrc

        run("on", bashrc, profile)
        assert "# >>> tuimux autostart >>>" in open(bashrc).read()
        prof = open(profile).read()
        assert "# >>> tuimux autostart (login) >>>" in prof  # linked to .bashrc
        assert '. "$HOME/.bashrc"' in prof

        run("on", bashrc, profile)  # idempotent — one login block
        assert open(profile).read().count("# >>> tuimux autostart (login) >>>") == 1

        run("off", bashrc, profile)
        assert "tuimux autostart" not in open(bashrc).read()
        after = open(profile).read()
        assert "tuimux autostart" not in after  # both blocks gone
        assert "export PATH=/x" in after  # original preserved


def test_autostart_bash_skips_link_when_profile_already_sources_bashrc():
    with tempfile.TemporaryDirectory() as d:
        bashrc = os.path.join(d, "bashrc")
        profile = os.path.join(d, "bash_profile")
        with open(profile, "w") as f:
            f.write("[ -f ~/.bashrc ] && . ~/.bashrc\n")  # already sources it
        subprocess.run(
            ["bash", app.ENGINE, "autostart", "on"],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "SHELL": "/bin/bash",
                "TUIMUX_RC": bashrc,
                "TUIMUX_LOGIN_RC": profile,
            },
        )
        # no duplicate source line added to the login profile
        assert "tuimux autostart (login)" not in open(profile).read()
        assert "# >>> tuimux autostart >>>" in open(bashrc).read()


def test_autostart_state_reports_on_off_for_the_panel():
    # `__autostart` is the machine-readable on/off the dashboard shows in its border.
    with tempfile.TemporaryDirectory() as d:
        rc = os.path.join(d, "rc")
        env = {**os.environ, "TUIMUX_RC": rc}

        def state():
            return subprocess.run(
                ["bash", app.ENGINE, "__autostart"],
                capture_output=True,
                text=True,
                env=env,
            ).stdout.strip()

        assert state() == "off"
        _autostart("on", rc)
        assert state() == "on"
        _autostart("off", rc)
        assert state() == "off"


def test_autostart_snippet_carries_every_guard():
    with tempfile.TemporaryDirectory() as d:
        rc = os.path.join(d, "rc")
        _autostart("on", rc)
        body = open(rc).read()
    for guard in (
        '[ -z "$TMUX" ]',  # not already in tmux
        '[ -z "$SSH_CONNECTION" ]',  # local only (remote is `tuimux init`)
        '[ -z "$TUIMUX_NO_AUTOTMUX" ]',  # per-shell opt-out
        "case $- in *i*",  # interactive shells only
        "skip-autostart",  # the tuimux-spawned-tab exemption marker
        "__autoname",  # auto-named (happy-curie), not a bare number
        "tmux new-session",  # each terminal → its own fresh session
    ):
        assert guard in body, guard


def test_autostart_bakes_absolute_tuimux_path():
    # A fresh terminal often hasn't activated the conda env tuimux lives in, so the
    # snippet must call tuimux by ABSOLUTE path (baked from TUIMUX_BIN), not bare.
    with tempfile.TemporaryDirectory() as d:
        rc = os.path.join(d, "rc")
        fake = "/opt/envs/tuimux/bin/tuimux"
        subprocess.run(
            ["bash", app.ENGINE, "autostart", "on"],
            capture_output=True,
            text=True,
            env={**os.environ, "TUIMUX_RC": rc, "TUIMUX_BIN": fake},
        )
        body = open(rc).read()
        assert f"'{fake}' __autoname" in body


def test_autoname_prints_a_two_word_name():
    r = subprocess.run(
        ["bash", app.ENGINE, "__autoname"], capture_output=True, text=True
    )
    # docker-style "<adjective>-<scientist>", same generator tuimux uses elsewhere
    assert r.returncode == 0
    assert re.fullmatch(r"[a-z]+-[a-z]+", r.stdout.strip()), r.stdout


def test_term_spawn_drops_skip_autostart_marker():
    # tuimux's own spawns mark the new tab so the rc snippet skips auto-attaching it.
    with tempfile.TemporaryDirectory() as d:
        mark = os.path.join(d, "skip")
        subprocess.run(
            [
                "bash",
                "-c",
                f"source {app.ENGINE} __login >/dev/null 2>&1; "
                "linux_spawn(){ :; }; ghostty_spawn(){ :; }; terminal_spawn(){ :; }; "
                "term_spawn tab bash x __attach h s attach",
            ],
            env={**os.environ, "TUIMUX_SKIP_AUTOSTART": mark},
            stdin=subprocess.DEVNULL,
        )
        assert os.path.exists(mark)


# ---- probe parsing -----------------------------------------------------------
CANNED = "\n".join(
    [
        "OK",
        "S|keep-awake|0|1|/|caffeinate|0",
        "S|main|1|1|/Users/u/code|zsh|0",
        "S|cl|0|1|/Users/u/agent|node|0",
        "W|main|0|zsh|1",
        "L|main|xterm-ghostty",
        "C|cl",
        "A|cl|waiting",
    ]
)


def test_probe_parses_sessions_awake_and_agent():
    with stub_engine(CANNED):
        info = app.probe("host")
    assert info["reachable"] is True
    assert info["busy"] is False
    assert info["awake"] is True  # the keep-awake helper session is present
    # keep-awake is hidden; sessions are sorted by display name
    assert [s["auto"] for s in info["sessions"]] == ["cl", "main"]
    cl, main = info["sessions"]
    assert cl["agent"] is True and cl["state"] == "waiting"
    assert cl["open_in"] == "detached"  # not attached
    assert main["agent"] is False and main["state"] == "idle"  # zsh → idle
    assert main["open_in"] == "ghostty"  # attached + ghostty client


def test_probe_timeout_marks_busy_not_failed():
    with stub_engine("__TIMEOUT__\n"):
        info = app.probe("host")
    assert info["busy"] is True and info["reachable"] is False


def test_probe_unreachable():
    with stub_engine("UNREACHABLE\n"):
        info = app.probe("host")
    assert info["reachable"] is False and info["busy"] is False


def test_probe_keeps_raw_created_for_reconnect_identity():
    with stub_engine(CANNED):
        info = app.probe("host")
    # the raw session_created epoch is preserved (not just the formatted uptime),
    # so a reconnect can tell whether the very same session survived.
    assert all("created" in s for s in info["sessions"])
    assert {s["created"] for s in info["sessions"]} == {"0"}


# ---- reconnect reconciliation -----------------------------------------------
def _sess(name, created):
    return {"name": name, "created": created}


def test_reconcile_resumed_lost_and_quiet():
    old = [_sess("a", "100"), _sess("b", "200")]
    # a survived (same created), b is gone, c is brand new
    new = [_sess("a", "100"), _sess("c", "300")]
    assert app._reconcile_sessions(old, new) == {"a": "resumed", "b": "lost"}
    # same name but a new created → the old instance was lost (tmux restarted it)
    assert app._reconcile_sessions([_sess("a", "100")], [_sess("a", "999")]) == {
        "a": "lost"
    }
    # everything survived → all resumed (the happy reconnect case)
    assert app._reconcile_sessions(old, old) == {"a": "resumed", "b": "resumed"}
    assert app._reconcile_sessions([], []) == {}


# ---- view-model / rendering --------------------------------------------------
def _view_for(hosts, results, snap=None, reconcile=None):
    a = app.Tuimux()
    a._hosts = hosts
    a._results = results
    if snap:
        a._snap = snap
    if reconcile:
        a._reconcile = reconcile
    return a._view()


def test_view_status_words_per_state():
    hosts = [
        ("me", True, "online", "", "compute"),
        ("rem", False, "online", "", "compute"),
        ("busyh", False, "online", "", "compute"),
        ("noss", False, "online", "", "compute"),
        ("off", False, "offline", "2h ago", "compute"),
        ("pending", False, "online", "", "compute"),  # left unprobed → "checking…"
    ]
    base = {"reachable": False, "busy": False, "notmux": False, "awake": False}
    results = {
        "me": {**base, "reachable": True, "awake": True, "sessions": []},
        "rem": {**base, "reachable": True, "sessions": []},
        "busyh": {**base, "busy": True, "sessions": []},
        "noss": {**base, "sessions": []},
        "off": {**base, "sessions": [], "lastseen": "2h ago"},
    }
    rows = _view_for(hosts, results)
    status = {
        _cell_text(cells[0]).strip().lstrip("●○◐ "): _cell_text(cells[1])
        for cells, _ in rows
    }
    assert status["me"] == "local"
    assert status["rem"] == "ssh"
    assert status["busyh"] == "busy"
    assert status["noss"] == "no ssh"
    assert status["off"] == "offline"
    assert status["pending"] == "checking…"


def test_consumer_devices_show_status_only():
    # Phones/tablets are status-only: online/offline, never "no ssh", no probe.
    hosts = [
        ("phone-on", False, "online", "", "consumer"),
        ("phone-off", False, "offline", "1m ago", "consumer"),
    ]
    rows = _view_for(hosts, {})  # no probe results — must not show "checking…"
    status = {
        _cell_text(cells[0]).strip().lstrip("●○◐ "): _cell_text(cells[1])
        for cells, _ in rows
    }
    assert status["phone-on"] == "online"
    assert status["phone-off"] == "offline"
    # consumer rows are non-actionable
    assert all(m["action"] == "none" for _, m in rows)


def test_consumer_devices_sorted_last():
    raw = "comp\t0\toffline\t2h\tcompute\nphone\t0\tonline\t\tconsumer\nme\t1\tonline\t\tcompute\n"
    with stub_engine(raw):
        hosts = app.fetch_hosts()
    assert [h[0] for h in hosts] == ["comp", "me", "phone"]


def test_view_bold_only_on_identifiers():
    hosts = [
        ("me", True, "online", "", "compute"),
        ("off", False, "offline", "1h", "compute"),
    ]
    results = {
        "me": {
            "reachable": True,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [
                {
                    "name": "a",
                    "auto": "att",
                    "attached": True,
                    "dir": "~",
                    "tabs": "1",
                    "open_in": "ghostty",
                    "state": "running",
                    "uptime": "1h",
                    "agent": False,
                },
                {
                    "name": "b",
                    "auto": "det",
                    "attached": False,
                    "dir": "~",
                    "tabs": "1",
                    "open_in": "detached",
                    "state": "idle",
                    "uptime": "1h",
                    "agent": False,
                },
            ],
        },
        "off": {
            "reachable": False,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [],
            "lastseen": "1h",
        },
    }
    rows = _view_for(hosts, results)
    bolded = {
        _cell_text(cells[0]).strip(): True
        for cells, _ in rows
        if any("bold" in _cell_styles(cell) for cell in cells)
    }
    # online machine name and the attached session are bold; nothing else
    assert "● me" in bolded
    assert any(name.endswith("att") for name in bolded)  # attached session
    assert not any("det" in name for name in bolded)  # detached session not bold
    assert not any("off" in name for name in bolded)  # offline machine not bold


def test_view_row_meta_actions():
    hosts = [("me", True, "online", "", "compute")]
    results = {
        "me": {
            "reachable": True,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [
                {
                    "name": "s1",
                    "auto": "s1",
                    "attached": False,
                    "dir": "~",
                    "tabs": "1",
                    "open_in": "detached",
                    "state": "idle",
                    "uptime": "1h",
                    "agent": False,
                },
            ],
        }
    }
    rows = _view_for(hosts, results)
    actions = [meta["action"] for _, meta in rows]
    assert actions == ["machine", "attach", "new"]
    assert rows[-1][1]["session"] == "__NEW__"


# ---- offline "paused" sessions + reconnect verdicts -------------------------
_OPEN_IN = app._COLS.index("open_in")


def _snap_session(name, created="100", state="running"):
    return {
        "name": name,
        "auto": name,
        "attached": False,
        "dir": "~/code",
        "tabs": f"1  {state}",
        "open_in": "detached",
        "state": state,
        "created": created,
        "uptime": "1h",
        "agent": False,
    }


def test_offline_host_shows_remembered_sessions_as_unreachable():
    hosts = [("off", False, "offline", "2h ago", "compute")]
    base = {"reachable": False, "busy": False, "notmux": False, "awake": False}
    results = {"off": {**base, "sessions": [], "lastseen": "2h ago"}}
    snap = {"off": [_snap_session("build"), _snap_session("api")]}
    rows = _view_for(hosts, results, snap=snap)
    # machine row + its two remembered sessions (no "+ new" for an offline host)
    assert [m["action"] for _, m in rows] == ["none", "none", "none"]
    sess = rows[1:]
    assert {_cell_text(c[0]).strip() for c, _ in sess} == {"build", "api"}
    # each remembered session is flagged unreachable, and nothing is attachable
    assert all(_cell_text(c[_OPEN_IN]) == "unreachable" for c, _ in sess)
    assert all(m["session"] in ("build", "api") for _, m in sess)


def test_offline_host_without_snapshot_shows_only_the_machine_row():
    # no remembered sessions (e.g. never probed before going down) → unchanged
    hosts = [("off", False, "offline", "2h ago", "compute")]
    base = {"reachable": False, "busy": False, "notmux": False, "awake": False}
    results = {"off": {**base, "sessions": [], "lastseen": "2h ago"}}
    rows = _view_for(hosts, results)
    assert len(rows) == 1 and _cell_text(rows[0][0][1]) == "offline"


def test_reconnect_marks_resumed_and_lost():
    hosts = [("rem", False, "online", "", "compute")]
    live = _snap_session("build", created="100")
    live.update(auto="build", attached=False)
    results = {
        "rem": {
            "reachable": True,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [live],
        }
    }
    # "build" survived (still live); "gone" did not come back
    reconcile = {"rem": (app.time.monotonic(), {"build": "resumed", "gone": "lost"})}
    rows = _view_for(hosts, results, reconcile=reconcile)
    texts = [_cell_text(c[0]).strip() for c, _ in rows]
    # the surviving session carries a "resumed" marker in its OPEN IN cell
    build_row = next(c for c, _ in rows if _cell_text(c[0]).strip() == "build")
    assert "resumed" in _cell_text(build_row[_OPEN_IN])
    # the lost session gets its own transient row that isn't attachable
    lost = next((c, m) for c, m in rows if _cell_text(c[0]).strip() == "gone")
    assert _cell_text(lost[0][_OPEN_IN]) == "lost — not restored"
    assert lost[1]["action"] == "none"
    assert "gone" in texts


def test_reconnect_verdicts_age_out_after_ttl():
    hosts = [("rem", False, "online", "", "compute")]
    live = _snap_session("build", created="100")
    results = {
        "rem": {
            "reachable": True,
            "busy": False,
            "notmux": False,
            "awake": False,
            "sessions": [live],
        }
    }
    stale = app.time.monotonic() - app.RECONCILE_TTL - 1
    reconcile = {"rem": (stale, {"build": "resumed", "gone": "lost"})}
    rows = _view_for(hosts, results, reconcile=reconcile)
    # past the TTL: no "resumed" badge, no transient "lost" row
    assert not any("resumed" in _cell_text(c[_OPEN_IN]) for c, _ in rows)
    assert not any(_cell_text(c[0]).strip() == "gone" for c, _ in rows)


# ---- window location ("OPEN IN") --------------------------------------------
def test_window_label_this_vs_other():
    a = app.Tuimux()
    # tuimux + "main" share window 1; "build" is alone in window 2
    a._windows = [("1", "tuimux"), ("1", "main · zsh"), ("2", "build · vim")]
    a._self_win = "1"
    assert a._window_label("main") == "this window"
    assert a._window_label("build") == "other window"
    assert a._window_label("missing") is None  # no matching tab


def test_open_in_cell():
    a = app.Tuimux()
    a._windows = [("1", "tuimux"), ("1", "main · zsh"), ("2", "build · vim")]
    a._self_win = "1"

    def cell(s):
        return a._open_in_cell(s, a._window_label(s["name"]))

    assert cell({"name": "x", "open_in": "detached"}) == ("detached", "dim")
    assert cell({"name": "main", "open_in": "ghostty"})[0] == "this window"
    assert cell({"name": "build", "open_in": "ghostty"})[0] == "other window"
    # attached but no local tab found → fall back to the terminal type
    assert cell({"name": "zzz", "open_in": "ghostty"})[0] == "ghostty"


# ---- Linux spawn command builder (engine.sh) --------------------------------
# These shell out to the engine forcing TUIMUX_OS=linux + TUIMUX_DRY_RUN=1, so they
# exercise the Linux launch-command construction on any platform (incl. this macOS
# box) without ever opening a GUI window or touching osascript.
def _engine_dry(args, env=None):
    e = {**os.environ, "TUIMUX_OS": "linux", "TUIMUX_DRY_RUN": "1"}
    if env:
        e.update(env)
    return subprocess.run(
        ["bash", app.ENGINE, *args], capture_output=True, text=True, env=e
    ).stdout


def test_linux_gnome_tab():
    out = _engine_dry(
        ["__open", "tab", "macmini", "main", "attach"], {"TUIMUX_TERM": "gnome"}
    )
    assert (
        out.strip()
        == f"[dry-run] gnome-terminal --tab -- bash {app.ENGINE} __attach macmini main attach"
    )


def test_linux_gnome_window_carries_identity_and_quotes_spaces():
    out = _engine_dry(
        ["__open", "window", "macmini", "weird name", "attach"],
        {"TUIMUX_TERM": "gnome", "TUIMUX_SELF_HOST": "mybox"},
    )
    assert "gnome-terminal --window -- env TUIMUX_SELF_HOST=mybox bash" in out
    assert "weird\\ name attach" in out  # the spaced session name stays one token


def test_linux_custom_template_wraps_via_sh():
    out = _engine_dry(
        ["__open", "tab", "macmini", "main", "attach"],
        {"TUIMUX_TERM_CMD": "kitty -e sh -c {cmd}"},
    )
    assert out.startswith("[dry-run] sh -c ")
    # {cmd} expanded to the engine invocation, wrapped for the user's terminal
    for piece in ("kitty", "__attach", "macmini", "main"):
        assert piece in out


def test_linux_generic_terminal_window_only():
    out = _engine_dry(
        ["__open", "window", "macmini", "main", "new"],
        {"TUIMUX_TERM": "generic", "TERMINAL": "alacritty"},
    )
    assert (
        out.strip()
        == f"[dry-run] alacritty -e bash {app.ENGINE} __attach macmini main new"
    )


def test_linux_wayland_auto_falls_through_to_new_surface():
    # Wayland blocks jump-to-window, so `auto` can't focus → opens a fresh tab.
    out = _engine_dry(
        ["__open", "auto", "macmini", "happy-curie", "attach"],
        {"TUIMUX_TERM": "gnome", "XDG_SESSION_TYPE": "wayland"},
    )
    assert "[dry-run] gnome-terminal --tab -- bash" in out
    assert "__attach macmini happy-curie attach" in out


# ---- new-tab targeting (engine.sh) ------------------------------------------
# osascript can't run headless, so we source the engine and stub it. The macOS
# spawners feed their AppleScript to osascript on stdin (tab) or via -e (window);
# the stub appends stdin to a capture file so we can assert what each path builds.
def _engine_func(call, env=None, stubs=""):
    e = {**os.environ}
    if env:
        e.update(env)
    script = (
        f"source {app.ENGINE} __login >/dev/null 2>&1; "
        "osascript(){ return 0; }; "  # never drive the GUI in tests
        f"{stubs}{call}"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=e,
        stdin=subprocess.DEVNULL,
    ).stdout


def test_ghostty_new_tab_targets_dashboard_window():
    # Ghostty finds its window by the visible "tuimux" title: a new *tab* raises it
    # first; a new *window* does not.
    stubs = "ghostty_raise_self(){ echo RAISED_SELF; }; "
    tab = _engine_func("ghostty_spawn t bash x __attach h s attach", stubs=stubs)
    win = _engine_func("ghostty_spawn n bash x __attach h s attach", stubs=stubs)
    assert "RAISED_SELF" in tab  # new tab → opened into the dashboard window
    assert "RAISED_SELF" not in win  # new window → a brand-new window, left alone


def test_terminal_new_tab_pins_dashboard_window_id():
    # Terminal.app hides our title, so a new *tab* pins the dashboard by the window
    # id we captured at startup (set index … to 1) before Cmd-T; a *window* doesn't.
    with tempfile.TemporaryDirectory() as d:
        cap = os.path.join(d, "cap")
        stubs = (
            "build_exec_cmd(){ echo CMD; }; osa_escape(){ printf '%s' \"$1\"; }; "
            f"osascript(){{ cat >> {cap} 2>/dev/null; return 0; }}; "
        )
        env = {"TUIMUX_SELF_WINID": "4242"}
        open(cap, "w").close()
        _engine_func("terminal_spawn tab bash x", env=env, stubs=stubs)
        tab_script = open(cap).read()
        open(cap, "w").close()
        _engine_func("terminal_spawn window bash x", env=env, stubs=stubs)
        win_script = open(cap).read()
    assert "set index of window id 4242 to 1" in tab_script
    assert 'keystroke "t"' in tab_script  # the new tab itself
    assert "window id 4242" not in win_script  # a window is brand-new, not pinned


def test_terminal_new_tab_without_winid_does_not_pin():
    # No captured id (e.g. Ghostty/Linux, or capture failed) → no pin, just front
    # window — degrades to the old behaviour, never errors.
    with tempfile.TemporaryDirectory() as d:
        cap = os.path.join(d, "cap")
        stubs = (
            "build_exec_cmd(){ echo CMD; }; osa_escape(){ printf '%s' \"$1\"; }; "
            f"osascript(){{ cat >> {cap} 2>/dev/null; return 0; }}; "
        )
        open(cap, "w").close()
        _engine_func("terminal_spawn tab bash x", stubs=stubs)  # no TUIMUX_SELF_WINID
        tab_script = open(cap).read()
    assert "set index of window id" not in tab_script
    assert 'keystroke "t"' in tab_script


def test_linux_list_windows_x11_keeps_only_terminals():
    with tempfile.TemporaryDirectory() as d:
        stub = os.path.join(d, "wmctrl")
        with open(stub, "w") as f:
            f.write(
                "#!/bin/bash\n"
                'if [ "$1" = "-lx" ]; then\n'
                '  echo "0x01 0 gnome-terminal-server.Gnome-terminal h tuimux"\n'
                '  echo "0x02 0 gnome-terminal-server.Gnome-terminal h happy-curie · main"\n'
                '  echo "0x03 0 firefox.Firefox h Mozilla Firefox"\n'
                "fi\n"
            )
        os.chmod(stub, 0o755)
        out = _engine_dry(
            ["__windows"],
            {"XDG_SESSION_TYPE": "x11", "PATH": d + os.pathsep + os.environ["PATH"]},
        )
    # firefox dropped; terminals emitted as "<n>|<title>" for the OPEN IN column
    assert out.strip().splitlines() == ["1|tuimux", "2|happy-curie · main"]


if __name__ == "__main__":
    import inspect

    tests = [
        fn
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and inspect.isfunction(fn)
    ]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} tests passed")
