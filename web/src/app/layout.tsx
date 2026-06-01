import type { Metadata } from "next";

import { ErrorBoundary } from "@/components/error-boundary";
import { ThemeProvider } from "@/components/theme-provider";
import { AuthProvider } from "@/lib/auth/context";

import "./globals.css";
import { Toaster } from "sonner";

export const metadata: Metadata = {
  title: "Pronaos — Admin",
  description:
    "Pronaos: self-hosted LLM gateway with 48 empirical claims about its own behavior.",
  applicationName: "Pronaos Admin",
  robots: { index: false, follow: false },
};

/**
 * Root layout — wires the theme + auth providers + global toast
 * surface. Every page renders inside this tree.
 */
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen bg-background antialiased">
        <ErrorBoundary>
          <ThemeProvider>
            <AuthProvider>
              {children}
              <Toaster richColors closeButton position="top-right" />
            </AuthProvider>
          </ThemeProvider>
        </ErrorBoundary>
      </body>
    </html>
  );
}
