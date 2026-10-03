const fs = require('node:fs');
const {chromium} = require('playwright');
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
function assert(value, message) {if (!value) throw Error(message);}
async function main() {
 const browser = await chromium.launch({headless:true, executablePath:fixture.chrome});
 const context = await browser.newContext({viewport:{width:1440,height:1100}});
 await context.addCookies([{name:'sessionid',value:fixture.session,url:fixture.base}]);
 await context.route('**/*', r => new URL(r.request().url()).origin === fixture.base ? r.continue() : r.abort());
 const page = await context.newPage(), errors=[], cases=[];
 page.on('pageerror', e=>errors.push(e.message));
 async function open() {await page.goto(fixture.base+'/ledger/add-transaction/'); await page.waitForFunction(()=>typeof saveToOfflineQueue==='function');}
 async function selectFirm(type, firm, rep) {
  await page.selectOption('#id_source_type',type); await page.selectOption('#id_firm',String(firm));
  await page.waitForFunction(()=>Array.from(document.querySelector('#bill-choice-select').options).some(o=>o.value==='add_new'));
  if(rep) {await page.waitForFunction(id=>Array.from(document.querySelector('#id_representative').options).some(o=>o.value===String(id)),rep);await page.selectOption('#id_representative',String(rep));}
 }
 async function save() {
  const response = page.waitForResponse(r=>r.url().endsWith('/ledger/add-transaction/')&&r.request().method()==='POST');
  await page.click('#save-transaction-button'); const result=await response;
  assert(result.ok(),await result.text());
  await page.waitForFunction(()=>document.querySelector('#id_source_type').value==='');
  return result.json();
 }
 try {
  await open();
  for(const amount of ['100','200']) {
   await selectFirm('distributor',fixture.firm,fixture.rep);
   const options=await page.locator('#bill-choice-select option').evaluateAll(nodes=>nodes.map(n=>({id:n.value,disabled:n.disabled,text:n.textContent})));
   assert(!options.find(o=>o.id===String(fixture.first)).disabled,'Older invoice is disabled');
   assert(!options.find(o=>o.id===String(fixture.second)).disabled,'Newer invoice is disabled');
   await page.selectOption('#bill-choice-select',String(fixture.first));
   await page.selectOption('#id_payment_choice','other');await page.fill('#id_custom_payment_amount',amount);await save();
  }
  cases.push('older/newer invoice selection and repeated installments');
  await selectFirm('local_market',fixture.market,fixture.local_rep);
  await page.selectOption('#bill-choice-select','add_new');
  assert(await page.inputValue('#id_payment_choice')==='','Default form invents payment');
  await page.fill('#id_new_bill_amount','80');await save();
  await page.goto(fixture.base+'/ledger/bills/');
  await page.selectOption('#id_bill-source_type','local_market');
  await page.waitForFunction(id=>Array.from(document.querySelector('#id_bill-firm').options).some(o=>o.value===String(id)),fixture.market);
  await page.selectOption('#id_bill-firm',String(fixture.market));
  await page.waitForFunction(()=>!document.querySelector('#id_bill-representative').disabled);
  await page.fill('#id_bill-bill_number',''); await page.fill('#id_bill-bill_amount','60');
  await page.locator('button[type=submit]').filter({hasText:'Save Bill'}).click(); await page.waitForURL('**/ledger/bills/');
  assert(await page.locator('body').textContent().then(t=>t.includes('Local Market Bill #')&&t.includes('مقامی مارکیٹ کا بل')),'Unnamed bills lack bilingual ID references');
  cases.push('unnamed local invoices with optional representative and no automatic cash payments');
  await page.goto(fixture.base+`/ledger/bill/${fixture.first}/carry-forward/`);
  assert((await page.locator('body').textContent()).includes('آپ یہ بل غیر فعال کر رہے ہیں'),'Urdu carry confirmation missing');
  await page.getByRole('link',{name:/Cancel \/ No/}).click(); await page.waitForURL('**/ledger/bills/');
  await page.goto(fixture.base+`/ledger/bill/${fixture.first}/carry-forward/`);
  await page.selectOption('#id_destination',String(fixture.second));await page.check('#id_confirm');
  await page.locator('#carry-form button[type=submit]').click();await page.waitForURL('**/ledger/bills/');
  await open();await selectFirm('distributor',fixture.firm,fixture.rep);
  assert(await page.locator(`#bill-choice-select option[value="${fixture.first}"]`).isDisabled(),'Transferred source remains payable');
  await page.goto(fixture.base+'/ledger/bills/');
  await page.locator(`#bill-${fixture.first} details.row-actions > summary`).click();
  await page.locator(`#bill-${fixture.first} a`).filter({hasText:'Undo Debt Carry Forward'}).click();
  await page.locator('#undo-form button').click();await page.waitForURL('**/ledger/bills/');
  cases.push('cancel is inert, carry preserves total, disabled source is protected, audited undo');
  await page.goto(fixture.base+`/ledger/bill/${fixture.first}/carry-forward/`);
  await page.selectOption('#id_destination','new');await page.fill('#id_bill_number','NEW-CHARGES-ONLY');await page.fill('#id_bill_amount','800');
  await page.selectOption('#id_representative',String(fixture.rep));await page.check('#id_confirm');
  await page.locator('#carry-form button[type=submit]').click();await page.waitForURL('**/ledger/bills/');
  await page.locator(`#bill-${fixture.first} details.row-actions > summary`).click();
  await page.locator(`#bill-${fixture.first} a`).filter({hasText:'Undo Debt Carry Forward'}).click();
  await page.locator('#undo-form button').click();await page.waitForURL('**/ledger/bills/');
  cases.push('new carry destination creates new charges separately and remains after undo');
  await open();
  await page.evaluate(async f=>{
   const base={source_type:'distributor',firm:String(f.firm),representative:String(f.rep),bill_choice:String(f.first),new_bill_number:'',new_bill_amount:'',payment_choice:'other',custom_payment_amount:'100'};
   const old={...base,request_id:crypto.randomUUID()};
   const independent={...base,source_type:'local_market',firm:String(f.market),representative:'',bill_choice:'add_new',new_bill_number:'INDEPENDENT-QUEUE',new_bill_amount:'1',payment_choice:'',custom_payment_amount:'',posting_rules_version:'2',request_id:crypto.randomUUID()};
   await saveToOfflineQueue('/ledger/add-transaction/',new URLSearchParams(old));
   await saveToOfflineQueue('/ledger/add-transaction/',new URLSearchParams(independent));
   await syncOfflineData();
  },fixture);
  let queued=await page.evaluate(async()=>queueItems(await initDB()));
  assert(queued.length===1&&queued[0].state==='rejected'&&queued[0].data.firm===String(fixture.firm),'Legacy queue lost or independent supplier blocked');
  await page.locator('#offline-sync-indicator').evaluate(el=>el.open=true);
  await page.locator('#offline-sync-indicator details').evaluate(el=>el.open=true);
  await page.getByRole('button',{name:'Save correction as a new request'}).click();
  await page.waitForFunction(async()=>!(await queueItems(await initDB())).length);
  cases.push('legacy queue retained for explicit review while independent supplier synchronizes');
  assert(errors.length===0,'Browser errors: '+JSON.stringify(errors));
  fs.writeFileSync(fixture.output,JSON.stringify({backend:'disposable Django application',browser:browser.version(),cases,uncaught_errors:errors},null,2));
 } finally {await browser.close();}
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
