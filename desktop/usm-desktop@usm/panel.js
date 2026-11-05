// USM in GNOME's Quick Settings: a "VRAM swap" toggle with a menu (VRAM tier and guard switches, what is where,
// quick actions) and a top-bar pause icon while the guard has an app paused. Reads only world-readable files
// (/proc, /run/usm-*); switching runs /usr/local/lib/usm/usm-helper through pkexec (polkit action org.usm.switch).
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';

const HELPER = '/usr/local/lib/usm/usm-helper';
const USM = '/usr/local/sbin/usm';
const ICON = 'computer-chip-symbolic';
const decoder = new TextDecoder();

function readText(path) {
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        return ok ? decoder.decode(bytes) : '';
    } catch (e) {
        return '';
    }
}

const gib = mb => (mb / 1024).toFixed(1);

function readState() {
    const s = {tier: 'off', guard: 'off', paused: [], pausedMb: 0, stats: {}};
    s.swaps = readText('/proc/swaps').split('\n').slice(1).filter(l => l.trim()).map(l => {
        const f = l.trim().split(/\s+/);
        return {name: f[0], size: Math.round(f[2] / 1024), used: Math.round(f[3] / 1024), prio: +f[4]};
    });
    s.vram = s.swaps.find(x => x.name.startsWith('/dev/ublkb'));
    for (const kv of readText('/run/usm-vramd.stats').trim().split(/\s+/)) {
        const i = kv.indexOf('=');
        if (i > 0)
            s.stats[kv.slice(0, i)] = kv.slice(i + 1);
    }
    if (s.vram)
        s.tier = s.stats.vram_held === '0' ? 'spilled' : 'on';
    s.mem = {};
    for (const l of readText('/proc/meminfo').split('\n')) {
        const m = l.match(/^([\w()]+):\s+(\d+)/);
        if (m)
            s.mem[m[1]] = Math.round(m[2] / 1024);
    }
    try {
        const g = JSON.parse(readText(GLib.getenv('USM_GUARD_STATE') || '/run/usm-guard.state') || '{}');  // env: tests only
        if (g.t && Date.now() / 1000 - g.t < 15) {
            s.guard = g.mode;
            s.paused = g.frozen_apps || [];
            s.pausedMb = g.frozen_mb || 0;
        }
    } catch (e) {}
    return s;
}

function run(argv, done) {
    try {
        const proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE);
        proc.communicate_utf8_async(null, null, (p, res) => {
            let ok = false, out = '';
            try {
                const [, stdout] = p.communicate_utf8_finish(res);
                out = (stdout || '').trim();
                ok = p.get_successful();
            } catch (e) {
                out = e.message;
            }
            done(ok, out);
        });
    } catch (e) {
        done(false, e.message);
    }
}

const BUSY = {
    'tier-on': 'switching on…',
    'tier-off': 'switching off…',
    'guard-on': 'guard: switching on…',
    'guard-off': 'guard: switching off…',
};

const UsmToggle = GObject.registerClass(
class UsmToggle extends QuickMenuToggle {
    constructor(refresh) {
        super({title: 'VRAM swap', subtitle: '', iconName: ICON, toggleMode: false});
        this._refresh = refresh;
        this._busy = null;
        this.menu.setHeader(ICON, 'USM', 'GPU memory as swap · memory guard');

        this._lines = [];
        for (let i = 0; i < 6; i++) {
            const item = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
            this.menu.addMenuItem(item);
            this._lines.push(item);
        }
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._tierSwitch = new PopupMenu.PopupSwitchMenuItem('VRAM swap tier', false);
        this._tierSwitch.connect('toggled', (_i, on) => this._switch(on ? 'tier-on' : 'tier-off'));
        this.menu.addMenuItem(this._tierSwitch);
        this._guardSwitch = new PopupMenu.PopupSwitchMenuItem('Memory guard', false);
        this._guardSwitch.connect('toggled', (_i, on) => this._switch(on ? 'guard-on' : 'guard-off'));
        this.menu.addMenuItem(this._guardSwitch);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('It felt slow just now (mark it)', () => this._mark());
        this.menu.addAction('Live view', () => this._terminal([USM, 'dash']));
        this.menu.addAction('Report: last 24 hours', () => this._terminal(['sh', '-c',
            `${USM} report; echo; read -p "Press Enter to close " x`]));

        this.connect('clicked', () => this._switch(this.checked ? 'tier-off' : 'tier-on'));
    }

    update(s) {
        const st = s.stats;
        this.checked = s.tier !== 'off';
        if (this._busy)
            this.subtitle = this._busy;
        else if (s.tier === 'on')
            this.subtitle = `${gib(+st.in_vram_mb || 0)} of ${gib(+st.vram_mb || 0)} GiB`;
        else if (s.tier === 'spilled')
            this.subtitle = 'VRAM released';
        else
            this.subtitle = 'Off';
        this._tierSwitch.setToggleState(s.tier !== 'off');
        this._guardSwitch.setToggleState(s.guard !== 'off');

        // One line per tier, in the order memory goes: RAM -> zswap -> VRAM -> swap file; then the guard.
        const lines = [`RAM: ${gib(s.mem.MemAvailable || 0)} of ${gib(s.mem.MemTotal || 0)} GiB free`];
        lines.push(s.mem.Zswap > 0 ? `zswap: ${gib(s.mem.Zswapped)} GiB packed into ${gib(s.mem.Zswap)} GiB of RAM`
            : 'zswap: empty');
        if (s.tier === 'on') {
            let v = `VRAM: ${gib(+st.in_vram_mb || 0)} of ${gib(+st.vram_mb || 0)} GiB used`;
            if (+st.in_vram_mb >= 100 && +st.comp_pct > 0)
                v += `, data at ${st.comp_pct}%`;
            if (+st.on_disk_mb > 0)
                v += ` + ${gib(+st.on_disk_mb)} GiB on disk`;
            lines.push(v);
        } else {
            lines.push(s.tier === 'spilled' ? `VRAM: released, ${gib(s.vram.used)} GiB wait on disk` : 'VRAM: off');
        }
        const disk = s.swaps.filter(x => !x.name.startsWith('/dev/ublkb') && x.prio > 1);
        lines.push(disk.length ? `Swap file: ${disk.map(x => `${gib(x.used)} of ${gib(x.size)} GiB`).join(' + ')} used`
            : 'Swap file: none');
        lines.push({
            full: 'Guard: on',
            observe: 'Guard: watching only',
            protect: 'Guard: protects the desktop only',
            waiting: 'Guard: waiting for login',
        }[s.guard] || 'Guard: off');
        if (s.paused.length)
            lines[4] += ` · ${s.paused.length} paused, open one to resume`;
        else if (s.guard === 'full')
            lines[4] += ' · nothing paused';
        lines.push(s.paused.length ? `Paused: ${s.paused.join(', ')}` : '');
        lines.forEach((t, i) => {
            this._lines[i].label.text = t;
            this._lines[i].visible = t !== '';
        });
    }

    _switch(verb) {
        if (this._busy)
            return;
        this._busy = BUSY[verb];
        this.subtitle = this._busy;
        run(['pkexec', HELPER, verb], (ok, out) => {
            this._busy = null;
            if (!ok)
                Main.notifyError('USM', out.split('\n').slice(-4).join('\n') || `${verb} failed`);
            this._refresh();
        });
    }

    _mark() {
        Main.panel.closeQuickSettings();
        run([USM, 'mark', 'marked from the desktop menu'], (ok, out) =>
            Main.notify('USM', ok ? 'Marked: it felt slow just now. Shows in "usm report".' : out));
    }

    _terminal(argv) {
        Main.panel.closeQuickSettings();
        for (const t of [['xdg-terminal-exec'], ['wezterm', 'start', '--'], ['ptyxis', '--'], ['kgx', '--'],
            ['gnome-terminal', '--']]) {
            if (GLib.find_program_in_path(t[0])) {
                try {
                    Gio.Subprocess.new([...t, ...argv], Gio.SubprocessFlags.NONE);
                    return;
                } catch (e) {}
            }
        }
        Main.notifyError('USM', 'No terminal found to open.');
    }
});

export const UsmIndicator = GObject.registerClass(
class UsmIndicator extends SystemIndicator {
    constructor() {
        super();
        this._pausedIcon = this._addIndicator();
        this._pausedIcon.iconName = 'media-playback-pause-symbolic';
        this._pausedIcon.visible = false;
        this._toggle = new UsmToggle(() => this.refresh());
        this.quickSettingsItems.push(this._toggle);
        this._menuId = this._toggle.menu.connect('open-state-changed', (_m, open) => {
            if (open)
                this.refresh();
        });
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 5, () => {
            this.refresh();
            return GLib.SOURCE_CONTINUE;
        });
        this.refresh();
    }

    refresh() {
        try {
            const s = readState();
            this._toggle.update(s);
            this._pausedIcon.visible = s.paused.length > 0;
        } catch (e) {
            console.error(`usm-desktop panel: ${e}`);
        }
    }

    destroy() {
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        this.quickSettingsItems.forEach(i => i.destroy());
        this.quickSettingsItems = [];
        super.destroy();
    }
});

// For testing in a headless GNOME Shell (env USM_DESKTOP_SELFTEST=1): open the menu, write its texts and a screenshot
// to $XDG_RUNTIME_DIR/usm-selftest.{txt,png}.
export function selfTest(indicator) {
    const dir = GLib.get_user_runtime_dir();
    GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 4, () => {
        try {
            Main.panel.statusArea.quickSettings.menu.open(false);
            indicator._toggle.menu.open(false);
        } catch (e) {
            GLib.file_set_contents(`${dir}/usm-selftest.txt`, `open failed: ${e}\n`);
        }
        GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
            const t = indicator._toggle;
            const texts = [`toggle: ${t.title} | ${t.subtitle} | checked=${t.checked}`,
                ...t._lines.map(l => `line: ${l.label.text}`),
                `tier switch: ${t._tierSwitch.state}`, `guard switch: ${t._guardSwitch.state}`,
                `paused icon visible: ${indicator._pausedIcon.visible}`];
            GLib.file_set_contents(`${dir}/usm-selftest.txt`, `${texts.join('\n')}\n`);
            try {
                const file = Gio.File.new_for_path(`${dir}/usm-selftest.png`);
                const stream = file.replace(null, false, Gio.FileCreateFlags.NONE, null);
                const shooter = new Shell.Screenshot();
                shooter.screenshot(false, stream, (o, res) => {
                    try {
                        o.screenshot_finish(res);
                    } catch (e) {
                        console.error(`usm selftest screenshot: ${e}`);
                    }
                    stream.close(null);
                });
            } catch (e) {
                console.error(`usm selftest screenshot: ${e}`);
            }
            return GLib.SOURCE_REMOVE;
        });
        return GLib.SOURCE_REMOVE;
    });
}
