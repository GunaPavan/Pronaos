"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * App-level error boundary. Catches render-time errors anywhere
 * under it and renders a friendly recoverable fallback rather than
 * a white screen. Use one near the root in app/layout, and
 * route-scoped boundaries via Next.js's error.tsx files for finer
 * granularity.
 */
type State = { error: Error | null };

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  public override state: State = { error: null };

  public static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  public override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[ErrorBoundary]", error, info);
  }

  private readonly reset = (): void => {
    this.setState({ error: null });
  };

  public override render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="flex min-h-screen items-center justify-center p-6">
          <Card className="max-w-md">
            <CardHeader>
              <div className="flex items-center gap-2">
                <AlertTriangle className="h-5 w-5 text-destructive" />
                <CardTitle>Something went wrong</CardTitle>
              </div>
              <CardDescription>
                The admin UI hit an unrecoverable error. The technical
                detail below is also printed to the browser console.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <pre className="rounded-md bg-muted p-3 text-xs font-mono overflow-x-auto">
                {this.state.error.message}
              </pre>
              <div className="mt-4 flex gap-2">
                <Button onClick={this.reset}>Try again</Button>
                <Button
                  variant="outline"
                  onClick={() => window.location.assign("/")}
                >
                  Back to dashboard
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      );
    }
    return this.props.children;
  }
}
