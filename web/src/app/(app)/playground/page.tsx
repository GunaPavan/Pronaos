"use client";

/**
 * /playground — Chat, Embeddings, and Rerank playground (Phase 65 — complete).
 *
 * Three tabs per the original plan:
 *   Chat      — multi-turn streaming chat + response inspector
 *   Embeddings — text input + vector dimension preview
 *   Rerank     — query + documents + scored results
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import {
  AlertCircle,
  Coins,
  Database,
  Hash,
  Layers,
  List,
  Loader2,
  MessageSquare,
  Play,
  RotateCcw,
  Send,
  Sparkles,
  Square,
  Timer,
  Zap,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
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
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  chatCompletion,
  createEmbeddings,
  createRerank,
  listModels,
  streamChatCompletion,
  type PlaygroundMessage,
} from "@/lib/api/client";
import type { ModelInfo } from "@/lib/api/schemas";
import { formatHcents, formatTokens } from "@/lib/format";

// =========================================================================== //
// Chat types + helpers (unchanged from Phase 65)                             //
// =========================================================================== //

const STORAGE_KEY = "pronaos.playground.settings.v1";

interface PlaygroundSettings {
  model: string;
  temperature: number;
  maxTokens: number;
  streaming: boolean;
  systemPrompt: string;
}

const DEFAULT_SETTINGS: PlaygroundSettings = {
  model: "",
  temperature: 0.7,
  maxTokens: 1024,
  streaming: true,
  systemPrompt: "You are a helpful assistant.",
};

interface TurnMetadata {
  startedAt: number;
  ttftMs: number | null;
  totalMs: number | null;
  headers: Record<string, string>;
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number } | null;
  error: string | null;
}

interface Turn {
  id: string;
  user: PlaygroundMessage;
  assistant: PlaygroundMessage;
  streaming: boolean;
  metadata: TurnMetadata;
}

function newTurnId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `turn_${Math.random().toString(36).slice(2)}`;
}

function loadSettings(): PlaygroundSettings {
  if (typeof window === "undefined") return DEFAULT_SETTINGS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_SETTINGS;
    const parsed = JSON.parse(raw) as Partial<PlaygroundSettings>;
    return { ...DEFAULT_SETTINGS, ...parsed };
  } catch {
    return DEFAULT_SETTINGS;
  }
}

function saveSettings(settings: PlaygroundSettings): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch { /* full or blocked */ }
}

function snapshotHeaders(response: Response): Record<string, string> {
  const out: Record<string, string> = {};
  response.headers.forEach((value, key) => {
    if (key.toLowerCase().startsWith("x-pronaos-")) out[key] = value;
  });
  return out;
}

// =========================================================================== //
// Main page — tab switcher                                                    //
// =========================================================================== //

type Tab = "chat" | "embeddings" | "rerank";

export default function PlaygroundPage() {
  const [tab, setTab] = useState<Tab>("chat");
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try { setModels(await listModels()); }
      catch (err) {
        const msg = err instanceof ApiError ? `HTTP ${err.status}: ${err.message}`
          : err instanceof Error ? err.message : "Unknown error";
        setModelsError(msg);
      }
    })();
  }, []);

  const embeddingModels = useMemo(
    () => models.filter((m) => m.provider_configured && m.allowed),
    [models],
  );

  const tabs: { key: Tab; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
    { key: "chat", label: "Chat", icon: MessageSquare },
    { key: "embeddings", label: "Embeddings", icon: Hash },
    { key: "rerank", label: "Rerank", icon: Layers },
  ];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Playground</h1>
        <p className="text-sm text-muted-foreground">
          Test the gateway's chat, embeddings, and rerank endpoints against the
          same middleware stack as production traffic.
        </p>
      </div>

      {modelsError ? (
        <Card><CardContent className="py-4">
          <p className="text-sm text-destructive" data-testid="models-load-error">{modelsError}</p>
        </CardContent></Card>
      ) : null}

      {/* Tab bar */}
      <div className="flex gap-1 border-b pb-0" role="tablist">
        {tabs.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            data-testid={`tab-${key}`}
            className={
              tab === key
                ? "flex items-center gap-2 border-b-2 border-primary px-4 py-2 text-sm font-medium text-primary"
                : "flex items-center gap-2 border-b-2 border-transparent px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground"
            }
          >
            <Icon className="h-4 w-4" />
            {label}
          </button>
        ))}
      </div>

      {tab === "chat" && <ChatTab models={models} />}
      {tab === "embeddings" && <EmbeddingsTab models={embeddingModels} />}
      {tab === "rerank" && <RerankTab />}
    </div>
  );
}

// =========================================================================== //
// Chat tab (Phase 65 content — unchanged)                                    //
// =========================================================================== //

function ChatTab({ models }: { models: ModelInfo[] }) {
  const [settings, setSettings] = useState<PlaygroundSettings>(DEFAULT_SETTINGS);
  const [composer, setComposer] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [inFlight, setInFlight] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setSettings(loadSettings());
  }, []);

  useEffect(() => {
    if (models.length === 0) return;
    setSettings((cur) => {
      if (cur.model && models.some((m) => m.fqmn === cur.model)) return cur;
      const firstReady = models.find((m) => m.allowed && m.provider_configured);
      if (firstReady) return { ...cur, model: firstReady.fqmn };
      return cur;
    });
  }, [models]);

  useEffect(() => { saveSettings(settings); }, [settings]);

  const selectedModel = useMemo(
    () => models.find((m) => m.fqmn === settings.model) ?? null,
    [models, settings.model],
  );

  const send = useCallback(async () => {
    const prompt = composer.trim();
    if (!prompt || inFlight || !settings.model) return;
    const turnId = newTurnId();
    const startedAt = Date.now();
    const userMsg: PlaygroundMessage = { role: "user", content: prompt };
    const assistantMsg: PlaygroundMessage = { role: "assistant", content: "" };
    setComposer("");
    setTurns((prev) => [...prev, { id: turnId, user: userMsg, assistant: assistantMsg, streaming: true,
      metadata: { startedAt, ttftMs: null, totalMs: null, headers: {}, usage: null, error: null } }]);
    setInFlight(true);
    const history: PlaygroundMessage[] = [];
    if (settings.systemPrompt.trim()) history.push({ role: "system", content: settings.systemPrompt.trim() });
    for (const t of turns) {
      history.push(t.user);
      if (t.assistant.content) history.push(t.assistant);
    }
    history.push(userMsg);
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      if (settings.streaming) {
        const { response, stream } = await streamChatCompletion(
          { model: settings.model, messages: history, temperature: settings.temperature, max_tokens: settings.maxTokens },
          { signal: ac.signal },
        );
        let firstChunkAt: number | null = null;
        let acc = "";
        for await (const chunk of stream) {
          if (chunk.delta) {
            if (firstChunkAt === null) firstChunkAt = Date.now();
            acc += chunk.delta;
            setTurns((prev) => prev.map((t) => t.id === turnId
              ? { ...t, assistant: { ...t.assistant, content: acc },
                  metadata: { ...t.metadata, ttftMs: firstChunkAt ? firstChunkAt - startedAt : t.metadata.ttftMs } }
              : t));
          }
        }
        const headers = snapshotHeaders(response);
        setTurns((prev) => prev.map((t) => t.id === turnId
          ? { ...t, streaming: false, metadata: { ...t.metadata, headers, totalMs: Date.now() - startedAt } } : t));
      } else {
        const { data, response } = await chatCompletion(
          { model: settings.model, messages: history, temperature: settings.temperature, max_tokens: settings.maxTokens },
          { signal: ac.signal },
        );
        const headers = snapshotHeaders(response);
        const totalMs = Date.now() - startedAt;
        const text = (data.choices[0]?.message.content as string | null | undefined) ?? "";
        setTurns((prev) => prev.map((t) => t.id === turnId
          ? { ...t, streaming: false, assistant: { ...t.assistant, content: text },
              metadata: { ...t.metadata, headers, totalMs, ttftMs: totalMs, usage: data.usage ?? null } } : t));
      }
    } catch (err) {
      if (ac.signal.aborted) {
        setTurns((prev) => prev.map((t) => t.id === turnId
          ? { ...t, streaming: false, metadata: { ...t.metadata, totalMs: Date.now() - startedAt, error: "cancelled" } } : t));
      } else {
        const message = err instanceof ApiError ? `HTTP ${err.status}: ${err.message}`
          : err instanceof Error ? err.message : "Unknown error";
        toast.error(message);
        setTurns((prev) => prev.map((t) => t.id === turnId
          ? { ...t, streaming: false, metadata: { ...t.metadata, totalMs: Date.now() - startedAt, error: message } } : t));
      }
    } finally {
      setInFlight(false);
      abortRef.current = null;
    }
  }, [composer, inFlight, settings, turns]);

  const cancel = useCallback(() => { abortRef.current?.abort(); }, []);
  const lastTurn = turns[turns.length - 1] ?? null;

  return (
    <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)_320px]">
      {/* Parameter sidebar */}
      <Card>
        <CardHeader><CardTitle className="text-sm">Parameters</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="chat-model" className="text-xs">Model</Label>
            <select id="chat-model" value={settings.model}
              onChange={(e) => setSettings({ ...settings, model: e.target.value })}
              className="flex h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm font-mono"
              data-testid="model-select">
              {models.length === 0 ? <option value="">Loading…</option>
                : models.map((m) => (
                  <option key={m.fqmn} value={m.fqmn} disabled={!m.allowed}>
                    {m.fqmn}{!m.provider_configured ? " (unconfigured)" : ""}
                  </option>
                ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <Label htmlFor="chat-temp" className="text-xs">Temperature</Label>
              <span className="font-mono text-xs tabular-nums text-muted-foreground" data-testid="temperature-value">{settings.temperature.toFixed(2)}</span>
            </div>
            <input id="chat-temp" type="range" min={0} max={2} step={0.05} value={settings.temperature}
              onChange={(e) => setSettings({ ...settings, temperature: parseFloat(e.target.value) })}
              className="w-full accent-primary" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="chat-maxtok" className="text-xs">Max tokens</Label>
            <Input id="chat-maxtok" type="number" min={1} max={100_000} value={settings.maxTokens}
              onChange={(e) => setSettings({ ...settings, maxTokens: Math.max(1, parseInt(e.target.value, 10) || 1) })} />
          </div>
          <div className="flex items-center justify-between rounded-md border p-3">
            <div><p className="text-xs font-medium">Streaming</p><p className="text-[11px] text-muted-foreground">SSE deltas</p></div>
            <label className="relative inline-flex h-5 w-9 cursor-pointer items-center">
              <input type="checkbox" className="peer sr-only" checked={settings.streaming}
                onChange={(e) => setSettings({ ...settings, streaming: e.target.checked })} data-testid="streaming-toggle" />
              <span className="h-5 w-9 rounded-full bg-muted transition peer-checked:bg-primary" />
              <span className="absolute left-0.5 h-4 w-4 rounded-full bg-background shadow transition peer-checked:translate-x-4" />
            </label>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="chat-system" className="text-xs">System prompt</Label>
            <Textarea id="chat-system" rows={4} value={settings.systemPrompt}
              onChange={(e) => setSettings({ ...settings, systemPrompt: e.target.value })}
              placeholder="You are a helpful assistant." />
          </div>
        </CardContent>
      </Card>

      {/* Conversation */}
      <Card className="flex flex-col" data-testid="conversation">
        <CardContent className="flex h-[min(70vh,720px)] flex-col gap-3 p-4">
          <div className="flex-1 space-y-3 overflow-y-auto pr-1">
            {turns.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center text-center text-sm text-muted-foreground">
                <Sparkles className="mb-2 h-6 w-6 opacity-50" />
                <p>Send a message to start the conversation.</p>
              </div>
            ) : turns.map((t) => (
              <div key={t.id} className="space-y-2" data-testid="turn">
                <div className="flex justify-end">
                  <div className="max-w-[80%] rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground" data-testid="message-user">
                    <pre className="whitespace-pre-wrap break-words font-sans">{t.user.content as string}</pre>
                  </div>
                </div>
                <div className="flex justify-start">
                  <div className="max-w-[85%] rounded-lg border bg-card px-3 py-2 text-sm" data-testid="message-assistant">
                    {t.metadata.error ? <div className="flex items-center gap-2 text-destructive"><AlertCircle className="h-4 w-4" /><span>{t.metadata.error}</span></div> : null}
                    <pre className="whitespace-pre-wrap break-words font-sans">
                      {t.assistant.content as string || (t.streaming ? "" : "(no response)")}
                      {t.streaming ? <span className="inline-block animate-pulse">▋</span> : null}
                    </pre>
                  </div>
                </div>
              </div>
            ))}
          </div>
          <div className="flex items-end gap-2 border-t pt-3">
            <Textarea value={composer} onChange={(e) => setComposer(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); } }}
              rows={2} placeholder={selectedModel?.provider_configured && selectedModel.allowed ? "Send a message…" : "Pick a configured model first"}
              disabled={!selectedModel?.provider_configured || !selectedModel?.allowed} data-testid="composer" />
            {inFlight ? (
              <Button type="button" variant="destructive" onClick={cancel} data-testid="cancel-button"><Square className="h-4 w-4" />Stop</Button>
            ) : (
              <Button type="button" onClick={() => void send()}
                disabled={!selectedModel?.provider_configured || !selectedModel?.allowed || !composer.trim()} data-testid="send-button">
                <Send className="h-4 w-4" />Send
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Inspector */}
      <Card>
        <CardHeader><CardTitle className="text-sm">Response inspector</CardTitle>
          <CardDescription>{lastTurn ? "Last turn" : "Send a message to populate"}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {selectedModel ? (
            <div className="space-y-1 rounded-md border bg-muted/30 p-3">
              <p className="font-mono text-xs">{selectedModel.fqmn}</p>
              <div className="flex flex-wrap gap-1 pt-1">
                {selectedModel.supports_tools ? <Badge variant="secondary">tools</Badge> : null}
                {selectedModel.supports_vision ? <Badge variant="secondary">vision</Badge> : null}
                {selectedModel.supports_streaming ? <Badge variant="secondary">stream</Badge> : null}
              </div>
            </div>
          ) : null}
          <div className="grid grid-cols-2 gap-2">
            {(() => {
              const h = lastTurn?.metadata.headers ?? {};
              const cost = h["x-pronaos-cost-hcents"] ? parseInt(h["x-pronaos-cost-hcents"], 10) : null;
              const cacheStatus = h["x-pronaos-cache"] ?? null;
              const ttft = lastTurn?.metadata.ttftMs ?? null;
              const total = lastTurn?.metadata.totalMs ?? null;
              return [
                { icon: Coins, label: "Cost", value: cost != null ? formatHcents(cost) : "—", testId: "stat-cost" },
                { icon: Database, label: "Cache", value: cacheStatus ?? "—", testId: "stat-cache", highlight: cacheStatus?.startsWith("hit") ? "success" as const : undefined },
                { icon: Zap, label: "TTFT", value: ttft != null ? `${ttft} ms` : "—", testId: "stat-ttft" },
                { icon: Timer, label: "Total", value: total != null ? `${total} ms` : "—", testId: "stat-total" },
              ].map(({ icon: Icon, label, value, testId, highlight }) => (
                <div key={label} className="rounded-md border p-2">
                  <div className="flex items-center gap-1 text-[10px] uppercase text-muted-foreground"><Icon className="h-3 w-3" />{label}</div>
                  <p className={`font-mono text-sm tabular-nums${highlight === "success" ? " text-green-600 dark:text-green-400" : ""}`} data-testid={testId}>{value}</p>
                </div>
              ));
            })()}
          </div>
          {lastTurn?.metadata.headers["x-pronaos-routed-model"] ? (
            <div className="rounded-md border p-3">
              <p className="text-xs font-semibold">Routing</p>
              <p className="font-mono text-[11px]" data-testid="inspector-routed-model">{lastTurn.metadata.headers["x-pronaos-routed-model"]}</p>
            </div>
          ) : null}
          {lastTurn?.metadata.headers["x-pronaos-request-id"] ? (
            <div className="rounded-md border p-3">
              <p className="text-xs font-semibold">Request ID</p>
              <p className="font-mono text-[11px] break-all" data-testid="inspector-request-id">{lastTurn.metadata.headers["x-pronaos-request-id"]}</p>
            </div>
          ) : null}
          {lastTurn?.metadata.usage ? (
            <div className="rounded-md border p-3">
              <p className="text-xs font-semibold">Tokens</p>
              <p className="text-[11px] text-muted-foreground">
                {formatTokens(lastTurn.metadata.usage.prompt_tokens)} in / {formatTokens(lastTurn.metadata.usage.completion_tokens)} out
              </p>
            </div>
          ) : null}
          {!lastTurn ? <div className="flex items-center gap-2 text-xs text-muted-foreground"><Play className="h-3 w-3" /><span>Awaiting first turn.</span></div> : null}
          {lastTurn?.streaming ? <div className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="h-3 w-3 animate-spin" /><span>Streaming…</span></div> : null}
        </CardContent>
      </Card>
    </div>
  );
}

// =========================================================================== //
// Embeddings tab — text input + vector dimension preview                      //
// =========================================================================== //

function EmbeddingsTab({ models }: { models: ModelInfo[] }) {
  const [model, setModel] = useState("");
  const [inputText, setInputText] = useState("");
  const [dimensions, setDimensions] = useState<string>("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<{
    vectors: number[][];
    model: string;
    prompt_tokens: number;
    cacheHit: boolean;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (models.length > 0 && !model) setModel(models[0]?.fqmn ?? "");
  }, [models, model]);

  async function run(): Promise<void> {
    const text = inputText.trim();
    if (!text || !model) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const body: Record<string, unknown> = { model, input: text };
      const dimN = parseInt(dimensions, 10);
      if (!isNaN(dimN) && dimN > 0) body["dimensions"] = dimN;
      const { data, response } = await createEmbeddings(body as Parameters<typeof createEmbeddings>[0]);
      const d = data as {
        data: Array<{ embedding: number[]; index: number }>;
        model: string;
        usage: { prompt_tokens: number };
      };
      const cacheHeader = response.headers.get("x-pronaos-cache") ?? "";
      setResult({
        vectors: d.data.map((item) => item.embedding),
        model: d.model,
        prompt_tokens: d.usage.prompt_tokens,
        cacheHit: cacheHeader.startsWith("hit"),
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
      toast.error(err instanceof Error ? err.message : "Embeddings failed");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Embeddings</CardTitle>
          <CardDescription>
            POST /v1/embeddings — runs through auth, quota, guardrails, and cache
            the same as production traffic.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="emb-model" className="text-xs">Model</Label>
              <select id="emb-model" value={model} onChange={(e) => setModel(e.target.value)}
                className="flex h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm font-mono"
                data-testid="emb-model-select">
                {models.length === 0
                  ? <option value="">No embedding models available</option>
                  : models.map((m) => <option key={m.fqmn} value={m.fqmn}>{m.fqmn}</option>)}
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="emb-dimensions" className="text-xs">Dimensions (optional)</Label>
              <Input id="emb-dimensions" type="number" min={1} max={4096}
                placeholder="default (model-native)" value={dimensions}
                onChange={(e) => setDimensions(e.target.value)} data-testid="emb-dimensions" />
            </div>
            <div className="flex items-end">
              <Button onClick={() => void run()} disabled={running || !model || !inputText.trim()} className="w-full" data-testid="emb-run-button">
                {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}
                Embed
              </Button>
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="emb-input" className="text-xs">Input text</Label>
            <Textarea id="emb-input" rows={4} placeholder="Enter text to embed…"
              value={inputText} onChange={(e) => setInputText(e.target.value)} data-testid="emb-input" />
          </div>
        </CardContent>
      </Card>

      {error ? (
        <Card><CardContent className="py-4">
          <p className="text-sm text-destructive">{error}</p>
        </CardContent></Card>
      ) : null}

      {result ? (
        <Card data-testid="emb-result">
          <CardHeader>
            <div className="flex items-center gap-3">
              <CardTitle className="text-sm">Result</CardTitle>
              {result.cacheHit ? <Badge variant="success">cache hit</Badge> : <Badge variant="outline">cache miss</Badge>}
            </div>
            <CardDescription>
              Model: <code className="font-mono">{result.model}</code> ·{" "}
              {result.prompt_tokens} tokens · {result.vectors.length} vector{result.vectors.length !== 1 ? "s" : ""} ·{" "}
              {result.vectors[0]?.length ?? 0} dimensions
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {result.vectors.map((vec, i) => (
              <div key={i} className="space-y-1">
                <p className="text-xs font-medium text-muted-foreground">
                  Vector {i} ({vec.length} dims)
                </p>
                <div className="overflow-x-auto rounded-md border bg-muted/30 p-3">
                  <p className="font-mono text-[11px]" data-testid={`emb-vector-${i}`}>
                    [{vec.slice(0, 12).map((v) => v.toFixed(4)).join(", ")}
                    {vec.length > 12 ? `, …${vec.length - 12} more` : ""}]
                  </p>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}

// =========================================================================== //
// Rerank tab — query + documents + scored results                             //
// =========================================================================== //

const DEFAULT_RERANK_DOCS = `The quick brown fox jumps over the lazy dog.
Machine learning models require large datasets.
The Eiffel Tower is located in Paris, France.
Quantum computers use qubits instead of classical bits.
Photosynthesis converts sunlight into chemical energy.`;

function RerankTab() {
  const [model, setModel] = useState("cohere/rerank-english-v3.0");
  const [query, setQuery] = useState("");
  const [docsText, setDocsText] = useState(DEFAULT_RERANK_DOCS);
  const [topN, setTopN] = useState<string>("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<{
    items: Array<{ index: number; relevance_score: number; document?: string }>;
    model: string;
    prompt_tokens: number;
    cacheHit: boolean;
    originalDocs: string[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(): Promise<void> {
    const q = query.trim();
    const docs = docsText.split("\n").map((d) => d.trim()).filter(Boolean);
    if (!q || docs.length === 0 || !model) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const body: Parameters<typeof createRerank>[0] = {
        model, query: q, documents: docs, return_documents: true,
      };
      const n = parseInt(topN, 10);
      if (!isNaN(n) && n > 0) body.top_n = n;
      const { data, response } = await createRerank(body);
      const d = data as {
        data: Array<{ index: number; relevance_score: number; document?: string }>;
        model: string;
        usage: { prompt_tokens: number };
      };
      const cacheHeader = response.headers.get("x-pronaos-cache") ?? "";
      setResult({
        items: d.data,
        model: d.model,
        prompt_tokens: d.usage.prompt_tokens,
        cacheHit: cacheHeader.startsWith("hit"),
        originalDocs: docs,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
      toast.error(err instanceof Error ? err.message : "Rerank failed");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Rerank</CardTitle>
          <CardDescription>
            POST /v1/rerank — scores documents by relevance to a query. Cohere v3 +
            Voyage adapters supported.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="rerank-model" className="text-xs">Model</Label>
              <Input id="rerank-model" type="text" value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="cohere/rerank-english-v3.0"
                className="font-mono text-xs" data-testid="rerank-model" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="rerank-topn" className="text-xs">Top N (optional)</Label>
              <Input id="rerank-topn" type="number" min={1}
                placeholder="return all" value={topN}
                onChange={(e) => setTopN(e.target.value)} data-testid="rerank-topn" />
            </div>
            <div className="flex items-end">
              <Button onClick={() => void run()} disabled={running || !model || !query.trim()} className="w-full" data-testid="rerank-run-button">
                {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <List className="h-4 w-4" />}
                Rerank
              </Button>
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="rerank-query" className="text-xs">Query</Label>
            <Input id="rerank-query" type="text" placeholder="What is photosynthesis?"
              value={query} onChange={(e) => setQuery(e.target.value)} data-testid="rerank-query" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="rerank-docs" className="text-xs">Documents (one per line)</Label>
            <Textarea id="rerank-docs" rows={6} value={docsText}
              onChange={(e) => setDocsText(e.target.value)} data-testid="rerank-docs" />
          </div>
        </CardContent>
      </Card>

      {error ? (
        <Card><CardContent className="py-4">
          <p className="text-sm text-destructive">{error}</p>
        </CardContent></Card>
      ) : null}

      {result ? (
        <Card data-testid="rerank-result">
          <CardHeader>
            <div className="flex items-center gap-3">
              <CardTitle className="text-sm">Ranked results</CardTitle>
              {result.cacheHit ? <Badge variant="success">cache hit</Badge> : <Badge variant="outline">cache miss</Badge>}
            </div>
            <CardDescription>
              Model: <code className="font-mono">{result.model}</code> ·{" "}
              {result.prompt_tokens} tokens · {result.items.length} of{" "}
              {result.originalDocs.length} documents returned
            </CardDescription>
          </CardHeader>
          <CardContent>
            <table className="w-full text-sm" data-testid="rerank-table">
              <thead className="text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-2 py-2">Rank</th>
                  <th className="px-2 py-2 text-right">Score</th>
                  <th className="px-2 py-2">Document</th>
                </tr>
              </thead>
              <tbody>
                {result.items.map((item, rank) => (
                  <tr key={item.index} className="border-t">
                    <td className="px-2 py-2 tabular-nums text-muted-foreground">#{rank + 1}</td>
                    <td className="px-2 py-2 text-right tabular-nums font-medium">
                      {item.relevance_score.toFixed(4)}
                    </td>
                    <td className="px-2 py-2 text-sm">
                      {item.document ?? result.originalDocs[item.index] ?? `(doc ${item.index})`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
