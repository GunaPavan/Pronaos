# Pronaos admin UI

Next.js 15 (App Router) + TypeScript + Tailwind + shadcn/ui frontend for the
Pronaos gateway. This is **Phase 62 — UI Foundation** — the auth shell, app
layout, and connectivity dashboard. Subsequent phases (63→71) add the
content modules (identity, FinOps, playground, routing, compliance,
reliability, async, onboarding).

## Stack

| Layer | Choice |
| --- | --- |
| Framework | Next.js 15.5 (App Router, RSC where appropriate) |
| Language | TypeScript 5.7 with `strict` + `noUncheckedIndexedAccess` |
| Styling | Tailwind CSS 3.4 + shadcn/ui (new-york preset) |
| Theme | next-themes (class strategy, light/dark/system) |
| Forms | React Hook Form + Zod (planned for Phase 63) |
| Data | Custom typed fetch wrapper + Zod validation |
| Auth | API-key bearer in localStorage (Phase 62 trade-off — see below) |
| E2E tests | Playwright 1.50 (Chromium) |
| Linting | ESLint (Next.js core-web-vitals) + Prettier |

## Run locally

```bash
# Install dependencies (first time only)
npm install

# Start dev server (proxies /v1/* to FastAPI on :8000)
npm run dev

# Run typecheck
npm run typecheck

# Run production build
npm run build

# Run end-to-end tests (boots its own dev server)
npm test
```

Visit `http://localhost:3000`. The login page expects you to paste a
Pronaos API key with the `admin:usage` scope.

## Backend prerequisites

The dev server proxies `/v1/*` to `http://localhost:8000` by default. To
point at a different gateway, set `PRONAOS_API_URL` in `.env.local`:

```bash
PRONAOS_API_URL=http://pronaos.example.com:8000
```

## Production deployment

```bash
npm run build && npm run start
```

Or bake the static output into the FastAPI Docker image — the gateway
mounts `/admin/*` to serve the SPA from the same origin, so both API and
UI ship in one container. See `src/pronaos/main.py::_mount_admin_ui`.

## Directory layout

```
web/
├── src/
│   ├── app/                # Next.js App Router routes
│   │   ├── (auth)/login/   # Unauthenticated route (login form)
│   │   ├── (app)/          # Authenticated route group
│   │   │   ├── layout.tsx  # AppShell (top nav + side nav)
│   │   │   └── page.tsx    # /  — dashboard landing
│   │   ├── layout.tsx      # Root layout (providers + theme + toast)
│   │   └── globals.css     # Tailwind base + CSS variables
│   ├── components/
│   │   ├── layout/         # AppShell, TopNav, SideNav
│   │   ├── ui/             # shadcn/ui primitives (Button, Card, Input, Label)
│   │   ├── error-boundary.tsx
│   │   ├── theme-provider.tsx
│   │   └── theme-toggle.tsx
│   └── lib/
│       ├── api/
│       │   ├── client.ts   # Typed fetch wrapper + ApiError
│       │   └── schemas.ts  # Zod schemas mirroring backend Pydantic models
│       ├── auth/
│       │   └── context.tsx # AuthProvider + useAuth hook
│       └── utils.ts        # cn() helper
└── tests/
    └── e2e/                # Playwright specs (mocked-backend)
```

## Auth model trade-off (read me)

The bearer token is stored in `localStorage`. This makes integration with
the existing bearer-token Pronaos API straightforward (the same key
works for curl, the OpenAI SDK, and the UI). The trade-off is XSS
vulnerability — a successful script injection could exfiltrate the key.

Mitigations:
- Strict Content-Security-Policy (added in Phase 71 polish).
- Same key the user already pastes into curl / SDK configs — no new
  surface, just the same risk as `OPENAI_API_KEY` in `.env`.

For deployments where this trade-off isn't acceptable, the alternative
is a Next.js BFF route that swaps the API key for an httponly session
cookie at login. Phase 71 ships that as an opt-in.

## Adding new pages

Pages under `src/app/(app)/` live inside the authenticated shell —
they get the top nav + side nav + auth gate for free. Just create the
file:

```tsx
// src/app/(app)/teams/page.tsx
"use client";
export default function TeamsPage() {
  return <h1>Teams</h1>;
}
```

The side nav (`src/components/layout/side-nav.tsx`) lists which page
ships in which phase. Update it when you add a destination.

## Empirical claim #49

The Phase 62 foundation is verified by:

- **Python verify**: `python scripts/verify_ui_foundation.py` —
  8 assertions covering the backend contract (endpoint paths, response
  shapes, unauthenticated 4xx, conditional static mount).
- **Playwright e2e**: `npm test` — 7 tests covering login flow,
  bad-key rejection, dashboard render, sign-out, error states,
  masked session key.

See [`../CLAIMS.md`](../CLAIMS.md#claim-49--ui-foundation) for the
full deep-dive.
