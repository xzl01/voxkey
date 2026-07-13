import type { EngineInfo } from "./tauri";
import { getAsrToken } from "./tauri";

// The local ASR service mints a per-process auth token at startup and writes
// it to a file the desktop shell can read. We attach it to the protected (mic
// / model / transcribe) endpoints so an arbitrary web page on localhost cannot
// drive them. The token is cached, but on a 401 we drop the cache and re-read
// once (the service restarts with a fresh token, or the first read raced the
// service startup) so we don't get stuck on a stale/empty token forever.
let tokenPromise: Promise<string> | null = null;
function getAsrTokenCached(): Promise<string> {
  if (!tokenPromise) {
    tokenPromise = getAsrToken().catch(() => "");
  }
  return tokenPromise;
}

function withToken(init: RequestInit, token: string): RequestInit {
  const headers = new Headers(init.headers);
  headers.set("X-VoxKey-Token", token);
  return { ...init, headers };
}

async function fetchWithToken(url: string, init: RequestInit): Promise<Response> {
  const token = await getAsrTokenCached();
  const res = await fetch(url, withToken(init, token));
  if (res.status === 401) {
    // Stale or empty token: clear the cache, re-read once, and retry.
    tokenPromise = null;
    const fresh = await getAsrTokenCached();
    if (fresh && fresh !== token) {
      return fetch(url, withToken(init, fresh));
    }
  }
  return res;
}

export const LOCAL_ASR_BASE = "http://127.0.0.1:17863";

export interface HealthResponse {
  ok: boolean;
  service: string;
  engines: { kind: string; compute: string }[];
}

export interface TranscribeResult {
  text: string;
  chosen_engine?: string;
  total_latency_s?: number;
  funasr?: unknown;
  qwen3?: unknown;
}

export interface RemoteTranscribeOptions {
  url: string;
  apiKey: string;
  model: string;
}

function toJson<T>(res: Response): Promise<T> {
  return res.json() as Promise<T>;
}

export async function getHealth(baseUrl: string = LOCAL_ASR_BASE): Promise<HealthResponse> {
  const res = await fetch(`${baseUrl}/health`);
  if (!res.ok) throw new Error(`health check failed (${res.status})`);
  return toJson<HealthResponse>(res);
}

export interface EnginesResponse {
  engines: EngineInfo[];
  warnings: string[];
}

export async function getEngines(baseUrl: string = LOCAL_ASR_BASE): Promise<EnginesResponse> {
  const res = await fetch(`${baseUrl}/engines`);
  if (!res.ok) throw new Error(`engines request failed (${res.status})`);
  return toJson<EnginesResponse>(res);
}

export async function setEngines(
  enabled: Record<string, boolean>,
  baseUrl: string = LOCAL_ASR_BASE,
): Promise<EnginesResponse> {
  const res = await fetchWithToken(`${baseUrl}/engines`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw new Error(`engine switch failed (${res.status})`);
  return toJson<EnginesResponse>(res);
}

export async function transcribeFile(
  audio: Blob,
  baseUrl: string = LOCAL_ASR_BASE,
  language?: string,
): Promise<TranscribeResult> {
  const url = language
    ? `${baseUrl}/transcribe?language=${encodeURIComponent(language)}`
    : `${baseUrl}/transcribe`;
  const res = await fetchWithToken(url, { method: "POST", body: audio });
  if (!res.ok) throw new Error(`transcription failed (${res.status})`);
  return toJson<TranscribeResult>(res);
}

/** OpenAI/Whisper-compatible audio transcription. */
export async function transcribeRemote(
  file: Blob,
  opts: RemoteTranscribeOptions,
): Promise<TranscribeResult> {
  const form = new FormData();
  form.append("file", file, "audio.wav");
  form.append("model", opts.model || "whisper-1");
  const res = await fetch(opts.url, {
    method: "POST",
    headers: { Authorization: `Bearer ${opts.apiKey}` },
    body: form,
  });
  if (!res.ok) throw new Error(`remote transcription failed (${res.status})`);
  const data = (await toJson<{ text?: string }>(res)) as { text?: string };
  return { text: data.text ?? "" };
}

export interface StreamTranscribeOptions {
  maxSeconds?: number;
  emitInterval?: number;
  signal?: AbortSignal;
  onStart?: () => void;
  onPartial?: (text: string) => void;
  onFinal?: (text: string, info?: unknown) => void;
  onError?: (message: string) => void;
  onEnd?: () => void;
}

interface SseFrame {
  event: string;
  data: Record<string, unknown>;
}

function parseSseFrame(raw: string): SseFrame {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return { event, data: {} };
  }
}

export async function streamTranscribe(
  baseUrl: string = LOCAL_ASR_BASE,
  opts: StreamTranscribeOptions = {},
): Promise<void> {
  const params = new URLSearchParams();
  if (opts.maxSeconds) params.set("max_seconds", String(opts.maxSeconds));
  if (opts.emitInterval) params.set("emit_interval", String(opts.emitInterval));
  const url = `${baseUrl}/transcribe/stream?${params.toString()}`;

  const res = await fetchWithToken(url, { signal: opts.signal });
  if (!res.ok || !res.body) throw new Error(`streaming failed (${res.status})`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let ended = false;
  const finish = () => {
    if (!ended) {
      ended = true;
      opts.onEnd?.();
    }
  };

  opts.onStart?.();

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = parseSseFrame(buffer.slice(0, sep));
        buffer = buffer.slice(sep + 2);
        if (frame.event === "partial") {
          const text = (frame.data.text as string) ?? "";
          if (text) opts.onPartial?.(text);
        } else if (frame.event === "final") {
          const text = (frame.data.text as string) ?? "";
          opts.onFinal?.(text, frame.data);
        } else if (frame.event === "error") {
          // The service streams `event: error` (still HTTP 200); surface it
          // instead of silently swallowing it.
          const msg = (frame.data.error as string) ?? "streaming error";
          opts.onError?.(msg);
        } else if (frame.event === "end") {
          finish();
        }
      }
    }
  } finally {
    finish();
  }
}
