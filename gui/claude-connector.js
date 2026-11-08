/*
 * Claude Connector tab for the Proxmox VE web interface (Datacenter > Claude Connector).
 * Part of proxmox-claude-connector - https://github.com/ahmedmotta/proxmox-claude-connector
 *
 * Installed by gui/install-gui.sh. It talks only to the connector's admin API, sending the
 * logged-in user's Proxmox ticket; the connector verifies it with Proxmox and accepts
 * administrators only. Every part is wrapped so a failure here can never break the Proxmox GUI.
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
                        throw new Error(json.error || ('HTTP ' + response.status));
                    }
                    return json.data;
                });
            },
            function () {
                throw new Error(Ext.String.format(
                    t('Cannot reach the connector at {0}. Check that it is running and that this browser has internet access.'),
                    CONNECTOR_URL,
                ));
            },
        );
    }

    function showError(err) {
        Ext.Msg.alert(t('Claude Connector'), Ext.htmlEncode(err && err.message ? err.message : String(err)));
    }

    function vmidsToText(list) {
        return (list || []).join(', ');
    }

    function textToVmids(text) {
        return String(text || '').split(/[\s,;]+/).filter(function (v) { return v !== ''; }).map(Number);
    }

    // -------------------------------------------------------------------------------------
    // Password window
    // -------------------------------------------------------------------------------------
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
                html: t('This password is typed on the approval page when a Claude account is connected. Existing sessions stay connected.'),
                margin: '0 0 10 0',
            },
            { xtype: 'textfield', inputType: 'password', fieldLabel: t('New password'), itemId: 'pw1', minLength: 12, allowBlank: false },
            { xtype: 'textfield', inputType: 'password', fieldLabel: t('Confirm password'), itemId: 'pw2', allowBlank: false },
        ],
        buttons: [
            {
                text: t('Save'),
                handler: function (btn) {
                    var win = btn.up('window');
                    var pw1 = win.down('#pw1').getValue();
                    var pw2 = win.down('#pw2').getValue();
                    if (pw1.length < 12) {
                        showError(t('Use at least 12 characters.'));
                        return;
                    }
                    if (pw1 !== pw2) {
                        showError(t('The passwords do not match.'));
                        return;
                    }
                    win.setLoading(true);
                    api('POST', 'password', { password: pw1 }).then(function () {
                        win.close();
                        Ext.toast({ html: t('Approval password changed.'), align: 't' });
                    }).catch(function (err) {
                        win.setLoading(false);
                        showError(err);
                    });
                },
            },
        ],
    });

    // -------------------------------------------------------------------------------------
    // Main panel
    // -------------------------------------------------------------------------------------
    Ext.define('PVE.ClaudeConnector.Panel', {
        extend: 'Ext.panel.Panel',
        alias: 'widget.pveClaudeConnectorPanel',

        scrollable: true,
        bodyPadding: 10,
        layout: { type: 'vbox', align: 'stretch' },
        defaults: { margin: '0 0 10 0' },

        initComponent: function () {
            var me = this;

            me.sessionStore = Ext.create('Ext.data.Store', {
                fields: ['client_id', 'client_name', 'connected', 'expires'],
            });
            me.auditStore = Ext.create('Ext.data.Store', {
                fields: ['time', 'client', 'tool', 'ok', 'seconds', 'error', 'args'],
            });

            me.tbar = [
                { text: t('Reload'), iconCls: 'fa fa-refresh', handler: function () { me.reload(); } },
                '-',
                {
                    itemId: 'pauseBtn',
                    text: t('Pause connector'),
                    iconCls: 'fa fa-pause',
                    disabled: true,
                    handler: function () { me.togglePause(); },
                },
                {
                    text: t('Change approval password'),
                    iconCls: 'fa fa-key',
                    handler: function () { Ext.create('PVE.ClaudeConnector.PasswordWindow', { autoShow: true }); },
                },
                '->',
                {
                    xtype: 'component',
                    html: '<a href="https://github.com/ahmedmotta/proxmox-claude-connector" target="_blank" rel="noopener">'
                        + 'proxmox-claude-connector</a>',
                },
            ];

            me.items = [
                {
                    xtype: 'panel',
                    itemId: 'statusPanel',
                    title: t('Status'),
                    bodyPadding: 10,
                    html: t('Loading...'),
                },
                {
                    xtype: 'form',
                    itemId: 'settingsForm',
                    title: t('Permissions'),
                    bodyPadding: 10,
                    layout: 'anchor',
                    defaults: { anchor: '100%', labelWidth: 170, maxWidth: 760 },
                    items: [
                        {
                            xtype: 'combobox',
                            name: 'mode',
                            fieldLabel: t('Mode'),
                            editable: false,
                            queryMode: 'local',
                            store: [
                                ['full', t('Full - everything, including deletes and shell')],
                                ['safe', t('Safe - create, change, start, clean shutdown; no deletes or shell')],
                                ['readonly', t('Read only - look, never change')],
                            ],
                        },
                        {
                            xtype: 'textfield',
                            name: 'protected_vmids',
                            fieldLabel: t('Protected VMIDs'),
                            emptyText: t('e.g. 100, 105'),
                            regex: /^[\d\s,;]*$/,
                            regexText: t('VMIDs separated by commas'),
                        },
                        {
                            xtype: 'displayfield',
                            value: t('Claude can read, start, snapshot and back up protected guests, but never shut them down, stop, delete, roll back or restore over them.'),
                            fieldCls: 'x-form-display-field pmx-hint',
                        },
                        {
                            xtype: 'textfield',
                            name: 'allowed_vmids',
                            fieldLabel: t('Only these VMIDs'),
                            emptyText: t('empty = all guests'),
                            regex: /^[\d\s,;]*$/,
                            regexText: t('VMIDs separated by commas'),
                        },
                        {
                            xtype: 'displayfield',
                            value: t('When set, Claude sees only these guests and the raw API and host shell tools are turned off.'),
                            fieldCls: 'x-form-display-field pmx-hint',
                        },
                        {
                            xtype: 'numberfield',
                            name: 'max_wait',
                            fieldLabel: t('Max wait per task (s)'),
                            minValue: 10,
                            maxValue: 600,
                            allowDecimals: false,
                            maxWidth: 320,
                        },
                    ],
                    buttons: [
                        {
                            text: t('Save'),
                            iconCls: 'fa fa-save',
                            formBind: true,
                            handler: function () { me.saveSettings(); },
                        },
                    ],
                },
                {
                    xtype: 'grid',
                    itemId: 'sessionsGrid',
                    title: t('Connected Claude sessions'),
                    store: me.sessionStore,
                    minHeight: 120,
                    emptyText: t('No Claude account is connected.'),
                    viewConfig: { deferEmptyText: false },
                    tbar: [
                        {
                            text: t('Disconnect all'),
                            iconCls: 'fa fa-ban',
                            handler: function () {
                                Ext.Msg.confirm(t('Disconnect all'), t('Disconnect every Claude session? Each account must approve again with the password.'), function (answer) {
                                    if (answer === 'yes') { me.revoke(null); }
                                });
                            },
                        },
                    ],
                    columns: [
                        { text: t('Application'), dataIndex: 'client_name', flex: 1, renderer: Ext.htmlEncode },
                        { text: t('Connected'), dataIndex: 'connected', width: 150 },
                        { text: t('Expires'), dataIndex: 'expires', width: 150 },
                        { text: t('Client ID'), dataIndex: 'client_id', flex: 1, renderer: Ext.htmlEncode },
                        {
                            xtype: 'actioncolumn',
                            width: 40,
                            items: [{
                                iconCls: 'fa fa-trash-o',
                                tooltip: t('Disconnect'),
                                handler: function (grid, rowIndex) {
                                    me.revoke(grid.getStore().getAt(rowIndex).get('client_id'));
                                },
                            }],
                        },
                    ],
                },
                {
                    xtype: 'grid',
                    itemId: 'auditGrid',
                    title: t('Recent actions (audit log)'),
                    store: me.auditStore,
                    height: 360,
                    emptyText: t('Nothing has been done through the connector yet.'),
                    viewConfig: { deferEmptyText: false },
                    columns: [
                        {
                            text: t('Time'),
                            dataIndex: 'time',
                            width: 160,
                            renderer: function (v) {
                                var d = v ? new Date(v) : null;
                                return d && !isNaN(d) ? Ext.Date.format(d, 'Y-m-d H:i:s') : Ext.htmlEncode(v);
                            },
                        },
                        {
                            text: t('Result'),
                            dataIndex: 'ok',
                            width: 70,
                            renderer: function (ok) {
                                return ok
                                    ? '<i class="fa fa-check good"></i>'
                                    : '<i class="fa fa-times critical"></i>';
                            },
                        },
                        { text: t('Action'), dataIndex: 'tool', width: 170, renderer: Ext.htmlEncode },
                        {
                            text: t('By'),
                            dataIndex: 'client',
                            width: 150,
                            renderer: function (v) {
                                var session = me.sessionStore.findRecord('client_id', v, 0, false, true, true);
                                var name = session && session.get('client_name');
                                return Ext.htmlEncode(name ? name + ' (' + String(v).slice(0, 8) + ')' : v);
                            },
                        },
                        {
                            text: t('Details'),
                            flex: 1,
                            renderer: function (v, meta, rec) {
                                var text = rec.get('error') || JSON.stringify(rec.get('args') || {});
                                meta.tdAttr = 'data-qtip="' + Ext.htmlEncode(Ext.htmlEncode(text)) + '"';
                                return Ext.htmlEncode(text);
                            },
                        },
                    ],
                },
            ];

            me.callParent();
            me.on('afterrender', function () { me.reload(); });
        },

        reload: function () {
            var me = this;
            if (me.isDestroyed) { return; }
            me.setLoading(t('Loading...'));
            Promise.all([api('GET', 'status'), api('GET', 'audit?limit=200')]).then(function (res) {
                if (me.isDestroyed) { return; }
                me.setLoading(false);
                me.applyStatus(res[0]);
                me.auditStore.setData(res[1] || []);
            }).catch(function (err) {
                if (me.isDestroyed) { return; }
                me.setLoading(false);
                me.down('#statusPanel').setHtml(
                    '<i class="fa fa-exclamation-triangle warning"></i> ' + Ext.htmlEncode(err.message),
                );
                me.down('#pauseBtn').setDisabled(true);
            });
        },

        applyStatus: function (data) {
            var me = this;
            me.status = data;
            var s = data.settings || {};
            var state = s.paused
                ? '<i class="fa fa-pause-circle warning"></i> ' + t('Paused - Claude cannot do anything')
                : '<i class="fa fa-check-circle good"></i> ' + t('Running');
            var rows = [
                [t('State'), state],
                [t('Connector URL for Claude'), '<code>' + Ext.htmlEncode(data.mcp_url) + '</code>'],
                [t('Mode'), Ext.htmlEncode(s.mode)],
                [t('Host shell and container commands'), data.ssh_enabled ? t('Enabled (SSH)') : t('Disabled')],
                [t('Connector version'), Ext.htmlEncode(data.version) + ' &nbsp; <span style="opacity:.6">(' + t('tab') + ' ' + VERSION + ')</span>'],
                [t('Signed in as'), Ext.htmlEncode(data.user)],
            ];
            me.down('#statusPanel').setHtml(
                '<table style="border-spacing:0 6px">' + rows.map(function (r) {
                    return '<tr><td style="padding-right:24px;opacity:.75;white-space:nowrap">' + r[0] + '</td><td>' + r[1] + '</td></tr>';
                }).join('') + '</table>',
            );
            me.down('#settingsForm').getForm().setValues({
                mode: s.mode,
                protected_vmids: vmidsToText(s.protected_vmids),
                allowed_vmids: vmidsToText(s.allowed_vmids),
                max_wait: s.max_wait,
            });
            var btn = me.down('#pauseBtn');
            btn.setDisabled(false);
            btn.setText(s.paused ? t('Resume connector') : t('Pause connector'));
            btn.setIconCls(s.paused ? 'fa fa-play' : 'fa fa-pause');
            me.sessionStore.setData(data.sessions || []);
        },

        saveSettings: function () {
            var me = this;
            var v = me.down('#settingsForm').getForm().getValues();
            me.setLoading(t('Saving...'));
            api('PUT', 'settings', {
                mode: v.mode,
                protected_vmids: textToVmids(v.protected_vmids),
                allowed_vmids: textToVmids(v.allowed_vmids),
                max_wait: Number(v.max_wait),
            }).then(function () {
                me.setLoading(false);
                Ext.toast({ html: t('Saved. The new rules apply immediately.'), align: 't' });
                me.reload();
            }).catch(function (err) {
                me.setLoading(false);
                showError(err);
            });
        },

        togglePause: function () {
            var me = this;
            var paused = !!(me.status && me.status.settings && me.status.settings.paused);
            var go = function () {
                api('PUT', 'settings', { paused: !paused }).then(function () { me.reload(); }).catch(showError);
            };
            if (paused) {
                go();
            } else {
                Ext.Msg.confirm(t('Pause connector'), t('Claude will be refused every action until you resume. Continue?'), function (answer) {
                    if (answer === 'yes') { go(); }
                });
            }
        },

        revoke: function (clientId) {
            var me = this;
            api('POST', 'revoke', clientId ? { client_id: clientId } : {}).then(function () {
                Ext.toast({ html: t('Disconnected.'), align: 't' });
                me.reload();
            }).catch(showError);
        },
    });

    // -------------------------------------------------------------------------------------
    // Add the tab to Datacenter
    // -------------------------------------------------------------------------------------
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
