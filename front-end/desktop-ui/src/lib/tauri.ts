import { invoke } from "@tauri-apps/api/core";

export type ComputeClass = "cpu" | "gpu" | "npu";
export type AsrBackend = "local" | "http";
export type Platform = "windows" | "linux" | "macos" | "unknown";

export interface RuntimeCandidate {
  id: string;
  label: string;
  compute: ComputeClass;
  runtime: string;
  platform: Platform;
  installed: boolean;
  recommended: boolean;
  notes: string;
}

export interface DesktopSettings {
  selected_runtime_id: string | null;
  asr_backend: AsrBackend;
  asr_service_url: string;
  asr_fallback_local: boolean;
  asr_http_timeout: number;
  /** Remote Whisper-compatible API key (plaintext — known limitation, see plan). */
  asr_api_key: string;
  /** Remote model name, e.g. "whisper-1". */
  asr_remote_model: string;
}

export interface AsrServiceStatus {
  reachable: boolean;
  url: string;
  status: string;
  detail: string;
}

/** Engine readiness state returned by the local service /engines and model_status. */
export interface EngineInfo {
  id: string;
  label: string;
  compute: ComputeClass;
  present: boolean;
  enabled: boolean;
  loaded: boolean;
  path: string | null;
  size_bytes?: number;
}

const FALLBACK_CANDIDATES: RuntimeCandidate[] = [
  {
    id: "cpu-onnx-llamacpp",
    label: "CPU baseline",
    compute: "cpu",
    runtime: "onnxruntime + llama.cpp CPU",
    platform: "unknown",
    installed: false,
    recommended: true,
    notes: "Fallback option when the native shell is not connected.",
  },
];

export async function listRuntimeCandidates(): Promise<RuntimeCandidate[]> {
  try {
    const items = await invoke<RuntimeCandidate[]>("list_runtime_candidates");
    return items.length > 0 ? items : FALLBACK_CANDIDATES;
  } catch (err) {
    console.error("list_runtime_candidates failed", err);
    return FALLBACK_CANDIDATES;
  }
}

export async function loadSettings(): Promise<DesktopSettings> {
  return invoke<DesktopSettings>("load_settings");
}

export async function saveSelectedRuntime(runtimeId: string): Promise<DesktopSettings> {
  // Tauri v2 maps camelCase -> snake_case, so `runtimeId` becomes `runtime_id`.
  return invoke<DesktopSettings>("save_selected_runtime", { runtimeId });
}

export interface SaveAsrSettingsInput {
  backend: AsrBackend;
  serviceUrl: string;
  fallbackLocal: boolean;
  httpTimeout: number;
  apiKey?: string;
  remoteModel?: string;
}

export async function saveAsrSettings(input: SaveAsrSettingsInput): Promise<DesktopSettings> {
  return invoke<DesktopSettings>("save_asr_settings", {
    backend: input.backend,
    serviceUrl: input.serviceUrl,
    fallbackLocal: input.fallbackLocal,
    httpTimeout: input.httpTimeout,
    apiKey: input.apiKey ?? "",
    remoteModel: input.remoteModel ?? "",
  });
}

export async function getAsrServiceStatus(): Promise<AsrServiceStatus> {
  return invoke<AsrServiceStatus>("get_asr_service_status");
}

/** Read-only local ASR service auth token (so the webview can call the
 * protected mic / model / transcribe endpoints). */
export async function getAsrToken(): Promise<string> {
  return invoke<string>("get_asr_token");
}

export async function modelStatus(): Promise<EngineInfo[]> {
  return invoke<EngineInfo[]>("model_status");
}

/** Launch the bundled local ASR service (spawns the Python process). */
export async function startAsrService(): Promise<void> {
  return invoke<void>("start_asr_service");
}

/** Stop the local ASR service spawned by `start_asr_service`. */
export async function stopAsrService(): Promise<void> {
  return invoke<void>("stop_asr_service");
}

/** Trigger an on-demand download of the ASR model weights from the published
 * release assets (runs the bundled `ensure_*.py` downloaders). The UI should
 * then poll `model_status` to learn when the weights are ready. */
export async function startModelDownload(): Promise<void> {
  return invoke<void>("start_model_download");
}

/** Read the bundled third-party license / attribution text (shown in the
 * Licenses view). */
export async function getLicenses(): Promise<string> {
  return invoke<string>("get_licenses");
}
