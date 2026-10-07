const RESOURCE_NUMBER_PREFIX = /^\s*\d+\s*[-–—]\s*(?=\S)/u;

export function resourceDisplayName(value: string | null | undefined): string {
  const raw = (value ?? "").trim();
  if (!raw) return "";
  return raw.replace(RESOURCE_NUMBER_PREFIX, "").trim() || raw;
}
