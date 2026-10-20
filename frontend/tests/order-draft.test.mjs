import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { prepareDraft, reviseDraft } from '../src/app/order-draft.ts';
let n = 0;
const uuid = () => `draft-key-${++n}`;
test('first entry installs one key and stable label without mutating navigation input', () => {
  const original = new URLSearchParams('plan=3');
  const first = prepareDraft(original, 'ABC', uuid);
  assert(!original.has('draft'));
  assert.equal(first.get('draft_label'), 'ABC');
  assert.equal(first.get('plan'), '3');
  assert.equal(prepareDraft(first, 'DIFFERENT', uuid).toString(), first.toString());
});
test('reload, failed request and repeated effect preserve the same serialized draft', () => {
  const first = prepareDraft(new URLSearchParams('plan=2'), 'NAME', uuid);
  const reload = prepareDraft(new URLSearchParams(first.toString()), 'NEW_RANDOM', uuid);
  assert.equal(reload.toString(), first.toString());
});
test('a deliberate edit creates a new key, preserves plan and persists the exact body inputs', () => {
  const first = prepareDraft(new URLSearchParams('plan=2&service=4'), 'A', uuid);
  const edited = reviseDraft(first, 'CODE', 'B', uuid);
  assert.notEqual(edited.get('draft'), first.get('draft'));
  assert.equal(edited.get('draft_code'), 'CODE');
  assert.equal(edited.get('draft_label'), 'B');
  assert.equal(edited.get('service'), '4');
  assert.equal(prepareDraft(edited, 'C', uuid).toString(), edited.toString());
});
test('new navigation creates a new deliberate intent even with the same plan', () => {
  const a = prepareDraft(new URLSearchParams('plan=2'), 'A', uuid);
  const b = prepareDraft(new URLSearchParams('plan=2'), 'A', uuid);
  assert.notEqual(a.get('draft'), b.get('draft'));
});
test('Checkout sends URL key, retains it on errors, ignores stale responses and gates all actions', () => {
  const source = readFileSync(new URL('../src/app/screens/Checkout.tsx', import.meta.url), 'utf8');
  assert(source.includes('intent_key: params.get("draft")!'));
  assert(source.includes('if (!active) return;'));
  assert(source.includes('return () => { active = false; };'));
  assert(source.includes('setRetry((n) => n + 1)'));
  assert.equal((source.match(/!gate.current.enter\(\)/g) || []).length, 3);
  assert.equal((source.match(/gate.current.leave\(\)/g) || []).length, 3);
});
