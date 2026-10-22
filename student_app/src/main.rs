//! Программа ученика (прототип): окно теста на весь экран, из которого незаметно не уйти.
//!
//! - Открывает страницу теста с компьютера учителя (адрес — `server.txt` рядом с программой
//!   или `--server http://192.168.1.10:8080`); пока система учителя не запущена, показывает «Подождите».
//! - Работает на **своём рабочем столе Windows** (как Safe Exam Browser): там нет проводника, панели задач
//!   и меню «Пуск», поэтому Win и Alt+Tab увести некуда. Уход на обычный стол (Ctrl+Alt+Del → «Диспетчер
//!   задач») сразу возвращается и виден учителю. `--no-desktop` — без своего стола.
//! - Перехватывает Win, Alt+Tab, Alt+Esc, Ctrl+Esc, Alt+F4, Ctrl+Shift+Esc (запасной путь: пока впереди
//!   окно WebView2, Windows часто не показывает нажатия перехватчикам).
//! - Если на передний план вышла не разрешённая программа (браузер, проводник, диспетчер задач…),
//!   возвращает окно теста и сообщает учителю. Разрешённые программы (Excel, Python…) — `allowed.txt`.
//! - Ctrl+Alt+Del Windows не отдаёт ни одной программе: уход через него тоже виден учителю.
//! - Выход для учителя: Ctrl+Alt+Shift+Q.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod desktop;
mod lock;
mod server;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy};
use tao::platform::windows::WindowExtWindows;
use tao::window::{Fullscreen, WindowBuilder};
use wry::WebViewBuilder;

pub const APP_MARK: &str = "TestingStudentApp/0.1";

/// Ctrl+Alt+Shift+Q на любой странице — выход (учитель).
const EXIT_SCRIPT: &str = "window.addEventListener('keydown', e => {     if (e.ctrlKey && e.altKey && e.shiftKey && e.code === 'KeyQ') { e.preventDefault(); window.ipc.postMessage('exit'); } }, true);";

/// Диагностика: `set STUDENT_APP_DEBUG=1` — программа пишет в stderr, что видит и что делает.
pub fn debug_enabled() -> bool {
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ON.get_or_init(|| std::env::var_os("STUDENT_APP_DEBUG").is_some())
}

#[macro_export]
macro_rules! debug_log {
    ($($arg:tt)*) => { if $crate::debug_enabled() { eprintln!($($arg)*); } };
}

#[derive(Debug)]
pub enum UserEvent {
    /// Текст на странице ожидания.
    Status(String),
    /// Система учителя ответила — открыть страницу теста.
    ServerUp,
    /// На передний план вышла чужая программа (имя файла программы).
    ForeignWindow(String),
    /// Учитель нажал Ctrl+Alt+Shift+Q.
    Exit,
}

fn main() -> wry::Result<()> {
    if let Some(code) = desktop::launch() {
        std::process::exit(code); // тест шёл на своём столе, экран уже возвращён
    }
    let config = server::Config::load();
    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();

    let window = WindowBuilder::new()
        .with_title("Тестирование")
        .with_decorations(false)
        .with_fullscreen(Some(Fullscreen::Borderless(None)))
        .build(&event_loop)
        .expect("окно");
    let hwnd = window.hwnd();

    let origin = config.origin.clone();
    let webview = WebViewBuilder::new()
        .with_html(server::waiting_page(&config.origin))
        .with_user_agent(format!(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 Edg/130.0 {APP_MARK}"
        ))
        .with_devtools(cfg!(debug_assertions))
        // только страницы системы тестирования: ни поиска, ни других сайтов
        .with_navigation_handler(move |url| !url.starts_with("http") || url.starts_with(&origin))
        .with_new_window_req_handler(|_, _| wry::NewWindowResponse::Deny)
        // выход для учителя: нажатия точно доходят до страницы (до перехвата клавиатуры — не всегда)
        .with_initialization_script(EXIT_SCRIPT)
        .with_ipc_handler({
            let proxy = proxy.clone();
            move |request| {
                if request.body() == "exit" {
                    send(&proxy, UserEvent::Exit);
                }
            }
        })
        .build(&window)?;

    lock::install_keyboard_hook(proxy.clone());
    lock::start_watchdog(hwnd, config.allowed.clone(), proxy.clone());
    server::wait_until_up(config.origin.clone(), proxy.clone());
    if let Some(seconds) = config.exit_after {
        // для проверки на своём компьютере: программа сама закроется и вернёт экран
        let proxy = proxy.clone();
        std::thread::spawn(move || {
            sleep(seconds * 1000);
            send(&proxy, UserEvent::Exit);
        });
    }

    // последние уходы, ещё не отправленные (страница теста могла ещё не открыться)
    let pending: Arc<Mutex<Vec<String>>> = Arc::default();
    let mut test_open = false;

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            Event::UserEvent(UserEvent::Status(text)) => {
                let _ = webview.evaluate_script(&format!(
                    "window.setStatus && window.setStatus({})",
                    js_string(&text)
                ));
            }
            Event::UserEvent(UserEvent::ServerUp) => {
                test_open = true;
                let _ = webview.load_url(&format!("{}/test", config.origin));
            }
            Event::UserEvent(UserEvent::ForeignWindow(app)) => {
                debug_log!("впереди чужая программа: {app}");
                pending.lock().unwrap().push(app);
                if test_open {
                    for app in pending.lock().unwrap().drain(..) {
                        // тот же адрес, что и у страницы теста: сервер засчитает уход и запишет, что открыли
                        let _ = webview.evaluate_script(&report_script(&app));
                    }
                }
            }
            Event::UserEvent(UserEvent::Exit) => *control_flow = ControlFlow::Exit,
            Event::WindowEvent { event: WindowEvent::CloseRequested, .. } => {} // закрыть окно нельзя
            _ => {}
        }
    });
}

/// Скрипт в странице теста: «ушёл к программе app» и сразу «вернулся» (окно теста уже снова впереди).
fn report_script(app: &str) -> String {
    let app = js_string(app);
    format!(
        r#"(function () {{
            if (!location.pathname.startsWith('/test')) return;
            const send = body => fetch('/test/away', {{ method: 'POST', body: new URLSearchParams(body), keepalive: true }}).catch(() => {{}});
            send({{ away: '1', app: {app} }}).then(() => setTimeout(() => send({{ away: '0' }}), 1000));
        }})();"#
    )
}

fn js_string(text: &str) -> String {
    let escaped: String = text
        .chars()
        .flat_map(|c| match c {
            '\\' => vec!['\\', '\\'],
            '\'' => vec!['\\', '\''],
            '\n' => vec!['\\', 'n'],
            '<' => "\\u003c".chars().collect(),
            c => vec![c],
        })
        .collect();
    format!("'{escaped}'")
}

pub fn send(proxy: &EventLoopProxy<UserEvent>, event: UserEvent) {
    let _ = proxy.send_event(event);
}

pub fn sleep(ms: u64) {
    std::thread::sleep(Duration::from_millis(ms));
}
