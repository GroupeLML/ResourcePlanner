import {
  APIRequestContext,
  Browser,
  BrowserContext,
  Page,
  expect,
  test,
} from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

function commandId(index: number) {
  return `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
}

async function roleContext(browser: Browser, role: "ADMIN" | "TECHNICIAN") {
  return browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": role },
  });
}

async function createBroadcast(
  request: APIRequestContext,
  index: number,
  title: string,
  message: string,
) {
  const response = await request.post("/api/v1/admin/runtime/broadcasts", {
    data: {
      creation_command_id: commandId(index),
      title,
      message,
      confirmed: true,
    },
  });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()).broadcast as {
    id: string;
    version: number;
    title: string;
    message: string;
  };
}

async function activeBroadcast(request: APIRequestContext) {
  const response = await request.get("/api/v1/admin/runtime/status");
  expect(response.status(), await response.text()).toBe(200);
  const body = await response.json() as {
    broadcast: { id: string; state: string } | null;
  };
  return body.broadcast?.state === "ACTIVE" ? body.broadcast : null;
}

async function endIfActive(request: APIRequestContext) {
  const active = await activeBroadcast(request);
  if (!active) return;
  const response = await request.post(
    `/api/v1/admin/runtime/broadcasts/${encodeURIComponent(active.id)}/end`,
    { data: {} },
  );
  expect(response.status(), await response.text()).toBe(200);
}

async function setFailureMode(
  request: APIRequestContext,
  statusFailure: boolean,
  acknowledgmentFailure: boolean,
) {
  const response = await request.post(
    `/__e2e__/emergency/failure-mode?status_failure=${statusFailure ? "true" : "false"}&acknowledgment_failure=${acknowledgmentFailure ? "true" : "false"}`,
  );
  expect(response.ok(), await response.text()).toBeTruthy();
}

async function openRolePage(context: BrowserContext, identityText: string) {
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText(identityText);
  return page;
}

async function expectEmergency(page: Page, title: string) {
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible({ timeout: 12_000 });
  await expect(dialog.getByRole("heading", { name: title })).toBeVisible();
  return dialog;
}

test.describe.serial("#894 emergency broadcast Phase 3", () => {
  test("admin UI creates plain-text warning; user ACK preserves blocking/accessibility and admin history", async ({ browser }) => {
    const adminContext = await roleContext(browser, "ADMIN");
    const userContext = await roleContext(browser, "TECHNICIAN");
    const adminPage = await openRolePage(adminContext, "Administrateur E2E");
    const userPage = await openRolePage(userContext, "Technicien Alice");

    try {
      await endIfActive(adminContext.request);
      await setFailureMode(adminContext.request, false, false);

      await adminPage.getByRole("button", { name: "Configuration" }).click();
      const adminPanel = adminPage.locator(".emergency-admin-panel");
      await expect(adminPanel).toBeVisible();

      await adminPanel.getByLabel("Titre").fill("Avis <script>alert('x')</script> É");
      await adminPanel.getByLabel("Message").fill(
        "Ligne 1\nLigne 2 😀 <img src=x onerror=alert(1)>",
      );
      adminPage.once("dialog", (dialog) => void dialog.accept());
      await adminPanel.getByRole("button", { name: "Diffuser maintenant" }).click();
      await expect(adminPanel).toContainText("La diffusion d’urgence est active.");

      const userDialog = await expectEmergency(
        userPage,
        "Avis <script>alert('x')</script> É",
      );
      await expect(userDialog.locator(".emergency-broadcast-message")).toHaveText(
        "Ligne 1\nLigne 2 😀 <img src=x onerror=alert(1)>",
      );
      await expect(userDialog.locator("script")).toHaveCount(0);
      await expect(userDialog.locator("img")).toHaveCount(0);
      await expect(userPage.locator("#resourceplanner-application")).toHaveAttribute(
        "aria-hidden",
        "true",
      );
      expect(await userPage.locator("#resourceplanner-application").evaluate(
        (node) => (node as HTMLElement & { inert?: boolean }).inert,
      )).toBe(true);

      await userPage.keyboard.press("Escape");
      await expect(userDialog).toBeVisible();
      await userPage.locator(".emergency-broadcast-layer").click({
        position: { x: 4, y: 4 },
      });
      await expect(userDialog).toBeVisible();
      await expect(userDialog.getByRole("button", { name: "J’ai compris" })).toBeFocused();

      await userDialog.getByRole("button", { name: "J’ai compris" }).click();
      await expect(userPage.getByRole("alertdialog")).toHaveCount(0);
      await expect(userPage.locator("#resourceplanner-application")).not.toHaveAttribute(
        "aria-hidden",
        "true",
      );

      const adminDialog = adminPage.getByRole("alertdialog");
      if (await adminDialog.count()) {
        await adminDialog.getByRole("button", { name: "J’ai compris" }).click();
        await expect(adminDialog).toHaveCount(0);
      }

      await adminPanel.getByRole("button", { name: "Actualiser" }).click();
      await expect(adminPanel).toContainText("Technicien Alice");
      await expect(adminPanel).toContainText(/acquitt/i);

      const historyItem = adminPanel.locator(".emergency-history-list > button").first();
      await expect(historyItem).toContainText("Avis <script>alert('x')</script> É");
      await historyItem.click();
      await expect(adminPanel.locator(".emergency-receipt-history")).toContainText(
        "Technicien Alice",
      );

      const denied = await userContext.request.get("/api/v1/admin/runtime/status");
      expect(denied.status(), await denied.text()).toBe(403);

      const endButton = adminPanel.getByRole("button", { name: "Terminer la diffusion" });
      adminPage.once("dialog", (dialog) => void dialog.accept());
      await endButton.click();
      await expect(adminPanel).toContainText("La diffusion est terminée.");
    } finally {
      await setFailureMode(adminContext.request, false, false).catch(() => undefined);
      await endIfActive(adminContext.request).catch(() => undefined);
      await userContext.close();
      await adminContext.close();
    }
  });

  test("three tabs and two browser contexts converge on one server receipt and new identity", async ({ browser }) => {
    const adminContext = await roleContext(browser, "ADMIN");
    const userContext = await roleContext(browser, "TECHNICIAN");
    const secondDevice = await roleContext(browser, "TECHNICIAN");

    try {
      await endIfActive(adminContext.request);
      const pages = await Promise.all([
        openRolePage(userContext, "Technicien Alice"),
        openRolePage(userContext, "Technicien Alice"),
        openRolePage(userContext, "Technicien Alice"),
      ]);
      const devicePage = await openRolePage(secondDevice, "Technicien Alice");

      const first = await createBroadcast(
        adminContext.request,
        894101,
        "A — trois onglets",
        "Même utilisateur, reçu SQL canonique.",
      );
      await Promise.all([
        ...pages.map((page) => expectEmergency(page, "A — trois onglets")),
        expectEmergency(devicePage, "A — trois onglets"),
      ]);

      await pages[0].getByRole("button", { name: "J’ai compris" }).click();
      await expect(pages[0].getByRole("alertdialog")).toHaveCount(0);

      for (const page of [...pages.slice(1), devicePage]) {
        await page.bringToFront();
        await page.evaluate(() => window.dispatchEvent(new Event("online")));
        await expect(page.getByRole("alertdialog")).toHaveCount(0, { timeout: 4_000 });
      }

      const status = await adminContext.request.get("/api/v1/admin/runtime/status");
      expect(status.status(), await status.text()).toBe(200);
      const statusBody = await status.json() as {
        users: Array<{ display_name: string; user_id: string }>;
      };
      expect(statusBody.users.filter((user) => user.display_name === "Technicien Alice"))
        .toHaveLength(1);

      const endA = await adminContext.request.post(
        `/api/v1/admin/runtime/broadcasts/${first.id}/end`,
        { data: {} },
      );
      expect(endA.status(), await endA.text()).toBe(200);

      await createBroadcast(
        adminContext.request,
        894102,
        "B — nouvelle identité",
        "B doit être acquitté indépendamment de A.",
      );
      await pages[0].bringToFront();
      await pages[0].evaluate(() => window.dispatchEvent(new Event("online")));
      await expectEmergency(pages[0], "B — nouvelle identité");
      await pages[0].getByRole("button", { name: "J’ai compris" }).click();
      await expect(pages[0].getByRole("alertdialog")).toHaveCount(0);
    } finally {
      await endIfActive(adminContext.request).catch(() => undefined);
      await secondDevice.close();
      await userContext.close();
      await adminContext.close();
    }
  });

  test("detached Planning receives warning and preserves the underlying workspace", async ({ browser }) => {
    const adminContext = await roleContext(browser, "ADMIN");
    const userContext = await roleContext(browser, "TECHNICIAN");
    const page = await openRolePage(userContext, "Technicien Alice");

    try {
      await endIfActive(adminContext.request);
      await page.getByRole("button", { name: /Planning opérationnel/i }).click();
      await expect(page.locator(".planning-board-panel")).toBeVisible();

      const [detached] = await Promise.all([
        userContext.waitForEvent("page"),
        page.getByRole("button", { name: "Ouvrir dans une fenêtre séparée" }).click(),
      ]);
      await detached.waitForLoadState("domcontentloaded");
      await expect(detached.locator(".planning-board-panel")).toBeVisible();

      await createBroadcast(
        adminContext.request,
        894201,
        "Avis Planning détaché",
        "La fenêtre séparée doit rester couverte.",
      );
      const dialog = await expectEmergency(detached, "Avis Planning détaché");
      await expect(detached.locator(".planning-board-panel")).toBeVisible();
      await dialog.getByRole("button", { name: "J’ai compris" }).click();
      await expect(detached.getByRole("alertdialog")).toHaveCount(0);
      await expect(detached.locator(".planning-board-panel")).toBeVisible();
      await detached.close();
    } finally {
      await endIfActive(adminContext.request).catch(() => undefined);
      await userContext.close();
      await adminContext.close();
    }
  });

  test("poll and ACK failures are non-destructive; reconnect/end/restart converge authoritatively", async ({ browser }) => {
    const adminContext = await roleContext(browser, "ADMIN");
    const userContext = await roleContext(browser, "TECHNICIAN");
    const adminPage = await openRolePage(adminContext, "Administrateur E2E");
    const userPage = await openRolePage(userContext, "Technicien Alice");

    try {
      await endIfActive(adminContext.request);
      await adminPage.getByRole("button", { name: "Configuration" }).click();
      const smtpServer = adminPage.getByLabel("Serveur SMTP");
      await smtpServer.fill("draft-preserved.example.test");

      await setFailureMode(adminContext.request, true, false);
      await createBroadcast(
        adminContext.request,
        894301,
        "Reconnexion",
        "Le poll échoue sans détruire le formulaire.",
      );
      await adminPage.waitForTimeout(3_500);
      await expect(smtpServer).toHaveValue("draft-preserved.example.test");
      await expect(adminPage.locator(".configuration-page")).toBeVisible();

      await setFailureMode(adminContext.request, false, false);
      await userPage.evaluate(() => window.dispatchEvent(new Event("online")));
      const userDialog = await expectEmergency(userPage, "Reconnexion");

      await setFailureMode(adminContext.request, false, true);
      await userDialog.getByRole("button", { name: "J’ai compris" }).click();
      await expect(userDialog).toBeVisible();
      await expect(userDialog).toContainText(/n’a pas été enregistrée|indisponible/i);

      await setFailureMode(adminContext.request, false, false);
      await userDialog.getByRole("button", { name: "J’ai compris" }).click();
      await expect(userPage.getByRole("alertdialog")).toHaveCount(0);

      const active = await activeBroadcast(adminContext.request);
      expect(active).not.toBeNull();
      const restart = await adminContext.request.post("/__e2e__/emergency/restart-ephemeral");
      expect(restart.ok(), await restart.text()).toBeTruthy();
      const restartBody = await restart.json() as { presence_warming: boolean };
      expect(restartBody.presence_warming).toBe(true);

      const userAfterRestart = await userContext.newPage();
      await userAfterRestart.goto("/");
      await expect(userAfterRestart.locator(".sidebar-footer")).toContainText("Technicien Alice");
      await userAfterRestart.evaluate(() => window.dispatchEvent(new Event("online")));
      await expect(userAfterRestart.getByRole("alertdialog")).toHaveCount(0);

      const end = await adminContext.request.post(
        `/api/v1/admin/runtime/broadcasts/${active!.id}/end`,
        { data: {} },
      );
      expect(end.status(), await end.text()).toBe(200);

      await createBroadcast(
        adminContext.request,
        894302,
        "Fin autoritaire",
        "Le serveur peut terminer la diffusion pendant l’affichage.",
      );
      const secondDialog = await expectEmergency(userAfterRestart, "Fin autoritaire");
      const secondActive = await activeBroadcast(adminContext.request);
      expect(secondActive).not.toBeNull();
      const endSecond = await adminContext.request.post(
        `/api/v1/admin/runtime/broadcasts/${secondActive!.id}/end`,
        { data: {} },
      );
      expect(endSecond.status(), await endSecond.text()).toBe(200);
      await userAfterRestart.evaluate(() => window.dispatchEvent(new Event("online")));
      await expect(secondDialog).toHaveCount(0, { timeout: 4_000 });
      await expect(smtpServer).toHaveValue("draft-preserved.example.test");
    } finally {
      await setFailureMode(adminContext.request, false, false).catch(() => undefined);
      await endIfActive(adminContext.request).catch(() => undefined);
      await userContext.close();
      await adminContext.close();
    }
  });
});
