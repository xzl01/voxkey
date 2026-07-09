import type { EngineInfo } from "./tauri";

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

export async function getEngines(baseUrl: string = LOCAL_ASR_BASE): Promise<EngineInfo[]> {
  const res = await fetch(`${baseUrl}/engines`);
  if (!res.ok) throw new Error(`engines request failed (${res.status})`);
  return toJson<EngineInfo[]>(res);
}

export async function setEngines(
  enabled: Record<string, boolean>,
  baseUrl: string = LOCAL_ASR_BASE,
): Promise<EngineInfo[]> {
  const res = await fetch(`${baseUrl}/engines`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw new Error(`engine switch failed (${res.status})`);
  return toJson<EngineInfo[]>(res);
}

export async function transcribeFile(
  audio: Blob,
  baseUrl: string = LOCAL_ASR_BASE,
  language?: string,
): Promise<TranscribeResult> {
  const url = language
    ? `${baseUrl}/transcribe?language=${encodeURIComponent(language)}`
    : `${baseUrl}/transcribe`;
  const res = await fetch(url, { method: "POST", body: audio });
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

  const res = await fetch(url, { signal: opts.signal });
  if (!res.ok || !res.body) throw new Error(`streaming failed (${res.status})`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

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
        } else if (frame.event === "end") {
          opts.onEnd?.();
        }
      }
    }
  } finally {
    opts.onEnd?.();
  }
}
