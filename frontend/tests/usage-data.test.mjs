import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/app/usage/usage-data.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
const { validateHistory, segments, valueScale } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const point = (hour, bytes, boundary = false) => ({ at: `2026-10-14T${String(hour).padStart(2,'0')}:00:00Z`, used_bytes: bytes, break_before: boundary });
const history = (extra = {}) => ({ service_id: 1, days: 7, resolution: 'hourly_last_observation',
 from:'2026-10-07T12:00:00Z',to:'2026-10-14T12:00:00Z',points:[point(8,0),point(9,1024)], ...extra });

test('real zero remains a real observation; no filled missing rows',()=>{const d=history();assert.equal(validateHistory(d,1,7),d);assert.equal(d.points.length,2);});
for(const extra of [{service_id:2},{days:30},{resolution:'daily_estimate'},{from:'invalid'},
 {to:'2026-10-01'}, {points:[point(9,-1)]},{points:[point(9,Infinity)]},{points:[point(9,NaN)]},
 {points:[point(9,2**53)]},{points:[point(9,null)]},{points:[point(9,10,'false')]},
 {points:[point(9,10),point(8,20)]},{points:[point(8,10),point(8,20)]},
 {points:[{...point(8,10),at:'2026-10-20T00:00:00Z'}]}])
 test(`invalid history ${JSON.stringify(extra)}`,()=>assert.throws(()=>validateHistory(history(extra),1,7)));
test('empty history is empty, not fabricated zeros',()=>assert.equal(validateHistory(history({points:[]}),1,7).points.length,0));
test('too many samples fail closed',()=>assert.throws(()=>validateHistory(history({points:Array(170).fill(point(8,0))}),1,7)));
test('counter drop starts a new line, never a negative usage segment',()=>assert.deepEqual(segments([point(8,100),point(9,10),point(10,20)]).map(g=>g.length),[1,2]));
test('known period boundary starts a new line even without counter drop',()=>assert.deepEqual(segments([point(8,10),point(9,20,true)]).map(g=>g.length),[1,1]));
test('gap over two hours starts a new line',()=>assert.deepEqual(segments([point(5,10),point(8,20),point(9,30)]).map(g=>g.length),[1,2]));
test('exact two-hour spacing may connect; only observed points survive',()=>{const input=[point(8,10),point(10,20)];assert.deepEqual(segments(input),[input]);});
for(const bytes of [0,1,1024,1024**2,1024**3,9007199254740991])
 test(`finite zero-based single-unit scale for ${bytes}`,()=>{const s=valueScale([point(8,bytes)]);assert.equal(s.ticks.length,5);assert.equal(s.ticks[0],0);assert(Number.isFinite(s.maximum)&&s.maximum>0);assert(s.maximum*s.divisor>=bytes);});
