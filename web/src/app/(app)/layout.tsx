import { AppShell } from "@/components/layout/app-shell";

/**
 * Layout for the (app) route group. Every page here lives inside
 * the authenticated app shell (top nav + side nav). The shell
 * itself handles the unauthenticated-redirect-to-login flow.
 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
