/* Older saved links used a fragment for the now separate page-local Trash view. */
(() => {
    const toolbar = document.querySelector('[data-page-trash-url]');
    if (!toolbar) return;
    function openLegacyTrash() {
        if (location.hash === '#page-trash' && new URL(location.href).searchParams.get('view') !== 'trash') {
            location.replace(toolbar.dataset.pageTrashUrl);
        }
    }
    window.addEventListener('hashchange', openLegacyTrash);
    openLegacyTrash();
})();
