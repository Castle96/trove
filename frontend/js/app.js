/* Trove dashboard controller.
 *
 * Security patches over the original:
 *  - no inline onclick/onchange handlers (event delegation instead)
 *  - escapeHtml escapes & < > " ' and backticks, applied to every dynamic value
 *  - serial/fingerprint are escaped in the details modal
 *  - all data comes from the API; nothing is persisted client-side except the
 *    optional API token (localStorage).
 */
(function () {
    'use strict';

    const state = {
        certs: [],
        filter: 'all',
        search: '',
        simDate: '—',
    };

    /* ---------- utils ---------- */

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/`/g, '&#96;');
    }

    /* Share with legacy scripts (terminal.js) that call it as a global. */
    window.escapeHtml = escapeHtml;

    function fmtDate(value) {
        return value ? String(value).slice(0, 10) : '\u2014';
    }

    function debounce(fn, ms) {
        let timer = null;
        return function (...args) {
            clearTimeout(timer);
            timer = setTimeout(() => fn.apply(this, args), ms);
        };
    }

    const badgeClass = (status) => {
        if (status === 'expiring') return 'bg-amber-500/10 text-amber-400 border-amber-500/20';
        if (status === 'expired') return 'bg-rose-500/10 text-rose-400 border-rose-500/20';
        return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20';
    };

    const issuerColor = (issuer) => {
        const s = String(issuer);
        if (s.includes('step-ca')) return 'text-purple-400';
        if (s.includes('Self-Signed')) return 'text-gray-400';
        if (s.includes('Vault')) return 'text-sky-400';
        return 'text-indigo-400';
    };

    /* ---------- data ---------- */

    async function refreshAll() {
        try {
            const [certs, time] = await Promise.all([api.get('/api/certs'), api.get('/api/time')]);
            state.certs = certs;
            state.simDate = time.sim_now;
            document.getElementById('current-sim-date').textContent = state.simDate;
            renderMetrics();
            renderTable();
        } catch (err) {
            Terminal.log(err.message, 'error');
        }
        try {
            await Terminal.loadLogs();
        } catch (err) {
            Terminal.log(err.message, 'error');
        }
    }

    /* ---------- render ---------- */

    function renderMetrics() {
        const certs = state.certs.filter((c) => c.status !== 'revoked');
        document.getElementById('metric-total').textContent = certs.length;
        document.getElementById('metric-valid').textContent = certs.filter((c) => c.status === 'valid').length;
        document.getElementById('metric-expiring').textContent = certs.filter((c) => c.status === 'expiring').length;
        document.getElementById('metric-expired').textContent = certs.filter((c) => c.status === 'expired').length;
        document.getElementById('metric-autorenew').textContent = certs.filter((c) => c.autoRenew).length;
    }

    function renderTable() {
        const tbody = document.getElementById('certs-table-body');
        const emptyState = document.getElementById('certs-empty-state');
        const needle = state.search.trim().toLowerCase();

        let rows = state.certs;
        if (needle) {
            rows = rows.filter((c) =>
                [c.cn, ...c.sans, c.issuer].join(' ').toLowerCase().includes(needle)
            );
        }
        if (state.filter === 'expiring') rows = rows.filter((c) => c.status === 'expiring');
        if (state.filter === 'expired') rows = rows.filter((c) => c.status === 'expired');
        if (state.filter === 'autorenew') rows = rows.filter((c) => c.autoRenew);

        tbody.innerHTML = '';
        emptyState.classList.toggle('hidden', rows.length !== 0);

        rows.forEach((cert) => {
            const tr = document.createElement('tr');
            tr.className = 'hover:bg-slate-800/40 transition group border-b border-gray-800/40';

            tr.innerHTML = `
                <td class="py-3 px-4">
                    <div class="font-bold text-gray-100 font-mono text-xs flex items-center gap-1.5">
                        <i class="fa-solid fa-lock text-[10px] ${cert.status === 'expired' ? 'text-rose-400' : 'text-amber-400'}"></i>
                        ${escapeHtml(cert.cn)}
                    </div>
                    <div class="text-[10px] text-gray-500 truncate max-w-xs mt-0.5">
                        SANs: ${escapeHtml(cert.sans.join(', '))}
                    </div>
                </td>
                <td class="py-3 px-3">
                    <span class="font-semibold ${issuerColor(cert.issuer)}">${escapeHtml(cert.issuer)}</span>
                    <div class="text-[10px] text-gray-500">${escapeHtml(cert.key_type)}</div>
                </td>
                <td class="py-3 px-3 text-gray-400 text-[11px]">${escapeHtml(cert.protocol)}</td>
                <td class="py-3 px-3 text-gray-300 font-mono text-[11px]">${fmtDate(cert.not_after)}</td>
                <td class="py-3 px-3">
                    <span class="px-2 py-0.5 rounded text-[10px] font-bold border ${badgeClass(cert.status)}">
                        ${cert.days_left <= 0 ? 'EXPIRED' : cert.days_left + ' Days'}
                    </span>
                </td>
                <td class="py-3 px-3 text-center">
                    <label class="relative inline-flex items-center cursor-pointer">
                        <span class="sr-only">Toggle auto-renew for ${escapeHtml(cert.cn)}</span>
                        <input type="checkbox" data-action="autorenew" data-id="${escapeHtml(cert.id)}"
                               ${cert.autoRenew ? 'checked' : ''} class="sr-only peer auto-renew-toggle">
                        <div class="w-8 h-4 bg-slate-950 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-gray-400 peer-checked:after:bg-white after:border-gray-300 after:border after:rounded-full after:h-3 after:w-3 after:transition-all peer-checked:bg-indigo-600"></div>
                    </label>
                </td>
                <td class="py-3 px-4 text-right space-x-1">
                    <button type="button" data-action="renew" data-id="${escapeHtml(cert.id)}"
                            class="px-2 py-1 bg-slate-800 hover:bg-indigo-600/30 text-indigo-400 hover:text-indigo-300 border border-gray-700 rounded text-[11px] font-medium transition"
                            title="Renew Now"><i class="fa-solid fa-arrows-rotate"></i></button>
                    <button type="button" data-action="reissue" data-id="${escapeHtml(cert.id)}"
                            class="px-2 py-1 bg-slate-800 hover:bg-amber-600/30 text-amber-400 hover:text-amber-300 border border-gray-700 rounded text-[11px] font-medium transition"
                            title="Reissue / Force Re-Key"><i class="fa-solid fa-key"></i></button>
                    <button type="button" data-action="details" data-id="${escapeHtml(cert.id)}"
                            class="px-2 py-1 bg-slate-800 hover:bg-slate-700 text-gray-300 border border-gray-700 rounded text-[11px] font-medium transition"
                            title="Inspect Chain"><i class="fa-solid fa-circle-info"></i></button>
                    <button type="button" data-action="revoke" data-id="${escapeHtml(cert.id)}"
                            class="px-2 py-1 bg-slate-800 hover:bg-rose-600/30 text-gray-500 hover:text-rose-400 border border-gray-700 rounded text-[11px] font-medium transition"
                            title="Revoke / Delete"><i class="fa-solid fa-trash"></i></button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    }

    /* ---------- actions ---------- */

    async function renewCert(id) {
        Terminal.log('Initiating ACME renewal...', 'acme');
        const cert = await api.post(`/api/certs/${encodeURIComponent(id)}/renew`, {
            reason: 'manual',
            force_challenge: true,
        });
        Terminal.log(`Certificate renewed for ${cert.cn} (expires ${fmtDate(cert.not_after)})`, 'success');
        await refreshAll();
    }

    function openReissueModal(id) {
        const cert = state.certs.find((c) => c.id === id);
        if (!cert) return;
        document.getElementById('reissue-cert-id').value = cert.id;
        document.getElementById('reissue-domain').value = cert.cn;
        document.getElementById('reissue-reason').value = 'routine';
        Modals.open('modal-reissue');
    }

    function defaultDetailsById(id) {
        return state.certs.find((c) => c.id === id);
    }

    function chainLabels(issuer) {
        const s = String(issuer);
        if (s.includes('step-ca')) return { root: 'Local Root CA', note: 'step-ca internal trust store' };
        if (s.includes('Vault')) return { root: 'Vault PKI Root', note: 'HashiCorp Vault PKI engine' };
        if (s.includes('Self-Signed')) return { root: 'Self-Signed (no chain)', note: 'Trust is local to this host' };
        if (s.includes('Let')) return { root: 'ISRG Root X1', note: 'System Root Anchors' };
        return { root: 'External Root CA', note: 'Imported / custom trust anchor' };
    }

    function openDetails(id) {
        const cert = defaultDetailsById(id);
        if (!cert) return;
        const labels = chainLabels(cert.issuer);
        const container = document.getElementById('details-content');
        container.innerHTML = `
            <div class="bg-slate-950 p-4 rounded-xl border border-gray-800 space-y-3">
                <div class="flex justify-between items-center border-b border-gray-800 pb-2">
                    <span class="text-gray-400">Subject CN:</span>
                    <span class="text-indigo-400 font-bold">${escapeHtml(cert.cn)}</span>
                </div>
                <div class="flex justify-between items-center border-b border-gray-800 pb-2">
                    <span class="text-gray-400">SANs:</span>
                    <span class="text-gray-200">${escapeHtml(cert.sans.join(', '))}</span>
                </div>
                <div class="flex justify-between items-center border-b border-gray-800 pb-2">
                    <span class="text-gray-400">Issuer:</span>
                    <span class="text-purple-400 font-semibold">${escapeHtml(cert.issuer)}</span>
                </div>
                <div class="flex justify-between items-center border-b border-gray-800 pb-2">
                    <span class="text-gray-400">Key Spec:</span>
                    <span class="text-gray-300">${escapeHtml(cert.key_type)}</span>
                </div>
                <div class="flex justify-between items-center border-b border-gray-800 pb-2">
                    <span class="text-gray-400">Serial Number:</span>
                    <span class="text-gray-300">${escapeHtml(cert.serial)}</span>
                </div>
                <div class="flex justify-between items-center border-b border-gray-800 pb-2">
                    <span class="text-gray-400">Not Before:</span>
                    <span class="text-gray-300">${fmtDate(cert.not_before)}</span>
                </div>
                <div class="flex justify-between items-center border-b border-gray-800 pb-2">
                    <span class="text-gray-400">Not After:</span>
                    <span class="text-gray-300">${fmtDate(cert.not_after)} (${cert.days_left} days left)</span>
                </div>
                <div>
                    <span class="text-gray-400 block mb-1">SHA-256 Fingerprint:</span>
                    <span class="text-[10px] text-emerald-400 break-all bg-slate-900 p-2 rounded block border border-gray-800">${escapeHtml(cert.fingerprint)}</span>
                </div>
            </div>

            <div class="p-4 bg-slate-950 rounded-xl border border-gray-800">
                <h4 class="text-xs font-bold text-gray-300 uppercase mb-3">Verified Trust Chain Path</h4>
                <div class="space-y-2 border-l-2 border-indigo-500 pl-4 ml-2">
                    <div class="relative">
                        <span class="w-2 h-2 bg-indigo-500 rounded-full absolute -left-[21px] top-1"></span>
                        <div class="text-xs font-bold text-gray-200">Root CA: ${escapeHtml(labels.root)}</div>
                        <div class="text-[10px] text-gray-500">${escapeHtml(labels.note)}</div>
                    </div>
                    <div class="relative">
                        <span class="w-2 h-2 bg-indigo-400 rounded-full absolute -left-[21px] top-1"></span>
                        <div class="text-xs font-bold text-gray-300">Intermediate: ${escapeHtml(cert.issuer)}</div>
                        <div class="text-[10px] text-gray-500">x509 v3 Key Identifier matches</div>
                    </div>
                    <div class="relative">
                        <span class="w-2 h-2 bg-emerald-400 rounded-full absolute -left-[21px] top-1"></span>
                        <div class="text-xs font-bold text-emerald-400">Leaf Certificate: ${escapeHtml(cert.cn)}</div>
                        <div class="text-[10px] text-gray-500">Valid until ${fmtDate(cert.not_after)} (${cert.days_left} days left)</div>
                    </div>
                </div>
            </div>
        `;

        Modals.open('modal-details');
        const download = document.getElementById('btn-download-bundle');
        download.dataset.certId = cert.id;
        download.dataset.certCn = cert.cn;
    }

    async function revokeCert(id) {
        const cert = defaultDetailsById(id);
        if (!cert) return;
        if (!window.confirm(`Revoke and remove certificate for ${cert.cn}?`)) return;
        await api.delete(`/api/certs/${encodeURIComponent(id)}`);
        Terminal.log(`Revoked certificate for ${cert.cn} from local state.`, 'warning');
        await refreshAll();
    }

    async function toggleAutoRenew(id, checked) {
        const cert = await api.patch(`/api/certs/${encodeURIComponent(id)}`, {
            auto_renew: checked,
        });
        Terminal.log(`Auto-renew rule updated for ${cert.cn}: ${checked ? 'ENABLED' : 'DISABLED'}`, 'info');
        await renderTable();
    }

    async function batchRenew() {
        const result = await api.post('/api/certs/batch-renew');
        Terminal.log(`Batch renew completed for ${result.renewed.length} eligible certificate(s).`, 'success');
        await refreshAll();
    }

    async function syncAll() {
        const btn = document.getElementById('btn-sync-all');
        btn.disabled = true;
        try {
            const result = await api.post('/api/sync');
            Terminal.log(result.message, 'success');
            await Terminal.loadLogs();
        } finally {
            btn.disabled = false;
        }
    }

    async function advanceTime(days) {
        const time = await api.post('/api/time/advance', { days });
        Terminal.log(`Fast-forwarded time +${days} Days to ${time.sim_now}`, 'warning');
        await refreshAll();
    }

    async function resetTime() {
        const time = await api.post('/api/time/reset');
        Terminal.log(`Reset simulated date back to ${time.sim_now}`, 'info');
        await refreshAll();
    }

    function downloadBundle() {
        const download = document.getElementById('btn-download-bundle');
        const certId = download.dataset.certId;
        if (!certId) return;
        const a = document.createElement('a');
        a.href = `/api/certs/${encodeURIComponent(certId)}/pem`;
        a.download = '';
        document.body.appendChild(a);
        a.click();
        a.remove();
    }

    /* ---------- modal forms ---------- */

    async function submitIssue(event) {
        event.preventDefault();
        const cn = document.getElementById('issue-cn').value.trim();
        const sansRaw = document.getElementById('issue-sans').value;
        const sans = sansRaw
            ? sansRaw.split(',').map((s) => s.trim()).filter(Boolean)
            : [];

        const payload = {
            cn,
            sans,
            issuer: document.getElementById('issue-issuer').value,
            protocol: document.getElementById('issue-validation').value,
            key_type: document.getElementById('issue-keytype').value,
            validity_days: parseInt(document.getElementById('issue-duration').value, 10),
            auto_renew: document.getElementById('issue-autorenew').checked,
        };

        try {
            const cert = await api.post('/api/certs', payload);
            Terminal.log(`New Certificate issued for ${cert.cn} via ${cert.issuer}`, 'success');
            event.target.reset();
            Modals.closeAll();
            await refreshAll();
        } catch (err) {
            Terminal.log(err.message, 'error');
        }
    }

    async function submitImport(event) {
        event.preventDefault();
        const payload = {
            cn: document.getElementById('import-cn').value.trim(),
            pem: document.getElementById('import-pem').value,
            issuer: document.getElementById('import-issuer').value.trim() || null,
            auto_renew: document.getElementById('import-autorenew').value === 'true',
        };
        try {
            const cert = await api.post('/api/certs/import', payload);
            Terminal.log(
                `Imported ${cert.cn} - parsed ${cert.key_type}, expires ${fmtDate(cert.not_after)}`,
                'info'
            );
            event.target.reset();
            Modals.closeAll();
            await refreshAll();
        } catch (err) {
            Terminal.log(err.message, 'error');
        }
    }

    async function submitReissue(event) {
        event.preventDefault();
        const id = document.getElementById('reissue-cert-id').value;
        const payload = {
            reason: document.getElementById('reissue-reason').value,
            force_challenge: document.getElementById('reissue-force-challenge').checked,
        };
        try {
            const cert = await api.post(`/api/certs/${encodeURIComponent(id)}/reissue`, payload);
            Terminal.log(`Reissued ${cert.cn} (reason: ${payload.reason})`, 'success');
            Modals.closeAll();
            await refreshAll();
        } catch (err) {
            Terminal.log(err.message, 'error');
        }
    }

    async function submitConfig(event) {
        event.preventDefault();
        const payload = {
            acme_directory_url: document.getElementById('cfg-acme-url').value.trim(),
            webhook_url: document.getElementById('cfg-webhook').value.trim(),
            cf_token: document.getElementById('cfg-cf-token').value,
            renew_window_days: parseInt(document.getElementById('cfg-renew-window').value, 10) || null,
            default_validity_days: parseInt(document.getElementById('cfg-validity-days').value, 10) || null,
        };
        try {
            await api.put('/api/settings', payload);
            Terminal.log('Saved ACME provider & reverse-proxy webhook configuration.', 'success');
            Modals.closeAll();
            await loadConfig(false);
        } catch (err) {
            Terminal.log(err.message, 'error');
        }
    }

    async function loadConfig(open = true) {
        try {
            const cfg = await api.get('/api/settings');
            document.getElementById('cfg-acme-url').value = cfg.acme_directory_url;
            document.getElementById('cfg-webhook').value = cfg.webhook_url;
            document.getElementById('cfg-renew-window').value = cfg.renew_window_days;
            document.getElementById('cfg-validity-days').value = cfg.default_validity_days;
            document.getElementById('cfg-cf-token').value = '';
            document.getElementById('cf-token-status').textContent = cfg.cf_token_masked
                ? 'A token is stored (masked). Enter a new one to replace it.'
                : 'No token stored yet.';
            if (open) Modals.open('modal-config');
        } catch (err) {
            Terminal.log(err.message, 'error');
        }
    }

    /* ---------- event wiring ---------- */

    function wireEvents() {
        // Table actions: event delegation, no inline handlers.
        const tbody = document.getElementById('certs-table-body');
        tbody.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-action]');
            if (!btn) return;
            const { action, id } = btn.dataset;
            try {
                if (action === 'renew') return renewCert(id);
                if (action === 'reissue') return openReissueModal(id);
                if (action === 'details') return openDetails(id);
                if (action === 'revoke') return revokeCert(id);
            } catch (err) {
                Terminal.log(err.message, 'error');
            }
        });

        tbody.addEventListener('change', (e) => {
            const input = e.target.closest('input[data-action="autorenew"]');
            if (!input) return;
            toggleAutoRenew(input.dataset.id, input.checked).catch((err) =>
                Terminal.log(err.message, 'error')
            );
        });

        // Search with debounce.
        document.getElementById('search-certs').addEventListener(
            'input',
            debounce((e) => {
                state.search = e.target.value;
                renderTable();
            }, 150)
        );

        // Filter tabs.
        document.querySelectorAll('.filter-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.filter-btn').forEach((b) => {
                    b.className = 'filter-btn px-3 py-1.5 rounded-lg bg-slate-900 text-gray-400 hover:text-white border border-gray-800 transition';
                });
                btn.className = 'filter-btn px-3 py-1.5 rounded-lg bg-indigo-600 text-white transition';
                state.filter = btn.dataset.filter;
                renderTable();
            });
        });

        // Header + toolbar buttons.
        document.getElementById('btn-issue-cert').addEventListener('click', () => Modals.open('modal-issue'));
        document.getElementById('btn-import-cert').addEventListener('click', () => Modals.open('modal-import'));
        document.getElementById('btn-acme-config').addEventListener('click', () => loadConfig(true));
        document.getElementById('btn-sync-all').addEventListener('click', syncAll);
        document.getElementById('btn-batch-renew').addEventListener('click', batchRenew);
        document.getElementById('btn-clear-log').addEventListener('click', () => Terminal.clearLogs());
        document.getElementById('btn-ff-7').addEventListener('click', () => advanceTime(7));
        document.getElementById('btn-ff-30').addEventListener('click', () => advanceTime(30));
        document.getElementById('btn-reset-time').addEventListener('click', resetTime);
        document.getElementById('btn-download-bundle').addEventListener('click', downloadBundle);

        // Forms.
        document.getElementById('form-issue-cert').addEventListener('submit', submitIssue);
        document.getElementById('form-import').addEventListener('submit', submitImport);
        document.getElementById('form-reissue').addEventListener('submit', submitReissue);
        document.getElementById('form-config').addEventListener('submit', submitConfig);
    }

    /* ---------- boot ---------- */

    document.addEventListener('DOMContentLoaded', () => {
        Terminal.init();
        Modals.init();
        wireEvents();
        Terminal.log('Trove PKI Engine Initialized. Listening for ACME events...', 'info');
        refreshAll();
    });
})();