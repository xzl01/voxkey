use serde::Serialize;
use std::{
    collections::HashMap,
    fs,
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::Duration,
};
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

/// Locate the directory that holds the ASR service (`service.py` + modules +
/// `capture_helper` + `config.json`). In a bundled app this is
/// `<Resources>/asr`; under `tauri dev` it is the repo's
/// `back-end/platforms/macos`.
fn asr_bundle_dir(app: &tauri::AppHandle) -> Option<PathBuf> {
    if let Ok(res) = app.path().resource_dir() {
        // Bundled layout: <Resources>/asr/service.py
        let prod = res.join("asr");
        if prod.join("service.py").exists() {
            return Some(prod);
        }
        // Dev layout: src-tauri/../../../back-end/platforms/macos/service.py
        let dev = res.join("../../../back-end/platforms/macos");
        if dev.join("service.py").exists() {
            return Some(dev);
        }
    }
    None
}

/// Python interpreter used to launch the ASR service.
/// Production: `<Resources>/asr/python/bin/python3` (bundled relocatable Python).
/// Dev: the repo venv at `back-end/platforms/macos/.venv/bin/python`.
/// Fallback: system `python3`.
fn asr_python_exe(app: &tauri::AppHandle) -> PathBuf {
    if let Ok(res) = app.path().resource_dir() {
        let prod_py = res.join("asr").join("python").join("bin").join("python3");
        if prod_py.exists() {
            return prod_py;
        }
        let dev_venv = res.join("../../../back-end/platforms/macos/.venv/bin/python");
        if dev_venv.exists() {
            return dev_venv;
        }
    }
    PathBuf::from("python3")
}

/// Default on-disk location of the per-process auth token, mirroring the
/// Python service's home-cache fallback so the webview (`get_asr_token`) and
/// the spawned service agree on the same path.
fn default_token_file() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    PathBuf::from(home)
        .join("Library")
        .join("Caches")
        .join("dev.xzl01.voxkey")
        .join("asr_token")
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
    // 3. Bundled (Resources/asr) or dev (repo back-end/platforms/macos) layout.
    if let Some(base) = asr_bundle_dir(app) {
        return Some(base);
    }
    // 4. Legacy: walk up from the executable looking for the repo layout.
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

/// Read-only access to the bundled third-party license / attribution text
/// (`THIRD_PARTY_LICENSES`), shown in the app's Licenses view. The file lives
/// next to `service.py` inside the ASR bundle (Resources/asr), so the same
/// resolution as the service applies for both dev and bundled layouts.
#[tauri::command]
fn get_licenses(app: tauri::AppHandle) -> Result<String, String> {
    let base = asr_bundle_dir(&app).ok_or("could not locate the ASR service bundle")?;
    let path = base.join("THIRD_PARTY_LICENSES");
    std::fs::read_to_string(&path)
        .map_err(|err| format!("failed to read licenses at {}: {err}", path.display()))
}

/// Owns the optionally-running local ASR service subprocess so the UI can
/// start/stop it. The Tauri app is responsible for launching the Python
/// service (which itself spawns the Swift mic helper); without this the app
/// could only probe an externally-managed service.
struct ServiceState {
    child: Mutex<Option<Child>>,
}

#[tauri::command]
fn start_asr_service(
    app: tauri::AppHandle,
    state: tauri::State<ServiceState>,
) -> Result<(), String> {
    // Refuse to double-start; reap a dead child first so a prior crash doesn't
    // block a restart.
    {
        let mut guard = state.child.lock().unwrap();
        if let Some(child) = guard.as_mut() {
            match child.try_wait() {
                Ok(Some(_)) => *guard = None, // already exited -> clear and respawn
                Ok(None) => return Err("ASR service is already running".into()),
                Err(e) => return Err(format!("failed to inspect ASR service: {e}")),
            }
        }
    }
    let base = asr_bundle_dir(&app).ok_or("could not locate the ASR service bundle")?;
    let python = asr_python_exe(&app);
    let service_py = base.join("service.py");
    if !service_py.exists() {
        return Err(format!("service.py not found at {}", service_py.display()));
    }
    let token_file = default_token_file();
    let mut cmd = Command::new(python);
    cmd.arg(&service_py)
        .current_dir(&base)
        // The launched service reads these same vars (mirroring _startup):
        .env("VOXKEY_MACOS_CONFIG", base.join("config.json"))
        .env("VOXKEY_ASR_TOKEN_FILE", &token_file)
        .env("VOXKEY_ASR_HOST", "127.0.0.1")
        .env("VOXKEY_ASR_PORT", "17863")
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = cmd
        .spawn()
        .map_err(|e| format!("failed to start ASR service: {e}"))?;
    *state.child.lock().unwrap() = Some(child);
    Ok(())
}

#[tauri::command]
fn stop_asr_service(state: tauri::State<ServiceState>) -> Result<(), String> {
    let mut guard = state.child.lock().unwrap();
    if let Some(mut child) = guard.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    Ok(())
}

/// Trigger an on-demand download of the (large) ASR model weights from the
/// published release assets. The bundle ships `ensure_funasr.py` /
/// `ensure_qwen3.py` next to `service.py`, so we run the bundled Python to pull
/// the weights into `models/` inside the bundle directory. This is what makes
/// the DMG self-contained without embedding gigabytes of weights: the app
/// fetches them on first launch instead.
///
/// Returns `Ok(())` once the download has been *spawned* (it runs detached on a
/// helper thread; the UI polls `GET /engines` or `model_status` to learn when
/// the weights are ready). Errors here surface failures to even launch the
/// downloader (missing script / python), not download progress.
#[tauri::command]
fn start_model_download(app: tauri::AppHandle) -> Result<(), String> {
    let base = asr_bundle_dir(&app).ok_or("could not locate the ASR service bundle")?;
    let python = asr_python_exe(&app);

    // The two downloaders are siblings of service.py inside the bundle.
    let ensure_funasr = base.join("ensure_funasr.py");
    let ensure_qwen3 = base.join("ensure_qwen3.py");
    if !ensure_funasr.exists() || !ensure_qwen3.exists() {
        return Err(
            "model downloaders (ensure_funasr.py / ensure_qwen3.py) are missing from the bundle"
                .into(),
        );
    }

    // Run both downloaders. Each is idempotent (skips files already present) and
    // verifies SHA-256 against the committed manifest before use, so re-running
    // is safe. A failure in one engine's download is logged but does not block
    // the other; the spawned process exits non-zero on total failure.
    let mut cmd = Command::new(python);
    cmd.current_dir(&base)
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // Run both downloaders. Each is idempotent (skips files already present) and
    // verifies SHA-256 against the committed manifest before use. We pass
    // `--no-convert` to ensure_funasr so a failed release download fails loudly
    // instead of attempting a heavy local torch conversion on the user's Mac
    // (the design is: weights always come from the published release).
    // ensure_*.py default `--out` already points at models/funasr_coreml and
    // models/qwen3_asr relative to CWD (the bundle dir), so CWD is enough.
    let b = base.to_string_lossy().into_owned();
    let f = ensure_funasr.to_string_lossy().into_owned();
    let q = ensure_qwen3.to_string_lossy().into_owned();
    let script = format!(
        "import sys, runpy\n\
         sys.path.insert(0, {base})\n\
         sys.argv = [{funasr}, '--no-convert']\n\
         runpy.run_path({funasr}, run_name='__main__', alter_sys=True)\n\
         sys.argv = [{qwen3}]\n\
         runpy.run_path({qwen3}, run_name='__main__', alter_sys=True)",
        base = sh_quote(&b),
        funasr = sh_quote(&f),
        qwen3 = sh_quote(&q),
    );
    cmd.arg("-c").arg(script);

    let child = cmd
        .spawn()
        .map_err(|e| format!("failed to start model download: {e}"))?;
    // Detach: we don't need the handle. Leak it into the OS (reaped on exit).
    let _ = child.id();
    std::mem::forget(child);
    Ok(())
}

/// Minimal single-quote shell escaping for embedding paths into a `python -c`
/// string. Bundle paths are controlled (our own python + our own scripts), so a
/// simple quote-escaping is sufficient and avoids spawning a shell.
fn sh_quote(s: &str) -> String {
    let escaped = s.replace('\\', "\\\\").replace('\'', "\\'");
    format!("'{}'", escaped)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(ServiceState {
            child: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            list_runtime_candidates,
            load_settings,
            save_selected_runtime,
            save_asr_settings,
            get_asr_service_status,
            model_status,
            get_asr_token,
            start_asr_service,
            stop_asr_service,
            start_model_download,
            get_licenses
        ])
        .run(tauri::generate_context!())
        .expect("failed to run VoxKey desktop shell");
}
