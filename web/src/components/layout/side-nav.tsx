"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  Beaker,
  GaugeCircle,
  KeyRound,
  Layers,
  LayoutDashboard,
  Settings,
  ShieldCheck,
  Stethoscope,
  Users,
  Webhook,
  Workflow,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Side navigation. The pages it links to don't all exist yet — they
 * arrive across Phases 63-71. Each link is rendered even when the
 * page is unbuilt; clicking lands on a friendly "Coming in Phase XX"
 * placeholder until that phase ships.
 */
type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Phase the destination ships in. Surfaced on the placeholder. */
  phase?: number;
  /** Visually indented child of the preceding top-level item. */
  sub?: boolean;
};

const ITEMS: NavItem[] = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/tenants", label: "Tenants", icon: Users },
  { href: "/teams", label: "Teams", icon: Users },
  { href: "/keys", label: "API Keys", icon: KeyRound },
  { href: "/usage", label: "Usage & Budgets", icon: GaugeCircle },
  { href: "/playground", label: "Playground", icon: Beaker },
  { href: "/routing", label: "Routing & Quality", icon: Workflow },
  { href: "/routing/observations", label: "Observations", icon: Activity, sub: true },
  { href: "/routing/ab-tests", label: "A/B Tests", icon: Beaker, sub: true },
  { href: "/guardrails", label: "Guardrails & Audit", icon: ShieldCheck },
  { href: "/batches", label: "Batches", icon: Layers },
  { href: "/providers", label: "Providers & Cache", icon: Activity },
  { href: "/doctor", label: "Doctor", icon: Stethoscope },
  { href: "/webhooks", label: "Webhooks", icon: Webhook },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function SideNav() {
  const pathname = usePathname();

  return (
    <aside
      className="hidden md:flex md:w-60 md:flex-col border-r bg-card/30"
      role="navigation"
      aria-label="Primary"
    >
      <nav className="flex flex-1 flex-col gap-1 p-3">
        {ITEMS.map((item) => {
          const active =
            item.href === "/"
              ? pathname === "/"
              : pathname === item.href || (item.sub ? false : pathname.startsWith(item.href) && !pathname.startsWith(item.href + "/"));
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center justify-between gap-3 rounded-md py-1.5 text-sm font-medium transition-colors",
                item.sub ? "pl-8 pr-3" : "px-3 py-2",
                active
                  ? "bg-secondary text-secondary-foreground"
                  : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
              )}
              aria-current={active ? "page" : undefined}
            >
              <span className="flex items-center gap-3">
                <Icon className="h-4 w-4" />
                {item.label}
              </span>
              {item.phase ? (
                <span
                  className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
                  title={`Ships in Phase ${item.phase}`}
                >
                  P{item.phase}
                </span>
              ) : null}
            </Link>
          );
        })}
      </nav>
      <footer className="border-t p-3 text-xs text-muted-foreground">
        <p>Pronaos admin v0.1</p>
        <p className="font-mono">Phase 71 — Settings</p>
      </footer>
    </aside>
  );
}
