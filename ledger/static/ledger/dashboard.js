/* Keep the entry workspace current without clearing an in-progress form. */
(() => {
    const recent = document.getElementById('dashboard-recent');
    if (!recent) return;
    const analytics = document.getElementById('dashboard-analytics');
    const review = document.getElementById('dashboard-review');
    let pending = false, refreshAgain = false, reviewLoaded = false;
    let chartLibrary = null;
    function loadCharts() {
        if (window.Chart) { window.refreshDashboardCharts?.(); return; }
        if (!chartLibrary) {
            const controls = analytics.querySelectorAll('.chart-chip, #debt-chart-eye');
            controls.forEach(button => { button.disabled = true; });
            chartLibrary = new Promise((resolve, reject) => {
                const script = document.createElement('script');
                script.src = 'https://cdn.jsdelivr.net/npm/chart.js';
                script.onload = resolve;
                script.onerror = () => { script.remove(); reject(new Error('Charts could not load. Check your connection and reopen Analytics to retry.')); };
                document.head.appendChild(script);
            }).then(() => {
                controls.forEach(button => { button.disabled = false; });
                document.getElementById('dashboard-chart-error')?.remove();
                window.refreshDashboardCharts?.();
            }).catch(failure => {
                chartLibrary = null;
                if (!document.getElementById('dashboard-chart-error')) {
                    const error = document.createElement('p');
                    error.id = 'dashboard-chart-error';
                    error.setAttribute('role', 'alert');
                    ledgerUI.set(error, failure.message);
                    review.after(error);
                }
            });
        }
    }
    async function loadReview() {
        review.setAttribute('aria-busy', 'true');
        try {
            const response = await fetch(review.dataset.url, {headers: {'X-Requested-With': 'XMLHttpRequest'}});
            if (!response.ok || response.redirected) throw new Error('Could not load record review.');
            review.innerHTML = await response.text();
            reviewLoaded = true;
        } catch (failure) {
            review.replaceChildren();
            const link = document.createElement('a');
            link.href = review.dataset.url;
            ledgerUI.set(link, 'Record review could not load. Open it here to retry.');
            review.appendChild(link);
        } finally { review.removeAttribute('aria-busy'); }
    }
    async function refresh() {
        if (pending) { refreshAgain = true; return; }
        if (!navigator.onLine) return;
        pending = true;
        const error = document.getElementById('dashboard-refresh-error');
        try {
            const response = await fetch(recent.dataset.url, {headers: {'X-Requested-With': 'XMLHttpRequest'}});
            if (!response.ok || response.redirected) throw new Error('Recent transactions could not refresh. Reload or sign in again to retry.');
            const data = await response.json();
            const focusWasInTable = recent.contains(document.activeElement);
            recent.innerHTML = data.html;
            if (focusWasInTable) document.getElementById('recent-heading').focus();
            document.querySelectorAll('[data-summary]').forEach(element => {
                if (data[element.dataset.summary] !== undefined) element.textContent = data[element.dataset.summary];
            });
            window.rawTotalDebt = data.total_debt;
            document.getElementById('dashboard-date-context').dataset.today = data.today_date;
            const balance = document.getElementById('total-debt-value');
            if (!balance.classList.contains('debt-hidden')) balance.textContent = data.total_debt;
            error.hidden = true;
            reviewLoaded = false;
            if (analytics.open) { loadReview(); loadCharts(); }
        } catch (failure) { ledgerUI.set(error, failure.message); error.hidden = false; }
        finally {
            pending = false;
            if (refreshAgain) { refreshAgain = false; refresh(); }
        }
    }
    analytics.addEventListener('toggle', () => {
        if (analytics.open) {
            if (!reviewLoaded) loadReview();
            loadCharts();
        }
    });
    window.addEventListener('ledger-records-changed', refresh);
    window.addEventListener('storage', event => { if (event.key === 'ledger-records-changed') refresh(); });
    window.addEventListener('online', refresh);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();
