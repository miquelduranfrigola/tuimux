"""Unit tests for tuimux's pure logic — parsing, naming, and table rendering.

No network, SSH, or running Textual app required: the engine call is stubbed and
the dashboard's view-model (`_view`) is exercised directly. Run with `pytest`, or
standalone with `python tests/test_tuimux.py`.
"""

import contextlib
import os
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


# ---- view-model / rendering --------------------------------------------------
def _view_for(hosts, results):
    a = app.Tuimux()
    a._hosts = hosts
    a._results = results
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
    assert a._open_in_cell({"name": "x", "open_in": "detached"}) == ("detached", "dim")
    assert a._open_in_cell({"name": "main", "open_in": "ghostty"})[0] == "this window"
    assert a._open_in_cell({"name": "build", "open_in": "ghostty"})[0] == "other window"
    # attached but no local tab found → fall back to the terminal type
    assert a._open_in_cell({"name": "zzz", "open_in": "ghostty"})[0] == "ghostty"


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
