use serde::{Deserialize, Serialize};

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

pub fn runtime_candidates(platform: HostPlatform) -> Vec<RuntimeCandidate> {
    let mut candidates = vec![RuntimeCandidate {
        id: "cpu-onnx-llamacpp".into(),
        label: "CPU baseline".into(),
        compute: ComputeClass::Cpu,
        runtime: "onnxruntime + llama.cpp CPU".into(),
        platform,
        installed: false,
        recommended: true,
        notes: "Compatibility baseline for first-run setup.".into(),
    }];

    match platform {
        HostPlatform::Windows => {
            candidates.push(RuntimeCandidate {
                id: "gpu-directml".into(),
                label: "GPU via DirectML".into(),
                compute: ComputeClass::Gpu,
                runtime: "onnxruntime-directml + llama.cpp".into(),
                platform,
                installed: false,
                recommended: true,
                notes: "Preferred Windows GPU path when DirectML is available.".into(),
            });
            candidates.push(RuntimeCandidate {
                id: "npu-openvino-qnn".into(),
                label: "NPU experimental".into(),
                compute: ComputeClass::Npu,
                runtime: "OpenVINO NPU / QNN".into(),
                platform,
                installed: false,
                recommended: false,
                notes: "Hardware and driver specific; enable only after detection.".into(),
            });
        }
        HostPlatform::Linux => {
            candidates.push(RuntimeCandidate {
                id: "gpu-vulkan".into(),
                label: "GPU via Vulkan".into(),
                compute: ComputeClass::Gpu,
                runtime: "onnxruntime CPU + llama.cpp Vulkan".into(),
                platform,
                installed: false,
                recommended: true,
                notes: "Matches the current Arch/niri prototype path.".into(),
            });
            candidates.push(RuntimeCandidate {
                id: "npu-openvino-linux".into(),
                label: "NPU experimental".into(),
                compute: ComputeClass::Npu,
                runtime: "OpenVINO NPU or vendor runtime".into(),
                platform,
                installed: false,
                recommended: false,
                notes: "Depends on SoC and vendor runtime availability.".into(),
            });
        }
        HostPlatform::Macos => {
            candidates.push(RuntimeCandidate {
                id: "gpu-metal".into(),
                label: "GPU via Metal".into(),
                compute: ComputeClass::Gpu,
                runtime: "onnxruntime CPU + llama.cpp Metal".into(),
                platform,
                installed: false,
                recommended: true,
                notes: "Preferred Apple Silicon GPU path after validation.".into(),
            });
            candidates.push(RuntimeCandidate {
                id: "npu-coreml".into(),
                label: "ANE experimental".into(),
                compute: ComputeClass::Npu,
                runtime: "Core ML / ANE".into(),
                platform,
                installed: false,
                recommended: false,
                notes: "Requires a separate model conversion path.".into(),
            });
        }
        HostPlatform::Unknown => {}
    }

    candidates
}
