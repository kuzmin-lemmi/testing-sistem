//! Где система учителя: находим сами в своей сети, запоминаем, при смене адреса ищем заново.
//!
//! Порядок: `--server` или `server.txt` рядом с программой (для проверки) → запомненный адрес
//! (`%LOCALAPPDATA%\Тестирование\server.txt`) → поиск в сети. Нашлась одна система — подключаемся сами,
//! несколько — ученик выбирает, ни одной — ищем снова и даём ввести адрес вручную.
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tao::event_loop::EventLoopProxy;

use crate::{debug_log, send, sleep, UserEvent};

pub const PORT: u16 = 8080;

/// Программы, в которые ученику можно переключаться (нужны для заданий с файлами).
const DEFAULT_ALLOWED: &[&str] = &[
    "excel.exe", "winword.exe", "soffice.exe", "soffice.bin", "scalc.exe",
    "python.exe", "pythonw.exe", "notepad.exe", "calc.exe", "calculatorapp.exe",
];

pub struct Config {
    /// Адрес задан явно (`--server` или `server.txt` рядом с программой) — не ищем и не запоминаем.
    pub fixed: Option<String>,
    /// Имена файлов программ в нижнем регистре.
    pub allowed: Vec<String>,
    /// `--exit-after 30`: закрыться через столько секунд (проверка на своём компьютере).
    pub exit_after: Option<u64>,
}

impl Config {
    pub fn load() -> Self {
        let dir = exe_dir();
        let args: Vec<String> = std::env::args().collect();
        let from_args = args.iter().position(|a| a == "--server").and_then(|i| args.get(i + 1).cloned());
        let from_file = read_lines(&dir.join("server.txt")).into_iter().next();
        let fixed = from_args.or(from_file).and_then(|a| normalize(&a));

        let mut allowed: Vec<String> = read_lines(&dir.join("allowed.txt")).iter().map(|l| l.to_lowercase()).collect();
        if allowed.is_empty() {
            allowed = DEFAULT_ALLOWED.iter().map(|s| s.to_string()).collect();
        }
        let exit_after = args
            .iter()
            .position(|a| a == "--exit-after")
            .and_then(|i| args.get(i + 1))
            .and_then(|v| v.parse().ok());
        Config { fixed, allowed, exit_after }
    }
}

fn exe_dir() -> PathBuf {
    std::env::current_exe().ok().and_then(|p| p.parent().map(PathBuf::from)).unwrap_or_default()
}

/// Непустые строки файла без комментариев `#`. Блокнот может сохранить файл с невидимой меткой BOM
/// в начале — её отбрасываем, иначе адрес сломается.
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

/// «192.168.0.77», «192.168.0.77:8080/test», «http://teacher-pc» → «http://192.168.0.77:8080».
pub fn normalize(address: &str) -> Option<String> {
    let mut host = address.trim().trim_start_matches("http://").trim_start_matches("https://");
    host = host.split('/').next().unwrap_or("").trim();
    if host.is_empty() || host.contains(char::is_whitespace) {
        return None;
    }
    Some(if host.contains(':') { format!("http://{host}") } else { format!("http://{host}:{PORT}") })
}

// --- запомненный адрес ---------------------------------------------------------

/// `%LOCALAPPDATA%\Тестирование` — в профиле ученика, а не рядом с программой на рабочем столе.
pub fn settings_dir() -> PathBuf {
    std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(exe_dir).join("Тестирование")
}

fn saved_server() -> Option<String> {
    read_lines(&settings_dir().join("server.txt")).into_iter().next().and_then(|a| normalize(&a))
}

pub fn save_server(origin: &str) {
    let dir = settings_dir();
    let _ = std::fs::create_dir_all(&dir);
    let _ = std::fs::write(dir.join("server.txt"), format!("{origin}\r\n"));
}

// --- разговор с системой учителя -------------------------------------------------

fn http_get(origin: &str, path: &str, timeout_ms: u64) -> Option<String> {
    let host_port = origin.trim_start_matches("http://");
    let addr: SocketAddr = host_port.to_socket_addrs().ok()?.next()?;
    let timeout = Duration::from_millis(timeout_ms);
    let mut stream = TcpStream::connect_timeout(&addr, timeout).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.write_all(format!("GET {path} HTTP/1.0\r\nHost: {host_port}\r\n\r\n").as_bytes()).ok()?;
    let mut reply = Vec::new();
    let _ = stream.take(64 * 1024).read_to_end(&mut reply);
    let reply = String::from_utf8_lossy(&reply);
    if !reply.starts_with("HTTP/1.") || reply.get(9..12) != Some("200") {
        return None;
    }
    Some(reply.split_once("\r\n\r\n").map(|(_, body)| body.to_string()).unwrap_or_default())
}

/// Найденная система тестирования.
#[derive(Clone, Debug)]
pub struct Found {
    pub origin: String,
    pub computer: String,
}

/// Это система тестирования? (`/test/hello` → `{"app": "testing", "computer": "TEACHER-PC"}`)
pub fn hello(origin: &str, timeout_ms: u64) -> Option<Found> {
    let Some(body) = http_get(origin, "/test/hello", timeout_ms) else {
        // система старой версии (без /test/hello) отвечает «ok» на /test/alive — имени компьютера не узнаем
        return (http_get(origin, "/test/alive", timeout_ms)?.trim() == "ok")
            .then(|| Found { origin: origin.to_string(), computer: String::new() });
    };
    if !body.contains("\"testing\"") {
        return None;
    }
    let computer = json_string(&body, "computer").unwrap_or_default();
    Some(Found { origin: origin.to_string(), computer })
}

/// Строковое поле из небольшого JSON (с разбором \uXXXX: русское имя компьютера приходит так).
fn json_string(body: &str, key: &str) -> Option<String> {
    let start = body.find(&format!("\"{key}\""))?;
    let rest = &body[start + key.len() + 2..];
    let rest = &rest[rest.find('"')? + 1..];
    let mut out = String::new();
    let mut chars = rest.chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => return Some(out),
            '\\' => match chars.next()? {
                'u' => {
                    let code: String = chars.by_ref().take(4).collect();
                    out.push(char::from_u32(u32::from_str_radix(&code, 16).ok()?).unwrap_or('?'));
                }
                other => out.push(other),
            },
            c => out.push(c),
        }
    }
    None
}

/// Свои адреса в локальных сетях (192.168.x.x, 10.x.x.x, 172.16–31.x.x) — сначала обычная школьная 192.168.
fn local_addresses() -> Vec<Ipv4Addr> {
    let host = std::env::var("COMPUTERNAME").unwrap_or_else(|_| "localhost".into());
    let mut list: Vec<Ipv4Addr> = (host.as_str(), 0)
        .to_socket_addrs()
        .map(|it| it.filter_map(|a| match a.ip() { IpAddr::V4(ip) => Some(ip), _ => None }).collect())
        .unwrap_or_default();
    list.retain(|ip| ip.is_private() && !ip.is_link_local());
    list.sort_by_key(|ip| (ip.octets()[0] != 192, ip.octets()));
    list.dedup();
    list
}

/// Обойти свои сети: на каком компьютере отвечает система тестирования. Несколько секунд.
pub fn discover() -> Vec<Found> {
    let own = local_addresses();
    let mut candidates: Vec<Ipv4Addr> = vec![Ipv4Addr::LOCALHOST]; // система на этом же компьютере
    for ip in &own {
        let [a, b, c, _] = ip.octets();
        candidates.extend((1..=254).map(|d| Ipv4Addr::new(a, b, c, d)).filter(|candidate| candidate != ip));
    }
    debug_log!("ищем систему: свои адреса {own:?}, проверяем {} адресов", candidates.len());
    let mut found: Vec<Found> = Vec::new();
    for chunk in candidates.chunks(96) {
        let results: Vec<Option<Found>> = std::thread::scope(|scope| {
            let handles: Vec<_> = chunk
                .iter()
                .map(|ip| scope.spawn(move || hello(&format!("http://{ip}:{PORT}"), 400)))
                .collect();
            handles.into_iter().map(|h| h.join().ok().flatten()).collect()
        });
        found.extend(results.into_iter().flatten());
    }
    // одна система на компьютере учителя отвечает и по 127.0.0.1, и по адресу в сети — оставляем сетевой
    let mut unique: Vec<Found> = Vec::new();
    for item in found.into_iter().rev() {
        if !unique.iter().any(|u| !u.computer.is_empty() && u.computer == item.computer) {
            unique.push(item);
        }
    }
    unique.reverse();
    unique
}

/// Найти систему и подключиться: событие ServerUp(адрес). Останавливается, когда подключились (`connected`).
pub fn start_finder(fixed: Option<String>, connected: Arc<AtomicBool>, proxy: EventLoopProxy<UserEvent>) {
    std::thread::spawn(move || {
        let mut attempt = 0u32;
        while !connected.load(Ordering::Relaxed) {
            attempt += 1;
            if let Some(origin) = &fixed {
                if http_get(origin, "/test/alive", 1500).is_some() {
                    send(&proxy, UserEvent::ServerUp(origin.clone()));
                    return;
                }
                if attempt == 1 || attempt.is_multiple_of(5) {
                    send(&proxy, UserEvent::Status(format!("Компьютер учителя пока не отвечает ({origin}). Ждём…")));
                }
                sleep(2000);
                continue;
            }
            if let Some(origin) = saved_server().filter(|origin| hello(origin, 1500).is_some()) {
                send(&proxy, UserEvent::ServerUp(origin));
                return;
            }
            send(&proxy, UserEvent::Status("Ищем компьютер учителя в сети…".into()));
            let found = discover();
            if connected.load(Ordering::Relaxed) {
                return; // пока искали, ученик выбрал сам
            }
            match found.as_slice() {
                [only] => {
                    save_server(&only.origin);
                    send(&proxy, UserEvent::ServerUp(only.origin.clone()));
                    return;
                }
                [] => {
                    send(&proxy, UserEvent::Servers(Vec::new()));
                    send(&proxy, UserEvent::Status(
                        "Компьютер учителя не найден: возможно, учитель ещё не запустил систему. Ищем снова…".into(),
                    ));
                }
                several => {
                    send(&proxy, UserEvent::Servers(several.to_vec()));
                    send(&proxy, UserEvent::Status("Найдено несколько компьютеров с тестированием — выберите свой.".into()));
                }
            }
            sleep(5000);
        }
    });
}

/// Ученик выбрал систему из списка или ввёл адрес: проверить, запомнить, подключиться.
pub fn choose(address: String, proxy: EventLoopProxy<UserEvent>) {
    std::thread::spawn(move || {
        let Some(origin) = normalize(&address) else {
            send(&proxy, UserEvent::Status(format!("«{address}» — это не адрес компьютера.")));
            return;
        };
        if hello(&origin, 2000).is_some() {
            save_server(&origin);
            send(&proxy, UserEvent::ServerUp(origin));
        } else {
            send(&proxy, UserEvent::Status(format!("По адресу {origin} система тестирования не отвечает.")));
        }
    });
}

pub fn waiting_page() -> String {
    r#"<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Тестирование</title>
<style>
  html, body { height: 100%; margin: 0; }
  body { display: flex; align-items: center; justify-content: center; font-family: "Segoe UI", sans-serif;
         background: linear-gradient(135deg, #6366f1, #8b5cf6); color: #fff; }
  .box { text-align: center; max-width: 680px; padding: 24px; }
  h1 { font-size: 40px; margin: 0 0 12px; }
  p { font-size: 20px; opacity: .9; margin: 0; }
  .dots::after { content: ''; animation: d 1.5s steps(4) infinite; }
  @keyframes d { 0% { content: ''; } 25% { content: '.'; } 50% { content: '..'; } 75% { content: '...'; } }
  #status { font-size: 15px; opacity: .8; margin-top: 28px; min-height: 20px; }
  #servers { display: flex; flex-direction: column; gap: 10px; margin-top: 18px; align-items: center; }
  #servers button, .manual button { font: 600 17px "Segoe UI", sans-serif; padding: 12px 22px; border-radius: 12px;
         border: none; cursor: pointer; background: #fff; color: #4338ca; min-width: 320px; }
  #servers button small { display: block; font-weight: 400; color: #64748b; font-size: 13px; }
  .manual-link { margin-top: 26px; background: none; border: none; color: #fff; opacity: .75; text-decoration: underline;
         font: 14px "Segoe UI", sans-serif; cursor: pointer; }
  .manual { display: none; margin-top: 14px; gap: 8px; justify-content: center; }
  .manual.open { display: flex; }
  .manual input { font: 17px "Segoe UI", sans-serif; padding: 10px 14px; border-radius: 10px; border: none; width: 240px; }
  .manual button { min-width: 0; }
</style></head><body><div class="box">
  <h1>🔒 Тестирование</h1>
  <p class="dots">Подождите, учитель скоро начнёт</p>
  <div id="status">Ищем компьютер учителя…</div>
  <div id="servers"></div>
  <button class="manual-link" onclick="document.querySelector('.manual').classList.toggle('open')">Ввести адрес учителя вручную</button>
  <form class="manual" onsubmit="event.preventDefault(); window.ipc.postMessage('server:' + this.address.value)">
    <input name="address" placeholder="например 192.168.0.77" autocomplete="off">
    <button>Подключиться</button>
  </form>
</div>
<script>
  window.setStatus = text => document.getElementById('status').textContent = text;
  window.showServers = list => {
    const box = document.getElementById('servers');
    box.replaceChildren(...list.map(item => {
      const button = document.createElement('button');
      button.textContent = item.computer ? 'Компьютер ' + item.computer : item.origin;
      const small = document.createElement('small');
      small.textContent = item.origin.replace('http://', '');
      button.append(small);
      button.onclick = () => window.ipc.postMessage('server:' + item.origin);
      return button;
    }));
  };
</script>
</body></html>"#
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn addresses_are_normalized() {
        assert_eq!(normalize("192.168.0.77").as_deref(), Some("http://192.168.0.77:8080"));
        assert_eq!(normalize(" http://192.168.0.77:8080/test ").as_deref(), Some("http://192.168.0.77:8080"));
        assert_eq!(normalize("teacher-pc:9000").as_deref(), Some("http://teacher-pc:9000"));
        assert_eq!(normalize(""), None);
        assert_eq!(normalize("два слова"), None);
    }

    #[test]
    fn hello_reply_is_parsed() {
        let body = r#"{"app":"testing","computer":"УЧИТЕЛЬ-PC"}"#;
        assert_eq!(json_string(body, "computer").as_deref(), Some("УЧИТЕЛЬ-PC"));
        assert_eq!(json_string(r#"{"app": "testing", "computer": "HOME-PC"}"#, "computer").as_deref(), Some("HOME-PC"));
    }
}
