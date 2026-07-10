use std::path::Path;

fn main() {
    // Ensure the bundled ASR service directory exists so `tauri-build`'s resource
    // validation passes even when only compiling (`cargo check`) or on a non-macOS
    // host. The real macOS build populates it fully via scripts/bundle_asr.sh
    // (run by `beforeBuildCommand`); here we only need the path to resolve.
    let asr_dir = Path::new("asr");
    if !asr_dir.exists() {
        let _ = std::fs::create_dir_all(asr_dir);
        let _ = std::fs::write(asr_dir.join(".gitkeep"), b"");
    }

    // Explicitly register the application's own commands so tauri-build generates
    // the `allow-*`/`deny-*` permissions referenced by capabilities/default.json.
    // `tauri_build::build()` (Attributes::default()) leaves the app command list
    // empty, so without this the commands would have no permissions and the build
    // would fail capability validation.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "list_runtime_candidates",
            "load_settings",
            "save_selected_runtime",
            "save_asr_settings",
            "get_asr_service_status",
            "model_status",
            "get_asr_token",
            "start_asr_service",
            "stop_asr_service",
        ]),
    ))
    .expect("failed to run tauri-build");
}
