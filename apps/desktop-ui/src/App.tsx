import { invoke } from "@tauri-apps/api/core";
import { Cpu, HardDriveDownload, Mic, MonitorCog, RadioTower, Zap } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

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

  useEffect(() => {
    invoke<RuntimeCandidate[]>("list_runtime_candidates")
      .then((items) => {
        setCandidates(items);
        setSelected(items.find((item) => item.recommended)?.id ?? items[0]?.id ?? "cpu-onnx-llamacpp");
      })
      .catch(() => {
        setCandidates(fallbackCandidates);
      });
  }, []);

  const selectedCandidate = useMemo(
    () => candidates.find((candidate) => candidate.id === selected) ?? candidates[0],
    [candidates, selected],
  );

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
          <div className="status-pill">Scaffold</div>
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
                    onClick={() => setSelected(candidate.id)}
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
                <dt>Notes</dt>
                <dd>{selectedCandidate?.notes}</dd>
              </div>
            </dl>
            <button className="primary-action" type="button">
              Prepare Runtime
            </button>
          </div>
        </section>
      </section>
    </main>
  );
}
