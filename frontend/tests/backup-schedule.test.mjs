// The automatic backup schedule on the Backups page: the intervals offered and its summary line.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { screenHarness } from "./helpers/rename-screen.mjs";

const h = screenHarness();
const schedule = h.compile(new URL("../src/admin/backup-schedule.ts", import.meta.url), {});
const locale = (name) => JSON.parse(readFileSync(new URL(`../src/admin/locales/${name}.json`, import.meta.url), "utf8"));
const base = { enabled: true, every_hours: 24, hour: 4, keep: 14, telegram: true };

test("intervals: a few hours, or whole days at an hour", () => {
  assert.equal(schedule.atAnHour(6), false);
  assert.equal(schedule.atAnHour(24), true);
  assert.deepEqual(schedule.everyLabel(6), ["backup_every_hours", { n: 6 }]);
  assert.deepEqual(schedule.everyLabel(24), ["backup_every_day", {}]);
  assert.deepEqual(schedule.everyLabel(72), ["backup_every_days", { n: 3 }]);
  assert.deepEqual(schedule.everyLabel(168), ["backup_every_week", {}]);
  for (const hours of schedule.EVERY_OPTIONS) assert.ok(hours < 24 || hours % 24 === 0, `${hours} is whole days`);
  assert.deepEqual(schedule.everyOptions(5).slice(0, 5), [1, 2, 3, 4, 5], "a saved odd value stays selectable");
  assert.equal(schedule.everyOptions(6).length, schedule.EVERY_OPTIONS.length);
});

test("the summary says what will happen", () => {
  assert.deepEqual(schedule.summary({ ...base, enabled: false }), ["backup_summary_off", {}]);
  assert.deepEqual(schedule.summary(base), ["backup_summary_daily", { hour: 4, keep: 14 }]);
  assert.deepEqual(schedule.summary({ ...base, every_hours: 48 }), ["backup_summary_days", { days: 2, hour: 4, keep: 14 }]);
  assert.deepEqual(schedule.summary({ ...base, every_hours: 6 }), ["backup_summary_hours", { hours: 6, keep: 14 }]);
});

test("every label exists in both admin languages", () => {
  const fa = locale("fa"), en = locale("en");
  const keys = [
    "backup_auto_title", "backup_summary_off", "backup_summary_hours", "backup_summary_daily", "backup_summary_days",
    "backup_every_hours", "backup_every_day", "backup_every_days", "backup_every_week",
    "setting_backup_auto_enabled", "setting_backup_every_hours", "setting_backup_hour", "setting_backup_keep",
    "setting_backup_send_telegram", "err_backup_every_whole_days", "err_backup_root_only", "err_backup_settings_elsewhere",
  ];
  for (const key of keys) {
    assert.ok(key in fa, `fa is missing ${key}`);
    assert.ok(key in en, `en is missing ${key}`);
  }
});
