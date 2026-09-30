import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { ApiError } from "./api";
import {
  AuthPrincipal,
  getCurrentPrincipal,
  getLoginUrl,
  loginBreakGlass as requestBreakGlassLogin,
  logoutCurrentSession,
} from "./auth-api";

type AuthState = {
  principal: AuthPrincipal | null;
  loading: boolean;
  error: string | null;
  authenticationRequired: boolean;
  can: (permission: string) => boolean;
  login: () => void;
  breakGlassLogin: (loginName: string, secret: string) => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [principal, setPrincipal] = useState<AuthPrincipal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [authenticationRequired, setAuthenticationRequired] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setAuthenticationRequired(false);
    getCurrentPrincipal(controller.signal)
      .then((identity) => {
        setPrincipal(identity);
        setAuthenticationRequired(false);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setPrincipal(null);
        if (reason instanceof ApiError && reason.code === "authentication_required") {
          setAuthenticationRequired(true);
          setError(null);
          return;
        }
        if (reason instanceof ApiError) {
          setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
          return;
        }
        setError(reason instanceof Error ? reason.message : "Impossible de charger l’identité courante.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  const login = useCallback(() => {
    window.location.assign(getLoginUrl());
  }, []);

  const breakGlassLogin = useCallback(async (loginName: string, secret: string) => {
    setError(null);
    try {
      const identity = await requestBreakGlassLogin(loginName, secret);
      setPrincipal(identity);
      setAuthenticationRequired(false);
    } catch (reason: unknown) {
      setPrincipal(null);
      setAuthenticationRequired(true);
      if (reason instanceof ApiError) {
        setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
      } else {
        setError(reason instanceof Error ? reason.message : "Connexion administrateur de secours impossible.");
      }
      throw reason;
    }
  }, []);

  const logout = useCallback(async () => {
    setError(null);
    try {
      await logoutCurrentSession();
      setPrincipal(null);
      setAuthenticationRequired(true);
    } catch (reason: unknown) {
      if (reason instanceof ApiError) {
        setError(`${reason.message}${reason.code ? ` (${reason.code})` : ""}`);
      } else {
        setError(reason instanceof Error ? reason.message : "Impossible de fermer la session.");
      }
      throw reason;
    }
  }, []);

  const value = useMemo<AuthState>(() => ({
    principal,
    loading,
    error,
    authenticationRequired,
    can: (permission: string) => Boolean(principal?.permissions.includes(permission)),
    login,
    breakGlassLogin,
    logout,
  }), [principal, loading, error, authenticationRequired, login, breakGlassLogin, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth doit être utilisé sous AuthProvider");
  return context;
}
