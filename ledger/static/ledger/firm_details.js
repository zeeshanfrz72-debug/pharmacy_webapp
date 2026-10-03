/* Disclosure links retain their normal destination when JS is unavailable. */
(() => {
    async function load(target, url) {
        target.setAttribute('aria-busy', 'true');
        try {
            const response = await fetch(url, {headers: {'X-Requested-With': 'XMLHttpRequest'}});
            if (!response.ok || response.redirected) throw new Error('Firm details could not load.');
            target.innerHTML = await response.text();
            target.dataset.loaded = 'true';
        } catch (_) {
            target.replaceChildren();
            const link = document.createElement('a');
            link.href = url;
            ledgerUI.set(link, 'Could not load bills. Open firm details to retry.');
            target.appendChild(link);
        } finally { target.removeAttribute('aria-busy'); }
    }
    document.addEventListener('click', event => {
        const disclosure = event.target.closest('[data-firm-details]');
        if (disclosure && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) {
            const row = document.getElementById(disclosure.getAttribute('aria-controls'));
            if (!row) return;
            event.preventDefault();
            row.hidden = !row.hidden;
            disclosure.setAttribute('aria-expanded', String(!row.hidden));
            const content = row.querySelector('[data-firm-content]');
            if (!row.hidden && !content.dataset.loaded) load(content, disclosure.href);
        }
        const page = event.target.closest('[data-firm-page]');
        if (page && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) {
            const target = page.closest('[data-firm-content]');
            if (!target) return;
            event.preventDefault();
            load(target, page.href).then(() => target.querySelector('.table-pagination')?.focus());
        }
    });
})();
