#!/usr/bin/env bash
# Install (or upgrade) and enable the Shortcut Sage KWin script.
#
# Usage: scripts/install-kwin-script.sh [--uninstall]
#
# Works on Plasma 6 (kpackagetool6/kwriteconfig6) and falls back to the Plasma 5 tools.
set -euo pipefail

SCRIPT_ID="shortcut-sage"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_DIR="${REPO_ROOT}/kwin/${SCRIPT_ID}"

pick() {
    for tool in "$@"; do
        if command -v "$tool" >/dev/null 2>&1; then
            echo "$tool"
            return 0
        fi
    done
    return 1
}

KPACKAGETOOL="$(pick kpackagetool6 kpackagetool5)" || {
    echo "error: kpackagetool6 not found (install the KDE Frameworks 'kpackage' tools)" >&2
    exit 1
}
KWRITECONFIG="$(pick kwriteconfig6 kwriteconfig5)" || {
    echo "error: kwriteconfig6 not found" >&2
    exit 1
}

reconfigure_kwin() {
    if command -v qdbus6 >/dev/null 2>&1; then
        qdbus6 org.kde.KWin /KWin reconfigure >/dev/null 2>&1 || true
    elif command -v qdbus >/dev/null 2>&1; then
        qdbus org.kde.KWin /KWin reconfigure >/dev/null 2>&1 || true
    elif command -v dbus-send >/dev/null 2>&1; then
        dbus-send --session --type=method_call --dest=org.kde.KWin /KWin org.kde.KWin.reconfigure >/dev/null 2>&1 || true
    else
        echo "note: could not reach KWin over DBus; log out and back in to load the script"
    fi
}

if [[ "${1:-}" == "--uninstall" ]]; then
    "$KWRITECONFIG" --file kwinrc --group Plugins --key "${SCRIPT_ID}Enabled" false
    reconfigure_kwin
    "$KPACKAGETOOL" --type=KWin/Script --remove "$SCRIPT_ID" || true
    echo "Shortcut Sage KWin script removed."
    exit 0
fi

if [[ ! -f "${PACKAGE_DIR}/metadata.json" ]]; then
    echo "error: ${PACKAGE_DIR}/metadata.json not found" >&2
    exit 1
fi

if "$KPACKAGETOOL" --type=KWin/Script --list 2>/dev/null | grep -qx "$SCRIPT_ID"; then
    echo "Upgrading KWin script '${SCRIPT_ID}'..."
    "$KPACKAGETOOL" --type=KWin/Script --upgrade "$PACKAGE_DIR"
else
    echo "Installing KWin script '${SCRIPT_ID}'..."
    "$KPACKAGETOOL" --type=KWin/Script --install "$PACKAGE_DIR"
fi

# Toggle off/on so KWin reloads the new code even if it was already enabled.
"$KWRITECONFIG" --file kwinrc --group Plugins --key "${SCRIPT_ID}Enabled" false
reconfigure_kwin
"$KWRITECONFIG" --file kwinrc --group Plugins --key "${SCRIPT_ID}Enabled" true
reconfigure_kwin

cat <<MSG
Shortcut Sage KWin script installed and enabled.

Check it is running:
  journalctl --user -f QT_CATEGORY=js QT_CATEGORY=kwin_scripting
  (look for "Shortcut Sage KWin script loaded"; press Ctrl+Alt+S to send a test event)

Verbose event logging (includes window titles) for debugging:
  ${KWRITECONFIG} --file kwinrc --group Script-${SCRIPT_ID} --key debug true
  then re-run this script.
MSG
