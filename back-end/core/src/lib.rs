use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum HostPlatform {
    Windows,
    Linux,
    Macos,
    Unknown,
}

impl HostPlatform {
    pub fn current() -> Self {
        match std::env::consts::OS {
            "windows" => Self::Windows,
            "linux" => Self::Linux,
            "macos" => Self::Macos,
            _ => Self::Unknown,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ComputeClass {
    Cpu,
    Gpu,
    Npu,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeCandidate {
    pub id: String,
    pub label: String,
    pub compute: ComputeClass,
    pub runtime: String,
    pub platform: HostPlatform,
    pub installed: bool,
    pub recommended: bool,
    pub notes: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DesktopSettings {
    /// Selected compute/runtime candidate id (best-effort, never enforced).
    pub selected_runtime_id: Option<String>,
    /// ASR backend: `local` runs the bundled Qwen3-ASR engine, `http` calls an
    /// external ASR service. Mirrors the daemon `asr_backend` setting.
    pub asr_backend: String,
    /// Base URL of the ASR service (no path). Used by the `http` backend and
    /// for health probing. Mirrors the daemon `asr_service_url` setting.
    pub asr_service_url: String,
    /// When the `http` backend fails, fall back to the local engine instead of
    /// surfacing the error. Mirrors the daemon `asr_fallback_local` setting.
    pub asr_fallback_local: bool,
    /// Request timeout (seconds) for the `http` backend. Mirrors the daemon
    /// `asr_http_timeout` setting.
    pub asr_http_timeout: u64,
}

impl Default for DesktopSettings {
    fn default() -> Self {
        Self {
            selected_runtime_id: None,
            asr_backend: "local".into(),
            asr_service_url: "http://127.0.0.1:17863".into(),
            asr_fallback_local: true,
            asr_http_timeout: 30,
        }
    }
}

fn executable_in_path(name: &str) -> bool {
    let path_var = match std::env::var_os("PATH") {
        Some(value) => value,
        None => return false,
    };
    std::env::split_paths(&path_var).any(|dir| {
        if dir.join(name).is_file() {
            return true;
        }
        if cfg!(windows) && dir.join(format!("{name}.exe")).is_file() {
            return true;
        }
        false
    })
}

#[cfg(target_os = "linux")]
fn has_vulkan_icd() -> bool {
    if let Ok(entries) = std::fs::read_dir("/usr/share/vulkan/icd.d") {
        return entries.flatten().count() > 0;
    }
    false
}

#[cfg(not(target_os = "linux"))]
fn has_vulkan_icd() -> bool {
    false
}

/// Heuristically detect whether a runtime backend is available on the current
/// machine.
///
/// Detection is best-effort and based on well-known executables, shared
/// libraries and framework paths. A `false` result means "not obviously
/// available" rather than a hard guarantee, so the UI should still let the user
/// override the selection.
fn detect_installed(platform: HostPlatform, id: &str) -> bool {
    match (platform, id) {
        (_, "cpu-onnx-llamacpp") => true,
        (HostPlatform::Linux, "gpu-vulkan") => {
            executable_in_path("vulkaninfo")
                || Path::new("/dev/dri/renderD128").exists()
                || has_vulkan_icd()
        }
        (HostPlatform::Linux, "npu-openvino-linux") => {
            executable_in_path("openvino")
                || Path::new("/opt/intel/openvino").exists()
                || Path::new("/opt/intel/openvino_2024").exists()
        }
        (HostPlatform::Macos, "gpu-metal") => true,
        (HostPlatform::Macos, "npu-coreml") => {
            Path::new("/System/Library/Frameworks/CoreML.framework").exists()
        }
        (HostPlatform::Windows, "gpu-directml") => {
            Path::new(r"C:\Windows\System32\DirectML.dll").exists()
                || executable_in_path("directml")
        }
        (HostPlatform::Windows, "npu-openvino-qnn") => {
            Path::new(r"C:\Program Files\Intel\OpenVINO").exists()
                || Path::new(r"C:\Program Files (x86)\Intel\OpenVINO").exists()
                || executable_in_path("openvino")
        }
        _ => false,
    }
}

struct CandidateSpec {
    id: &'static str,
    label: &'static str,
    compute: ComputeClass,
    runtime: &'static str,
    installed_note: &'static str,
    missing_note: &'static str,
}

fn candidate_specs(platform: HostPlatform) -> Vec<CandidateSpec> {
    let mut specs = vec![CandidateSpec {
        id: "cpu-onnx-llamacpp",
        label: "CPU baseline",
        compute: ComputeClass::Cpu,
        runtime: "onnxruntime + llama.cpp CPU",
        installed_note: "Compatibility baseline; always available for first-run setup.",
        missing_note: "Compatibility baseline for first-run setup.",
    }];

    match platform {
        HostPlatform::Windows => {
            specs.push(CandidateSpec {
                id: "gpu-directml",
                label: "GPU via DirectML",
                compute: ComputeClass::Gpu,
                runtime: "onnxruntime-directml + llama.cpp",
                installed_note: "DirectML detected; GPU path is available.",
                missing_note:
                    "DirectML not detected; install onnxruntime-directml or the Windows AI stack.",
            });
            specs.push(CandidateSpec {
                id: "npu-openvino-qnn",
                label: "NPU experimental",
                compute: ComputeClass::Npu,
                runtime: "OpenVINO NPU / QNN",
                installed_note: "OpenVINO/QNN runtime detected; experimental NPU path may work.",
                missing_note: "Hardware and driver specific; enable only after detection.",
            });
        }
        HostPlatform::Linux => {
            specs.push(CandidateSpec {
                id: "gpu-vulkan",
                label: "GPU via Vulkan",
                compute: ComputeClass::Gpu,
                runtime: "onnxruntime CPU + llama.cpp Vulkan",
                installed_note: "Vulkan runtime detected; llama.cpp will use the GPU.",
                missing_note:
                    "No Vulkan runtime detected; install vulkan-tools / mesa-vulkan-drivers.",
            });
            specs.push(CandidateSpec {
                id: "npu-openvino-linux",
                label: "NPU experimental",
                compute: ComputeClass::Npu,
                runtime: "OpenVINO NPU or vendor runtime",
                installed_note: "OpenVINO runtime detected; experimental NPU path may work.",
                missing_note: "Depends on SoC and vendor runtime availability.",
            });
        }
        HostPlatform::Macos => {
            specs.push(CandidateSpec {
                id: "gpu-metal",
                label: "GPU via Metal",
                compute: ComputeClass::Gpu,
                runtime: "onnxruntime CPU + llama.cpp Metal",
                installed_note: "Metal is available on Apple GPUs; GPU path is available.",
                missing_note: "Metal path requires an Apple GPU.",
            });
            specs.push(CandidateSpec {
                id: "npu-coreml",
                label: "ANE experimental",
                compute: ComputeClass::Npu,
                runtime: "Core ML / ANE",
                installed_note: "Core ML framework detected; experimental ANE path may work.",
                missing_note: "Requires a separate model conversion path.",
            });
        }
        HostPlatform::Unknown => {}
    }

    specs
}

pub fn runtime_candidates(platform: HostPlatform) -> Vec<RuntimeCandidate> {
    let mut candidates: Vec<RuntimeCandidate> = candidate_specs(platform)
        .into_iter()
        .map(|spec| {
            let installed = detect_installed(platform, spec.id);
            RuntimeCandidate {
                id: spec.id.into(),
                label: spec.label.into(),
                compute: spec.compute,
                runtime: spec.runtime.into(),
                platform,
                installed,
                recommended: false,
                notes: if installed {
                    spec.installed_note.into()
                } else {
                    spec.missing_note.into()
                },
            }
        })
        .collect();

    // Recommend the first installed accelerator. The NPU path stays
    // experimental and is never auto-recommended; fall back to CPU when no
    // accelerator is detected.
    let mut accelerator_recommended = false;
    for candidate in candidates.iter_mut() {
        if candidate.id == "cpu-onnx-llamacpp" {
            continue;
        }
        if candidate.installed && candidate.compute == ComputeClass::Gpu && !accelerator_recommended
        {
            candidate.recommended = true;
            accelerator_recommended = true;
        }
    }
    if let Some(cpu) = candidates
        .iter_mut()
        .find(|candidate| candidate.id == "cpu-onnx-llamacpp")
    {
        cpu.recommended = !accelerator_recommended;
    }

    candidates
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cpu_baseline_is_available_on_every_platform() {
        for platform in [
            HostPlatform::Windows,
            HostPlatform::Linux,
            HostPlatform::Macos,
            HostPlatform::Unknown,
        ] {
            let candidates = runtime_candidates(platform);
            assert!(candidates.iter().any(|candidate| {
                candidate.id == "cpu-onnx-llamacpp" && candidate.compute == ComputeClass::Cpu
            }));
        }
    }

    #[test]
    fn settings_default_to_local_service_health_url() {
        let settings = DesktopSettings::default();
        assert_eq!(settings.selected_runtime_id, None);
        assert_eq!(settings.asr_backend, "local");
        assert_eq!(settings.asr_service_url, "http://127.0.0.1:17863");
        assert!(settings.asr_fallback_local);
        assert_eq!(settings.asr_http_timeout, 30);
    }

    #[test]
    fn exactly_one_candidate_is_recommended() {
        for platform in [
            HostPlatform::Windows,
            HostPlatform::Linux,
            HostPlatform::Macos,
            HostPlatform::Unknown,
        ] {
            let candidates = runtime_candidates(platform);
            let recommended = candidates.iter().filter(|c| c.recommended).count();
            assert_eq!(
                recommended, 1,
                "platform {platform:?} should recommend exactly one candidate"
            );
        }
    }

    #[test]
    fn cpu_baseline_is_always_installed() {
        for platform in [
            HostPlatform::Windows,
            HostPlatform::Linux,
            HostPlatform::Macos,
            HostPlatform::Unknown,
        ] {
            let candidates = runtime_candidates(platform);
            let cpu = candidates
                .iter()
                .find(|c| c.id == "cpu-onnx-llamacpp")
                .unwrap();
            assert!(cpu.installed, "cpu baseline must always be installed");
        }
    }
}
