// Tests for the Shortcut Sage KWin script. Run with: node --test tests/kwin/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createEnv } = require("./kwin_mock");

const RULE_ACTIONS = [
    "show_desktop",
    "tile_left",
    "tile_right",
    "maximize",
    "switch_desktop_next",
    "switch_desktop_prev",
    "move_window_left_desktop",
    "move_window_right_desktop",
    "krunner",
];

for (const kwin6 of [true, false]) {
    const label = kwin6 ? "KWin 6" : "KWin 5";

    test(`${label}: loads, registers test shortcut, hooks existing windows`, () => {
        const env = createEnv({ kwin6 });
        const existing = env.addWindow({ caption: "Existing" }, { initial: true });
        env.load();
        assert.equal(env.shortcuts.length, 1);
        assert.equal(env.shortcuts[0].keys, "Ctrl+Alt+S");
        assert.ok(env.printed.some((l) => l.includes(kwin6 ? "KWin 6" : "KWin 5")));
        // existing window was hooked: tiling it emits an event
        env.setGeometry(existing, { x: 0, y: 0, width: 960, height: 1040 });
        existing.quickTileModeChanged.emit();
        assert.deepEqual(env.actions(), ["tile_left"]);
    });

    test(`${label}: events go to the daemon with timestamp, window title and app`, () => {
        const env = createEnv({ kwin6 }).load();
        const w = env.addWindow({ caption: "main.py - Kate", resourceClass: "org.kde.kate" });
        env.activate(w);
        const calls = env.calls.filter((c) => c.method === "SendEvent");
        assert.equal(calls.length, 1);
        assert.equal(calls[0].service, "org.shortcutsage.Daemon");
        assert.equal(calls[0].objPath, "/org/shortcutsage/Daemon");
        assert.equal(calls[0].iface, "org.shortcutsage.Daemon");
        const event = JSON.parse(calls[0].args[0]);
        assert.equal(event.type, "window_focus");
        assert.equal(event.action, "window_focus");
        assert.match(event.timestamp, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$/);
        assert.equal(event.metadata.window, "main.py - Kate");
        assert.equal(event.metadata.app, "org.kde.kate");
    });

    test(`${label}: window titles are not printed unless debug is enabled`, () => {
        const env = createEnv({ kwin6 }).load();
        env.activate(env.addWindow({ caption: "Secret document" }));
        assert.ok(!env.printed.some((l) => l.includes("Secret document")));
        assert.ok(!env.printed.some((l) => l.includes("event ")));

        const dbg = createEnv({ kwin6, config: { debug: true } }).load();
        dbg.activate(dbg.addWindow({ caption: "Secret document" }));
        assert.ok(dbg.printed.some((l) => l.includes("event window_focus/window_focus")));
    });

    test(`${label}: quick tiling left/right emits tile actions`, () => {
        const env = createEnv({ kwin6 }).load();
        const w = env.addWindow({});
        // signal before geometry update: emitted once geometry arrives
        w.quickTileModeChanged.emit();
        assert.deepEqual(env.actions(), []);
        env.setGeometry(w, { x: 0, y: 0, width: 960, height: 1040 });
        assert.deepEqual(env.actions(), ["tile_left"]);
        // further geometry changes don't re-emit
        env.setGeometry(w, { x: 10, y: 10, width: 500, height: 500 });
        assert.deepEqual(env.actions(), ["tile_left"]);

        const w2 = env.addWindow({});
        env.setGeometry(w2, { x: 960, y: 0, width: 960, height: 1040 });
        w2.quickTileModeChanged.emit();
        assert.deepEqual(env.actions(), ["tile_left", "tile_right"]);
    });

    test(`${label}: a stale pending tile does not turn a later resize into a tile event`, () => {
        const env = createEnv({ kwin6 }).load();
        const w = env.addWindow({});
        const realNow = Date.now;
        try {
            let now = realNow();
            Date.now = () => now;
            w.quickTileModeChanged.emit(); // e.g. un-tile: geometry is not a tile shape
            now += 5000; // much later the user resizes the window by hand
            env.setGeometry(w, { x: 0, y: 0, width: 960, height: 1040 });
            assert.deepEqual(env.actions(), []);
        } finally {
            Date.now = realNow;
        }
    });

    test(`${label}: quarter tiling is classified`, () => {
        const env = createEnv({ kwin6 }).load();
        const w = env.addWindow({});
        env.setGeometry(w, { x: 960, y: 520, width: 960, height: 520 });
        w.quickTileModeChanged.emit();
        assert.deepEqual(env.actions(), ["tile_bottom_right"]);
    });

    test(`${label}: maximize emits maximize`, () => {
        const env = createEnv({ kwin6 }).load();
        const w = env.addWindow({});
        if (kwin6) {
            w.maximizedAboutToChange.emit(3);
            w.maximizedAboutToChange.emit(0);
        } else {
            env.workspace.clientMaximizeSet.emit(w, true, true);
            env.workspace.clientMaximizeSet.emit(w, false, false);
        }
        assert.deepEqual(env.actions(), ["maximize", "restore"]);
    });

    test(`${label}: desktop switching emits next/prev with wraparound`, () => {
        const env = createEnv({ kwin6, desktops: 4 }).load();
        env.switchDesktop(1);
        env.switchDesktop(0);
        env.switchDesktop(3); // wraps backwards
        env.switchDesktop(0); // wraps forwards
        env.switchDesktop(2); // jump
        assert.deepEqual(env.actions(), [
            "switch_desktop_next",
            "switch_desktop_prev",
            "switch_desktop_prev",
            "switch_desktop_next",
            "switch_desktop",
        ]);
        assert.equal(env.events()[0].metadata.desktop, 2);
    });

    test(`${label}: moving a window to the next desktop is not double-reported`, () => {
        const env = createEnv({ kwin6, desktops: 4 }).load();
        const w = env.addWindow({});
        env.activate(w);
        env.clear();
        env.moveWindowToDesktop(w, 1);
        env.switchDesktop(1, w);
        env.moveWindowToDesktop(w, 0);
        assert.deepEqual(env.actions(), ["move_window_right_desktop", "move_window_left_desktop"]);
    });

    test(`${label}: show desktop is reported once per toggle`, () => {
        const env = createEnv({ kwin6 }).load();
        const a = env.addWindow({});
        const b = env.addWindow({});
        a.hiddenByShowDesktopChanged.emit();
        b.hiddenByShowDesktopChanged.emit();
        assert.deepEqual(env.actions(), ["show_desktop"]);
    });

    test(`${label}: krunner activation emits krunner`, () => {
        const env = createEnv({ kwin6 }).load();
        env.activate(env.addWindow({ resourceClass: "krunner", normalWindow: false }));
        assert.deepEqual(env.actions(), ["krunner"]);
    });

    test(`${label}: panels, docks and popups do not emit focus events`, () => {
        const env = createEnv({ kwin6 }).load();
        env.activate(env.addWindow({ dock: true, resourceClass: "plasmashell" }));
        env.activate(env.addWindow({ popupWindow: true }));
        env.activate(env.addWindow({ desktopWindow: true }));
        assert.deepEqual(env.actions(), []);
    });

    test(`${label}: overlay is ignored, pinned top-left, kept above and never keeps focus`, () => {
        const env = createEnv({ kwin6 }).load();
        const app = env.addWindow({ caption: "Editor" });
        env.activate(app);
        env.clear();
        env.area.x = 1920; // second screen
        const overlay = env.addWindow({
            caption: "Shortcut Sage Overlay",
            resourceClass: "shortcut-sage-overlay",
            frameGeometry: { x: 500, y: 500, width: 400, height: 80 },
        });
        assert.equal(overlay.keepAbove, true);
        assert.equal(overlay.onAllDesktops, true);
        assert.equal(overlay.frameGeometry.x, 1940);
        assert.equal(overlay.frameGeometry.y, 20);
        assert.equal(overlay.frameGeometry.width, 400);
        env.activate(overlay);
        assert.equal(kwin6 ? env.workspace.activeWindow : env.workspace.activeClient, app);
        overlay.quickTileModeChanged.emit();
        overlay.minimizedChanged.emit();
        assert.deepEqual(env.actions(), []);
    });

    test(`${label}: test shortcut sends test_shortcut`, () => {
        const env = createEnv({ kwin6 }).load();
        env.shortcuts[0].cb();
        assert.deepEqual(env.actions(), ["test_shortcut"]);
    });

    test(`${label}: closed windows are forgotten`, () => {
        const env = createEnv({ kwin6 }).load();
        const w = env.addWindow({});
        env.activate(w);
        w.closed.emit();
        env.workspace[kwin6 ? "windowRemoved" : "clientRemoved"].emit(w);
        assert.doesNotThrow(() => env.switchDesktop(1));
    });
}

test("every rule-relevant action is emitted by the script", () => {
    const fs = require("fs");
    const path = require("path");
    const src = fs.readFileSync(
        path.join(__dirname, "..", "..", "kwin", "shortcut-sage", "contents", "code", "main.js"),
        "utf8"
    );
    for (const action of RULE_ACTIONS) {
        assert.ok(src.includes(`"${action}"`), `script never emits ${action}`);
    }
});
