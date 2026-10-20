/** An opaque URL key identifies one deliberate draft, not a payment credential. */
export function prepareDraft(params: URLSearchParams, label: string, uuid = () => crypto.randomUUID()) {
  const next = new URLSearchParams(params);
  if (!next.has("draft")) {
    next.set("draft", uuid());
    next.set("draft_label", label);
  }
  return next;
}
export function reviseDraft(params: URLSearchParams, code: string, label: string, uuid = () => crypto.randomUUID()) {
  const next = new URLSearchParams(params);
  next.set("draft", uuid());
  next.set("draft_code", code);
  next.set("draft_label", label);
  return next;
}
