import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  screenHarness,
  nodes,
  deferred,
  tick,
} from "./helpers/announcement-screen.mjs";
const root = (path) => new URL(`../src/${path}`, import.meta.url);
const copy = screenHarness().compile(root("shared/announcements.ts"), {});
const row = (id = 1) => ({
  id,
  title: `Notice ${id}`,
  text: '<script>alert("not HTML")</script>',
  kind: "operational",
  starts_at: "2026-01-01T00:00:00Z",
  expires_at: null,
  revoked_at: null,
  seen_at: null,
  read_at: null,
});
const page = (items = [row()], next_before = null) => ({ items, next_before });
const button = (tree, label) =>
  nodes(tree, (n) => n.type === "Button" && n.props.children === label)[0];
async function ready(h) {
  h.render();
  await tick();
  return h.render();
}
function inbox({
  list = async () => page(),
  mark = async (id, action) => ({
    item: {
      ...row(id),
      seen_at: "seen",
      read_at: action === "read" ? "read" : null,
    },
  }),
  locale = "en",
} = {}) {
  const h = screenHarness(),
    calls = { list: [], mark: [] };
  const { AnnouncementInbox } = h.compile(
    root("app/screens/AnnouncementInbox.tsx"),
    {
      "@shared/announcements": copy,
      "@shared/format": { dateTime: (x) => x },
      "../ui": { Button: "Button" },
      "../state": { useApp: () => ({ locale }) },
      "../api": {
        api: {
          announcements: (...a) => {
            calls.list.push(a);
            return list(...a);
          },
          announcementState: (...a) => {
            calls.mark.push(a);
            return mark(...a);
          },
        },
      },
    },
  );
  return { ...h, calls, render: () => h.render(AnnouncementInbox, {}) };
}
test("GET renders inbox without marking anything seen or read", async () => {
  const h = inbox();
  const tree = await ready(h);
  assert(button(tree, "Open"));
  assert.deepEqual(h.calls.mark, []);
  assert.equal(JSON.stringify(tree).includes(row().text), false);
  h.unmount();
});
test("only acknowledged opening reveals plain escaped text and seen state", async () => {
  const h = inbox();
  let tree = await ready(h);
  await button(tree, "Open").props.onClick();
  tree = h.render();
  assert.deepEqual(h.calls.mark, [[1, "seen"]]);
  assert(
    nodes(tree, (n) => n.type === "p" && n.props.children === row().text)
      .length,
  );
  assert.equal(nodes(tree, (n) => n.props.dangerouslySetInnerHTML).length, 0);
  assert(button(tree, "Mark read"));
  h.unmount();
});
test("read is server acknowledged and persists in next server-loaded view", async () => {
  const h = inbox();
  let tree = await ready(h);
  await button(tree, "Open").props.onClick();
  tree = h.render();
  await button(tree, "Mark read").props.onClick();
  tree = h.render();
  assert.equal(button(tree, "Mark read"), undefined);
  assert.deepEqual(h.calls.mark, [
    [1, "seen"],
    [1, "read"],
  ]);
  h.unmount();
  const other = inbox({
    list: async () => page([{ ...row(), seen_at: "seen", read_at: "read" }]),
  });
  assert(JSON.stringify(await ready(other)).includes("Read"));
  assert.equal(other.calls.mark.length, 0);
  other.unmount();
});
test("same-tick repeated opens make one state mutation", async () => {
  const d = deferred(),
    h = inbox({ mark: () => d.promise });
  let tree = await ready(h);
  const action = button(tree, "Open").props.onClick;
  action();
  action();
  assert.equal(h.calls.mark.length, 1);
  d.resolve({ item: { ...row(), seen_at: "seen" } });
  await tick();
  h.unmount();
});
test("failed state update keeps unread; retry repeats only the idempotent state action", async () => {
  let n = 0;
  const h = inbox({
    mark: async (id, a) => {
      if (++n === 1) throw Error("offline");
      return { item: { ...row(id), seen_at: "seen", read_at: null } };
    },
  });
  let tree = await ready(h);
  await button(tree, "Open").props.onClick();
  tree = h.render();
  assert(nodes(tree, (n) => n.props.role === "alert").length);
  assert.equal(button(tree, "Mark read"), undefined);
  await button(tree, "Open").props.onClick();
  assert(button(h.render(), "Mark read"));
  assert.equal(h.calls.mark.length, 2);
  h.unmount();
});
test("failed list exposes retry, not an empty inbox", async () => {
  let n = 0;
  const h = inbox({
    list: async () => {
      if (++n === 1) throw Error();
      return page();
    },
  });
  let tree = await ready(h);
  assert(button(tree, "Retry"));
  assert(!JSON.stringify(tree).includes("No announcements."));
  await button(tree, "Retry").props.onClick();
  assert(button(h.render(), "Open"));
  assert.equal(h.calls.mark.length, 0);
  h.unmount();
});
test("empty successful list is distinct from failed/loading", async () => {
  const h = inbox({ list: async () => page([]) });
  assert(JSON.stringify(await ready(h)).includes("No announcements."));
  h.unmount();
});
test("unread filter changes the server query; read row disappears only after ack", async () => {
  const h = inbox();
  let tree = await ready(h);
  button(tree, "Unread").props.onClick();
  h.render();
  await tick();
  tree = h.render();
  assert.deepEqual(h.calls.list.at(-1), [undefined, true]);
  await button(tree, "Open").props.onClick();
  tree = h.render();
  await button(tree, "Mark read").props.onClick();
  assert.equal(button(h.render(), "Open"), undefined);
  h.unmount();
});
test("keyset more combines pages without duplicate rows", async () => {
  const h = inbox({
    list: async (before) =>
      before ? page([row(2), row(1)]) : page([row(3), row(2)], 2),
  });
  let tree = await ready(h);
  await button(tree, "Load more").props.onClick();
  tree = h.render();
  assert.deepEqual(h.calls.list.at(-1), [2, false]);
  assert.equal(nodes(tree, (n) => n.type === "article").length, 3);
  h.unmount();
});
test("refresh never replays a read mutation", async () => {
  const h = inbox();
  let tree = await ready(h);
  await button(tree, "Refresh").props.onClick();
  assert.equal(h.calls.list.length, 2);
  assert.equal(h.calls.mark.length, 0);
  h.unmount();
});
test("unmounted inbox ignores a late list response", async () => {
  const d = deferred(),
    h = inbox({ list: () => d.promise });
  h.render();
  h.unmount();
  d.resolve(page());
  await tick();
  assert.equal(h.calls.mark.length, 0);
});
test("Persian inbox copy is concrete", async () => {
  const h = inbox({ locale: "fa" });
  assert(button(await ready(h), "مشاهده"));
  h.unmount();
});

const storage = () => {
  const m = new Map();
  return {
    getItem: (k) => m.get(k) ?? null,
    setItem: (k, v) => m.set(k, v),
    removeItem: (k) => m.delete(k),
  };
};
class ApiError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status;
    this.code = code;
  }
}
function admin({
  store = storage(),
  owner = "1:10",
  post = async () => ({ item: row() }),
  list = async () => page(),
} = {}) {
  Object.defineProperty(globalThis, "sessionStorage", {
    value: store,
    configurable: true,
  });
  const h = screenHarness(),
    calls = [];
  const { Publisher } = h.compile(root("admin/screens/Announcements.tsx"), {
    "@shared/announcements": copy,
    "@shared/http": { ApiError },
    "@shared/format": { dateTime: (x) => x },
    "../session": {
      useSession: () => ({ locale: "en", me: { id: 10, shop_id: 1 } }),
    },
    "../ui": {
      Button: "Button",
      Input: "Input",
      Page: "Page",
      Section: "Section",
      Select: "Select",
    },
    "../api": {
      call: (path, options) => {
        calls.push({ path, options });
        return options?.method === "POST" ? post(path, options) : list(path);
      },
    },
  });
  return { ...h, calls, store, render: () => h.render(Publisher, { owner }) };
}
async function preview(h) {
  let tree = await ready(h);
  nodes(tree, (n) => n.type === "Input")[0].props.onChange({
    target: { value: "My notice" },
  });
  nodes(tree, (n) => n.type === "textarea")[0].props.onChange({
    target: { value: row().text },
  });
  tree = h.render();
  button(tree, "Publish in Mini App").props.onClick();
  return h.render();
}
test("admin preview has no write and displays exact plain text", async () => {
  const h = admin();
  const tree = await preview(h);
  assert(button(tree, "Publish this exact text to this shop’s customers?"));
  assert.equal(h.calls.filter((x) => x.options?.method === "POST").length, 0);
  assert(
    nodes(tree, (n) => n.type === "p" && n.props.children === row().text)
      .length,
  );
  assert.equal(nodes(tree, (n) => n.props.dangerouslySetInnerHTML).length, 0);
  h.unmount();
});
test("admin same-tick publish is gated and payload is saved before write", async () => {
  const d = deferred();
  let h;
  h = admin({
    post: () => {
      assert(h.store.getItem("pauru-announcement:1:10"));
      return d.promise;
    },
  });
  let tree = await preview(h);
  const action = button(
    tree,
    "Publish this exact text to this shop’s customers?",
  ).props.onClick;
  action();
  action();
  assert.equal(h.calls.filter((x) => x.options?.method === "POST").length, 1);
  d.resolve({ item: row() });
  await tick();
  assert.equal(h.store.getItem("pauru-announcement:1:10"), null);
  h.unmount();
});
test("unknown publication result survives reload and retries exact immutable payload", async () => {
  const store = storage();
  let h = admin({
    store,
    post: async () => {
      throw Error("lost");
    },
  });
  let tree = await preview(h);
  await button(
    tree,
    "Publish this exact text to this shop’s customers?",
  ).props.onClick();
  const original = h.calls.find((x) => x.options?.method === "POST").options
    .body;
  tree = h.render();
  assert(button(tree, "Retry same publication"));
  assert.equal(button(tree, "Publish in Mini App"), undefined);
  h.unmount();
  h = admin({ store });
  tree = await ready(h);
  await button(tree, "Retry same publication").props.onClick();
  assert.deepEqual(
    h.calls.find((x) => x.options?.method === "POST").options.body,
    original,
  );
  h.unmount();
});
test("another staff/shop owner does not recover a different owner’s pending payload", async () => {
  const store = storage();
  store.setItem(
    "pauru-announcement:1:10",
    JSON.stringify({
      request_key: "key",
      title: "secret pending",
      text: "x",
      kind: "operational",
      starts_at: null,
      expires_at: null,
    }),
  );
  const h = admin({ store, owner: "2:10" });
  assert(!JSON.stringify(await ready(h)).includes("secret pending"));
  h.unmount();
});
test("unavailable storage prevents publication", async () => {
  const h = admin({
    store: {
      getItem() {
        throw Error();
      },
      setItem() {
        throw Error();
      },
      removeItem() {
        throw Error();
      },
    },
  });
  const tree = await preview(h);
  const confirm = button(
    tree,
    "Publish this exact text to this shop’s customers?",
  );
  if (confirm) await confirm.props.onClick();
  assert.equal(h.calls.filter((x) => x.options?.method === "POST").length, 0);
  h.unmount();
});
test("storage write failure sends no publication", async () => {
  const store = storage(),
    h = admin({ store });
  let tree = await preview(h);
  store.setItem = () => {
    throw Error("quota");
  };
  await button(
    tree,
    "Publish this exact text to this shop’s customers?",
  ).props.onClick();
  assert.equal(h.calls.filter((x) => x.options?.method === "POST").length, 0);
  h.unmount();
});
test("explicit pre-write validation rejection unfreezes the draft safely", async () => {
  const h = admin({
    post: async () => {
      throw new ApiError(422, "validation_error");
    },
  });
  let tree = await preview(h);
  await button(
    tree,
    "Publish this exact text to this shop’s customers?",
  ).props.onClick();
  tree = h.render();
  assert(button(tree, "Publish in Mini App"));
  assert.equal(h.store.getItem("pauru-announcement:1:10"), null);
  h.unmount();
});
test("conflict does not discard an uncertain publication key", async () => {
  const h = admin({
    post: async () => {
      throw new ApiError(409, "announcement_conflict");
    },
  });
  let tree = await preview(h);
  await button(
    tree,
    "Publish this exact text to this shop’s customers?",
  ).props.onClick();
  assert(button(h.render(), "Retry same publication"));
  assert(h.store.getItem("pauru-announcement:1:10"));
  h.unmount();
});
test("revoke only calls the dedicated idempotent endpoint", async () => {
  const h = admin();
  let tree = await ready(h);
  await button(tree, "Revoke publication").props.onClick();
  assert.deepEqual(
    h.calls.filter((x) => x.options?.method === "POST").map((x) => x.path),
    ["/api/admin/announcements/1/revoke"],
  );
  h.unmount();
});
test("admin failed list has read-only retry", async () => {
  let n = 0;
  const h = admin({
    list: async () => {
      if (++n === 1) throw Error();
      return page([]);
    },
  });
  let tree = await ready(h);
  await button(tree, "Retry").props.onClick();
  assert(JSON.stringify(h.render()).includes("No announcements."));
  assert.equal(h.calls.filter((x) => x.options?.method === "POST").length, 0);
  h.unmount();
});
test("owner-keyed mounted subtree, backend auth and UI navigation are wired", () => {
  const notifications = readFileSync(
    root("app/screens/Notifications.tsx"),
    "utf8",
  );
  assert.match(notifications, /<NotificationFeed key=\{me.id\}/);
  assert.match(notifications, /<AnnouncementInbox key=/);
  assert.match(notifications, /buildAlerts/);
  const admin = readFileSync(root("admin/App.tsx"), "utf8");
  assert.match(
    admin,
    /path="\/announcements" element=\{g\("broadcast.send", <Announcements/,
  );
  // The admin menu lives in nav.ts since the hub regroup.
  const nav = readFileSync(root("admin/nav.ts"), "utf8");
  assert.match(
    nav,
    /page\("\/announcements"[^\n]*permission: "broadcast.send"[^\n]*shopWide: true/,
  );
});

test("StrictMode effect replay neither gets stuck nor accepts an obsolete list", async () => {
  const old = deferred(),
    fresh = deferred();
  let n = 0;
  const h = inbox({ list: () => (++n === 1 ? old.promise : fresh.promise) });
  h.render();
  h.replayEffects();
  assert.equal(h.calls.list.length, 2);
  old.resolve(page([row(99)]));
  await tick();
  fresh.resolve(page([row(2)]));
  await tick();
  const tree = h.render();
  assert.equal(
    nodes(tree, (x) => x.type === "h3" && x.props.children === "Notice 2")
      .length,
    1,
  );
  assert.equal(
    nodes(tree, (x) => x.type === "h3" && x.props.children === "Notice 99")
      .length,
    0,
  );
  assert.equal(button(tree, "Open").props.disabled, false);
  h.unmount();
});
