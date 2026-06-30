use serde::Serialize;
use std::{
    fs,
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    path::PathBuf,
    time::Duration,
};
use tauri::Manager;

const SETTINGS_FILE: &str = "settings.json";
const ASR_HEALTH_ADDR: ([u8; 4], u16) = ([127, 0, 0, 1], 17863);

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct AsrServiceStatus {
    reachable: bool,
    url: String,
    status: String,
    detail: String,
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
fn get_asr_service_status(app: tauri::AppHandle) -> Result<AsrServiceStatus, String> {
    let settings = load_settings(app)?;
    Ok(probe_asr_service(settings.asr_service_url))
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
    let addr = SocketAddr::from(ASR_HEALTH_ADDR);
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

    let request = b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:17863\r\nConnection: close\r\n\r\n";
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
        .invoke_handler(tauri::generate_handler![
            list_runtime_candidates,
            load_settings,
            save_selected_runtime,
            get_asr_service_status
        ])
        .run(tauri::generate_context!())
        .expect("failed to run VoxKey desktop shell");
}
