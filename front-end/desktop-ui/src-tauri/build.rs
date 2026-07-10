fn main() {
    // Explicitly register the application's own commands so tauri-build generates
    // the `allow-*`/`deny-*` permissions referenced by capabilities/default.json.
    // `tauri_build::build()` (Attributes::default()) leaves the app command list
    // empty, so without this the commands would have no permissions and the build
    // would fail capability validation.
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "list_runtime_candidates",
                "load_settings",
                "save_selected_runtime",
                "save_asr_settings",
                "get_asr_service_status",
                "model_status",
            ]),
        ),
    )
    .expect("failed to run tauri-build");
}
