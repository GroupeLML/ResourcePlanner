import { useEffect, useMemo, useState } from "react";

import { useAuth } from "./AuthContext";
import CommunicationsPage from "./CommunicationsPage";
import CoordinatorDashboardPage from "./CoordinatorDashboardPage";
import ConfigurationPage from "./ConfigurationPage";
import DevUserSwitcher from "./DevUserSwitcher";
import DeliveryPage from "./DeliveryPage";
import DemandsWorkspace from "./DemandsWorkspace";
import MediumTermPage from "./MediumTermPage";
import PlanningPage from "./PlanningPage";
import ProjectsPage from "./ProjectsPage";
import ResourcesPage from "./ResourcesPage";
import TechnicianSchedulePage from "./TechnicianSchedulePage";
import UserAdminPage from "./UserAdminPage";
import { useViewScope } from "./ViewScopeContext";

type View = "my-schedule" | "coordinator-dashboard" | "planning" | "medium-term" | "demands" | "projects" | "delivery" | "resources" | "users" | "communications" | "configuration";

type NavItem = { key: View; label: string; eyebrow: string; shortLabel: string; permission?: string; role?: string };

const navItems: NavItem[] = [
  { key: "my-schedule", label: "Mon horaire", eyebrow: "Personnel", shortLabel: "MH" },
  { key: "coordinator-dashboard", label: "Coordonnateur", eyebrow: "Pilotage", shortLabel: "TC", role: "COORDINATOR" },
  { key: "planning", label: "Planning opérationnel", eyebrow: "Semaine", shortLabel: "PL" },
  { key: "medium-term", label: "Moyen terme", eyebrow: "Capacité", shortLabel: "MT" },
  { key: "demands", label: "Demandes", eyebrow: "Main-d’œuvre", shortLabel: "DE" },
  { key: "projects", label: "Projets", eyebrow: "Portefeuille", shortLabel: "PR" },
  { key: "delivery", label: "Delivery", eyebrow: "WorkPackages", shortLabel: "DL" },
  { key: "communications", label: "Communications", eyebrow: "Révision", shortLabel: "CO", permission: "manage_communications" },
  { key: "resources", label: "Ressources", eyebrow: "Administration", shortLabel: "RE", permission: "manage_resources" },
  { key: "users", label: "Utilisateurs", eyebrow: "Sécurité", shortLabel: "UT", permission: "admin_users" },
  { key: "configuration", label: "Configuration", eyebrow: "Administration", shortLabel: "CF", permission: "admin_settings" },
];

const SIDEBAR_COMPACT_STORAGE_KEY = "resourceplanner.sidebar.compact";

function displayInitials(displayName: string) {
  const parts = displayName.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  return parts.slice(0, 2).map((part) => part[0]?.toUpperCase() ?? "").join("");
}

function Placeholder({ view }: { view: View }) {
  const item = navItems.find((entry) => entry.key === view)!;
  return (
    <section className="placeholder-panel">
      <span className="eyebrow">{item.eyebrow}</span>
      <h2>{item.label}</h2>
      <p>
        Cette section fait partie du shell React V2. Elle sera branchée progressivement
        aux contrats FastAPI après la stabilisation du planning opérationnel.
      </p>
    </section>
  );
}

export default function App() {
  const {
    principal,
    loading: authLoading,
    error: authError,
    authenticationRequired,
    can,
    login,
    logout,
  } = useAuth();
  const { setScope } = useViewScope();
  const [view, setView] = useState<View>("planning");
  const [demandToOpen, setDemandToOpen] = useState<string | null>(null);
  const [initialViewResolved, setInitialViewResolved] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCompact, setSidebarCompact] = useState(() => {
    try {
      return window.localStorage.getItem(SIDEBAR_COMPACT_STORAGE_KEY) === "true";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    if (!principal || initialViewResolved) return;
    if (principal.roles.length === 1 && principal.roles.includes("TECHNICIAN")) {
      setView("my-schedule");
    }
    setInitialViewResolved(true);
  }, [principal, initialViewResolved]);

  useEffect(() => {
    try {
      window.localStorage.setItem(SIDEBAR_COMPACT_STORAGE_KEY, String(sidebarCompact));
    } catch {
      // The shell remains usable when browser storage is unavailable.
    }
  }, [sidebarCompact]);

  const visibleNavItems = useMemo(
    () => navItems.filter((item) => (
      (!item.permission || can(item.permission))
      && (!item.role || Boolean(principal?.roles.includes(item.role)))
    )),
    [can, principal?.roles],
  );

  if (authLoading) {
    return (
      <main className="main-content">
        <section className="placeholder-panel">
          <span className="eyebrow">Authentification</span>
          <h2>Chargement de votre session…</h2>
        </section>
      </main>
    );
  }

  if (authenticationRequired || (!principal && !authError)) {
    return (
      <main className="main-content">
        <section className="placeholder-panel">
          <span className="eyebrow">Authentification</span>
          <h2>Connexion requise</h2>
          <p>Votre session RessourcePlanner est absente ou expirée.</p>
          <div className="week-navigation">
            <button type="button" onClick={login}>Se connecter avec Acumatica</button>
          </div>
        </section>
      </main>
    );
  }

  if (authError || !principal) {
    return (
      <main className="main-content">
        <section className="error-panel">
          <strong>Impossible d’ouvrir la session RessourcePlanner.</strong>
          <span>{authError || "Aucune identité active n’a été retournée par le serveur."}</span>
          <small>Les permissions sont déterminées par FastAPI; React ne peut pas contourner cette étape.</small>
        </section>
      </main>
    );
  }

  const currentItem = navItems.find((item) => item.key === view);

  function openDemand(number: string) {
    setScope("global");
    setDemandToOpen(number);
    setView("demands");
  }

  function openDemands() {
    setDemandToOpen(null);
    setView("demands");
  }

  return (
    <div className={`app-shell ${sidebarCompact ? "is-sidebar-compact" : ""}`}>
      <aside className={`app-sidebar ${sidebarOpen ? "is-open" : ""} ${sidebarCompact ? "is-compact" : ""}`}>
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true">RP</div>
          <div className="brand-copy">
            <strong>RessourcePlanner</strong>
            <span>Planification industrielle</span>
          </div>
          <button
            type="button"
            className="sidebar-collapse-button"
            onClick={() => setSidebarCompact((value) => !value)}
            aria-label={sidebarCompact ? "Déployer la navigation" : "Réduire la navigation"}
            title={sidebarCompact ? "Déployer la navigation" : "Réduire la navigation"}
          >
            <span aria-hidden="true">{sidebarCompact ? "›" : "‹"}</span>
          </button>
        </div>

        <nav className="main-nav" aria-label="Navigation principale">
          {visibleNavItems.map((item) => (
            <button
              type="button"
              key={item.key}
              className={view === item.key ? "active" : ""}
              onClick={() => {
                if (item.key === "demands") setDemandToOpen(null);
                setView(item.key);
                setSidebarOpen(false);
              }}
              aria-label={item.label}
              title={sidebarCompact ? item.label : undefined}
            >
              <span className="nav-short" aria-hidden="true">{item.shortLabel}</span>
              <div className="nav-copy">
                <span>{item.eyebrow}</span>
                <strong>{item.label}</strong>
              </div>
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="sidebar-footer-copy">
            <span>{principal.display_name}</span>
            <strong>{principal.roles.join(" · ")}</strong>
          </div>
          <span
            className="sidebar-identity-compact"
            aria-label={`Utilisateur actif : ${principal.display_name}`}
            title={`${principal.display_name} · ${principal.roles.join(" · ")}`}
          >
            {displayInitials(principal.display_name)}
          </span>
          {principal.auth_mode === "oidc" && (
            <button
              type="button"
              className="sidebar-logout-button"
              onClick={() => void logout().catch(() => undefined)}
              aria-label="Déconnexion"
              title={sidebarCompact ? "Déconnexion" : undefined}
            >
              <span className="sidebar-logout-label">Déconnexion</span>
              <span className="sidebar-logout-short" aria-hidden="true">↪</span>
            </button>
          )}
        </div>
      </aside>

      {sidebarOpen && (
        <button
          type="button"
          className="sidebar-backdrop"
          aria-label="Fermer la navigation"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <div className="app-content">
        <header className="topbar">
          <button
            type="button"
            className="menu-button"
            onClick={() => setSidebarOpen((value) => !value)}
            aria-label={sidebarOpen ? "Fermer la navigation" : "Ouvrir la navigation"}
            aria-expanded={sidebarOpen}
          >
            ☰
          </button>
          <div>
            <span className="topbar-context">RessourcePlanner V2</span>
            <strong>{currentItem?.label}</strong>
          </div>
          <DevUserSwitcher />
          <div className="topbar-status" title={`Mode d’authentification: ${principal.auth_mode}`}>
            <span className="status-dot" />
            {principal.display_name}
          </div>
        </header>

        <main className="main-content">
          {view === "my-schedule" ? (
            <TechnicianSchedulePage />
          ) : view === "coordinator-dashboard" && principal.roles.includes("COORDINATOR") ? (
            <CoordinatorDashboardPage
              onOpenDemand={openDemand}
              onOpenDemands={openDemands}
              onOpenPlanning={() => setView("planning")}
            />
          ) : view === "planning" ? (
            <PlanningPage onOpenDemands={() => setView("demands")} />
          ) : view === "medium-term" ? (
            <MediumTermPage onOpenDemands={() => setView("demands")} />
          ) : view === "demands" ? (
            demandToOpen ? (
              <DemandsWorkspace initialDemandNumber={demandToOpen} />
            ) : (
              <DemandsWorkspace />
            )
          ) : view === "projects" ? (
            <ProjectsPage />
          ) : view === "delivery" ? (
            <DeliveryPage />
          ) : view === "communications" && can("manage_communications") ? (
            <CommunicationsPage />
          ) : view === "resources" && can("manage_resources") ? (
            <ResourcesPage />
          ) : view === "users" && can("admin_users") ? (
            <UserAdminPage />
          ) : view === "configuration" && can("admin_settings") ? (
            <ConfigurationPage />
          ) : (
            <Placeholder view={view} />
          )}
        </main>
      </div>
    </div>
  );
}
