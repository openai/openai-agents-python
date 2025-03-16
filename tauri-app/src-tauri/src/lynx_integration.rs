use lynx::Lynx;
use tauri::Window;

pub fn initialize_lynx(window: &Window) {
    let lynx = Lynx::new();
    lynx.run(window);
}
