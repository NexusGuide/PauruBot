/** Client keys carry no credentials. Preserve uncertain requests across screen reloads. */
type Store = Pick<Storage, "getItem" | "setItem" | "removeItem">;
export function actionGate() {
  let active = false;
  return {
    enter() { if (active) return false; active = true; return true; },
    leave() { active = false; },
  };
}

export class TopupIntents {
  private memory = new Map<string, string>();
  private store?: Store;
  private uuid: () => string;
  constructor(store?: Store, uuid: () => string = () => crypto.randomUUID()) {
    this.store = store;
    this.uuid = uuid;
  }
  private slot(customer: number, amount: number, method: string) {
    return `pauru.topup.v1:${customer}:${amount}:${method}`;
  }
  get(customer: number, amount: number, method: string): string {
    const slot = this.slot(customer, amount, method);
    let key = this.memory.get(slot);
    if (!key) {
      try { key = this.store?.getItem(slot) ?? undefined; } catch { /* restricted WebView */ }
    }
    if (!key || !/^[A-Za-z0-9._:-]{8,128}$/.test(key)) key = this.uuid();
    this.memory.set(slot, key);
    try { this.store?.setItem(slot, key); } catch { /* in-memory fallback */ }
    return key;
  }
  clear(customer: number, amount: number, method: string, expected: string) {
    const slot = this.slot(customer, amount, method);
    if (this.memory.get(slot) === expected) this.memory.delete(slot);
    try { if (this.store?.getItem(slot) === expected) this.store.removeItem(slot); } catch { /* fallback */ }
  }
}

let shared: TopupIntents | undefined;
export function topupIntents() {
  if (!shared) {
    let store: Store | undefined;
    try { store = window.sessionStorage; } catch { /* private/restricted WebView */ }
    shared = new TopupIntents(store);
  }
  return shared;
}
