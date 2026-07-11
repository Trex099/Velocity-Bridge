use std::net::TcpListener;
use std::process::Command;

use serde::Serialize;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;
const DEFAULT_SERVER_PORT: u16 = 8080;
const MAX_SERVER_PORT: u16 = 8100;

#[derive(Serialize)]
struct PortOwner {
    pid: u32,
    process_name: Option<String>,
}

#[derive(Serialize)]
struct FirewallStatus {
    rule_present: bool,
}

fn config_dir() -> std::path::PathBuf {
    if let Some(base) = dirs::config_dir() {
        #[cfg(target_os = "windows")]
        {
            return base.join("VelocityBridge");
        }

        #[cfg(not(target_os = "windows"))]
        {
            return base.join("velocity-bridge");
        }
    }

    std::env::current_dir().unwrap_or_default()
}

fn local_data_dir() -> std::path::PathBuf {
    if let Some(base) = dirs::data_local_dir() {
        return base.join("VelocityBridge");
    }

    std::env::current_dir().unwrap_or_default()
}

fn onboarding_state_path() -> std::path::PathBuf {
    local_data_dir().join("onboarding.json")
}

fn can_bind_port(port: u16) -> bool {
    TcpListener::bind(("127.0.0.1", port)).is_ok()
}

#[cfg(target_os = "windows")]
fn get_port_owner_windows(port: u16) -> Option<PortOwner> {
    use std::os::windows::process::CommandExt;

    let output = Command::new("cmd")
        .args(["/C", "netstat -ano -p tcp | findstr LISTENING"])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .ok()?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    for line in stdout.lines() {
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() < 5 {
            continue;
        }
        let local = parts[1];
        let state = parts[3];
        let pid_str = parts[4];

        if state != "LISTENING" {
            continue;
        }

        if !local.ends_with(&format!(":{}", port)) {
            continue;
        }

        let pid: u32 = pid_str.parse().ok()?;
        let task_output = Command::new("cmd")
            .args([
                "/C",
                &format!("tasklist /FI \"PID eq {}\" /FO CSV /NH", pid),
            ])
            .creation_flags(CREATE_NO_WINDOW)
            .output()
            .ok()?;

        let task_stdout = String::from_utf8_lossy(&task_output.stdout);
        let process_name = task_stdout
            .lines()
            .next()
            .and_then(|line| line.split(',').next())
            .map(|name| name.trim_matches('"').to_string())
            .filter(|name| !name.is_empty() && name != "INFO: No tasks are running which match the specified criteria.");

        return Some(PortOwner { pid, process_name });
    }

    None
}

#[cfg(not(target_os = "windows"))]
fn get_port_owner_windows(_port: u16) -> Option<PortOwner> {
    None
}

#[cfg(target_os = "windows")]
fn firewall_rule_exists(exe_name: &str) -> bool {
    use std::os::windows::process::CommandExt;

    let cmd = format!(
        "netsh advfirewall firewall show rule name=all | findstr /I \"{}\"",
        exe_name
    );
    let output = Command::new("cmd")
        .args(["/C", &cmd])
        .creation_flags(CREATE_NO_WINDOW)
        .output();

    match output {
        Ok(result) => !result.stdout.is_empty(),
        Err(_) => false,
    }
}

#[cfg(not(target_os = "windows"))]
fn firewall_rule_exists(_exe_name: &str) -> bool {
    true
}

#[tauri::command]
fn pick_server_port(preferred: Option<u16>) -> Result<u16, String> {
    let preferred = preferred.unwrap_or(DEFAULT_SERVER_PORT);

    for port in preferred..=MAX_SERVER_PORT {
        if can_bind_port(port) {
            return Ok(port);
        }
    }

    for port in DEFAULT_SERVER_PORT..preferred {
        if can_bind_port(port) {
            return Ok(port);
        }
    }

    Err(format!(
        "No free local port available in range {}-{}",
        DEFAULT_SERVER_PORT, MAX_SERVER_PORT
    ))
}

#[tauri::command]
fn get_port_conflict(port: u16) -> Result<Option<PortOwner>, String> {
    Ok(get_port_owner_windows(port))
}

#[tauri::command]
fn get_firewall_status() -> Result<FirewallStatus, String> {
    #[cfg(target_os = "windows")]
    {
        let backend_rule = firewall_rule_exists("velocity-backend.exe");
        let ui_rule = firewall_rule_exists("velocity_tauri.exe");
        return Ok(FirewallStatus {
            rule_present: backend_rule && ui_rule,
        });
    }

    #[cfg(not(target_os = "windows"))]
    {
        Ok(FirewallStatus { rule_present: true })
    }
}

#[tauri::command]
fn stop_port_owner(port: u16) -> Result<bool, String> {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;

        if let Some(owner) = get_port_owner_windows(port) {
            // Use wmic instead of taskkill to avoid shutdown errors
            let status = Command::new("wmic")
                .args(["process", "where", &format!("ProcessId={}", owner.pid), "call", "terminate"])
                .creation_flags(CREATE_NO_WINDOW)
                .status()
                .map_err(|e| e.to_string())?;
            return Ok(status.success());
        }

        Ok(false)
    }

    #[cfg(not(target_os = "windows"))]
    {
        let _ = port;
        Err("Stopping a port owner is only supported on Windows.".to_string())
    }
}

#[tauri::command]
fn get_onboarding_completed() -> Result<bool, String> {
    let path = onboarding_state_path();
    if let Ok(raw) = std::fs::read_to_string(&path) {
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) {
            return Ok(value
                .get("completed")
                .and_then(|v| v.as_bool())
                .unwrap_or(false));
        }
    }
    Ok(false)
}

#[tauri::command]
fn set_onboarding_completed(completed: bool) -> Result<(), String> {
    let path = onboarding_state_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let payload = serde_json::json!({ "completed": completed });
    std::fs::write(
        path,
        serde_json::to_string_pretty(&payload).unwrap_or_else(|_| "{}".to_string()),
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn get_install_type() -> String {
    #[cfg(target_os = "windows")]
    {
        return "native".to_string();
    }

    if std::env::var("APPIMAGE").is_ok() {
        return "appimage".to_string();
    }

    if let Ok(exe_path) = std::env::current_exe() {
        let path_str = exe_path.to_string_lossy();
        if path_str.starts_with("/usr/") {
            if std::path::Path::new("/etc/arch-release").exists() {
                return "aur".to_string();
            } else if std::path::Path::new("/etc/fedora-release").exists() {
                return "dnf".to_string();
            } else if std::path::Path::new("/etc/debian_version").exists() {
                return "apt".to_string();
            }
            return "native".to_string();
        }
    }

    "manual".to_string()
}

#[tauri::command]
fn prepare_server_start() -> Result<(), String> {
    kill_server();
    Ok(())
}

#[tauri::command]
fn force_stop_server() -> Result<(), String> {
    kill_server();
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(target_os = "linux")]
    std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec!["--silent"]),
        ))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .invoke_handler(tauri::generate_handler![
            get_install_type,
            install_update,
            prepare_server_start,
            force_stop_server,
            pick_server_port,
            get_port_conflict,
            get_firewall_status,
            stop_port_owner,
            get_onboarding_completed,
            set_onboarding_completed
        ])
        .setup(|app| {
            let silent_flag = std::env::args().any(|a| a == "--silent" || a == "-s");

            let start_minimized = {
                let config_dir = config_dir();
                let settings_path = config_dir.join("settings.json");
                if let Ok(raw) = std::fs::read_to_string(&settings_path) {
                    if let Ok(val) = serde_json::from_str::<serde_json::Value>(&raw) {
                        val.get("start_minimized")
                            .and_then(|v| v.as_bool())
                            .unwrap_or(false)
                    } else {
                        false
                    }
                } else {
                    false
                }
            };

            if !silent_flag && !start_minimized {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }

            let show = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;

            let _tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Velocity Bridge")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "quit" => {
                        kill_server();
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        if let Some(window) = tray.app_handle().get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                window.hide().unwrap();
                api.prevent_close();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|_app_handle, event| match event {
            tauri::RunEvent::Exit => {
                kill_server();
            }
            _ => {}
        });
}

#[cfg(target_os = "linux")]
fn kill_server() {
    let _ = std::process::Command::new("pkill")
        .args(["-f", "velocity-backend"])
        .status();
}

#[cfg(target_os = "windows")]
fn kill_server() {
    use std::os::windows::process::CommandExt;

    // Use wmic instead of taskkill to avoid shutdown errors (0xc0000142)
    // wmic has better handling during system shutdown
    let _ = std::process::Command::new("wmic")
        .args(["process", "where", "name=velocity-backend.exe", "call", "terminate"])
        .creation_flags(CREATE_NO_WINDOW)
        .status();
}

#[cfg(not(any(target_os = "linux", target_os = "windows")))]
fn kill_server() {
    // No-op for other OSs
}

#[tauri::command]
fn install_update(package_path: String) -> Result<(), String> {
    use std::process::Command;

    #[cfg(unix)]
    let _ = Command::new("chmod").args(["+x", &package_path]).status();

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;

        Command::new(&package_path)
            .arg("/S")
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| format!("Failed to launch updater: {}", e))?;
    }

    #[cfg(not(target_os = "windows"))]
    Command::new(package_path)
        .arg("--silent")
        .spawn()
        .map_err(|e| format!("Failed to launch updater: {}", e))?;

    kill_server();
    std::process::exit(0);
}




