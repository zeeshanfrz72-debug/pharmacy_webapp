// Durable offline queue. Acknowledgement means the IndexedDB transaction committed.
const DB_NAME = 'pharmacy_ledger_db';
const STORE_NAME = 'pending_transactions';
let syncPausedForAuthentication = false;
let syncInFlight = null;
const account = () => document.body.dataset.offlineUserId || 'anonymous';
function initDB() {
    return new Promise((resolve, reject) => {
        let blocked = false;
        const request = indexedDB.open(DB_NAME, 3);
        request.onupgradeneeded = () => {
            if (!request.result.objectStoreNames.contains(STORE_NAME))
                request.result.createObjectStore(STORE_NAME, {keyPath: 'id', autoIncrement: true});
        };
        request.onsuccess = () => {
            request.result.onversionchange = () => request.result.close();
            if (blocked) request.result.close();
            else resolve(request.result);
        };
        request.onerror = () => reject(request.error);
        request.onblocked = () => { blocked = true; reject(new Error('Close older tabs before saving or synchronizing offline transactions.')); };
    });
}
function queueTransaction(db, mode, work) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction(STORE_NAME, mode);
        let value;
        tx.oncomplete = () => resolve(value);
        tx.onerror = tx.onabort = () => reject(tx.error || new Error('Offline storage did not commit.'));
        try { work(tx.objectStore(STORE_NAME), result => value = result); }
        catch (error) { tx.abort(); reject(error); }
    });
}
async function queueItems(db) {
    return queueTransaction(db, 'readonly', (store, result) => {
        store.getAll().onsuccess = event => result(event.target.result);
    });
}
async function saveToOfflineQueue(url, formData) {
    const data = {};
    formData.forEach((value, key) => { if (key !== 'csrfmiddlewaretoken') data[key] = value; });
    data.request_id = data.request_id || crypto.randomUUID();
    const db = await initDB();
    await queueTransaction(db, 'readwrite', store => store.add({url, method: 'POST', data,
        requestId: data.request_id, accountId: account(), timestamp: Date.now(), state: 'pending'}));
    await updateSyncIndicator();
}
// Read, assign identity, and write legacy records in ONE serialized transaction.
async function claimItems(db) {
    return queueTransaction(db, 'readwrite', (store, result) => {
        store.getAll().onsuccess = event => {
            const items = event.target.result;
            for (const item of items) {
                if (!item.accountId) item.accountId = account();
                if (item.accountId !== account()) continue;
                item.data.request_id = item.data.request_id || item.requestId || crypto.randomUUID();
                item.requestId = item.data.request_id;
                delete item.data.csrfmiddlewaretoken;
                store.put(item);
            }
            result(items);
        };
    });
}
const supplierKey = item => {
    const value = String(item.data.firm || item.data['bill-firm'] || 'unknown').trim();
    return /^[+-]?\d+$/.test(value) ? BigInt(value).toString() : value;
};
const toast = (message, type = 'warning') => { if (window.showToast) window.showToast(message, type, 7000); };
async function runSync() {
    if (!navigator.onLine || syncPausedForAuthentication || account() === 'anonymous') return;
    const db = await initDB();
    const items = await claimItems(db);
    const blocked = new Set();
    const csrf = window.csrftoken || document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
    for (const item of items) {
        if (item.accountId !== account()) continue;
        const supplier = supplierKey(item);
        if (blocked.has(supplier)) continue;
        if (['rejected', 'conflict'].includes(item.state)) { blocked.add(supplier); continue; }
        try {
            const url = new URL(item.url, location.href);
            if (url.origin !== location.origin) throw new Error('Queued destination must belong to this application.');
            const response = await fetch(url.href, {method: 'POST', redirect: 'manual',
                headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrf,
                    'X-Offline-Sync': '1', 'X-Ledger-Queue-Version': '4', 'X-Requested-With': 'XMLHttpRequest'},
                body: new URLSearchParams(item.data).toString()});
            if (response.type === 'opaqueredirect' || [401, 403].includes(response.status)) {
                syncPausedForAuthentication = true;
                toast('Sign in again before syncing queued transactions. They are saved on this device.');
                break;
            }
            const result = (response.headers.get('content-type') || '').includes('application/json') ? await response.json() : null;
            if (response.ok && result?.success === true && ['created', 'duplicate'].includes(result.status)) {
                await queueTransaction(db, 'readwrite', store => store.delete(item.id));
                window.dispatchEvent(new Event('ledger-records-changed'));
                try { localStorage.setItem('ledger-records-changed', Date.now().toString()); } catch (_) {}
                continue;
            }
            item.state = [400, 428].includes(response.status) && result?.success === false ? 'rejected' : response.status === 409 && result?.success === false ? 'conflict' : 'retry';
            item.error = result?.error ? ledgerUI.errorDetail(result.error) : result?.errors ? ledgerUI.errorDetail(result.errors) : `HTTP ${response.status}`;
            await queueTransaction(db, 'readwrite', store => store.put(item));
            blocked.add(supplier);
            toast(ledgerUI.format('queue_attention', {detail: item.error}), 'error');
        } catch (error) {
            // Response loss may follow a commit. Preserve the original identity.
            item.state = 'retry'; item.error = String(error.message);
            await queueTransaction(db, 'readwrite', store => store.put(item));
            blocked.add(supplier);
        }
    }
    await updateSyncIndicator();
    if (!(await queueItems(db)).some(item => item.accountId === account())) toast('Offline data synced successfully!', 'success');
}
function syncOfflineData() {
    if (syncInFlight) return syncInFlight;
    // Web Locks coordinates every tab on this origin. Without it, keep the queue.
    if (!navigator.locks) { toast('This browser cannot coordinate queue synchronization. Use a supported secure browser.', 'error'); return Promise.resolve(); }
    syncInFlight = navigator.locks.request('pharmacy-ledger-sync', runSync)
        .catch(error => toast(`Queue retained: ${error.message}`, 'error')).finally(() => { syncInFlight = null; });
    return syncInFlight;
}
async function queuedCount(db) { return (await queueItems(db)).length; }
async function updateSyncIndicator() {
    const db = await initDB();
    const items = (await queueItems(db)).filter(item => !item.accountId || item.accountId === account());
    let indicator = document.getElementById('offline-sync-indicator');
    if (!indicator) {
        indicator = document.createElement('details'); indicator.id = 'offline-sync-indicator';
        Object.assign(indicator.style, {position: 'fixed', top: '10px', right: '10px', background: '#fff', color: '#222', padding: '10px', zIndex: '9999', maxWidth: '440px', maxHeight: '75vh', overflow: 'auto'});
        document.body.appendChild(indicator);
    }
    indicator.replaceChildren(); indicator.hidden = items.length === 0;
    const summary = document.createElement('summary'); ledgerUI.set(summary, ledgerUI.format('pending', {count: items.length})); indicator.appendChild(summary);
    const exportButton = document.createElement('button'); exportButton.type = 'button'; exportButton.textContent = 'Export queued transactions';
    exportButton.onclick = () => {
        const url = URL.createObjectURL(new Blob([JSON.stringify(items, null, 2)], {type: 'application/json'}));
        const link = document.createElement('a'); link.href = url; link.download = 'queued-transactions.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }; indicator.appendChild(exportButton);
    const blocked = new Set();
    for (const item of items) {
        const detail = document.createElement('details'); const title = document.createElement('summary');
        title.textContent = `Supplier ${supplierKey(item)}: ${blocked.has(supplierKey(item)) ? 'waiting for earlier action' : item.state || 'pending'} - ${item.error || ''}`;
        detail.appendChild(title);
        if (item.state === 'conflict') {
            const warning = document.createElement('p'); warning.textContent = 'This request ID already identifies a different committed action. Review the existing transaction before entering another one.';
            const review = document.createElement('a'); review.textContent = 'Review committed transactions'; review.href = `/ledger/history/?firm_id=${encodeURIComponent(supplierKey(item))}`;
            detail.append(warning, review);
        }
        const fields = {};
        const vocabulary = {source_type: 'Source Type', firm: 'Firm', representative: 'Representative',
            bill_choice: 'Bill', new_bill_number: 'Bill Number', new_bill_amount: 'Bill Amount',
            payment_choice: 'Payment Amount', custom_payment_amount: 'Other Amount', notes: 'Notes', bill_date: 'Bill Date'};
        const lookup = typeof offlineData === 'undefined' ? {} : offlineData;
        const suppliers = Object.entries(lookup.firms_by_source || {}).flatMap(([type, records]) => records.map(record => ({...record, type})));
        for (const [key, value] of Object.entries(item.data)) {
            if (['request_id', 'csrfmiddlewaretoken'].includes(key) || key.endsWith('posting_rules_version')) continue;
            const canonical = key.replace(/^bill-/, '');
            let options;
            if (canonical === 'firm' && suppliers.length) options = suppliers.map(record => [String(record.id), record.name]);
            if (canonical === 'representative' && lookup.reps_by_firm) options = Object.entries(lookup.reps_by_firm).flatMap(([firmId, records]) => records.map(record => [String(record.id), `${record.name} (${suppliers.find(supplier => String(supplier.id) === firmId)?.name || firmId})`]));
            if (canonical === 'bill_choice') options = [['add_new', 'Add New Bill'], ...Object.entries(lookup.bills_by_firm || {}).flatMap(([firmId, records]) => records.map(record => [String(record.id), `${record.display_reference} (${suppliers.find(supplier => String(supplier.id) === firmId)?.name || firmId})`]))];
            if (canonical === 'source_type') options = [['direct_company', 'Direct Company'], ['distributor', 'Distributor Company'], ['stockist', 'Stockist Company'], ['local_market', 'Open / Local Market']];
            if (canonical === 'payment_choice') options = ['1000', '1500', '2000', '2500', '3000', 'other'].map(amount => [amount, amount === 'other' ? 'Other Amount' : amount]);
            const label = document.createElement('label'); ledgerUI.set(label, vocabulary[canonical] || canonical.replaceAll('_', ' '));
            const input = document.createElement(options ? 'select' : 'input');
            if (options) {
                for (const [id, name] of [['', 'Select'], ...options]) { const option = document.createElement('option'); option.value = id; option.textContent = name; input.appendChild(option); }
                if (value && !options.some(([id]) => id === String(value))) { const old = document.createElement('option'); old.value = value; old.textContent = `Unavailable reference ${value}`; input.appendChild(old); }
            } else { input.type = 'text'; if (canonical.includes('amount')) input.inputMode = 'decimal'; }
            input.value = value; input.dataset.queueField = key; input.disabled = item.state !== 'rejected';
            label.appendChild(input); detail.appendChild(label); fields[key] = input;
            if (canonical === 'firm') input.onchange = () => {
                const supplier = suppliers.find(record => String(record.id) === input.value);
                const source = fields.source_type || fields['bill-source_type'];
                if (source && supplier) source.value = supplier.type;
            };
        }
        const retry = document.createElement('button'); retry.type = 'button';
        retry.textContent = item.state === 'rejected' ? 'Save correction as a new request' : 'Retry original request';
        retry.onclick = async () => {
            try {
                let correctedData;
                if (item.state === 'rejected') {
                    correctedData = {...item.data};
                    for (const [key, input] of Object.entries(fields)) {
                        const value = input.value;
                        if (key.includes('amount') && value && (!/^\d+(\.\d{1,2})?$/.test(value) || Number(value) <= 0 || Number(value) > 9999999999.99)) throw new Error('Enter a positive amount in whole cents within the supported limit.');
                        correctedData[key] = value;
                    }
                }
                // Wait for synchronization, then reread to avoid resurrecting an acknowledged item.
                await navigator.locks.request('pharmacy-ledger-sync', async () => {
                    await queueTransaction(db, 'readwrite', store => {
                        store.get(item.id).onsuccess = event => {
                            const latest = event.target.result;
                            if (!latest) return;
                            if (latest.state === 'rejected') {
                                if (!correctedData || latest.requestId !== item.requestId) { toast('The action changed. Refresh the queue before editing.', 'error'); return; }
                                latest.corrections = [...(latest.corrections || []), {data: latest.data, error: latest.error, timestamp: Date.now()}];
                                correctedData[item.data['bill-firm'] ? 'bill-posting_rules_version' : 'posting_rules_version'] = '2';
                                latest.data = correctedData; latest.data.request_id = crypto.randomUUID(); latest.requestId = latest.data.request_id;
                            }
                            latest.state = 'pending'; delete latest.error; store.put(latest);
                        };
                    });
                });
                syncPausedForAuthentication = false; await syncOfflineData(); await updateSyncIndicator();
            } catch (error) { toast(`Queue retained: ${error.message}`, 'error'); }
        }; detail.appendChild(retry); indicator.appendChild(detail);
        if (['rejected', 'retry', 'conflict'].includes(item.state)) blocked.add(supplierKey(item));
    }
}
window.addEventListener('online', () => { syncPausedForAuthentication = false; syncOfflineData(); const el = document.getElementById('offline-indicator'); if (el) el.style.display = 'none'; });
window.addEventListener('offline', () => { const el = document.getElementById('offline-indicator'); if (el) el.style.display = 'inline-block'; });
window.addEventListener('load', () => { updateSyncIndicator().catch(error => toast(`Offline storage unavailable: ${error.message}`, 'error')); syncOfflineData(); });
