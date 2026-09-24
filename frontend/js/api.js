/* API client: thin fetch wrapper with optional bearer-token support. */
const api = {
    // Prefer the Trove key; fall back to the legacy certvault_token so existing
    // sessions keep working after the rebrand (migrated on next setToken).
    token: localStorage.getItem('trove_token') || localStorage.getItem('certvault_token') || '',

    setToken(token) {
        this.token = token || '';
        if (this.token) {
            localStorage.setItem('trove_token', this.token);
            localStorage.removeItem('certvault_token');
        } else {
            localStorage.removeItem('trove_token');
        }
    },

    // One prompt shared with the Dockwatch modules (frontend/js/dockwatch/core.js)
    // so both API clients never double-prompt on a 401.
    _prompted() {
        return Boolean(window.__troveAuthPrompted);
    },
    _markPrompt(start) {
        window.__troveAuthPrompted = start;
    },

    async request(method, path, body) {
        const headers = { Accept: 'application/json' };
        if (body !== undefined) headers['Content-Type'] = 'application/json';
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;

        const res = await fetch(path, {
            method,
            headers,
            body: body !== undefined ? JSON.stringify(body) : undefined,
        });

        // If the server demands an API key, prompt once (shared flag) and retry.
        if (res.status === 401 && !this._prompted()) {
            this._markPrompt(true);
            const entered = window.prompt(
                'Trove requires an API token. Enter it now (or press Cancel to abort):'
            );
            this._markPrompt(false);
            if (entered) {
                this.setToken(entered);
                return this.request(method, path, body);
            }
        }

        if (res.status === 204) return null;
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const detail = data.detail || data.message || `Request failed (${res.status})`;
            const message = typeof detail === 'string' ? detail : JSON.stringify(detail);
            throw new Error(message);
        }
        return data;
    },

    get(path) {
        return this.request('GET', path);
    },
    post(path, body) {
        return this.request('POST', path, body);
    },
    put(path, body) {
        return this.request('PUT', path, body);
    },
    patch(path, body) {
        return this.request('PATCH', path, body);
    },
    delete(path) {
        return this.request('DELETE', path);
    },
};