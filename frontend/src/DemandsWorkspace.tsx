import { useEffect, useState } from "react";

import { useAuth } from "./AuthContext";
import DemandSegmentsPage from "./DemandSegmentsPage";
import DemandsPage, { type DemandCreateContext } from "./DemandsPage";
import EmergencyOverridePage from "./EmergencyOverridePage";

type DemandWorkspaceView = "requests" | "segments" | "emergency";

type DemandsWorkspaceProps = {
  initialDemandNumber?: string | null;
  initialCreateContext?: DemandCreateContext | null;
};

export default function DemandsWorkspace({
  initialDemandNumber = null,
  initialCreateContext = null,
}: DemandsWorkspaceProps) {
  const { can } = useAuth();
  const canManagePlanning = can("manage_planning");
  const canApprove = can("approve_demands");
  const [view, setView] = useState<DemandWorkspaceView>("requests");

  useEffect(() => {
    if (view === "segments" && !canManagePlanning) setView("requests");
    if (view === "emergency" && !canApprove) setView("requests");
  }, [view, canManagePlanning, canApprove]);

  return (
    <div className="demands-workspace-shell">
      <nav className="demands-subnav" aria-label="Sections des demandes">
        <button
          type="button"
          className={view === "requests" ? "active" : ""}
          onClick={() => setView("requests")}
        >
          Demandes
        </button>
        {canManagePlanning && (
          <button
            type="button"
            className={view === "segments" ? "active" : ""}
            onClick={() => setView("segments")}
          >
            Segments
          </button>
        )}
        {canApprove && (
          <button
            type="button"
            className={view === "emergency" ? "active" : ""}
            onClick={() => setView("emergency")}
          >
            Urgence
          </button>
        )}
      </nav>

      {view === "requests" ? (
        initialDemandNumber ? (
          <DemandsPage initialDemandNumber={initialDemandNumber} />
        ) : initialCreateContext ? (
          <DemandsPage initialCreateContext={initialCreateContext} />
        ) : (
          <DemandsPage />
        )
      ) : view === "segments" ? (
        <DemandSegmentsPage />
      ) : (
        <EmergencyOverridePage />
      )}
    </div>
  );
}
