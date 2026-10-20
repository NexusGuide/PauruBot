/** One local screen's actions: no duplicate submits or updates after leaving the screen. */
export function createActionGate() {
  let active = true;
  let busy = false;
  return {
    get active() { return active; },
    get busy() { return busy; },
    begin() {
      if (!active || busy) return false;
      busy = true;
      return true;
    },
    finish() { busy = false; },
    close() { active = false; },
  };
}

/** The work may already have reached the server; closing suppresses UI effects, not remote work. */
export async function runServiceAction<T>(
  gate: ReturnType<typeof createActionGate>,
  work: () => Promise<T>,
  success: (value: T) => void,
  failure: (error: unknown) => void,
  settled: () => void,
) {
  if (!gate.begin()) return;
  try {
    const value = await work();
    if (gate.active) success(value);
  } catch (error) {
    if (gate.active) failure(error);
  } finally {
    gate.finish();
    if (gate.active) settled();
  }
}
