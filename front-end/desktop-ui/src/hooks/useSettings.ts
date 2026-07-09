import { useCallback, useEffect, useMemo, useState } from "react";
import {
  listRuntimeCandidates,
  loadSettings,
  saveAsrSettings,
  saveSelectedRuntime,
  type DesktopSettings,
  type RuntimeCandidate,
} from "../lib/tauri";
import { useI18n } from "../i18n";
import { useToast } from "./useToast";

type SaveState = "idle" | "saving" | "saved" | "error";

export const DEFAULT_SETTINGS: DesktopSettings = {
  selected_runtime_id: null,
  asr_backend: "local",
  asr_service_url: "http://127.0.0.1:17863",
  asr_fallback_local: true,
  asr_http_timeout: 30,
  asr_api_key: "",
  asr_remote_model: "whisper-1",
};

export function useSettings() {
  const { t } = useI18n();
  const { showToast } = useToast();

  const [candidates, setCandidates] = useState<RuntimeCandidate[]>([]);
  const [settings, setSettings] = useState<DesktopSettings>(DEFAULT_SETTINGS);
  const [savedSettings, setSavedSettings] = useState<DesktopSettings>(DEFAULT_SETTINGS);
  const [loading, setLoading] = useState(true);
  const [runtimeSave, setRuntimeSave] = useState<SaveState>("idle");
  const [backendSave, setBackendSave] = useState<SaveState>("idle");

  useEffect(() => {
    let active = true;
    Promise.all([listRuntimeCandidates(), loadSettings()])
      .then(([items, loaded]) => {
        if (!active) return;
        const next = { ...DEFAULT_SETTINGS, ...loaded };
        setCandidates(items);
        setSettings(next);
        setSavedSettings(next);
      })
      .catch((err) => {
        console.error("Failed to load settings", err);
        if (active) setCandidates([]);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const resetAfter = useCallback((setter: (s: SaveState) => void) => {
    window.setTimeout(() => setter("idle"), 1600);
  }, []);

  const update = useCallback((patch: Partial<DesktopSettings>) => {
    setSettings((prev) => ({ ...prev, ...patch }));
    setRuntimeSave("idle");
    setBackendSave("idle");
  }, []);

  const saveRuntime = useCallback(
    async (runtimeId: string) => {
      setRuntimeSave("saving");
      try {
        const next = await saveSelectedRuntime(runtimeId);
        const merged = { ...DEFAULT_SETTINGS, ...next };
        setSavedSettings(merged);
        setSettings((prev) => ({ ...prev, selected_runtime_id: merged.selected_runtime_id }));
        setRuntimeSave("saved");
        showToast(t("toast.runtimeSaved"), "success");
        resetAfter(setRuntimeSave);
      } catch (err) {
        console.error(err);
        setRuntimeSave("error");
        showToast(t("toast.backendFailed"), "error");
        resetAfter(setRuntimeSave);
      }
    },
    [t, showToast, resetAfter],
  );

  const saveBackend = useCallback(
    async () => {
      setBackendSave("saving");
      try {
        const next = await saveAsrSettings({
          backend: settings.asr_backend,
          serviceUrl: settings.asr_service_url,
          fallbackLocal: settings.asr_fallback_local,
          httpTimeout: settings.asr_http_timeout,
          apiKey: settings.asr_api_key,
          remoteModel: settings.asr_remote_model,
        });
        const merged = { ...DEFAULT_SETTINGS, ...next };
        setSavedSettings(merged);
        setSettings(merged);
        setBackendSave("saved");
        showToast(t("toast.backendSaved"), "success");
        resetAfter(setBackendSave);
      } catch (err) {
        console.error(err);
        setBackendSave("error");
        showToast(t("toast.backendFailed"), "error");
        resetAfter(setBackendSave);
      }
    },
    [settings, t, showToast, resetAfter],
  );

  const selectedCandidate = useMemo(
    () =>
      candidates.find((c) => c.id === settings.selected_runtime_id) ??
      candidates.find((c) => c.recommended) ??
      candidates[0] ??
      null,
    [candidates, settings.selected_runtime_id],
  );

  const hasUnsavedRuntime = selectedCandidate?.id !== savedSettings.selected_runtime_id;

  const hasUnsavedBackend =
    settings.asr_backend !== savedSettings.asr_backend ||
    settings.asr_service_url !== savedSettings.asr_service_url ||
    settings.asr_fallback_local !== savedSettings.asr_fallback_local ||
    settings.asr_http_timeout !== savedSettings.asr_http_timeout ||
    settings.asr_api_key !== savedSettings.asr_api_key ||
    settings.asr_remote_model !== savedSettings.asr_remote_model;

  return {
    candidates,
    settings,
    savedSettings,
    loading,
    runtimeSave,
    backendSave,
    update,
    saveRuntime,
    saveBackend,
    selectedCandidate,
    hasUnsavedRuntime,
    hasUnsavedBackend,
  };
}
