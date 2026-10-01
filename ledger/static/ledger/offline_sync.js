// offline_sync.js

// Initialize IndexedDB
const DB_NAME = 'pharmacy_ledger_db';
const STORE_NAME = 'pending_transactions';
let syncPausedForAuthentication = false;

function initDB() {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(DB_NAME, 2);
        request.onupgradeneeded = (event) => {
            const db = event.target.result;
            if (!db.objectStoreNames.contains(STORE_NAME)) {
                db.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
            }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
    });
}

// Save transaction to IndexedDB
async function saveToOfflineQueue(url, formData) {
    const db = await initDB();
    const transaction = db.transaction(STORE_NAME, 'readwrite');
    const store = transaction.objectStore(STORE_NAME);

    // Convert FormData to plain object to store
    const dataObj = {};
    formData.forEach((value, key) => {
        if (key !== 'csrfmiddlewaretoken') dataObj[key] = value;
    });

    dataObj.request_id = dataObj.request_id || crypto.randomUUID();

    const payload = {
        url: url,
        method: 'POST',
        data: dataObj,
        requestId: dataObj.request_id,
        accountId: document.body.dataset.offlineUserId || 'anonymous',
        timestamp: Date.now()
    };

    store.add(payload);
    
    // Update UI indicator if we have one
    updateSyncIndicator();
}

// Process the offline queue
async function syncOfflineData() {
    if (!navigator.onLine || syncPausedForAuthentication) return;
    const accountId = document.body.dataset.offlineUserId || 'anonymous';
    if (accountId === 'anonymous') return;

    const db = await initDB();
    const transaction = db.transaction(STORE_NAME, 'readonly');
    const store = transaction.objectStore(STORE_NAME);
    const request = store.getAll();

    request.onsuccess = async () => {
        const pendingItems = request.result;
        if (pendingItems.length === 0) return;

        console.log(`Attempting to sync ${pendingItems.length} offline items...`);

        // Use the current account's CSRF token after the owner signs back in.
        const csrfToken = window.csrftoken || document.querySelector('[name=csrfmiddlewaretoken]')?.value;

        for (const item of pendingItems) {
            try {
                // Reconstruct FormData
                const formData = new URLSearchParams();
                if (!item.accountId) {
                    // The older queue had no account field. This app has one
                    // configured owner, so claim legacy items after login.
                    item.accountId = accountId;
                    item.data.request_id = item.data.request_id || crypto.randomUUID();
                    delete item.data.csrfmiddlewaretoken;
                    const claimTx = db.transaction(STORE_NAME, 'readwrite');
                    claimTx.objectStore(STORE_NAME).put(item);
                }
                if (item.accountId !== accountId) continue;
                for (const key in item.data) {
                    formData.append(key, item.data[key]);
                }

                // Send it to the original URL
                const response = await fetch(item.url, {
                    method: item.method,
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-CSRFToken': csrfToken || '',
                        'X-Offline-Sync': '1',
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    redirect: 'manual',
                    body: formData.toString()
                });

                if (response.type === 'opaqueredirect' || response.status === 401 || response.status === 403) {
                    syncPausedForAuthentication = true;
                    if (typeof window.showToast === 'function') {
                        window.showToast('Sign in again before syncing queued transactions. They are saved on this device.', 'warning', 7000);
                    }
                    break;
                }

                const contentType = response.headers.get('content-type') || '';
                let result = null;
                if (contentType.includes('application/json')) {
                    result = await response.json();
                }

                if (response.ok && result?.success === true && ['created', 'duplicate'].includes(result.status)) {
                    // Remove only after the server confirms a committed batch.
                    const delTx = db.transaction(STORE_NAME, 'readwrite');
                    delTx.objectStore(STORE_NAME).delete(item.id);
                    await new Promise((resolve, reject) => {
                        delTx.oncomplete = resolve;
                        delTx.onerror = () => reject(delTx.error);
                    });
                    console.log(`Synced item ${item.id}`);
                    continue;
                }
                const detail = result?.error || (result?.errors ? JSON.stringify(result.errors) : `HTTP ${response.status}`);
                if (typeof window.showToast === 'function') {
                    window.showToast(`Queued transaction needs attention: ${detail}`, 'error', 7000);
                }
                break;
            } catch (err) {
                console.error("Sync failed for item, keeping in queue.", err);
                break; // Stop syncing if connection fails again
            }
        }
        
        updateSyncIndicator();
        const remaining = await queuedCount(db);
        if (remaining === 0 && typeof window.showToast === 'function') {
            window.showToast("Offline data synced successfully!");
            // Optionally reload page to show new data
            setTimeout(() => window.location.reload(), 1500);
        }
    };
}

function queuedCount(db) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction(STORE_NAME, 'readonly');
        const request = tx.objectStore(STORE_NAME).count();
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
    });
}

// UI Indicator
async function updateSyncIndicator() {
    const db = await initDB();
    const tx = db.transaction(STORE_NAME, 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const countReq = store.getAll();
    
    countReq.onsuccess = () => {
        const accountId = document.body.dataset.offlineUserId || 'anonymous';
        const count = countReq.result.filter(item => !item.accountId || item.accountId === accountId).length;
        let indicator = document.getElementById('offline-sync-indicator');
        
        if (!indicator && count > 0) {
            indicator = document.createElement('div');
            indicator.id = 'offline-sync-indicator';
            indicator.style.position = 'fixed';
            indicator.style.top = '10px';
            indicator.style.right = '10px';
            indicator.style.background = '#f59e0b'; // warning color
            indicator.style.color = '#fff';
            indicator.style.padding = '4px 10px';
            indicator.style.borderRadius = '20px';
            indicator.style.fontSize = '12px';
            indicator.style.fontWeight = 'bold';
            indicator.style.zIndex = '9999';
            document.body.appendChild(indicator);
        }
        
        if (indicator) {
            if (count > 0) {
                indicator.style.display = 'block';
                indicator.textContent = `☁️ ${count} Pending Sync`;
            } else {
                indicator.style.display = 'none';
            }
        }
    };
}

// Listen for connection status changes
window.addEventListener('online', () => {
    syncPausedForAuthentication = false;
    syncOfflineData();
    const indicator = document.getElementById('offline-indicator');
    if (indicator) indicator.style.display = 'none';
});

window.addEventListener('offline', () => {
    const indicator = document.getElementById('offline-indicator');
    if (indicator) indicator.style.display = 'inline-block';
});

window.addEventListener('load', () => {
    updateSyncIndicator();
    if (!navigator.onLine) {
        const indicator = document.getElementById('offline-indicator');
        if (indicator) indicator.style.display = 'inline-block';
    }
    syncOfflineData();
});
