/* Real application form + IndexedDB + Django HTTP, on a disposable local DB. */
const fs = require('node:fs');
const {chromium} = require('playwright');
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
function assert(value, message) { if (!value) throw Error(message); }
async function pending(page) {
  return page.evaluate(async () => queueItems(await initDB()));
}
async function main() {
  const browser = await chromium.launch({headless:true, executablePath:fixture.chrome});
  const context = await browser.newContext();
  await context.addCookies([{name:'sessionid',value:fixture.session,url:fixture.base}]);
  await context.route('**/*', route => new URL(route.request().url()).origin === fixture.base ? route.continue() : route.abort());
  const page = await context.newPage(); const errors=[];page.on('pageerror', e=>errors.push(e.message));
  const results=[];
  async function open() { await page.goto(fixture.base+'/ledger/add-transaction/'); await page.waitForFunction(()=>typeof saveToOfflineQueue==='function'); }
  async function fill(number) {
    await page.selectOption('#id_source_type','distributor');
    await page.selectOption('#id_firm',String(fixture.firm));
    await page.waitForFunction(id=>Array.from(document.querySelector('#id_representative').options).some(o=>o.value===String(id)),fixture.rep);
    await page.selectOption('#id_representative',String(fixture.rep));
    await page.selectOption('#bill-choice-select','add_new');
    await page.fill('#id_new_bill_number',number);await page.fill('#id_new_bill_amount','100');
    await page.selectOption('#id_payment_choice','other');await page.fill('#id_custom_payment_amount','10');
    await page.waitForFunction(()=>!document.querySelector('#save-transaction-button').disabled);
  }
  try {
    await open(); await fill('BROWSER-STORAGE');
    const originalId=await page.inputValue('#id_request_id');
    await context.setOffline(true);
    await page.evaluate(()=>{
      window.actualAdd=IDBObjectStore.prototype.add;
      IDBObjectStore.prototype.add=function(...args){const request=actualAdd.apply(this,args);this.transaction.abort();return request;};
    });
    await page.click('#save-transaction-button');
    await page.waitForFunction(()=>document.querySelector('#toast-container').textContent.includes('form is still available'));
    assert((await pending(page)).length===0,'Aborted storage persisted');
    assert(await page.inputValue('#id_new_bill_number')==='BROWSER-STORAGE','Failed storage cleared the form');
    assert(await page.inputValue('#id_request_id')===originalId,'Failed storage changed request identity');
    await page.evaluate(()=>IDBObjectStore.prototype.add=actualAdd);
    await page.click('#save-transaction-button');
    await page.waitForFunction(id=>document.querySelector('#id_request_id').value!==id, originalId);
    const saved=await pending(page);assert(saved.length===1 && saved[0].data.request_id===originalId,'Offline save lost stable identity');
    assert(await page.inputValue('#id_new_bill_number')==='','Committed storage did not clear form');
    await context.setOffline(false);await page.evaluate(()=>syncOfflineData());
    assert((await pending(page)).length===0,'Actual Django did not acknowledge queued save');
    results.push({name:'aborted_storage_keeps_form_then_committed_queue_syncs',status:'passed'});

    await fill('BROWSER-LOST');let lose=true;
    await page.route('**/ledger/add-transaction/',async route=>{
      if(route.request().method()==='POST' && lose){lose=false;await route.fetch();await route.abort('failed');}
      else await route.continue();
    });
    await page.click('#save-transaction-button');
    await page.waitForFunction(()=>document.querySelector('#toast-container').textContent.includes('Connection lost'));
    const lost=await pending(page);assert(lost.length===1,'Lost committed response was not queued');
    await page.evaluate(()=>syncOfflineData());
    assert((await pending(page)).length===0,'Lost response replay did not acknowledge original commit');
    await page.unroute('**/ledger/add-transaction/');
    results.push({name:'lost_django_commit_response_replays_original_request',status:'passed'});

    await fill('BROWSER-EXPIRED');const expiredId=await page.inputValue('#id_request_id');
    await context.clearCookies();await page.click('#save-transaction-button');
    await page.waitForURL('**/accounts/login/**');
    const queued=await pending(page);assert(queued.length===1 && queued[0].data.request_id===expiredId,'Session expiry lost the queued action');
    await context.addCookies([{name:'sessionid',value:fixture.session,url:fixture.base}]);await open();
    await page.evaluate(()=>syncOfflineData());assert((await pending(page)).length===0,'Sign-in did not resume queued transaction');
    results.push({name:'expired_session_preserves_queue_and_resumes_after_signin',status:'passed'});
    assert(errors.length===0,'Uncaught browser errors: '+JSON.stringify(errors));
    fs.writeFileSync(fixture.output,JSON.stringify({backend:'actual disposable Django application',browser:browser.version(),cases:results,uncaught_errors:errors},null,2));
    console.log('Actual application browser checks passed:',results.length);
  } finally { await browser.close(); }
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
