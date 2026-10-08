import { Browser, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

function isoDaysFromNow(offset: number) {
  const day = new Date();
  day.setUTCDate(day.getUTCDate() + offset);
  return day.toISOString().slice(0, 10);
}

test("707A — un WorkPackage visible ouvre et enregistre un brouillon canonique", async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "PROJECT_MANAGER" },
  });
  const page = await context.newPage();

  try {
    await page.goto("/");
    const catalogResponse = await page.request.get("/api/v1/task-catalog?project_number=P-251&active_only=true");
    expect(catalogResponse.status(), await catalogResponse.text()).toBe(200);
    const catalog = await catalogResponse.json() as Array<{
      id: string | null;
      code: string;
      resource_class_code: string | null;
    }>;
    const task = catalog.find((row) => row.code === "210" && row.id);
    expect(task, "La fixture E2E doit contenir la tâche 210 du projet P-251").toBeDefined();

    const name = `Brouillon WP 707A ${Date.now()}`;
    const start = isoDaysFromNow(2);
    const end = isoDaysFromNow(5);
    const createWorkPackage = await page.request.post("/api/v1/work-packages", {
      headers: { "Idempotency-Key": `e2e-707a-${Date.now()}` },
      data: {
        project_number: "P-251",
        task_catalog_item_id: task!.id,
        name,
        description: "Parcours brouillon depuis Moyen terme",
        start_date: start,
        end_date: end,
        planned_hours: 16,
        resource_class_code: task!.resource_class_code,
      },
    });
    expect(createWorkPackage.status(), await createWorkPackage.text()).toBe(201);
    const { reference } = await createWorkPackage.json() as { reference: string };

    await page.locator(".main-nav").getByRole("button", { name: /Moyen terme/i }).click();
    const row = page.locator(".mt-timeline-row").filter({ hasText: name });
    await expect(row).toBeVisible();

    // Simule un projet visible dans Moyen terme mais absent de la liste
    // active-only de Demandes. Le catalogue scoped complet doit le résoudre.
    await page.route("**/api/v1/projects?**", async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.get("active_only") === "true") {
        await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      } else {
        await route.continue();
      }
    });
    const draftPosts: Array<Record<string, unknown>> = [];
    await page.route("**/api/v1/demands", async (route) => {
      if (route.request().method() === "POST") {
        draftPosts.push(route.request().postDataJSON() as Record<string, unknown>);
      }
      await route.continue();
    });

    await row.getByRole("button", { name: "Créer une demande" }).click();
    const editor = page.locator(".demand-editor-form");
    await expect(editor.getByRole("heading", { name: "Nouvelle demande" })).toBeVisible();
    await expect(editor.getByRole("combobox", { name: "Projet", exact: true })).toHaveValue(/P-251/);
    await expect(editor.getByRole("combobox", { name: "Plage moyen terme \/ WorkPackage" })).toHaveValue(new RegExp(name));
    await expect(editor.getByRole("combobox", { name: "Tâche ERP" })).toHaveValue(/210/);

    const save = editor.getByRole("button", { name: "Créer le brouillon" });
    await expect(save).toBeEnabled();
    expect(draftPosts).toHaveLength(0); // Ouvrir le formulaire ne crée pas de demande vide.

    await editor.getByLabel("Début souhaité").fill(start);
    await editor.getByLabel("Fin souhaitée").fill(start);
    await editor.getByLabel("Jours actifs souhaités").fill("2");
    await save.click();
    await expect(page.locator(".error-panel")).toContainText("cible de 2 jours actifs dépasse les 1 dates");
    expect(draftPosts).toHaveLength(0);

    // Les heures sont facultatives au brouillon; aucune validation métier n'est contournée.
    await editor.getByLabel("Jours actifs souhaités").fill("1");
    await save.click();
    await expect(page.locator(".demand-notice")).toContainText("créée en brouillon");
    expect(draftPosts).toHaveLength(1);
    expect(draftPosts[0].project_number).toBe("P-251");
    expect(draftPosts[0].work_package_ref).toBe(reference);
    expect(draftPosts[0].task_code).toBe("210");
    expect(draftPosts[0].estimated_hours).toBeNull();

    const demandNumber = (await page.locator(".demand-editor-heading h2").textContent())?.trim();
    expect(demandNumber).toMatch(/^DMO-/);
    const detailResponse = await page.request.get(`/api/v1/demands/${encodeURIComponent(demandNumber!)}`);
    expect(detailResponse.status(), await detailResponse.text()).toBe(200);
    const demand = await detailResponse.json() as {
      status: string;
      work_package_ref: string | null;
      task_code: string | null;
    };
    expect(demand.status).toBe("Brouillon");
    expect(demand.work_package_ref).toBe(reference);
    expect(demand.task_code).toBe("210");
  } finally {
    await context.close();
  }
});
