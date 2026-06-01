"use client";

/**
 * Auth context for the Pronaos admin UI.
 *
 * Identity model: API-key bearer. The user pastes a Pronaos API key
 * at /login; we persist it via setStoredToken and re-hydrate on app
 * start. There's no separate session token — every request carries
 * the API key the user supplied. Logout clears localStorage and
 * redirects to /login.
 *
 * Why localStorage and not httponly cookies: Pronaos's backend uses
 * bearer-token auth at the API layer; rewriting it to issue session
 * cookies would force a fork in the auth code path. Trade-off:
 * vulnerable to XSS exfiltration. Mitigated by strict CSP at the
 * Next.js layer (added in Phase 71 polish) + the same key the user
 * already pastes into curl / postman / IDE configs.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { getStoredToken, setStoredToken } from "@/lib/api/client";

export type AuthStatus = "loading" | "authenticated" | "unauthenticated";

export type AuthContextValue = {
  status: AuthStatus;
  /** The bearer token currently in use, or null. */
  token: string | null;
  /** Persist a new token + flip status to authenticated. */
  signIn: (token: string) => void;
  /** Clear the persisted token + flip status to unauthenticated. */
  signOut: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [token, setToken] = useState<string | null>(null);

  // Hydrate from localStorage on mount.
  useEffect(() => {
    const stored = getStoredToken();
    if (stored) {
      setToken(stored);
      setStatus("authenticated");
    } else {
      setStatus("unauthenticated");
    }
  }, []);

  const signIn = useCallback((nextToken: string) => {
    setStoredToken(nextToken);
    setToken(nextToken);
    setStatus("authenticated");
  }, []);

  const signOut = useCallback(() => {
    setStoredToken(null);
    setToken(null);
    setStatus("unauthenticated");
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ status, token, signIn, signOut }),
    [status, token, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/**
 * Hook to read + mutate auth state. Throws when used outside
 * AuthProvider so the failure is loud + easy to fix.
 */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used inside <AuthProvider>");
  }
  return ctx;
}
