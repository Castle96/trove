/* Modal helpers: open/close, focus trap, Escape, backdrop click. */
const Modals = {
    _lastFocus: null,

    open(id) {
        const modal = document.getElementById(id);
        if (!modal) return;
        this._lastFocus = document.activeElement;
        modal.classList.remove('hidden');
        modal.classList.add('flex');
        modal.setAttribute('aria-hidden', 'false');

        const focusable = modal.querySelector(
            'input:not([type="hidden"]), select, textarea, button[data-autofocus], button:not(.modal-close)'
        );
        if (focusable) focusable.focus();
    },

    closeAll() {
        document.querySelectorAll('[data-modal]').forEach((modal) => {
            modal.classList.add('hidden');
            modal.classList.remove('flex');
            modal.setAttribute('aria-hidden', 'true');
        });
        if (this._lastFocus && document.contains(this._lastFocus)) {
            this._lastFocus.focus();
            this._lastFocus = null;
        }
    },

    isOpen(id) {
        return !document.getElementById(id).classList.contains('hidden');
    },

    init() {
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') this.closeAll();
        });

        document.querySelectorAll('[data-modal]').forEach((modal) => {
            // Close when clicking the backdrop (not inside the panel).
            modal.addEventListener('mousedown', (e) => {
                if (e.target === modal) this.closeAll();
            });
            modal.setAttribute('aria-hidden', 'true');
        });

        document.querySelectorAll('.modal-close').forEach((btn) => {
            btn.addEventListener('click', () => this.closeAll());
        });
    },
};