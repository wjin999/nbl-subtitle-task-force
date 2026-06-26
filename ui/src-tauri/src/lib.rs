use rand::{rngs::OsRng, RngCore};
use serde::Serialize;
use std::net::TcpListener;
use std::process::Command;
use std::sync::Mutex;
use tauri::Manager;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct BackendConfig {
    host: String,
    port: u16,
    token: String,
}

#[tauri::command]
fn get_backend_config(state: tauri::State<BackendProcess>) -> BackendConfig {
    state.config.clone()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![get_backend_config])
        .setup(|app| {
            let host = "127.0.0.1".to_string();
            let reserved_listener =
                reserve_backend_listener().expect("Failed to reserve backend listener");
            let port = reserved_listener
                .local_addr()
                .expect("Failed to read reserved backend address")
                .port();
            let token = generate_backend_token();
            let config = BackendConfig {
                host: host.clone(),
                port,
                token: token.clone(),
            };
            let port_value = port.to_string();
            let sidecar_command = app
                .shell()
                .sidecar("api-server")
                .expect("Failed to create sidecar command")
                .env("NBL_SUBTITLE_PORT", &port_value)
                .env("NBL_SUBTITLE_API_TOKEN", &token)
                .env("NBL_SUBTITLE_HOST", &host);

            drop(reserved_listener);
            let (mut rx, child) = sidecar_command.spawn().expect("Failed to start backend");
            let backend_pid = child.pid();

            // Save child handle so we can kill it on window close
            app.manage(BackendProcess {
                child: Mutex::new(Some(child)),
                pid: backend_pid,
                config,
            });

            // Listen for sidecar events in background to capture errors
            tauri::async_runtime::spawn(async move {
                use tauri_plugin_shell::process::CommandEvent;
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) => {
                            eprintln!("[api-server] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Stderr(line) => {
                            eprintln!("[api-server:err] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Terminated(status) => {
                            eprintln!("[api-server] exited with {:?}", status);
                        }
                        CommandEvent::Error(err) => {
                            eprintln!("[api-server:error] {}", err);
                        }
                        _ => {}
                    }
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(
                event,
                tauri::WindowEvent::CloseRequested { .. } | tauri::WindowEvent::Destroyed
            ) {
                stop_backend(&window.app_handle());
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

/// Holds the backend sidecar process handle for cleanup on window close
struct BackendProcess {
    child: Mutex<Option<CommandChild>>,
    pid: u32,
    config: BackendConfig,
}

fn reserve_backend_listener() -> std::io::Result<TcpListener> {
    TcpListener::bind(("127.0.0.1", 0))
}

fn generate_backend_token() -> String {
    let mut bytes = [0_u8; 32];
    OsRng.fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn stop_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<BackendProcess>() {
        if let Ok(mut guard) = state.child.lock() {
            if let Some(child) = guard.take() {
                kill_process_tree(state.pid);
                let _ = child.kill();
            }
        }
    }
}

fn kill_process_tree(pid: u32) {
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .status();
    }

    #[cfg(not(windows))]
    {
        let _ = Command::new("kill")
            .args(["-TERM", &pid.to_string()])
            .status();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_backend_token_is_hex_256_bit_value() {
        let token = generate_backend_token();

        assert_eq!(token.len(), 64);
        assert!(token.chars().all(|ch| ch.is_ascii_hexdigit()));
    }

    #[test]
    fn reserved_backend_port_is_local_ephemeral_port() {
        let listener = reserve_backend_listener().expect("reserve local listener");
        let port = listener.local_addr().expect("reserved address").port();

        assert!(port > 0);
    }

    #[test]
    fn reserved_backend_listener_keeps_port_bound_until_spawn() {
        let listener = reserve_backend_listener().expect("reserve local listener");
        let port = listener.local_addr().expect("reserved address").port();

        assert!(TcpListener::bind(("127.0.0.1", port)).is_err());

        drop(listener);
        TcpListener::bind(("127.0.0.1", port)).expect("port released after listener drop");
    }
}
