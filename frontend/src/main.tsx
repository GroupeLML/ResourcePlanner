/// <reference types="vite/client" />

import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import { AuthProvider } from "./AuthContext";
import { ViewScopeProvider } from "./ViewScopeContext";
import "./styles.css";
import "./shift-editor.css";
import "./quick-shift.css";
import "./demands.css";\nimport "./searchable-combobox.css";
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

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthProvider>
      <ViewScopeProvider>
        <App />
      </ViewScopeProvider>
    </AuthProvider>
  </React.StrictMode>,
);