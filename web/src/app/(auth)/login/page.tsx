"use client";

/**
 * /login — the only unauthenticated route.
 *
 * The user pastes a Pronaos API key; we attempt a probe against
 * /v1/health (which is unauthenticated) to confirm reachability,
 * then a second probe against /v1/admin/usage to confirm the key
 * has the admin scope. On 401 we surface a clear error. On success
 * we persist the token and redirect to /.
 */
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { KeyRound, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, getHealth, getUsage } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/context";

export default function LoginPage() {
  const router = useRouter();
  const { status, signIn } = useAuth();
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // If already authenticated, bounce away from login.
  useEffect(() => {
    if (status === "authenticated") {
      router.replace("/");
    }
  }, [status, router]);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!token.trim()) {
      toast.error("Paste a Pronaos API key first");
      return;
    }
    setSubmitting(true);
    try {
      // Step 1: confirm the gateway is reachable at all.
      await getHealth({ token });
      // Step 2: confirm the key has the admin scope by hitting an
      // admin endpoint. We don't care about the body — only the
      // status. A 401/403 here means the key is real but underscoped.
      try {
        await getUsage({}, { token });
      } catch (err) {
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          toast.error("Key authenticated, but lacks the admin:usage scope.");
          setSubmitting(false);
          return;
        }
        // Other errors (e.g. 500) are not a key problem; let through.
      }
      signIn(token.trim());
      toast.success("Signed in");
      router.replace("/");
    } catch (err) {
      if (err instanceof ApiError) {
        toast.error(
          err.status === 401
            ? "Invalid API key — gateway rejected it"
            : `Sign-in failed: ${err.message}`,
        );
      } else {
        toast.error(
          "Could not reach the gateway. Is FastAPI running on :8000?",
        );
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-2">
          <div className="flex items-center gap-2">
            <KeyRound className="h-5 w-5 text-muted-foreground" />
            <CardTitle>Sign in to Pronaos</CardTitle>
          </div>
          <CardDescription>
            Paste a Pronaos API key. The same key you use with{" "}
            <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
              curl
            </code>{" "}
            or the OpenAI SDK works here.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={onSubmit}>
            <div className="space-y-2">
              <Label htmlFor="token">API key</Label>
              <Input
                id="token"
                name="token"
                type="password"
                placeholder="pron_…"
                autoComplete="off"
                autoFocus
                value={token}
                onChange={(e) => setToken(e.target.value)}
                aria-describedby="token-help"
              />
              <p
                id="token-help"
                className="text-xs text-muted-foreground"
              >
                Stored locally in your browser. Never sent to anywhere
                except the Pronaos gateway.
              </p>
            </div>
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Signing in…
                </>
              ) : (
                "Continue"
              )}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
