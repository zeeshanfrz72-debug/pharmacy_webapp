// offline_sync.js

// Initialize IndexedDB
const DB_NAME = 'pharmacy_ledger_db';
const STORE_NAME = 'pending_transactions';

function initDB() {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(DB_NAME, 1);
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
        dataObj[key] = value;
    });

    const payload = {
        url: url,
        method: 'POST',
        data: dataObj,
        timestamp: Date.now()
    };

    store.add(payload);
    
    // Update UI indicator if we have one
    updateSyncIndicator();
}

// Process the offline queue
async function syncOfflineData() {
    if (!navigator.onLine) return;

    const db = await initDB();
    const transaction = db.transaction(STORE_NAME, 'readwrite');
    const store = transaction.objectStore(STORE_NAME);
    const request = store.getAll();

    request.onsuccess = async () => {
        const pendingItems = request.result;
        if (pendingItems.length === 0) return;

        console.log(`Attempting to sync ${pendingItems.length} offline items...`);

        // Get CSRF token from page
        const csrfToken = window.csrftoken || document.querySelector('[name=csrfmiddlewaretoken]')?.value;

        for (const item of pendingItems) {
            try {
                // Reconstruct FormData
                const formData = new URLSearchParams();
                for (const key in item.data) {
                    formData.append(key, item.data[key]);
                }

                // Send it to the original URL
                const response = await fetch(item.url, {
                    method: item.method,
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-CSRFToken': csrfToken
                    },
                    body: formData.toString()
                });

                if (response.ok) {
                    // Remove from IndexedDB
                    const delTx = db.transaction(STORE_NAME, 'readwrite');
                    delTx.objectStore(STORE_NAME).delete(item.id);
                    console.log(`Synced item ${item.id}`);
                }
            } catch (err) {
                console.error("Sync failed for item, keeping in queue.", err);
                break; // Stop syncing if connection fails again
            }
        }
        
        updateSyncIndicator();
        if (typeof window.showToast === 'function') {
            window.showToast("Offline data synced successfully!");
            // Optionally reload page to show new data
            setTimeout(() => window.location.reload(), 1500);
        }
    };
}

// UI Indicator
async function updateSyncIndicator() {
    const db = await initDB();
    const tx = db.transaction(STORE_NAME, 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const countReq = store.count();
    
    countReq.onsuccess = () => {
        const count = countReq.result;
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

// Intercept form submissions
document.addEventListener('submit', async (e) => {
    // Only intercept if we are offline or explicitly want to
    if (!navigator.onLine) {
        // Prevent default submission
        e.preventDefault();
        
        const form = e.target;
        // Don't intercept GET forms like search
        if (form.method.toLowerCase() === 'get') return;

        const formData = new FormData(form);
        const url = form.action || window.location.href;

        await saveToOfflineQueue(url, formData);
        
        if (typeof window.showToast === 'function') {
            window.showToast("You are offline. Saved locally. Will sync when online.", 'warning');
        }

        // Optionally clear form
        form.reset();
    }
});

// Listen for connection status changes
window.addEventListener('online', () => {
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
