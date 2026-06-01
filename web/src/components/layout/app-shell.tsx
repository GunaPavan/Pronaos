"use client";

import { useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { SideNav } from "@/components/layout/side-nav";
import { TopNav } from "@/components/layout/top-nav";
import { useAuth } from "@/lib/auth/context";

/**
 * The authenticated-only shell: top nav + side nav + main content.
 * Wraps every page under the (app) route group. Redirects to /login
 * when auth status flips to unauthenticated.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status === "unauthenticated") {
      router.replace("/login");
    }
  }, [status, router]);

  if (status !== "authenticated") {
    return (
      <div
        className="flex min-h-screen items-center justify-center text-sm text-muted-foreground"
        role="status"
        aria-live="polite"
      >
        Checking session…
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col">
      <TopNav />
      <div className="flex flex-1 overflow-hidden">
        <SideNav />
        <main
          className="flex-1 overflow-y-auto px-6 py-6"
          role="main"
        >
          {children}
        </main>
      </div>
    </div>
  );
}
