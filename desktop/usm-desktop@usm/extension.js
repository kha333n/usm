// usm-desktop: tells USM (the memory guard and its logger) which app is in front, how long the user
// has been idle and how late GNOME Shell's own main loop runs (desktop lag). Writes one small file,
// $XDG_RUNTIME_DIR/usm-desktop (tmpfs), every 2 s and at once when the focus changes. No window titles.
// Also adds USM to Quick Settings (panel.js): switches, status, quick actions.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {UsmIndicator, selfTest} from './panel.js';

const TICK_MS = 250;
const WRITE_EVERY = 8;   // ticks: every 2 s

export default class UsmDesktop extends Extension {
    enable() {
        this._path = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'usm-desktop']);
        this._lagMax = 0;
        this._ticks = 0;
        this._last = GLib.get_monotonic_time();
        this._tracker = Shell.WindowTracker.get_default();
        this._focusId = global.display.connect('notify::focus-window', () => this._write());
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, TICK_MS, () => {
            const now = GLib.get_monotonic_time();
            const lag = Math.round((now - this._last) / 1000) - TICK_MS;   // how late this tick ran
            this._last = now;
            if (lag > this._lagMax)
                this._lagMax = lag;
            if (++this._ticks >= WRITE_EVERY)
                this._write();
            return GLib.SOURCE_CONTINUE;
        });
        this._write();
        // The panel is extra: if it fails, the focus reporting above (which the guard needs) keeps working.
        try {
            this._panel = new UsmIndicator();
            Main.panel.statusArea.quickSettings.addExternalIndicator(this._panel);
            if (GLib.getenv('USM_DESKTOP_SELFTEST'))
                selfTest(this._panel);
        } catch (e) {
            console.error(`usm-desktop: Quick Settings panel failed: ${e}`);
            this._panel?.destroy();
            this._panel = null;
        }
    }

    disable() {
        this._panel?.destroy();
        this._panel = null;
        if (this._focusId)
            global.display.disconnect(this._focusId);
        if (this._timer)
            GLib.source_remove(this._timer);
        this._focusId = this._timer = 0;
        this._tracker = null;
        try {
            Gio.File.new_for_path(this._path).delete(null);
        } catch (e) {}
    }

    _write() {
        this._ticks = 0;
        let pid = 0, app = '', idle = '';
        try {
            const w = global.display.focus_window;
            if (w) {
                pid = w.get_pid();
                const a = this._tracker.get_window_app(w);
                app = (a && a.get_id()) || w.get_wm_class() || '';
            }
        } catch (e) {}
        try {
            idle = global.backend.get_core_idle_monitor().get_idletime();
        } catch (e) {}
        const text = `t=${Math.floor(Date.now() / 1000)}\nfocus_pid=${pid}\n` +
            `focus_app=${String(app).replace(/[\s,=]/g, '_')}\nidle_ms=${idle}\n` +
            `lag_max_ms=${Math.max(0, this._lagMax)}\n`;
        this._lagMax = 0;
        try {
            GLib.file_set_contents(this._path, text);
        } catch (e) {}
    }
}
