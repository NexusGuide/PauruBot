import test from "node:test";
import assert from "node:assert/strict";
async function load(name) {
  return import(new URL(`../src/app/receipts/${name}.ts`, import.meta.url));
}
const {
  parseReceipt,
  canSend,
  validateFile,
  confirmUpload,
  parseInbox,
  checkoutAccounts,
} = await load("model");
const { translator } = await load("copy");
const p = () => ({
  id: 19,
  provider: "card",
  status: "pending",
  amount: 100,
  currency: "IRT",
  order_id: 3,
  fulfilled: false,
  expires_at: "2099-01-01T00:00:00Z",
  revision: "a".repeat(64),
  correction: null,
  receipt_submitted: false,
  phase: "awaiting_receipt",
  can_upload: true,
});
test("valid state excludes raw metadata and staff fields", () => {
  const result = parseReceipt({ ...p(), meta: { private: "secret" } }, 19);
  assert(!("meta" in result));
  assert(canSend(result));
});
test("correction instruction stays literal", () => {
  const correction = {
    kind: "card",
    note: "<script>alert(1)</script>",
    requested_at: "2026-10-14T00:00:00Z",
  };
  assert.equal(
    parseReceipt({ ...p(), phase: "correction_required", correction })
      .correction.note,
    correction.note,
  );
});
for (const extra of [
  { id: 18 },
  { id: 0 },
  { amount: "100" },
  { amount: -1 },
  { amount: 1.5 },
  { order_id: 0 },
  { fulfilled: "false" },
  { currency: "" },
  { expires_at: "bad" },
  { expires_at: "2099-01-01T00:00:00" },
  { revision: "bad" },
  { phase: "future" },
  { status: "future" },
  { can_upload: "true" },
  { receipt_submitted: true },
  { can_upload: false },
  { provider: "stars" },
  { phase: "correction_required" },
  { phase: "under_review" },
  { phase: "paid" },
  { phase: "closed" },
  { correction: { kind: "weird", note: null, requested_at: "bad" } },
])
  test("reject malformed/contradictory state " + JSON.stringify(extra), () =>
    assert.throws(() => parseReceipt({ ...p(), ...extra }, 19)),
  );
for (const [status, phase] of [
  ["verifying", "under_review"],
  ["paid", "paid"],
  ["failed", "closed"],
  ["expired", "closed"],
  ["disputed", "closed"],
  ["refunded", "closed"],
  ["created", "unknown"],
])
  test("known inactive state " + status, () =>
    assert(
      !canSend(parseReceipt({ ...p(), status, phase, can_upload: false })),
    ),
  );
test("deadline locally blocks even formerly enabled snapshot", () =>
  assert(
    !canSend(parseReceipt({ ...p(), expires_at: "2020-01-01T00:00:00Z" })),
  ));
for (const type of ["image/jpeg", "image/png", "image/webp"])
  test("supported file " + type, () =>
    assert(validateFile({ type, size: 8 * 1024 * 1024 })),
  );
for (const file of [
  { type: "image/png", size: 0 },
  { type: "image/png", size: 8 * 1024 * 1024 + 1 },
  { type: "image/gif", size: 1 },
  { type: "image/svg+xml", size: 1 },
  { type: "", size: 1 },
])
  test("reject file " + JSON.stringify(file), () =>
    assert(!validateFile(file)),
  );
for (const [status, phase] of [
  ["verifying", "under_review"],
  ["paid", "paid"],
  ["failed", "closed"],
])
  test("confirm actual upload outcome " + status, () =>
    assert.equal(
      confirmUpload(
        {
          ...p(),
          status,
          phase,
          revision: "b".repeat(64),
          receipt_submitted: true,
          can_upload: false,
        },
        p(),
      ).status,
      status,
    ),
  );
for (const extra of [
  { revision: "a".repeat(64) },
  { id: 20 },
  { amount: 999 },
  { order_id: 99 },
  { currency: "USD" },
  { receipt_submitted: false },
  { status: "pending", phase: "awaiting_receipt", can_upload: true },
])
  test("never falsely confirm upload " + JSON.stringify(extra), () =>
    assert.throws(() =>
      confirmUpload(
        {
          ...p(),
          status: "verifying",
          phase: "under_review",
          revision: "b".repeat(64),
          receipt_submitted: true,
          can_upload: false,
          ...extra,
        },
        p(),
      ),
    ),
  );
test("valid bounded keyset inbox", () => {
  const items = Array.from({ length: 20 }, (_, i) => ({ ...p(), id: 50 - i }));
  const result = parseInbox({ items, next_before: 31 }, 51);
  assert.equal(result.items.length, 20);
  assert.equal(result.next_before, 31);
});
for (const raw of [
  { items: [p(), p()], next_before: null },
  { items: [{ ...p(), id: 20 }, p()], next_before: 18 },
  { items: [], next_before: 9 },
  { items: [{ ...p(), id: 50 }], next_before: null },
  { items: null, next_before: null },
])
  test("invalid inbox " + JSON.stringify(raw), () =>
    assert.throws(() => parseInbox(raw, 50)),
  );
test("checkout card details only for matching initial payment", () => {
  const accounts = [
    { number: "1234567890123456", holder: "Name", bank: "Bank" },
  ];
  assert.equal(checkoutAccounts(accounts, 100, p()).length, 1);
  assert.equal(checkoutAccounts(accounts, 101, p()).length, 0);
  assert.equal(
    checkoutAccounts(accounts, 100, { ...p(), phase: "correction_required" })
      .length,
    0,
  );
  assert.equal(
    checkoutAccounts(
      [{ number: "javascript:bad", holder: "", bank: "" }],
      100,
      p(),
    ).length,
    0,
  );
});
for (const locale of ["fa", "en"])
  test("review and uncertainty copy " + locale, () => {
    for (const key of [
      "ack",
      "warning",
      "uncertain",
      "error",
      "initial",
      "missingAccounts",
    ])
      assert(translator(locale)(key).length > 10);
  });
