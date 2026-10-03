/* Real Chromium/IndexedDB tests of the repaired offline_sync.js.
 * The loopback server models the documented JSON commit/replay contract; it
 * never contacts the application or a real database. Requires playwright.
 * NODE_PATH must point at the installed package directory when nonstandard.
 * node scripts/accounting_audit_browser.cjs [chrome.exe] [output.json]
 */
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const code = fs.readFileSync(path.join(root, 'ledger/static/ledger/offline_sync.js'), 'utf8');
const results = { environment: {}, cases: [] };
const commits = new Set();
let calls = [];
let loseOnce = true;
const server = http.createServer(async (req, res) => {
  if (req.url === '/offline_sync.js') {
    res.setHeader('Content-Type', 'application/javascript'); res.end(code); return;
  }
  if (req.method === 'GET') {
    res.setHeader('Content-Type', 'text/html');
    res.end(`<!doctype html><body data-offline-user-id="synthetic-owner">
      <input name="csrfmiddlewaretoken" value="synthetic-token"><div id="dashboard-recent"></div>
      <script>window.toasts=[]; window.showToast=(...args)=>toasts.push(args);
      window.ledgerUI={set:(el,text)=>el.textContent=text,format:(name,args)=>JSON.stringify({name,args}),errorDetail:value=>JSON.stringify(value)};
      window.csrftoken='synthetic-token';</script>${req.url.includes("legacy=1") ? "" : '<script src="/offline_sync.js"></script>'}</body>`);
    return;
  }
  let body = '';
  for await (const part of req) body += part;
  const id = new URLSearchParams(body).get('request_id');
  calls.push({url:req.url, id});
  res.setHeader('Content-Type', 'application/json');
  if (req.url === '/bad' && new URLSearchParams(body).get('new_bill_amount') !== '100') {
    res.statusCode=400; res.end(JSON.stringify({success:false,error:'Choose a bill belonging to the selected firm.'})); return;
  }
  if (req.url === '/conflict') {
    res.statusCode=409;res.end(JSON.stringify({success:false,error:'This request ID was already used for different transaction data.'}));return;
  }
  if (req.url === '/auth') {
    res.statusCode=401; res.end(JSON.stringify({success:false,error:'authentication_required'})); return;
  }
  if (!commits.has(id) && req.headers['x-ledger-queue-version']!=='4') {
    res.statusCode=428;res.end(JSON.stringify({success:false,error:'Update the offline queue client.'}));return;
  }
  const duplicate=commits.has(id); commits.add(id);
  if (req.url === '/lost' && loseOnce) { loseOnce=false; req.socket.destroy(); return; }
  res.end(JSON.stringify({success:true,status:duplicate?'duplicate':'created'}));
});
async function pending(page) {
  return page.evaluate(async()=> {
    const db=await initDB();
    return await new Promise((resolve,reject)=>{
      const tx=db.transaction('pending_transactions','readonly');
      const request=tx.objectStore('pending_transactions').getAll();
      request.onsuccess=()=>resolve(request.result); request.onerror=()=>reject(request.error);
    });
  });
}
async function fill(page, items) {
  await page.evaluate(async items=>{
    const db=await initDB();
    await new Promise((resolve,reject)=>{
      const tx=db.transaction('pending_transactions','readwrite');
      for(const item of items) tx.objectStore('pending_transactions').add(item);
      tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);
    });
  },items);
}
function item(url, id='stable-id') {
  return {url,method:'POST',data:{request_id:id},requestId:id,accountId:'synthetic-owner',timestamp:Date.now()};
}
function assert(value, message) { if(!value) throw Error(message); }
async function main() {
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base=`http://127.0.0.1:${server.address().port}`;
  const options={headless:true};
  if(process.argv[2]) options.executablePath=process.argv[2];
  const browser=await chromium.launch(options);
  results.environment={browser:browser.version(),backend:'isolated loopback JSON contract fixture',real_indexeddb:true};
  async function test(name, fn, status='passed') {
    commits.clear();calls=[];loseOnce=true;
    const context=await browser.newContext();const page=await context.newPage();
    try {
      await page.goto(base);
      await page.waitForFunction(()=>typeof syncOfflineData==='function');
      // Ensure the automatic empty-queue load attempt has completed.
      await pending(page);
      const evidence=await fn(page);
      results.cases.push({name,status,evidence});console.log(name,status,JSON.stringify(evidence));
    } catch(e) {
      results.cases.push({name,status:'harness_error',error:e.stack});console.log(name,'harness_error',e.message);
    } finally {await context.close();}
  }
  try {
    await test('B01_aborted_storage_rejects_save',async page=>{
      const outcome=await page.evaluate(async()=>{
        const original=IDBObjectStore.prototype.add;
        IDBObjectStore.prototype.add=function(...args){const request=original.apply(this,args);this.transaction.abort();return request;};
        let resolved=false,error=null;
        try {await saveToOfflineQueue('/commit',new FormData());resolved=true;}catch(e){error=e.name;}
        IDBObjectStore.prototype.add=original;
        return {resolved,error};
      });
      const queue=await pending(page);
      assert(!outcome.resolved && outcome.error && queue.length===0,'Aborted writes must reject acknowledgement');
      return {...outcome,persisted_items:queue.length};
    });
    await test('B02_rejected_supplier_blocks_dependents_only',async page=>{
      const bad=item('/bad','bad');bad.data.firm='+0001';const same=item('/commit','dependent');same.data.firm='1';const valid=item('/commit','valid');valid.data.firm='2';await fill(page,[bad,same,valid]);
      await page.evaluate(()=>syncOfflineData());
      await page.waitForFunction(()=>toasts.some(x=>x[0].includes('queue_attention')));
      const queue=await pending(page);
      assert(calls.length===2 && queue.length===2 && commits.size===1 && queue[0].state==='rejected','Independent supplier must commit and rejected/dependent items remain');
      return {sent_requests:calls,remaining_items:queue.length,valid_commits:commits.size};
    });
    await test('B03_legacy_claim_is_durable_across_two_tabs',async page=>{
      await fill(page,[{url:'/commit',method:'POST',data:{firm:'synthetic'},timestamp:0}]);
      const other=await page.context().newPage();await other.goto(base);
      await Promise.all([page.evaluate(()=>syncOfflineData()),other.evaluate(()=>syncOfflineData())]);
      assert(commits.size===1 && calls.length===1 && calls[0].id && (await pending(page)).length===0,'Legacy identity must be assigned once before transmission');
      return {sent_requests:calls,committed_actions:commits.size};
    });
    await test('B04_normal_queue_concurrent_sync_is_server_idempotent',async page=>{
      await fill(page,[item('/commit')]);
      await page.evaluate(()=>{syncOfflineData();syncOfflineData();});
      await page.waitForFunction(()=>toasts.some(x=>x[0]==='Offline data synced successfully!'));
      const queue=await pending(page);
      assert(commits.size===1 && queue.length===0,'Normal stable-ID replay must commit once');
      return {http_requests:calls.length,committed_actions:commits.size,remaining_items:0};
    },'passed');
    await test('B05_authentication_failure_preserves_queue',async page=>{
      await fill(page,[item('/auth'),item('/commit','later')]);
      await page.evaluate(()=>syncOfflineData());
      await page.waitForFunction(()=>toasts.some(x=>x[0].includes('Sign in again')));
      const queue=await pending(page);
      assert(queue.length===2 && commits.size===0,'Authentication failure lost queue');
      return {remaining_items:queue.length,committed_actions:0,sent_requests:calls};
    },'passed');
    await test('B06_lost_commit_response_replays_without_duplicate',async page=>{
      await fill(page,[item('/commit')]);
      await page.evaluate(()=>{
        window.requestFailures=0;const actual=window.fetch.bind(window);
        // Deterministically lose the response after the fixture has committed.
        // Chromium can transparently retry a socket reset, masking that fault.
        window.fetch=async(...args)=>{
          const response=await actual(...args);
          if(requestFailures===0){requestFailures++;throw new TypeError('Injected lost response after commit');}
          return response;
        };
        syncOfflineData();
      });
      await page.waitForFunction(()=>requestFailures>=1);
      assert((await pending(page)).length===1 && commits.size===1,'Committed request should remain queued');
      await page.evaluate(()=>syncOfflineData());
      await page.waitForFunction(()=>toasts.some(x=>x[0]==='Offline data synced successfully!'));
      assert((await pending(page)).length===0 && commits.size===1,'Retry duplicated a stable-ID request');
      return {http_requests:calls.length,committed_actions:commits.size,remaining_items:0};
    },'passed');
    await test('B07_rejected_item_correction_uses_new_id_and_keeps_evidence',async page=>{
      const bad=item('/bad','rejected-id');bad.data.firm='1';bad.data.new_bill_amount='-1';await fill(page,[bad]);
      await page.evaluate(()=>syncOfflineData());
      const queue=await pending(page);assert(queue[0].state==='rejected','Expected conclusive server rejection');
      const errors=[];page.on('pageerror',error=>errors.push(error.message));
      await page.locator('#offline-sync-indicator').evaluate(el=>el.open=true);
      await page.locator('#offline-sync-indicator details').evaluate(el=>el.open=true);
      const editor=page.locator('#offline-sync-indicator [data-queue-field=new_bill_amount]');await editor.fill('broken');
      await page.getByRole('button',{name:'Save correction as a new request'}).click();
      assert((await pending(page))[0].requestId==='rejected-id' && errors.length===0,'Malformed correction changed identity or raised an uncaught error');
      // Inspect durable correction evidence at transmission, before acknowledgement removes it.
      await page.evaluate(()=>{
        const actual=window.fetch.bind(window);
        window.fetch=async(...args)=>{window.correctionAtSend=(await queueItems(await initDB()))[0];return actual(...args);};
      });
      await editor.fill('100');
      await page.getByRole('button',{name:'Save correction as a new request'}).click();
      await page.waitForFunction(()=>document.querySelector('#offline-sync-indicator').hidden);
      assert(commits.size===1 && calls.at(-1).id!=='rejected-id','A conclusively rejected correction needs a new request ID');
      const evidence=await page.evaluate(()=>correctionAtSend.corrections);
      assert(evidence.length===1 && evidence[0].data.request_id==='rejected-id' && evidence[0].error,'Correction evidence was lost before transmission');
      return {original_request_id:'rejected-id',corrected_request_id:calls.at(-1).id,committed_actions:commits.size,uncaught_errors:errors};
    });
    await test('B08_export_preserves_rejected_and_dependent_actions',async page=>{
      const bad=item('/bad','rejected');bad.data.firm='1';const next=item('/commit','dependent');next.data.firm='1';
      await fill(page,[bad,next]);await page.evaluate(()=>syncOfflineData());
      await page.locator('#offline-sync-indicator').evaluate(el=>el.open=true);
      const downloadEvent=page.waitForEvent('download');await page.getByRole('button',{name:'Export queued transactions'}).click();
      const download=await downloadEvent;const exported=JSON.parse(fs.readFileSync(await download.path(),'utf8'));
      assert(exported.length===2 && exported[0].error && exported[0].state==='rejected' && exported[1].requestId==='dependent','Export dropped queue state or dependencies');
      assert((await pending(page)).length===2,'Export must not remove queue entries');
      return {exported_items:exported.length,rejected_error_preserved:true,queue_retained:true};
    });
    await test('B09_aborted_legacy_claim_never_sends_unpersisted_id',async page=>{
      await fill(page,[{url:'/commit',method:'POST',data:{firm:'1'},timestamp:0}]);
      await page.evaluate(async()=>{
        const actual=IDBObjectStore.prototype.put;
        IDBObjectStore.prototype.put=function(...args){const request=actual.apply(this,args);this.transaction.abort();return request;};
        await syncOfflineData();IDBObjectStore.prototype.put=actual;
      });
      const queue=await pending(page);assert(calls.length===0 && queue.length===1 && !queue[0].data.request_id,'Aborted identity assignment must not transmit');
      await page.evaluate(()=>syncOfflineData());assert(commits.size===1 && (await pending(page)).length===0,'Retry after storage fault did not commit exactly once');
      return {requests_before_retry:0,committed_actions:1};
    });
    await test('B10_mixed_clients_cannot_mutate_queue_across_upgrade',async page=>{
      // Start on a clean origin without loading the current script, then emulate
      // an older tab holding a v2 connection and a persisted legacy item.
      await page.goto(base+'/?legacy=1');
      await page.evaluate(async()=>{
        await new Promise((resolve,reject)=>{
          const request=indexedDB.deleteDatabase('pharmacy_ledger_db');request.onsuccess=resolve;request.onerror=()=>reject(request.error);
        });
        window.legacyDb=await new Promise((resolve,reject)=>{
          const request=indexedDB.open('pharmacy_ledger_db',2);
          request.onupgradeneeded=()=>request.result.createObjectStore('pending_transactions',{keyPath:'id',autoIncrement:true});
          request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
        });
        await new Promise((resolve,reject)=>{
          const tx=legacyDb.transaction('pending_transactions','readwrite');
          tx.objectStore('pending_transactions').add({url:'/commit',method:'POST',data:{firm:'1'},timestamp:0});tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);
        });
      });
      const newer=await page.context().newPage();await newer.goto(base);
      await newer.waitForFunction(()=>toasts.some(x=>x[0].includes('Close older tabs')));
      assert(commits.size===0,'Current client posted before the upgrade boundary completed');
      const rejected=await page.evaluate(async()=>{
        const response=await fetch('/commit',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'firm=1&request_id=obsolete-id'});return response.status;
      });
      assert(rejected===428 && commits.size===0,'Obsolete client posted a new action during upgrade');
      await page.evaluate(()=>legacyDb.close());
      await newer.evaluate(()=>syncOfflineData());
      assert(commits.size===1 && (await pending(newer)).length===0,'Upgrade failed to retain and synchronize the legacy action exactly once');
      const oldVersion=await page.evaluate(()=>new Promise(resolve=>{
        const request=indexedDB.open('pharmacy_ledger_db',2);request.onerror=()=>resolve(request.error.name);request.onsuccess=()=>{request.result.close();resolve('opened');};
      }));
      assert(oldVersion==='VersionError','Old client could reopen and overwrite upgraded identity');
      return {obsolete_post_status:rejected,obsolete_storage_reopen:oldVersion,committed_actions:commits.size,legacy_queue_preserved:true};
    });
    await test('B11_conflict_requires_review_and_never_reassigns_identity',async page=>{
      commits.add('conflicted');
      const conflict=item('/conflict','conflicted');conflict.data.firm='1';conflict.data.new_bill_amount='200';
      const dependent=item('/commit','dependent');dependent.data.firm='1';const other=item('/commit','other');other.data.firm='2';
      await fill(page,[conflict,dependent,other]);await page.evaluate(()=>syncOfflineData());
      const queue=await pending(page);assert(queue.length===2 && queue[0].state==='conflict' && queue[0].requestId==='conflicted','Conflict did not retain original identity');
      await page.locator('#offline-sync-indicator').evaluate(el=>el.open=true);
      await page.locator('#offline-sync-indicator details').first().evaluate(el=>el.open=true);
      assert(await page.locator('[data-queue-field=new_bill_amount]').isDisabled(),'Committed-ID conflict must not offer ordinary rejection correction');
      assert(await page.getByRole('link',{name:'Review committed transactions'}).count()===1,'Conflict lacks a committed-history review path');
      await page.evaluate(()=>syncOfflineData());
      assert(calls.length===2 && commits.size===2,'Conflict auto-replayed or created a second action; independent supplier should commit');
      return {original_id_preserved:true,conflict_readonly:true,requests:calls,committed_actions_including_original:commits.size};
    });
  } finally {
    await browser.close();await new Promise(resolve=>server.close(resolve));
  }
  const output=process.argv[3]||path.join(root,'docs/accounting-repair-browser-results.json');
  fs.writeFileSync(output,JSON.stringify(results,null,2));
  process.exitCode=results.cases.some(x=>x.status==='harness_error')?1:0;
}
main().catch(e=>{console.error(e);server.close();process.exitCode=1;});
