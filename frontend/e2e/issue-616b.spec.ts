import { expect, test } from "@playwright/test";

import { resourceDisplayName } from "../src/resourceLabels";

test("616B retire uniquement le préfixe numérique des libellés ressources", () => {
  expect(resourceDisplayName("1001 - ALICE EXEMPLE")).toBe("ALICE EXEMPLE");
  expect(resourceDisplayName(" 1005 – ELI ÉCHANTILLON ")).toBe("ELI ÉCHANTILLON");
  expect(resourceDisplayName("42—Jean Tremblay")).toBe("Jean Tremblay");
  expect(resourceDisplayName("Jean Tremblay")).toBe("Jean Tremblay");
  expect(resourceDisplayName("EMP-123 - Jean Tremblay")).toBe("EMP-123 - Jean Tremblay");
  expect(resourceDisplayName("123 Électriciens")).toBe("123 Électriciens");
  expect(resourceDisplayName(null)).toBe("");
});
