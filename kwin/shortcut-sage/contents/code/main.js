/*
 * Shortcut Sage - KWin script
 *
 * Reports desktop actions to the Shortcut Sage daemon over DBus and keeps the
 * suggestion overlay pinned to the top-left corner (Wayland clients cannot position
 * themselves, so the compositor has to do it).
 *
 * Works on KWin 6 (Plasma 6) and KWin 5 (Plasma 5.27). Written in ES5 style so it runs
 * on every QJSEngine KWin ships with.
 *
 * Every event carries the window title ("window") and application class ("app") of
 * the focused window: the daemon needs them for app/title specific rules. They are not
 * printed to the journal unless the script's "debug" config key is true.
 *
 * Emitted actions (must match action IDs in shortcuts.yaml):
 *   window_focus, show_desktop, tile_left, tile_right, tile_top, tile_bottom,
 *   tile_top_left, tile_top_right, tile_bottom_left, tile_bottom_right, maximize,
 *   restore, minimize, switch_desktop_next, switch_desktop_prev, switch_desktop,
 *   move_window_right_desktop, move_window_left_desktop, krunner, test_shortcut
 */

var SAGE_SERVICE = "org.shortcutsage.Daemon";
var SAGE_PATH = "/org/shortcutsage/Daemon";
var SAGE_INTERFACE = "org.shortcutsage.Daemon";

var OVERLAY_APP_ID = "shortcut-sage-overlay";
var OVERLAY_TITLE = "Shortcut Sage Overlay";
var OVERLAY_MARGIN = 20;

var SHOW_DESKTOP_DEBOUNCE_MS = 750;
var DUPLICATE_WINDOW_MS = 400;
var MAXIMIZE_AREA = (typeof KWin !== "undefined" && KWin.MaximizeArea !== undefined) ? KWin.MaximizeArea : 2;

var DEBUG = false;
try {
    DEBUG = readConfig("debug", false) === true || readConfig("debug", false) === "true";
} catch (e) {
    DEBUG = false;
}

var IS_KWIN6 = typeof workspace.windowList === "function";

var state = {
    lastActive: null,
    lastShowDesktop: 0,
    lastWindowMove: 0,
    lastEmitted: {},          // window id -> {action, time}
    windowDesktop: {},        // window id -> desktop index
    pendingTile: {},          // window id -> true while waiting for geometry
    connected: {}             // window id -> true once per-window signals are hooked
};

function log(msg) {
    if (DEBUG) {
        print("[shortcut-sage] " + msg);
    }
}

// --------------------------------------------------------------------------- API shims
function activeWindow() {
    return IS_KWIN6 ? workspace.activeWindow : workspace.activeClient;
}

function setActiveWindow(w) {
    if (IS_KWIN6) {
        workspace.activeWindow = w;
    } else {
        workspace.activeClient = w;
    }
}

function windowList() {
    return IS_KWIN6 ? workspace.windowList() : workspace.clientList();
}

function connectSignal(obj, name, handler) {
    if (obj && obj[name] && typeof obj[name].connect === "function") {
        obj[name].connect(handler);
        return true;
    }
    return false;
}

function windowId(w) {
    if (!w) {
        return "";
    }
    if (w.internalId !== undefined && w.internalId !== null) {
        return String(w.internalId);
    }
    return String(w.windowId);
}

function desktopCount() {
    if (IS_KWIN6) {
        return workspace.desktops ? workspace.desktops.length : 1;
    }
    return workspace.desktops || 1;
}

function desktopIndex(desktop) {
    // KWin 6: VirtualDesktop object. KWin 5: 1-based desktop number.
    if (desktop === null || desktop === undefined) {
        return -1;
    }
    if (typeof desktop === "number") {
        return desktop - 1;
    }
    var list = workspace.desktops || [];
    for (var i = 0; i < list.length; i++) {
        if (list[i] === desktop || (list[i].id !== undefined && list[i].id === desktop.id)) {
            return i;
        }
    }
    return -1;
}

function currentDesktopIndex() {
    return desktopIndex(workspace.currentDesktop);
}

function windowDesktopIndex(w) {
    if (!w || w.onAllDesktops) {
        return -1;
    }
    if (IS_KWIN6) {
        var desktops = w.desktops || [];
        return desktops.length === 1 ? desktopIndex(desktops[0]) : -1;
    }
    return typeof w.desktop === "number" && w.desktop > 0 ? w.desktop - 1 : -1;
}

function direction(fromIndex, toIndex) {
    var n = desktopCount();
    if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex || n < 2) {
        return 0;
    }
    if (toIndex === (fromIndex + 1) % n) {
        return 1;
    }
    if (toIndex === (fromIndex - 1 + n) % n) {
        return -1;
    }
    return 2; // jump
}

function clientArea(w) {
    try {
        if (w) {
            return workspace.clientArea(MAXIMIZE_AREA, w);
        }
        return workspace.clientArea(MAXIMIZE_AREA, workspace.activeScreen, workspace.currentDesktop);
    } catch (e) {
        return null;
    }
}

// --------------------------------------------------------------------------- helpers
function isOverlay(w) {
    if (!w) {
        return false;
    }
    var cls = String(w.resourceClass || "").toLowerCase();
    var name = String(w.resourceName || "").toLowerCase();
    var desktopFile = String(w.desktopFileName || "").toLowerCase();
    return cls === OVERLAY_APP_ID || name === OVERLAY_APP_ID || desktopFile === OVERLAY_APP_ID ||
        String(w.caption || "") === OVERLAY_TITLE;
}

function isManagedAppWindow(w) {
    if (!w || isOverlay(w)) {
        return false;
    }
    if (w.specialWindow || w.desktopWindow || w.dock || w.popupWindow) {
        return false;
    }
    return w.normalWindow !== false || w.dialog === true;
}

function windowMeta(w) {
    var meta = {};
    if (!w) {
        return meta;
    }
    meta.window = String(w.caption || "");
    meta.app = String(w.resourceClass || "");
    if (w.desktopFileName) {
        meta.desktop_file = String(w.desktopFileName);
    }
    return meta;
}

function contextMeta(extra) {
    var meta = windowMeta(activeWindow());
    if (extra) {
        for (var key in extra) {
            if (Object.prototype.hasOwnProperty.call(extra, key)) {
                meta[key] = extra[key];
            }
        }
    }
    return meta;
}

function sendEvent(type, action, metadata) {
    var payload = JSON.stringify({
        timestamp: new Date().toISOString(),
        type: type,
        action: action,
        metadata: metadata || {}
    });
    log("event " + type + "/" + action);
    callDBus(SAGE_SERVICE, SAGE_PATH, SAGE_INTERFACE, "SendEvent", payload);
}

function sendWindowEvent(w, type, action, extra) {
    // Collapse duplicates (e.g. quickTileModeChanged + geometry change for one action)
    var id = windowId(w);
    var now = Date.now();
    var last = state.lastEmitted[id];
    if (last && last.action === action && now - last.time < DUPLICATE_WINDOW_MS) {
        return;
    }
    state.lastEmitted[id] = { action: action, time: now };
    var meta = windowMeta(w);
    if (extra) {
        for (var key in extra) {
            if (Object.prototype.hasOwnProperty.call(extra, key)) {
                meta[key] = extra[key];
            }
        }
    }
    sendEvent(type, action, meta);
}

// --------------------------------------------------------------------------- tiling
function near(a, b, tolerance) {
    return Math.abs(a - b) <= tolerance;
}

function classifyTile(w) {
    var area = clientArea(w);
    var g = w.frameGeometry;
    if (!area || !g || area.width <= 0 || area.height <= 0) {
        return null;
    }
    var tolX = Math.max(8, area.width * 0.04);
    var tolY = Math.max(8, area.height * 0.04);

    var left = near(g.x, area.x, tolX);
    var right = near(g.x + g.width, area.x + area.width, tolX);
    var top = near(g.y, area.y, tolY);
    var bottom = near(g.y + g.height, area.y + area.height, tolY);
    var halfW = near(g.width, area.width / 2, tolX);
    var halfH = near(g.height, area.height / 2, tolY);
    var fullW = near(g.width, area.width, tolX);
    var fullH = near(g.height, area.height, tolY);

    if (halfW && fullH && top && bottom) {
        if (left) { return "tile_left"; }
        if (right) { return "tile_right"; }
    }
    if (fullW && halfH && left && right) {
        if (top) { return "tile_top"; }
        if (bottom) { return "tile_bottom"; }
    }
    if (halfW && halfH) {
        if (top && left) { return "tile_top_left"; }
        if (top && right) { return "tile_top_right"; }
        if (bottom && left) { return "tile_bottom_left"; }
        if (bottom && right) { return "tile_bottom_right"; }
    }
    return null;
}

function maybeEmitTile(w) {
    var action = classifyTile(w);
    if (action) {
        delete state.pendingTile[windowId(w)];
        sendWindowEvent(w, "window_state", action);
    }
    return action;
}

// --------------------------------------------------------------------------- overlay
function placeOverlay(w) {
    if (!w) {
        return;
    }
    try {
        w.keepAbove = true;
        w.skipTaskbar = true;
        w.skipPager = true;
        w.skipSwitcher = true;
        w.onAllDesktops = true;
    } catch (e) {
        log("could not set overlay flags: " + e);
    }
    var area = clientArea(null);
    var g = w.frameGeometry;
    if (!area || !g) {
        return;
    }
    var x = area.x + OVERLAY_MARGIN;
    var y = area.y + OVERLAY_MARGIN;
    if (Math.abs(g.x - x) > 1 || Math.abs(g.y - y) > 1) {
        w.frameGeometry = { x: x, y: y, width: g.width, height: g.height };
    }
}

function findOverlay() {
    var list = windowList() || [];
    for (var i = 0; i < list.length; i++) {
        if (isOverlay(list[i])) {
            return list[i];
        }
    }
    return null;
}

// --------------------------------------------------------------------------- per-window hooks
function hookWindow(w) {
    if (!w) {
        return;
    }
    var id = windowId(w);
    if (state.connected[id]) {
        return;
    }
    state.connected[id] = true;

    if (isOverlay(w)) {
        placeOverlay(w);
        connectSignal(w, "frameGeometryChanged", function () { placeOverlay(w); });
        return;
    }

    state.windowDesktop[id] = windowDesktopIndex(w);

    // Show desktop (KWin 6): windows get hidden by "show desktop"
    // (the hiddenByShowDesktop property is not exported to scripts, only its signal;
    // show desktop is a toggle on one shortcut, so both directions count)
    connectSignal(w, "hiddenByShowDesktopChanged", function () {
        if (w.hiddenByShowDesktop !== false) {
            emitShowDesktop();
        }
    });

    // Quick tiling
    connectSignal(w, "quickTileModeChanged", function () {
        if (!maybeEmitTile(w)) {
            state.pendingTile[id] = true; // geometry not updated yet
        }
    });
    connectSignal(w, "frameGeometryChanged", function () {
        if (state.pendingTile[id]) {
            maybeEmitTile(w);
        }
    });

    // Maximize (KWin 6). KWin 5 uses workspace.clientMaximizeSet below.
    connectSignal(w, "maximizedAboutToChange", function (mode) {
        if (mode === 3) {
            sendWindowEvent(w, "window_state", "maximize");
        } else if (mode === 0) {
            sendWindowEvent(w, "window_state", "restore");
        }
    });

    // Minimize (both versions expose minimizedChanged on the window)
    connectSignal(w, "minimizedChanged", function () {
        if (w.minimized && isManagedAppWindow(w) && Date.now() - state.lastShowDesktop > SHOW_DESKTOP_DEBOUNCE_MS) {
            sendWindowEvent(w, "window_state", "minimize");
        }
    });

    // Window moved to another virtual desktop
    var onDesktopChange = function () {
        var before = state.windowDesktop[id];
        var after = windowDesktopIndex(w);
        state.windowDesktop[id] = after;
        var dir = direction(before, after);
        if (dir === 1 || dir === -1) {
            state.lastWindowMove = Date.now();
        }
        if (dir === 1) {
            sendWindowEvent(w, "window_desktop", "move_window_right_desktop", { desktop: after + 1 });
        } else if (dir === -1) {
            sendWindowEvent(w, "window_desktop", "move_window_left_desktop", { desktop: after + 1 });
        }
    };
    if (!connectSignal(w, "desktopsChanged", onDesktopChange)) {
        connectSignal(w, "desktopChanged", onDesktopChange);
    }

    connectSignal(w, "closed", function () { forgetWindow(w); });
}

function forgetWindow(w) {
    var id = windowId(w);
    delete state.connected[id];
    delete state.windowDesktop[id];
    delete state.pendingTile[id];
    delete state.lastEmitted[id];
    if (state.lastActive === w) {
        state.lastActive = null;
    }
}

function emitShowDesktop() {
    var now = Date.now();
    if (now - state.lastShowDesktop < SHOW_DESKTOP_DEBOUNCE_MS) {
        return;
    }
    state.lastShowDesktop = now;
    sendEvent("desktop_state", "show_desktop", contextMeta());
}

// --------------------------------------------------------------------------- workspace hooks
function onWindowAdded(w) {
    hookWindow(w);
}

function onWindowRemoved(w) {
    forgetWindow(w);
}

function onWindowActivated(w) {
    if (!w) {
        return;
    }
    if (isOverlay(w)) {
        // The overlay must never steal focus: hand it back.
        if (state.lastActive && state.lastActive !== w) {
            setActiveWindow(state.lastActive);
        }
        return;
    }
    var overlay = findOverlay();
    if (overlay) {
        placeOverlay(overlay); // follow the active screen
    }

    var cls = String(w.resourceClass || "").toLowerCase();
    if (cls === "krunner" || cls === "org.kde.krunner") {
        sendEvent("launcher", "krunner", windowMeta(w));
        return;
    }
    if (!isManagedAppWindow(w)) {
        return;
    }
    state.lastActive = w;
    sendEvent("window_focus", "window_focus", windowMeta(w));
}

function onCurrentDesktopChanged(previous, movedWindow) {
    // KWin 6: previous is a VirtualDesktop. KWin 5: previous desktop number and the
    // window that was moved along with the switch (if any).
    var before = desktopIndex(previous);
    var after = currentDesktopIndex();
    var overlay = findOverlay();
    if (overlay) {
        placeOverlay(overlay);
    }
    if ((!IS_KWIN6 && movedWindow) || Date.now() - state.lastWindowMove < DUPLICATE_WINDOW_MS) {
        return; // "window to next desktop" also switches; already reported as a move
    }
    var dir = direction(before, after);
    var action = dir === 1 ? "switch_desktop_next" : dir === -1 ? "switch_desktop_prev" : dir === 2 ? "switch_desktop" : null;
    if (action) {
        sendEvent("desktop_switch", action, contextMeta({ desktop: after + 1, previous_desktop: before + 1 }));
    }
}

function setupWorkspace() {
    if (IS_KWIN6) {
        connectSignal(workspace, "windowAdded", onWindowAdded);
        connectSignal(workspace, "windowRemoved", onWindowRemoved);
        connectSignal(workspace, "windowActivated", onWindowActivated);
    } else {
        connectSignal(workspace, "clientAdded", onWindowAdded);
        connectSignal(workspace, "clientRemoved", onWindowRemoved);
        connectSignal(workspace, "clientActivated", onWindowActivated);
        connectSignal(workspace, "clientMaximizeSet", function (client, horizontal, vertical) {
            if (client && !isOverlay(client)) {
                sendWindowEvent(client, "window_state", horizontal && vertical ? "maximize" : "restore");
            }
        });
    }
    connectSignal(workspace, "currentDesktopChanged", onCurrentDesktopChanged);
    // Older KWin versions expose show-desktop on the workspace itself.
    connectSignal(workspace, "showingDesktopChanged", function (showing) {
        if (showing) {
            emitShowDesktop();
        }
    });

    var existing = windowList() || [];
    for (var i = 0; i < existing.length; i++) {
        hookWindow(existing[i]);
    }
    var current = activeWindow();
    if (current && !isOverlay(current)) {
        state.lastActive = current;
    }
}

function setupTestShortcut() {
    registerShortcut(
        "Shortcut Sage: Test Event",
        "Shortcut Sage: send a test event to the daemon",
        "Ctrl+Alt+S",
        function () {
            sendEvent("test", "test_shortcut", contextMeta({ source: "kwin_script" }));
        }
    );
}

function init() {
    setupWorkspace();
    setupTestShortcut();
    print("Shortcut Sage KWin script loaded (KWin " + (IS_KWIN6 ? "6" : "5") + " API)");
}

init();
