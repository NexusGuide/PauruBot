// The automatic backup schedule on the Backups page: the intervals offered and how a schedule
// reads in a sentence. Pure, so it is tested on its own.

export interface BackupSchedule {
  enabled: boolean;
  every_hours: number;
  hour: number;
  keep: number;
  telegram: boolean;
}

/** Every few hours, or whole days at a set hour (the server refuses 36 hours and the like). */
export const EVERY_OPTIONS = [1, 2, 3, 4, 6, 8, 12, 24, 48, 72, 168] as const;

/** Past a few hours the backup runs once a day or less, at the hour chosen. */
export function atAnHour(everyHours: number): boolean {
  return everyHours >= 24;
}

/** The label of an interval: a translation key and its values. */
export function everyLabel(hours: number): [string, Record<string, number>] {
  if (hours === 24) return ["backup_every_day", {}];
  if (hours === 168) return ["backup_every_week", {}];
  if (atAnHour(hours)) return ["backup_every_days", { n: hours / 24 }];
  return ["backup_every_hours", { n: hours }];
}

/** The page's one-line summary: a translation key and its values. */
export function summary(schedule: BackupSchedule): [string, Record<string, number>] {
  if (!schedule.enabled) return ["backup_summary_off", {}];
  if (schedule.every_hours === 24) return ["backup_summary_daily", { hour: schedule.hour, keep: schedule.keep }];
  if (atAnHour(schedule.every_hours)) {
    return ["backup_summary_days", { days: schedule.every_hours / 24, hour: schedule.hour, keep: schedule.keep }];
  }
  return ["backup_summary_hours", { hours: schedule.every_hours, keep: schedule.keep }];
}

/** The intervals to offer: the usual ones, plus a saved value that is not among them. */
export function everyOptions(current: number): number[] {
  const options: number[] = [...EVERY_OPTIONS];
  if (!options.includes(current)) options.push(current);
  return options.sort((a, b) => a - b);
}
