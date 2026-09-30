import { Browser, BrowserContext, Locator, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

type Role = "ADMIN" | "PROJECT_MANAGER" | "COORDINATOR" | "TECHNICIAN";

const TEST_DOMAIN = "example.test";

function testEmail(localPart: string) {
  return `${localPart}${String.fromCharCode(64)}${TEST_DOMAIN}`;
}

function testPhone() {
  return ["450", "555", "0199"].join("-");
}

const DISPLAY_NAMES: Record<Role, string> = {
  ADMIN: "Administrateur E2E",
  PROJECT_MANAGER: "Chargé E2E",
  COORDINATOR: "Coordonnateur E2E",
  TECHNICIAN: "Technicien Alice",
};

function addDays(value: Date, days: number) {
  const next = new Date(value);
  next.setDate(next.getDate() + days);
  return next;
}

function localIso(value: Date) {
  const adjusted = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return adjusted.toISOString().slice(0, 10);
}

function startOfWeek(value: Date) {
  const day = value.getDay();
  return addDays(value, -(day === 0 ? 6 : day - 1));
}

function acceptanceDates() {
  const today = new Date();
  const nextMonday = addDays(startOfWeek(today), 7);
  return {
    today: localIso(today),
    d1: localIso(nextMonday),
    d2: localIso(addDays(nextMonday, 1)),
    d3: localIso(addDays(nextMonday, 2)),
    d4: localIso(addDays(nextMonday, 3)),
    d5: localIso(addDays(nextMonday, 4)),
  };
}

async function openAs(browser: Browser, role?: Role) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: role ? { "X-E2E-Role": role } : { "X-E2E-Anonymous": "1" },
  });
  const page = await context.newPage();
  await page.goto("/");
  if (role) await expect(page.locator(".sidebar-footer")).toContainText(DISPLAY_NAMES[role]);
  return { context, page };
}

async function closeContext(context: BrowserContext) {
  await context.close();
}

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

function labelled(scope: Locator, label: string, control: "select" | "input" | "textarea") {
  return scope.locator("label").filter({ hasText: label }).first().locator(control);
}

async function dragWithDataTransfer(page: Page, source: Locator, target: Locator) {
  const dataTransfer = await page.evaluateHandle(() => new DataTransfer());
  await source.dispatchEvent("dragstart", { dataTransfer });
  await target.dispatchEvent("dragenter", { dataTransfer });
  await target.dispatchEvent("dragover", { dataTransfer });
  await target.dispatchEvent("drop", { dataTransfer });
  await source.dispatchEvent("dragend", { dataTransfer });
  await dataTransfer.dispose();
}

async function selectOptionContaining(select: Locator, text: string) {
  const option = select.locator("option", { hasText: text }).first();
  await expect(option).toBeAttached();
  const value = await option.getAttribute("value");
  expect(value).not.toBeNull();
  await select.selectOption(value!);
}

function demandNumberFrom(text: string | null) {
  const match = String(text || "").match(/\b(DMO-[A-Za-z0-9-]+)\b/);
  expect(match, `Numéro de demande introuvable dans: ${text}`).not.toBeNull();
  return match![1];
}

async function createDemand(
  page: Page,
  input: {
    start: string;
    end: string;
    priority?: string;
    hours: string;
    activeDays: string;
    description: string;
    proposedResource?: string;
    workPackage?: string;
  },
) {
  await navigateMain(page, "Demandes");
  await expect(page.getByRole("heading", { name: "Demandes", level: 1 })).toBeVisible();
  await page.getByRole("button", { name: /Nouvelle demande/ }).click();
  const editor = page.locator(".demand-editor-form");
  await expect(editor.getByRole("heading", { name: "Nouvelle demande" })).toBeVisible();

  await labelled(editor, "Projet", "select").selectOption("P-251");
  await expect(editor.getByText("Recherche catalogue ERP", { exact: true })).toHaveCount(0);
  const taskSelect = labelled(editor, "Tâche ERP", "select");
  await expect(taskSelect.locator("option", { hasText: "210 — AUTOMATISATION E2E" })).toBeAttached();
  await taskSelect.selectOption("210");
  if (input.workPackage) {
    await selectOptionContaining(labelled(editor, "Plage moyen terme", "select"), input.workPackage);
  }
  await labelled(editor, "Priorité", "select").selectOption(input.priority || "Normale");
  await labelled(editor, "Confirmation", "select").selectOption("Confirmée");
  await labelled(editor, "Début souhaité", "input").fill(input.start);
  await labelled(editor, "Fin souhaitée", "input").fill(input.end);
  await labelled(editor, "Heures estimées totales", "input").fill(input.hours);
  await labelled(editor, "Jours actifs souhaités", "input").fill(input.activeDays);
  await labelled(editor, "Description", "textarea").fill(input.description);
  if (input.proposedResource) {
    await labelled(editor, "Ressource proposée", "select").selectOption(input.proposedResource);
  }
  return editor;
}

async function openDemandDetail(page: Page, demandNumber: string) {
  await navigateMain(page, "Demandes");
  const demandSubnav = page.locator(".demands-subnav");
  if (await demandSubnav.isVisible()) {
    const requestsButton = demandSubnav.getByRole("button", { name: "Demandes", exact: true });
    if (await requestsButton.count()) {
      await requestsButton.click();
    }
  }
  const card = page.locator(".demand-card").filter({ hasText: demandNumber }).first();
  await expect(card).toBeVisible();
  await card.click();
  await expect(page.locator(`.demand-detail-context[data-demand-number="${demandNumber}"]`)).toBeVisible();
}

async function workflowSelect(page: Page, demandNumber: string) {
  await openDemandDetail(page, demandNumber);
  const section = page.locator(".demand-detail-section").filter({ hasText: "Workflow et impact" }).first();
  const isOpen = await section.evaluate((node) => (node as HTMLDetailsElement).open);
  if (!isOpen) {
    await section.locator("summary").click();
  }
  await expect(section.locator(".workflow-detail-panel")).toBeVisible();
}

async function periodsSelect(page: Page, demandNumber: string) {
  await openDemandDetail(page, demandNumber);
  const section = page.locator(".demand-detail-section").filter({ hasText: "Périodes de travail" }).first();
  const isOpen = await section.evaluate((node) => (node as HTMLDetailsElement).open);
  if (!isOpen) {
    await section.locator("summary").click();
  }
  await expect(section.locator(".period-demand-summary")).toBeVisible();
}

test("V2 local acceptance path runs through React, Chromium, FastAPI and SQLite", async ({ browser }) => {
  test.setTimeout(240_000);
  const { today, d1, d2, d3, d4, d5 } = acceptanceDates();
  let demandNumber = "";
  let urgentNumber = "";

  await test.step("401 and role-guided shell are visible in React", async () => {
    const anonymous = await openAs(browser);
    await expect(anonymous.page.getByRole("heading", { name: "Connexion requise" })).toBeVisible();
    await expect(anonymous.page.getByText("session RessourcePlanner est absente ou expirée", { exact: false })).toBeVisible();
    await closeContext(anonymous.context);

    const admin = await openAs(browser, "ADMIN");
    await expect(admin.page.locator(".main-nav").getByText("Ressources", { exact: true })).toBeVisible();
    await expect(admin.page.locator(".main-nav").getByText("Utilisateurs", { exact: true })).toBeVisible();
    await expect(admin.page.locator(".main-nav").getByText("Configuration", { exact: true })).toBeVisible();

    await navigateMain(admin.page, "Configuration");
    await expect(admin.page.getByRole("heading", { name: "Configuration", level: 2 })).toBeVisible();
    const smtpForm = admin.page.locator(".smtp-form");
    await labelled(smtpForm, "Serveur SMTP", "input").fill("smtp.example.invalid");
    await labelled(smtpForm, "Port", "input").fill("587");
    await labelled(smtpForm, "Sécurité", "select").selectOption("STARTTLS");
    await labelled(smtpForm, "Courriel expéditeur", "input").fill(testEmail("planning"));
    await smtpForm.getByLabel("Envoi SMTP activé").check();
    await smtpForm.getByRole("button", { name: "Enregistrer", exact: true }).click();
    await expect(admin.page.locator(".configuration-notice")).toContainText("Configuration SMTP enregistrée");
    await smtpForm.getByRole("button", { name: "Tester la connexion enregistrée" }).click();
    await expect(admin.page.locator(".configuration-notice")).toContainText("Connexion SMTP réussie");

    await navigateMain(admin.page, "Utilisateurs");
    await expect(admin.page.getByRole("heading", { name: "Utilisateurs et rôles", level: 2 })).toBeVisible();
    const pendingErpRow = admin.page
      .locator(".erp-user-directory-table tbody tr")
      .filter({ hasText: "ERP-OIDC-PENDING" });
    await expect(pendingErpRow).toContainText("Actif");
    await expect(pendingErpRow).toContainText("TECHNICIAN");
    await expect(pendingErpRow).toContainText("En attente de première connexion");
    const appUserId = (await pendingErpRow.locator("td").nth(5).innerText()).trim();
    expect(appUserId).not.toBe("Non provisionné");

    const pendingAppUser = admin.page
      .locator(".user-admin-user-list > button")
      .filter({ hasText: "Utilisateur OIDC E2E" });
    await expect(pendingAppUser).toContainText("Compte · Actif");
    await expect(pendingAppUser).toContainText("OIDC · En attente de première connexion");
    await pendingAppUser.click();
    const pendingStatus = admin.page.getByTestId("app-user-identity-status");
    await expect(pendingStatus).toContainText("Compte");
    await expect(pendingStatus).toContainText("Actif");
    await expect(pendingStatus).toContainText("En attente de première connexion");
    await expect(pendingStatus).toContainText(appUserId);
    await expect(pendingStatus).toContainText("ERP-OIDC-PENDING");

    const linkResponse = await admin.context.request.get("/__e2e__/identity/link-pending");
    expect(linkResponse.ok()).toBeTruthy();
    const linkedIdentity = (await linkResponse.json()) as {
      app_user_id: string;
      erp_user_id: string;
      oidc_state: string;
    };
    expect(linkedIdentity.app_user_id).toBe(appUserId);
    expect(linkedIdentity.erp_user_id).toBe("ERP-OIDC-PENDING");
    expect(linkedIdentity.oidc_state).toBe("linked");

    await admin.page.reload();
    await navigateMain(admin.page, "Utilisateurs");
    const linkedErpRow = admin.page
      .locator(".erp-user-directory-table tbody tr")
      .filter({ hasText: "ERP-OIDC-PENDING" });
    await expect(linkedErpRow).toContainText(appUserId);
    await expect(linkedErpRow).toContainText("Lié");
    const linkedAppUser = admin.page
      .locator(".user-admin-user-list > button")
      .filter({ hasText: "Utilisateur OIDC E2E" });
    await expect(linkedAppUser).toContainText("Compte · Actif");
    await expect(linkedAppUser).toContainText("OIDC · Lié");
    await closeContext(admin.context);

    const projectManager = await openAs(browser, "PROJECT_MANAGER");
    await expect(projectManager.page.locator(".main-nav").getByText("Communications", { exact: true })).toHaveCount(0);
    await expect(projectManager.page.locator(".main-nav").getByText("Ressources", { exact: true })).toHaveCount(0);
    await expect(projectManager.page.locator(".main-nav").getByText("Utilisateurs", { exact: true })).toHaveCount(0);
    await expect(projectManager.page.locator(".main-nav").getByText("Configuration", { exact: true })).toHaveCount(0);
    await closeContext(projectManager.context);
  });

  await test.step("project manager creates WorkPackage, demand, periods and selected alternative", async () => {
    const { context, page } = await openAs(browser, "PROJECT_MANAGER");

    await navigateMain(page, "Moyen terme");
    await page.getByRole("button", { name: /WorkPackage/ }).click();
    const workPackageDialog = page.getByRole("dialog", { name: "Créer un lot" });
    await labelled(workPackageDialog, "Projet", "select").selectOption("P-251");
    const workPackageTask = labelled(workPackageDialog, "Tâche ERP", "select");
    await expect(
      workPackageTask.locator("option", { hasText: "210 — AUTOMATISATION E2E" }),
    ).toBeAttached();
    await workPackageTask.selectOption({ label: "210 — AUTOMATISATION E2E" });
    await labelled(workPackageDialog, "Code", "input").fill("WP-E2E");
    await labelled(workPackageDialog, "Nom", "input").fill("Lot acceptation Playwright");
    await labelled(workPackageDialog, "Début", "input").fill(d1);
    await labelled(workPackageDialog, "Fin", "input").fill(d5);
    await labelled(workPackageDialog, "Heures prévues", "input").fill("40");
    await labelled(workPackageDialog, "Description", "textarea").fill("Parcours React V2 avec Chromium");
    await workPackageDialog.getByRole("button", { name: "Créer le WorkPackage" }).click();
    await expect(workPackageDialog).toBeHidden();
    await expect(page.getByText("WP-E2E", { exact: true }).first()).toBeVisible();

    const editor = await createDemand(page, {
      start: d1,
      end: d5,
      hours: "20",
      activeDays: "6",
      description: "Demande acceptation navigateur V2",
      proposedResource: "Alice",
      workPackage: "WP-E2E",
    });
    await editor.getByRole("button", { name: "Créer le brouillon" }).click();
    await expect(page.locator(".error-panel")).toContainText("cible de 6 jours actifs dépasse les 5 dates");

    await labelled(editor, "Jours actifs souhaités", "input").fill("4");
    await editor.getByRole("button", { name: "Créer le brouillon" }).click();
    const createdNotice = page.locator(".demand-notice");
    await expect(createdNotice).toContainText("créée en brouillon");
    demandNumber = demandNumberFrom(await createdNotice.textContent());
    await expect(page.locator(".demand-editor-panel")).toContainText(demandNumber);

    await periodsSelect(page, demandNumber);
    await page.getByRole("button", { name: /Période cumulative/ }).click();
    await page.getByRole("button", { name: /Groupe alternatif/ }).click();

    const cumulative = page.locator(".period-card.cumulative").first();
    await labelled(cumulative, "Début", "input").fill(d1);
    await labelled(cumulative, "Fin", "input").fill(d3);
    await labelled(cumulative, "Heures totales", "input").fill("12");
    await labelled(cumulative, "Ressources simultanées", "input").fill("1");
    await labelled(cumulative, "Jours actifs souhaités", "input").fill("3");
    await labelled(cumulative, "Confirmation", "select").selectOption("Tentative");
    await labelled(cumulative, "Ressource proposée", "select").selectOption("Alice");

    const alternatives = page.locator(".alternative-option");
    await expect(alternatives).toHaveCount(2);
    for (const [card, day, confirmation] of [
      [alternatives.nth(0).locator(".period-card"), d4, "Confirmée"],
      [alternatives.nth(1).locator(".period-card"), d5, "Tentative"],
    ] as const) {
      await labelled(card, "Début", "input").fill(day);
      await labelled(card, "Fin", "input").fill(day);
      await labelled(card, "Heures totales", "input").fill("8");
      await labelled(card, "Ressources simultanées", "input").fill("1");
      await labelled(card, "Jours actifs souhaités", "input").fill("1");
      await labelled(card, "Confirmation", "select").selectOption(confirmation);
      await labelled(card, "Ressource proposée", "select").selectOption("Bob");
    }

    await page.getByRole("button", { name: "Enregistrer les périodes" }).click();
    await expect(page.locator(".demand-notice").filter({ hasText: "Périodes enregistrées." })).toHaveText("Périodes enregistrées.");
    await alternatives.nth(0).getByRole("button", { name: "Retenir cette option" }).click();
    await expect(alternatives.nth(0).getByRole("button", { name: "Option retenue" })).toBeVisible();

    await labelled(editor, "Description / contexte de la demande", "textarea").fill(
      "Demande acceptation navigateur V2 modifiée avant soumission",
    );
    const workflowSection = page.locator(".demand-detail-section").filter({ hasText: "Workflow et impact" }).first();
    if (!(await workflowSection.evaluate((node) => (node as HTMLDetailsElement).open))) {
      await workflowSection.locator("summary").click();
    }
    const submitButton = workflowSection.getByRole("button", { name: "Soumettre", exact: true });
    await expect(submitButton).toBeDisabled();
    await expect(
      workflowSection.getByText("Enregistre les modifications avant de poursuivre.", { exact: true }),
    ).toBeVisible();

    await editor.getByRole("button", { name: "Enregistrer les modifications" }).click();
    await expect(
      page.locator(".demand-notice").filter({ hasText: "Modification enregistrée." }),
    ).toBeVisible();
    await expect(submitButton).toBeEnabled();
    await submitButton.click();
    await expect(page.locator(".demand-notice").filter({ hasText: "soumise pour approbation" })).toContainText("soumise pour approbation");
    await expect(page.getByTestId("plan-delta-preview")).toBeVisible();

    await expect(
      page.getByRole("button", { name: "Approuver", exact: true }),
    ).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Segments", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Urgence", exact: true })).toHaveCount(0);

    await closeContext(context);
  });

  await test.step("coordinator approves, sees generated segments and current plan", async () => {
    const { context, page } = await openAs(browser, "COORDINATOR");
    await navigateMain(page, "Demandes");
    await workflowSelect(page, demandNumber);
    await expect(page.getByTestId("plan-delta-preview")).toContainText("Plan actuel → plan proposé");
    await page.getByLabel(/Commentaire d’approbation/).fill("Acceptation initiale Playwright");
    await page.getByRole("button", { name: "Approuver", exact: true }).click();
    await expect(page.locator(".demand-notice").filter({ hasText: "Demande approuvée" })).toContainText("Demande approuvée");

    await page.getByRole("button", { name: "Segments", exact: true }).click();
    await page.locator(".segment-demand-card").filter({ hasText: demandNumber }).click();
    const segmentCards = page.locator(".segment-list .segment-card");
    await expect(segmentCards).toHaveCount(2);
    const cumulativeCard = segmentCards.filter({ hasText: d1 }).filter({ hasText: d3 }).first();
    const alternativeCard = segmentCards.filter({ hasText: d4 }).first();
    await expect(cumulativeCard).toContainText("12 h");
    await expect(cumulativeCard).toContainText("Tentative");
    await expect(cumulativeCard).toContainText("Cible 3 jour(s) actif(s)");
    await expect(alternativeCard).toContainText("8 h");
    await expect(alternativeCard).toContainText("Confirmée");

    await navigateMain(page, "Planning opérationnel");
    await page.getByRole("button", { name: /Suivante/ }).click();
    await page.getByLabel("Recherche").fill(demandNumber);
    await expect(page.locator(".shift-card")).toHaveCount(4);
    await expect(page.locator(".shift-card").filter({ hasText: "Tentative" }).first()).toBeVisible();
    await expect(page.locator(".shift-card").filter({ hasText: "Confirmée" }).first()).toBeVisible();

    await closeContext(context);
  });

  await test.step("approved envelope change shows read-only delta before reapproval", async () => {
    const projectManager = await openAs(browser, "PROJECT_MANAGER");
    await navigateMain(projectManager.page, "Demandes");
    await periodsSelect(projectManager.page, demandNumber);
    const cumulative = projectManager.page.locator(".period-card.cumulative").first();
    await labelled(cumulative, "Heures totales", "input").fill("16");
    await projectManager.page.getByRole("button", { name: "Enregistrer les périodes" }).click();
    await expect(projectManager.page.locator(".demand-notice").filter({ hasText: "doit être approuvée de nouveau" })).toContainText("doit être approuvée de nouveau");

    const firstAlternative = projectManager.page.locator(".alternative-option").nth(0);
    await firstAlternative.getByRole("button", { name: "Retenir cette option" }).click();
    await expect(firstAlternative.getByRole("button", { name: "Option retenue" })).toBeVisible();

    await workflowSelect(projectManager.page, demandNumber);
    await expect(projectManager.page.getByTestId("approval-state")).toContainText("CAPTURED");
    await expect(projectManager.page.getByTestId("envelope-decision")).toContainText(
      "Réapprobation requise",
    );
    await expect(projectManager.page.getByTestId("envelope-decision")).toContainText(
      "l’ancien plan reste la référence",
    );
    const delta = projectManager.page.getByTestId("plan-delta-preview");
    await expect(delta).toContainText("Plan actuel → plan proposé");
    await expect(delta.locator(".plan-delta-row").first()).toBeVisible();
    await closeContext(projectManager.context);

    const coordinator = await openAs(browser, "COORDINATOR");
    await navigateMain(coordinator.page, "Demandes");
    await workflowSelect(coordinator.page, demandNumber);
    await expect(coordinator.page.getByTestId("plan-delta-preview").locator(".plan-delta-row").first()).toBeVisible();
    await coordinator.page.getByLabel(/Commentaire d’approbation/).fill("Réapprobation après delta Playwright");
    await coordinator.page.getByRole("button", { name: "Approuver", exact: true }).click();
    await expect(coordinator.page.locator(".demand-notice").filter({ hasText: "Demande approuvée" })).toContainText("Demande approuvée");
    await closeContext(coordinator.context);
  });

  await test.step("segment profile, active days and audit are editable through React", async () => {
    const { context, page } = await openAs(browser, "COORDINATOR");
    await navigateMain(page, "Demandes");
    await page.getByRole("button", { name: "Segments", exact: true }).click();
    await page.locator(".segment-demand-card").filter({ hasText: demandNumber }).click();
    const cumulativeCard = page.locator(".segment-list .segment-card").filter({ hasText: d1 }).filter({ hasText: d3 }).first();
    await expect(cumulativeCard).toContainText("16 h");
    await cumulativeCard.click();

    const dialog = page.getByRole("dialog", { name: "Modifier le segment" });
    await labelled(dialog, "Profil de charge", "select").selectOption("BELL");
    await labelled(dialog, "Description", "textarea").fill("Profil en cloche validé par Playwright");
    await dialog.getByRole("button", { name: "Enregistrer", exact: true }).click();
    await expect(dialog).toBeHidden();

    const refreshedCard = page.locator(".segment-list .segment-card").filter({ hasText: d1 }).filter({ hasText: d3 }).first();
    await expect(refreshedCard).toContainText("Charge en cloche");
    await expect(refreshedCard).toContainText("Cible 3 jour(s) actif(s)");
    await refreshedCard.click();
    const reopened = page.getByRole("dialog", { name: "Modifier le segment" });
    await expect(labelled(reopened, "Profil de charge", "select")).toHaveValue("BELL");
    await expect(reopened.getByLabel("Historique des changements")).toContainText("Coordonnateur E2E");
    await reopened.getByRole("button", { name: "Fermer" }).first().click();

    await closeContext(context);
  });

  await test.step("editing a shift forces an explicit overallocation decision and keeps audit visible", async () => {
    const { context, page } = await openAs(browser, "COORDINATOR");
    await navigateMain(page, "Planning opérationnel");
    await page.getByRole("button", { name: /Suivante/ }).click();
    await page.getByLabel("Recherche").fill(demandNumber);

    const bobRow = page.locator(".resource-row").filter({ hasText: "Bob" });
    await expect(bobRow).toBeVisible();
    await bobRow.getByRole("button", { name: /Modifier le quart P-251, 8 heures/ }).first().click();
    let dialog = page.getByRole("dialog", { name: "Modifier le quart" });
    await labelled(dialog, "Heures", "input").fill("10");
    await dialog.getByRole("button", { name: "Enregistrer les modifications" }).click();
    const choice = dialog.getByRole("alert");
    await expect(choice).toContainText("dépasse les heures prévues du segment");
    await choice.getByRole("button", { name: /Conserver la dérogation/ }).click();
    await expect(dialog).toBeHidden();

    const updatedBobRow = page.locator(".resource-row").filter({ hasText: "Bob" });
    await updatedBobRow.getByRole("button", { name: /Modifier le quart P-251, 10 heures/ }).first().click();
    dialog = page.getByRole("dialog", { name: "Modifier le quart" });
    await expect(dialog).toContainText("Surallocation manuelle active : +2 h");
    await expect(dialog.getByLabel("Historique des changements")).toContainText("Coordonnateur E2E");
    await dialog.getByRole("button", { name: "Fermer" }).first().click();

    await closeContext(context);
  });

  await test.step("communications use project To/CC, manual review and only create fake local drafts", async () => {
    const { context, page } = await openAs(browser, "COORDINATOR");
    await navigateMain(page, "Communications");
    await expect(page.getByRole("heading", { name: "Communications de planification" })).toBeVisible();
    await expect(page.getByText("Destinataires gérés dans Utilisateurs")).toBeVisible();
    await expect(page.locator(".contact-row")).toHaveCount(0);

    await page.getByLabel("Semaine du").fill(d1);
    await page.getByRole("button", { name: "Générer la prévisualisation" }).click();

    const draft = page.locator(".draft-card").filter({ hasText: "Projet P-251" }).first();
    await expect(draft).toBeVisible();
    await expect(draft.locator(".draft-recipients")).toContainText(testEmail("pm"));
    await expect(draft.locator(".draft-recipients")).toContainText(testEmail("alice"));
    await expect(draft.locator(".draft-recipients")).toContainText(testEmail("bob"));
    await expect(draft.getByText("Courriel manquant")).toHaveCount(0);
    await expect(draft.locator("textarea")).toHaveValue(/Chargé de projet Démo/);
    await expect(draft.locator("textarea")).toHaveValue(new RegExp(testPhone()));
    await draft.locator(".draft-subject").fill("Confirmation E2E — P-251");

    await page.getByRole("button", { name: "Préparer le lot" }).click();
    await expect(page.locator(".communications-notice")).toContainText("Lot projet préparé");

    const batch = page.locator(".batch-row").first();
    await expect(batch).toContainText("Sujet : Confirmation E2E — P-251");
    await expect(batch).toContainText(`To : ${testEmail("pm")}`);
    await expect(batch).toContainText(testEmail("alice"));
    await expect(batch).toContainText(testEmail("bob"));

    await batch.getByRole("button", { name: "Approuver" }).click();
    await expect(page.locator(".communications-notice")).toContainText("Lot projet approuvé");
    page.once("dialog", (dialog) => dialog.accept());
    await page.locator(".batch-row").first().getByRole("button", { name: "Créer brouillons M365" }).click();
    await expect(page.locator(".communications-notice")).toContainText("brouillon(s) M365 créé(s)");

    page.once("dialog", (dialog) => dialog.accept());
    await page.locator(".batch-row").first().getByRole("button", { name: "Envoyer par SMTP" }).click();
    await expect(page.locator(".communications-notice")).toContainText("Envoi SMTP complété");
    await expect(page.locator(".batch-row").first()).toContainText("COMMUNICATED");
    await expect(page.locator(".batch-row").first()).toContainText("SMTP : SENT");
    await expect(page.locator(".batch-row").first().getByRole("button", { name: "Envoyer par SMTP" })).toHaveCount(0);
    await closeContext(context);
  });

  await test.step("demand history exposes backend audit actors", async () => {
    const { context, page } = await openAs(browser, "COORDINATOR");
    await openDemandDetail(page, demandNumber);
    const historySection = page.locator(".demand-detail-section").filter({ hasText: "Historique" }).first();
    const isOpen = await historySection.evaluate((node) => (node as HTMLDetailsElement).open);
    if (!isOpen) {
      await historySection.locator("summary").click();
    }
    await expect(historySection.locator(".demand-history-timeline")).toContainText("Coordonnateur E2E");
    await expect(historySection.locator(".demand-history-timeline li").first()).toBeVisible();
    await closeContext(context);
  });

  await test.step("React-created Urgente demand can use emergency override and regular approval", async () => {
    const projectManager = await openAs(browser, "PROJECT_MANAGER");
    const editor = await createDemand(projectManager.page, {
      start: today,
      end: today,
      priority: "Urgente",
      hours: "4",
      activeDays: "1",
      description: "Intervention urgente créée dans React",
      proposedResource: "Alice",
    });
    await editor.getByRole("button", { name: "Créer le brouillon" }).click();
    const urgentNotice = projectManager.page.locator(".demand-notice");
    await expect(urgentNotice).toContainText("créée en brouillon");
    urgentNumber = demandNumberFrom(await urgentNotice.textContent());
    await workflowSelect(projectManager.page, urgentNumber);
    await projectManager.page.getByRole("button", { name: "Soumettre", exact: true }).click();
    await expect(projectManager.page.locator(".demand-notice").filter({ hasText: "soumise pour approbation" })).toContainText("soumise pour approbation");
    await closeContext(projectManager.context);

    const coordinator = await openAs(browser, "COORDINATOR");
    await navigateMain(coordinator.page, "Demandes");
    await coordinator.page.getByRole("button", { name: "Urgence", exact: true }).click();
    const urgentPanel = coordinator.page.locator(".workflow-list-panel");
    await labelled(urgentPanel, "Demande urgente", "select").selectOption(urgentNumber);
    await expect(coordinator.page.locator(".workflow-detail-panel")).toContainText("Urgente");
    await coordinator.page.getByLabel(/Justification de l’urgence/).fill("Intervention requise aujourd'hui");
    coordinator.page.once("dialog", (dialog) => dialog.accept());
    await coordinator.page.getByRole("button", { name: "Planifier en urgence" }).click();
    await expect(coordinator.page.getByTestId("emergency-override-active")).toContainText("Dérogation d’approbation active");
    await expect(coordinator.page.getByTestId("emergency-override-active")).toContainText("Coordonnateur E2E");

    await workflowSelect(coordinator.page, urgentNumber);
    await coordinator.page.getByLabel(/Commentaire d’approbation/).fill("Régularisation après urgence");
    await coordinator.page.getByRole("button", { name: "Approuver", exact: true }).click();
    await expect(coordinator.page.locator(".demand-notice").filter({ hasText: "Demande approuvée" })).toContainText("Demande approuvée");
    await closeContext(coordinator.context);
  });

  await test.step("technician lands on Today and can inspect Tomorrow and My week without mutation navigation", async () => {
    const { context, page } = await openAs(browser, "TECHNICIAN");
    await expect(page.getByRole("heading", { name: "Aujourd’hui", level: 1 })).toBeVisible();
    await expect(page.locator(".my-schedule-summary")).toContainText("Alice");
    await expect(page.locator(".my-schedule-summary")).toContainText("4 h");
    await expect(page.locator(".my-shift-card").first()).toContainText("P-251");

    await page.getByRole("button", { name: "Demain", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Demain", level: 1 })).toBeVisible();
    await page.getByRole("button", { name: "Ma semaine", exact: true }).click();
    await expect(page.getByRole("heading", { name: /Semaine du/ })).toBeVisible();
    await expect(page.locator(".my-schedule-summary")).toContainText("Alice");

    await expect(page.locator(".main-nav").getByText("Communications", { exact: true })).toHaveCount(0);
    await expect(page.locator(".main-nav").getByText("Ressources", { exact: true })).toHaveCount(0);
    await expect(page.locator(".main-nav").getByText("Utilisateurs", { exact: true })).toHaveCount(0);
    await expect(page.locator(".main-nav").getByText("Configuration", { exact: true })).toHaveCount(0);

    await navigateMain(page, "Demandes");
    await expect(page.getByRole("button", { name: "Segments", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /Périodes & alternatives/ })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Workflow", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Urgence", exact: true })).toHaveCount(0);

    await closeContext(context);
  });

  await test.step("coordinator uses the contextual DnD dialog for cancel, extend, move, split and duplicate", async () => {
    const { context, page } = await openAs(browser, "COORDINATOR");
    await navigateMain(page, "Planning opérationnel");
    await page.getByRole("button", { name: /Suivante/ }).click();

    await page.getByRole("button", { name: /Quick Shift/ }).click();
    const quickShift = page.getByRole("dialog", { name: "Créer un Quick Shift" });
    await labelled(quickShift, "Projet", "select").selectOption("P-251");
    await labelled(quickShift, "Technicien", "select").selectOption("Alice");
    await labelled(quickShift, "Date", "input").fill(d2);
    await labelled(quickShift, "Heures", "input").fill("1.25");
    await labelled(quickShift, "Confirmation", "select").selectOption("Confirmée");
    await labelled(quickShift, "Description", "textarea").fill("Validation drag-and-drop Playwright");
    await labelled(quickShift, "Note", "textarea").fill("DnD #275");
    await quickShift.getByRole("button", { name: "Créer le Quick Shift" }).click();
    await expect(quickShift).toBeHidden();

    const shiftsResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    expect(shiftsResponse.ok()).toBeTruthy();
    const shifts = await shiftsResponse.json() as Array<{
      allocation_id: string;
      segment_id: string;
      resource_name: string;
      work_date: string;
      note: string | null;
    }>;
    const createdShift = shifts.find((row) => row.note === "DnD #275");
    expect(createdShift, "Quart Quick Shift DnD introuvable après création").toBeDefined();
    const allocationId = createdShift!.allocation_id;
    expect(createdShift!.resource_name).toBe("Alice");
    expect(createdShift!.work_date).toBe(d2);

    const segmentResponse = await page.request.get(
      `/api/v1/segments/${encodeURIComponent(createdShift!.segment_id)}`,
    );
    expect(segmentResponse.ok()).toBeTruthy();
    const createdSegment = await segmentResponse.json() as {
      segment_id: string;
      start_date: string;
      end_date: string;
    };
    expect(createdSegment.segment_id).toBe(createdShift!.segment_id);
    expect(createdSegment.start_date).toBe(d2);
    expect(createdSegment.end_date).toBe(d2);

    const aliceRow = page.locator(".resource-identity").filter({ hasText: "Alice" }).first().locator("..");
    const bobRow = page.locator(".resource-identity").filter({ hasText: "Bob" }).first().locator("..");
    const sourceCell = aliceRow.locator(`.planning-drop-day[data-day="${d2}"]`);
    const source = sourceCell.locator(`.shift-card[data-allocation-id="${allocationId}"]`);
    await expect(source).toBeVisible();

    const outsideTarget = bobRow.locator(`.planning-drop-day[data-day="${d3}"]`);
    const evaluatePromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && response.url().includes(`/api/v1/allocations/${encodeURIComponent(allocationId)}/evaluate-drop`)
    ));
    await dragWithDataTransfer(page, source, outsideTarget);
    const evaluated = await evaluatePromise;
    expect(evaluated.status(), await evaluated.text()).toBe(200);
    expect(evaluated.request().postDataJSON()).toEqual({
      resource_id: "R-BOB",
      day: d3,
      outside_standard_hours: false,
    });
    expect((await evaluated.json()).actions.map((row: { code: string }) => row.code)).toEqual([
      "EXTEND_AND_MOVE",
      "CANCEL",
    ]);

    let dropDialog = page.getByRole("dialog", { name: "Choisir l’action du déplacement" });
    await expect(dropDialog).toContainText("Fenêtre proposée");
    await expect(dropDialog).toContainText("Besoin autonome");
    await dropDialog.getByRole("button", { name: "Annuler", exact: true }).click();
    await expect(dropDialog).toBeHidden();

    const unchangedSegmentResponse = await page.request.get(
      `/api/v1/segments/${encodeURIComponent(createdShift!.segment_id)}`,
    );
    expect(unchangedSegmentResponse.ok()).toBeTruthy();
    expect((await unchangedSegmentResponse.json()).end_date).toBe(d2);
    const unchangedShiftsResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    const unchangedShift = (await unchangedShiftsResponse.json() as Array<{
      allocation_id: string;
      resource_name: string;
      work_date: string;
    }>).find((row) => row.allocation_id === allocationId);
    expect(unchangedShift?.resource_name).toBe("Alice");
    expect(unchangedShift?.work_date).toBe(d2);

    await dragWithDataTransfer(
      page,
      sourceCell.locator(`.shift-card[data-allocation-id="${allocationId}"]`),
      outsideTarget,
    );
    dropDialog = page.getByRole("dialog", { name: "Choisir l’action du déplacement" });
    await expect(dropDialog).toBeVisible();
    const extendPromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && response.url().includes(`/api/v1/allocations/${encodeURIComponent(allocationId)}/extend-and-move`)
    ));
    await dropDialog.getByRole("button", { name: "Étendre la période et déplacer", exact: true }).click();
    const extended = await extendPromise;
    expect(extended.status(), await extended.text()).toBe(200);
    expect(extended.request().headers()["idempotency-key"]).toBeTruthy();
    expect(typeof extended.request().postDataJSON().expected_planning_version).toBe("number");
    await expect(page.locator(".planning-drag-feedback")).toContainText("Période étendue et quart déplacé");

    const extendedSegmentResponse = await page.request.get(
      `/api/v1/segments/${encodeURIComponent(createdShift!.segment_id)}`,
    );
    expect(extendedSegmentResponse.ok()).toBeTruthy();
    expect((await extendedSegmentResponse.json()).end_date).toBe(d3);
    await expect(
      bobRow.locator(`.planning-drop-day[data-day="${d3}"] .shift-card[data-allocation-id="${allocationId}"]`),
    ).toBeVisible();

    const bobD3Source = bobRow
      .locator(`.planning-drop-day[data-day="${d3}"]`)
      .locator(`.shift-card[data-allocation-id="${allocationId}"]`);
    const aliceD2Target = aliceRow.locator(`.planning-drop-day[data-day="${d2}"]`);
    await dragWithDataTransfer(page, bobD3Source, aliceD2Target);
    dropDialog = page.getByRole("dialog", { name: "Choisir l’action du déplacement" });
    await expect(dropDialog.getByRole("button", { name: "Déplacer", exact: true })).toBeVisible();
    await expect(dropDialog.getByRole("button", { name: "Partager", exact: true })).toBeVisible();
    await expect(dropDialog.getByRole("button", { name: "Dupliquer", exact: true })).toBeVisible();
    const movePromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && response.url().includes(`/api/v1/allocations/${encodeURIComponent(allocationId)}/move`)
    ));
    await dropDialog.getByRole("button", { name: "Déplacer", exact: true }).click();
    expect((await movePromise).status()).toBe(200);
    await expect(page.locator(".planning-drag-feedback")).toContainText("Quart déplacé vers Alice");

    const aliceD2Source = aliceRow
      .locator(`.planning-drop-day[data-day="${d2}"]`)
      .locator(`.shift-card[data-allocation-id="${allocationId}"]`);
    const bobD3Target = bobRow.locator(`.planning-drop-day[data-day="${d3}"]`);
    await dragWithDataTransfer(page, aliceD2Source, bobD3Target);
    dropDialog = page.getByRole("dialog", { name: "Choisir l’action du déplacement" });
    const transfer = dropDialog.locator(".planning-drop-split-hours input");
    await transfer.fill("0.5");
    const splitPromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && response.url().includes(`/api/v1/allocations/${encodeURIComponent(allocationId)}/split`)
    ));
    await dropDialog.getByRole("button", { name: "Partager", exact: true }).click();
    const split = await splitPromise;
    expect(split.status(), await split.text()).toBe(201);
    expect(split.request().headers()["idempotency-key"]).toBeTruthy();

    const afterSplitResponse = await page.request.get(
      `/api/v1/shifts?start=${d1}&end=${d5}`,
    );
    const afterSplitRows = (await afterSplitResponse.json() as Array<{
      allocation_id: string;
      segment_id: string;
      resource_id: string;
      work_date: string;
      hours: number;
    }>).filter((row) => row.segment_id === createdShift!.segment_id);
    const splitTarget = afterSplitRows.find((row) => (
      row.resource_id === "R-BOB"
      && row.work_date === d3
      && Math.abs(row.hours - 0.5) < 0.001
    ));
    expect(splitTarget, "Quart cible du partage DnD introuvable").toBeDefined();

    const bobSplitCard = bobRow
      .locator(`.planning-drop-day[data-day="${d3}"]`)
      .locator(`.shift-card[data-allocation-id="${splitTarget!.allocation_id}"]`);
    const aliceD3Target = aliceRow.locator(`.planning-drop-day[data-day="${d3}"]`);
    await dragWithDataTransfer(page, bobSplitCard, aliceD3Target);
    dropDialog = page.getByRole("dialog", { name: "Choisir l’action du déplacement" });

    const duplicateFirstPromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && response.url().includes(`/api/v1/allocations/${encodeURIComponent(splitTarget!.allocation_id)}/duplicate`)
    ));
    await dropDialog.getByRole("button", { name: "Dupliquer", exact: true }).click();
    const duplicateFirst = await duplicateFirstPromise;
    expect(duplicateFirst.status(), await duplicateFirst.text()).toBe(422);
    const duplicateKey = duplicateFirst.request().headers()["idempotency-key"];
    expect(duplicateKey).toBeTruthy();
    await expect(dropDialog).toContainText("Décision de surallocation requise");

    await dropDialog.getByRole("radio", { name: /Conserver la surallocation comme dérogation/ }).check();
    const duplicateRetryPromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && response.url().includes(`/api/v1/allocations/${encodeURIComponent(splitTarget!.allocation_id)}/duplicate`)
    ));
    await dropDialog.getByRole("button", { name: "Dupliquer", exact: true }).click();
    const duplicateRetry = await duplicateRetryPromise;
    expect(duplicateRetry.status(), await duplicateRetry.text()).toBe(201);
    expect(duplicateRetry.request().headers()["idempotency-key"]).toBe(duplicateKey);
    await expect(page.locator(".planning-drag-feedback")).toContainText("Quart dupliqué vers Alice");

    await closeContext(context);
  });
});


test("REQUEST window proposal never replays the original drag after direct approval", async ({ browser }) => {
  test.setTimeout(120_000);
  const { d1 } = acceptanceDates();
  const cleanWeek = new Date(`${d1}T12:00:00`);
  const sourceDay = localIso(addDays(cleanWeek, 7));
  const targetDay = localIso(addDays(cleanWeek, 8));
  const weekEnd = localIso(addDays(cleanWeek, 13));
  const { context, page } = await openAs(browser, "COORDINATOR");

  const created = await page.request.post("/api/v1/demands", {
    data: {
      project_number: "P-251",
      desired_start: sourceDay,
      desired_end: sourceDay,
      estimated_hours: 2,
      task_code: "210",
      proposed_technician: "Alice",
      description: "Proposition fenêtre DnD #333C",
      submit: true,
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const demandNumber = (await created.json()).demand_number as string;

  const snapshotBeforeApproval = await page.request.get(
    `/api/v1/planning/snapshot?start=${sourceDay}&end=${weekEnd}&scope=global`,
  );
  expect(snapshotBeforeApproval.ok()).toBeTruthy();
  const planningVersion = (await snapshotBeforeApproval.json()).planning_version as number;

  const approved = await page.request.post(
    `/api/v1/demands/${encodeURIComponent(demandNumber)}/approve`,
    {
      data: {
        comment: "Approbation initiale DnD #333C",
        expected_planning_version: planningVersion,
      },
    },
  );
  expect(approved.status(), await approved.text()).toBe(200);

  const shiftsResponse = await page.request.get(
    `/api/v1/shifts?start=${sourceDay}&end=${weekEnd}`,
  );
  expect(shiftsResponse.ok()).toBeTruthy();
  const shift = (await shiftsResponse.json() as Array<{
    allocation_id: string;
    segment_id: string;
    demand_number: string | null;
    resource_name: string;
    work_date: string;
    allocation_type: string | null;
  }>).find((row) => (
    row.demand_number === demandNumber
    && row.allocation_type !== "Hors horaire requis"
  ));
  expect(shift, "Quart REQUEST #333C introuvable après approbation").toBeDefined();
  expect(shift!.resource_name).toBe("Alice");
  expect(shift!.work_date).toBe(sourceDay);

  await navigateMain(page, "Planning opérationnel");
  await page.getByRole("button", { name: /Suivante/ }).click();
  await page.getByRole("button", { name: /Suivante/ }).click();

  const aliceRow = page.locator(".resource-identity").filter({ hasText: "Alice" }).first().locator("..");
  const bobRow = page.locator(".resource-identity").filter({ hasText: "Bob" }).first().locator("..");
  const source = aliceRow
    .locator(`.planning-drop-day[data-day="${sourceDay}"]`)
    .locator(`.shift-card[data-allocation-id="${shift!.allocation_id}"]`);
  const target = bobRow.locator(`.planning-drop-day[data-day="${targetDay}"]`);
  await expect(source).toBeVisible();

  const evaluatePromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().includes(
      `/api/v1/allocations/${encodeURIComponent(shift!.allocation_id)}/evaluate-drop`,
    )
  ));
  await dragWithDataTransfer(page, source, target);
  const evaluated = await evaluatePromise;
  expect(evaluated.status(), await evaluated.text()).toBe(200);
  expect((await evaluated.json()).authorization_decision).toBe(
    "WINDOW_EXTENSION_REAPPROVAL_REQUIRED",
  );
  let dialog = page.getByRole("dialog", { name: "Choisir l’action du déplacement" });
  await expect(dialog).toContainText("Extension hors enveloppe approuvée");
  await expect(
    dialog.getByRole("button", { name: "Soumettre l'extension de période", exact: true }),
  ).toBeVisible();

  const proposalPromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().includes(
      `/api/v1/allocations/${encodeURIComponent(shift!.allocation_id)}/propose-window-extension`,
    )
  ));
  await dialog.getByRole("button", { name: "Soumettre l'extension de période", exact: true }).click();
  const proposal = await proposalPromise;
  expect(proposal.status(), await proposal.text()).toBe(200);
  expect(proposal.request().headers()["idempotency-key"]).toBeTruthy();
  const proposalResult = await proposal.json() as {
    status: string | null;
    reapproval_required: boolean;
  };
  expect(proposalResult.reapproval_required).toBeFalsy();
  expect(proposalResult.status).toBe("En planification");
  await expect(page.locator(".planning-drag-feedback")).toContainText("Extension approuvée");
  await expect(page.locator(".planning-drag-feedback")).toContainText("Aucun quart n’a été déplacé");

  const afterProposalResponse = await page.request.get(
    `/api/v1/shifts?start=${sourceDay}&end=${weekEnd}`,
  );
  const unchangedShift = (await afterProposalResponse.json() as Array<{
    allocation_id: string;
    segment_id: string;
    demand_number: string | null;
    resource_name: string;
    work_date: string;
    allocation_type: string | null;
  }>).find((row) => (
    row.demand_number === demandNumber
    && row.allocation_type !== "Hors horaire requis"
  ));
  expect(unchangedShift, "Quart REQUEST frais introuvable après approbation de l’extension").toBeDefined();
  expect(unchangedShift!.resource_name).toBe("Alice");
  expect(unchangedShift!.work_date).toBe(sourceDay);

  const segmentAfterProposal = await page.request.get(
    `/api/v1/segments/${encodeURIComponent(unchangedShift!.segment_id)}`,
  );
  expect(segmentAfterProposal.ok()).toBeTruthy();
  expect((await segmentAfterProposal.json()).end_date).toBe(targetDay);

  const refreshedSource = aliceRow
    .locator(`.planning-drop-day[data-day="${sourceDay}"]`)
    .locator(`.shift-card[data-allocation-id="${unchangedShift!.allocation_id}"]`);
  await expect(refreshedSource).toBeVisible();
  await dragWithDataTransfer(page, refreshedSource, target);
  dialog = page.getByRole("dialog", { name: "Choisir l’action du déplacement" });
  await expect(dialog.getByRole("button", { name: "Déplacer", exact: true })).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Soumettre l'extension de période", exact: true }),
  ).toHaveCount(0);
  await dialog.getByRole("button", { name: "Annuler", exact: true }).click();

  const afterCancelResponse = await page.request.get(
    `/api/v1/shifts?start=${sourceDay}&end=${weekEnd}`,
  );
  const afterCancel = (await afterCancelResponse.json() as Array<{
    allocation_id: string;
    demand_number: string | null;
    resource_name: string;
    work_date: string;
    allocation_type: string | null;
  }>).find((row) => (
    row.demand_number === demandNumber
    && row.allocation_type !== "Hors horaire requis"
  ));
  expect(afterCancel, "Le nouveau DnD annulé ne doit supprimer aucun quart REQUEST comptabilisé").toBeDefined();
  expect(afterCancel!.resource_name).toBe("Alice");
  expect(afterCancel!.work_date).toBe(sourceDay);

  await closeContext(context);
});


test("coordinator splits and duplicates a shift atomically from React", async ({ browser }) => {
  test.setTimeout(120_000);
  const { d1, d2, d5 } = acceptanceDates();
  const { context, page } = await openAs(browser, "COORDINATOR");

  await navigateMain(page, "Planning opérationnel");
  await page.getByRole("button", { name: /Suivante/ }).click();

  await page.getByRole("button", { name: /Quick Shift/ }).click();
  const quickShift = page.getByRole("dialog", { name: "Créer un Quick Shift" });
  await labelled(quickShift, "Projet", "select").selectOption("P-251");
  await labelled(quickShift, "Technicien", "select").selectOption("Alice");
  await labelled(quickShift, "Date", "input").fill(d2);
  await labelled(quickShift, "Heures", "input").fill("4");
  await labelled(quickShift, "Confirmation", "select").selectOption("Confirmée");
  await labelled(quickShift, "Description", "textarea").fill("Validation partage et duplication #332");
  await labelled(quickShift, "Note", "textarea").fill("Atomic #332");
  await quickShift.getByRole("button", { name: "Créer le Quick Shift" }).click();
  await expect(quickShift).toBeHidden();

  const createdResponse = await page.request.get(
    `/api/v1/shifts?start=${d1}&end=${d5}`,
  );
  expect(createdResponse.ok()).toBeTruthy();
  const createdRows = await createdResponse.json() as Array<{
    allocation_id: string;
    segment_id: string;
    resource_id: string;
    work_date: string;
    hours: number;
    note: string | null;
    locked: boolean;
    source: string;
  }>;
  const sourceShift = createdRows.find((row) => row.note === "Atomic #332");
  expect(sourceShift, "Quart source #332 introuvable").toBeDefined();
  const segmentId = sourceShift!.segment_id;

  const aliceRow = page.locator(".resource-identity").filter({ hasText: "Alice" }).first().locator("..");
  const sourceCard = aliceRow
    .locator(`.planning-drop-day[data-day="${d2}"]`)
    .locator(`.shift-card[data-allocation-id="${sourceShift!.allocation_id}"]`);
  await expect(sourceCard).toBeVisible();
  await sourceCard.click();

  const editor = page.getByRole("dialog", { name: "Modifier le quart" });
  await expect(editor).toBeVisible();
  await editor.getByRole("button", { name: "Partager", exact: true }).click();
  const splitPanel = editor.getByTestId("atomic-split-panel");
  await expect(splitPanel).toBeVisible();
  await labelled(splitPanel, "Ressource du nouveau quart", "select").selectOption("R-BOB");
  await labelled(splitPanel, "Date du nouveau quart", "input").fill(d2);
  await labelled(splitPanel, "Heures à transférer", "input").fill("1.5");

  const splitResponsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().includes(`/api/v1/allocations/${encodeURIComponent(sourceShift!.allocation_id)}/split`)
  ));
  await splitPanel.getByRole("button", { name: "Partager le quart", exact: true }).click();
  const splitResponse = await splitResponsePromise;
  expect(splitResponse.status(), await splitResponse.text()).toBe(201);
  const splitRequest = splitResponse.request();
  expect(splitRequest.headers()["idempotency-key"]).toBeTruthy();
  const splitBody = splitRequest.postDataJSON() as Record<string, unknown>;
  expect(typeof splitBody.expected_planning_version).toBe("number");
  expect(splitBody.resource_id).toBe("R-BOB");
  expect(splitBody.transfer_hours).toBe(1.5);
  await expect(editor).toBeHidden();

  const afterSplitResponse = await page.request.get(
    `/api/v1/shifts?start=${d1}&end=${d5}`,
  );
  expect(afterSplitResponse.ok()).toBeTruthy();
  const afterSplitRows = (await afterSplitResponse.json() as Array<{
    allocation_id: string;
    segment_id: string;
    resource_id: string;
    work_date: string;
    hours: number;
    locked: boolean;
    source: string;
  }>).filter((row) => row.segment_id === segmentId);
  expect(afterSplitRows).toHaveLength(2);
  expect(afterSplitRows.reduce((sum, row) => sum + row.hours, 0)).toBeCloseTo(4, 2);
  expect(afterSplitRows.every((row) => row.locked && row.source === "MANUAL")).toBeTruthy();
  const bobShift = afterSplitRows.find((row) => row.resource_id === "R-BOB");
  expect(bobShift?.hours).toBeCloseTo(1.5, 2);

  const bobRow = page.locator(".resource-identity").filter({ hasText: "Bob" }).first().locator("..");
  const bobCard = bobRow
    .locator(`.planning-drop-day[data-day="${d2}"]`)
    .locator(`.shift-card[data-allocation-id="${bobShift!.allocation_id}"]`);
  await expect(bobCard).toBeVisible();
  await bobCard.click();

  const duplicateEditor = page.getByRole("dialog", { name: "Modifier le quart" });
  await duplicateEditor.getByRole("button", { name: "Dupliquer", exact: true }).click();
  const duplicatePanel = duplicateEditor.getByTestId("atomic-duplicate-panel");
  await expect(duplicatePanel).toBeVisible();

  const firstDuplicatePromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().includes(`/api/v1/allocations/${encodeURIComponent(bobShift!.allocation_id)}/duplicate`)
  ));
  await duplicatePanel.getByRole("button", { name: "Dupliquer le quart", exact: true }).click();
  const firstDuplicate = await firstDuplicatePromise;
  expect(firstDuplicate.status(), await firstDuplicate.text()).toBe(422);
  const firstKey = firstDuplicate.request().headers()["idempotency-key"];
  expect(firstKey).toBeTruthy();
  await expect(duplicateEditor.locator(".overallocation-choice")).toContainText(
    "dépasse les heures prévues",
  );

  const keptDuplicatePromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().includes(`/api/v1/allocations/${encodeURIComponent(bobShift!.allocation_id)}/duplicate`)
  ));
  await duplicateEditor.getByRole("button", { name: /Conserver la dérogation/ }).click();
  const keptDuplicate = await keptDuplicatePromise;
  expect(keptDuplicate.status(), await keptDuplicate.text()).toBe(201);
  expect(keptDuplicate.request().headers()["idempotency-key"]).toBe(firstKey);
  expect(
    (keptDuplicate.request().postDataJSON() as Record<string, unknown>).overallocation_policy,
  ).toBe("KEEP_EXCEPTION");
  await expect(duplicateEditor).toBeHidden();

  const finalResponse = await page.request.get(
    `/api/v1/shifts?start=${d1}&end=${d5}`,
  );
  expect(finalResponse.ok()).toBeTruthy();
  const finalRows = (await finalResponse.json() as Array<{
    segment_id: string;
    resource_id: string;
    hours: number;
    locked: boolean;
    source: string;
    segment_overallocated_hours?: number;
  }>).filter((row) => row.segment_id === segmentId);
  expect(finalRows).toHaveLength(3);
  expect(finalRows.reduce((sum, row) => sum + row.hours, 0)).toBeCloseTo(5.5, 2);
  expect(finalRows.every((row) => row.locked && row.source === "MANUAL")).toBeTruthy();
  expect(finalRows.filter((row) => row.resource_id === "R-BOB")).toHaveLength(2);
  expect(finalRows.some((row) => Number(row.segment_overallocated_hours ?? 0) > 0)).toBeTruthy();

  await closeContext(context);
});


test("multi-line demand editor generates independent RequestLines and materializes them", async ({ browser }) => {
  const { d1, d2 } = acceptanceDates();
  const projectManager = await openAs(browser, "PROJECT_MANAGER");
  await navigateMain(projectManager.page, "Demandes");
  await projectManager.page.getByRole("button", { name: /Nouvelle demande/ }).click();
  const editor = projectManager.page.locator(".demand-editor-form");
  await expect(editor.getByRole("heading", { name: "Nouvelle demande" })).toBeVisible();

  await labelled(editor, "Projet", "select").selectOption("P-251");
  await expect(editor.getByText("Recherche catalogue ERP", { exact: true })).toHaveCount(0);
  const multiTaskSelect = labelled(editor, "Tâche ERP", "select");
  await expect(multiTaskSelect.locator("option", { hasText: "210 — AUTOMATISATION E2E" })).toBeAttached();
  await multiTaskSelect.selectOption("210");
  await labelled(editor, "Début souhaité", "input").fill(d1);
  await labelled(editor, "Fin souhaitée", "input").fill(d2);
  await labelled(editor, "Nombre de ressources simultanées", "input").fill("2");
  await labelled(editor, "Jours actifs souhaités", "input").fill("1");
  await labelled(editor, "Description / contexte de la demande", "textarea").fill(
    "Demande multi-lignes React #288",
  );

  await editor.getByRole("button", { name: "Passer aux lignes multiples" }).click();
  const cards = editor.locator(".request-line-card");
  await expect(cards).toHaveCount(2);
  const firstDateGroup = cards.nth(0).getByTestId("request-line-dates-0");
  await expect(firstDateGroup).toBeVisible();
  const startBox = await labelled(firstDateGroup, "Début", "input").boundingBox();
  const endBox = await labelled(firstDateGroup, "Fin", "input").boundingBox();
  expect(startBox).not.toBeNull();
  expect(endBox).not.toBeNull();
  expect(Math.abs(startBox!.y - endBox!.y)).toBeLessThan(4);
  await expect(labelled(cards.nth(0), "Confirmation", "select")).toBeVisible();
  await expect(labelled(cards.nth(0), "Classe de ressource", "select")).toBeVisible();
  await expect(editor.locator(".request-lines-summary")).toContainText("16");
  await expect(editor.locator(".request-lines-summary")).toContainText("heure(s) humaines projetées");

  const generationCount = editor.getByLabel("Quantité de lignes à générer");
  await generationCount.fill("3");
  await editor.getByRole("button", { name: "Générer les lignes" }).click();
  await expect(cards).toHaveCount(3);
  await cards.nth(2).getByRole("button", { name: "Retirer" }).click();
  await expect(cards).toHaveCount(2);

  await cards.nth(0).getByRole("button", { name: "Dupliquer" }).click();
  await expect(cards).toHaveCount(3);
  await cards.nth(1).getByRole("button", { name: "Retirer" }).click();
  await expect(cards).toHaveCount(2);

  const line1 = cards.nth(0);
  const line2 = cards.nth(1);
  await labelled(line1, "Classe de ressource", "select").selectOption("PROGRAMMEUR");
  await line1.locator('select[aria-label="Compétences requises — ligne 1"]').selectOption(["C-SCADA"]);
  await labelled(line1, "Ressource proposée", "select").selectOption("R-ALICE");
  await labelled(line1, "Description spécifique", "textarea").fill("SCADA en début de fenêtre");

  await labelled(line2, "Classe de ressource", "select").selectOption("PROGRAMMEUR");
  await line2.locator('select[aria-label="Compétences requises — ligne 2"]').selectOption(["C-PLC"]);
  await labelled(line2, "Début", "input").fill(d2);
  await labelled(line2, "Fin", "input").fill(d2);
  await labelled(line2, "Ressource proposée", "select").selectOption("R-BOB");
  await labelled(line2, "Confirmation", "select").selectOption("Tentative");
  await labelled(line2, "Description spécifique", "textarea").fill("PLC en deuxième journée");

  await editor.getByRole("button", { name: "Créer le brouillon" }).click();
  const notice = projectManager.page.locator(".demand-notice");
  await expect(notice).toContainText("créée en brouillon");
  const number = demandNumberFrom(await notice.textContent());

  const detailResponse = await projectManager.page.request.get(
    `/api/v1/demands/${encodeURIComponent(number)}`,
  );
  expect(detailResponse.ok()).toBeTruthy();
  const detail = await detailResponse.json() as {
    line_mode: boolean;
    lines: Array<{
      line_id: string;
      active: boolean;
      required_resource_class: string | null;
      required_competency_ids: string[];
      estimated_hours: number | null;
      estimated_hours_source: string | null;
      proposed_resource_id: string | null;
      confirmation: string;
    }>;
  };
  const activeLines = detail.lines.filter((line) => line.active);
  expect(detail.line_mode).toBeTruthy();
  expect(activeLines).toHaveLength(2);
  expect(activeLines.map((line) => line.required_resource_class)).toEqual([
    "PROGRAMMEUR",
    "PROGRAMMEUR",
  ]);
  expect(activeLines.map((line) => line.required_competency_ids)).toEqual([
    ["C-SCADA"],
    ["C-PLC"],
  ]);
  expect(activeLines.map((line) => line.estimated_hours)).toEqual([8, 8]);
  expect(activeLines.map((line) => line.estimated_hours_source)).toEqual([
    "DEFAULT_8H",
    "DEFAULT_8H",
  ]);
  expect(activeLines.map((line) => line.proposed_resource_id)).toEqual([
    "R-ALICE",
    "R-BOB",
  ]);
  expect(activeLines.map((line) => line.confirmation)).toEqual([
    "Confirmée",
    "Tentative",
  ]);

  await expect(editor.locator(".request-line-card")).toHaveCount(2);
  await expect(labelled(editor.locator(".request-line-card").nth(0), "Heures", "input")).toHaveValue("");

  await periodsSelect(projectManager.page, number);
  const lineSelector = projectManager.page.getByLabel("Ligne de demande");
  await expect(lineSelector).toBeVisible();
  await expect(lineSelector.locator("option")).toHaveCount(2);

  await lineSelector.selectOption(activeLines[0].line_id);
  await expect(projectManager.page.locator(".period-demand-summary")).toContainText("Ligne 1");
  await projectManager.page.getByRole("button", { name: /Période cumulative/ }).click();
  let linePeriod = projectManager.page.locator(".period-card.cumulative").first();
  await labelled(linePeriod, "Début", "input").fill(d1);
  await labelled(linePeriod, "Fin", "input").fill(d1);
  await labelled(linePeriod, "Heures totales", "input").fill("8");
  await expect(labelled(linePeriod, "Ressources simultanées", "input")).toBeDisabled();
  await expect(labelled(linePeriod, "Ressources simultanées", "input")).toHaveValue("1");
  await projectManager.page.getByRole("button", { name: "Enregistrer les périodes" }).click();
  await expect(projectManager.page.locator(".demand-notice").filter({ hasText: "Périodes enregistrées." })).toHaveText("Périodes enregistrées.");

  await lineSelector.selectOption(activeLines[1].line_id);
  await expect(projectManager.page.locator(".period-demand-summary")).toContainText("Ligne 2");
  await expect(projectManager.page.locator(".period-empty")).toBeVisible();
  await projectManager.page.getByRole("button", { name: /Période cumulative/ }).click();
  linePeriod = projectManager.page.locator(".period-card.cumulative").first();
  await labelled(linePeriod, "Début", "input").fill(d2);
  await labelled(linePeriod, "Fin", "input").fill(d2);
  await labelled(linePeriod, "Heures totales", "input").fill("8");
  await expect(labelled(linePeriod, "Ressources simultanées", "input")).toBeDisabled();
  await projectManager.page.getByRole("button", { name: "Enregistrer les périodes" }).click();
  await expect(projectManager.page.locator(".demand-notice").filter({ hasText: "Périodes enregistrées." })).toHaveText("Périodes enregistrées.");

  const firstLinePeriods = await projectManager.page.request.get(
    `/api/v1/demands/${encodeURIComponent(number)}/lines/${encodeURIComponent(activeLines[0].line_id)}/periods`,
  );
  expect(firstLinePeriods.ok()).toBeTruthy();
  expect((await firstLinePeriods.json()) as Array<unknown>).toHaveLength(1);
  const secondLinePeriods = await projectManager.page.request.get(
    `/api/v1/demands/${encodeURIComponent(number)}/lines/${encodeURIComponent(activeLines[1].line_id)}/periods`,
  );
  expect(secondLinePeriods.ok()).toBeTruthy();
  expect((await secondLinePeriods.json()) as Array<unknown>).toHaveLength(1);

  await workflowSelect(projectManager.page, number);
  await projectManager.page.getByRole("button", { name: "Soumettre", exact: true }).click();
  await expect(projectManager.page.locator(".demand-notice").filter({ hasText: "soumise pour approbation" })).toContainText("soumise pour approbation");
  await closeContext(projectManager.context);

  const coordinator = await openAs(browser, "COORDINATOR");
  await navigateMain(coordinator.page, "Demandes");
  await workflowSelect(coordinator.page, number);
  await coordinator.page.getByLabel(/Commentaire d’approbation/).fill("Approbation multi-lignes #288");
  await coordinator.page.getByRole("button", { name: "Approuver", exact: true }).click();
  await expect(coordinator.page.locator(".demand-notice").filter({ hasText: "Demande approuvée" })).toContainText("Demande approuvée");

  const segmentsResponse = await coordinator.page.request.get("/api/v1/segments?include_cancelled=false");
  expect(segmentsResponse.ok()).toBeTruthy();
  const segments = await segmentsResponse.json() as Array<{
    demand_number: string | null;
    resource_name: string | null;
    planned_hours: number;
  }>;
  const materialized = segments.filter((row) => row.demand_number === number);
  expect(materialized).toHaveLength(2);
  expect(materialized.map((row) => row.planned_hours).sort((a, b) => a - b)).toEqual([8, 8]);
  expect(new Set(materialized.map((row) => row.resource_name))).toEqual(new Set(["Alice", "Bob"]));

  await closeContext(coordinator.context);
});



test("asset UX creates Nacelle #63 and links only real operator allocations on human shifts", async ({ browser }) => {
  test.setTimeout(240_000);
  const { d1, d2, d3, d4, d5 } = acceptanceDates();
  let demandNumber = "";
  let assetTypeId = "";
  let lift63Id = "";
  let lift64Id = "";
  let lift65Id = "";

  const catalogManager = await openAs(browser, "COORDINATOR");
  await navigateMain(catalogManager.page, "Ressources");
  const catalogPanel = catalogManager.page.locator(".asset-catalog-card");
  await expect(catalogPanel.getByRole("heading", { name: "Catalogue des actifs" })).toBeVisible();

  await catalogPanel.getByRole("button", { name: "+ Type d’actif" }).click();
  const typeEditor = catalogPanel.locator(".asset-type-editor");
  await labelled(typeEditor, "Code", "input").fill("LIFT496");
  await labelled(typeEditor, "Libellé", "input").fill("Nacelle");
  await labelled(typeEditor, "Catégorie", "select").selectOption("EQUIPMENT");
  await typeEditor.getByLabel("Compétences / permis requis", { exact: true }).selectOption(["C-SCADA"]);
  await typeEditor.getByRole("button", { name: "Enregistrer le type" }).click();
  await expect(catalogPanel.locator(".asset-catalog-notice")).toContainText("Type d’actif créé");

  const typeRow = catalogPanel.locator(".asset-type-row").filter({ hasText: "LIFT496" }).first();
  await expect(typeRow).toContainText("Nacelle");
  await typeRow.click();
  await catalogPanel.getByRole("button", { name: "+ Unité" }).click();
  const unitEditor = catalogPanel.locator(".asset-unit-editor");
  await labelled(unitEditor, "Code", "input").fill("NAC-63");
  await labelled(unitEditor, "Libellé", "input").fill("Nacelle #63");
  await unitEditor.getByRole("button", { name: "Enregistrer l’unité" }).click();
  await expect(catalogPanel.locator(".asset-catalog-notice")).toContainText("Unité physique créée");
  await expect(catalogPanel.locator(".asset-unit-row").filter({ hasText: "Nacelle #63" })).toBeVisible();

  let catalogResponse = await catalogManager.page.request.get("/api/v1/assets/catalog");
  expect(catalogResponse.ok()).toBeTruthy();
  let catalog = await catalogResponse.json() as {
    types: Array<{ id: string; code: string }>;
    assets: Array<{ id: string; code: string }>;
    planning_version: number;
  };
  assetTypeId = catalog.types.find((row) => row.code === "LIFT496")?.id ?? "";
  lift63Id = catalog.assets.find((row) => row.code === "NAC-63")?.id ?? "";
  expect(assetTypeId).not.toBe("");
  expect(lift63Id).not.toBe("");

  for (const unit of [
    { code: "NAC-64", label: "Nacelle #64" },
    { code: "NAC-65", label: "Nacelle #65" },
  ]) {
    const created = await catalogManager.page.request.post("/api/v1/assets", {
      data: {
        code: unit.code,
        label: unit.label,
        asset_type_id: assetTypeId,
      },
    });
    expect(created.status(), await created.text()).toBe(201);
  }
  catalogResponse = await catalogManager.page.request.get("/api/v1/assets/catalog");
  catalog = await catalogResponse.json() as typeof catalog;
  lift64Id = catalog.assets.find((row) => row.code === "NAC-64")?.id ?? "";
  lift65Id = catalog.assets.find((row) => row.code === "NAC-65")?.id ?? "";
  expect(lift64Id).not.toBe("");
  expect(lift65Id).not.toBe("");

  await catalogPanel.locator(".asset-unit-row").filter({ hasText: "Nacelle #63" }).first().click();
  const catalogUnavailability = catalogPanel.locator(".asset-catalog-unavailability-form");
  await labelled(catalogUnavailability, "Début", "input").fill(d5);
  await labelled(catalogUnavailability, "Fin", "input").fill(d5);
  await labelled(catalogUnavailability, "Raison", "input").fill("Entretien catalogue #496");
  await catalogUnavailability.getByRole("button", { name: "Ajouter l’indisponibilité" }).click();
  const catalogMaintenance = catalogPanel
    .locator(".asset-catalog-unavailability-list article")
    .filter({ hasText: "Entretien catalogue #496" })
    .first();
  await expect(catalogMaintenance).toContainText(d5);
  await catalogMaintenance.getByRole("button", { name: "Retirer" }).click();
  await expect(
    catalogPanel.locator(".asset-catalog-unavailability-list article").filter({ hasText: "Entretien catalogue #496" }),
  ).toHaveCount(0);
  await closeContext(catalogManager.context);

  const projectManager = await openAs(browser, "PROJECT_MANAGER");
  await navigateMain(projectManager.page, "Demandes");
  await projectManager.page.getByRole("button", { name: /Nouvelle demande/ }).click();
  let editor = projectManager.page.locator(".demand-editor-form");
  await labelled(editor, "Projet", "select").selectOption("P-251");
  await expect(editor.getByText("Recherche catalogue ERP", { exact: true })).toHaveCount(0);
  const mixedTaskSelect = labelled(editor, "Tâche ERP", "select");
  await expect(mixedTaskSelect.locator("option", { hasText: "210 — AUTOMATISATION E2E" })).toBeAttached();
  await mixedTaskSelect.selectOption("210");
  await labelled(editor, "Description / contexte de la demande", "textarea").fill(
    "Demande mixte main-d’œuvre + nacelles #496",
  );
  await editor.getByRole("button", { name: "Passer aux lignes multiples" }).click();

  let cards = editor.locator(".request-line-card");
  await expect(cards).toHaveCount(1);
  let assetLine = cards.nth(0);
  await assetLine.getByLabel("Type de besoin — ligne 1").selectOption("ASSET");
  await labelled(assetLine, "Début", "input").fill(d1);
  await labelled(assetLine, "Fin", "input").fill(d2);
  await assetLine.getByLabel("Type d’actif — ligne 1").selectOption(assetTypeId);
  await assetLine.getByLabel("Unité proposée — ligne 1").selectOption(lift63Id);
  await labelled(assetLine, "Budget d’usage (h, optionnel)", "input").fill("4");
  await labelled(assetLine, "Description spécifique", "textarea").fill("Nacelle #63 proposée seulement");

  await editor.getByRole("button", { name: "+ Ajouter une ligne" }).click();
  cards = editor.locator(".request-line-card");
  await expect(cards).toHaveCount(2);
  const secondAssetLine = cards.nth(1);
  await secondAssetLine.getByLabel("Type de besoin — ligne 2").selectOption("ASSET");
  await labelled(secondAssetLine, "Début", "input").fill(d1);
  await labelled(secondAssetLine, "Fin", "input").fill(d2);
  await secondAssetLine.getByLabel("Type d’actif — ligne 2").selectOption(assetTypeId);
  await secondAssetLine.getByLabel("Unité proposée — ligne 2").selectOption(lift64Id);
  await labelled(secondAssetLine, "Budget d’usage (h, optionnel)", "input").fill("2");
  await labelled(secondAssetLine, "Description spécifique", "textarea").fill("Nacelle #64 proposée seulement");

  await editor.getByRole("button", { name: "+ Ajouter une ligne" }).click();
  cards = editor.locator(".request-line-card");
  await expect(cards).toHaveCount(3);
  const aliceLine = cards.nth(2);
  await labelled(aliceLine, "Début", "input").fill(d1);
  await labelled(aliceLine, "Fin", "input").fill(d2);
  await labelled(aliceLine, "Classe de ressource", "select").selectOption("PROGRAMMEUR");
  await labelled(aliceLine, "Ressource proposée", "select").selectOption("R-ALICE");
  await labelled(aliceLine, "Heures", "input").fill("8");
  await labelled(aliceLine, "Description spécifique", "textarea").fill("Support Alice pour les nacelles");

  await editor.getByRole("button", { name: "+ Ajouter une ligne" }).click();
  cards = editor.locator(".request-line-card");
  await expect(cards).toHaveCount(4);
  const bobLine = cards.nth(3);
  await labelled(bobLine, "Début", "input").fill(d1);
  await labelled(bobLine, "Fin", "input").fill(d2);
  await labelled(bobLine, "Classe de ressource", "select").selectOption("PROGRAMMEUR");
  await labelled(bobLine, "Ressource proposée", "select").selectOption("R-BOB");
  await labelled(bobLine, "Heures", "input").fill("8");
  await labelled(bobLine, "Description spécifique", "textarea").fill("Support Bob sans actif associé");

  const summary = editor.locator(".request-lines-summary");
  await expect(summary).toContainText("2ligne(s) main-d’œuvre");
  await expect(summary).toContainText("2ligne(s) actif");
  await expect(summary).toContainText("16heure(s) humaines projetées");

  await editor.getByRole("button", { name: "Créer le brouillon" }).click();
  const createdNotice = projectManager.page.locator(".demand-notice");
  await expect(createdNotice).toContainText("créée en brouillon");
  demandNumber = demandNumberFrom(await createdNotice.textContent());

  const draftResponse = await projectManager.page.request.get(
    "/api/v1/demands/" + encodeURIComponent(demandNumber),
  );
  expect(draftResponse.ok()).toBeTruthy();
  const draft = await draftResponse.json() as {
    lines: Array<{
      line_id: string;
      position: number;
      kind: "WORKFORCE" | "ASSET";
      asset_type_id: string | null;
      proposed_asset_id: string | null;
      proposed_resource_id: string | null;
      estimated_hours: number | null;
    }>;
  };
  const activeAssets = draft.lines.filter((line) => line.kind === "ASSET");
  const activeWorkforce = draft.lines.filter((line) => line.kind === "WORKFORCE");
  expect(activeAssets).toHaveLength(2);
  expect(new Set(activeAssets.map((line) => line.proposed_asset_id))).toEqual(new Set([lift63Id, lift64Id]));
  expect(activeAssets.every((line) => line.asset_type_id === assetTypeId)).toBeTruthy();
  expect(activeWorkforce).toHaveLength(2);
  expect(new Set(activeWorkforce.map((line) => line.proposed_resource_id))).toEqual(new Set(["R-ALICE", "R-BOB"]));
  const extendedAssetLine = [...activeAssets].sort((left, right) => left.position - right.position)[0];
  expect(extendedAssetLine?.line_id).toBeTruthy();
  const extendedAssetLineId = extendedAssetLine.line_id;

  await workflowSelect(projectManager.page, demandNumber);
  await projectManager.page.getByRole("button", { name: "Soumettre", exact: true }).click();
  await expect(projectManager.page.locator(".demand-notice").filter({ hasText: "soumise pour approbation" })).toContainText(
    "soumise pour approbation",
  );
  await closeContext(projectManager.context);

  const coordinator = await openAs(browser, "COORDINATOR");
  await navigateMain(coordinator.page, "Demandes");
  await workflowSelect(coordinator.page, demandNumber);
  await coordinator.page.getByLabel(/Commentaire d’approbation/).fill("Approbation demande mixte #496");
  await coordinator.page.getByRole("button", { name: "Approuver", exact: true }).click();
  await expect(coordinator.page.locator(".demand-notice").filter({ hasText: "Demande approuvée" })).toContainText(
    "Demande approuvée",
  );

  const materializedAssets = coordinator.page.locator(".asset-detail-row").filter({ hasText: "LIFT496 — Nacelle" });
  await expect(materializedAssets).toHaveCount(2);
  await expect(materializedAssets.nth(0)).toContainText("À réserver");
  await expect(materializedAssets.nth(1)).toContainText("À réserver");
  await expect(materializedAssets.nth(0)).not.toContainText("Nacelle #63");
  await expect(materializedAssets.nth(1)).not.toContainText("Nacelle #64");

  await navigateMain(coordinator.page, "Planning opérationnel");
  await coordinator.page.getByRole("button", { name: /Suivante/ }).click();
  const assetPanel = coordinator.page.locator(".asset-planning-panel");
  await expect(assetPanel.getByRole("heading", { name: "Actifs et réservations" })).toBeVisible();
  const orderedPlanningPanels = await coordinator.page
    .locator(".planning-layout, .asset-planning-panel")
    .evaluateAll((nodes) => nodes.map((node) => (
      node.classList.contains("planning-layout") ? "human" : "assets"
    )));
  expect(orderedPlanningPanels.slice(0, 2)).toEqual(["human", "assets"]);
  let demandRequirements = assetPanel.locator(".asset-requirement-card").filter({ hasText: demandNumber });
  await expect(demandRequirements).toHaveCount(2);
  await expect(demandRequirements.nth(0)).toContainText("À réserver");
  await expect(demandRequirements.nth(1)).toContainText("À réserver");

  const requirementSnapshotResponse = await coordinator.page.request.get(
    "/api/v1/planning/snapshot?start=" + d1 + "&end=" + d5 + "&scope=global",
  );
  expect(requirementSnapshotResponse.ok()).toBeTruthy();
  const requirementSnapshot = await requirementSnapshotResponse.json() as {
    asset_requirements: Array<{
      requirement_id: string;
      demand_number: string;
      source_request_line_id: string;
    }>;
  };
  const extendedRequirementId = requirementSnapshot.asset_requirements.find(
    (row) => row.demand_number === demandNumber && row.source_request_line_id === extendedAssetLineId,
  )?.requirement_id ?? "";
  expect(extendedRequirementId).not.toBe("");

  const aliceRow = coordinator.page.locator(".resource-row").filter({ hasText: "Alice" }).first();
  const bobRow = coordinator.page.locator(".resource-row").filter({ hasText: "Bob" }).first();
  let aliceShift = aliceRow.locator(".shift-card").filter({ hasText: demandNumber }).first();
  let bobShift = bobRow.locator(".shift-card").filter({ hasText: demandNumber }).first();
  await expect(aliceShift).toBeVisible();
  await expect(bobShift).toBeVisible();
  await expect(aliceShift).not.toContainText("Nacelle #63");
  await expect(bobShift).not.toContainText("Nacelle #63");

  catalogResponse = await coordinator.page.request.get("/api/v1/assets/catalog");
  expect(catalogResponse.ok()).toBeTruthy();
  catalog = await catalogResponse.json() as typeof catalog;
  const bumpVersion = await coordinator.page.request.post("/api/v1/assets/A-LIFT-2/unavailability", {
    data: {
      start_date: d5,
      end_date: d5,
      reason: "Bump version E2E",
      expected_planning_version: catalog.planning_version,
    },
  });
  expect(bumpVersion.status(), await bumpVersion.text()).toBe(201);

  let requirementCard = assetPanel.locator(
    `.asset-requirement-card[data-requirement-id="${extendedRequirementId}"]`,
  );
  await expect(requirementCard).toBeVisible();
  await requirementCard.getByRole("combobox").first().selectOption(lift63Id);
  await requirementCard.getByRole("button", { name: "Réserver cette unité" }).click();
  await expect(assetPanel.locator(".asset-planning-feedback")).toContainText(
    "Le planning a changé depuis l’ouverture de cette vue",
  );

  demandRequirements = assetPanel.locator(".asset-requirement-card").filter({ hasText: demandNumber });
  requirementCard = assetPanel.locator(
    `.asset-requirement-card[data-requirement-id="${extendedRequirementId}"]`,
  );
  await expect(requirementCard).toBeVisible();
  await requirementCard.getByRole("combobox").first().selectOption(lift63Id);
  await requirementCard.getByRole("button", { name: "Réserver cette unité" }).click();
  await expect(assetPanel.locator(".asset-planning-feedback")).toContainText("Réservation enregistrée");

  requirementCard = assetPanel.locator(".asset-requirement-card").filter({
    has: coordinator.page.locator(".asset-current-allocation strong").filter({ hasText: "Nacelle #63" }),
  }).first();
  await expect(requirementCard).toContainText("décision manuelle verrouillée");
  let operatorSelect = requirementCard.getByLabel(/Opérateur qualifiant/);
  await expect(operatorSelect.locator('option[value="R-ALICE"]')).toBeAttached();
  await operatorSelect.selectOption("R-ALICE");
  await requirementCard.getByRole("button", { name: "Enregistrer l’opérateur" }).click();
  await expect(assetPanel.locator(".asset-planning-feedback")).toContainText("Opérateur qualifiant enregistré");

  demandRequirements = assetPanel.locator(".asset-requirement-card").filter({ hasText: demandNumber });
  const secondRequirement = demandRequirements.filter({ hasText: "À réserver" }).first();
  await secondRequirement.getByRole("combobox").first().selectOption(lift64Id);
  await secondRequirement.getByRole("button", { name: "Réserver cette unité" }).click();
  await expect(assetPanel.locator(".asset-planning-feedback")).toContainText("Réservation enregistrée");

  const secondAllocated = assetPanel.locator(".asset-requirement-card").filter({
    has: coordinator.page.locator(".asset-current-allocation strong").filter({ hasText: "Nacelle #64" }),
  }).first();
  operatorSelect = secondAllocated.getByLabel(/Opérateur qualifiant/);
  await expect(operatorSelect.locator('option[value="R-ALICE"]')).toBeAttached();
  await operatorSelect.selectOption("R-ALICE");
  await secondAllocated.getByRole("button", { name: "Enregistrer l’opérateur" }).click();
  await expect(assetPanel.locator(".asset-planning-feedback")).toContainText("Opérateur qualifiant enregistré");

  aliceShift = aliceRow.locator(".shift-card").filter({ hasText: demandNumber }).first();
  bobShift = bobRow.locator(".shift-card").filter({ hasText: demandNumber }).first();
  await expect(aliceShift).toContainText("Nacelle #63");
  await expect(aliceShift).toContainText("Nacelle #64");
  await expect(bobShift).not.toContainText("Nacelle #63");
  await expect(bobShift).not.toContainText("Nacelle #64");

  const unavailabilityForm = assetPanel.locator(".asset-unavailability-form");
  await labelled(unavailabilityForm, "Actif", "select").selectOption("A-LIFT-2");
  await labelled(unavailabilityForm, "Début", "input").fill(d4);
  await labelled(unavailabilityForm, "Fin", "input").fill(d4);
  await labelled(unavailabilityForm, "Raison", "input").fill("Entretien E2E");
  await unavailabilityForm.getByRole("button", { name: "Ajouter l’indisponibilité" }).click();
  const maintenance = assetPanel.locator(".asset-unavailability-list article").filter({ hasText: "Entretien E2E" }).first();
  await expect(maintenance).toContainText("NAC-02 — Nacelle 02");
  await maintenance.getByRole("button", { name: "Retirer" }).click();
  await expect(assetPanel.locator(".asset-unavailability-list article").filter({ hasText: "Entretien E2E" })).toHaveCount(0);

  const conflictCreated = await coordinator.page.request.post("/api/v1/demands", {
    data: {
      project_number: "P-251",
      priority: "Normale",
      description: "Deuxième besoin d’actif en conflit #496",
      lines: [{
        position: 0,
        kind: "ASSET",
        required_resource_class: null,
        required_competency_ids: [],
        desired_start: d1,
        desired_end: d2,
        desired_active_days: null,
        estimated_hours: null,
        work_package_ref: null,
        task_code: "210",
        proposed_resource_id: null,
        asset_type_id: assetTypeId,
        proposed_asset_id: null,
        confirmation: "Confirmée",
        description: "Conflit exclusif attendu",
      }],
      submit: true,
    },
  });
  expect(conflictCreated.status(), await conflictCreated.text()).toBe(201);
  const conflictNumber = (await conflictCreated.json()).demand_number as string;
  const approvalSnapshot = await coordinator.page.request.get(
    "/api/v1/planning/snapshot?start=" + d1 + "&end=" + d5 + "&scope=global",
  );
  expect(approvalSnapshot.ok()).toBeTruthy();
  const approvalVersion = (await approvalSnapshot.json()).planning_version as number;
  const conflictApproved = await coordinator.page.request.post(
    "/api/v1/demands/" + encodeURIComponent(conflictNumber) + "/approve",
    { data: { comment: "Approbation conflit actif #496", expected_planning_version: approvalVersion } },
  );
  expect(conflictApproved.status(), await conflictApproved.text()).toBe(200);

  await coordinator.page.reload();
  await navigateMain(coordinator.page, "Planning opérationnel");
  await coordinator.page.getByRole("button", { name: /Suivante/ }).click();
  const refreshedAssetPanel = coordinator.page.locator(".asset-planning-panel");
  let conflictCard = refreshedAssetPanel.locator(".asset-requirement-card").filter({ hasText: conflictNumber }).first();
  await expect(conflictCard).toBeVisible();
  await conflictCard.getByRole("combobox").first().selectOption(lift63Id);
  await conflictCard.getByRole("button", { name: "Réserver cette unité" }).click();
  await expect(refreshedAssetPanel.locator(".asset-planning-feedback")).toContainText("Actif déjà réservé");
  await expect(refreshedAssetPanel.locator(".asset-planning-feedback")).toContainText("asset_double_booking");

  conflictCard = refreshedAssetPanel.locator(".asset-requirement-card").filter({ hasText: conflictNumber }).first();
  await conflictCard.getByRole("combobox").first().selectOption(lift65Id);
  await conflictCard.getByRole("button", { name: "Réserver cette unité" }).click();
  await expect(refreshedAssetPanel.locator(".asset-planning-feedback")).toContainText("Réservation enregistrée");
  await expect(
    refreshedAssetPanel.locator(".asset-requirement-card").filter({ hasText: conflictNumber }).first(),
  ).toContainText("Nacelle #65");
  await closeContext(coordinator.context);

  const editorContext = await openAs(browser, "PROJECT_MANAGER");
  await navigateMain(editorContext.page, "Demandes");
  const demandCard = editorContext.page.locator(".demand-card").filter({ hasText: demandNumber }).first();
  await demandCard.click();
  editor = editorContext.page.locator(".demand-editor-form");
  await expect(editor.getByRole("heading", { name: demandNumber })).toBeVisible();
  assetLine = editor.locator(".request-line-card").nth(0);
  await expect(assetLine.getByLabel("Type de besoin — ligne 1")).toHaveValue("ASSET");
  await labelled(assetLine, "Fin", "input").fill(d3);
  await editor.getByRole("button", { name: "Enregistrer les modifications" }).click();
  await expect(editorContext.page.locator(".demand-notice")).toContainText("doit être approuvée de nouveau");
  await closeContext(editorContext.context);

  const reapprover = await openAs(browser, "COORDINATOR");
  await navigateMain(reapprover.page, "Demandes");
  await workflowSelect(reapprover.page, demandNumber);
  await reapprover.page.getByLabel(/Commentaire d’approbation/).fill("Réapprobation actif étendu #496");
  await reapprover.page.getByRole("button", { name: "Approuver", exact: true }).click();
  await expect(reapprover.page.locator(".demand-notice").filter({ hasText: "Demande approuvée" })).toContainText(
    "Demande approuvée",
  );
  const preserved63 = reapprover.page.locator(".asset-detail-row").filter({ hasText: "Nacelle #63" }).first();
  const preserved64 = reapprover.page.locator(".asset-detail-row").filter({ hasText: "Nacelle #64" }).first();
  await expect(preserved63).toContainText(d1 + " → " + d3);
  await expect(preserved63).toContainText("NAC-63 · verrouillée");
  await expect(preserved64).toContainText("NAC-64 · verrouillée");
  await closeContext(reapprover.context);
});

test("draft demand exposes primary submit and cancel actions and direct cancel succeeds", async ({ browser }) => {
  const { d1 } = acceptanceDates();
  const requester = await openAs(browser, "PROJECT_MANAGER");
  const editor = await createDemand(requester.page, {
    start: d1,
    end: d1,
    hours: "8",
    activeDays: "1",
    description: "Demande brouillon annulation directe #506",
  });
  await editor.getByRole("button", { name: "Créer le brouillon" }).click();
  const createdNotice = requester.page.locator(".demand-notice");
  await expect(createdNotice).toContainText("créée en brouillon");
  const number = demandNumberFrom(await createdNotice.textContent());

  const detail = requester.page.locator(`.demand-detail-context[data-demand-number="${number}"]`);
  const primaryActions = detail.getByTestId("primary-demand-actions");
  await expect(primaryActions).toBeVisible();
  const submit = primaryActions.getByRole("button", { name: "Soumettre", exact: true });
  const cancel = primaryActions.getByRole("button", { name: "Annuler la demande", exact: true });
  await expect(submit).toBeVisible();
  await expect(submit).toBeEnabled();
  await expect(cancel).toBeVisible();
  await expect(cancel).toBeEnabled();

  requester.page.once("dialog", (dialog) => dialog.accept());
  await cancel.click();
  await expect(detail.locator(".demand-detail-statuses")).toContainText("Annulée");
  await expect(detail.locator(".error-panel")).toHaveCount(0);
  await expect(detail.getByTestId("primary-demand-actions")).toHaveCount(0);
  await closeContext(requester.context);
});

test("materialized demand cancellation is requested, reviewed, rejected or accepted through React", async ({ browser }) => {
  test.setTimeout(180_000);
  const { d5 } = acceptanceDates();

  const requester = await openAs(browser, "PROJECT_MANAGER");
  const editor = await createDemand(requester.page, {
    start: d5,
    end: d5,
    hours: "8",
    activeDays: "1",
    description: "Demande dédiée annulation matérialisée #399D",
    proposedResource: "Alice",
  });
  await editor.getByRole("button", { name: "Créer le brouillon" }).click();
  const createdNotice = requester.page.locator(".demand-notice");
  await expect(createdNotice).toContainText("créée en brouillon");
  const cancellationDemand = demandNumberFrom(await createdNotice.textContent());

  await periodsSelect(requester.page, cancellationDemand);
  await requester.page.getByRole("button", { name: /Période cumulative/ }).click();
  const materializedPeriod = requester.page.locator(".period-card.cumulative").first();
  await labelled(materializedPeriod, "Début", "input").fill(d5);
  await labelled(materializedPeriod, "Fin", "input").fill(d5);
  await labelled(materializedPeriod, "Heures totales", "input").fill("8");
  await labelled(materializedPeriod, "Ressources simultanées", "input").fill("1");
  await labelled(materializedPeriod, "Jours actifs souhaités", "input").fill("1");
  await labelled(materializedPeriod, "Confirmation", "select").selectOption("Confirmée");
  await labelled(materializedPeriod, "Ressource proposée", "select").selectOption("Alice");
  await requester.page.getByRole("button", { name: "Enregistrer les périodes" }).click();
  await expect(
    requester.page.locator(".demand-notice").filter({ hasText: "Périodes enregistrées." }).first(),
  ).toHaveText("Périodes enregistrées.");

  await workflowSelect(requester.page, cancellationDemand);
  await requester.page.getByRole("button", { name: "Soumettre", exact: true }).click();
  await expect(requester.page.locator(".demand-notice").filter({ hasText: "soumise pour approbation" }).first()).toContainText("soumise pour approbation");

  const approver = await openAs(browser, "COORDINATOR");
  await workflowSelect(approver.page, cancellationDemand);
  await approver.page.getByLabel(/Commentaire d’approbation/).fill("Matérialiser le plan #399D");
  await approver.page.getByRole("button", { name: "Approuver", exact: true }).click();
  await expect(approver.page.locator(".demand-notice").filter({ hasText: "Demande approuvée" }).first()).toContainText("Demande approuvée");
  await navigateMain(approver.page, "Planning opérationnel");
  await approver.page.getByRole("button", { name: /Suivante/ }).click();
  await approver.page.getByLabel("Recherche").fill(cancellationDemand);
  await expect(approver.page.locator(".shift-card")).not.toHaveCount(0);
  await closeContext(approver.context);

  await requester.page.reload();
  await workflowSelect(requester.page, cancellationDemand);
  await expect(requester.page.getByRole("button", { name: "Annuler la demande", exact: true })).toHaveCount(0);
  const requestCancellation = requester.page.getByRole("button", { name: "Demander l’annulation", exact: true });
  await expect(requestCancellation).toBeDisabled();

  const cancellationReason = requester.page.getByLabel("Raison de la demande d’annulation (requise)");
  await cancellationReason.fill("Mandat retiré par le client");
  const requestEditor = requester.page.locator(".demand-editor-form");
  const description = labelled(requestEditor, "Description / contexte de la demande", "textarea");
  await description.fill("Modification locale non enregistrée avant annulation");
  await expect(requestCancellation).toBeDisabled();
  await expect(
    requester.page.getByText("Enregistre les modifications avant de poursuivre.", { exact: true }),
  ).toBeVisible();

  await requester.page.reload();
  await workflowSelect(requester.page, cancellationDemand);
  await requester.page.getByLabel("Raison de la demande d’annulation (requise)").fill("Mandat retiré par le client");

  const concurrentEditor = await openAs(browser, "PROJECT_MANAGER");
  await openDemandDetail(concurrentEditor.page, cancellationDemand);
  const concurrentForm = concurrentEditor.page.locator(".demand-editor-form");
  await labelled(concurrentForm, "Description / contexte de la demande", "textarea").fill(
    "Modification concurrente #399D",
  );
  await concurrentForm.getByRole("button", { name: "Enregistrer les modifications" }).click();
  await expect(concurrentEditor.page.locator(".demand-notice").filter({ hasText: "Modification enregistrée" }).first()).toContainText("Modification enregistrée");
  await closeContext(concurrentEditor.context);

  await requester.page.getByRole("button", { name: "Demander l’annulation", exact: true }).click();
  await expect(requester.page.locator(".error-panel")).toContainText("demand_version_conflict");

  await requester.page.reload();
  await workflowSelect(requester.page, cancellationDemand);
  await requester.page.getByLabel("Raison de la demande d’annulation (requise)").fill("Mandat retiré par le client");
  await requester.page.getByRole("button", { name: "Demander l’annulation", exact: true }).click();
  await expect(requester.page.getByTestId("cancellation-pending-state")).toContainText("Annulation demandée");
  await expect(requester.page.getByTestId("detail-cancellation-pending")).toContainText("Annulation demandée");
  await expect(
    requester.page.locator(".demand-card").filter({ hasText: cancellationDemand }).getByTestId("demand-cancellation-pending"),
  ).toContainText("Annulation demandée");
  await expect(requester.page.getByRole("button", { name: "Demander l’annulation", exact: true })).toHaveCount(0);
  await expect(requester.page.getByRole("button", { name: "Annuler la demande", exact: true })).toHaveCount(0);

  const coordinator = await openAs(browser, "COORDINATOR");
  await workflowSelect(coordinator.page, cancellationDemand);
  await expect(coordinator.page.getByTestId("cancellation-pending-state")).toBeVisible();
  await coordinator.page.getByRole("button", { name: "Traiter l’annulation", exact: true }).click();
  const review = coordinator.page.getByTestId("cancellation-review");
  await expect(review).toContainText("Planning qui sera libéré");
  await expect(review).toContainText("Plan humain");
  await coordinator.page.getByLabel("Commentaire de résolution (requis)").fill("Plan encore requis cette semaine");
  await coordinator.page.getByRole("button", { name: "Refuser", exact: true }).click();
  await expect(coordinator.page.locator(".demand-notice").filter({ hasText: "planning actif est conservé" }).first()).toContainText("planning actif est conservé");

  await navigateMain(coordinator.page, "Planning opérationnel");
  await coordinator.page.getByRole("button", { name: /Suivante/ }).click();
  await coordinator.page.getByLabel("Recherche").fill(cancellationDemand);
  await expect(coordinator.page.locator(".shift-card")).not.toHaveCount(0);

  await requester.page.reload();
  await workflowSelect(requester.page, cancellationDemand);
  await requester.page.getByLabel("Raison de la demande d’annulation (requise)").fill("Annulation confirmée par le client");
  await requester.page.getByRole("button", { name: "Demander l’annulation", exact: true }).click();
  await expect(requester.page.getByTestId("cancellation-pending-state")).toBeVisible();

  await navigateMain(coordinator.page, "Demandes");
  await workflowSelect(coordinator.page, cancellationDemand);
  await coordinator.page.getByRole("button", { name: "Traiter l’annulation", exact: true }).click();
  await expect(coordinator.page.getByTestId("cancellation-review")).toContainText("Planning qui sera libéré");
  await coordinator.page.getByLabel("Commentaire de résolution (requis)").fill("Annulation approuvée #399D");
  await coordinator.page.getByRole("button", { name: "Annuler la demande et libérer le planning", exact: true }).click();
  await expect(coordinator.page.locator(".demand-notice").filter({ hasText: "Planning libéré" }).first()).toContainText("Planning libéré");
  await expect(coordinator.page.locator(".demand-detail-statuses")).toContainText("Annulée");
  await expect(coordinator.page.getByTestId("detail-cancellation-pending")).toHaveCount(0);

  await navigateMain(coordinator.page, "Planning opérationnel");
  await coordinator.page.getByRole("button", { name: /Suivante/ }).click();
  await coordinator.page.getByLabel("Recherche").fill(cancellationDemand);
  await expect(coordinator.page.locator(".shift-card")).toHaveCount(0);

  await closeContext(coordinator.context);
  await closeContext(requester.context);
});

test("development identity selector switches real local users and technician schedules", async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
  });
  const page = await context.newPage();
  await page.goto("/");

  const selector = page.getByLabel("Identité de test");
  await expect(selector).toBeVisible();
  await expect(page.locator(".sidebar-footer")).toContainText("Administrateur bootstrap E2E");

  await selectOptionContaining(selector, "Technicien Démo A");
  await expect(page.locator(".sidebar-footer")).toContainText("Technicien Démo A");
  await expect(page.getByRole("heading", { name: "Aujourd’hui", level: 1 })).toBeVisible();
  await expect(page.locator(".main-nav").getByText("Utilisateurs", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Ma semaine", exact: true }).click();
  await page.getByRole("button", { name: /Suivante/ }).click();
  await expect(page.locator(".my-schedule-days")).toBeVisible();
  const technicianAText = await page.locator(".my-schedule-days").innerText();
  expect(technicianAText).toContain("P-251");

  await selectOptionContaining(page.getByLabel("Identité de test"), "Technicien Démo B");
  await expect(page.locator(".sidebar-footer")).toContainText("Technicien Démo B");
  await page.getByRole("button", { name: "Ma semaine", exact: true }).click();
  await page.getByRole("button", { name: /Suivante/ }).click();
  await expect(page.locator(".my-schedule-days")).toBeVisible();
  const technicianBText = await page.locator(".my-schedule-days").innerText();
  expect(technicianBText).toContain("P-251");
  expect(technicianBText).not.toBe(technicianAText);

  await selectOptionContaining(page.getByLabel("Identité de test"), "Administrateur Démo");
  await expect(page.locator(".sidebar-footer")).toContainText("Administrateur Démo");
  await expect(page.locator(".main-nav").getByText("Utilisateurs", { exact: true })).toBeVisible();

  await context.close();
});

test("desktop sidebar collapse persists and Demands workspace remains responsive", async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    viewport: { width: 1440, height: 900 },
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  await page.goto("/");

  const shell = page.locator(".app-shell");
  const sidebar = page.locator(".app-sidebar");
  const collapseButton = sidebar.getByRole("button", { name: "Réduire la navigation" });

  await expect(collapseButton).toBeVisible();
  expect(await sidebar.evaluate((node) => node.getBoundingClientRect().width)).toBeGreaterThan(230);

  await collapseButton.click();
  await expect(shell).toHaveClass(/is-sidebar-compact/);
  await expect(sidebar.getByRole("button", { name: "Déployer la navigation" })).toBeVisible();
  expect(await sidebar.evaluate((node) => node.getBoundingClientRect().width)).toBeLessThan(80);
  expect(await page.evaluate(() => window.localStorage.getItem("resourceplanner.sidebar.compact"))).toBe("true");

  await navigateMain(page, "Demandes");
  await expect(page.getByRole("heading", { name: "Demandes", level: 1 })).toBeVisible();

  const workspace = page.locator(".demands-workspace");
  const listPanel = page.locator(".demand-list-panel");
  const editorPanel = page.locator(".demand-editor-panel");
  const listWidth = await listPanel.evaluate((node) => node.getBoundingClientRect().width);
  const detailWidth = await editorPanel.evaluate((node) => node.getBoundingClientRect().width);
  expect(listWidth).toBeGreaterThanOrEqual(390);
  expect(listWidth).toBeLessThanOrEqual(430);
  expect(detailWidth).toBeGreaterThan(500);
  await expect(workspace).toBeVisible();

  const projectName = page.locator(".demand-project span").first();
  await expect(projectName).toBeAttached();
  expect(await projectName.evaluate((node) => getComputedStyle(node).webkitLineClamp)).toBe("2");

  await page.reload();
  await expect(page.locator(".app-shell")).toHaveClass(/is-sidebar-compact/);
  await expect(page.locator(".app-sidebar").getByRole("button", { name: "Déployer la navigation" })).toBeVisible();

  await page.setViewportSize({ width: 800, height: 900 });
  const mobileMenu = page.getByRole("button", { name: "Ouvrir la navigation" });
  await expect(mobileMenu).toBeVisible();
  await mobileMenu.click();
  await expect(page.locator(".sidebar-backdrop")).toBeVisible();
  expect(await page.locator(".app-sidebar").evaluate((node) => node.getBoundingClientRect().width)).toBeGreaterThan(230);
  await expect(page.locator(".main-nav").getByRole("button", { name: "Demandes" })).toBeVisible();

  await closeContext(context);
});
