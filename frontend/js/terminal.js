/* Terminal: mirrors the server's RenewalLog history. */
const Terminal = {
    el: null,
    countEl: null,

    init() {
        this.el = document.getElementById('terminal-log');
        this.countEl = document.getElementById('terminal-count');
    },

    log(message, type = 'info') {
        const entry = document.createElement('div');
        const ts = new Date().toISOString().split('T')[1].slice(0, 8);

        let cls = 'text-gray-300';
        let prefix = '[INFO]';
        if (type === 'success') { cls = 'text-emerald-400'; prefix = '[SUCCESS]'; }
        if (type === 'warning') { cls = 'text-amber-400'; prefix = '[WARN]'; }
        if (type === 'acme') { cls = 'text-sky-400'; prefix = '[ACME]'; }
        if (type === 'error') { cls = 'text-rose-400'; prefix = '[ERR]'; }

        entry.className = `${cls} text-[11px] font-mono`;
        entry.innerHTML =
            `<span class="text-gray-600">[${ts}]</span> ` +
            `<span class="font-bold">${prefix}</span> ${escapeHtml(message)}`;
        this.el.appendChild(entry);
        this.el.scrollTop = this.el.scrollHeight;
        this._updateCount();
    },

    async loadLogs() {
        const logs = await api.get('/api/logs?limit=200');
        this.clear();
        logs.forEach((l) => this.log(l.message, l.level));
    },

    async clearLogs() {
        await api.delete('/api/logs');
        this.clear();
    },

    clear() {
        this.el.innerHTML = '';
        this._updateCount();
    },

    _updateCount() {
        if (this.countEl) this.countEl.textContent = `${this.el.children.length} events`;
    },
};
