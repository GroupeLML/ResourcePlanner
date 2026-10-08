/// <reference types="vite/client" />

import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import { AuthProvider, useAuth } from "./AuthContext";
import { ViewScopeProvider } from "./ViewScopeContext";
import "./styles.css";
import "./shift-editor.css";
import "./quick-shift.css";
import "./demands.css";
import "./searchable-combobox.css";
import "./demand-history.css";
import "./demand-detail.css";
import "./demand-periods.css";
import "./demand-workflow.css";
import "./medium-term.css";
import "./projects.css";
import "./delivery.css";
import "./resource-admin.css";
import "./business-contacts.css";
import "./segments.css";
import "./segment-parent.css";
import "./technician-schedule.css";
import "./communications.css";
import "./coordinator-dashboard.css";
import "./configuration.css";
import "./planning-history.css";
import "./overallocation.css";

// A change of server-resolved identity invalidates every view, dialog and in-flight
// request owned by the former user (not only the visible navigation tab).
function SessionScopedApp() {
  const { principal } = useAuth();
  const identityKey = JSON.stringify(principal && [
    principal.local_user_id,
    principal.issuer,
    principal.subject,
    principal.auth_mode,
    principal.roles,
    principal.permissions,
  ]);
  return (
    <ViewScopeProvider key={identityKey}>
      <App />
    </ViewScopeProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthProvider>
      <SessionScopedApp />
    </AuthProvider>
  </React.StrictMode>,
);