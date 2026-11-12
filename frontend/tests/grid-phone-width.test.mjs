// On a phone every admin grid is one column, and that column may never grow wider than the screen.
// A grid with columns only from a breakpoint up (`md:grid-cols-2`) has no column rule below it, so
// its single implicit column takes the width of its longest unbroken line: a list of text keys or
// a t.me link pushed the message-variables and quick-links pages far past the phone's edge.
// `grid-cols-1` (minmax(0, 1fr)) keeps the column at the screen's width.
import assert from "node:assert/strict";
import test from "node:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../src/admin/", import.meta.url));

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : /\.tsx$/.test(name) ? [path] : [];
  });
}

/** Class lists that make a grid with columns only at some breakpoint and none below it. */
export function unboundedGrids(source) {
  const found = [];
  for (const match of source.matchAll(/className=(?:"([^"]*)"|\{`([^`]*)`\})/g)) {
    const classes = (match[1] ?? match[2]).split(/\s+/);
    if (!classes.includes("grid")) continue;
    const responsive = classes.some((c) => /^(sm|md|lg|xl|2xl):grid-cols-/.test(c));
    const base = classes.some((c) => /^grid-cols-/.test(c));
    if (responsive && !base) found.push(match[1] ?? match[2]);
  }
  return found;
}

test("the check itself tells a bounded grid from an unbounded one", () => {
  assert.deepEqual(unboundedGrids('<div className="grid gap-2 md:grid-cols-2">'), ["grid gap-2 md:grid-cols-2"]);
  assert.deepEqual(unboundedGrids('<div className="grid grid-cols-1 gap-2 md:grid-cols-2">'), []);
  assert.deepEqual(unboundedGrids('<div className="grid grid-cols-3 gap-2">'), []);
  assert.deepEqual(unboundedGrids('<div className="grid-cols-2 md:grid-cols-3">'), [], "not a grid by itself");
});

test("no admin grid can grow wider than a phone screen", () => {
  const offenders = files(root).flatMap((path) =>
    unboundedGrids(readFileSync(path, "utf8")).map((classes) => `${path.slice(root.length)}: ${classes}`),
  );
  assert.deepEqual(offenders, [], "add grid-cols-1 so the phone column stays at the screen's width");
});
