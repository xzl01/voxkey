import { invoke } from "@tauri-apps/api/core";
import {
  AlertCircle,
  CheckCircle2,
  Cpu,
  HardDriveDownload,
  Mic,
  MonitorCog,
  RadioTower,
  RefreshCw,
  Save,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

type ComputeClass = "cpu" | "gpu" | "npu";

type RuntimeCandidate = {
  id: string;
  label: string;
  compute: ComputeClass;
  runtime: string;
  platform: "windows" | "linux" | "macos" | "unknown";
  installed: boolean;
  recommended: boolean;
  notes: string;
};

type DesktopSettings = {
  selected_runtime_id: string | null;
  asr_service_url: string;
};

type AsrServiceStatus = {
  reachable: boolean;
  url: string;
  status: "online" | "offline" | "unhealthy" | "error" | string;
  detail: string;
};

const fallbackCandidates: RuntimeCandidate[] = [
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

const computeIcon = {
  cpu: Cpu,
  gpu: Zap,
  npu: MonitorCog,
} satisfies Record<ComputeClass, typeof Cpu>;

export function App() {
  const [candidates, setCandidates] = useState<RuntimeCandidate[]>(fallbackCandidates);
  const [selected, setSelected] = useState("cpu-onnx-llamacpp");
  const [savedRuntimeId, setSavedRuntimeId] = useState<string | null>(null);
  const [serviceStatus, setServiceStatus] = useState<AsrServiceStatus | null>(null);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [statusState, setStatusState] = useState<"loading" | "idle" | "error">("loading");

  useEffect(() => {
    Promise.all([invoke<RuntimeCandidate[]>("list_runtime_candidates"), invoke<DesktopSettings>("load_settings")])
      .then(([items, settings]) => {
        const nextCandidates = items.length > 0 ? items : fallbackCandidates;
        const savedCandidate = settings.selected_runtime_id
          ? nextCandidates.find((item) => item.id === settings.selected_runtime_id)
          : undefined;

        setCandidates(nextCandidates);
        setSavedRuntimeId(settings.selected_runtime_id);
        setSelected(savedCandidate?.id ?? nextCandidates.find((item) => item.recommended)?.id ?? nextCandidates[0].id);
      })
      .catch(() => {
        setCandidates(fallbackCandidates);
      });
  }, []);

  const refreshServiceStatus = useCallback(() => {
    setStatusState("loading");
    invoke<AsrServiceStatus>("get_asr_service_status")
      .then((status) => {
        setServiceStatus(status);
        setStatusState("idle");
      })
      .catch((error) => {
        setServiceStatus({
          reachable: false,
          url: "http://127.0.0.1:17863/health",
          status: "error",
          detail: String(error),
        });
        setStatusState("error");
      });
  }, []);

  useEffect(() => {
    refreshServiceStatus();
  }, [refreshServiceStatus]);

  const selectedCandidate = useMemo(
    () => candidates.find((candidate) => candidate.id === selected) ?? candidates[0],
    [candidates, selected],
  );

  const saveSelection = useCallback(() => {
    if (!selectedCandidate) {
      return;
    }
    setSaveState("saving");
    invoke<DesktopSettings>("save_selected_runtime", {
      runtimeId: selectedCandidate.id,
      runtime_id: selectedCandidate.id,
    })
      .then((settings) => {
        setSavedRuntimeId(settings.selected_runtime_id);
        setSaveState("saved");
      })
      .catch(() => {
        setSaveState("error");
      });
  }, [selectedCandidate]);

  const hasUnsavedSelection = selectedCandidate?.id !== savedRuntimeId;
  const serviceTone = serviceStatus?.reachable ? "online" : "offline";
  const ServiceIcon = serviceStatus?.reachable ? CheckCircle2 : AlertCircle;

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">简</div>
          <div>
            <div className="brand-title">简听输入</div>
            <div className="brand-subtitle">VoxKey local ASR</div>
          </div>
        </div>

        <nav className="nav-list" aria-label="Primary">
          <button className="nav-item active" type="button">
            <MonitorCog size={18} />
            Setup
          </button>
          <button className="nav-item" type="button">
            <HardDriveDownload size={18} />
            Models
          </button>
          <button className="nav-item" type="button">
            <Mic size={18} />
            Capture
          </button>
          <button className="nav-item" type="button">
            <RadioTower size={18} />
            Service
          </button>
        </nav>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <h1>Runtime Setup</h1>
            <p>Install only the app shell first, then download the runtime that matches this machine.</p>
          </div>
          <div className={`status-pill ${serviceTone}`}>
            <ServiceIcon size={16} />
            {serviceStatus?.status ?? "checking"}
          </div>
        </header>

        <section className="setup-grid">
          <div className="panel runtime-panel">
            <div className="panel-header">
              <h2>Compute Plan</h2>
              <span>{candidates.length} options</span>
            </div>

            <div className="runtime-list">
              {candidates.map((candidate) => {
                const Icon = computeIcon[candidate.compute];
                return (
                  <button
                    className={`runtime-option ${selected === candidate.id ? "selected" : ""}`}
                    key={candidate.id}
                    type="button"
                    onClick={() => {
                      setSelected(candidate.id);
                      setSaveState("idle");
                    }}
                  >
                    <Icon size={22} />
                    <span>
                      <strong>{candidate.label}</strong>
                      <small>{candidate.runtime}</small>
                    </span>
                    {candidate.recommended ? <em>Recommended</em> : null}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="panel detail-panel">
            <div className="panel-header">
              <h2>Selected Backend</h2>
              <span>{selectedCandidate?.platform ?? "unknown"}</span>
            </div>
            <dl className="detail-list">
              <div>
                <dt>Class</dt>
                <dd>{selectedCandidate?.compute.toUpperCase()}</dd>
              </div>
              <div>
                <dt>Runtime</dt>
                <dd>{selectedCandidate?.runtime}</dd>
              </div>
              <div>
                <dt>Status</dt>
                <dd>{selectedCandidate?.installed ? "Installed" : "Not installed"}</dd>
              </div>
              <div>
                <dt>Saved</dt>
                <dd>{selectedCandidate?.id === savedRuntimeId ? "Current preference" : "Not saved"}</dd>
              </div>
              <div>
                <dt>Notes</dt>
                <dd>{selectedCandidate?.notes}</dd>
              </div>
            </dl>
            <button
              className="primary-action"
              type="button"
              disabled={!hasUnsavedSelection || saveState === "saving"}
              onClick={saveSelection}
            >
              <Save size={16} />
              {saveState === "saving" ? "Saving" : hasUnsavedSelection ? "Save Selection" : "Saved"}
            </button>
            {saveState === "error" ? <p className="inline-error">Could not save runtime preference.</p> : null}
          </div>
        </section>

        <section className="service-strip" aria-label="ASR service status">
          <div>
            <h2>ASR Service</h2>
            <p>{serviceStatus?.url ?? "http://127.0.0.1:17863/health"}</p>
          </div>
          <div className="service-state">
            <span className={`service-dot ${serviceTone}`} />
            <strong>{statusState === "loading" ? "Checking" : serviceStatus?.status ?? "Unknown"}</strong>
            <small>{serviceStatus?.detail ?? "No health result yet."}</small>
          </div>
          <button className="icon-action" type="button" onClick={refreshServiceStatus} aria-label="Refresh service status">
            <RefreshCw size={18} />
          </button>
        </section>
      </section>
    </main>
  );
}
