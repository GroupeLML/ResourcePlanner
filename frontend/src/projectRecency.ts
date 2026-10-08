import type { ProjectReadModel } from "./api";

/**
 * RP_Projects exposes a numeric Acumatica ProjectId, but not an ERP creation date.
 * StartDate is the planned project start, LastModifiedDateTime is a modification
 * timestamp, and the local SQL created_at is the import date: none is ERP creation.
 *
 * Deterministic fallback (newest first): numeric ERP ProjectId descending; then
 * natural/numeric project number descending; then stable internal ID. Records without
 * a numeric ERP ID follow ERP-backed records. This is a recency proxy, not a
 * guarantee of the actual ERP creation date.
 *
 * Sort only already authorized project collections; never fetch on keystrokes.
 */
export function compareProjectNumbersRecentFirst(left: string, right: string): number {
  return right.localeCompare(left, "fr-CA", { numeric: true, sensitivity: "base" })
    || right.localeCompare(left, "fr-CA", { numeric: true });
}

function numericErpId(project: ProjectReadModel): bigint | null {
  const value = project.erp_external_id?.trim();
  return value && /^\d+$/.test(value) ? BigInt(value) : null;
}

export function compareProjectsRecentFirst(left: ProjectReadModel, right: ProjectReadModel): number {
  const leftErp = numericErpId(left);
  const rightErp = numericErpId(right);
  if (leftErp !== null && rightErp !== null) {
    if (leftErp > rightErp) return -1;
    if (leftErp < rightErp) return 1;
  } else if (leftErp !== null) {
    return -1;
  } else if (rightErp !== null) {
    return 1;
  }
  return compareProjectNumbersRecentFirst(left.number, right.number)
    || left.id.localeCompare(right.id, "fr-CA");
}

export function sortProjectsRecentFirst(projects: readonly ProjectReadModel[]): ProjectReadModel[] {
  return [...projects].sort(compareProjectsRecentFirst);
}
