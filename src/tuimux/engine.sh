#!/usr/bin/env bash
#
# tuimux — a TUI to open, reach, and keep awake tmux sessions across your
# tailnet over Tailscale SSH.
#
# Usage:
#   tuimux                    Launch the dashboard — this is all you normally need
#   tuimux attach [name]      Put THIS terminal into a tmux session — attach if it
#                             exists, create it otherwise (shows up in the dashboard);
#                             no name = a fresh auto-named one
#   tuimux detach             Detach THIS terminal's session — it keeps running in
#                             the background; the terminal drops back to a shell
#   tuimux autostart on|off   Auto-attach EVERY new local terminal to its own tmux
#                  |status     session (edits your shell rc); status shows the state
#   tuimux mouse on|off       Toggle tmux mouse mode — wheel scrolls the pane, not
#                  |status     shell history (persists in ~/.tmux.conf)
#   tuimux init <host>        Make a remote auto-tmux on SSH login (opt-in, asks first)
#   tuimux login [host user]  Show or set the SSH username per host (for shared
#                  |--rm host  machines); no args lists, --rm removes one
#   tuimux devices            List every device in the tailnet (the team fleet)
#   tuimux doctor             Check local deps + per-host reachability and remote tmux
#   tuimux -h|--help          This help
#
# Everything else (open / rename / detach / close / keep-awake) lives in the
# dashboard. This script is the engine it calls for discovery, probing, actions.
#
# Config (optional): ~/.config/tuimux/config  — see config.example
#   TUIMUX_HOSTS, TUIMUX_LOGIN, TUIMUX_LOGINS, TUIMUX_DEFAULT_SESSION, TUIMUX_SSH_TIMEOUT, TUIMUX_REFRESH
#
set -uo pipefail

# Public command name used when we re-invoke ourselves into a new terminal
# (set by the `tuimux` entry point); ENGINE_FILE is this script, for usage text.
SELF="${TUIMUX_BIN:-tuimux}"
ENGINE_FILE="$0"

# ----- defaults / config -----------------------------------------------------
TUIMUX_HOSTS="${TUIMUX_HOSTS:-}"
TUIMUX_LOGIN="${TUIMUX_LOGIN:-$USER}"
# Per-host SSH usernames, for shared machines where your account differs from the
# default ($USER). A space-separated list of host=user tokens, e.g.
#   TUIMUX_LOGINS="herbert=mduran nebula=mduran"
# Plain string (not an associative array) so it works on macOS's bash 3.2. Manage
# it with `tuimux login`. Unmapped hosts fall back to TUIMUX_LOGIN.
TUIMUX_LOGINS="${TUIMUX_LOGINS:-}"
TUIMUX_DEFAULT_SESSION="${TUIMUX_DEFAULT_SESSION:-main}"
TUIMUX_SSH_TIMEOUT="${TUIMUX_SSH_TIMEOUT:-5}"
TUIMUX_REFRESH="${TUIMUX_REFRESH:-3}"          # panel auto-refresh interval (seconds)
# TERM to use for *remote* interactive sessions. Ghostty's own "xterm-ghostty"
# terminfo usually isn't installed on other machines, so propagating it makes
# remote tmux abort with "missing or unsuitable terminal" and the tab dies.
# xterm-256color is present essentially everywhere. (Local stays as-is.)
TUIMUX_REMOTE_TERM="${TUIMUX_REMOTE_TERM:-xterm-256color}"
# Pause (seconds) after opening a new tab/window before the command is sent to
# it — must cover the new shell becoming ready. Lower = snappier opens; raise it
# if the first keystrokes ever get dropped on a slower machine.
TUIMUX_SPAWN_DELAY="${TUIMUX_SPAWN_DELAY:-0.2}"
# Title the dashboard puts on its own surface (matches app.py `self.title`). A new
# tab is opened INTO this window, so "new tab" always lands beside the panel —
# never in whatever window happens to be frontmost (e.g. one you just opened).
# Keep in sync with app.py's window-self detection.
TUIMUX_SELF_TITLE="${TUIMUX_SELF_TITLE:-tuimux}"
# One-shot marker the dashboard drops right before it spawns a surface, so the
# `tuimux autostart` rc snippet knows THAT terminal was opened by tuimux (and
# already has its own attach command coming) and skips auto-attaching it.
TUIMUX_SKIP_AUTOSTART="${TUIMUX_SKIP_AUTOSTART:-$HOME/.cache/tuimux/skip-autostart}"

CONFIG_FILE="${TUIMUX_CONFIG:-$HOME/.config/tuimux/config}"
# shellcheck disable=SC1090
[ -f "$CONFIG_FILE" ] && . "$CONFIG_FILE"

NEW_SENTINEL="__NEW__"
NONE_SENTINEL="__NONE__"
AWAKE_SESSION="keep-awake"   # dedicated tmux session that holds the keep-awake lock

# No ControlMaster/ControlPersist multiplexing: Tailscale SSH accepts the master
# connection but then hangs every multiplexed session over it, so each probe just
# makes its own connection (kept cheap by the per-host, non-overlapping refresh).
SSH_OPTS=(-o ConnectTimeout="$TUIMUX_SSH_TIMEOUT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)

# ----- helpers ---------------------------------------------------------------
err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
note() { printf '\033[2m%s\033[0m\n'  "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
usage() { sed -n '3,25p' "$ENGINE_FILE" | sed 's/^# \{0,1\}//'; }

# Which terminal we're running inside, normalized to a spawn driver:
#   ghostty   → drive Ghostty (AppleScript keybind + keystroke)
#   terminal  → drive Apple Terminal.app (AppleScript `do script`)
# Apple Terminal is the universal macOS fallback — always installed and
# scriptable — so any terminal we don't specifically recognise routes through it.
# Force a driver with TUIMUX_TERM=ghostty|terminal (e.g. for an unsupported term).
term_kind() {
  case "${TUIMUX_TERM:-${TERM_PROGRAM:-}}" in
    Ghostty|ghostty) echo ghostty ;;
    *)               echo terminal ;;   # Apple_Terminal + universal fallback
  esac
}

# Platform we open new terminal surfaces on: darwin (osascript) or linux. macOS
# drives Ghostty/Terminal.app via AppleScript; Linux launches a terminal binary.
# Override with TUIMUX_OS=darwin|linux (mirrors TUIMUX_TERM; lets the Linux paths
# be exercised from a Mac, and forces a choice on an unrecognised `uname`).
os_kind() {
  case "${TUIMUX_OS:-$(uname 2>/dev/null)}" in
    Darwin|darwin) echo darwin ;;
    *)             echo linux ;;
  esac
}

# Which Linux terminal to launch — gnome (GNOME Terminal / VTE family), custom
# (TUIMUX_TERM_CMD template), or generic (x-terminal-emulator/$TERMINAL). TUIMUX_TERM
# forces it; otherwise sniff the VTE env and fall back to what's on PATH.
linux_term() {
  [ -n "${TUIMUX_TERM_CMD:-}" ] && { echo custom; return; }
  case "${TUIMUX_TERM:-}" in
    gnome|custom|generic) echo "$TUIMUX_TERM"; return ;;
  esac
  if [ -n "${GNOME_TERMINAL_SCREEN:-}${VTE_VERSION:-}" ] || have gnome-terminal; then
    echo gnome
  else
    echo generic
  fi
}

# X11 vs Wayland — decides whether jump-to-window / window-listing are possible at
# all (X11: best-effort via wmctrl; Wayland: the compositor blocks cross-window control).
linux_display() {
  case "${XDG_SESSION_TYPE:-}" in
    wayland) echo wayland; return ;;
    x11)     echo x11; return ;;
  esac
  [ -n "${WAYLAND_DISPLAY:-}" ] && { echo wayland; return; }
  echo x11
}

# `tailscale status`/`ip` memoized per invocation via env vars. A single __hosts
# otherwise shells out to tailscale ~16 times (discover + offline + once per host
# via self_host); hosts_data fills this cache so its command-substitution
# subshells reuse one snapshot. Other entry points just call tailscale directly.
ts_status() {
  [ -n "${_EOS_TS_STATUS+x}" ] && { printf '%s' "$_EOS_TS_STATUS"; return; }
  tailscale status 2>/dev/null
}
ts_ip() {
  [ -n "${_EOS_TS_IP+x}" ] && { printf '%s' "$_EOS_TS_IP"; return; }
  tailscale ip -4 2>/dev/null | head -1
}

# This machine's tailnet hostname. The dashboard already learned it during host
# discovery and passes it back as TUIMUX_SELF_HOST, which lets every probe skip
# the tailscale lookups below; we only compute it when that hint is absent.
self_host() {
  if [ -n "${TUIMUX_SELF_HOST:-}" ]; then printf '%s' "$TUIMUX_SELF_HOST"; return; fi
  local ip name
  ip="$(ts_ip)"
  name="$(ts_status | awk -v ip="$ip" '$1==ip {print $2; exit}')"
  [ -n "$name" ] || name="$(hostname -s 2>/dev/null)"
  printf '%s' "$name"
}
is_local() { [ "$1" = "$(self_host)" ]; }

# Canonical tailnet fleet: every device name, sorted — the same absolute, stable
# ordering on every machine and for every teammate, so a host's accent colour is
# identical everywhere. Derived from one place (here) and exposed to the dashboard
# as a hosts_data column, so the UI and the tmux status bar can never disagree.
fleet_order() {
  ts_status | awk '$1 ~ /^[0-9]/ && NF>=2 {print $2}' | LC_ALL=C sort -u
}
# 0-based position of a host in the fleet ordering (or the count, i.e. "past the
# end", for a name not in the tailnet — e.g. a TUIMUX_HOSTS-only entry).
fleet_index() {
  local h="$1" i=0 name
  while IFS= read -r name; do
    [ "$name" = "$h" ] && { printf '%d' "$i"; return; }
    i=$((i + 1))
  done <<EOF
$(fleet_order)
EOF
  printf '%d' "$i"
}

# Stable per-machine accent colour. This machine is always teal; every other host
# gets its own distinct hue from its fleet index (golden-angle stepping around the
# colour wheel → adjacent machines look clearly different, and a given host is the
# same colour everywhere). The dashboard reuses this exact value (hosts_data color
# column), so a session's tmux status bar always matches its row.
host_color() {
  is_local "$1" && { printf '#34d8b1'; return; }
  awk -v i="$(fleet_index "$1")" '
    function abs(x) { return x < 0 ? -x : x }
    BEGIN {
      H = i * 137.508; H = H - int(H / 360) * 360   # golden-angle hue, wrapped to [0,360)
      S = 0.62; L = 0.66                            # tuned for dark text on the bar
      C = (1 - abs(2 * L - 1)) * S
      Hp = H / 60.0
      X = C * (1 - abs((Hp - 2 * int(Hp / 2)) - 1))
      m = L - C / 2
      if      (Hp < 1) { r = C; g = X; b = 0 }
      else if (Hp < 2) { r = X; g = C; b = 0 }
      else if (Hp < 3) { r = 0; g = C; b = X }
      else if (Hp < 4) { r = 0; g = X; b = C }
      else if (Hp < 5) { r = X; g = 0; b = C }
      else             { r = C; g = 0; b = X }
      printf "#%02x%02x%02x", int((r+m)*255+0.5), int((g+m)*255+0.5), int((b+m)*255+0.5)
    }'
}

# tmux options tuimux sets on every session it drives, as a "cmd1; cmd2; " prefix
# (run on whichever host owns the session). set-titles makes window titles match
# the dashboard's "open in" detection; mouse on so the trackpad scrolls tmux's
# scrollback and selects text — without it, being wrapped in tmux hides the
# terminal's own scrollback and scrolling does nothing. (Hold Shift to bypass
# tmux and use the terminal's native selection.) Kept in one place so the attach,
# new, and autostart paths can't drift apart.
tmux_opts() {
  printf "tmux set -g set-titles on; tmux set -g set-titles-string '#S · #W'; tmux set -g mouse on; "
}

# tmux commands that tint the status bar with the host's accent + a name label,
# so once you're attached you can see at a glance which machine you're on.
status_style() {
  printf "tmux set -g status-style 'bg=%s,fg=#16161e'; tmux set -g status-left ' #[bold]%s#[nobold] '; tmux set -g status-left-length 40; " \
    "$(host_color "$1")" "$1"
}

# Docker-style random name:  <adjective>-<scientist>  (e.g. happy-curie).
# Big pools (~115 × ~150 ≈ 17k combos) so names rarely repeat or look alike.
docker_name() {
  local adj=(
    admiring adoring adventurous affectionate agitated amazing awesome blissful
    bold boring brave bright bubbly busy calm charming cheerful clever cool
    compassionate competent confident cosmic cranky crafty crazy curious daring
    dazzling determined distracted dreamy eager ecstatic elated elegant eloquent
    epic exciting fearless fervent festive fierce flamboyant focused friendly
    frosty funny gallant gentle gifted gleaming goofy gracious happy hardy
    heuristic hopeful hungry infallible inspiring jolly jovial joyful keen kind
    laughing lively loving lucid luminous magical mellow merry mighty modest
    musing mystic nifty nimble nostalgic objective optimistic peaceful pensive
    playful practical precious quirky quizzical radiant relaxed reverent rustic
    sage serene sharp shiny silly sleepy snappy spirited stoic sturdy sunny sweet
    tender thirsty trusty upbeat vibrant vigilant vigorous vivid witty wizardly
    wonderful youthful zealous zen zesty
  )
  local sci=(
    agnesi archimedes aryabhata babbage banach bardeen bartik bassi bell bohr
    booth bose boyd brahmagupta brattain brown buck cannon carson cartwright cerf
    chandra chaplygin chatelet chebyshev clarke cohen colden cori cray curie
    darwin davinci dewdney dhawan diffie dijkstra dirac driscoll easley edison
    einstein elion ellis engelbart euclid euler faraday fermat fermi feynman
    franklin gagarin galileo galois gates gauss germain goldberg goodall gould
    greider hamilton hawking heisenberg hertz hodgkin hoover hopper hypatia
    jackson jang jemison jennings jepsen johnson jones kalam kapitsa kare keller
    kepler khorana kilby kirch knuth lamarr lamport leakey leavitt lederberg
    lehmann lewin liskov lovelace lumiere maxwell mayer mccarthy mcclintock
    mclean meitner mendel mendeleev merkle mestorf moore morse moser nash newton
    nightingale nobel noether noyce ohm pare pascal pasteur payne perlman pike
    poincare ptolemy raman ramanujan rhodes ride ritchie robinson roentgen rubin
    saha sammet satoshi shamir shannon shaw shirley shockley snyder solomon spence
    sutherland swanson swartz tesla thompson torvalds turing vaughan villani volta
    wescoff wilbur wiles williams wilson wing wozniak wright wu yalow yonath
  )
  printf '%s-%s' "${adj[RANDOM % ${#adj[@]}]}" "${sci[RANDOM % ${#sci[@]}]}"
}

# The SSH username to use for a host: its explicit TUIMUX_LOGINS mapping if any,
# else the global TUIMUX_LOGIN ($USER). Tokens are "host=user"; first match wins.
login_for() {
  local host="$1" tok
  for tok in $TUIMUX_LOGINS; do
    case "$tok" in "$host="?*) printf '%s' "${tok#*=}"; return ;; esac
  done
  printf '%s' "$TUIMUX_LOGIN"
}

# Just the host names that have an explicit login mapping (one per line). Used by
# discovery so a mapped shared host always shows up even when it's owned by a
# teammate (different tailnet owner).
mapped_host_keys() {
  local tok
  for tok in $TUIMUX_LOGINS; do
    case "$tok" in *=?*) printf '%s\n' "${tok%%=*}" ;; esac
  done
}

# The EXPLICIT mapped login for a host, or empty if it has none. Unlike login_for
# this never falls back to $USER — the dashboard uses the emptiness to tell a real
# mapping ("connect as gturon") apart from the default.
mapping_for() {
  local host="$1" tok
  for tok in $TUIMUX_LOGINS; do
    case "$tok" in "$host="?*) printf '%s' "${tok#*=}"; return ;; esac
  done
}

# Run a command string on a host — locally if it's this machine, else over SSH
# as the host's mapped login (login_for).
rssh() {
  local host="$1"; shift
  if is_local "$host"; then sh -c "$*"; else ssh "${SSH_OPTS[@]}" "$(login_for "$host")@$host" "$*"; fi
}

# Abort unless $1 is one of your currently-reachable machines.
require_reachable_host() {
  local only="$1" hosts
  [ -n "$only" ] || return 1
  hosts="$(discover_hosts)" || exit 1
  printf '%s\n' "$hosts" | grep -qx "$only" && return 0
  err "host '$only' is not in your reachable set:"; printf '%s\n' "$hosts" >&2; exit 1
}

# Discover reachable peers. Scope (TUIMUX_SCOPE, default "mine"):
#   mine — your own machines (same tailnet owner) PLUS any host you've mapped a
#          login for, even if a teammate owns it (so shared boxes you use appear).
#   org  — every online machine in the tailnet, whoever owns it (the team fleet).
discover_hosts() {
  if [ -n "$TUIMUX_HOSTS" ]; then
    printf '%s\n' $TUIMUX_HOSTS
    return
  fi
  have tailscale || { err "tailscale not found"; return 1; }
  local self_ip owner scope mapped
  scope="${TUIMUX_SCOPE:-mine}"
  self_ip="$(ts_ip)"
  [ -n "$self_ip" ] || { err "could not determine this machine's tailscale IP"; return 1; }
  owner="$(ts_status | awk -v ip="$self_ip" '$1==ip {print $3; exit}')"
  [ -n "$owner" ] || { err "could not determine your tailnet owner from 'tailscale status'"; return 1; }
  mapped="$(mapped_host_keys | tr '\n' ' ')"
  printf '%s\n' "$(self_host)"           # this machine first — always reachable
  ts_status | awk -v ip="$self_ip" -v owner="$owner" -v scope="$scope" -v mapped="$mapped" '
    BEGIN { n=split(mapped, a, " "); for (i=1;i<=n;i++) M[a[i]]=1 }
    $1!=ip {
      if ($0 ~ /offline/) next                          # only currently-up machines
      if ($3==owner || scope=="org" || ($2 in M)) print $2
    }'
}

# Same-owner machines Tailscale currently reports as offline, with elapsed time:
#   host \t <last-seen text, e.g. "2h ago">  (may be empty if unknown)
# Empty when TUIMUX_HOSTS pins the set (we can't tell which are down) — those
# fall back to showing offline via a failed probe instead.
offline_hosts() {
  [ -n "$TUIMUX_HOSTS" ] && return 0
  have tailscale || return 0
  local self_ip owner scope mapped
  scope="${TUIMUX_SCOPE:-mine}"
  self_ip="$(ts_ip)"
  [ -n "$self_ip" ] || return 0
  owner="$(ts_status | awk -v ip="$self_ip" '$1==ip {print $3; exit}')"
  [ -n "$owner" ] || return 0
  mapped="$(mapped_host_keys | tr '\n' ' ')"
  ts_status | awk -v ip="$self_ip" -v owner="$owner" -v scope="$scope" -v mapped="$mapped" '
    BEGIN { n=split(mapped, a, " "); for (i=1;i<=n;i++) M[a[i]]=1 }
    $1!=ip && /offline/ && ($3==owner || scope=="org" || ($2 in M)) {
      seen=""; n2=index($0, "last seen ")
      if (n2>0) { seen=substr($0, n2+10); sub(/[[:space:]]+$/, "", seen) }
      print $2 "\t" seen
    }'
}

# Consumer devices (phones/tablets/TVs) only *consume* the tailnet — they never
# run code, so we never SSH into them; the dashboard just shows online/offline
# and parks them at the end of the list. Classify by the OS column ($4) of the
# cached tailscale snapshot.
host_os() {
  ts_status | awk -v n="$1" '$2==n {print $4; exit}'
}
# Tailnet owner of a host (column 3, e.g. "arnau@"), trailing '@' stripped for
# display. Empty if the host isn't in the snapshot.
host_owner() {
  ts_status | awk -v n="$1" '$2==n {o=$3; sub(/@$/,"",o); print o; exit}'
}
is_consumer() {
  case "$(host_os "$1" | tr '[:upper:]' '[:lower:]')" in
    ios|android|androidtv|tvos) return 0 ;;
    *) return 1 ;;
  esac
}

# ----- session probe ---------------------------------------------------------
# Remote probe: list sessions (S|…) plus which sessions are running Claude (C|…).
# Claude shows up as a `claude` process under a pane's shell, so we scan pane
# children — folded into one SSH round-trip.
REMOTE_PROBE='command -v tmux >/dev/null 2>&1 || { echo __NOTMUX__; exit 0; }
tmux list-sessions -F "S|#{session_name}|#{?session_attached,1,0}|#{session_windows}|#{pane_current_path}|#{pane_current_command}|#{session_created}" 2>/dev/null
tmux list-windows -a -F "W|#{session_name}|#{window_index}|#{window_name}|#{?window_active,1,0}" 2>/dev/null
tmux list-clients -F "L|#{client_session}|#{client_termname}" 2>/dev/null
tmux list-panes -a -F "#{session_name} #{pane_id} #{pane_pid}" 2>/dev/null | while read s pane ppid; do
  for ch in $(pgrep -P "$ppid" 2>/dev/null); do
    case "$(ps -p "$ch" -o comm= 2>/dev/null)" in
      *claude*)
        echo "C|$s"
        # "esc to interrupt" shows only while Claude is generating → working; else waiting
        if tmux capture-pane -p -t "$pane" 2>/dev/null | grep -qi "esc to interrupt"; then
          echo "A|$s|working"
        else
          echo "A|$s|waiting"
        fi
        break ;;
    esac
  done
done
true'

# ----- machine-readable backend for the Textual UI ---------------------------
# List machines, tab-separated:
#   host \t islocal \t status \t lastseen \t kind \t owner \t mapping \t probe \t color \t login
#   kind    "compute" (we SSH into it) or "consumer" (phone/tablet — status only)
#   owner   tailnet owner login (e.g. "arnau"), for the org-fleet view
#   mapping explicit per-host login, or empty (lets the UI tell mapped from default)
#   probe   1 if tuimux should SSH-probe it (local, your own, or mapped), else 0 —
#           org-view hosts you have no account on are listed but not probed
#   color   the machine's accent (host_color) — UI uses it so the tmux bar matches
#   login   resolved SSH username (login_for) — who we connect as on this host
# Self leads; Tailscale-offline ones follow; the dashboard re-sorts consumers last.
hosts_data() {
  local h hosts name seen me myowner kind owner mapping probe color login
  # one tailscale snapshot for the whole call (exported so the subshells below
  # reuse it via ts_status/ts_ip), and resolve "me"/owner once instead of per host.
  export _EOS_TS_STATUS _EOS_TS_IP
  _EOS_TS_STATUS="$(tailscale status 2>/dev/null)"
  _EOS_TS_IP="$(tailscale ip -4 2>/dev/null | head -1)"
  hosts="$(discover_hosts)" || return 1
  me="$(self_host)"
  myowner="$(host_owner "$me")"
  for h in $hosts; do
    is_consumer "$h" && kind=consumer || kind=compute
    owner="$(host_owner "$h")"; [ -n "$owner" ] || owner="$myowner"
    mapping="$(mapping_for "$h")"
    color="$(host_color "$h")"
    login="$(login_for "$h")"
    # probe our own machines + anything we've mapped a login for; skip the rest
    if [ "$h" = "$me" ] || [ "$owner" = "$myowner" ] || [ -n "$mapping" ]; then probe=1; else probe=0; fi
    [ "$h" = "$me" ] && printf '%s\t1\tonline\t\t%s\t%s\t%s\t%s\t%s\t%s\n' "$h" "$kind" "$owner" "$mapping" "$probe" "$color" "$login" \
                     || printf '%s\t0\tonline\t\t%s\t%s\t%s\t%s\t%s\t%s\n' "$h" "$kind" "$owner" "$mapping" "$probe" "$color" "$login"
  done
  offline_hosts | while IFS="$(printf '\t')" read -r name seen; do
    [ -n "$name" ] || continue
    is_consumer "$name" && kind=consumer || kind=compute
    owner="$(host_owner "$name")"; [ -n "$owner" ] || owner="$myowner"
    mapping="$(mapping_for "$name")"
    color="$(host_color "$name")"
    login="$(login_for "$name")"
    printf '%s\t0\toffline\t%s\t%s\t%s\t%s\t0\t%s\t%s\n' "$name" "$seen" "$kind" "$owner" "$mapping" "$color" "$login"
  done
}

# Raw probe output for one host (S|/W|/L|/A|/C| lines); first line OK or UNREACHABLE.
probe_host() {
  local out
  if out="$(rssh "$1" "$REMOTE_PROBE" 2>/dev/null)"; then printf 'OK\n%s\n' "$out"; else echo UNREACHABLE; fi
}

# Read-only snapshot of a session's active pane, for the dashboard's live preview.
# `-p` → stdout, `-e` → keep colour escapes (the UI renders them). Non-intrusive;
# empty output if the session/host is gone. Local via sh, remote via Tailscale SSH.
peek_pane() {
  local host="$1" session="$2"
  [ -n "$host" ] && [ -n "$session" ] || return 0
  rssh "$host" "tmux capture-pane -p -e -t '$session' 2>/dev/null"
}

# ----- attach / new ----------------------------------------------------------
# Run an action; when $4 = exec, replace the process (CLI). Otherwise return
# control to the caller after the SSH session ends (TUI home loop).
do_attach() {
  local host="$1" session="$2" action="$3" mode="${4:-return}" cmd nm
  case "$action" in
    attach) note "attaching to $session on $host …"
            cmd="$(tmux_opts)tmux attach -t '$session'" ;;
    new)    nm="$session"; [ "$nm" = "$NEW_SENTINEL" ] && nm="$(docker_name)"
            note "opening session '$nm' on $host …"
            cmd="$(tmux_opts)tmux new -A -s '$nm' -c \"\$HOME\"" ;;
    none)   err "$host is unreachable."; return 1 ;;
    *)      return 0 ;;
  esac
  cmd="$(status_style "$host")$cmd"   # tint the bar with the machine's colour
  if is_local "$host"; then
    surface "$mode" sh -c "$cmd"
  else
    # Force a portable TERM so remote tmux doesn't choke on xterm-ghostty.
    surface "$mode" env TERM="$TUIMUX_REMOTE_TERM" \
      ssh -t "${SSH_OPTS[@]}" "$(login_for "$host")@$host" "$cmd"
  fi
}

# Run the attach/new command for a spawned terminal tab/window. In exec mode the
# tab is meant to *become* ssh+tmux — but if that command fails (host
# unreachable, no such session, …) we keep the tab open showing the exit code
# instead of letting it vanish before the error can be read. A clean detach
# (exit 0) still closes the tab. In return mode (TUI home loop) just run it.
surface() {
  local mode="$1"; shift
  "$@"
  local ec=$?
  [ "$mode" = exec ] || return "$ec"
  [ "$ec" -eq 0 ] && exit 0
  printf '\n\033[31m[tuimux] terminal exited (code %s).\033[0m Press Enter to close … ' "$ec" >&2
  read -r _ </dev/tty 2>/dev/null || true
  exit "$ec"
}

# `tuimux attach [name]` — put the CURRENT terminal into a (local) tmux session,
# so it persists and shows up in the dashboard. With no name it starts a fresh
# auto-named session; with a name it creates that one or re-attaches if it
# exists. Same create-or-attach path (titles + status colour) as the dashboard.
attach_here() {
  have tmux || { err "tmux is not installed here."; exit 1; }
  local name="${1:-$(docker_name)}"
  do_attach "$(self_host)" "$name" new exec
}

# `tuimux detach` — detach THIS terminal from its tmux session: the session keeps
# running in the background (and stays in the dashboard) while the terminal drops
# back to a normal shell. The local counterpart to the dashboard's detach (which
# detaches a named session on any host). A friendly no-op when not inside tmux.
detach_here() {
  [ -n "${TMUX:-}" ] || { err "Not inside a tmux session — nothing to detach."; return 0; }
  tmux detach-client
}

# Detach a session: drop any attached clients so its tab closes, but keep it
# running in the background.
detach_session() {
  local host="$1" session="$2"
  case "$session" in "$NEW_SENTINEL"|"$NONE_SENTINEL") return 0 ;; esac
  rssh "$host" "tmux detach-client -s '$session' 2>/dev/null"
}

# tmux's own interactive control panel (choose-tree): the live session/window
# tree where arrows navigate, enter switches, x kills, etc.
tmux_browse() {            # runs inside the spawned tab; takes it over into tmux
  local host="$1" cmd
  # attach to a real session (not the keep-awake helper), then open the tree
  cmd='t=$(tmux ls -F "#{session_name}" 2>/dev/null | grep -vx "'"$AWAKE_SESSION"'" | head -n1)
if [ -n "$t" ]; then exec tmux attach -t "$t" \; choose-tree -Zs; else exec tmux attach \; choose-tree -Zs; fi'
  if is_local "$host"; then exec sh -c "$cmd"
  else exec env TERM="$TUIMUX_REMOTE_TERM" ssh -t "${SSH_OPTS[@]}" "$(login_for "$host")@$host" "$cmd"; fi
}

# Open the Tailscale admin console (machines page) in the default browser —
# handy for checking online/DNS/ACL state when a host shows up as "no ssh".
open_console() {
  local url="https://login.tailscale.com/admin/machines"
  if have open; then open "$url"
  elif have xdg-open; then xdg-open "$url"
  else err "no browser opener found (need 'open' or 'xdg-open')"; fi
}

# ----- spawning new terminal surfaces ----------------------------------------
# Opening a session launches a new terminal tab/window that execs straight into
# ssh+tmux. macOS drives Ghostty / Apple Terminal.app via AppleScript (osascript);
# Linux launches a terminal binary (GNOME Terminal, or a configured command).
# Everything routes through term_spawn / term_focus / list_windows, each of which
# branches on os_kind, so the rest of the engine stays platform-agnostic.

# AppleScript string-escape: backslash and double-quote.
osa_escape() {
  local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; printf '%s' "$s"
}

# The shell command a spawned surface runs: `exec [env hint] <argv…>`, each
# argument shell-quoted so names with spaces survive. The env hint carries our
# tailnet identity into the fresh shell: it doesn't inherit the dashboard's env,
# and "$(hostname -s)" can differ from the tailnet name (e.g. Miquels-MacBook-Pro
# vs miquel-macbook-pro). Without it the new session could decide "this isn't me"
# and try to SSH to the local machine by a name that doesn't resolve — the tab
# would just flash and die.
build_exec_cmd() {
  local out="exec" a
  [ -n "${TUIMUX_SELF_HOST:-}" ] && printf -v out 'exec env TUIMUX_SELF_HOST=%q' "$TUIMUX_SELF_HOST"
  for a in "$@"; do printf -v out '%s %q' "$out" "$a"; done
  printf '%s' "$out"
}

# Launch a spawned surface's argv. With TUIMUX_DRY_RUN set we just print the exact
# command (shell-quoted) and succeed — used to verify the Linux launchers without a
# GUI. Otherwise run it detached so the dashboard never blocks waiting on it.
do_spawn() {
  if [ -n "${TUIMUX_DRY_RUN:-}" ]; then
    local q a; for a in "$@"; do printf -v q '%s %q' "${q:-}" "$a"; done
    printf '[dry-run]%s\n' "$q"; return 0
  fi
  ( "$@" >/dev/null 2>&1 & )
}

# Linux: launch a new terminal surface running <argv…> (the engine re-invocation).
# $1 = "tab" or "window". GNOME Terminal gets real tabs (--tab) against its running
# server; a TUIMUX_TERM_CMD template covers any other terminal; otherwise we fall
# back to x-terminal-emulator/$TERMINAL (window only). The TUIMUX_SELF_HOST identity
# hint rides along exactly as on macOS (here as a literal `env VAR=val` argv prefix,
# since VTE runs the argv directly — no shell to interpret it).
# X11: raise the dashboard's own window so a following `gnome-terminal --tab` (which
# opens into the server's active window) lands beside the panel, not in whatever
# window is frontmost. Prefer the window id the dashboard captured at startup
# (TUIMUX_SELF_WINID); fall back to matching the "tuimux" title (VTE does expose the
# escape-set title in WM_NAME). Best-effort: a no-op on Wayland or without the tools.
linux_raise_self() {
  [ "$(linux_display)" = x11 ] || return 0
  if [ -n "${TUIMUX_SELF_WINID:-}" ]; then
    have wmctrl && wmctrl -i -a "$TUIMUX_SELF_WINID" 2>/dev/null && return 0
    have xdotool && xdotool windowactivate "$TUIMUX_SELF_WINID" 2>/dev/null && return 0
  fi
  have wmctrl && wmctrl -a "$TUIMUX_SELF_TITLE" 2>/dev/null
}

linux_spawn() {
  local mode="$1"; shift
  # env-hint as an argv prefix (empty if unset). The ${env[@]+…} guard keeps an
  # empty array from tripping `set -u` on bash 3.2.
  local env=()
  [ -n "${TUIMUX_SELF_HOST:-}" ] && env=(env "TUIMUX_SELF_HOST=$TUIMUX_SELF_HOST")
  case "$(linux_term)" in
    gnome)
      local flag=--window
      # A new tab must land in the dashboard's window; bring it forward first.
      [ "$mode" = tab ] && { flag=--tab; linux_raise_self; }
      do_spawn gnome-terminal "$flag" -- ${env[@]+"${env[@]}"} "$@" ;;
    custom)
      # TUIMUX_TERM_CMD is a template run via `sh -c`; {cmd} ← the engine command
      # as a single shell-quoted token, so the author wraps it for their terminal,
      # e.g. 'kitty -e sh -c {cmd}' or 'wezterm start -- sh -c {cmd}'.
      local cmd resolved; printf -v cmd '%q' "$(build_exec_cmd "$@")"
      resolved="${TUIMUX_TERM_CMD//\{cmd\}/$cmd}"
      do_spawn sh -c "$resolved" ;;
    *)
      # Generic: most terminals take `-e <cmd> <args…>` for a new window. We pass
      # argv directly (no shell), so the engine binary runs as the surface process.
      local term="${TERMINAL:-x-terminal-emulator}"
      do_spawn "$term" -e ${env[@]+"${env[@]}"} "$@" ;;
  esac
}

# Ghostty: drive its own keybind via AppleScript (needs Accessibility), then type
# the command — $1 = "t" (new tab) or "n" (new window). Ghostty on macOS can't
# open a tab/window with a command straight from the CLI, hence the keystroke
# dance. Falls back to `open` (a single window) if AppleScript can't drive it —
# note: NO `-n`, so it reuses the running instance rather than spawning a second.
# Speed: Ghostty is already frontmost (we run inside it), so the activate pause is
# tiny; the bigger wait is for the new shell to be ready — TUIMUX_SPAWN_DELAY.
# Bring the dashboard's own Ghostty window to the front so a following Cmd-T adds
# its tab there. Matched by the exact "tuimux" tab/window title; best-effort — if
# it can't be found we leave focus alone and the tab just opens in the front window.
ghostty_raise_self() {
  osascript >/dev/null 2>&1 <<OSA
tell application "System Events" to tell process "ghostty"
  set target to missing value
  repeat with w in windows
    set hit to false
    try
      repeat with r in radio buttons of tab group 1 of w
        if (name of r) is "$TUIMUX_SELF_TITLE" then set hit to true
      end repeat
    end try
    if (not hit) and (name of w is "$TUIMUX_SELF_TITLE") then set hit to true
    if hit then
      set target to w
      exit repeat
    end if
  end repeat
  if target is missing value then error "no self window"
  try
    perform action "AXRaise" of target
  end try
  set frontmost to true
end tell
OSA
}

ghostty_spawn() {
  local key="$1"; shift
  local env=()
  [ -n "${TUIMUX_SELF_HOST:-}" ] && env=(env "TUIMUX_SELF_HOST=$TUIMUX_SELF_HOST")
  # A new tab must land in the dashboard's window, not whatever's frontmost
  # (e.g. a window you opened a moment ago). New windows are exempt — they're new.
  [ "$key" = t ] && ghostty_raise_self
  osascript >/dev/null 2>&1 \
    -e 'tell application "Ghostty" to activate' \
    -e 'delay 0.04' \
    -e "tell application \"System Events\" to keystroke \"$key\" using command down" \
    -e "delay $TUIMUX_SPAWN_DELAY" \
    -e "tell application \"System Events\" to keystroke \"exec ${env[*]:+${env[*]} }$*\"" \
    -e 'tell application "System Events" to key code 36' \
    || open -a Ghostty.app --args -e "${env[@]}" "$@"
}

# Apple Terminal.app: its AppleScript dictionary runs a command directly with
# `do script` — `do script "cmd"` opens a new WINDOW. For a TAB we make an empty
# one with Cmd-T (System Events), then run the command in the front window's new
# (selected) tab via `do script … in front window`. Cmd-T is what reliably creates
# a *new* tab: `do script … in <window>` won't add a tab while that window's tab is
# busy (and the dashboard's tab always is), so we don't use it here.
#
# Targeting the dashboard's own window for a tab: unlike Ghostty, Terminal.app does
# NOT expose Textual's title to AppleScript (the dashboard window shows only login +
# size), so we can't find it by name. Instead the dashboard captured its window id
# at startup (TUIMUX_SELF_WINID, via __selfwin) while it was frontmost; we bring
# exactly that window to the front before Cmd-T. Falls back to the front window if
# the id is unknown or stale (closed) — degrading to the old behaviour, never erroring.
terminal_spawn() {
  local mode="$1"; shift
  local esc; esc="$(osa_escape "$(build_exec_cmd "$@")")"
  if [ "$mode" = tab ]; then
    local pin=""
    [ -n "${TUIMUX_SELF_WINID:-}" ] && pin="
  try
    set index of window id ${TUIMUX_SELF_WINID} to 1
  end try"
    osascript >/dev/null 2>&1 <<OSA && return 0
tell application "Terminal"$pin
  activate
end tell
tell application "System Events" to keystroke "t" using command down
delay $TUIMUX_SPAWN_DELAY
tell application "Terminal" to do script "$esc" in front window
OSA
  fi
  osascript >/dev/null 2>&1 \
    -e 'tell application "Terminal" to activate' \
    -e "tell application \"Terminal\" to do script \"$esc\""
}

# Spawn dispatcher: mode is "tab" or "window"; the rest is the command + its args.
term_spawn() {
  local mode="$1"; shift
  # This surface is ours and carries its own attach command, so tell the
  # `tuimux autostart` rc snippet to leave the new shell alone (it consumes the
  # marker and skips auto-attaching). Harmless when autostart is off.
  mkdir -p "$(dirname "$TUIMUX_SKIP_AUTOSTART")" 2>/dev/null && : > "$TUIMUX_SKIP_AUTOSTART" 2>/dev/null || true
  if [ "$(os_kind)" = linux ]; then linux_spawn "$mode" "$@"; return; fi
  case "$(term_kind)" in
    ghostty) case "$mode" in window) ghostty_spawn n "$@" ;; *) ghostty_spawn t "$@" ;; esac ;;
    *)       terminal_spawn "$mode" "$@" ;;
  esac
}

# Note: spawned surfaces re-enter the engine via `bash <engine> …` rather than
# the `tuimux` wrapper, so the new tab skips a whole Python interpreter startup
# (~150ms) before it can attach.
open_browse() {            # open a new surface that drops into tmux's control panel
  term_spawn tab bash "$ENGINE_FILE" __browse "$1"
}

# List the local terminal's windows/tabs and which window each lives in, so the
# dashboard can show where a session is open ("this window" vs another). Output:
# "<win>|<title>" per line, <win> being the window order (z-order). Best-effort:
# if it can't be read, the UI just falls back to showing the attaching term type.

# Ghostty: tabs are radio buttons in each window's tab group; single-tab windows
# have no tab group, so we fall back to the window name.
ghostty_windows() {
  osascript 2>/dev/null <<'OSA'
tell application "System Events"
  if not (exists process "ghostty") then return ""
  tell process "ghostty"
    set out to ""
    set i to 0
    repeat with w in windows
      set i to i + 1
      set n to 0
      try
        repeat with r in radio buttons of tab group 1 of w
          set out to out & i & "|" & (name of r) & linefeed
          set n to n + 1
        end repeat
      end try
      if n is 0 then
        try
          set out to out & i & "|" & (name of w) & linefeed
        end try
      end if
    end repeat
    return out
  end tell
end tell
OSA
}

# Terminal.app: each window's title bar reflects its selected tab's title (the
# "#S · #W" string tmux sets via set-titles), so one line per window suffices —
# tuimux opens sessions as separate windows here.
terminal_windows() {
  osascript 2>/dev/null <<'OSA'
tell application "Terminal"
  set out to ""
  set i to 0
  repeat with w in windows
    set i to i + 1
    try
      set out to out & i & "|" & (name of w) & linefeed
    end try
  end repeat
  return out
end tell
OSA
}

# Linux/X11: list terminal windows via wmctrl. `wmctrl -lx` gives
# "<id> <desk> <WM_CLASS> <host> <title…>"; we keep windows whose class looks like a
# terminal and emit "<n>|<title>". Empty on Wayland or without wmctrl — the UI then
# falls back to the attaching terminal type. wmctrl sees only the active tab's title
# per window, so background tabs won't be located (documented limitation).
linux_windows() {
  { [ "$(linux_display)" = x11 ] && have wmctrl; } || return 0
  wmctrl -lx 2>/dev/null | awk '
    tolower($3) ~ /term/ {
      title = ""
      for (i = 5; i <= NF; i++) title = title (i > 5 ? " " : "") $i
      print ++n "|" title
    }'
}

list_windows() {
  if [ "$(os_kind)" = linux ]; then linux_windows; return; fi
  case "$(term_kind)" in
    ghostty) ghostty_windows ;;
    *)       terminal_windows ;;
  esac
}

# Identifier of the dashboard's OWN window, captured once at startup (while it's
# frontmost) and handed back to spawns as TUIMUX_SELF_WINID so "new tab" can target
# this exact window. Only the terminals that hide the dashboard title need it:
#   • Terminal.app → the AppleScript window id (its title isn't matchable later)
#   • Linux/X11    → the focused X11 window id (xdotool), used by linux_raise_self
# Ghostty matches by the visible "tuimux" title instead, so it needs nothing here.
self_winid() {
  if [ "$(os_kind)" = darwin ]; then
    [ "$(term_kind)" = terminal ] || return 0
    osascript -e 'tell application "Terminal" to id of front window' 2>/dev/null
  elif [ "$(linux_display)" = x11 ] && have xdotool; then
    xdotool getactivewindow 2>/dev/null
  fi
}

# Focus an existing surface whose title matches the session name (surfaces are
# titled "#S · #W" via set-titles) and raise it. Returns 0 if focused. Too-short
# names (0,1) are ambiguous — don't guess.

# Ghostty: click the matching tab (multi-tab windows) or match the window title
# (single-tab windows), then AXRaise that window.
ghostty_focus() {
  local n="$1"
  [ ${#n} -ge 3 ] || return 1
  osascript >/dev/null 2>&1 <<OSA
tell application "System Events" to tell process "ghostty"
  set target to missing value
  repeat with w in windows
    set found to false
    try
      repeat with r in radio buttons of tab group 1 of w
        if (name of r) contains "$n" then
          click r
          set found to true
          exit repeat
        end if
      end repeat
    end try
    if (not found) and (name of w contains "$n") then set found to true
    if found then
      set target to w
      exit repeat
    end if
  end repeat
  if target is missing value then error "not found"
  try
    perform action "AXRaise" of target
  end try
  set frontmost to true
end tell
OSA
}

# Terminal.app: match a window title (or a tab's custom title), select that tab,
# bring its window to front. No Accessibility needed — all via Terminal's own API.
terminal_focus() {
  local n="$1"
  [ ${#n} -ge 3 ] || return 1
  osascript >/dev/null 2>&1 <<OSA
tell application "Terminal"
  set target to missing value
  repeat with w in windows
    if (name of w) contains "$n" then
      set target to w
      exit repeat
    end if
    repeat with t in tabs of w
      try
        if (custom title of t) contains "$n" then
          set selected of t to true
          set target to w
          exit repeat
        end if
      end try
    end repeat
    if target is not missing value then exit repeat
  end repeat
  if target is missing value then error "not found"
  set index of target to 1
  activate
end tell
OSA
}

# Linux/X11: raise the window whose title matches the session via wmctrl (or
# xdotool). Returns non-zero on Wayland / without the tools, so open_surface's auto
# mode cleanly falls through to opening a fresh surface instead of erroring.
linux_focus() {
  local n="$1"
  [ ${#n} -ge 3 ] || return 1
  [ "$(linux_display)" = x11 ] || return 1
  if have wmctrl; then
    wmctrl -a "$n"
  elif have xdotool; then
    local id; id="$(xdotool search --name "$n" 2>/dev/null | head -1)"
    [ -n "$id" ] && xdotool windowactivate "$id" >/dev/null 2>&1
  else
    return 1
  fi
}

term_focus() {
  if [ "$(os_kind)" = linux ]; then linux_focus "$@"; return; fi
  case "$(term_kind)" in
    ghostty) ghostty_focus "$@" ;;
    *)       terminal_focus "$@" ;;
  esac
}

# Open a session in a new terminal surface. The new surface runs `tuimux
# __attach …`, which execs straight into ssh+tmux.
open_surface() {
  local mode="$1" host="$2" session="$3" action="$4"
  case "$action" in attach|new) ;; *) return 0 ;; esac   # ignore headers/spacers
  case "$mode" in
    auto)
      # already open in a surface? jump to it. otherwise open a fresh tab.
      if [ "$action" = "attach" ] && term_focus "$session"; then return 0; fi
      open_surface tab "$host" "$session" "$action" ;;
    window) term_spawn window bash "$ENGINE_FILE" __attach "$host" "$session" "$action" ;;
    tab)    term_spawn tab    bash "$ENGINE_FILE" __attach "$host" "$session" "$action" ;;
  esac
}

# ----- keep-awake ------------------------------------------------------------
# prints: on | off | "" (unreachable)
awake_state() {
  rssh "$1" "tmux has-session -t '$AWAKE_SESSION' 2>/dev/null && echo on || echo off" 2>/dev/null
}

# Turn keep-awake on for a host (OS-appropriate keeper inside a tmux session).
awake_on() {
  local host="$1" out
  out="$(rssh "$host" '
    os=$(uname)
    if [ "$os" = Darwin ]; then
      keeper="caffeinate -dimsu"
    elif command -v systemd-inhibit >/dev/null 2>&1; then
      keeper="systemd-inhibit --what=sleep:idle --who=tuimux --why=keep-awake sleep infinity"
    else
      echo __UNSUPPORTED__; exit 0
    fi
    tmux new -d -s '"'$AWAKE_SESSION'"' $keeper 2>/dev/null && echo __ON__ || echo __FAIL__
  ' 2>/dev/null)"
  case "$out" in
    *__ON__*)          note "$host: keep-awake ON" ;;
    *__UNSUPPORTED__*) err  "$host: no supported keep-awake method (need caffeinate or systemd-inhibit)" ;;
    *)                 err  "$host: failed to enable keep-awake" ;;
  esac
}

awake_off() {
  rssh "$1" "tmux kill-session -t '$AWAKE_SESSION' 2>/dev/null" && note "$1: keep-awake OFF"
}

toggle_awake() {
  local host="$1" st
  st="$(awake_state "$host")"
  case "$st" in
    on)  awake_off "$host" ;;
    off) awake_on  "$host" ;;
    *)   err "$host is unreachable." ;;
  esac
}

# ----- init (auto-tmux on login) ---------------------------------------------
autotmux_snippet() {
cat <<SNIP
# >>> tuimux auto-tmux >>>
# Auto-attach interactive SSH logins to a persistent tmux session, so every
# terminal opened here is discoverable / re-attachable via tuimux.
# Skip for one connection with:  TUIMUX_NO_AUTOTMUX=1 ssh <host>
if [ -n "\$SSH_CONNECTION" ] && [ -z "\$TMUX" ] && [ -z "\$TUIMUX_NO_AUTOTMUX" ] && command -v tmux >/dev/null 2>&1; then
  case \$- in *i*) tmux set -g mouse on 2>/dev/null; tmux new -A -s '$TUIMUX_DEFAULT_SESSION' ;; esac
fi
# <<< tuimux auto-tmux <<<
SNIP
}

init_host() {
  local host="${1:-}" rc snippet ans
  [ -n "$host" ] || { err "usage: tuimux init <host>"; exit 1; }
  require_reachable_host "$host"
  rc="$(rssh "$host" 'case "$SHELL" in *zsh) echo "$HOME/.zshrc";; *bash) echo "$HOME/.bashrc";; *) echo "$HOME/.profile";; esac')"
  [ -n "$rc" ] || { err "could not determine remote shell rc on $host"; exit 1; }
  if rssh "$host" "grep -q 'tuimux auto-tmux' '$rc' 2>/dev/null"; then
    note "$host already initialized ($rc) — nothing to do."; return 0
  fi
  snippet="$(autotmux_snippet)"
  printf '\nWill append to %s:%s\n\n%s\n\n' "$host" "$rc" "$snippet"
  printf 'Proceed? [y/N] '; read -r ans
  case "$ans" in y|Y|yes|YES) ;; *) note "aborted — nothing changed."; return 1 ;; esac
  if printf '%s\n' "$snippet" | rssh "$host" "cat >> '$rc'"; then
    note "done — new SSH logins to $host will auto-attach to tmux '$TUIMUX_DEFAULT_SESSION'."
    note "skip it for one session with:  TUIMUX_NO_AUTOTMUX=1 ssh $(login_for "$host")@$host"
  else
    err "failed to write $rc on $host"; exit 1
  fi
}

# ----- autostart (auto-tmux for every LOCAL terminal) ------------------------
# The local counterpart to `init`: instead of SSH logins on a remote, every new
# interactive terminal on THIS machine drops into its own fresh tmux session.

# Which shell family we're configuring (drives the bash login-shell hardening).
rc_kind() {
  case "${SHELL:-}" in *zsh) echo zsh ;; *bash) echo bash ;; *) echo other ;; esac
}

# Shell rc this user's INTERACTIVE shells read. TUIMUX_RC overrides it (tests).
#   zsh  → ~/.zshrc       (sourced by every interactive zsh, login or not)
#   bash → ~/.bashrc      (interactive non-login; login shells need the link below)
#   else → ~/.profile
default_rc() {
  case "$(rc_kind)" in
    zsh)  printf '%s' "$HOME/.zshrc" ;;
    bash) printf '%s' "$HOME/.bashrc" ;;
    *)    printf '%s' "$HOME/.profile" ;;
  esac
}

# bash login shells (e.g. macOS Terminal.app / Ghostty) read .bash_profile (or
# .bash_login / .profile), NOT .bashrc — so the autostart block in .bashrc wouldn't
# run there unless one of those sources .bashrc. This is the file we ensure does.
# TUIMUX_LOGIN_RC overrides it (tests).
bash_login_profile() {
  local f
  for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
    [ -f "$f" ] && { printf '%s' "$f"; return; }
  done
  printf '%s' "$HOME/.bash_profile"   # none exist → the default login file to create
}

# Block helpers: present? / append (idempotent, keeps a separating newline) /
# remove the marked region. index() matches the markers literally (no regex).
_block_present() { grep -qF "$2" "$1" 2>/dev/null; }   # $1=file $2=begin-marker
_block_append() {                          # $1=file $2=snippet-fn [extra args → fn]
  local f="$1" fn="$2"; shift 2
  [ -s "$f" ] && [ -n "$(tail -c1 "$f" 2>/dev/null)" ] && printf '\n' >> "$f"
  "$fn" "$@" >> "$f"
}
_block_remove() {                                        # $1=file $2=begin $3=end
  local tmp; [ -f "$1" ] || return 0
  tmp="$(mktemp)" || return 1
  awk -v b="$2" -v e="$3" 'index($0,b){s=1} !s; index($0,e){s=0}' "$1" > "$tmp" \
    && mv "$tmp" "$1" || { rm -f "$tmp"; return 1; }
}

# Login-shell link block, added to .bash_profile/.profile so bash login shells pull
# in .bashrc (and thus the autostart block). Only added when that file doesn't
# already reference .bashrc — we never duplicate an existing source.
autostart_login_snippet() {
cat <<'SNIP'
# >>> tuimux autostart (login) >>>
# Load .bashrc in login shells (macOS Terminal.app/Ghostty) so tuimux autostart runs there too.
[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"
# <<< tuimux autostart (login) <<<
SNIP
}

# The rc block installed by `tuimux autostart on`. $1 = the ABSOLUTE path to the
# tuimux binary (baked in at install time): a fresh terminal usually hasn't
# activated the conda env tuimux lives in, so a bare `tuimux` wouldn't be on PATH —
# the absolute path always resolves. It generates the same happy-curie names tuimux
# uses elsewhere (`__autoname`), and if it can't run for any reason the session
# falls back to tmux's numbering. The heredoc is UNQUOTED so the path interpolates;
# every live shell var is escaped (\$) to stay literal in your rc. set-titles match
# the dashboard's window detection ("go to"); the skip-marker is how tuimux's OWN
# spawned tabs opt out (see term_spawn).
autostart_snippet() {
  local self="${1:-tuimux}"
cat <<SNIP
# >>> tuimux autostart >>>
# Auto-attach every new interactive terminal to its own tmux session, so it
# persists and shows up in the tuimux dashboard. Toggle: tuimux autostart off
# Skip once (e.g. to launch the dashboard itself): TUIMUX_NO_AUTOTMUX=1 <command>
if [ -z "\$TMUX" ] && [ -z "\$SSH_CONNECTION" ] && [ -z "\$TUIMUX_NO_AUTOTMUX" ] \\
   && command -v tmux >/dev/null 2>&1; then
  case \$- in *i*)
    if [ -e "\$HOME/.cache/tuimux/skip-autostart" ]; then
      command rm -f "\$HOME/.cache/tuimux/skip-autostart"   # this tab was opened by tuimux
    else
      _tx_name="\$('$self' __autoname 2>/dev/null || command tuimux __autoname 2>/dev/null)"
      tmux set -g set-titles on; tmux set -g set-titles-string '#S · #W'
      # An explicit if, NOT a ":+" alternate-value expansion: zsh doesn't word-split
      # that, so tmux would get one "-s name" token → a " name" with a leading space.
      if [ -n "\$_tx_name" ]; then tmux new-session -s "\$_tx_name" 2>/dev/null || tmux new-session
      else tmux new-session; fi
      unset _tx_name
    fi ;;
  esac
fi
# <<< tuimux autostart <<<
SNIP
}

# Machine-readable autostart state for the dashboard: prints "on" or "off".
autostart_state() {
  local rc; rc="${TUIMUX_RC:-$(default_rc)}"
  _block_present "$rc" '# >>> tuimux autostart >>>' && echo on || echo off
}

autostart() {
  local action="${1:-status}" rc lp kind did=0
  rc="${TUIMUX_RC:-$(default_rc)}"
  kind="$(rc_kind)"
  lp="${TUIMUX_LOGIN_RC:-$(bash_login_profile)}"
  case "$action" in
    on)
      have tmux || { err "tmux is not installed here."; exit 1; }
      _block_present "$rc" '# >>> tuimux autostart >>>' \
        || { _block_append "$rc" autostart_snippet "$SELF" || { err "failed to write $rc"; exit 1; }; did=1; }
      # bash login shells (macOS) read .bash_profile/.profile, not .bashrc — make
      # sure one of them sources .bashrc, unless it already references it.
      if [ "$kind" = bash ] \
         && ! _block_present "$lp" '# >>> tuimux autostart (login) >>>' \
         && ! grep -q 'bashrc' "$lp" 2>/dev/null; then
        _block_append "$lp" autostart_login_snippet && did=1
      fi
      if [ "$did" = 1 ]; then
        note "autostart ON ($rc) — every new terminal now attaches to its own tmux session."
        [ "$kind" = bash ] && _block_present "$lp" '# >>> tuimux autostart (login) >>>' \
          && note "(also linked $lp → .bashrc so login shells pick it up.)"
        note "launch the dashboard itself without it:  TUIMUX_NO_AUTOTMUX=1 tuimux"
        note "open a new terminal (or restart your shell) for it to take effect."
      else
        note "autostart already on ($rc) — nothing to do."
      fi
      ;;
    off)
      _block_present "$rc" '# >>> tuimux autostart >>>' \
        && { _block_remove "$rc" '# >>> tuimux autostart >>>' '# <<< tuimux autostart <<<' \
             || { err "failed to update $rc"; exit 1; }; did=1; }
      if [ "$kind" = bash ] && _block_present "$lp" '# >>> tuimux autostart (login) >>>'; then
        _block_remove "$lp" '# >>> tuimux autostart (login) >>>' '# <<< tuimux autostart (login) <<<' && did=1
      fi
      [ "$did" = 1 ] && note "autostart OFF ($rc) — new terminals no longer auto-attach." \
                     || note "autostart already off — nothing to do."
      ;;
    status)
      if _block_present "$rc" '# >>> tuimux autostart >>>'; then
        note "autostart: on ($rc)"
      else
        note "autostart: off ($rc)"
      fi
      ;;
    *) err "usage: tuimux autostart on|off|status"; exit 1 ;;
  esac
}

# ----- mouse (tmux mouse mode: wheel scrolls the pane, not shell history) -----
# tmux's config; TUIMUX_TMUX_CONF overrides it (tests). It's read when a tmux
# SERVER starts, so we also apply the change live to any running server.
tmux_conf() { printf '%s' "${TUIMUX_TMUX_CONF:-$HOME/.tmux.conf}"; }

mouse_snippet() {
cat <<'SNIP'
# >>> tuimux mouse >>>
set -g mouse on
# <<< tuimux mouse <<<
SNIP
}

# Machine-readable mouse state for the dashboard: prints "on" or "off".
mouse_state() {
  _block_present "$(tmux_conf)" '# >>> tuimux mouse >>>' && echo on || echo off
}

# `tuimux mouse on|off|status` — persist the setting in ~/.tmux.conf (so new tmux
# servers pick it up) AND apply it to any running server now (so it takes effect
# without restarting tmux). on: wheel scrolls the pane; off: back to the default.
mouse() {
  local action="${1:-status}" conf; conf="$(tmux_conf)"
  case "$action" in
    on)
      if _block_present "$conf" '# >>> tuimux mouse >>>'; then
        note "mouse already on ($conf) — nothing to do."
      else
        _block_append "$conf" mouse_snippet || { err "failed to write $conf"; exit 1; }
        note "mouse ON ($conf) — the wheel now scrolls the pane (hold Option to select text)."
      fi
      tmux set -g mouse on 2>/dev/null   # apply to the running server immediately
      ;;
    off)
      if _block_present "$conf" '# >>> tuimux mouse >>>'; then
        _block_remove "$conf" '# >>> tuimux mouse >>>' '# <<< tuimux mouse <<<' \
          || { err "failed to update $conf"; exit 1; }
        note "mouse OFF ($conf)."
      else
        note "mouse already off — nothing to do."
      fi
      tmux set -g mouse off 2>/dev/null
      ;;
    status)
      _block_present "$conf" '# >>> tuimux mouse >>>' \
        && note "mouse: on ($conf)" || note "mouse: off ($conf)"
      ;;
    *) err "usage: tuimux mouse on|off|status"; exit 1 ;;
  esac
}

# ----- first run -------------------------------------------------------------
# On the very first run (pip can't reliably run post-install hooks, so we do it
# here), turn on the sensible defaults: autostart + mouse scroll. A one-time
# marker means it never repeats — so it won't fight a later `… off`, and upgrades
# don't re-enable. TUIMUX_STATE_DIR overrides the marker location (tests).
first_run_setup() {
  local state marker
  state="${TUIMUX_STATE_DIR:-$HOME/.config/tuimux}"
  marker="$state/initialized"
  [ -e "$marker" ] && return 0
  mkdir -p "$state" 2>/dev/null
  : > "$marker"   # mark first, so a hiccup can't make this loop on every launch
  note "tuimux first run — enabling autostart + mouse scroll by default"
  note "(turn either off any time:  tuimux autostart off  /  tuimux mouse off)"
  autostart on >/dev/null 2>&1
  mouse on >/dev/null 2>&1
}

# ----- doctor ----------------------------------------------------------------
doctor() {
  printf 'tuimux doctor\n==============\n'
  for dep in tailscale ssh tmux; do
    if have "$dep"; then printf '  [ok]   %s\n' "$dep"
    else printf '  [MISS] %s\n' "$dep"; fi
  done
  echo
  local self_ip owner hosts h
  self_ip="$(tailscale ip -4 2>/dev/null | head -1)"
  owner="$(tailscale status 2>/dev/null | awk -v ip="$self_ip" '$1==ip {print $3; exit}')"
  printf 'this machine: %s  (owner %s)\n' "${self_ip:-?}" "${owner:-?}"
  printf 'login user:   %s  (default; unmapped hosts use this)\n' "$TUIMUX_LOGIN"
  if [ -n "$TUIMUX_LOGINS" ]; then
    printf 'per-host:     %s\n' "$TUIMUX_LOGINS"
  fi
  # how this machine will open sessions in new terminal surfaces
  if [ "$(os_kind)" = linux ]; then
    local lt ld jump
    lt="$(linux_term)"; ld="$(linux_display)"
    case "$lt" in
      gnome)   lt="GNOME Terminal (tabs + windows)" ;;
      custom)  lt="custom: $TUIMUX_TERM_CMD" ;;
      generic) lt="${TERMINAL:-x-terminal-emulator} (windows)"; have "${TERMINAL:-x-terminal-emulator}" || lt="$lt — NOT FOUND" ;;
    esac
    if [ "$ld" = x11 ] && { have wmctrl || have xdotool; }; then jump="jump-to-window + OPEN IN: yes"
    elif [ "$ld" = x11 ]; then jump="jump-to-window + OPEN IN: install wmctrl or xdotool"
    else jump="jump-to-window + OPEN IN: unavailable on Wayland (always a new surface)"; fi
    printf 'terminal:     linux → %s\n' "$lt"
    printf 'display:      %s — %s\n\n' "$ld" "$jump"
  elif have osascript; then
    case "$(term_kind)" in
      ghostty) printf 'terminal:     %s → new tabs/windows via Ghostty\n\n' "${TERM_PROGRAM:-?}" ;;
      *)       printf 'terminal:     %s → new windows via Apple Terminal.app\n\n' "${TERM_PROGRAM:-?}" ;;
    esac
  else
    printf 'terminal:     opening sessions in new tabs/windows needs macOS (osascript)\n\n'
  fi
  hosts="$(discover_hosts)" || return 1
  printf 'machines:\n'
  for h in $hosts; do
    local probe
    if is_local "$h"; then
      if have tmux; then printf '  [ok]   %-15s this machine (local)\n' "$h"
      else printf '  [warn] %-15s this machine — tmux NOT installed\n' "$h"; fi
      continue
    fi
    probe="$(rssh "$h" 'command -v tmux >/dev/null 2>&1 && echo tmux-ok || echo tmux-missing' 2>/dev/null)"
    case "$probe" in
      tmux-ok)      printf '  [ok]   %-15s ssh + tmux  (as %s)\n' "$h" "$(login_for "$h")" ;;
      tmux-missing) printf '  [warn] %-15s ssh ok, tmux NOT installed  (as %s)\n' "$h" "$(login_for "$h")" ;;
      *)            printf '  [FAIL] %-15s no Tailscale SSH as %s (run "sudo tailscale up --ssh" there + check ACLs)\n' "$h" "$(login_for "$h")" ;;
    esac
  done
}

# A flat catalog of every device in the tailnet (whoever owns it), up or down —
# the team fleet at a glance, without launching the dashboard.
cmd_devices() {
  have tailscale || { err "tailscale not found"; exit 1; }
  printf '%-22s %-10s %-9s %-6s %s\n' HOST OWNER OS STATE 'LAST SEEN'
  tailscale status 2>/dev/null | awk '
    $1=="" { next }
    {
      host=$2; owner=$3; sub(/@$/,"",owner); os=$4
      state="up"; seen=""
      if ($0 ~ /offline/) {
        state="down"; n=index($0,"last seen ")
        if (n>0) { seen=substr($0,n+10); sub(/[[:space:]]+$/,"",seen) }
      }
      printf "%-22s %-10s %-9s %-6s %s\n", host, owner, os, state, seen
    }'
}

# ----- per-host logins -------------------------------------------------------
# Persist KEY="VALUE" into the config file, creating it (and its dir) if needed
# and replacing any existing assignment so the line never duplicates. VALUE is
# only ever the validated, space-joined login map, so plain double-quoting is safe.
set_config_kv() {
  local key="$1" val="$2" tmp
  mkdir -p "$(dirname "$CONFIG_FILE")" 2>/dev/null || true
  [ -f "$CONFIG_FILE" ] || : > "$CONFIG_FILE"
  tmp="$(mktemp "${TMPDIR:-/tmp}/tuimux.XXXXXX")" || { err "could not write config"; return 1; }
  awk -v k="$key" -v v="$val" '$0 ~ "^"k"=" {next} {print} END {print k"=\""v"\""}' \
    "$CONFIG_FILE" > "$tmp" && mv "$tmp" "$CONFIG_FILE"
}

# A login token must be a bare host/user name — no spaces, '=', or quotes — so it
# round-trips safely through the space-separated, '='-delimited TUIMUX_LOGINS map.
valid_login_token() { case "$1" in ""|*[!A-Za-z0-9._-]*) return 1 ;; *) return 0 ;; esac; }

# Current TUIMUX_LOGINS with the given host's mapping dropped (space-separated).
logins_without() {
  local drop="$1" tok out=""
  for tok in $TUIMUX_LOGINS; do
    case "$tok" in "$drop="*) ;; *=?*) out="$out${out:+ }$tok" ;; esac
  done
  printf '%s' "$out"
}

# `tuimux login` — manage the per-host SSH username map (TUIMUX_LOGINS).
#   tuimux login                  list current mappings
#   tuimux login <host> <user>    set/replace one
#   tuimux login --rm <host>      remove one
cmd_login() {
  local tok host user
  case "${1:-}" in
    "")
      if [ -z "$TUIMUX_LOGINS" ]; then
        printf 'No per-host logins set — every host uses "%s".\n' "$TUIMUX_LOGIN"
      else
        printf 'Per-host SSH logins (unmapped hosts use "%s"):\n' "$TUIMUX_LOGIN"
        for tok in $TUIMUX_LOGINS; do
          case "$tok" in *=?*) printf '  %-20s %s\n' "${tok%%=*}" "${tok#*=}" ;; esac
        done
      fi ;;
    --rm)
      host="${2:-}"
      [ -n "$host" ] || { err "usage: tuimux login --rm <host>"; exit 1; }
      set_config_kv TUIMUX_LOGINS "$(logins_without "$host")" \
        && note "removed login mapping for $host"
      ;;
    *)
      host="$1"; user="${2:-}"
      [ -n "$user" ] || { err "usage: tuimux login <host> <user>"; exit 1; }
      valid_login_token "$host" || { err "invalid host name: '$host'"; exit 1; }
      valid_login_token "$user" || { err "invalid user name: '$user'"; exit 1; }
      local base; base="$(logins_without "$host")"
      set_config_kv TUIMUX_LOGINS "${base:+$base }$host=$user" \
        && note "set $host → $user"
      ;;
  esac
}

# ----- dispatch --------------------------------------------------------------
case "${1:-}" in
  attach        ) shift; attach_here "${1:-}" ;;
  detach        ) detach_here ;;
  autostart     ) shift; autostart "${1:-}" ;;
  mouse         ) shift; mouse "${1:-status}" ;;
  init          ) shift; init_host "${1:-}" ;;
  login         ) shift; cmd_login "$@" ;;
  devices       ) cmd_devices ;;
  doctor        ) doctor ;;
  # ----- backend called by the Textual UI -----
  __hosts       ) shift; [ "${1:-}" = org ] && export TUIMUX_SCOPE=org; hosts_data ;;
  __probe       ) shift; probe_host "${1:-}" ;;
  __peek        ) shift; peek_pane "${1:-}" "${2:-}" ;;
  __login       ) printf '%s\n' "$TUIMUX_LOGIN" ;;
  __loginfor    ) shift; login_for "${1:-}"; echo ;;
  # set a mapping (host user); an empty user removes it
  __setlogin    ) shift; if [ -n "${2:-}" ]; then cmd_login "${1:-}" "${2:-}" >/dev/null 2>&1; else cmd_login --rm "${1:-}" >/dev/null 2>&1; fi ;;
  __attach      ) shift; do_attach "${1:-}" "${2:-}" "${3:-}" exec ;;
  __open        ) shift; open_surface "${1:-}" "${2:-}" "${3:-}" "${4:-}" ;;
  __detach      ) shift; detach_session "${1:-}" "${2:-}" ;;
  __killraw     ) shift; rssh "${1:-}" "tmux kill-session -t '${2:-}' 2>/dev/null" ;;
  __renameto    ) shift; rssh "${1:-}" "tmux rename-session -t '${2:-}' '${3:-}'" ;;
  __browse      ) shift; tmux_browse "${1:-}" ;;
  __openbrowse  ) shift; open_browse "${1:-}" ;;
  __windows     ) list_windows ;;
  __selfwin     ) self_winid ;;
  __autoname    ) docker_name ;;
  __autostart   ) autostart_state ;;
  __mouse       ) mouse_state ;;
  __firstrun    ) first_run_setup ;;
  __console     ) open_console ;;
  __awaketoggle ) shift; toggle_awake "${1:-}" >/dev/null 2>&1 ;;
  -h|--help|"" ) usage ;;
  *             ) usage; exit 1 ;;
esac
