/* Explicit presentation labels only: never walk/translate arbitrary record text. */
(() => {
    const catalog = JSON.parse(document.getElementById('bilingual-catalog').textContent);
    const labels = new Map(Object.entries(catalog.labels).map(([key, pair]) => [key.toLowerCase(), pair]));
    function pair(value) {
        if (Array.isArray(value)) return value;
        const known = labels.get(String(value).toLowerCase());
        if (known) return known;
        for (const [pattern, translation] of catalog.error_patterns) {
            const match = new RegExp(`^${pattern}$`).exec(String(value));
            if (match) return [String(value), translation.replace(/\{(\d+)\}/g, (_, index) => `\u2068${match[Number(index)]}\u2069`)];
        }
        return [String(value), ''];
    }
    function set(element, value) {
        if (!element) return;
        const [english, urdu] = pair(value);
        if (!urdu) { element.textContent = english; return; }
        const wrapper = document.createElement('span');
        wrapper.className = 'bi-label';
        for (const [language, direction, text] of [['en', 'ltr', english], ['ur', 'rtl', urdu]]) {
            const span = document.createElement('span');
            span.lang = language; span.dir = direction; span.className = `bi-${language}`;
            span.textContent = text; wrapper.appendChild(span);
        }
        element.replaceChildren(wrapper);
    }
    function text(value) { const [english, urdu] = pair(value); return urdu ? `${english} — \u2067${urdu}\u2069` : english; }
    function format(key, values) {
        return catalog.formats[key].map(pattern => pattern.replace(/\{(\w+)\}/g, (_, name) => `\u2068${values[name] ?? ''}\u2069`));
    }
    function errorDetail(errors) {
        if (typeof errors === 'string') return text(errors);
        if (Array.isArray(errors)) return errors.map(errorDetail).join(' · ');
        if (errors && typeof errors === 'object') {
            if ('message' in errors) return text(errors.message);
            return Object.values(errors).map(errorDetail).join(' · ');
        }
        return String(errors ?? '');
    }
    function isolate(value) { return `\u2068${String(value)}\u2069`; }
    window.ledgerUI = {set, text, pair, format, errorDetail, isolate};
})();
