(() => {

    document.querySelectorAll('[data-record-form]').forEach(form => {

        const source = form.querySelector('select[name$="source_type"]');

        const firm = form.querySelector('select[name$="firm"]');

        const rep = form.querySelector('select[name$="representative"]');

        const error = form.querySelector('[data-selector-error]');

        let sourceVersion = 0, firmVersion = 0;

        function reset(select, text) {

            if (!select) return;

            select.replaceChildren(new Option(ledgerUI.text(text), ''));

        }

        function populate(select, records, selected) {

            records.forEach(record => select.add(new Option(record.name, record.id)));

            select.value = selected || '';

        }

        async function loadReps(selected = '') {

            if (!rep) return;

            const version = ++firmVersion;

            reset(rep, source.value === 'local_market' ? 'No representative — paid in cash' : 'Select representative');

            rep.disabled = source.value === 'local_market' || !firm.value;

            rep.required = source.value !== 'local_market';

            if (rep.disabled) return;

            try {

                const response = await fetch(`${form.dataset.repsUrl}?firm_id=${encodeURIComponent(firm.value)}`, {cache: 'no-store'});

                if (!response.ok) throw new Error('Could not load representatives. Refresh this page to retry.');

                const data = await response.json();

                if (version !== firmVersion) return;

                populate(rep, data.representatives, selected);

            } catch (failure) { if (version === firmVersion) ledgerUI.set(error, failure.message); }

        }

        async function loadFirms(selectedFirm = '', selectedRep = '') {

            const version = ++sourceVersion;

            ++firmVersion;

            reset(firm, 'Select firm'); reset(rep, 'Select representative');

            firm.disabled = !source.value;

            if (rep) rep.disabled = true;

            ledgerUI.set(error, '');

            if (!source.value) return;

            try {

                const response = await fetch(`${form.dataset.firmsUrl}?source_type=${encodeURIComponent(source.value)}`, {cache: 'no-store'});

                if (!response.ok) throw new Error('Could not load firms. Refresh this page to retry.');

                const data = await response.json();

                if (version !== sourceVersion) return;

                populate(firm, data.firms, selectedFirm);

                await loadReps(selectedRep);

            } catch (failure) { if (version === sourceVersion) ledgerUI.set(error, failure.message); }

        }

        source.addEventListener('change', () => loadFirms());

        firm.addEventListener('change', () => { ledgerUI.set(error, ''); loadReps(); });

        loadFirms(firm.value, rep ? rep.value : '');

    });

})();
