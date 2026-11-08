/*
 * Claude Connector tab for the Proxmox VE web interface (Datacenter > Claude Connector).
 * Part of proxmox-claude-connector - https://github.com/ahmedmotta/proxmox-claude-connector
 *
 * Installed by gui/install-gui.sh. It talks to the connector's admin API with the logged-in
 * user's Proxmox ticket (the connector verifies it with Proxmox and accepts administrators only)
 * and reads the guest list from the Proxmox API itself. Every part is wrapped so a failure here
 * can never break the Proxmox GUI.
 */
(function () {
    // No 'use strict' on purpose: ExtJS callParent() relies on Function.caller, which strict mode forbids.

    var CONNECTOR_URL = '__CONNECTOR_URL__'; // replaced by install-gui.sh
    var VERSION = '__GUI_VERSION__';

    if (typeof Ext === 'undefined' || typeof PVE === 'undefined' || typeof Proxmox === 'undefined') {
        return;
    }
    if (!Ext.ClassManager.get('PVE.panel.Config') || !Ext.ClassManager.get('PVE.dc.Config')) {
        if (window.console) { console.warn('Claude Connector tab: unsupported Proxmox GUI version'); }
        return;
    }

    var t = typeof gettext === 'function' ? gettext : function (s) { return s; };

    // ---------------------------------------------------------------------------------------
    // Data access
    // ---------------------------------------------------------------------------------------
    function ticket() {
        try {
            return Proxmox.Utils.authOK() || Ext.util.Cookies.get('PVEAuthCookie') || '';
        } catch (e) {
            return Ext.util.Cookies.get('PVEAuthCookie') || '';
        }
    }

    function api(method, action, body) {
        return fetch(CONNECTOR_URL + '/admin/api/' + action, {
            method: method,
            cache: 'no-store',
            headers: { 'X-PVE-Ticket': ticket(), 'Content-Type': 'application/json' },
            body: body === undefined ? undefined : JSON.stringify(body),
        }).then(
            function (response) {
                return response.json().catch(function () { return {}; }).then(function (json) {
                    if (!response.ok) {
                        var err = new Error(json.error || ('HTTP ' + response.status));
                        err.status = response.status;
                        throw err;
                    }
                    return json.data;
                });
            },
            function () {
                var err = new Error('unreachable');
                err.unreachable = true;
                throw err;
            },
        );
    }

    function pveGuests() {
        return new Promise(function (resolve) {
            try {
                Proxmox.Utils.API2Request({
                    url: '/cluster/resources',
                    params: { type: 'vm' },
                    method: 'GET',
                    success: function (response) { resolve(response.result.data || []); },
                    failure: function () { resolve([]); },
                });
            } catch (e) {
                resolve([]);
            }
        });
    }

    // ---------------------------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------------------------
    function esc(value) {
        return Ext.htmlEncode(value === undefined || value === null ? '' : String(value));
    }

    function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

    function ago(iso) {
        var d = new Date(iso);
        if (isNaN(d)) { return esc(iso); }
        var s = Math.max(0, (Date.now() - d.getTime()) / 1000);
        if (s < 60) { return t('just now'); }
        if (s < 3600) { return plural(Math.floor(s / 60), t('minute'), t('minutes')) + ' ' + t('ago'); }
        if (s < 86400) { return plural(Math.floor(s / 3600), t('hour'), t('hours')) + ' ' + t('ago'); }
        return Ext.Date.format(d, 'M j, H:i');
    }

    function localTime(iso) {
        var d = new Date(iso);
        return isNaN(d) ? esc(iso) : Ext.Date.format(d, 'Y-m-d H:i:s');
    }

    function fromPveDate(text) {
        // "2026-11-08 04:00" in the connector's local time
        var d = Ext.Date.parse(text, 'Y-m-d H:i');
        return d ? d.toISOString() : text;
    }

    function copyText(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text);
        }
        var area = document.createElement('textarea');
        area.value = text;
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        try { document.execCommand('copy'); } finally { document.body.removeChild(area); }
        return Promise.resolve();
    }

    function isDark() {
        try {
            var rgb = window.getComputedStyle(document.body).backgroundColor.match(/\d+/g);
            if (!rgb) { return false; }
            return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) < 110;
        } catch (e) {
            return false;
        }
    }

    // What each action means, in the words an administrator would use.
    var POWER_WORDS = {
        start: 'Started', shutdown: 'Shut down', stop: 'Force-stopped', reboot: 'Rebooted',
        reset: 'Reset', suspend: 'Suspended', resume: 'Resumed',
    };
    var READ_TOOLS = {
        get_overview: 1, list_guests: 1, get_guest: 1, node_status: 1, list_backups: 1, list_storage: 1,
        storage_content: 1, list_lxc_templates: 1, list_tasks: 1, get_task: 1, api_help: 1, connector_info: 1,
    };

    function describe(entry) {
        var a = entry.args || {};
        var g = a.vmid !== undefined ? t('guest') + ' ' + a.vmid : '';
        switch (entry.tool) {
            case 'get_overview': return t('Looked at the overview');
            case 'list_guests': return t('Listed guests');
            case 'get_guest': return t('Checked') + ' ' + g;
            case 'node_status': return t('Checked the node') + (a.node ? ' ' + a.node : '');
            case 'power_guest': return t(POWER_WORDS[a.action] || 'Changed power of') + ' ' + g;
            case 'update_guest_config': return t('Changed settings of') + ' ' + g;
            case 'resize_disk': return t('Grew') + ' ' + a.disk + ' ' + t('of') + ' ' + g + ' ' + t('to') + ' ' + a.size;
            case 'create_vm': return t('Created a virtual machine') + (a.settings && a.settings.name ? ' ' + a.settings.name : '');
            case 'create_container': return t('Created a container') + (a.settings && a.settings.hostname ? ' ' + a.settings.hostname : '');
            case 'clone_guest': return t('Cloned') + ' ' + g + (a.name ? ' ' + t('as') + ' ' + a.name : '');
            case 'migrate_guest': return t('Moved') + ' ' + g + ' ' + t('to') + ' ' + a.target_node;
            case 'convert_to_template': return t('Turned') + ' ' + g + ' ' + t('into a template');
            case 'delete_guest': return t('Deleted') + ' ' + g;
            case 'manage_snapshots':
                if (a.action === 'create') { return t('Took snapshot') + ' ' + a.name + ' ' + t('of') + ' ' + g; }
                if (a.action === 'rollback') { return t('Rolled') + ' ' + g + ' ' + t('back to') + ' ' + a.name; }
                if (a.action === 'delete') { return t('Deleted snapshot') + ' ' + a.name + ' ' + t('of') + ' ' + g; }
                return t('Listed snapshots of') + ' ' + g;
            case 'backup_guests': return t('Backed up') + ' ' + (a.vmids || []).join(', ');
            case 'list_backups': return t('Listed backups');
            case 'restore_backup': return t('Restored a backup') + (a.vmid ? ' ' + t('as') + ' ' + a.vmid : '');
            case 'list_storage': case 'storage_content': return t('Checked storage') + (a.storage ? ' ' + a.storage : '');
            case 'download_to_storage': return t('Downloaded') + ' ' + (a.filename || t('a file'));
            case 'list_lxc_templates': return t('Browsed container templates');
            case 'download_lxc_template': return t('Downloaded the template') + ' ' + a.template;
            case 'list_tasks': return t('Checked recent tasks');
            case 'get_task': return t('Followed a task');
            case 'guest_exec': return t('Ran a command in') + ' ' + g;
            case 'guest_file': return (a.action === 'write' ? t('Wrote') : t('Read')) + ' ' + a.path + ' ' + t('in') + ' ' + g;
            case 'host_shell': return t('Ran a command on the host');
            case 'proxmox_api': return a.method + ' ' + a.path;
            case 'api_help': return t('Looked up API parameters');
            case 'connector_info': return t('Checked the connector');
            case 'admin:settings': return t('Changed the connector settings');
            case 'admin:revoke': return a.client_id ? t('Disconnected a session') : t('Disconnected all sessions');
            case 'admin:password': return t('Changed the approval password');
            default: return entry.tool;
        }
    }

    function isChange(entry) {
        if (READ_TOOLS[entry.tool]) { return false; }
        var a = entry.args || {};
        if (entry.tool === 'manage_snapshots' && a.action === 'list') { return false; }
        if (entry.tool === 'guest_file' && a.action === 'read') { return false; }
        if (entry.tool === 'proxmox_api' && a.method === 'GET') { return false; }
        return true;
    }

    // ---------------------------------------------------------------------------------------
    // Styles (scoped under .ccx)
    // ---------------------------------------------------------------------------------------
    var CSS = [
        '.ccx{--ink:#1C2430;--slate:#5B6675;--faint:#8A94A3;--rail:#E6E9EE;--rail2:#F3F5F8;--paper:#FFFFFF;',
        '--blue:#2F5D8A;--blue-soft:#E8EEF5;--green:#1F8A5B;--green-soft:#E6F4EC;--amber:#B86E00;--amber-soft:#FBF1E1;',
        '--red:#C23B30;--red-soft:#FBEAE8;color:var(--ink);background:var(--paper);font-size:13px;line-height:1.5;',
        'padding:20px 24px 32px;box-sizing:border-box;min-height:100%}',
        '.ccx.ccx-dark{--ink:#E6E9EE;--slate:#A9B2BF;--faint:#7D8794;--rail:#38404A;--rail2:#2C333B;--paper:#22282F;',
        '--blue:#7FA8D1;--blue-soft:#2B3A4A;--green:#4CC38A;--green-soft:#1F3A2E;--amber:#E0A040;--amber-soft:#3D3120;',
        '--red:#EF6F63;--red-soft:#43282A}',
        '.ccx *{box-sizing:border-box}',
        '.ccx button{font:inherit;cursor:pointer}',
        '.ccx a{color:var(--blue)}',
        '.ccx b,.ccx strong{font-weight:600}',
        '.ccx button:focus-visible,.ccx input:focus-visible,.ccx .ccx-switch:focus-visible{outline:2px solid var(--blue);outline-offset:2px}',
        '.ccx h2{font-size:15px;font-weight:600;margin:0 0 4px}',
        '.ccx p{margin:0}',
        '.ccx .ccx-muted{color:var(--slate)}',
        '.ccx .ccx-num{font-variant-numeric:tabular-nums}',

        /* status strip */
        '.ccx-strip{display:flex;align-items:center;gap:20px;padding:18px 20px;border-radius:10px;',
        'border:1px solid var(--rail);border-left:6px solid var(--green);background:var(--green-soft)}',
        '.ccx-strip.is-paused{border-left-color:var(--amber);background:var(--amber-soft)}',
        '.ccx-strip.is-down{border-left-color:var(--red);background:var(--red-soft)}',
        '.ccx-strip.is-loading{border-left-color:var(--rail);background:var(--rail2)}',
        '.ccx-pulse{width:12px;height:12px;border-radius:50%;background:var(--green);flex:none;',
        'box-shadow:0 0 0 0 rgba(31,138,91,.5);animation:ccx-pulse 2.4s ease-out infinite}',
        '.is-paused .ccx-pulse{background:var(--amber);animation:none}',
        '.is-down .ccx-pulse{background:var(--red);animation:none}',
        '.is-loading .ccx-pulse{background:var(--faint);animation:none}',
        '@keyframes ccx-pulse{0%{box-shadow:0 0 0 0 rgba(31,138,91,.45)}70%{box-shadow:0 0 0 10px rgba(31,138,91,0)}100%{box-shadow:0 0 0 0 rgba(31,138,91,0)}}',
        '@media (prefers-reduced-motion:reduce){.ccx-pulse{animation:none}}',
        '.ccx-strip-text{flex:1;min-width:0}',
        '.ccx-strip-title{font-size:20px;font-weight:600;letter-spacing:-.01em;line-height:1.25}',
        '.ccx-strip-sub{color:var(--slate);margin-top:2px}',
        '.ccx-btn{border:1px solid var(--rail);background:var(--paper);color:var(--ink);border-radius:7px;',
        'padding:7px 14px;font-weight:500;white-space:nowrap}',
        '.ccx-btn:hover{border-color:var(--slate)}',
        '.ccx-btn-strong{background:var(--ink);border-color:var(--ink);color:var(--paper);padding:9px 18px}',
        '.ccx-btn-strong:hover{opacity:.88}',
        '.ccx-btn-go{background:var(--green);border-color:var(--green);color:#fff;padding:9px 18px}',
        '.ccx-btn-danger{color:var(--red)}',
        '.ccx-btn-danger:hover{border-color:var(--red)}',
        '.ccx-link{background:none;border:0;padding:0;color:var(--blue);font-weight:500}',
        '.ccx-link:hover{text-decoration:underline}',

        /* url line */
        '.ccx-url{display:flex;align-items:center;gap:10px;margin:12px 2px 0;flex-wrap:wrap}',
        '.ccx-url code{font:inherit;font-variant-numeric:tabular-nums;background:var(--rail2);border:1px solid var(--rail);',
        'border-radius:6px;padding:3px 8px;user-select:all}',

        /* sections */
        '.ccx-grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:36px;margin-top:28px}',
        '@media (max-width:1100px){.ccx-grid{grid-template-columns:minmax(0,1fr);gap:28px}}',
        '.ccx-section{margin-top:32px}',
        '.ccx-grid .ccx-section{margin-top:0}',
        '.ccx-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:12px}',

        /* access levels */
        '.ccx-levels{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}',
        '@media (max-width:760px){.ccx-levels{grid-template-columns:minmax(0,1fr)}}',
        '.ccx-level{text-align:left;border:1px solid var(--rail);background:var(--paper);color:var(--ink);',
        'border-radius:9px;padding:12px 14px;position:relative;display:flex;flex-direction:column;justify-content:flex-start}',
        '.ccx-level:hover{border-color:var(--slate)}',
        '.ccx-level.is-on{border-color:var(--blue);box-shadow:inset 0 0 0 1px var(--blue);background:var(--blue-soft)}',
        '.ccx-level b{display:block;font-size:14px;font-weight:600;margin-bottom:3px;padding-right:18px}',
        '.ccx-level span{color:var(--slate);font-size:12.5px}',
        '.ccx-level .ccx-tick{position:absolute;top:11px;right:12px;color:var(--blue);display:none}',
        '.ccx-level.is-on .ccx-tick{display:block}',

        /* sessions */
        '.ccx-list{border-top:1px solid var(--rail)}',
        '.ccx-row{display:flex;align-items:center;gap:12px;padding:10px 2px;border-bottom:1px solid var(--rail)}',
        '.ccx-row-main{flex:1;min-width:0}',
        '.ccx-row-main b{font-weight:600}',
        '.ccx-empty{padding:14px 2px;color:var(--slate)}',

        /* rack */
        '.ccx-rack{border:1px solid var(--rail);border-radius:10px;overflow:hidden;background:var(--paper)}',
        '.ccx-unit{display:grid;grid-template-columns:18px 56px minmax(0,1fr) 110px 96px 150px;align-items:center;',
        'gap:12px;padding:0 16px;height:46px;border-top:1px solid var(--rail);position:relative}',
        '.ccx-unit:first-child{border-top:0}',
        '.ccx-rack-head{display:flex;justify-content:space-between;gap:12px;padding:8px 16px;background:var(--rail2);',
        'color:var(--slate);font-size:12px;border-bottom:1px solid var(--rail)}',
        '.ccx-rack-head + .ccx-unit{border-top:0}',
        '.ccx-rack.has-scope .ccx-unit{grid-template-columns:18px 56px minmax(0,1fr) 110px 96px 150px 110px}',
        '@media (max-width:900px){.ccx-unit,.ccx-rack.has-scope .ccx-unit{grid-template-columns:18px 48px minmax(0,1fr) auto;',
        'height:auto;padding:10px 14px}.ccx-unit .ccx-hide-sm{display:none}}',
        '.ccx-unit::before{content:"";position:absolute;left:0;top:8px;bottom:8px;width:3px;border-radius:0 3px 3px 0;background:transparent}',
        '.ccx-unit.is-protected::before{background:var(--blue)}',
        '.ccx-unit.is-hidden{opacity:.5}',
        '.ccx-led{width:9px;height:9px;border-radius:50%;background:var(--faint)}',
        '.ccx-led.is-running{background:var(--green)}',
        '.ccx-led.is-template{background:transparent;border:2px solid var(--faint);width:10px;height:10px}',
        '.ccx-vmid{font-weight:600;font-variant-numeric:tabular-nums}',
        '.ccx-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
        '.ccx-name small{color:var(--slate);margin-left:8px}',
        '.ccx-kind{color:var(--slate)}',
        '.ccx-state{color:var(--slate)}',
        '.ccx-switch{display:inline-flex;align-items:center;gap:8px;background:none;border:0;padding:4px 0;color:var(--slate)}',
        '.ccx-switch i.ccx-track{width:30px;height:17px;border-radius:9px;background:var(--rail);position:relative;flex:none;',
        'transition:background .15s}',
        '.ccx-switch i.ccx-track::after{content:"";position:absolute;top:2px;left:2px;width:13px;height:13px;border-radius:50%;',
        'background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.25);transition:left .15s}',
        '.ccx-switch[aria-checked=true]{color:var(--ink)}',
        '.ccx-switch[aria-checked=true] i.ccx-track{background:var(--blue)}',
        '.ccx-switch[aria-checked=true] i.ccx-track::after{left:15px}',
        '.ccx-switch[disabled]{cursor:not-allowed;opacity:.6}',
        '@media (prefers-reduced-motion:reduce){.ccx-switch i.ccx-track,.ccx-switch i.ccx-track::after{transition:none}}',
        '.ccx-scope{margin-top:10px;display:flex;align-items:center;gap:10px;color:var(--slate)}',

        /* activity */
        '.ccx-filters{display:flex;gap:6px}',
        '.ccx-chip{border:1px solid var(--rail);background:var(--paper);color:var(--slate);border-radius:20px;padding:3px 12px}',
        '.ccx-chip.is-on{background:var(--ink);border-color:var(--ink);color:var(--paper)}',
        '.ccx-event{display:grid;grid-template-columns:22px minmax(0,1fr) 130px 120px;gap:12px;align-items:baseline;',
        'padding:9px 2px;border-bottom:1px solid var(--rail);cursor:pointer}',
        '.ccx-event:hover{background:var(--rail2)}',
        '@media (max-width:760px){.ccx-event{grid-template-columns:22px minmax(0,1fr)}.ccx-event .ccx-hide-sm{display:none}}',
        '.ccx-event .fa-check{color:var(--green)}',
        '.ccx-event .fa-times{color:var(--red)}',
        '.ccx-event .fa-circle-o{color:var(--faint)}',
        '.ccx-event-what.is-change{font-weight:600}',
        '.ccx-event-err{color:var(--red);font-size:12.5px;margin-top:2px}',
        '.ccx-detail{grid-column:2 / -1;background:var(--rail2);border-radius:6px;padding:8px 10px;margin-top:6px;',
        'white-space:pre-wrap;word-break:break-word;font-size:12px;color:var(--slate);cursor:text}',

        /* footer */
        '.ccx-foot{margin-top:32px;padding-top:16px;border-top:1px solid var(--rail);display:flex;flex-wrap:wrap;',
        'align-items:center;gap:10px 28px;color:var(--slate)}',
        '.ccx-foot input{width:64px;border:1px solid var(--rail);border-radius:6px;padding:4px 6px;background:var(--paper);',
        'color:var(--ink);font:inherit;font-variant-numeric:tabular-nums}',
        '.ccx-spacer{flex:1}',
    ].join('');

    function ensureStyles() {
        if (!document.getElementById('ccx-styles')) {
            var style = document.createElement('style');
            style.id = 'ccx-styles';
            style.textContent = CSS;
            document.head.appendChild(style);
        }
    }

    // ---------------------------------------------------------------------------------------
    // Password window
    // ---------------------------------------------------------------------------------------
    Ext.define('PVE.ClaudeConnector.PasswordWindow', {
        extend: 'Ext.window.Window',
        title: t('Change approval password'),
        modal: true,
        width: 420,
        bodyPadding: 12,
        layout: 'anchor',
        defaults: { anchor: '100%', labelWidth: 140 },
        items: [
            {
                xtype: 'component',
                html: t('You type this password on the approval page when you connect a Claude account. Accounts already connected stay connected.'),
                margin: '0 0 10 0',
            },
            { xtype: 'textfield', inputType: 'password', fieldLabel: t('New password'), itemId: 'pw1', minLength: 12, allowBlank: false },
            { xtype: 'textfield', inputType: 'password', fieldLabel: t('Confirm password'), itemId: 'pw2', allowBlank: false },
        ],
        buttons: [
            {
                text: t('Change password'),
                handler: function (btn) {
                    var win = btn.up('window');
                    var pw1 = win.down('#pw1').getValue();
                    if (pw1.length < 12) {
                        Ext.Msg.alert(t('Claude Connector'), t('Use at least 12 characters.'));
                        return;
                    }
                    if (pw1 !== win.down('#pw2').getValue()) {
                        Ext.Msg.alert(t('Claude Connector'), t('The two passwords are different.'));
                        return;
                    }
                    win.setLoading(true);
                    api('POST', 'password', { password: pw1 }).then(function () {
                        win.close();
                        Ext.toast({ html: t('Password changed.'), align: 't' });
                    }).catch(function (err) {
                        win.setLoading(false);
                        Ext.Msg.alert(t('Claude Connector'), esc(err.message));
                    });
                },
            },
        ],
    });

    // ---------------------------------------------------------------------------------------
    // The tab
    // ---------------------------------------------------------------------------------------
    var LEVELS = [
        { id: 'full', name: t('Full control'), text: t('Everything, including deleting guests and running commands on the host and inside guests.') },
        { id: 'safe', name: t('Safe'), text: t('Create, change, start and shut down. No deleting, force stops or commands.') },
        { id: 'readonly', name: t('Read only'), text: t('Look at everything, change nothing.') },
    ];

    Ext.define('PVE.ClaudeConnector.Panel', {
        extend: 'Ext.Component',
        alias: 'widget.pveClaudeConnectorPanel',
        scrollable: true,

        initComponent: function () {
            var me = this;
            me.state = { phase: 'loading', filter: 'all', open: {} };
            me.callParent();
            me.on('afterrender', me.onFirstRender, me);
            me.on('destroy', function () { if (me.timer) { clearInterval(me.timer); } });
        },

        onFirstRender: function () {
            var me = this;
            ensureStyles();
            me.getEl().on('click', me.onClick, me);
            me.getEl().on('change', me.onChange, me);
            me.paint();
            me.reload();
            // Keep the activity fresh while the tab is open.
            me.timer = setInterval(function () {
                if (me.isVisible(true) && !me.busy) { me.reload(true); }
            }, 30000);
        },

        reload: function (quiet) {
            var me = this;
            if (!quiet) { me.state.phase = me.state.status ? me.state.phase : 'loading'; }
            return Promise.all([api('GET', 'status'), api('GET', 'audit?limit=300'), pveGuests()]).then(function (res) {
                if (me.destroyed || me.isDestroyed) { return; }
                me.state.status = res[0];
                me.state.audit = res[1] || [];
                me.state.guests = res[2];
                me.state.phase = res[0].settings.paused ? 'paused' : 'live';
                me.state.error = null;
                me.paint();
            }).catch(function (err) {
                if (me.destroyed || me.isDestroyed) { return; }
                me.state.phase = 'down';
                me.state.error = err;
                me.paint();
            });
        },

        save: function (changes, message) {
            var me = this;
            me.busy = true;
            return api('PUT', 'settings', changes).then(function (settings) {
                me.state.status.settings = settings;
                me.state.phase = settings.paused ? 'paused' : 'live';
                me.busy = false;
                me.paint();
                if (message) { Ext.toast({ html: esc(message), align: 't' }); }
            }).catch(function (err) {
                me.busy = false;
                Ext.Msg.alert(t('Claude Connector'), esc(err.message));
                me.reload(true);
            });
        },

        // ---- events ---------------------------------------------------------------------
        onClick: function (e) {
            var me = this;
            var target = e.getTarget('[data-act]');
            if (!target || target.disabled) { return; }
            var act = target.getAttribute('data-act');
            var arg = target.getAttribute('data-arg');
            var st = me.state;
            var settings = st.status && st.status.settings;

            if (act === 'retry') { me.state.phase = 'loading'; me.paint(); me.reload(); }
            if (act === 'pause') {
                Ext.Msg.confirm(t('Pause Claude'), t('Claude will be refused every action until you resume. Pause now?'), function (answer) {
                    if (answer === 'yes') { me.save({ paused: true }, t('Claude is paused.')); }
                });
            }
            if (act === 'resume') { me.save({ paused: false }, t('Claude can act again.')); }
            if (act === 'copy') {
                copyText(st.status.mcp_url).then(function () { Ext.toast({ html: t('Address copied.'), align: 't' }); });
            }
            if (act === 'level' && settings && arg !== settings.mode) {
                var apply = function () { me.save({ mode: arg }, t('Access level changed. It applies right away.')); };
                if (arg === 'full') {
                    Ext.Msg.confirm(t('Full control'), t('Claude will be able to delete guests and run commands as root. Continue?'), function (answer) {
                        if (answer === 'yes') { apply(); }
                    });
                } else {
                    apply();
                }
            }
            if (act === 'protect' && settings) {
                var vmid = Number(arg);
                var list = settings.protected_vmids.slice();
                var at = list.indexOf(vmid);
                if (at >= 0) { list.splice(at, 1); } else { list.push(vmid); }
                me.save({ protected_vmids: list }, at >= 0
                    ? Ext.String.format(t('Guest {0} is no longer protected.'), vmid)
                    : Ext.String.format(t('Guest {0} is protected.'), vmid));
            }
            if (act === 'scope' && settings) {
                if (settings.allowed_vmids.length) {
                    me.save({ allowed_vmids: [] }, t('Claude can see every guest again.'));
                } else {
                    var all = (st.guests || []).map(function (g) { return g.vmid; });
                    Ext.Msg.confirm(t('Limit Claude to some guests'), t('Claude will see only the guests you tick, and the raw API and host shell tools will be turned off. Continue?'), function (answer) {
                        if (answer === 'yes') { me.save({ allowed_vmids: all }, t('Untick the guests Claude should not see.')); }
                    });
                }
            }
            if (act === 'visible' && settings) {
                var id = Number(arg);
                var allowed = settings.allowed_vmids.slice();
                var pos = allowed.indexOf(id);
                if (pos >= 0) { allowed.splice(pos, 1); } else { allowed.push(id); }
                if (!allowed.length) {
                    Ext.Msg.alert(t('Claude Connector'), t('Keep at least one guest ticked, or turn the limit off.'));
                    return;
                }
                me.save({ allowed_vmids: allowed });
            }
            if (act === 'revoke' || act === 'revoke-all') {
                var question = act === 'revoke-all'
                    ? t('Disconnect every Claude account? Each one must be approved again with the password.')
                    : t('Disconnect this Claude account?');
                Ext.Msg.confirm(t('Disconnect'), question, function (answer) {
                    if (answer !== 'yes') { return; }
                    api('POST', 'revoke', act === 'revoke' ? { client_id: arg } : {}).then(function () {
                        Ext.toast({ html: t('Disconnected.'), align: 't' });
                        me.reload(true);
                    }).catch(function (err) { Ext.Msg.alert(t('Claude Connector'), esc(err.message)); });
                });
            }
            if (act === 'filter') { st.filter = arg; me.paint(); }
            if (act === 'event') { st.open[arg] = !st.open[arg]; me.paint(); }
            if (act === 'password') { Ext.create('PVE.ClaudeConnector.PasswordWindow', { autoShow: true }); }
        },

        onChange: function (e) {
            var input = e.getTarget('input[data-field=max_wait]');
            if (!input) { return; }
            var value = Number(input.value);
            if (value >= 10 && value <= 600) {
                this.save({ max_wait: value }, t('Saved.'));
            } else {
                Ext.Msg.alert(t('Claude Connector'), t('Pick a number of seconds from 10 to 600.'));
            }
        },

        // ---- rendering ------------------------------------------------------------------
        paint: function () {
            var me = this;
            if (!me.rendered) { return; }
            var scroll = me.getScrollable ? me.getScrollable() : null;
            var y = scroll && scroll.getPosition ? scroll.getPosition().y : 0;
            me.update('<div class="ccx' + (isDark() ? ' ccx-dark' : '') + '">' + me.html_() + '</div>');
            if (scroll && scroll.scrollTo && y) { scroll.scrollTo(0, y); }
        },

        html_: function () {
            var st = this.state;
            if (st.phase === 'loading') {
                return this.strip('is-loading', t('Checking the connector'), t('One moment.'), '');
            }
            if (st.phase === 'down') {
                var err = st.error || {};
                var title = err.status === 403 ? t('This page is for Proxmox administrators')
                    : err.status === 401 ? t('Your Proxmox session has expired')
                    : t('Can’t reach the connector');
                var sub = err.unreachable
                    ? Ext.String.format(t('This browser cannot open {0}. If the connector was installed recently, your network may still remember the address as missing; it clears within 30 minutes. Claude itself is not affected.'), '<b>' + esc(CONNECTOR_URL) + '</b>')
                    : esc(err.message);
                return this.strip('is-down', title, sub, '<button class="ccx-btn" data-act="retry">' + t('Try again') + '</button>');
            }
            return this.top() + '<div class="ccx-grid">' + this.levels() + this.sessions() + '</div>'
                + this.rack() + this.activity() + this.foot();
        },

        strip: function (cls, title, sub, action) {
            return '<div class="ccx-strip ' + cls + '" role="status"><span class="ccx-pulse"></span>'
                + '<div class="ccx-strip-text"><div class="ccx-strip-title">' + esc(title) + '</div>'
                + '<div class="ccx-strip-sub">' + sub + '</div></div>' + action + '</div>';
        },

        top: function () {
            var s = this.state.status;
            var cfg = s.settings;
            var level = { full: t('Full control'), safe: t('Safe mode'), readonly: t('Read only') }[cfg.mode];
            var bits = [level, plural(cfg.protected_vmids.length, t('protected guest'), t('protected guests'))];
            if (cfg.allowed_vmids.length) { bits.push(Ext.String.format(t('limited to {0}'), plural(cfg.allowed_vmids.length, t('guest'), t('guests')))); }
            bits.push(s.ssh_enabled ? t('host commands on') : t('host commands off'));
            var strip = cfg.paused
                ? this.strip('is-paused', t('Claude is paused'), esc(t('Every request from Claude is refused until you resume.')),
                    '<button class="ccx-btn ccx-btn-go" data-act="resume"><i class="fa fa-play"></i>&nbsp; ' + t('Resume Claude') + '</button>')
                : this.strip('', t('Claude can manage this server'), esc(bits.join(', ') + '.'),
                    '<button class="ccx-btn ccx-btn-strong" data-act="pause"><i class="fa fa-pause"></i>&nbsp; ' + t('Pause Claude') + '</button>');
            return strip + '<div class="ccx-url"><span class="ccx-muted">' + t('Address to add in Claude') + '</span>'
                + '<code>' + esc(s.mcp_url) + '</code>'
                + '<button class="ccx-link" data-act="copy">' + t('Copy') + '</button></div>';
        },

        levels: function () {
            var mode = this.state.status.settings.mode;
            return '<section class="ccx-section"><div class="ccx-head"><div><h2>' + t('What Claude may do') + '</h2>'
                + '<p class="ccx-muted">' + t('Enforced by the connector itself, not left to Claude.') + '</p></div></div>'
                + '<div class="ccx-levels" role="radiogroup">' + LEVELS.map(function (l) {
                    return '<button class="ccx-level' + (l.id === mode ? ' is-on' : '') + '" role="radio" aria-checked="'
                        + (l.id === mode) + '" data-act="level" data-arg="' + l.id + '"><i class="fa fa-check ccx-tick"></i>'
                        + '<b>' + esc(l.name) + '</b><span>' + esc(l.text) + '</span></button>';
                }).join('') + '</div></section>';
        },

        sessions: function () {
            var list = this.state.status.sessions || [];
            var rows = list.length ? list.map(function (s) {
                return '<div class="ccx-row"><i class="fa fa-plug ccx-muted"></i><div class="ccx-row-main"><b>'
                    + esc(s.client_name || t('Unnamed app')) + '</b><div class="ccx-muted">'
                    + t('Connected') + ' ' + ago(fromPveDate(s.connected)) + '</div></div>'
                    + '<button class="ccx-btn ccx-btn-danger" data-act="revoke" data-arg="' + esc(s.client_id) + '">'
                    + t('Disconnect') + '</button></div>';
            }).join('') : '<div class="ccx-empty">' + t('No Claude account is connected. Add the address above in Claude to connect one.') + '</div>';
            return '<section class="ccx-section"><div class="ccx-head"><div><h2>' + t('Connected accounts') + '</h2>'
                + '<p class="ccx-muted">' + t('Each one was approved with your password.') + '</p></div>'
                + (list.length > 1 ? '<button class="ccx-link" data-act="revoke-all">' + t('Disconnect all') + '</button>' : '')
                + '</div><div class="ccx-list">' + rows + '</div></section>';
        },

        rack: function () {
            var cfg = this.state.status.settings;
            var scoped = cfg.allowed_vmids.length > 0;
            var guests = (this.state.guests || []).slice().sort(function (a, b) { return a.vmid - b.vmid; });
            var units = guests.map(function (g) {
                var isProtected = cfg.protected_vmids.indexOf(g.vmid) >= 0;
                var visible = !scoped || cfg.allowed_vmids.indexOf(g.vmid) >= 0;
                var self = g.name === 'pve-connector' || String(g.tags || '').split(/[;,\s]/).indexOf('claude') >= 0;
                var kind = g.template ? t('Template') : g.type === 'lxc' ? t('Container') : t('Virtual machine');
                var led = g.template ? 'is-template' : g.status === 'running' ? 'is-running' : '';
                var lock = '<button class="ccx-switch" role="switch" aria-checked="' + isProtected + '" data-act="protect" data-arg="'
                    + g.vmid + '"' + (self && isProtected ? ' disabled title="' + t('The connector always stays protected') + '"' : '')
                    + '><i class="ccx-track"></i>' + (isProtected ? t('Protected') : t('Protect')) + '</button>';
                var see = scoped ? '<button class="ccx-switch" role="switch" aria-checked="' + visible + '" data-act="visible" data-arg="'
                    + g.vmid + '"><i class="ccx-track"></i>' + (visible ? t('Visible') : t('Hidden')) + '</button>' : '';
                return '<div class="ccx-unit' + (isProtected ? ' is-protected' : '') + (visible ? '' : ' is-hidden') + '">'
                    + '<span class="ccx-led ' + led + '" title="' + esc(g.status) + '"></span>'
                    + '<span class="ccx-vmid">' + g.vmid + '</span>'
                    + '<span class="ccx-name">' + esc(g.name || '') + (self ? '<small>' + t('this connector') + '</small>' : '') + '</span>'
                    + '<span class="ccx-kind ccx-hide-sm">' + kind + '</span>'
                    + '<span class="ccx-state ccx-hide-sm">' + esc(g.template ? '' : g.status) + '</span>'
                    + lock + see + '</div>';
            }).join('');
            var count = cfg.protected_vmids.length;
            var nodes = {};
            guests.forEach(function (g) { nodes[g.node] = 1; });
            var running = guests.filter(function (g) { return g.status === 'running' && !g.template; }).length;
            var head = guests.length ? '<div class="ccx-rack-head"><span>' + esc(t('Node') + ' ' + Object.keys(nodes).join(', '))
                + '</span><span class="ccx-num">' + esc(plural(guests.length, t('guest'), t('guests')) + ', ' + running + ' ' + t('running'))
                + '</span></div>' : '';
            return '<section class="ccx-section"><div class="ccx-head"><div><h2>' + t('Guests') + '</h2>'
                + '<p class="ccx-muted">' + t('Claude can read, start, snapshot and back up a protected guest, but never shut it down, stop, delete, roll back or restore over it.')
                + '</p></div><span class="ccx-muted ccx-num">' + plural(count, t('protected'), t('protected')) + '</span></div>'
                + '<div class="ccx-rack' + (scoped ? ' has-scope' : '') + '">' + head
                + (units || '<div class="ccx-empty" style="padding:14px 16px">' + t('No guests on this cluster yet.') + '</div>') + '</div>'
                + '<div class="ccx-scope"><button class="ccx-switch" role="switch" aria-checked="' + scoped + '" data-act="scope">'
                + '<i class="ccx-track"></i>' + t('Let Claude see only the guests I tick') + '</button></div></section>';
        },

        activity: function () {
            var st = this.state;
            var sessions = {};
            (st.status.sessions || []).forEach(function (s) { sessions[s.client_id] = s.client_name; });
            var entries = st.audit || [];
            var shown = entries.filter(function (e) {
                if (st.filter === 'changes') { return isChange(e); }
                if (st.filter === 'failed') { return e.ok === false; }
                return true;
            }).slice(0, 120);
            var dayAgo = Date.now() - 86400000;
            var lastDay = entries.filter(function (e) { return new Date(e.time).getTime() > dayAgo; });
            var failed = lastDay.filter(function (e) { return e.ok === false; }).length;
            var summary = plural(lastDay.length, t('action'), t('actions')) + ' ' + t('in the last 24 hours')
                + (failed ? ', ' + plural(failed, t('refused or failed'), t('refused or failed')) : '');
            var chip = function (id, label) {
                return '<button class="ccx-chip' + (st.filter === id ? ' is-on' : '') + '" data-act="filter" data-arg="' + id + '">' + label + '</button>';
            };
            var rows = shown.map(function (e, i) {
                var key = e.time + '|' + e.tool + '|' + i;
                var who = String(e.client || '');
                who = who.indexOf('pve:') === 0 ? who.slice(4) + ' ' + t('in Proxmox') : (sessions[who] || t('Claude'));
                var icon = e.ok === false ? 'fa-times' : isChange(e) ? 'fa-check' : 'fa-circle-o';
                var detail = st.open[key]
                    ? '<div class="ccx-detail">' + esc(JSON.stringify(e.args || {}, null, 2)) + (e.seconds !== undefined ? '\n' + t('Took') + ' ' + e.seconds + ' s' : '') + '</div>'
                    : '';
                return '<div class="ccx-event" data-act="event" data-arg="' + esc(key) + '" title="' + t('Show details') + '">'
                    + '<i class="fa ' + icon + '"></i><div><div class="ccx-event-what' + (isChange(e) ? ' is-change' : '') + '">'
                    + esc(describe(e)) + '</div>' + (e.error ? '<div class="ccx-event-err">' + esc(e.error) + '</div>' : '') + '</div>'
                    + '<span class="ccx-muted ccx-hide-sm">' + esc(who) + '</span>'
                    + '<span class="ccx-muted ccx-num ccx-hide-sm" title="' + localTime(e.time) + '">' + ago(e.time) + '</span>'
                    + detail + '</div>';
            }).join('');
            return '<section class="ccx-section"><div class="ccx-head"><div><h2>' + t('Activity') + '</h2>'
                + '<p class="ccx-muted">' + esc(summary) + '</p></div><div class="ccx-filters">'
                + chip('all', t('Everything')) + chip('changes', t('Changes')) + chip('failed', t('Refused or failed'))
                + '</div></div><div class="ccx-list">'
                + (rows || '<div class="ccx-empty">' + (st.filter === 'all'
                    ? t('Nothing has been done through the connector yet.')
                    : t('Nothing matches this filter.')) + '</div>')
                + '</div></section>';
        },

        foot: function () {
            var s = this.state.status;
            return '<div class="ccx-foot"><label>' + t('Wait up to') + ' <input type="number" min="10" max="600" data-field="max_wait" value="'
                + s.settings.max_wait + '"> ' + t('seconds for long tasks, then hand Claude the task to follow up')
                + '</label><button class="ccx-link" data-act="password">' + t('Change approval password') + '</button>'
                + '<span class="ccx-spacer"></span><span>' + t('Connector') + ' ' + esc(s.version) + ', ' + t('tab') + ' ' + esc(VERSION)
                + '. ' + t('Signed in as') + ' ' + esc(s.user) + '.</span>'
                + '<a href="https://github.com/ahmedmotta/proxmox-claude-connector" target="_blank" rel="noopener">' + t('Project page') + '</a></div>';
        },
    });

    // ---------------------------------------------------------------------------------------
    // Add the tab to Datacenter
    // ---------------------------------------------------------------------------------------
    Ext.define('PVE.ClaudeConnector.DatacenterTab', {
        override: 'PVE.panel.Config',

        initComponent: function () {
            var me = this;
            try {
                if (PVE.dc && PVE.dc.Config && me instanceof PVE.dc.Config && Ext.isArray(me.items)) {
                    var caps = Ext.state.Manager.get('GuiCap') || {};
                    if (caps.dc && caps.dc['Sys.Audit']) {
                        var tab = {
                            xtype: 'pveClaudeConnectorPanel',
                            title: 'Claude Connector',
                            itemId: 'claude-connector',
                            iconCls: 'fa fa-plug',
                        };
                        var supportIndex = -1;
                        Ext.Array.each(me.items, function (item, index) {
                            if (item && item.itemId === 'support') { supportIndex = index; }
                        });
                        if (supportIndex >= 0) {
                            me.items.splice(supportIndex, 0, tab);
                        } else {
                            me.items.push(tab);
                        }
                    }
                }
            } catch (e) {
                if (window.console) { console.error('Claude Connector tab:', e); }
            }
            me.callParent(arguments);
        },
    });
}());
