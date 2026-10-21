//! Где система учителя и запущена ли она.
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::time::Duration;

use tao::event_loop::EventLoopProxy;

use crate::{send, sleep, UserEvent};

/// Программы, в которые ученику можно переключаться (нужны для заданий с файлами).
const DEFAULT_ALLOWED: &[&str] = &[
    "excel.exe", "winword.exe", "soffice.exe", "soffice.bin", "scalc.exe",
    "python.exe", "pythonw.exe", "notepad.exe", "calc.exe", "calculatorapp.exe",
];

pub struct Config {
    /// «http://192.168.1.10:8080» без «/» в конце.
    pub origin: String,
    /// Имена файлов программ в нижнем регистре.
    pub allowed: Vec<String>,
    /// `--exit-after 30`: закрыться через столько секунд (проверка на своём компьютере).
    pub exit_after: Option<u64>,
}

impl Config {
    pub fn load() -> Self {
        let dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(PathBuf::from))
            .unwrap_or_default();
        let args: Vec<String> = std::env::args().collect();
        let from_args = args
            .iter()
            .position(|a| a == "--server")
            .and_then(|i| args.get(i + 1).cloned());
        let from_file = read_lines(&dir.join("server.txt")).into_iter().next();
        let mut origin = from_args.or(from_file).unwrap_or_else(|| "http://127.0.0.1:8080".into());
        if !origin.starts_with("http") {
            origin = format!("http://{origin}");
        }
        let origin = origin.trim_end_matches('/').to_string();

        let mut allowed: Vec<String> = read_lines(&dir.join("allowed.txt")).iter().map(|l| l.to_lowercase()).collect();
        if allowed.is_empty() {
            allowed = DEFAULT_ALLOWED.iter().map(|s| s.to_string()).collect();
        }
        let exit_after = args
            .iter()
            .position(|a| a == "--exit-after")
            .and_then(|i| args.get(i + 1))
            .and_then(|v| v.parse().ok());
        Config { origin, allowed, exit_after }
    }
}

/// Непустые строки файла без комментариев `#`. Блокнот может сохранить файл с невидимой меткой BOM
/// в начале — её отбрасываем, иначе адрес из server.txt сломается.
fn read_lines(path: &std::path::Path) -> Vec<String> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .trim_start_matches('\u{feff}')
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .map(String::from)
        .collect()
}

/// Ждём, пока система учителя ответит на /test/alive, и открываем тест.
pub fn wait_until_up(origin: String, proxy: EventLoopProxy<UserEvent>) {
    std::thread::spawn(move || {
        let mut attempt = 0u32;
        loop {
            if is_alive(&origin) {
                send(&proxy, UserEvent::ServerUp);
                return;
            }
            attempt += 1;
            if attempt == 1 || attempt.is_multiple_of(5) {
                send(&proxy, UserEvent::Status(format!("Компьютер учителя пока не отвечает ({origin}). Ждём…")));
            }
            sleep(2000);
        }
    });
}

fn is_alive(origin: &str) -> bool {
    let host_port = origin.trim_start_matches("http://").split('/').next().unwrap_or("");
    let address = if host_port.contains(':') { host_port.to_string() } else { format!("{host_port}:80") };
    let Some(addr) = address.to_socket_addrs().ok().and_then(|mut a| a.next()) else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(1500)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(1500)));
    let request = format!("GET /test/alive HTTP/1.0\r\nHost: {host_port}\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut head = [0u8; 16];
    matches!(stream.read(&mut head), Ok(n) if n >= 12 && head[9..12] == *b"200")
}

pub fn waiting_page(origin: &str) -> String {
    format!(
        r#"<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Тестирование</title>
<style>
  html, body {{ height: 100%; margin: 0; }}
  body {{ display: flex; align-items: center; justify-content: center; font-family: "Segoe UI", sans-serif;
         background: linear-gradient(135deg, #6366f1, #8b5cf6); color: #fff; }}
  .box {{ text-align: center; max-width: 640px; padding: 24px; }}
  h1 {{ font-size: 40px; margin: 0 0 12px; }}
  p {{ font-size: 20px; opacity: .9; }}
  .dots::after {{ content: ''; animation: d 1.5s steps(4) infinite; }}
  @keyframes d {{ 0% {{ content: ''; }} 25% {{ content: '.'; }} 50% {{ content: '..'; }} 75% {{ content: '...'; }} }}
  #status {{ font-size: 15px; opacity: .75; margin-top: 32px; }}
</style></head><body><div class="box">
  <h1>🔒 Тестирование</h1>
  <p class="dots">Подождите, учитель скоро начнёт</p>
  <div id="status">Подключение к {origin}</div>
</div>
<script>window.setStatus = t => document.getElementById('status').textContent = t;</script>
</body></html>"#
    )
}
