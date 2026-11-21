import {
  ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { ApiError } from "./api";
import { useAuth } from "./AuthContext";
import {
  EmergencyActiveBroadcast,
  acknowledgeEmergencyBroadcast,
  getEmergencyRuntimeStatus,
} from "./emergencyBroadcastApi";

type KnownBroadcast = EmergencyActiveBroadcast & {
  content: NonNullable<EmergencyActiveBroadcast["content"]>;
};

const POLL_MIN_MS = 4_000;
const POLL_MAX_MS = 6_000;
const RESUME_MAX_MS = 1_000;
const STATUS_TIMEOUT_MS = 3_000;
const ACK_TIMEOUT_MS = 10_000;
const BACKOFF_MS = [10_000, 20_000, 40_000, 60_000] as const;

function randomBetween(minimum: number, maximum: number) {
  return Math.floor(minimum + Math.random() * (maximum - minimum));
}

function jitterBackoff(value: number) {
  const spread = value * 0.2;
  return Math.max(1_000, Math.floor(value - spread + Math.random() * spread * 2));
}

function formatActivation(value: string) {
  try {
    return new Intl.DateTimeFormat("fr-CA", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(value));
  } catch {
    return value;
  }
}

function isAbort(reason: unknown) {
  return reason instanceof DOMException && reason.name === "AbortError";
}

function errorMessage(reason: unknown, fallback: string) {
  if (reason instanceof ApiError) {
    return `${reason.message}${reason.code ? ` (${reason.code})` : ""}`;
  }
  return reason instanceof Error ? reason.message : fallback;
}

function EmergencyDialog({
  broadcast,
  acknowledging,
  error,
  reconnecting,
  onAcknowledge,
}: {
  broadcast: KnownBroadcast;
  acknowledging: boolean;
  error: string | null;
  reconnecting: boolean;
  onAcknowledge: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const actionRef = useRef<HTMLButtonElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    actionRef.current?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        actionRef.current?.focus();
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable?.length) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      const previous = previousFocusRef.current;
      if (previous?.isConnected) previous.focus();
    };
  }, [broadcast.id]);

  return (
    <div
      className="emergency-broadcast-layer"
      data-testid="emergency-broadcast-layer"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          event.preventDefault();
          actionRef.current?.focus();
        }
      }}
    >
      <div
        ref={dialogRef}
        className="emergency-broadcast-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="emergency-broadcast-title"
        aria-describedby="emergency-broadcast-message emergency-broadcast-time"
      >
        <span className="emergency-broadcast-eyebrow">Avis urgent</span>
        <h2 id="emergency-broadcast-title">{broadcast.content.title}</h2>
        <p id="emergency-broadcast-message" className="emergency-broadcast-message">
          {broadcast.content.message}
        </p>
        <p id="emergency-broadcast-time" className="emergency-broadcast-time">
          Diffusé le {formatActivation(broadcast.activated_at)}
        </p>

        {(error || reconnecting) && (
          <div className="emergency-broadcast-retry" role="status" aria-live="polite">
            {error
              ? `La confirmation n’a pas été enregistrée. ${error}`
              : "Connexion temporairement indisponible. Le message reste affiché jusqu’à confirmation du serveur."}
          </div>
        )}

        <button
          ref={actionRef}
          type="button"
          className="emergency-broadcast-ack"
          disabled={acknowledging}
          onClick={onAcknowledge}
        >
          {acknowledging ? "Confirmation…" : "J’ai compris"}
        </button>
      </div>
    </div>
  );
}

export default function EmergencyBroadcastProvider({
  children,
}: {
  children: ReactNode;
}) {
  const { principal, loading } = useAuth();
  const [broadcast, setBroadcastState] = useState<KnownBroadcast | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [ackError, setAckError] = useState<string | null>(null);
  const [acknowledgingId, setAcknowledgingId] = useState<string | null>(null);

  const broadcastRef = useRef<KnownBroadcast | null>(null);
  const timerRef = useRef<number | null>(null);
  const statusControllerRef = useRef<AbortController | null>(null);
  const ackControllerRef = useRef<AbortController | null>(null);
  const lifecycleGenerationRef = useRef(0);
  const requestGenerationRef = useRef(0);
  const failureCountRef = useRef(0);
  const contentCacheRef = useRef<Map<string, KnownBroadcast>>(new Map());
  const locallyAcknowledgedRef = useRef<Set<string>>(new Set());
  const pollRef = useRef<(() => Promise<void>) | null>(null);
  const mountedRef = useRef(true);

  const identityKey = useMemo(
    () => principal
      ? JSON.stringify([
        principal.local_user_id,
        principal.issuer,
        principal.subject,
        principal.auth_mode,
      ])
      : "",
    [principal],
  );

  const setBroadcast = useCallback((value: KnownBroadcast | null) => {
    broadcastRef.current = value;
    setBroadcastState(value);
  }, []);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const schedule = useCallback((delayMs: number, work: () => void) => {
    clearTimer();
    timerRef.current = window.setTimeout(work, delayMs);
  }, [clearTimer]);

  const resetIdentityState = useCallback(() => {
    clearTimer();
    statusControllerRef.current?.abort();
    ackControllerRef.current?.abort();
    statusControllerRef.current = null;
    ackControllerRef.current = null;
    lifecycleGenerationRef.current += 1;
    requestGenerationRef.current = 0;
    failureCountRef.current = 0;
    contentCacheRef.current.clear();
    locallyAcknowledgedRef.current.clear();
    pollRef.current = null;
    setBroadcast(null);
    setPollError(null);
    setAckError(null);
    setAcknowledgingId(null);
  }, [clearTimer, setBroadcast]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => resetIdentityState, [identityKey, resetIdentityState]);

  useEffect(() => {
    if (loading || !principal?.local_user_id) return undefined;

    const lifecycleGeneration = lifecycleGenerationRef.current;
    let stopped = false;

    const poll = async () => {
      if (stopped || lifecycleGenerationRef.current !== lifecycleGeneration) return;
      if (statusControllerRef.current) return;

      const requestGeneration = ++requestGenerationRef.current;
      const controller = new AbortController();
      statusControllerRef.current = controller;
      let timedOut = false;
      const timeout = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, STATUS_TIMEOUT_MS);

      try {
        const current = broadcastRef.current;
        const known = current ? { id: current.id, version: current.version } : null;
        let status = await getEmergencyRuntimeStatus(controller.signal, known);

        if (
          status.active_broadcast
          && status.active_broadcast.content === null
          && !contentCacheRef.current.has(status.active_broadcast.id)
        ) {
          status = await getEmergencyRuntimeStatus(controller.signal, null);
        }

        if (
          stopped
          || lifecycleGenerationRef.current !== lifecycleGeneration
          || requestGeneration !== requestGenerationRef.current
        ) {
          return;
        }

        failureCountRef.current = 0;
        setPollError(null);

        const active = status.active_broadcast;
        if (!active) {
          setBroadcast(null);
          setAckError(null);
        } else {
          const cached = contentCacheRef.current.get(active.id);
          const resolved = active.content
            ? ({ ...active, content: active.content } as KnownBroadcast)
            : cached
              ? ({ ...cached, ...active, content: cached.content } as KnownBroadcast)
              : null;

          if (resolved) {
            contentCacheRef.current.set(resolved.id, resolved);
            const acknowledged = Boolean(active.acknowledged_at)
              || locallyAcknowledgedRef.current.has(active.id);
            if (acknowledged) {
              if (broadcastRef.current?.id === active.id) setBroadcast(null);
              setAckError(null);
            } else {
              setBroadcast(resolved);
            }
          }
        }

        schedule(randomBetween(POLL_MIN_MS, POLL_MAX_MS), () => {
          void pollRef.current?.();
        });
      } catch (reason) {
        if (
          stopped
          || lifecycleGenerationRef.current !== lifecycleGeneration
          || (isAbort(reason) && !timedOut)
        ) {
          return;
        }

        const index = Math.min(failureCountRef.current, BACKOFF_MS.length - 1);
        failureCountRef.current += 1;
        setPollError(errorMessage(
          reason,
          timedOut
            ? "Le contrôle d’urgence a dépassé son délai. Une nouvelle tentative sera effectuée."
            : "Impossible de confirmer l’état de la diffusion d’urgence.",
        ));
        schedule(jitterBackoff(BACKOFF_MS[index]), () => {
          void pollRef.current?.();
        });
      } finally {
        window.clearTimeout(timeout);
        if (statusControllerRef.current === controller) {
          statusControllerRef.current = null;
        }
      }
    };

    pollRef.current = poll;

    const expedite = () => {
      if (stopped || lifecycleGenerationRef.current !== lifecycleGeneration) return;
      if (statusControllerRef.current) return;
      schedule(randomBetween(0, RESUME_MAX_MS), () => {
        void pollRef.current?.();
      });
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") expedite();
    };

    window.addEventListener("focus", expedite);
    window.addEventListener("online", expedite);
    window.addEventListener("pageshow", expedite);
    document.addEventListener("visibilitychange", onVisibility);
    schedule(0, () => void poll());

    return () => {
      stopped = true;
      pollRef.current = null;
      clearTimer();
      statusControllerRef.current?.abort();
      window.removeEventListener("focus", expedite);
      window.removeEventListener("online", expedite);
      window.removeEventListener("pageshow", expedite);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [
    clearTimer,
    identityKey,
    loading,
    principal?.local_user_id,
    schedule,
    setBroadcast,
  ]);

  const acknowledge = useCallback(async () => {
    const target = broadcastRef.current;
    if (!target || acknowledgingId) return;

    setAcknowledgingId(target.id);
    setAckError(null);

    const lifecycleGeneration = lifecycleGenerationRef.current;
    const controller = new AbortController();
    ackControllerRef.current = controller;
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, ACK_TIMEOUT_MS);

    try {
      const result = await acknowledgeEmergencyBroadcast(target.id, controller.signal);
      if (
        !mountedRef.current
        || lifecycleGenerationRef.current !== lifecycleGeneration
      ) {
        return;
      }

      const acknowledgedId = result.acknowledgment.broadcast_id;
      locallyAcknowledgedRef.current.add(acknowledgedId);
      if (broadcastRef.current?.id === acknowledgedId) {
        setBroadcast(null);
      }
      setAckError(null);

      schedule(randomBetween(POLL_MIN_MS, POLL_MAX_MS), () => {
        void pollRef.current?.();
      });
    } catch (reason) {
      if (
        lifecycleGenerationRef.current !== lifecycleGeneration
        || (isAbort(reason) && !timedOut)
      ) {
        return;
      }
      setAckError(errorMessage(
        reason,
        timedOut
          ? "Le serveur n’a pas confirmé la réception avant le délai. Réessayez."
          : "Le serveur n’a pas confirmé la réception. Réessayez.",
      ));
    } finally {
      window.clearTimeout(timeout);
      if (ackControllerRef.current === controller) {
        ackControllerRef.current = null;
      }
      if (mountedRef.current && lifecycleGenerationRef.current === lifecycleGeneration) {
        setAcknowledgingId(null);
      }
    }
  }, [acknowledgingId, schedule, setBroadcast]);

  useEffect(() => {
    if (!broadcast) return undefined;
    const root = document.getElementById("resourceplanner-application");
    if (!root) return undefined;

    const inertRoot = root as HTMLElement & { inert: boolean };
    inertRoot.inert = true;
    root.setAttribute("aria-hidden", "true");
    return () => {
      inertRoot.inert = false;
      root.removeAttribute("aria-hidden");
    };
  }, [broadcast]);

  if (!principal?.local_user_id) {
    return <>{children}</>;
  }

  return (
    <>
      <div id="resourceplanner-application">{children}</div>
      {broadcast && (
        <EmergencyDialog
          broadcast={broadcast}
          acknowledging={acknowledgingId === broadcast.id}
          error={ackError}
          reconnecting={Boolean(pollError)}
          onAcknowledge={() => void acknowledge()}
        />
      )}
    </>
  );
}
