import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

// Compile the real pure TypeScript helper with the project's existing compiler; no test dependency.
const source = readFileSync(new URL('../src/admin/typed-confirm.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
const { confirmationSpec, validateChallenge, normalizeConfirmation, needsTyping, confirmationFields } =
  await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const challenge = (extra = {}) => ({ action: 'service.delete', token: 'test-token', phrase: 'Customer_12', expires_in: 300, ...extra });

for (const [input, expected] of [
  ['  @CUSTOMER_12  ', 'customer_12'], ['@@ Customer_12', 'customer_12'],
  ['۱٬۰۰۰٬۰۰۰', '1000000'], ['١,٠٠٠,٠٠٠', '1000000'], [' A  \n B ', 'a b'],
]) test(`normalization: ${JSON.stringify(input)}`, () => assert.equal(normalizeConfirmation(input), expected));

for (const action of ['service.delete', 'order.refund']) test(`endpoint for ${action}`, () => {
  const spec = confirmationSpec({ action, id: 9 });
  assert.equal(spec.action, action);
  assert.equal(spec.path, action === 'service.delete' ? '/api/admin/services/9/confirm-delete' : '/api/admin/orders/9/confirm-refund');
});
for (const id of [0, -1, 1.5, NaN, Infinity]) test(`reject invalid id ${id}`, () => assert.throws(() => confirmationSpec({ action: 'service.delete', id })));

for (const action of ['delete', 'disable']) test(`bulk ${action} uses a selection snapshot`, () => {
  const body = { action, ids: [1, 2], filter: null, notify: true };
  const spec = confirmationSpec({ action: `service.bulk_${action}`, body });
  body.ids.push(3); body.notify = false;
  assert.deepEqual(spec.body, { action, ids: [1, 2], filter: null, notify: true });
  assert.equal(spec.path, '/api/admin/services/bulk/confirm');
});
test('bulk action cannot switch between challenge and submit', () => assert.throws(() => confirmationSpec({ action: 'service.bulk_delete', body: { action: 'disable', ids: [1] } })));

for (const extra of [
  {action:'order.refund'}, {token:''}, {token:'x'.repeat(65)}, {phrase:''}, {phrase:'x'.repeat(129)},
  {expires_in:0}, {expires_in:301}, {expires_in:NaN}, {expires_in:1.5}, {required:'false'}, {count:-1},
]) test(`reject malformed challenge ${JSON.stringify(extra)}`, () => assert.throws(() => validateChallenge(challenge(extra), 'service.delete')));

test('server action and phrase are retained', () => assert.deepEqual(validateChallenge(challenge(), 'service.delete'), challenge()));
test('bulk count must agree with phrase', () => {
  const c=challenge({action:'service.bulk_delete',phrase:'2',count:2});
  assert.deepEqual(validateChallenge(c,c.action),c);
  assert.throws(() => validateChallenge({...c,count:3},c.action));
});
test('refund must state whether typing is required', () => {
  assert.throws(() => validateChallenge(challenge({action:'order.refund'}),'order.refund'));
  assert.equal(needsTyping(challenge({action:'order.refund',required:false})),false);
  assert.equal(needsTyping(challenge({action:'order.refund',required:true})),true);
  assert.equal(needsTyping(challenge({required:false})),true);
});
test('blank and wrong input cannot produce mutation fields', () => {
  for(const typed of ['', ' ', 'wrong', 'x'.repeat(129)]) assert.equal(confirmationFields(challenge(),typed,1000,100),null);
});
test('original typed input and server token are submitted', () => {
  assert.deepEqual(confirmationFields(challenge(),' @CUSTOMER_12 ',1000,100),{confirm_token:'test-token',confirm_text:' @CUSTOMER_12 '});
});
test('Persian amount accepted without replacing the server amount', () => {
  assert.deepEqual(confirmationFields(challenge({action:'order.refund',phrase:'1000000',required:true}),'۱٬۰۰۰٬۰۰۰',1000,100),{confirm_token:'test-token',confirm_text:'۱٬۰۰۰٬۰۰۰'});
});
test('small refunds use an explicit simple confirmation', () => {
  assert.deepEqual(confirmationFields(challenge({action:'order.refund',required:false}),'',1000,100),{confirm_token:'test-token',confirm_text:''});
});
for(const now of [1000,1001]) test(`expiry boundary ${now}`,()=>assert.equal(confirmationFields(challenge(),'Customer_12',1000,now),null));
test('invalid deadline fails closed',()=>assert.equal(confirmationFields(challenge(),'Customer_12',Infinity,100),null));
