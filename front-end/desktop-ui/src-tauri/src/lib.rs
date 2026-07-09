use serde::Serialize;
use std::{
    collections::HashMap,
    fs,
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    path::PathBuf,
    time::Duration,
};
use tauri::Manager;

const SETTINGS_FILE: &str = "settings.json";
const DEFAULT_ASR_PORT: u16 = 17863;

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

fn parse_asr_url(url: &str) -> (String, u16, String) {
    let without_scheme = match url.split_once("://") {
        Some((_, rest)) => rest,
        None => url,
    };
    let authority_path = without_scheme
        .split(['?', '#'])
        .next()
        .unwrap_or(without_scheme);
    let (authority, path) = match authority_path.split_once('/') {
        Some((a, p)) => (a, format!("/{p}")),
        None => (authority_path, "/".to_string()),
    };
    let (host, port) = match authority.rsplit_once(':') {
        Some((h, p)) => match p.parse::<u16>() {
            Ok(port) => (h.to_string(), port),
            Err(_) => (authority.to_string(), DEFAULT_ASR_PORT),
        },
        None => (authority.to_string(), DEFAULT_ASR_PORT),
    };
    (host, port, path)
}

fn probe_asr_service(url: String) -> AsrServiceStatus {
    let (host, port, _path) = parse_asr_url(&url);
    let addr: SocketAddr = format!("{host}:{port}")
        .parse()
        .unwrap_or_else(|_| SocketAddr::from(([127, 0, 0, 1], DEFAULT_ASR_PORT)));
    let timeout = Duration::from_millis(600);

    let mut stream = match TcpStream::connect_timeout(&addr, timeout) {
        Ok(stream) => stream,
        Err(err) => {
            return AsrServiceStatus {
                reachable: false,
                url,
                status: "offline".into(),
                detail: err.to_string(),
            };
        }
    };

    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));

    let request =
        format!("GET /health HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n");
    if let Err(err) = stream.write_all(request) {
        return AsrServiceStatus {
            reachable: false,
            url,
            status: "error".into(),
            detail: format!("failed to write health request: {err}"),
        };
    }

    let mut response = String::new();
    if let Err(err) = stream.read_to_string(&mut response) {
        return AsrServiceStatus {
            reachable: false,
            url,
            status: "error".into(),
            detail: format!("failed to read health response: {err}"),
        };
    }

    let http_ok = response.starts_with("HTTP/1.0 200") || response.starts_with("HTTP/1.1 200");
    let healthy = http_ok && response.contains("\"ok\": true") && response.contains("voxkey-asr");

    AsrServiceStatus {
        reachable: healthy,
        url,
        status: if healthy { "online" } else { "unhealthy" }.into(),
        detail: if healthy {
            "voxkey-asr health check passed".into()
        } else {
            "service responded but did not return expected health payload".into()
        },
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(    tauri::generate_handler![
        list_runtime_candidates,
        load_settings,
        save_selected_runtime,
        save_asr_settings,
        get_asr_service_status,
        model_status
    ])
        .run(tauri::generate_context!())
        .expect("failed to run VoxKey desktop shell");
}
