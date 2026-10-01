import { parseIsoDate, startOfWeek, toIsoDate } from "./dates";

export type ResourceSortMode = "manual" | "availability" | "alphabetical";

const DEFAULT_RESOURCE_SORT_MODE: ResourceSortMode = "manual";
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const PREFIX = "resourceplanner:planning";

function storage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function ownerKey(localUserId: string | null) {
  const normalized = localUserId?.trim();
  return encodeURIComponent(normalized || "application");
}

function key(localUserId: string | null, preference: "week-start" | "resource-sort" | "collapsed-classes") {
  return `${PREFIX}:${ownerKey(localUserId)}:${preference}`;
}

function read(localUserId: string | null, preference: "week-start" | "resource-sort" | "collapsed-classes") {
  try {
    return storage()?.getItem(key(localUserId, preference)) ?? null;
  } catch {
    return null;
  }
}

function write(
  localUserId: string | null,
  preference: "week-start" | "resource-sort" | "collapsed-classes",
  value: string,
) {
  try {
    storage()?.setItem(key(localUserId, preference), value);
  } catch {
    // Browser preferences must never prevent the Planning from rendering.
  }
}

export function loadPlanningResourceSort(localUserId: string | null): ResourceSortMode {
  const value = read(localUserId, "resource-sort");
  return value === "manual" || value === "availability" || value === "alphabetical"
    ? value
    : DEFAULT_RESOURCE_SORT_MODE;
}

export function savePlanningResourceSort(localUserId: string | null, value: ResourceSortMode) {
  write(localUserId, "resource-sort", value);
}

export function loadPlanningWeekStart(localUserId: string | null, now = new Date()) {
  const fallback = startOfWeek(now);
  const value = read(localUserId, "week-start");
  if (!value || !ISO_DATE.test(value)) return fallback;

  const parsed = parseIsoDate(value);
  if (Number.isNaN(parsed.getTime()) || toIsoDate(parsed) !== value) return fallback;
  if (toIsoDate(startOfWeek(parsed)) !== value) return fallback;
  return parsed;
}

export function savePlanningWeekStart(localUserId: string | null, value: Date) {
  write(localUserId, "week-start", toIsoDate(startOfWeek(value)));
}

export function loadCollapsedPlanningClasses(localUserId: string | null): string[] {
  const value = read(localUserId, "collapsed-classes");
  if (!value) return [];
  try {
    const parsed: unknown = JSON.parse(value);
    if (!Array.isArray(parsed)) return [];
    return [...new Set(parsed.filter((item): item is string => typeof item === "string" && item.trim().length > 0))]
      .sort((left, right) => left.localeCompare(right, "fr-CA"));
  } catch {
    return [];
  }
}

export function saveCollapsedPlanningClasses(localUserId: string | null, values: readonly string[]) {
  const normalized = [...new Set(values.map((value) => value.trim()).filter(Boolean))]
    .sort((left, right) => left.localeCompare(right, "fr-CA"));
  write(localUserId, "collapsed-classes", JSON.stringify(normalized));
}
