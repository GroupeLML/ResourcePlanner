import { addDays, startOfWeek, toIsoDate } from "./dates";
import type { UserViewScope } from "./auth-api";
import type { ResourceSortMode } from "./planningPreferences";

export type DetachedPlanningContext = {
  weekStart: Date | null;
  scope: UserViewScope | null;
  search: string;
  project: string;
  confirmation: "all" | "confirmed" | "tentative";
  classFilter: string;
  resourceFilter: string;
  onlyWithCapacity: boolean;
  filtersExpanded: boolean;
  resourceSortMode: ResourceSortMode | null;
};

export type DetachedPlanningUrlState = {
  weekStart: Date;
  scope: UserViewScope | null;
  search: string;
  project: string;
  confirmation: "all" | "confirmed" | "tentative";
  classFilter: string;
  resourceFilter: string;
  onlyWithCapacity: boolean;
  filtersExpanded: boolean;
  resourceSortMode: ResourceSortMode;
};

export type PlanningInvalidationMessage = {
  type: "planning-invalidated";
  week: string;
  planningVersion: number | null;
};

const DETACHED_PATH = "/planning/resources-window";
const PLANNING_CHANNEL = "resourceplanner-planning";

function parseIsoDate(value: string | null): Date | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const parsed = new Date(`${value}T12:00:00`);
  if (Number.isNaN(parsed.getTime()) || toIsoDate(parsed) !== value) return null;
  return startOfWeek(parsed);
}

function enumValue<T extends string>(
  value: string | null,
  allowed: readonly T[],
  fallback: T,
): T {
  return value && allowed.includes(value as T) ? value as T : fallback;
}

export function isDetachedPlanningResourcesRoute(pathname = window.location.pathname) {
  return pathname.replace(/\/+$/, "") === DETACHED_PATH;
}

export function readDetachedPlanningContext(
  search = window.location.search,
): DetachedPlanningContext {
  const params = new URLSearchParams(search);
  const requestedScope = params.get("scope");
  const requestedSort = params.get("sort");
  return {
    weekStart: parseIsoDate(params.get("week")),
    scope: requestedScope === "mine" || requestedScope === "global"
      ? requestedScope
      : null,
    search: params.get("q") ?? "",
    project: params.get("project") || "all",
    confirmation: enumValue(
      params.get("confirmation"),
      ["all", "confirmed", "tentative"] as const,
      "all",
    ),
    classFilter: params.get("class") || "all",
    resourceFilter: params.get("resource") || "all",
    onlyWithCapacity: params.get("capacity") === "1",
    filtersExpanded: params.get("filters") === "1",
    resourceSortMode:
      requestedSort === "manual"
      || requestedSort === "availability"
      || requestedSort === "alphabetical"
        ? requestedSort
        : null,
  };
}

export function buildDetachedPlanningUrl(state: DetachedPlanningUrlState) {
  const url = new URL(DETACHED_PATH, window.location.origin);
  url.searchParams.set("week", toIsoDate(startOfWeek(state.weekStart)));
  if (state.scope) url.searchParams.set("scope", state.scope);
  if (state.search) url.searchParams.set("q", state.search);
  if (state.project !== "all") url.searchParams.set("project", state.project);
  if (state.confirmation !== "all") {
    url.searchParams.set("confirmation", state.confirmation);
  }
  if (state.classFilter !== "all") url.searchParams.set("class", state.classFilter);
  if (state.resourceFilter !== "all") {
    url.searchParams.set("resource", state.resourceFilter);
  }
  if (state.onlyWithCapacity) url.searchParams.set("capacity", "1");
  if (state.filtersExpanded) url.searchParams.set("filters", "1");
  url.searchParams.set("sort", state.resourceSortMode);
  return url.toString();
}

function isPlanningInvalidationMessage(value: unknown): value is PlanningInvalidationMessage {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return (
    candidate.type === "planning-invalidated"
    && typeof candidate.week === "string"
    && /^\d{4}-\d{2}-\d{2}$/.test(candidate.week)
    && (
      candidate.planningVersion === null
      || typeof candidate.planningVersion === "number"
    )
  );
}

export function publishPlanningInvalidation(
  weekStart: Date,
  planningVersion: number | null,
) {
  if (typeof BroadcastChannel === "undefined") return;
  const channel = new BroadcastChannel(PLANNING_CHANNEL);
  try {
    channel.postMessage({
      type: "planning-invalidated",
      week: toIsoDate(startOfWeek(weekStart)),
      planningVersion,
    } satisfies PlanningInvalidationMessage);
  } finally {
    channel.close();
  }
}

export function subscribePlanningInvalidations(
  onInvalidated: (message: PlanningInvalidationMessage) => void,
) {
  if (typeof BroadcastChannel === "undefined") return () => undefined;
  const channel = new BroadcastChannel(PLANNING_CHANNEL);
  channel.onmessage = (event: MessageEvent<unknown>) => {
    if (!isPlanningInvalidationMessage(event.data)) return;
    onInvalidated(event.data);
  };
  return () => channel.close();
}

// Exported only for deterministic tests of week normalization.
export function nextDetachedWeek(weekStart: Date, deltaWeeks: number) {
  return startOfWeek(addDays(weekStart, deltaWeeks * 7));
}
