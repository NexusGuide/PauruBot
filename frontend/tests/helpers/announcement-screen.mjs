// Small deterministic hook/JSX harness; not a DOM or a replacement for browser QA.
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
const require = createRequire(import.meta.url);
const compiler = join(
  dirname(require.resolve("typescript/package.json")),
  "lib/tsc.js",
);
const compiled = new Map();
export function screenHarness() {
  const slots = [],
    effects = [],
    cleanups = [],
    effectFns = [];
  let index = 0;
  const same = (a, b) =>
    a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const hooks = {
    useState(initial) {
      const i = index++;
      if (!(i in slots))
        slots[i] = typeof initial === "function" ? initial() : initial;
      return [
        slots[i],
        (value) => {
          slots[i] = typeof value === "function" ? value(slots[i]) : value;
        },
      ];
    },
    useRef(initial) {
      const i = index++;
      return (slots[i] ??= { current: initial });
    },
    useEffect(effect, deps) {
      const i = index++;
      if (!same(slots[i], deps)) {
        slots[i] = deps;
        effectFns[i] = effect;
        effects.push(() => {
          cleanups[i]?.();
          cleanups[i] = effect();
        });
      }
    },
    useCallback(fn, deps) {
      const i = index++;
      if (!same(slots[i]?.deps, deps)) slots[i] = { fn, deps };
      return slots[i].fn;
    },
  };
  function compile(url, mocks, suffix = "") {
    const source = readFileSync(url, "utf8") + suffix;
    let code = compiled.get(source);
    if (!code) {
      const temp = mkdtempSync(join(tmpdir(), "pauru-screen-"));
      try {
        writeFileSync(join(temp, "screen.tsx"), source);
        execFileSync(
          process.execPath,
          [
            compiler,
            "--ignoreConfig",
            "--noCheck",
            "--jsx",
            "react-jsx",
            "--module",
            "commonjs",
            "--target",
            "es2022",
            "--outDir",
            temp,
            join(temp, "screen.tsx"),
          ],
          { stdio: "pipe" },
        );
        code = readFileSync(join(temp, "screen.js"), "utf8");
        compiled.set(source, code);
      } finally {
        rmSync(temp, { recursive: true, force: true });
      }
    }
    const exports = {};
    const jsx = (type, props, key) => ({ type, props: props ?? {}, key });
    new Function("require", "exports", code)((id) => {
      if (id === "react") return hooks;
      if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (!(id in mocks)) throw new Error(`Unmocked import ${id}`);
      return mocks[id];
    }, exports);
    return exports;
  }
  return {
    compile,
    render(component, props) {
      index = 0;
      const tree = component(props);
      while (effects.length) effects.shift()();
      return tree;
    },
    replayEffects() {
      for (const cleanup of cleanups) cleanup?.();
      for (const [i, effect] of effectFns.entries())
        if (effect) cleanups[i] = effect();
    },
    unmount() {
      for (const cleanup of cleanups) cleanup?.();
    },
  };
}
export function nodes(tree, predicate) {
  if (tree === null || tree === undefined || typeof tree === "boolean")
    return [];
  if (Array.isArray(tree))
    return tree.flatMap((child) => nodes(child, predicate));
  if (typeof tree !== "object") return [];
  return [
    ...(predicate(tree) ? [tree] : []),
    ...nodes(tree.props?.children, predicate),
  ];
}
export function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
export const tick = () => new Promise((resolve) => setImmediate(resolve));
