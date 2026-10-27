//! Программа ученика (прототип): окно теста на весь экран, из которого незаметно не уйти.
//!
//! - Первый запуск (с флешки) устанавливает программу в профиль ученика и кладёт на рабочий стол ярлык
//!   «Тестирование» (`install.rs`).
//! - Сама находит систему учителя в своей сети и запоминает её; несколько — ученик выбирает, адрес можно
//!   ввести и вручную (`server.rs`). Пока система не запущена — «Подождите».
//! - Работает на **своём рабочем столе Windows** (как Safe Exam Browser): там нет проводника, панели задач
//!   и меню «Пуск», поэтому Win и Alt+Tab увести некуда. Уход на обычный стол (Ctrl+Alt+Del → «Диспетчер
//!   задач») сразу возвращается и виден учителю. `--no-desktop` — без своего стола.
//! - Перехватывает Win, Alt+Tab, Alt+Esc, Ctrl+Esc, Alt+F4, Ctrl+Shift+Esc (запасной путь: пока впереди
//!   окно WebView2, Windows часто не показывает нажатия перехватчикам).
//! - Если на передний план вышла не разрешённая программа (браузер, проводник, диспетчер задач…),
//!   возвращает окно теста и сообщает учителю. Разрешённые программы (Excel, Python…) — `allowed.txt`.
//! - Ctrl+Alt+Del Windows не отдаёт ни одной программе: уход через него тоже виден учителю.
//! - Выполняет код Python ученика из редактора под заданием его собственным Python (`python.rs`).
//! - Открывает файл задания в LibreOffice на столе теста; окна документов — под шапкой теста, как вкладки (`office.rs`).
//! - Ученик закрывает программу кнопкой «Закрыть программу» — только вне теста (до входа или когда учитель
//!   завершил тестирование и открылась оценка). Выход учителя: Ctrl+Alt+Shift+Q — всегда.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod desktop;
mod install;
mod json;
mod lock;
mod office;
mod python;
mod server;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy};
use tao::platform::windows::WindowExtWindows;
use tao::window::{Fullscreen, WindowBuilder};
use wry::WebViewBuilder;

pub const APP_MARK: &str = "TestingStudentApp/0.1";

/// На каждой странице: Ctrl+Alt+Shift+Q — выход учителя (всегда); вне теста — кнопка «Закрыть программу».
/// Нажатия до страницы доходят точно, до перехвата клавиатуры — не всегда.
const PAGE_SCRIPT: &str = r#"
window.addEventListener('keydown', e => {
    if (e.ctrlKey && e.altKey && e.shiftKey && e.code === 'KeyQ') { e.preventDefault(); window.ipc.postMessage('teacher-exit'); }
}, true);
window.addEventListener('DOMContentLoaded', () => {
    if (location.pathname.startsWith('/test/exam')) return; // во время теста кнопки нет
    const button = document.createElement('button');
    button.textContent = '✕ Закрыть программу';
    button.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;padding:10px 18px;border:none;' +
        'border-radius:10px;background:#1f2937;color:#fff;font:600 15px "Segoe UI",sans-serif;cursor:pointer;opacity:.85';
    button.onclick = () => window.ipc.postMessage('exit');
    document.body.appendChild(button);
});
"#;

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
    /// Нашли систему учителя (адрес «http://192.168.0.77:8080») — открыть страницу теста.
    ServerUp(String),
    /// Нашлось несколько систем — ученик выбирает (пустой список — убрать выбор).
    Servers(Vec<server::Found>),
    /// На передний план вышла чужая программа (имя файла программы).
    ForeignWindow(String),
    /// Учитель нажал Ctrl+Alt+Shift+Q (или --exit-after): закрыть всегда.
    Exit,
    /// Ученик нажал «Закрыть программу»: можно, только если он не в тесте.
    StudentExit,
    /// Скрипт для страницы из другого потока (вывод Python): страницей управляет только поток окна.
    Script(String),
}

fn main() -> wry::Result<()> {
    if let Some(code) = install::install_if_needed() {
        std::process::exit(code); // отработала установленная копия
    }
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

    // Ученик зашёл в тест (/test/exam) — закрыть программу нельзя, пока не откроется страница с оценкой
    // (/test/result: учитель завершил тестирование). По адресам, а не по текущей странице: «Назад» (Alt+←)
    // может показать из кеша страницу входа или ожидания, не спрашивая сервер.
    let in_test = Arc::new(AtomicBool::new(false));
    // адрес системы учителя — станет известен, когда найдём (до того — только страница ожидания)
    let origin: Arc<Mutex<String>> = Arc::default();
    let connected = Arc::new(AtomicBool::new(false));
    let navigation_origin = origin.clone();
    let navigation_test = in_test.clone();
    let webview = WebViewBuilder::new()
        .with_html(server::waiting_page())
        .with_user_agent(format!(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 Edg/130.0 {APP_MARK}"
        ))
        .with_devtools(cfg!(debug_assertions))
        // только страницы системы тестирования: ни поиска, ни других сайтов
        .with_navigation_handler(move |url| {
            let origin = navigation_origin.lock().unwrap().clone();
            if origin.is_empty() {
                return !url.starts_with("http"); // ещё не подключились: только страница ожидания
            }
            if let Some(path) = url.strip_prefix(origin.as_str()) {
                if path.starts_with("/test/exam") {
                    navigation_test.store(true, Ordering::Relaxed);
                } else if path.starts_with("/test/result") {
                    navigation_test.store(false, Ordering::Relaxed);
                    python::stop_all(); // тест закончился — программа ученика больше не нужна
                    office::close_all();
                }
            }
            !url.starts_with("http") || url.starts_with(&origin)
        })
        .with_new_window_req_handler(|_, _| wry::NewWindowResponse::Deny)
        // выход для учителя: нажатия точно доходят до страницы (до перехвата клавиатуры — не всегда)
        .with_initialization_script(PAGE_SCRIPT)
        .with_ipc_handler({
            let proxy = proxy.clone();
            let origin = origin.clone();
            let page: python::Page = {
                let proxy = proxy.clone();
                Arc::new(move |script| send(&proxy, UserEvent::Script(script)))
            };
            move |request| {
                match request.body().as_str() {
                    "teacher-exit" => send(&proxy, UserEvent::Exit),
                    "exit" => send(&proxy, UserEvent::StudentExit),
                    // выбрал систему из списка или ввёл адрес на странице ожидания
                    body if body.starts_with("server:") => server::choose(body["server:".len()..].to_string(), proxy.clone()),
                    // редактор Python под заданием (static/js/py_editor.js)
                    body if body.starts_with("py:") => python::handle(&body["py:".len()..], &origin.lock().unwrap(), &page),
                    // LibreOffice для заданий с файлом (static/js/office.js)
                    body if body.starts_with("office:") => office::handle(&body["office:".len()..], &origin.lock().unwrap(), &page),
                    _ => {}
                }
            }
        })
        .build(&window)?;

    lock::install_keyboard_hook(proxy.clone());
    office::init(hwnd);
    lock::start_watchdog(hwnd, config.allowed.clone(), proxy.clone());
    server::start_finder(config.fixed.clone(), connected.clone(), proxy.clone());
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
                    json::quote(&text)
                ));
            }
            Event::UserEvent(UserEvent::ServerUp(address)) => {
                if connected.swap(true, Ordering::Relaxed) {
                    return; // уже подключились (выбрал сам, пока шёл поиск)
                }
                debug_log!("подключаемся к {address}");
                *origin.lock().unwrap() = address.clone();
                test_open = true;
                let _ = webview.load_url(&format!("{address}/test"));
            }
            Event::UserEvent(UserEvent::Servers(list)) => {
                let items: Vec<String> = list
                    .iter()
                    .map(|f| format!("{{origin: {}, computer: {}}}", json::quote(&f.origin), json::quote(&f.computer)))
                    .collect();
                let _ = webview.evaluate_script(&format!("window.showServers && window.showServers([{}])", items.join(",")));
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
            Event::UserEvent(UserEvent::Script(script)) => {
                let _ = webview.evaluate_script(&script);
            }
            Event::UserEvent(UserEvent::Exit) => *control_flow = ControlFlow::Exit,
            Event::UserEvent(UserEvent::StudentExit) => {
                if in_test.load(Ordering::Relaxed) {
                    let _ = webview.evaluate_script(
                        "alert('Сейчас идёт тест. Закрыть программу можно, когда учитель завершит тестирование.')",
                    );
                } else {
                    *control_flow = ControlFlow::Exit;
                }
            }
            Event::WindowEvent { event: WindowEvent::CloseRequested, .. } => {} // закрыть окно нельзя
            _ => {}
        }
    });
}

/// Скрипт в странице теста: «ушёл к программе app» и сразу «вернулся» (окно теста уже снова впереди).
fn report_script(app: &str) -> String {
    let app = json::quote(app);
    format!(
        r#"(function () {{
            if (!location.pathname.startsWith('/test')) return;
            const send = body => fetch('/test/away', {{ method: 'POST', body: new URLSearchParams(body), keepalive: true }}).catch(() => {{}});
            send({{ away: '1', app: {app} }}).then(() => setTimeout(() => send({{ away: '0' }}), 1000));
        }})();"#
    )
}

pub fn send(proxy: &EventLoopProxy<UserEvent>, event: UserEvent) {
    let _ = proxy.send_event(event);
}

pub fn sleep(ms: u64) {
    std::thread::sleep(Duration::from_millis(ms));
}
