/*
 * Brand theme for the Proxmox VE web interface.
 * Part of proxmox-claude-connector - https://github.com/ahmedmotta/proxmox-claude-connector
 *
 * The colors and fonts live in /pwt/themes/theme-pcc-dark.css and theme-pcc-light.css, which
 * Proxmox loads itself through its own theme cookie. This script:
 *   - adds both themes to Proxmox's "Color Theme" dialog,
 *   - puts a dark / light / Proxmox switcher in the top bar,
 *   - turns the top bar into a brand bar and gives charts the brand palette,
 *   - waits for the brand fonts before the interface is drawn, so nothing is measured twice.
 *
 * Emergency exit: open the interface with ?pcc-theme=off to go back to the stock look.
 * Every part is wrapped so a failure here can never break the Proxmox interface.
 */
(function () {
    // No 'use strict' on purpose: ExtJS callParent() relies on Function.caller, which strict mode forbids.

    var VERSION = '__GUI_VERSION__';
    var DEFAULT_THEME = '__DEFAULT_THEME__'; // pcc-dark, pcc-light or none; set by install-gui.sh
    // install-gui.sh fills these in (--brand, --brand-url, --brand-logo); left alone, the project's own brand shows.
    var BRAND = {
        name: '__BRAND_NAME__',
        url: '__BRAND_URL__',
        logo: '__BRAND_LOGO__',
    };
    if (BRAND.name.indexOf('__') === 0) { BRAND.name = 'Innovation IT Hub'; }
    if (BRAND.url.indexOf('__') === 0) { BRAND.url = 'https://innithub.online'; }
    if (BRAND.logo.indexOf('__') === 0) { BRAND.logo = '/pwt/themes/pcc-logo.png'; }
    var THEMES = { 'pcc-dark': BRAND.name + ' Dark', 'pcc-light': BRAND.name + ' Light' };
    var CHART_COLORS = ['#ff6a00', '#ffb703', '#2bb3a8', '#8f7cf6', '#e2557a', '#7fb800'];
    var BUILDS = {"pcc-dark": "4c1b1e774025", "pcc-light": "2a6dc784f66d"}; // build.py: the build id written into each theme's CSS
    var COOKIE = 'PVEThemeCookie';
    // Per default theme, so a new default chosen by the admin applies once in every browser.
    var INIT_KEY = 'pcc-theme-initialized:' + DEFAULT_THEME;

    if (typeof Ext === 'undefined' || typeof Proxmox === 'undefined') {
        return;
    }

    function log(e) {
        if (window.console) { console.warn('Claude theme:', e); }
    }

    function getCookie() {
        try { return Ext.util.Cookies.get(COOKIE) || ''; } catch (e) { return ''; }
    }

    function setTheme(value, beforeStart) {
        // Same cookie and lifetime Proxmox's own "Color Theme" dialog uses.
        var expire = new Date(Date.now() + 10 * 365 * 24 * 3600 * 1000);
        Ext.util.Cookies.set(COOKIE, value || '__default__', expire, '/');
        try { window.localStorage.setItem(INIT_KEY, '1'); } catch (e) { /* private mode */ }
        if (beforeStart) {
            // the page is about to reload: do not draw the old look first
            try { Ext.env.Ready.block(); } catch (e) { /* not needed for the reload */ }
        }
        window.location.reload();
    }

    // ---- Proxmox's own theme dialog lists the brand themes ------------------------------------
    // First, before any early return: the tab script treats a pcc-* cookie without these entries as a
    // theme that was removed from the node.
    try {
        Ext.apply(Proxmox.Utils.theme_map, THEMES);
    } catch (e) {
        log(e);
    }

    // ---- emergency exit and first visit ----------------------------------------------------
    try {
        var params = new URLSearchParams(window.location.search);
        if (params.get('pcc-theme') === 'off') {
            params.delete('pcc-theme');
            var rest = params.toString();
            window.history.replaceState(null, '', window.location.pathname + (rest ? '?' + rest : '') + window.location.hash);
            setTheme('__default__', true);
            return;
        }
        var initialized = false;
        try { initialized = window.localStorage.getItem(INIT_KEY) === '1'; } catch (e) { initialized = true; }
        if (!initialized && THEMES[DEFAULT_THEME]) {
            try { window.localStorage.setItem(INIT_KEY, '1'); } catch (e) { /* ignore */ }
            if (getCookie() !== DEFAULT_THEME) {
                setTheme(DEFAULT_THEME, true);
                return;
            }
        }
    } catch (e) {
        log(e);
    }

    var current = getCookie();
    var active = !!THEMES[current];
    var mode = current === 'pcc-light' ? 'light' : 'dark';

    // ---- switcher styles (needed in every theme, so they live here, not in the theme files) ----
    try {
        var style = document.createElement('style');
        style.id = 'pcc-switch-style';
        style.textContent = [
            '.pcc-switch{display:flex;align-items:center;justify-content:flex-end}',
            '.pcc-switch-group{display:inline-flex;padding:2px;gap:2px;border:1px solid var(--pcc-switch-line,rgba(128,128,128,.45));',
            'border-radius:999px;background:var(--pcc-switch-bg,transparent)}',
            '.pcc-switch button{appearance:none;border:0;margin:0;width:26px;height:22px;border-radius:999px;background:transparent;',
            'color:var(--pcc-switch-fg,#777);font-size:12px;line-height:22px;cursor:pointer;padding:0}',
            '.pcc-switch button:hover{color:var(--pcc-switch-fg-hover,#333)}',
            '.pcc-switch button[aria-pressed=true]{background:var(--pcc-accent,#ff6a00);color:#0b0b0c}',
            '.pcc-switch button:focus-visible{outline:2px solid var(--pcc-accent,#ff6a00);outline-offset:1px}',
            '.pcc-header{--pcc-switch-line:var(--pcc-header-input-line);--pcc-switch-bg:var(--pcc-header-input);',
            '--pcc-switch-fg:var(--pcc-header-text2);--pcc-switch-fg-hover:var(--pcc-header-text)}',
            '.pcc-header .pcc-switch-group{padding:3px}.pcc-header .pcc-switch button{width:28px;height:24px;line-height:24px}',
        ].join('');
        document.head.appendChild(style);
    } catch (e) {
        log(e);
    }

    if (active) {
        document.documentElement.classList.add('pcc-theme', 'pcc-theme-' + mode);
        try {
            // the full-screen sign-in shows the brand from CSS
            document.documentElement.style.setProperty('--pcc-brand-name', JSON.stringify(BRAND.name));
            document.documentElement.style.setProperty('--pcc-brand-logo', 'url(' + JSON.stringify(BRAND.logo) + ')');
        } catch (e) {
            log(e);
        }
    }

    // ---- a browser that cached an older theme file fetches the current one ---------------------
    // Proxmox links the theme as theme-<name>.css?ver=<toolkit version>, which does not change when
    // the theme does. The CSS carries its build id; if it is not the one this script expects, load
    // the current file next to it (same rules, so the newer copy simply wins).
    if (active && BUILDS && typeof BUILDS === 'object' && BUILDS[current]) {
        try {
            var loaded = getComputedStyle(document.documentElement).getPropertyValue('--pcc-build').replace(/[\s"']/g, '');
            if (loaded !== BUILDS[current]) {
                var link = document.createElement('link');
                link.rel = 'stylesheet';
                link.href = '/pwt/themes/theme-' + current + '.css?build=' + BUILDS[current];
                document.head.appendChild(link);
            }
        } catch (e) {
            log(e);
        }
    }

    // ---- wait for the brand fonts before ExtJS measures anything ------------------------------
    if (active && document.fonts && Ext.env && Ext.env.Ready && typeof Ext.env.Ready.block === 'function') {
        var blocked = false;
        var released = false;
        var release = function () {
            if (blocked && !released) {
                released = true;
                try { Ext.env.Ready.unblock(); } catch (e) { log(e); }
            }
        };
        try {
            Ext.env.Ready.block();
            blocked = true;
            setTimeout(release, 2000); // never hold the interface back for long
            Promise.all([
                document.fonts.load('400 12.5px "PCC Inter"'),
                document.fonts.load('600 12.5px "PCC Inter"'),
                document.fonts.load('600 14px "PCC Grotesk"'),
            ]).then(release, release);
        } catch (e) {
            log(e);
            release();
        }
    }

    // ---- top bar ---------------------------------------------------------------------------------
    function switcherHtml() {
        var choices = [
            ['pcc-dark', 'fa-moon-o', BRAND.name + ' dark'],
            ['pcc-light', 'fa-sun-o', BRAND.name + ' light'],
            ['__default__', 'fa-desktop', 'Proxmox default'],
        ];
        return '<div class="pcc-switch-group" role="group" aria-label="Color theme">' + choices.map(function (c) {
            var on = c[0] === current || (c[0] === '__default__' && !active);
            var label = Ext.htmlEncode(c[2]);
            return '<button type="button" data-theme="' + c[0] + '" aria-pressed="' + on + '" title="' + label
                + '" aria-label="' + label + '"><i class="fa ' + c[1] + '" aria-hidden="true"></i></button>';
        }).join('') + '</div>';
    }

    if (Ext.ClassManager.get('PVE.StdWorkspace')) {
        Ext.define('PVE.ClaudeTheme.Workspace', {
            override: 'PVE.StdWorkspace',

            initComponent: function () {
                var me = this;
                me.callParent(arguments);
                try {
                    var north = me.query('[region=north]')[0];
                    if (!north) { return; }
                    var user = north.down('#userinfo');
                    var at = user ? north.items.indexOf(user) : north.items.getCount();
                    north.insert(at, {
                        xtype: 'component',
                        cls: 'pcc-switch',
                        width: 90,
                        height: 28,
                        margin: '0 8 0 0',
                        html: switcherHtml(),
                        listeners: {
                            click: {
                                element: 'el',
                                fn: function (event) {
                                    var button = event.getTarget('button[data-theme]');
                                    if (button && button.getAttribute('aria-pressed') !== 'true') {
                                        setTheme(button.getAttribute('data-theme'));
                                    }
                                },
                            },
                        },
                    });
                    if (!active) { return; }

                    north.addCls('pcc-header');
                    var info = north.down('#versioninfo');
                    if (info) { info.setHtml('<span class="pcc-version">Proxmox VE</span>'); }
                    var search = north.down('pveGlobalSearchField');
                    if (search) { search.setWidth(320); }
                    north.setHeight(56);
                    north.setMargin(0);

                    // the app shell: full-bleed regions with hooks for the stylesheet
                    var west = me.down('#west');
                    if (west) {
                        west.addCls('pcc-west');
                        west.setMargin(0);
                        var head = west.items && west.items.getAt(0);
                        if (head && !head.isXType('treepanel')) { head.addCls('pcc-west-head'); }
                    }
                    var tree = me.down('pveResourceTree');
                    if (tree) { tree.addCls('pcc-tree'); }
                    var content = me.down('#content');
                    if (content) {
                        content.addCls('pcc-content');
                        content.setMargin(0);
                    }
                    var south = me.down('#south');
                    if (south) {
                        south.addCls('pcc-south');
                        south.setMargin(0);
                    }
                    // one primary action in the top bar: Create VM stays orange, Create CT turns neutral
                    north.query('button').forEach(function (button) {
                        if (/\bfa-cube\b/.test(button.iconCls || '')) { button.addCls('pcc-header-secondary'); }
                    });
                    var logo = north.down('proxmoxlogo') || north.down('proxmoxLogoSvg'); // PVE 7/8 and PVE 9
                    if (logo) {
                        logo.setHidden(true);
                        north.insert(north.items.indexOf(logo), {
                            xtype: 'component',
                            height: 34,
                            margin: '0 10 0 16',
                            html: '<a class="pcc-brand" href="' + Ext.htmlEncode(BRAND.url) + '" target="_blank" rel="noopener">'
                                + '<img src="' + Ext.htmlEncode(BRAND.logo) + '" alt=""><b>' + Ext.htmlEncode(BRAND.name) + '</b></a>',
                        });
                    }
                    var help = north.down('proxmoxHelpButton');
                    if (help) { help.addCls('pcc-ghost'); }
                    if (user) {
                        user.addCls('pcc-ghost');
                        user.setMargin('0 14 0 0');
                    }
                    var favicon = document.querySelector('link[rel="icon"]');
                    if (favicon) { favicon.href = BRAND.logo; }
                } catch (e) {
                    log(e);
                }
            },

            updateVersionInfo: function () {
                var me = this;
                me.callParent(arguments);
                if (!active) { return; }
                try {
                    var info = me.query('#versioninfo')[0];
                    if (info && PVE.VersionInfo) {
                        info.update('<span class="pcc-version">Proxmox VE ' + Ext.htmlEncode(PVE.VersionInfo.version) + '</span>');
                        info.updateLayout();
                    }
                } catch (e) {
                    log(e);
                }
            },
        });
    }

    if (!active) {
        return;
    }

    // ---- configuration views: page header and section navigation ---------------------------------
    if (Ext.ClassManager.get('PVE.panel.Config')) {
        Ext.define('PVE.ClaudeTheme.Config', {
            override: 'PVE.panel.Config',
            initComponent: function () {
                this.callParent(arguments);
                try {
                    this.addCls('pcc-config');
                    var top = this.getDockedItems('toolbar[dock=top]')[0];
                    if (top) { top.addCls('pcc-config-head'); }
                    var left = this.getDockedItems('toolbar[dock=left]')[0];
                    if (left) { left.addCls('pcc-config-nav'); }
                } catch (e) {
                    log(e);
                }
            },
        });
    }

    // ---- login window: a full-screen, branded sign-in ---------------------------------------------
    if (Ext.ClassManager.get('PVE.window.LoginWindow')) {
        var loginOpen = function (open) {
            // on <html>: ExtJS rewrites the class list of <body> when the viewport renders
            try { document.documentElement.classList.toggle('pcc-login-open', open); } catch (e) { log(e); }
        };
        Ext.define('PVE.ClaudeTheme.LoginWindow', {
            override: 'PVE.window.LoginWindow',
            initComponent: function () {
                this.callParent(arguments);
                try {
                    this.addCls('pcc-login');
                    this.setIcon(BRAND.logo);
                    this.setWidth(440);
                    loginOpen(true);
                    this.on({
                        show: function () { loginOpen(true); },
                        hide: function () { loginOpen(false); },
                        destroy: function () { loginOpen(false); },
                    });
                } catch (e) {
                    log(e);
                }
            },
        });
    }

    // ---- gauges: the big percentage in the display font --------------------------------------------
    if (Ext.ClassManager.get('Proxmox.panel.GaugeWidget')) {
        Ext.define('PVE.ClaudeTheme.Gauge', {
            override: 'Proxmox.panel.GaugeWidget',
            initComponent: function () {
                this.callParent(arguments);
                try {
                    if (this.valueSprite) {
                        this.valueSprite.setAttributes({
                            fontFamily: '"PCC Grotesk", "Space Grotesk", helvetica, arial, sans-serif',
                            fontWeight: '500',
                        }, true);
                    }
                } catch (e) {
                    log(e);
                }
            },
        });
    }

    // ---- dialogs: one orange button per dialog -------------------------------------------------------
    // Proxmox renders every dialog button the same way. Back, Reset, No and Cancel become neutral so the
    // orange button is always the action that moves forward. (Help buttons are neutral through CSS.)
    function markSecondary(button) {
        if (button && typeof button.addCls === 'function') { button.addCls('pcc-secondary'); }
    }
    if (Ext.ClassManager.get('Proxmox.window.Edit')) {
        Ext.define('PVE.ClaudeTheme.EditWindow', {
            override: 'Proxmox.window.Edit',
            initComponent: function () {
                this.callParent(arguments);
                try {
                    var resetText = typeof gettext === 'function' ? gettext('Reset') : 'Reset';
                    this.query('button').forEach(function (button) {
                        if (button.text === 'Reset' || button.text === resetText) { markSecondary(button); }
                    });
                } catch (e) {
                    log(e);
                }
            },
        });
    }
    if (Ext.ClassManager.get('PVE.window.Wizard')) {
        Ext.define('PVE.ClaudeTheme.Wizard', {
            override: 'PVE.window.Wizard',
            initComponent: function () {
                this.callParent(arguments);
                try { markSecondary(this.down('#back')); } catch (e) { log(e); }
            },
        });
    }
    if (Ext.ClassManager.get('Ext.window.MessageBox')) {
        Ext.define('PVE.ClaudeTheme.MessageBox', {
            override: 'Ext.window.MessageBox',
            makeButton: function (index) {
                var button = this.callParent(arguments);
                try {
                    var id = this.buttonIds && this.buttonIds[index];
                    if (id === 'no' || id === 'cancel') { markSecondary(button); }
                } catch (e) {
                    log(e);
                }
                return button;
            },
        });
    }

    // ---- charts: brand palette ---------------------------------------------------------------------
    if (Ext.ClassManager.get('Proxmox.widget.RRDChart')) {
        Ext.define('PVE.ClaudeTheme.RRDChart', {
            override: 'Proxmox.widget.RRDChart',
            initComponent: function () {
                this.callParent(arguments);
                try {
                    // after the series exist, so ExtJS recolors them
                    this.setColors(CHART_COLORS);
                    // a crisp line over a fill that fades out towards the axis
                    (this.getSeries() || []).forEach(function (series, index) {
                        var color = Ext.util.Color.fromString(CHART_COLORS[index % CHART_COLORS.length]);
                        var rgba = function (alpha) {
                            return 'rgba(' + color.r + ',' + color.g + ',' + color.b + ',' + alpha + ')';
                        };
                        series.setStyle({
                            opacity: 1,
                            fillOpacity: 1,
                            strokeOpacity: 1,
                            lineWidth: 2,
                            strokeStyle: rgba(1),
                            fillStyle: {
                                type: 'linear',
                                degrees: 90,
                                stops: [
                                    { offset: 0, color: rgba(0.34) }, // under the line
                                    { offset: 1, color: rgba(0.02) }, // at the axis
                                ],
                            },
                        });
                    });
                } catch (e) {
                    log(e);
                }
            },
        });
    }
}());
