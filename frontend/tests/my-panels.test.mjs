import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const { dataLeft } = await import(new URL('../src/app/data-left.ts', import.meta.url));
const read = (path) => readFileSync(new URL(`../src/app/${path}`, import.meta.url), 'utf8');

test('remaining volume never goes below zero, and no limit means unlimited', () => {
  assert.equal(dataLeft(1000, 400), 600);
  assert.equal(dataLeft(1000, 1331), 0); // used past the quota: nothing left, not -331
  assert.equal(dataLeft(0, 50), null);
  assert.equal(dataLeft(null, 50), null);
});

test('the services list and home card show the clamped remaining volume', () => {
  for (const screen of ['screens/Services.tsx', 'screens/Home.tsx']) {
    const source = read(screen);
    assert(!/data_limit\s*-\s*\w+\.used_bytes/.test(source), `${screen} subtracts used bytes by hand`);
    assert(source.includes('dataLeft('), `${screen} uses dataLeft`);
  }
});

test('reseller panels are listed on the services screen, not only under Buy', () => {
  const services = read('screens/Services.tsx');
  assert(services.includes('<MyPanels'), 'Services renders MyPanels');
  // While a search or filter narrows the services, the panels step aside.
  assert(/!\(query \|\| status \|\| group !== null\) && <MyPanels/.test(services));
  const agency = read('screens/Agency.tsx');
  assert(agency.includes('export function MyPanels'), 'one shared list');
  assert(agency.includes('<AccountRows'), 'the agency hub uses the same rows');
});
