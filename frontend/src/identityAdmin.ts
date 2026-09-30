export type OidcState = "pending" | "linked" | "conflict";

export function oidcStateLabel(state: OidcState) {
  if (state === "linked") return "Lié";
  if (state === "conflict") return "Conflit";
  return "En attente de première connexion";
}

export function accountStateLabel(active: boolean) {
  return active ? "Actif" : "Inactif";
}
