import base64
import json
import unittest

import httpx

from app.config import Settings
from app.github import GitHubClient
from app.service import build_dashboard

ROADMAP_BODY = """
# Roadmap maître

**#331 est terminé. #13 est maintenant la tranche active, découpée en 13A→13H.**

Ordre actif :

1. **#331 — séparation des ressources**
   **Bloc terminé : 331A ✅ → 331E ✅.** #331 est désormais complétée.

2. **#13 — périodes + enveloppe approuvée commune**
   - **13A — contrat et politique pure**
   - **13B — révisions approuvées immuables**
   - **13C — préparation commune**

   Ordre : **13A → 13B → 13C**.

3. **#328 — identité canonique du demandeur**
"""

ISSUE_BODY = """
# #13 — périodes + enveloppe approuvée commune

Décisions architecturales documentées dans ADR-002 et ADR-003.

## Découpage d'implémentation

Ordre obligatoire :

### #13A — contrat et politique pure
### #13B — capture des révisions approuvées
### #13C — préparation commune et protection des verrous
"""



PIPELINE_ROADMAP_DONE_333 = """
**#332 est terminé. #333 est la tranche produit active.**

Ordre actif :

1. **#333 — extension de fenêtre contrôlée**
   - ✅ **333A — décision contextuelle**
   - ✅ **333B — proposition hors enveloppe**
   - ✅ **333C — dialogue React + acceptation**

### Suite produit après #333 — bloc P1 puis préparation environnementale

Chemin principal retenu :

```text
#333A → #333B → #333C
  ↓
ASTRA ciblé #901 sur main post-#333
  ↓
#901 actifs réservables
  ↓
#902 qualifications
  ↓
#903 validation VM Ubuntu réelle avec SQLite
```

**Gates et logique :**

| Étape | État / gate | Pourquoi maintenant |
|---|---|---|
| #333 | terminé | bloc DEV stabilisé |
| analyse ASTRA #901 | après fusion complète de #333 | vérifier l'architecture |
| #901 | NEXT après analyse | travail DEV |
| #902 | après #901 | travail DEV |
| #903 | après P1 ou en parallèle infra | valider la vraie VM Ubuntu |
"""



ROADMAP_CURRENT_292 = """
# Roadmap maître

Ordre actif :

1. **#291 — ressources réservables non humaines — TERMINÉ**
   Ordre obligatoire : **291A → 291B → 291C → 291D → 291E → 291F**.

### Suite produit après #333 — bloc P1 puis préparation environnementale

Chemin principal retenu :

```text
#291A ✅ → #291B ✅ → #291C ✅ → #291D ✅ → #291E ✅ → #291F ✅
  ↓
#292 qualifications/permis des actifs
  ↓
#399 demande d'annulation + résolution coordonnateur
  ↓
#276 routage d'approbation par tâches
  ↓
#278 dashboard Coordonnateur consolidé

En parallèle dès maintenant : #398 UX sidebar + liste Demandes
```

#291A–#291F sont maintenant terminés; le flux principal poursuit avec **#292 → #399 → #276 → #278**.
"""


ROADMAP_CURRENT_407 = """
# Roadmap maître

### Suite produit après #333 — bloc P1 puis préparation environnementale

Chemin principal retenu :

```text
#292 ✅ qualifications/permis des actifs — PR #406, CI #822
  ↓
#407 cohérence sauvegarde → workflow + protection des modifications non enregistrées
  ↓
#399 demande d'annulation + résolution coordonnateur
  ↓
#276 routage d'approbation par tâches
  ↓
#410 inclure les demandes du coordonnateur assigné dans Mon périmètre
  ↓
#278 dashboard Coordonnateur consolidé
```

| Étape | État / gate | Pourquoi maintenant |
|---|---|---|
| #292 | ✅ TERMINÉ — PR #406 / CI #822 | livré |
| #407 | 🟠 NEXT — #292 terminée | corriger la cohérence version/read model |
| #399 | après #407 | annulation |
| #276 | après #399 | approbation |
| #410 | avant #278 | scope coordonnateur |
| #278 | après #407 + #399 + #276 + #410 | dashboard |
"""


CANONICAL_399A_ROADMAP = """
# Roadmap maître

#407 est terminé via PR #412. ASTRA #399 est terminé.

<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
407 | WORK | DONE | #407 | MAIN | cohérence sauvegarde → workflow
ASTRA-399 | ARCHITECTURE_GATE | DONE | #399 | MAIN | analyse architecture #399
399A | WORK | READY | #399 | MAIN | état persistant + politique commune
399B | WORK | BLOCKED | #399 | MAIN | acceptation atomique humain + actif
399C | WORK | BLOCKED | #399 | MAIN | concurrence + invariants
399D | WORK | BLOCKED | #399 | MAIN | React + E2E
276 | WORK | BLOCKED | #276 | MAIN | routage approbation
410 | WORK | BLOCKED | #410 | MAIN | périmètre coordonnateur
278 | WORK | BLOCKED | #278 | MAIN | dashboard coordonnateur
<!-- /COCKPIT_PIPELINE_V1 -->

### Suite produit
```text
#407 ✅ → ASTRA #399 ✅ → #399A → #399B → #399C → #399D
```
"""

CANONICAL_276D_ROADMAP = """
# Roadmap maître

#276C est terminé via PR #438. 276D est la tranche active.

<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
276C | WORK | DONE | #276 | MAIN | quorum et finalisation atomique
276D | WORK | READY | #276 | MAIN | API administration React et parcours navigateur
410 | WORK | BLOCKED | #410 | MAIN | périmètre coordonnateur assigné
278 | WORK | BLOCKED | #278 | MAIN | dashboard coordonnateur
<!-- /COCKPIT_PIPELINE_V1 -->
"""

ISSUE_276_276D_READY = """
# #276 — multi-approbation

### #276C — quorum et finalisation atomique — DONE (PR #438)
### #276D — API administration React et parcours navigateur — READY
"""


INVALID_CANONICAL_ROADMAP = """
# Roadmap maître

### Suite produit
```text
#399A
```

<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
399A | WORK | READY | #399 | MAIN | A
399B | WORK | READY | #399 | MAIN | B
<!-- /COCKPIT_PIPELINE_V1 -->
"""

ISSUE_399_CONTRADICTORY = """
# #399 — annulation

### #399A — état persistant — DONE (PR #413)
### #399B — acceptation atomique
### #399C — concurrence
### #399D — React
"""

ROADMAP_291_READY = """
**L'analyse ASTRA est terminée. La prochaine tranche active est #291, avec 291A READY.**

Ordre actif :

1. **#291 — ressources réservables non humaines — ACTIF**
   - **291A — contrats et frontières pures — READY**
   - **291B — catalogue et persistance**
   - **291C — demande, approbation et matérialisation**
   - **291D — réservations et concurrence**
   - **291E — projections et delta**
   - **291F — React et acceptation**
"""

ISSUE_291_READY = """
# #291 — ressources réservables non humaines

Analyse ASTRA terminée. ADR-007 accepté via une PR documentaire avant DEV.

## Découpage d'implémentation

Ordre obligatoire : **291A → 291B → 291C → 291D → 291E → 291F**.

### #291A — contrats et frontières pures — READY
### #291B — catalogue et persistance
### #291C — demande, approbation et matérialisation
### #291D — réservations et concurrence
### #291E — projections et delta
### #291F — React et acceptation
"""



ROADMAP_291E_ACTIVE = """
# Roadmap maître

**#291 est la tranche produit active. 291A–291D terminées; 291E READY.**

Ordre actif :

1. **#291 — ressources réservables non humaines — ACTIF**
"""

ISSUE_291E_ACTIVE = """
# #291 — ressources réservables non humaines

## Découpage d'implémentation

Ordre obligatoire : **291A → 291B → 291C → 291D → 291E → 291F**.

### #291A — contrats et frontières pures — DONE (PR #394, CI #801)
### #291B — catalogue et persistance — DONE (PR #394, CI #801)
### #291C — demande, approbation et matérialisation — DONE (PR #394, CI #801)
### #291D — réservations et concurrence — DONE (PR #394, CI #801)
### #291E — projections et delta — READY
### #291F — React et acceptation
"""

AGENTS = """
## 20. Chained execution
Automatic chaining is allowed only when the next item belongs to the same approved work block.
"""


def response(data, status=200):
    return httpx.Response(status, json=data)


def encoded_file(text):
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def _dashboard(self, closed_pulls=None):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            query = dict(request.url.params)
            if path == "/repos/tchi99/RessourcePlanner/issues/55":
                return response({"number": 55, "title": "Roadmap maître", "body": ROADMAP_BODY, "html_url": "https://github.test/issues/55", "updated_at": "2026-09-22T15:00:00Z", "state": "open"})
            if path == "/repos/tchi99/RessourcePlanner/issues/13":
                return response({"number": 13, "title": "Périodes + enveloppe", "body": ISSUE_BODY, "state": "open", "html_url": "https://github.test/issues/13", "updated_at": "2026-09-22T15:00:00Z"})
            if path == "/repos/tchi99/RessourcePlanner/pulls" and query.get("state") == "open":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/pulls" and query.get("state") == "closed":
                return response(closed_pulls or [])
            if path.startswith("/repos/tchi99/RessourcePlanner/pulls/") and path.endswith("/files"):
                return response([
                    {"filename": "app/domain/implementation.py", "status": "modified"}
                ])
            if path == "/repos/tchi99/RessourcePlanner/commits":
                return response([{"sha": "def456", "html_url": "https://github.test/commit/def456", "commit": {"message": "main", "author": {"date": "2026-09-22T14:55:00Z"}}}])
            if path == "/repos/tchi99/RessourcePlanner/contents/AGENTS.md":
                return response(encoded_file(AGENTS))
            if path == "/repos/tchi99/RessourcePlanner/contents/docs/architecture":
                return response([
                    {"name": "ADR-001-other.md", "html_url": "https://github.test/adr1"},
                    {"name": "ADR-002-periods.md", "html_url": "https://github.test/adr2"},
                    {"name": "ADR-003-approval.md", "html_url": "https://github.test/adr3"},
                    {"name": "README.md", "html_url": "https://github.test/arch"},
                ])
            if path == "/repos/tchi99/RessourcePlanner/branches":
                return response([{"name": "main", "commit": {"sha": "def456"}}])
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        settings = Settings(
            github_token="secret-that-must-not-leak",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            stalled_after_minutes=20,
            github_api_url="https://api.github.test",
        )
        client = GitHubClient(settings, transport=httpx.MockTransport(handler))
        try:
            return await build_dashboard(client, settings, "tchi99/RessourcePlanner")
        finally:
            await client.close()

    async def test_current_issue_resolves_first_ready_subitem_and_dynamic_adrs(self):
        dashboard = await self._dashboard()
        self.assertEqual(dashboard["roadmap"]["active_issue"], 13)
        self.assertEqual(dashboard["roadmap"]["effective_active"], "13A")
        self.assertEqual(dashboard["active_work"]["subitem_key"], "13A")
        self.assertTrue(dashboard["active_work"]["can_chain_block"])
        self.assertEqual(dashboard["active_work"]["remaining_subitems"], ["13A", "13B", "13C"])
        self.assertEqual(dashboard["architecture"]["referenced_adrs"], ["ADR-002-periods.md", "ADR-003-approval.md"])
        self.assertIn("Continue #13 à partir de 13A", dashboard["dev_prompt"])
        self.assertNotIn("secret-that-must-not-leak", json.dumps(dashboard))

    async def test_branch_without_pr_is_observed_for_silent_stall(self):
        issue_body = ISSUE_BODY.replace(
            "### #13A — contrat et politique pure",
            "### 🟡 #13A — contrat et politique pure",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            query = dict(request.url.params)
            if path == "/repos/tchi99/RessourcePlanner/issues/55":
                return response({"number": 55, "title": "Roadmap maître", "body": ROADMAP_BODY, "html_url": "https://github.test/issues/55", "updated_at": "2026-09-22T14:00:00Z", "state": "open"})
            if path == "/repos/tchi99/RessourcePlanner/issues/13":
                return response({"number": 13, "title": "Périodes + enveloppe", "body": issue_body, "state": "open", "html_url": "https://github.test/issues/13", "updated_at": "2026-09-22T14:00:00Z"})
            if path == "/repos/tchi99/RessourcePlanner/pulls":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/commits":
                return response([{"sha": "main456", "html_url": "https://github.test/commit/main456", "commit": {"message": "main", "author": {"date": "2026-09-22T14:05:00Z"}}}])
            if path == "/repos/tchi99/RessourcePlanner/commits/abc":
                return response({"sha": "abc", "html_url": "https://github.test/commit/abc", "commit": {"message": "13A work", "author": {"date": "2026-09-22T14:00:00Z"}}})
            if path == "/repos/tchi99/RessourcePlanner/actions/runs":
                self.assertEqual(query.get("head_sha"), "abc")
                return response({"workflow_runs": []})
            if path == "/repos/tchi99/RessourcePlanner/contents/AGENTS.md":
                return response(encoded_file(AGENTS))
            if path == "/repos/tchi99/RessourcePlanner/contents/docs/architecture":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/branches":
                return response([
                    {"name": "main", "commit": {"sha": "main456"}},
                    {"name": "issue-13a", "commit": {"sha": "abc"}},
                ])
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            stalled_after_minutes=20,
            github_api_url="https://api.github.test",
        )
        client = GitHubClient(settings, transport=httpx.MockTransport(handler))
        try:
            dashboard = await build_dashboard(client, settings, "tchi99/RessourcePlanner")
        finally:
            await client.close()

        self.assertTrue(dashboard["active_work"]["explicit_in_progress"])
        self.assertEqual(dashboard["active_work"]["active_branch"]["name"], "issue-13a")
        self.assertIsNone(dashboard["active_work"]["primary_pr"])
        self.assertIn("STALLED", dashboard["active_work"]["states"])
        self.assertIn("branche issue-13a", dashboard["dev_prompt"])


    async def test_done_subitems_advance_active_branch_to_291e_head(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            query = dict(request.url.params)

            if path == "/repos/tchi99/RessourcePlanner/issues/55":
                return response({
                    "number": 55,
                    "title": "Roadmap maître",
                    "body": ROADMAP_291E_ACTIVE,
                    "html_url": "https://github.test/issues/55",
                    "updated_at": "2026-09-23T15:00:00Z",
                    "state": "open",
                })
            if path == "/repos/tchi99/RessourcePlanner/issues/291":
                return response({
                    "number": 291,
                    "title": "Ressources réservables",
                    "body": ISSUE_291E_ACTIVE,
                    "state": "open",
                    "html_url": "https://github.test/issues/291",
                    "updated_at": "2026-09-23T15:00:00Z",
                })
            if path == "/repos/tchi99/RessourcePlanner/pulls":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/commits":
                return response([{
                    "sha": "394e755bef94fb0fd7a4663e15e50380b8aee094",
                    "html_url": "https://github.test/commit/394e755",
                    "commit": {
                        "message": "feat: add reservable assets through approval and planning commands",
                        "author": {"date": "2026-09-23T14:50:00Z"},
                    },
                }])
            if path == "/repos/tchi99/RessourcePlanner/commits/f16d52f":
                return response({
                    "sha": "f16d52f",
                    "html_url": "https://github.test/commit/f16d52f",
                    "commit": {
                        "message": "test(291E): cover asset planning projections and delta",
                        "author": {"date": "2026-09-23T15:01:23Z"},
                    },
                })
            if path == "/repos/tchi99/RessourcePlanner/actions/runs":
                self.assertEqual(query.get("head_sha"), "f16d52f")
                return response({"workflow_runs": []})
            if path == "/repos/tchi99/RessourcePlanner/contents/AGENTS.md":
                return response(encoded_file(AGENTS))
            if path == "/repos/tchi99/RessourcePlanner/contents/docs/architecture":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/branches":
                return response([
                    {
                        "name": "main",
                        "commit": {
                            "sha": "394e755bef94fb0fd7a4663e15e50380b8aee094"
                        },
                    },
                    {
                        "name": "feat/291a-d-reservable-assets",
                        "commit": {"sha": "old291ad"},
                    },
                    {
                        "name": "feat/291e-asset-planning-projections",
                        "commit": {"sha": "f16d52f"},
                    },
                ])
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            stalled_after_minutes=20,
            github_api_url="https://api.github.test",
        )
        client = GitHubClient(settings, transport=httpx.MockTransport(handler))
        try:
            dashboard = await build_dashboard(client, settings, "tchi99/RessourcePlanner")
        finally:
            await client.close()

        self.assertEqual(dashboard["roadmap"]["effective_active"], "291E")
        self.assertEqual(dashboard["active_work"]["subitem_key"], "291E")
        self.assertEqual(
            dashboard["active_work"]["active_branch"]["name"],
            "feat/291e-asset-planning-projections",
        )
        self.assertEqual(
            dashboard["active_work"]["last_commit"]["sha"],
            "f16d52f",
        )
        self.assertEqual(
            dashboard["latest_commit"]["sha"],
            "394e755bef94fb0fd7a4663e15e50380b8aee094",
        )
        self.assertIn("IN_PROGRESS", dashboard["active_work"]["states"])
        self.assertIn("feat/291e-asset-planning-projections", dashboard["dev_prompt"])

    async def test_active_branch_can_be_on_later_page_and_newest_match_wins(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            query = dict(request.url.params)
            if path == "/repos/tchi99/RessourcePlanner/issues/55":
                return response({
                    "number": 55,
                    "title": "Roadmap maître",
                    "body": ROADMAP_BODY,
                    "html_url": "https://github.test/issues/55",
                    "updated_at": "2026-09-22T23:30:00Z",
                    "state": "open",
                })
            if path == "/repos/tchi99/RessourcePlanner/issues/13":
                return response({
                    "number": 13,
                    "title": "Périodes + enveloppe",
                    "body": ISSUE_BODY,
                    "state": "open",
                    "html_url": "https://github.test/issues/13",
                    "updated_at": "2026-09-22T23:30:00Z",
                })
            if path == "/repos/tchi99/RessourcePlanner/pulls":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/commits":
                return response([{
                    "sha": "main456",
                    "html_url": "https://github.test/commit/main456",
                    "commit": {
                        "message": "main",
                        "author": {"date": "2026-09-22T23:31:00Z"},
                    },
                }])
            if path == "/repos/tchi99/RessourcePlanner/commits/old13a":
                return response({
                    "sha": "old13a",
                    "html_url": "https://github.test/commit/old13a",
                    "commit": {
                        "message": "old 13A work",
                        "author": {"date": "2026-09-22T22:00:00Z"},
                    },
                })
            if path == "/repos/tchi99/RessourcePlanner/commits/new13a":
                return response({
                    "sha": "new13a",
                    "html_url": "https://github.test/commit/new13a",
                    "commit": {
                        "message": "current 13A work",
                        "author": {"date": "2026-09-22T23:45:00Z"},
                    },
                })
            if path == "/repos/tchi99/RessourcePlanner/actions/runs":
                self.assertEqual(query.get("head_sha"), "new13a")
                return response({"workflow_runs": []})
            if path == "/repos/tchi99/RessourcePlanner/contents/AGENTS.md":
                return response(encoded_file(AGENTS))
            if path == "/repos/tchi99/RessourcePlanner/contents/docs/architecture":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/branches":
                page = int(query.get("page", "1"))
                if page == 1:
                    branches = [
                        {"name": f"archive-{index:03d}", "commit": {"sha": f"archive{index:03d}"}}
                        for index in range(99)
                    ]
                    branches.append(
                        {"name": "issue-13a-old", "commit": {"sha": "old13a"}}
                    )
                    return response(branches)
                if page == 2:
                    return response([
                        {"name": "issue-13a-current", "commit": {"sha": "new13a"}},
                    ])
                return response([])
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            stalled_after_minutes=20,
            github_api_url="https://api.github.test",
        )
        client = GitHubClient(settings, transport=httpx.MockTransport(handler))
        try:
            dashboard = await build_dashboard(client, settings, "tchi99/RessourcePlanner")
        finally:
            await client.close()

        self.assertEqual(
            dashboard["active_work"]["active_branch"]["name"],
            "issue-13a-current",
        )
        self.assertEqual(
            dashboard["active_work"]["last_commit"]["sha"],
            "new13a",
        )
        self.assertNotEqual(
            dashboard["active_work"]["last_commit"]["sha"],
            dashboard["latest_commit"]["sha"],
        )


    async def _pipeline_dashboard(self, *, gate_done: bool):
        roadmap_body = PIPELINE_ROADMAP_DONE_333
        if gate_done:
            roadmap_body = roadmap_body.replace(
                "| analyse ASTRA #901 | après fusion complète de #333 |",
                "| analyse ASTRA #901 | ✅ terminée |",
            )

        issue_body = """
# #333 — extension de fenêtre contrôlée
### ✅ #333A — décision contextuelle
### ✅ #333B — proposition hors enveloppe
### ✅ #333C — dialogue React + acceptation
"""

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            query = dict(request.url.params)
            if path == "/repos/tchi99/RessourcePlanner/issues/55":
                return response({
                    "number": 55,
                    "title": "Roadmap maître",
                    "body": roadmap_body,
                    "html_url": "https://github.test/issues/55",
                    "updated_at": "2026-09-23T01:00:00Z",
                    "state": "open",
                })
            if path == "/repos/tchi99/RessourcePlanner/issues/333":
                return response({
                    "number": 333,
                    "title": "Extension de fenêtre",
                    "body": issue_body,
                    "state": "open",
                    "html_url": "https://github.test/issues/333",
                    "updated_at": "2026-09-23T01:00:00Z",
                })
            if path == "/repos/tchi99/RessourcePlanner/pulls":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/commits":
                return response([{
                    "sha": "main-pipeline",
                    "html_url": "https://github.test/commit/main-pipeline",
                    "commit": {
                        "message": "main",
                        "author": {"date": "2026-09-23T00:59:00Z"},
                    },
                }])
            if path == "/repos/tchi99/RessourcePlanner/contents/AGENTS.md":
                return response(encoded_file(AGENTS))
            if path == "/repos/tchi99/RessourcePlanner/contents/docs/architecture":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/branches":
                return response([{"name": "main", "commit": {"sha": "main-pipeline"}}])
            if path.startswith("/repos/tchi99/RessourcePlanner/issues/"):
                number = int(path.rsplit("/", 1)[-1])
                return response({
                    "number": number,
                    "title": f"Issue {number}",
                    "body": "",
                    "state": "open",
                    "html_url": f"https://github.test/issues/{number}",
                    "updated_at": "2026-09-23T01:00:00Z",
                })
            if path == "/repos/tchi99/RessourcePlanner/actions/runs":
                return response({"workflow_runs": []})
            raise AssertionError(f"Unexpected request: {request.method} {request.url} {query}")

        settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            stalled_after_minutes=20,
            github_api_url="https://api.github.test",
        )
        client = GitHubClient(settings, transport=httpx.MockTransport(handler))
        try:
            return await build_dashboard(client, settings, "tchi99/RessourcePlanner")
        finally:
            await client.close()

    async def test_architecture_gate_stops_dev_prompt_after_completed_block(self):
        dashboard = await self._pipeline_dashboard(gate_done=False)
        self.assertTrue(dashboard["active_work"]["block_done"])
        self.assertEqual(dashboard["pipeline"]["now"]["kind"], "ARCHITECTURE_GATE")
        self.assertEqual(dashboard["pipeline"]["now"]["issue_number"], 901)
        self.assertIn("Gate d'architecture requise", dashboard["next_action"])
        self.assertIn("Aucune tranche DEV", dashboard["dev_prompt"])
        self.assertIn("ne l'exécute pas comme une tranche", dashboard["dev_prompt"])
        self.assertNotIn("Démarrer/reprendre", dashboard["dev_prompt"])

    async def test_satisfied_architecture_gate_releases_next_work_prompt(self):
        dashboard = await self._pipeline_dashboard(gate_done=True)
        self.assertEqual(dashboard["pipeline"]["now"]["key"], "901")
        self.assertEqual(dashboard["pipeline"]["now"]["kind"], "WORK")
        self.assertEqual(dashboard["roadmap"]["active_issue"], 901)
        self.assertEqual(dashboard["roadmap"]["effective_active"], "901")
        self.assertIn("Démarrer/reprendre 901", dashboard["next_action"])
        self.assertIn("#901", dashboard["dev_prompt"])


    async def _dashboard_291_with_architecture_pr(self, *, merged: bool):
        pr = {
            "number": 392,
            "title": "docs: add architecture ADR for reservable assets",
            "body": "Architecture analysis for #291 completed before 291A. No business code is modified.",
            "html_url": "https://github.test/pull/392",
            "merged_at": "2026-09-23T14:17:04Z" if merged else None,
            "updated_at": "2026-09-23T14:17:04Z",
            "state": "closed" if merged else "open",
            "draft": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "head": {"ref": "docs/291-architecture", "sha": "docs392"},
            "base": {"ref": "main"},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            query = dict(request.url.params)
            if path == "/repos/tchi99/RessourcePlanner/issues/55":
                return response({
                    "number": 55,
                    "title": "Roadmap maître",
                    "body": ROADMAP_291_READY,
                    "html_url": "https://github.test/issues/55",
                    "updated_at": "2026-09-23T14:18:00Z",
                    "state": "open",
                })
            if path == "/repos/tchi99/RessourcePlanner/issues/291":
                return response({
                    "number": 291,
                    "title": "Ressources réservables",
                    "body": ISSUE_291_READY,
                    "state": "open",
                    "html_url": "https://github.test/issues/291",
                    "updated_at": "2026-09-23T14:18:00Z",
                })
            if path == "/repos/tchi99/RessourcePlanner/pulls":
                state = query.get("state")
                if merged:
                    return response([pr] if state == "closed" else [])
                return response([pr] if state == "open" else [])
            if path == "/repos/tchi99/RessourcePlanner/pulls/392":
                return response(pr)
            if path == "/repos/tchi99/RessourcePlanner/pulls/392/files":
                return response([
                    {
                        "filename": "docs/architecture/ADR-099-reservable-assets.md",
                        "status": "added",
                    },
                    {
                        "filename": "docs/architecture/README.md",
                        "status": "modified",
                    },
                ])
            if path == "/repos/tchi99/RessourcePlanner/actions/runs":
                return response({"workflow_runs": []})
            if path == "/repos/tchi99/RessourcePlanner/commits":
                return response([{
                    "sha": "main291",
                    "html_url": "https://github.test/commit/main291",
                    "commit": {
                        "message": "main",
                        "author": {"date": "2026-09-23T14:18:00Z"},
                    },
                }])
            if path == "/repos/tchi99/RessourcePlanner/contents/AGENTS.md":
                return response(encoded_file(AGENTS))
            if path == "/repos/tchi99/RessourcePlanner/contents/docs/architecture":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/branches":
                return response([{"name": "main", "commit": {"sha": "main291"}}])
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            stalled_after_minutes=20,
            github_api_url="https://api.github.test",
        )
        client = GitHubClient(settings, transport=httpx.MockTransport(handler))
        try:
            return await build_dashboard(client, settings, "tchi99/RessourcePlanner")
        finally:
            await client.close()

    async def test_open_architecture_docs_pr_is_not_dev_work(self):
        dashboard = await self._dashboard_291_with_architecture_pr(merged=False)

        self.assertEqual(dashboard["active_work"]["subitem_key"], "291A")
        self.assertIsNone(dashboard["active_work"]["primary_pr"])
        self.assertEqual(dashboard["open_prs"][0]["number"], 392)
        self.assertIn("Démarrer/reprendre 291A", dashboard["next_action"])
        self.assertIn(
            "291A → 291B → 291C → 291D → 291E → 291F",
            dashboard["dev_prompt"],
        )

    async def test_merged_architecture_docs_pr_does_not_block_ready_dev_slice(self):
        dashboard = await self._dashboard_291_with_architecture_pr(merged=True)

        self.assertEqual(dashboard["active_work"]["subitem_key"], "291A")
        self.assertIsNone(dashboard["active_work"]["merged_but_unmarked_pr"])
        self.assertNotIn("fusionnée, mais la tranche", dashboard["dev_prompt"])
        self.assertIn("Démarrer/reprendre 291A", dashboard["next_action"])
        self.assertIn(
            "291A → 291B → 291C → 291D → 291E → 291F",
            dashboard["dev_prompt"],
        )

    async def test_merged_pr_does_not_mark_unupdated_subitem_done(self):
        dashboard = await self._dashboard([
            {
                "number": 401,
                "title": "#13A — contrat et politique pure",
                "body": "Refs #13 #55",
                "html_url": "https://github.test/pull/401",
                "merged_at": "2026-09-22T15:05:00Z",
            }
        ])
        self.assertEqual(dashboard["roadmap"]["effective_active"], "13A")
        self.assertFalse(dashboard["active_work"]["block_done"])
        self.assertEqual(dashboard["active_work"]["merged_but_unmarked_pr"]["number"], 401)
        self.assertIn("n'est pas explicitement terminée", dashboard["dev_prompt"])


    async def test_product_pipeline_is_canonical_over_stale_active_order(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            query = dict(request.url.params)
            if path == "/repos/tchi99/RessourcePlanner/issues/55":
                return response({
                    "number": 55,
                    "title": "Roadmap maître",
                    "body": ROADMAP_CURRENT_292,
                    "html_url": "https://github.test/issues/55",
                    "updated_at": "2026-09-23T19:38:04Z",
                    "state": "open",
                })
            if path == "/repos/tchi99/RessourcePlanner/issues/292":
                return response({
                    "number": 292,
                    "title": "Qualifications/permis des actifs",
                    "body": "## Priorité\nP1 — READY.",
                    "state": "open",
                    "html_url": "https://github.test/issues/292",
                    "updated_at": "2026-09-23T17:01:45Z",
                })
            if path == "/repos/tchi99/RessourcePlanner/issues/291":
                raise AssertionError("Le resolver canonique ne doit plus charger #291.")
            if path == "/repos/tchi99/RessourcePlanner/pulls":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/commits":
                return response([{
                    "sha": "main-current",
                    "html_url": "https://github.test/commit/main-current",
                    "commit": {
                        "message": "main",
                        "author": {"date": "2026-09-23T19:39:00Z"},
                    },
                }])
            if path == "/repos/tchi99/RessourcePlanner/contents/AGENTS.md":
                return response(encoded_file(AGENTS))
            if path == "/repos/tchi99/RessourcePlanner/contents/docs/architecture":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/branches":
                return response([
                    {"name": "main", "commit": {"sha": "main-current"}},
                ])
            raise AssertionError(
                f"Unexpected request: {request.method} {request.url} {query}"
            )

        settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            stalled_after_minutes=20,
            github_api_url="https://api.github.test",
        )
        client = GitHubClient(settings, transport=httpx.MockTransport(handler))
        try:
            dashboard = await build_dashboard(
                client,
                settings,
                "tchi99/RessourcePlanner",
            )
        finally:
            await client.close()

        self.assertEqual(dashboard["roadmap"]["active_issue"], 292)
        self.assertEqual(dashboard["roadmap"]["effective_active"], "292")
        self.assertEqual(dashboard["active_work"]["issue_number"], 292)
        self.assertEqual(dashboard["pipeline"]["now"]["key"], "292")
        self.assertEqual(
            [step["key"] for step in dashboard["pipeline"]["parallel"]],
            ["398"],
        )
        self.assertEqual(
            [step["key"] for step in dashboard["pipeline"]["next"]],
            ["399", "276", "278"],
        )


    async def test_next_row_dependency_completion_keeps_407_active(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            query = dict(request.url.params)
            if path == "/repos/tchi99/RessourcePlanner/issues/55":
                return response({
                    "number": 55,
                    "title": "Roadmap maître",
                    "body": ROADMAP_CURRENT_407,
                    "html_url": "https://github.test/issues/55",
                    "updated_at": "2026-09-23T21:26:33Z",
                    "state": "open",
                })
            if path == "/repos/tchi99/RessourcePlanner/issues/407":
                return response({
                    "number": 407,
                    "title": "Cohérence sauvegarde → workflow",
                    "body": "## Priorité\nNEXT",
                    "state": "open",
                    "html_url": "https://github.test/issues/407",
                    "updated_at": "2026-09-23T21:20:00Z",
                })
            if path == "/repos/tchi99/RessourcePlanner/pulls":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/commits":
                return response([{
                    "sha": "main-407",
                    "html_url": "https://github.test/commit/main-407",
                    "commit": {
                        "message": "main",
                        "author": {"date": "2026-09-23T21:27:00Z"},
                    },
                }])
            if path == "/repos/tchi99/RessourcePlanner/contents/AGENTS.md":
                return response(encoded_file(AGENTS))
            if path == "/repos/tchi99/RessourcePlanner/contents/docs/architecture":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/branches":
                return response([
                    {"name": "main", "commit": {"sha": "main-407"}},
                ])
            raise AssertionError(
                f"Unexpected request: {request.method} {request.url} {query}"
            )

        settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            stalled_after_minutes=20,
            github_api_url="https://api.github.test",
        )
        client = GitHubClient(settings, transport=httpx.MockTransport(handler))
        try:
            dashboard = await build_dashboard(
                client,
                settings,
                "tchi99/RessourcePlanner",
            )
        finally:
            await client.close()

        self.assertEqual(dashboard["roadmap"]["active_issue"], 407)
        self.assertEqual(dashboard["roadmap"]["effective_active"], "407")
        self.assertEqual(dashboard["active_work"]["issue_number"], 407)
        self.assertEqual(dashboard["pipeline"]["now"]["key"], "407")
        self.assertEqual(dashboard["execution"]["phase"], "READY")
        self.assertEqual(
            dashboard["execution"]["missions"]["developer"]["state"],
            "action",
        )
        self.assertIn("407", dashboard["execution"]["next_action"])
        self.assertEqual(
            dashboard["handoff"]["packs"]["developer"]["confidence"],
            "COMPLETE",
        )
        handoff_prompt = dashboard["handoff"]["packs"]["developer"]["prompt"]
        self.assertIn("Roadmap humain : #55", handoff_prompt)
        self.assertIn("Pipeline canonique : #55", handoff_prompt)
        self.assertEqual(dashboard["attention"]["status"], "ACTION")
        self.assertEqual(dashboard["attention"]["action_count"], 1)
        self.assertEqual(dashboard["attention"]["items"][0]["role"], "developer")
        self.assertEqual(
            [step["key"] for step in dashboard["pipeline"]["next"]],
            ["399", "276", "410"],
        )

    async def test_canonical_pipeline_ignores_merged_pr_number_and_human_status(self):
        merged_412 = {
            "number": 412,
            "title": "fix: keep demand workflow aligned with canonical saved version (#407)",
            "body": "Refs #407 #55",
            "html_url": "https://github.test/pull/412",
            "merged_at": "2026-09-24T00:22:21Z",
            "updated_at": "2026-09-24T00:22:21Z",
            "state": "closed",
            "draft": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "head": {"ref": "fix/407-demand-workflow-coherence", "sha": "pr412"},
            "base": {"ref": "main"},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            query = dict(request.url.params)
            if path == "/repos/tchi99/RessourcePlanner/issues/55":
                return response({
                    "number": 55,
                    "title": "Roadmap maître",
                    "body": CANONICAL_399A_ROADMAP,
                    "html_url": "https://github.test/issues/55",
                    "updated_at": "2026-09-24T00:30:00Z",
                    "state": "open",
                })
            if path == "/repos/tchi99/RessourcePlanner/issues/399":
                return response({
                    "number": 399,
                    "title": "Demande annulation",
                    "body": ISSUE_399_CONTRADICTORY,
                    "state": "open",
                    "html_url": "https://github.test/issues/399",
                    "updated_at": "2026-09-24T00:25:00Z",
                })
            if path == "/repos/tchi99/RessourcePlanner/issues/407":
                raise AssertionError("Le pipeline canonique ne doit pas revenir à #407.")
            if path == "/repos/tchi99/RessourcePlanner/issues/412":
                raise AssertionError("Un numéro de PR ne peut jamais devenir une issue active.")
            if path == "/repos/tchi99/RessourcePlanner/pulls":
                return response([merged_412] if query.get("state") == "closed" else [])
            if path == "/repos/tchi99/RessourcePlanner/pulls/412/files":
                return response([{"filename": "frontend/src/App.tsx", "status": "modified"}])
            if path == "/repos/tchi99/RessourcePlanner/commits":
                return response([{
                    "sha": "main-after-412",
                    "html_url": "https://github.test/commit/main-after-412",
                    "commit": {
                        "message": "main",
                        "author": {"date": "2026-09-24T00:23:00Z"},
                    },
                }])
            if path == "/repos/tchi99/RessourcePlanner/contents/AGENTS.md":
                return response(encoded_file(AGENTS))
            if path == "/repos/tchi99/RessourcePlanner/contents/docs/architecture":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/branches":
                return response([
                    {"name": "main", "commit": {"sha": "main-after-412"}},
                    {"name": "fix/407-old", "commit": {"sha": "pr412"}},
                ])
            if path == "/repos/tchi99/RessourcePlanner/commits/pr412":
                return response({
                    "sha": "pr412",
                    "html_url": "https://github.test/commit/pr412",
                    "commit": {"message": "407", "author": {"date": "2026-09-24T00:20:00Z"}},
                })
            if path == "/repos/tchi99/RessourcePlanner/actions/runs":
                return response({"workflow_runs": []})
            raise AssertionError(f"Unexpected request: {request.method} {request.url} {query}")

        settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            stalled_after_minutes=20,
            github_api_url="https://api.github.test",
        )
        client = GitHubClient(settings, transport=httpx.MockTransport(handler))
        try:
            dashboard = await build_dashboard(client, settings, "tchi99/RessourcePlanner")
        finally:
            await client.close()

        self.assertTrue(dashboard["pipeline"]["valid"])
        self.assertEqual(dashboard["pipeline"]["source"], "canonical_v1")
        self.assertEqual(dashboard["pipeline"]["now"]["key"], "399A")
        self.assertEqual(dashboard["roadmap"]["active_issue"], 399)
        self.assertEqual(dashboard["roadmap"]["effective_active"], "399A")
        self.assertEqual(dashboard["active_work"]["key"], "399A")
        self.assertEqual(dashboard["active_work"]["subitem_key"], "399A")
        self.assertIsNone(dashboard["active_work"]["primary_pr"])
        self.assertNotEqual(dashboard["roadmap"]["effective_active"], "412")
        self.assertNotEqual(dashboard["roadmap"]["effective_active"], "ASTRA-399")
        self.assertIn("399A", dashboard["next_action"])
        self.assertEqual(
            dashboard["handoff"]["packs"]["developer"]["section"]["title"],
            "#399A — état persistant — DONE (PR #413)",
        )
        self.assertNotIn("#412", dashboard["handoff"]["packs"]["developer"]["prompt"])
        self.assertEqual(dashboard["attention"]["status"], "ACTION")
        self.assertEqual(dashboard["attention"]["items"][0]["key"], "399A")

    async def test_canonical_active_slice_ignores_prior_pr_out_of_scope_mention(self):
        prior_276c = {
            "number": 438,
            "title": "276C — quorum et finalisation atomique",
            "body": (
                "Implémente #276C.\n\n"
                "Closes the 276C implementation slice of #276. "
                "276D is intentionally out of scope."
            ),
            "head": {"ref": "276C-quorum-finalization-main", "sha": "pr438"},
            "base": {"ref": "main"},
            "merged_at": "2026-09-25T00:53:52Z",
            "html_url": "https://github.test/pull/438",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            query = dict(request.url.params)
            if path == "/repos/tchi99/RessourcePlanner/issues/55":
                return response({
                    "number": 55,
                    "title": "Roadmap maître",
                    "body": CANONICAL_276D_ROADMAP,
                    "html_url": "https://github.test/issues/55",
                    "updated_at": "2026-09-25T00:55:28Z",
                    "state": "open",
                })
            if path == "/repos/tchi99/RessourcePlanner/issues/276":
                return response({
                    "number": 276,
                    "title": "Multi-approbation par ligne",
                    "body": ISSUE_276_276D_READY,
                    "state": "open",
                    "html_url": "https://github.test/issues/276",
                    "updated_at": "2026-09-25T00:54:25Z",
                })
            if path == "/repos/tchi99/RessourcePlanner/pulls":
                return response([prior_276c] if query.get("state") == "closed" else [])
            if path == "/repos/tchi99/RessourcePlanner/commits":
                return response([{
                    "sha": "main-after-438",
                    "html_url": "https://github.test/commit/main-after-438",
                    "commit": {
                        "message": "276C: quorum and atomic approval finalization (#438)",
                        "author": {"date": "2026-09-25T00:53:50Z"},
                    },
                }])
            if path == "/repos/tchi99/RessourcePlanner/contents/AGENTS.md":
                return response(encoded_file(AGENTS))
            if path == "/repos/tchi99/RessourcePlanner/contents/docs/architecture":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/branches":
                return response([
                    {"name": "main", "commit": {"sha": "main-after-438"}},
                ])
            if path == "/repos/tchi99/RessourcePlanner/pulls/438/files":
                raise AssertionError(
                    "Une mention incidente de 276D ne doit même pas qualifier PR #438 comme candidate."
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url} {query}")

        settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            stalled_after_minutes=20,
            github_api_url="https://api.github.test",
        )
        client = GitHubClient(settings, transport=httpx.MockTransport(handler))
        try:
            dashboard = await build_dashboard(client, settings, "tchi99/RessourcePlanner")
        finally:
            await client.close()

        self.assertEqual(dashboard["pipeline"]["now"]["key"], "276D")
        self.assertEqual(dashboard["active_work"]["key"], "276D")
        self.assertIsNone(dashboard["active_work"]["primary_pr"])
        self.assertIsNone(dashboard["active_work"]["merged_but_unmarked_pr"])
        self.assertEqual(dashboard["execution"]["phase"], "READY")
        self.assertEqual(dashboard["attention"]["status"], "ACTION")
        self.assertEqual(dashboard["attention"]["items"][0]["role"], "developer")
        self.assertEqual(
            dashboard["attention"]["items"][0]["title"],
            "Tranche prête à démarrer",
        )
        self.assertIn("276D", dashboard["execution"]["next_action"])
        self.assertNotIn("fusionnée", dashboard["execution"]["summary"].lower())

    async def test_invalid_canonical_pipeline_fails_closed_without_active_issue(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/repos/tchi99/RessourcePlanner/issues/55":
                return response({
                    "number": 55,
                    "title": "Roadmap maître",
                    "body": INVALID_CANONICAL_ROADMAP,
                    "html_url": "https://github.test/issues/55",
                    "updated_at": "2026-09-24T01:00:00Z",
                    "state": "open",
                })
            if path == "/repos/tchi99/RessourcePlanner/pulls":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/commits":
                return response([{
                    "sha": "main-invalid",
                    "html_url": "https://github.test/commit/main-invalid",
                    "commit": {"message": "main", "author": {"date": "2026-09-24T01:00:00Z"}},
                }])
            if path == "/repos/tchi99/RessourcePlanner/contents/AGENTS.md":
                return response(encoded_file(AGENTS))
            if path == "/repos/tchi99/RessourcePlanner/contents/docs/architecture":
                return response([])
            if path == "/repos/tchi99/RessourcePlanner/branches":
                return response([{"name": "main", "commit": {"sha": "main-invalid"}}])
            if path.startswith("/repos/tchi99/RessourcePlanner/issues/"):
                raise AssertionError("Pipeline invalide: aucune issue active ne doit être inventée.")
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            stalled_after_minutes=20,
            github_api_url="https://api.github.test",
        )
        client = GitHubClient(settings, transport=httpx.MockTransport(handler))
        try:
            dashboard = await build_dashboard(client, settings, "tchi99/RessourcePlanner")
        finally:
            await client.close()

        self.assertFalse(dashboard["pipeline"]["valid"])
        self.assertEqual(dashboard["pipeline"]["source"], "canonical_v1")
        self.assertIsNone(dashboard["pipeline"]["now"])
        self.assertEqual(dashboard["execution"]["phase"], "PIPELINE_INVALID")
        self.assertEqual(
            dashboard["execution"]["missions"]["developer"]["state"],
            "blocked",
        )
        self.assertEqual(
            dashboard["handoff"]["packs"]["developer"]["confidence"],
            "BLOCKED",
        )
        self.assertEqual(dashboard["attention"]["status"], "ACTION")
        self.assertEqual(
            dashboard["attention"]["items"][0]["role"],
            "product-owner",
        )
        self.assertIsNone(dashboard["roadmap"]["active_issue"])
        self.assertIsNone(dashboard["roadmap"]["effective_active"])
        self.assertIsNone(dashboard["active_work"])
        self.assertIn("invalide", dashboard["next_action"].lower())
        self.assertIn("ne démarre", dashboard["dev_prompt"].lower())


if __name__ == "__main__":
    unittest.main()
