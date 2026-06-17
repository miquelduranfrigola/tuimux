# A TUI for tmux across tailnet

See and jump into every tmux session on every machine on your
[Tailscale](https://tailscale.com) tailnet, from one dashboard — over Tailscale SSH.

## Install

```sh
pip install .          # or: pipx install .
```

Needs `tmux`, `ssh`, `tailscale`, and a terminal. On macOS, opening sessions in
new tabs/windows works with **Ghostty** (recommended) and **Apple Terminal.app**;
any other terminal falls back to Terminal.app (force a driver with
`TUIMUX_TERM=ghostty|terminal`). On each remote machine: `sudo tailscale up --ssh`
and install `tmux`.

## Use

```sh
tuimux                  # the dashboard — all you normally need
tuimux here [name]      # drop this terminal into a tmux session
tuimux init <host>      # auto-tmux a remote's SSH logins
tuimux doctor           # check setup
```

Open / rename / detach / close / keep-awake all happen in the dashboard (footer
lists the keys). Any tmux session shows up regardless of how it was started.
