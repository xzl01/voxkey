use serde::Serialize;
use std::{collections::HashMap, fs, path::PathBuf, time::Duration};
use tauri::Manager;

const SETTINGS_FILE: &str = "settings.json";

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct AsrServiceStatus {
    reachable: bool,
    url: String,
    status: String,
    detail: String,
}

#[derive(Debug, Serialize)]
struct EngineInfo {
    id: String,
    label: String,
    compute: String,
    present: bool,
    enabled: bool,
    loaded: bool,
    path: Option<String>,
    size_bytes: Option<u64>,
}

#[tauri::command]
fn list_runtime_candidates() -> Vec<voxkey_core::RuntimeCandidate> {
    voxkey_core::runtime_candidates(voxkey_core::HostPlatform::current())
}

#[tauri::command]
fn load_settings(app: tauri::AppHandle) -> Result<voxkey_core::DesktopSettings, String> {
    let path = settings_path(&app)?;
    if !path.exists() {
        return Ok(voxkey_core::DesktopSettings::default());
    }

    let raw = fs::read_to_string(&path)
        .map_err(|err| format!("failed to read {}: {err}", path.display()))?;
    serde_json::from_str(&raw).map_err(|err| format!("failed to parse {}: {err}", path.display()))
}

#[tauri::command]
fn save_selected_runtime(
    app: tauri::AppHandle,
    runtime_id: String,
) -> Result<voxkey_core::DesktopSettings, String> {
    let valid_runtime = voxkey_core::runtime_candidates(voxkey_core::HostPlatform::current())
        .into_iter()
        .any(|candidate| candidate.id == runtime_id);
    if !valid_runtime {
        return Err(format!("unknown runtime candidate: {runtime_id}"));
    }

    let mut settings = load_settings(app.clone())?;
    settings.selected_runtime_id = Some(runtime_id);
    save_settings_file(&app, &settings)?;
    Ok(settings)
}

#[tauri::command]
fn save_asr_settings(
    app: tauri::AppHandle,
    backend: String,
    service_url: String,
    fallback_local: bool,
    http_timeout: u64,
    api_key: String,
    remote_model: String,
) -> Result<voxkey_core::DesktopSettings, String> {
    let backend = backend.trim().to_lowercase();
    if backend != "local" && backend != "http" {
        return Err(format!(
            "asr_backend must be 'local' or 'http', got {backend}"
        ));
    }
    if backend == "http" {
        let lower = service_url.to_lowercase();
        if !lower.starts_with("http://") && !lower.starts_with("https://") {
            return Err("asr_service_url must be an http(s) URL when asr_backend is 'http'".into());
        }
        if http_timeout == 0 {
            return Err("asr_http_timeout must be > 0".into());
        }
    }

    let mut settings = load_settings(app.clone())?;
    settings.asr_backend = backend;
    settings.asr_service_url = service_url.trim().to_string();
    settings.asr_fallback_local = fallback_local;
    settings.asr_http_timeout = http_timeout;
    settings.asr_api_key = api_key;
    settings.asr_remote_model = if remote_model.trim().is_empty() {
        "whisper-1".into()
    } else {
        remote_model.trim().to_string()
    };
    save_settings_file(&app, &settings)?;
    Ok(settings)
}

#[tauri::command]
fn get_asr_service_status(app: tauri::AppHandle) -> Result<AsrServiceStatus, String> {
    let settings = load_settings(app)?;
    Ok(probe_asr_service(settings.asr_service_url))
}

/// Offline snapshot of local ASR model files. Used by the Models page when the
/// local ASR service is not running (the live status comes from GET /engines).
#[tauri::command]
fn model_status(app: tauri::AppHandle) -> Vec<EngineInfo> {
    let base = resolve_macos_base(&app);
    let enabled = read_engine_enabled(&base);

    let specs: [(&str, &str, &str, &str); 2] = [
        (
            "funasr_coreml",
            "FunASR CoreML",
            "npu",
            "models/funasr_coreml/model.onnx",
        ),
        (
            "qwen3_gpu",
            "Qwen3-ASR GPU",
            "gpu",
            "models/qwen3_asr/qwen3_asr_llm.q4_k.gguf",
        ),
    ];

    specs
        .iter()
        .map(|(id, label, compute, rel)| {
            let model_path = base.as_ref().map(|b| b.join(rel));
            let present = model_path.as_ref().map(|p| p.exists()).unwrap_or(false);
            let size = model_path
                .as_ref()
                .and_then(|p| std::fs::metadata(p).ok().map(|m| m.len()));
            EngineInfo {
                id: id.to_string(),
                label: label.to_string(),
                compute: compute.to_string(),
                present,
                enabled: *enabled.get(*id).unwrap_or(&true),
                loaded: false,
                path: model_path.map(|p| p.to_string_lossy().into_owned()),
                size_bytes: size,
            }
        })
        .collect()
}

/// Locate the macOS platform directory that holds `config.json` and `models/`.
fn resolve_macos_base(app: &tauri::AppHandle) -> Option<PathBuf> {
    // 1. Same env var the Python service reads for its config file.
    if let Ok(cfg) = std::env::var("VOXKEY_MACOS_CONFIG") {
        if let Some(parent) = PathBuf::from(cfg).parent() {
            return Some(parent.to_path_buf());
        }
    }
    // 2. Explicit models directory (its parent holds config.json).
    if let Ok(dir) = std::env::var("VOXKEY_MODELS_DIR") {
        let models = PathBuf::from(dir);
        if let Some(parent) = models.parent() {
            return Some(parent.to_path_buf());
        }
        return Some(models);
    }
    // 3. Best-effort: walk up from the executable looking for the repo layout.
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent().map(|p| p.to_path_buf());
        while let Some(candidate) = dir {
            let probe = candidate.join("back-end/platforms/macos/config.json");
            if probe.exists() {
                return Some(candidate.join("back-end/platforms/macos"));
            }
            dir = candidate.parent().map(|p| p.to_path_buf());
        }
    }
    let _ = app;
    None
}

fn read_engine_enabled(base: &Option<PathBuf>) -> HashMap<String, bool> {
    let mut map = HashMap::new();
    if let Some(base) = base {
        let cfg = base.join("config.json");
        if let Ok(raw) = std::fs::read_to_string(&cfg) {
            if let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) {
                if let Some(engines) = value.get("engines").and_then(|e| e.as_object()) {
                    for (key, val) in engines {
                        if let Some(enabled) = val.get("enabled").and_then(|e| e.as_bool()) {
                            map.insert(key.clone(), enabled);
                        }
                    }
                }
            }
        }
    }
    map
}

fn settings_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let config_dir = app
        .path()
        .app_config_dir()
        .map_err(|err| format!("failed to resolve app config dir: {err}"))?;
    Ok(config_dir.join(SETTINGS_FILE))
}

fn save_settings_file(
    app: &tauri::AppHandle,
    settings: &voxkey_core::DesktopSettings,
) -> Result<(), String> {
    let path = settings_path(app)?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("failed to create {}: {err}", parent.display()))?;
    }
    let raw = serde_json::to_string_pretty(settings)
        .map_err(|err| format!("failed to serialize settings: {err}"))?;
    fs::write(&path, format!("{raw}\n"))
        .map_err(|err| format!("failed to write {}: {err}", path.display()))
}

fn probe_asr_service(url: String) -> AsrServiceStatus {
    // Run the blocking HTTP call on a dedicated OS thread. Tauri may invoke
    // this command from inside its async runtime, where reqwest::blocking would
    // panic; isolating it behind std::thread avoids that entirely.
    let fallback_url = url.clone();
    let handle = std::thread::spawn(move || {
        let url = url;
        let client = match reqwest::blocking::Client::builder()
            .timeout(Duration::from_millis(800))
            .build()
        {
            Ok(client) => client,
            Err(err) => {
                return AsrServiceStatus {
                    reachable: false,
                    url,
                    status: "error".into(),
                    detail: format!("failed to build http client: {err}"),
                };
            }
        };

        let target = if url.ends_with('/') {
            format!("{url}health")
        } else {
            format!("{url}/health")
        };

        match client.get(&target).send() {
            Ok(resp) => {
                let status_ok = resp.status().is_success();
                let body = resp.text().unwrap_or_default();
                // Parse the JSON health payload and read the boolean `ok` field
                // rather than string-matching. The macOS service returns a compact
                // payload ({"ok":true,...}); a plain substring check would miss or
                // mis-handle it.
                let healthy = status_ok
                    && serde_json::from_str::<serde_json::Value>(&body)
                        .map(|v| {
                            v.get("ok").and_then(|o| o.as_bool()).unwrap_or(false)
                                && v.get("service")
                                    .and_then(|s| s.as_str())
                                    .map(|s| s.contains("voxkey-asr"))
                                    .unwrap_or(false)
                        })
                        .unwrap_or(false);
                AsrServiceStatus {
                    reachable: healthy,
                    url,
                    status: if healthy { "online" } else { "unhealthy" }.into(),
                    detail: if healthy {
                        "voxkey-asr health check passed".into()
                    } else {
                        "service responded but did not return a valid health payload".into()
                    },
                }
            }
            Err(err) => AsrServiceStatus {
                reachable: false,
                url,
                status: "offline".into(),
                detail: err.to_string(),
            },
        }
    });

    match handle.join() {
        Ok(status) => status,
        Err(_) => AsrServiceStatus {
            reachable: false,
            url: fallback_url,
            status: "error".into(),
            detail: "health check thread panicked".into(),
        },
    }
}

/// Read-only access to the local ASR service's per-process auth token. The
/// Python service atomically writes it to one fixed path at startup; the
/// webview needs it to call protected endpoints. Path resolution mirrors the
/// Python side exactly (env override or home-cache default), so a stale fallback
/// file can never win over the current process token.
#[tauri::command]
fn get_asr_token() -> Result<String, String> {
    let path = match std::env::var("VOXKEY_ASR_TOKEN_FILE") {
        Ok(value) => {
            let path = PathBuf::from(value);
            if !path.is_absolute() {
                return Err("VOXKEY_ASR_TOKEN_FILE must be an absolute path".into());
            }
            path
        }
        Err(_) => {
            let home = std::env::var("HOME").unwrap_or_default();
            std::path::Path::new(&home)
                .join("Library")
                .join("Caches")
                .join("dev.xzl01.voxkey")
                .join("asr_token")
        }
    };
    let token = std::fs::read_to_string(&path)
        .map_err(|err| format!("failed to read ASR token at {}: {err}", path.display()))?;
    if token.is_empty() {
        return Err(format!("ASR token at {} is empty", path.display()));
    }
    Ok(token)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            list_runtime_candidates,
            load_settings,
            save_selected_runtime,
            save_asr_settings,
            get_asr_service_status,
            model_status,
            get_asr_token
        ])
        .run(tauri::generate_context!())
        .expect("failed to run VoxKey desktop shell");
}
