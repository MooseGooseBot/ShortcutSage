// Minimal mock of the KWin scripting environment (KWin 6 or KWin 5 flavour) used to
// exercise kwin/shortcut-sage/contents/code/main.js under Node.
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SCRIPT = path.join(__dirname, "..", "..", "kwin", "shortcut-sage", "contents", "code", "main.js");

class Signal {
    constructor() {
        this.handlers = [];
    }
    connect(fn) {
        this.handlers.push(fn);
    }
    emit(...args) {
        for (const fn of this.handlers.slice()) {
            fn(...args);
        }
    }
}

let nextId = 1;

function makeDesktop(index) {
    return { id: "desktop-" + index, name: "Desktop " + (index + 1), x11DesktopNumber: index + 1 };
}

function makeWindow(props, kwin6) {
    const w = Object.assign(
        {
            caption: "Untitled",
            resourceClass: "app",
            resourceName: "app",
            desktopFileName: "",
            normalWindow: true,
            dialog: false,
            specialWindow: false,
            desktopWindow: false,
            dock: false,
            popupWindow: false,
            minimized: false,
            keepAbove: false,
            onAllDesktops: false,
            frameGeometry: { x: 100, y: 100, width: 800, height: 600 },
        },
        props
    );
    w.internalId = "{" + nextId++ + "}";
    const signals = [
        "frameGeometryChanged",
        "quickTileModeChanged",
        "minimizedChanged",
        "closed",
        "hiddenByShowDesktopChanged",
    ];
    if (kwin6) {
        signals.push("maximizedAboutToChange", "desktopsChanged");
    } else {
        signals.push("desktopChanged");
    }
    for (const s of signals) {
        w[s] = new Signal();
    }
    return w;
}

function createEnv(options) {
    const opts = Object.assign({ kwin6: true, desktops: 4, config: {} }, options);
    const kwin6 = opts.kwin6;
    const calls = [];
    const shortcuts = [];
    const printed = [];
    const windows = [];
    const area = { x: 0, y: 0, width: 1920, height: 1040 };

    const desktopObjs = [];
    for (let i = 0; i < opts.desktops; i++) {
        desktopObjs.push(makeDesktop(i));
    }

    const workspace = {
        activeScreen: { name: "DP-1" },
        clientArea: () => Object.assign({}, area),
    };
    const wsSignals = kwin6
        ? ["windowAdded", "windowRemoved", "windowActivated", "currentDesktopChanged"]
        : ["clientAdded", "clientRemoved", "clientActivated", "currentDesktopChanged", "clientMaximizeSet"];
    for (const s of wsSignals) {
        workspace[s] = new Signal();
    }

    let active = null;
    let current = 0;
    if (kwin6) {
        workspace.windowList = () => windows.slice();
        workspace.desktops = desktopObjs;
        Object.defineProperty(workspace, "activeWindow", {
            get: () => active,
            set: (w) => {
                active = w;
            },
        });
        Object.defineProperty(workspace, "currentDesktop", {
            get: () => desktopObjs[current],
            set: (d) => {
                current = desktopObjs.indexOf(d);
            },
        });
    } else {
        workspace.clientList = () => windows.slice();
        workspace.desktops = opts.desktops;
        Object.defineProperty(workspace, "activeClient", {
            get: () => active,
            set: (w) => {
                active = w;
            },
        });
        Object.defineProperty(workspace, "currentDesktop", {
            get: () => current + 1,
            set: (d) => {
                current = d - 1;
            },
        });
    }

    const sandbox = {
        workspace,
        KWin: { MaximizeArea: 2 },
        print: (...args) => printed.push(args.join(" ")),
        readConfig: (key, def) => (key in opts.config ? opts.config[key] : def),
        registerShortcut: (title, text, keys, cb) => shortcuts.push({ title, text, keys, cb }),
        callDBus: (service, objPath, iface, method, ...args) => calls.push({ service, objPath, iface, method, args }),
        Date,
        JSON,
        Math,
        Object,
        String,
    };

    const env = {
        kwin6,
        workspace,
        calls,
        shortcuts,
        printed,
        windows,
        area,
        desktops: desktopObjs,
        load() {
            vm.createContext(sandbox);
            vm.runInContext(fs.readFileSync(SCRIPT, "utf8"), sandbox, { filename: SCRIPT });
            return env;
        },
        events() {
            return calls
                .filter((c) => c.method === "SendEvent")
                .map((c) => JSON.parse(c.args[0]));
        },
        actions() {
            return env.events().map((e) => e.action);
        },
        clear() {
            calls.length = 0;
        },
        addWindow(props, { initial = false } = {}) {
            const w = makeWindow(props, kwin6);
            if (kwin6) {
                w.desktops = props && props.desktops ? props.desktops : [desktopObjs[current]];
            } else {
                w.desktop = props && props.desktop ? props.desktop : current + 1;
            }
            windows.push(w);
            if (!initial) {
                workspace[kwin6 ? "windowAdded" : "clientAdded"].emit(w);
            }
            return w;
        },
        activate(w) {
            active = w;
            workspace[kwin6 ? "windowActivated" : "clientActivated"].emit(w);
        },
        switchDesktop(index, movedWindow) {
            const previous = kwin6 ? desktopObjs[current] : current + 1;
            current = index;
            if (kwin6) {
                workspace.currentDesktopChanged.emit(previous, desktopObjs[current], workspace.activeScreen);
            } else {
                workspace.currentDesktopChanged.emit(previous, movedWindow || null);
            }
        },
        moveWindowToDesktop(w, index) {
            if (kwin6) {
                w.desktops = [desktopObjs[index]];
                w.desktopsChanged.emit();
            } else {
                w.desktop = index + 1;
                w.desktopChanged.emit();
            }
        },
        setGeometry(w, g) {
            const old = w.frameGeometry;
            w.frameGeometry = g;
            w.frameGeometryChanged.emit(old);
        },
    };
    return env;
}

module.exports = { createEnv, Signal };
