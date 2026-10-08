import { expect, test } from "@playwright/test";

import type { ProjectReadModel } from "../src/api";
import { compareProjectNumbersRecentFirst, sortProjectsRecentFirst } from "../src/projectRecency";

function project(id: string, number: string, erpId: string | null): ProjectReadModel {
  return {
    id,
    number,
    erp_external_id: erpId,
    name: number,
    client: null,
    project_manager: null,
    status: "active",
    active: true,
  };
}

test("712A utilise les identifiants ERP numériques, pas l'ordre lexicographique", () => {
  const input = [
    project("old", "P-9", "9"),
    project("new", "P-2", "100"),
    project("middle", "P-10", "20"),
  ];
  expect(sortProjectsRecentFirst(input).map((row) => row.id)).toEqual(["new", "middle", "old"]);
  expect(input.map((row) => row.id)).toEqual(["old", "new", "middle"]);
});

test("712A garantit un fallback naturel stable pour les projets sans ID ERP numérique", () => {
  const input = [
    project("p2", "P-2", null),
    project("p10", "P-10", null),
    project("p1", "P-1", "local-1"),
    project("erp", "P-3", "0012"),
  ];
  expect(sortProjectsRecentFirst(input).map((row) => row.id)).toEqual(["erp", "p10", "p2", "p1"]);
  expect(compareProjectNumbersRecentFirst("P-10", "P-2")).toBeLessThan(0);
  const tied = [project("z", "P-10", null), project("a", "P-10", null)];
  expect(sortProjectsRecentFirst(tied).map((row) => row.id)).toEqual(["a", "z"]);
});

test("712A trie uniquement la collection déjà autorisée", () => {
  const authorized = [project("b", "P-2", "2"), project("a", "P-1", "1")];
  expect(sortProjectsRecentFirst(authorized).map((row) => row.id)).toEqual(["b", "a"]);
  expect(sortProjectsRecentFirst(authorized).some((row) => row.number === "P-99")).toBe(false);
});
