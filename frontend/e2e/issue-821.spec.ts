import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";

import { Browser, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";
const BUILT_REVISION = "0123456789abcdef0123456789abcdef01234567";

async function openAdmin(browser: Browser, buildMeta: string) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "ADMIN" },
  });
  await context.route("**/build-meta.js", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: `window.RESOURCEPLANNER_BUILD = Object.freeze({ revision: "${buildMeta}" });\n`,
    });
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Administrateur");
  return { context, page };
}

test("821 expose la révision Git complète construite au survol du shell", async ({ browser }) => {
  const { context, page } = await openAdmin(browser, BUILT_REVISION);
  try {
    const shellLabel = page.getByText("RessourcePlanner V2", { exact: true });
    await expect(shellLabel).toBeVisible();
    await shellLabel.hover();
    await expect(shellLabel).toHaveAttribute(
      "title",
      `Révision déployée : ${BUILT_REVISION}`,
    );
    await expect(shellLabel).toHaveAttribute(
      "aria-label",
      `RessourcePlanner V2 — Révision déployée : ${BUILT_REVISION}`,
    );
  } finally {
    await context.close();
  }
});

test("821 affiche un état explicite quand la métadonnée de build est invalide", async ({ browser }) => {
  const { context, page } = await openAdmin(browser, "main");
  try {
    const shellLabel = page.getByText("RessourcePlanner V2", { exact: true });
    await expect(shellLabel).toBeVisible();
    await shellLabel.hover();
    await expect(shellLabel).toHaveAttribute(
      "title",
      "Révision déployée : indisponible",
    );
    await expect(shellLabel).toHaveAttribute(
      "aria-label",
      "RessourcePlanner V2 — Révision déployée : indisponible",
    );
  } finally {
    await context.close();
  }
});


test("821 recharge la révision B dans le même contexte après remplacement de l'image", async ({ browser }) => {
  const revisionA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  const revisionB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
  const nginxConfig = readFileSync(new URL("../default.conf.template", import.meta.url), "utf8");
  const buildMetaLocation = nginxConfig.match(
    /location = \/build-meta\.js \{([\s\S]*?)\n    \}/,
  )?.[1] ?? "";
  const cacheControl = buildMetaLocation.match(
    /add_header Cache-Control "([^"]+)" always;/,
  )?.[1];

  expect(cacheControl).toBe("no-store, no-cache, must-revalidate");

  let activeRevision = revisionA;
  let metadataRequests = 0;

  const server = createServer((request, response) => {
    if (request.url === "/build-meta.js") {
      metadataRequests += 1;
      response.writeHead(200, {
        "Content-Type": "application/javascript",
        "Cache-Control": cacheControl!,
        "Expires": "Thu, 01 Jan 1970 00:00:01 GMT",
      });
      response.end(
        `window.RESOURCEPLANNER_BUILD = Object.freeze({ revision: "${activeRevision}" });\n`,
      );
      return;
    }

    response.writeHead(200, {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    });
    response.end(`<!doctype html>
<html lang="fr">
  <body>
    <span id="rp-label">RessourcePlanner V2</span>
    <script src="/build-meta.js"></script>
    <script>
      const raw = (window.RESOURCEPLANNER_BUILD?.revision || "").trim();
      const revision = /^[0-9a-f]{40}$/i.test(raw) ? raw.toLowerCase() : null;
      document.getElementById("rp-label").title = revision
        ? "Révision déployée : " + revision
        : "Révision déployée : indisponible";
    </script>
  </body>
</html>`);
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address() as AddressInfo;
  const origin = `http://127.0.0.1:${address.port}`;
  const context = await browser.newContext();

  try {
    const page = await context.newPage();
    await page.goto(origin);
    const shellLabel = page.getByText("RessourcePlanner V2", { exact: true });
    await expect(shellLabel).toHaveAttribute("title", `Révision déployée : ${revisionA}`);
    expect(metadataRequests).toBe(1);

    activeRevision = revisionB;
    await page.reload();

    await expect(shellLabel).toHaveAttribute("title", `Révision déployée : ${revisionB}`);
    expect(metadataRequests).toBe(2);
  } finally {
    await context.close();
    await new Promise<void>((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
  }
});
