import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {payment,canAct,validFile} from '../src/app/tonpay/model.ts';
const state=()=>({payment_id:1,status:'pending',request_amount:100000,payable_amount:103000,quote_revision:1,consented:false,uncertain:false,recovery:null,provider_status:'pending',provider_paid:false,expires_at:null,can_consent:true,can_receipt:false,can_change_card:true,receipt_ready_at:100});
test('custom quote distinguishes wallet credit from provider fee',()=>{const p=payment(state());assert.equal(p.payable_amount-p.request_amount,3000);assert(canAct(p,'consent',false));assert(!canAct(p,'receipt',false));});
for(const key of ['payment_id','request_amount','payable_amount','quote_revision'])test(`unsafe ${key} rejected`,()=>assert.throws(()=>payment({...state(),[key]:Number.MAX_SAFE_INTEGER+1})));
for(const patch of [{uncertain:true},{provider_paid:true},{status:'expired'},{payable_amount:null},{quote_revision:null},{can_receipt:true}])test(`contradictory capabilities rejected ${JSON.stringify(patch)}`,()=>assert.throws(()=>payment({...state(),...patch})));
test('unknown creation is displayable but never actionable',()=>{const p=payment({...state(),status:'created',uncertain:true,payable_amount:null,quote_revision:null,can_consent:false,can_change_card:false,recovery:'provider_lookup_unavailable'});for(const action of ['consent','receipt','change'])assert(!canAct(p,action,false));});
test('card rejected until explicit current consent',()=>{assert.throws(()=>payment({...state(),card:{number:'0000',holder:'Synthetic'}}));assert.equal(payment({...state(),consented:true,card:{number:'0000',holder:'Synthetic'}}).card.number,'0000');});
test('busy gate blocks every action',()=>{for(const a of ['consent','receipt','change'])assert(!canAct(payment(state()),a,true));});
for(const type of ['image/jpeg','image/png','image/webp'])test(`bounded file ${type}`,()=>{assert(validFile({size:10*1024*1024,type}));assert(!validFile({size:10*1024*1024+1,type}));assert(!validFile({size:0,type}));});
test('HTML SVG and disguised media are rejected by client hint',()=>{for(const type of ['image/svg+xml','text/html','application/octet-stream'])assert(!validFile({size:100,type}));});

// Page redesign: the card is fetched as soon as it may be shown, steps follow the payment.
const { groupCard, stage, needsCard, statusKey } = await import('../src/app/tonpay/model.ts');
test('card number is shown in groups of four', () => {
  assert.equal(groupCard('6037990000000000'), '6037 9900 0000 0000');
  assert.equal(groupCard('6037-9900 0000 0000'), '6037 9900 0000 0000');
});
test('steps follow the payment: confirm, transfer, receipt, then waiting', () => {
  const now = 1000;
  assert.equal(stage(payment(state()), now), 0);
  assert.equal(stage(payment({ ...state(), consented: true, receipt_ready_at: 2000 }), now), 1);
  assert.equal(stage(payment({ ...state(), consented: true, can_receipt: true }), now), 2);
  assert.equal(stage(payment({ ...state(), consented: true, receipt_ready_at: 10 }), now), 3);
  assert.equal(stage(payment({ ...state(), status: 'paid', can_consent: false, can_change_card: false }), now), 3);
});
test('after consent the card is fetched at once, never shown before', () => {
  assert(!needsCard(payment(state())));
  assert(needsCard(payment({ ...state(), consented: true })));
  assert(!needsCard(payment({ ...state(), consented: true, card: { number: '6037', holder: 'A' } })));
  assert(!needsCard(payment({ ...state(), consented: true, can_consent: false, can_change_card: false })));
});
test('statuses are words, not provider codes', () => {
  assert.equal(statusKey(payment(state())), 'pending');
  assert.equal(statusKey(payment({ ...state(), status: 'paid', can_consent: false, can_change_card: false })), 'paid');
  assert.equal(statusKey(payment({ ...state(), provider_paid: true, can_consent: false, can_change_card: false })), 'verifying');
});
test('the page fetches the card after consent and on reopening, and translates the status', () => {
  const source = readFileSync(new URL('../src/app/tonpay/CustomTonpayFlow.tsx', import.meta.url), 'utf8');
  assert(source.includes('needsCard('), 'fetches the card when it may be shown');
  assert(!source.includes('{value.status}'), 'no raw status code on the page');
});
