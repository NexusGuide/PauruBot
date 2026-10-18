import assert from 'node:assert/strict';
import test from 'node:test';
import { resumeSavedPayment } from '../src/app/payments/resume.ts';
function setup(payment, error) {
  const calls = [];
  return { calls, ports: {
    read: async id => { calls.push(['read', id]); if (error) throw error; return {payment}; },
    remember: data => calls.push(['remember', data]),
    open: url => calls.push(['open', url]),
    navigate: path => calls.push(['navigate', path]),
  }};
}
test('gateway resume reopens original URL and ID with no create/topup port', async () => {
  const {calls, ports} = setup({payment_id:7,provider:'nowpayments',amount:123,redirect_url:'https://pay.example/original'});
  await resumeSavedPayment(7,ports);
  assert.deepEqual(calls,[['read',7],['open','https://pay.example/original'],['navigate','/payment/7']]);
});
test('card and crypto restore the original saved instructions', async () => {
  for (const extra of [{accounts:[{number:'6037990000000000'}]},{crypto:{network:'trx',amount:'1.23',address:'T'}}]) {
    const {calls,ports}=setup({payment_id:8,provider:'card',amount:321,...extra});
    await resumeSavedPayment(8,ports);
    assert.equal(calls[1][0],'remember');
    assert.equal(calls[1][1].paymentId,8);
    assert.equal(calls[1][1].amount,321);
    assert.deepEqual(calls[2],['navigate','/payment/8']);
  }
});
test('409/network failure has no new-invoice, navigation or external-open fallback', async () => {
  for (const error of [new Error('409'),new Error('network')]) {
    const {calls,ports}=setup(null,error);
    await assert.rejects(resumeSavedPayment(7,ports));
    assert.deepEqual(calls,[['read',7]]);
  }
});
test('mismatched ID and newly generated Stars links fail before any side effect', async () => {
  for (const payment of [{payment_id:9},{payment_id:7,invoice_link:'https://t.me/pay'}]) {
    const {calls,ports}=setup(payment);
    await assert.rejects(resumeSavedPayment(7,ports));
    assert.deepEqual(calls,[['read',7]]);
  }
});
test('unsafe redirect fails before remembering, opening or navigating', async () => {
  for (const redirect_url of ['http://pay.example/','javascript:alert(1)','https://u:p@pay.example/']) {
    const {calls,ports}=setup({payment_id:7,redirect_url});
    await assert.rejects(resumeSavedPayment(7,ports));
    assert.deepEqual(calls,[['read',7]]);
  }
});
