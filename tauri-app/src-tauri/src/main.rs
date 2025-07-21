use tauri::Manager;
use lynx::Lynx;

mod lynx_integration;

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let main_window = app.get_window("main").unwrap();
            lynx_integration::initialize_lynx(&main_window);
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
