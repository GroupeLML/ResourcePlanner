const RESOURCE_NUMBER_PREFIX = /^\s*\d+\s*[-–—]\s*(?=\S)/u;

export function resourceDisplayName(value: string | null | undefined): string {
  const raw = (value ?? "").trim();
  if (!raw) return "";
  return raw.replace(RESOURCE_NUMBER_PREFIX, "").trim() || raw;
}

/** Localized nominal sort excluding numeric ERP prefixes; stable ID for ties. */
export function compareResourcesByDisplayName(
  left: { id: string; name: string | null | undefined },
  right: { id: string; name: string | null | undefined },
): number {
  const leftName = resourceDisplayName(left.name);
  const rightName = resourceDisplayName(right.name);
  const normalized = leftName.localeCompare(rightName, "fr-CA", { sensitivity: "base" });
  if (normalized !== 0) return normalized;
  const exact = leftName.localeCompare(rightName, "fr-CA");
  if (exact !== 0) return exact;
  return left.id.localeCompare(right.id, "fr-CA");
}
