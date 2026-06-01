import type { NextConfig } from "next";

/**
 * Pronaos admin UI — Next.js config.
 *
 * Dev: runs on :3000 with API requests rewritten to FastAPI on :8000.
 * Prod: built as a standalone bundle and mounted at /admin/ by the
 * FastAPI process. The basePath flips on via PRONAOS_UI_BASE_PATH=/admin
 * for prod; left empty in dev so the app serves at the root.
 */
const basePath = process.env.PRONAOS_UI_BASE_PATH ?? "";

const config: NextConfig = {
  basePath,
  // Cross-origin API in dev: the browser talks to :3000, but auth + admin
  // calls need to reach FastAPI on :8000. We proxy /v1/* through the Next
  // dev server so the browser never sees a cross-origin request.
  async rewrites() {
    if (process.env.NODE_ENV !== "development") {
      return [];
    }
    const apiTarget = process.env.PRONAOS_API_URL ?? "http://localhost:8000";
    return [
      {
        source: "/v1/:path*",
        destination: `${apiTarget}/v1/:path*`,
      },
    ];
  },
  // Strict mode catches double-render regressions early.
  reactStrictMode: true,
  poweredByHeader: false,
};

export default config;
