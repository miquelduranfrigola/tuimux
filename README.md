# A TUI for tmux across tailnet

See and jump into every tmux session on every machine on your
[Tailscale](https://tailscale.com) tailnet, from one dashboard — over Tailscale SSH.

## Install

```sh
pip install .          # or: pipx install .
```

Needs `tmux`, `ssh`, `tailscale`, and a terminal. Opening sessions in new
tabs/windows is supported on:

- **macOS** — **Ghostty** (recommended) and **Apple Terminal.app**; any other
  terminal falls back to Terminal.app.
- **Linux** — **GNOME Terminal** (real tabs + windows), or **any** terminal via a
  `TUIMUX_TERM_CMD` template (e.g. `kitty -e sh -c {cmd}`). Jumping to an
  already-open session and the "OPEN IN" column additionally need **X11** with
  `wmctrl` (or `xdotool`) installed — on Wayland every open is a new surface.

Force a driver with `TUIMUX_TERM` and override platform detection with `TUIMUX_OS`
if needed. Run `tuimux doctor` to see what's detected. On each remote machine:
`sudo tailscale up --ssh` and install `tmux`.

## Use

```sh
tuimux                      # the dashboard — all you normally need
tuimux attach [name]        # put this terminal into a tmux session (attach or create)
tuimux detach               # detach this terminal; the session keeps running
tuimux autostart on|off|status  # auto-attach EVERY new local terminal to its own session
tuimux init <host>          # auto-tmux a remote's SSH logins
tuimux doctor               # check setup
```

Open / rename / detach / close / keep-awake all happen in the dashboard (footer
lists the keys). Any tmux session shows up regardless of how it was started.

**`tuimux autostart on`** makes every new terminal you open (any app — Ghostty,
Terminal.app, GNOME Terminal, …) drop straight into its own fresh tmux session, so
it persists and appears in the dashboard without running `attach` by hand. It adds a
small guarded block to your shell rc (`~/.zshrc` etc.); `off` removes it, `status`
shows the state. Skip it for one shell with `TUIMUX_NO_AUTOTMUX=1 <command>` — handy
for launching the dashboard itself: `TUIMUX_NO_AUTOTMUX=1 tuimux`.

**Opening a session** lands in a new tab **next to the dashboard** (not in whatever
window happens to be frontmost), or in a new window if you ask for one. If a session
is already open on this machine, the menu offers **"go to its tab"** instead of
opening a duplicate.

**When a machine goes offline** (asleep, off the network, or shut down) its sessions
don't vanish — they stay listed, dimmed, marked **`unreachable`**, showing what was
last running. tmux can't tell "asleep" from "shut down" while a machine is away, so
the honest answer comes on **reconnect**: each session is briefly tagged **`resumed`**
(the same session survived — its process kept running) or a remembered one is flagged
**`lost`** (it was shut down / tmux restarted). This memory is in-process only.
