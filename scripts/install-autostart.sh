#!/usr/bin/env bash
# Start the Shortcut Sage daemon and overlay automatically with Plasma.
#
# Usage: scripts/install-autostart.sh [--uninstall] [--xdg]
#   --xdg   use XDG autostart .desktop files instead of systemd user units
#
# The shortcut-sage executable must be installed (pip install --user . or pipx).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="${CONFIG_HOME}/systemd/user"
AUTOSTART_DIR="${CONFIG_HOME}/autostart"
UNITS=(shortcut-sage-daemon.service shortcut-sage-overlay.service)
DESKTOPS=(shortcut-sage-daemon.desktop shortcut-sage-overlay.desktop)

mode=systemd
uninstall=false
for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall=true ;;
        --xdg) mode=xdg ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done
if [[ "$mode" == systemd ]] && ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "systemd user session not available; using XDG autostart instead"
    mode=xdg
fi

if $uninstall; then
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user disable --now "${UNITS[@]}" 2>/dev/null || true
    fi
    for u in "${UNITS[@]}"; do rm -f "${UNIT_DIR}/${u}"; done
    for d in "${DESKTOPS[@]}"; do rm -f "${AUTOSTART_DIR}/${d}"; done
    systemctl --user daemon-reload 2>/dev/null || true
    echo "Shortcut Sage autostart removed."
    exit 0
fi

BIN="$(command -v shortcut-sage || true)"
if [[ -z "$BIN" ]]; then
    echo "error: 'shortcut-sage' is not on PATH. Install it first (e.g. pipx install .)" >&2
    exit 1
fi
BINDIR="$(dirname "$BIN")"

if [[ "$mode" == systemd ]]; then
    mkdir -p "$UNIT_DIR"
    for u in "${UNITS[@]}"; do
        sed "s|@BINDIR@|${BINDIR}|g" "${REPO_ROOT}/packaging/systemd/${u}" > "${UNIT_DIR}/${u}"
    done
    systemctl --user daemon-reload
    systemctl --user enable --now "${UNITS[@]}"
    echo "Installed systemd user units. Logs: journalctl --user -u shortcut-sage-daemon -f"
else
    mkdir -p "$AUTOSTART_DIR"
    for d in "${DESKTOPS[@]}"; do
        sed "s|^Exec=shortcut-sage|Exec=${BIN}|" "${REPO_ROOT}/packaging/autostart/${d}" > "${AUTOSTART_DIR}/${d}"
    done
    echo "Installed XDG autostart entries in ${AUTOSTART_DIR} (take effect at next login)."
fi
