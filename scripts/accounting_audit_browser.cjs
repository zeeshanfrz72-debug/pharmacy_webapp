/* Real Chromium/IndexedDB tests of the unchanged offline_sync.js.
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
      window.csrftoken='synthetic-token';</script><script src="/offline_sync.js"></script></body>`);
    return;
  }
  let body = '';
  for await (const part of req) body += part;
  const id = new URLSearchParams(body).get('request_id');
  calls.push({url:req.url, id});
  res.setHeader('Content-Type', 'application/json');
  if (req.url === '/bad') {
    res.statusCode=400; res.end(JSON.stringify({success:false,error:'Choose a bill belonging to the selected firm.'})); return;
  }
  if (req.url === '/auth') {
    res.statusCode=401; res.end(JSON.stringify({success:false,error:'authentication_required'})); return;
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
  async function test(name, fn, status='confirmed') {
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
    await test('B01_queue_save_resolves_even_when_storage_aborts',async page=>{
      const outcome=await page.evaluate(async()=>{
        const original=IDBObjectStore.prototype.add;
        IDBObjectStore.prototype.add=function(...args){const request=original.apply(this,args);this.transaction.abort();return request;};
        let resolved=false,error=null;
        try {await saveToOfflineQueue('/commit',new FormData());resolved=true;}catch(e){error=e.name;}
        IDBObjectStore.prototype.add=original;
        return {resolved,error};
      });
      const queue=await pending(page);
      assert(outcome.resolved && queue.length===0,'Expected unacknowledged aborted write');
      return {...outcome,persisted_items:queue.length};
    });
    await test('B02_invalid_head_blocks_later_valid_transactions',async page=>{
      await fill(page,[item('/bad','bad'),item('/commit','valid')]);
      await page.evaluate(()=>syncOfflineData());
      await page.waitForFunction(()=>toasts.some(x=>x[0].includes('queue_attention')));
      const queue=await pending(page);
      assert(calls.length===1 && queue.length===2 && commits.size===0,'Expected queue blocking');
      return {sent_requests:calls,remaining_items:queue.length,valid_commits:commits.size};
    });
    await test('B03_legacy_queue_concurrent_claims_generate_different_ids',async page=>{
      await fill(page,[{url:'/commit',method:'POST',data:{firm:'synthetic'},timestamp:0}]);
      await page.evaluate(()=>{
        window.fetchIds=[];const actual=window.fetch.bind(window);
        window.fetch=async(...args)=>{
          fetchIds.push(new URLSearchParams(args[1].body).get('request_id'));
          if(fetchIds.length===1)await new Promise(resolve=>window.releaseFirst=resolve);
          else window.releaseFirst();
          return actual(...args);
        };
        syncOfflineData();syncOfflineData();
      });
      await page.waitForFunction(()=>fetchIds.length===2);
      await page.waitForFunction(()=>toasts.some(x=>x[0]==='Offline data synced successfully!'));
      const ids=await page.evaluate(()=>fetchIds);
      assert(new Set(ids).size===2 && commits.size===2,'Expected two commits from one legacy queue item');
      return {queued_actions:1,sent_request_ids:ids,committed_actions:commits.size,remaining_items:(await pending(page)).length};
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
  } finally {
    await browser.close();await new Promise(resolve=>server.close(resolve));
  }
  const output=process.argv[3]||path.join(root,'docs/accounting-audit-browser-results.json');
  fs.writeFileSync(output,JSON.stringify(results,null,2));
  process.exitCode=results.cases.some(x=>x.status==='harness_error')?1:0;
}
main().catch(e=>{console.error(e);server.close();process.exitCode=1;});
