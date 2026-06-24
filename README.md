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
tuimux mouse on|off|status  # tmux mouse mode: wheel scrolls the pane, not shell history
tuimux init <host>          # auto-tmux a remote's SSH logins
tuimux login [host user]    # show/set the SSH username per host (--rm host to clear)
tuimux devices              # list every device in the tailnet (the team fleet)
tuimux doctor               # check setup
```

Open / rename / detach / close / keep-awake all happen in the dashboard (footer
lists the keys). Any tmux session shows up regardless of how it was started. As
you move the cursor, a panel under the table shows a **live preview** of the
highlighted session's pane — a read-only glimpse of what it's doing without
attaching; press **`v`** to hide or show it.

**Shared machines & the team fleet.** By default the dashboard shows your own
machines. Press **`o`** to toggle the **org fleet view** — every device in the
tailnet, whoever owns it, with non-compute ones (phones, etc.) grouped as
status-only. On a shared box where your account isn't your local `$USER` (say you
log into `herbert` as `mduran`), press **`u`** on that row to set the SSH username
tuimux connects as — or run `tuimux login herbert mduran`. Once mapped, a host
always appears in your list (even if a teammate owns it) and tuimux probes it as
your user, so you see *your* tmux sessions there. Unmapped fleet machines are
listed but not contacted until you give them a login.

Access stays **passwordless** — tuimux only stores the *username*, never a
secret. You still need permission to log in: a Tailscale SSH ACL that lets you
assume that remote user, or your key in that user's `~/.ssh/authorized_keys`.
Teammates each run their own tuimux, mapping the shared box to their own account.

**`tuimux autostart on`** makes every new terminal you open (any app — Ghostty,
Terminal.app, GNOME Terminal, …) drop straight into its own fresh tmux session, so
it persists and appears in the dashboard without running `attach` by hand. It adds a
small guarded block to your shell rc (`~/.zshrc` etc.); `off` removes it, `status`
shows the state. Skip it for one shell with `TUIMUX_NO_AUTOTMUX=1 <command>`.

**`tuimux mouse on`** turns on tmux mouse mode so the trackpad/wheel scrolls the
pane's scrollback instead of being sent to the shell as history. It persists the
setting in `~/.tmux.conf` *and* applies it to the running tmux server, so it takes
effect immediately; `off` reverts it, `status` shows the state. With it on, to
select/copy using the terminal's *native* selection, hold **Shift** while dragging
(Ghostty, iTerm2, GNOME Terminal; **Option** on Apple Terminal).

**On the first run, both `autostart` and `mouse` are enabled for you** (a one-time
setup — it never repeats, so turning either `off` later sticks). The dashboard's
bottom border shows the current state: `autostart: …  ·  mouse scroll: …`.

The dashboard itself must run **outside** tmux. You don't have to think about it:
type **`tuimux`** from anywhere — if you happen to be inside a tmux session (e.g.
because autostart put you there), it detaches that client and relaunches the
dashboard **in the same window**. If that session was just a throwaway (a lone
autostart shell — one window, one pane, no other client) it's discarded too, so it
doesn't clutter the list; a session with real work (extra windows/panes, or shared
with another client) is only detached and keeps running.

**Opening a session** lands in a new tab **next to the dashboard** (not in whatever
window happens to be frontmost), or in a new window if you ask for one. If a session
is already open on this machine, the menu offers **"go to its tab"** instead of
opening a duplicate.

**The "OPEN IN" column** tells you where you can reach the session. If it's open
as a tab on **this machine** it says so and stops — `this window` (a tab in the
dashboard's own window) or `other window` — because you can just jump to it. If
it isn't open here (`—`) it reports its attachment **on the host that runs it**:
`N clients` when something else holds it (e.g. a teammate attached on a shared
box), or `detached` when nothing is. So `— · 2 clients` is a session you have no
local tab for but that two clients are in, and `— · detached` is idle and free to
open fresh.

**When a machine goes offline** (asleep, off the network, or shut down) its sessions
don't vanish — they stay listed, dimmed, marked **`unreachable`**, showing what was
last running. tmux can't tell "asleep" from "shut down" while a machine is away, so
the honest answer comes on **reconnect**: each session is briefly tagged **`resumed`**
(the same session survived — its process kept running) or a remembered one is flagged
**`lost`** (it was shut down / tmux restarted). This memory is in-process only.
