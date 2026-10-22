//! Запирание: перехват клавиш и возврат окна теста, если впереди оказалась чужая программа.
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};

use tao::event_loop::EventLoopProxy;
use windows_sys::Win32::Foundation::{CloseHandle, HWND, LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Threading::{
    GetCurrentProcessId, OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
    PROCESS_QUERY_LIMITED_INFORMATION,
};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP,
    VK_ESCAPE, VK_F4, VK_LWIN, VK_MENU, VK_Q, VK_RWIN, VK_TAB,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, DispatchMessageW, GetForegroundWindow, GetMessageW, SetTimer, SetWindowPos, UnhookWindowsHookEx, HWND_NOTOPMOST, WM_TIMER, HWND_TOPMOST, MSG,
    SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, GetWindowThreadProcessId, IsIconic, SetForegroundWindow,
    SetWindowsHookExW, ShowWindow, BringWindowToTop, KBDLLHOOKSTRUCT, LLKHF_ALTDOWN, SW_RESTORE, WH_KEYBOARD_LL,
    WM_KEYDOWN, WM_SYSKEYDOWN,
};

use crate::{send, sleep, UserEvent};

static PROXY: OnceLock<Mutex<EventLoopProxy<UserEvent>>> = OnceLock::new();

// Зажатые Ctrl, Alt, Shift — по событиям самого перехвата: GetAsyncKeyState внутри перехвата ненадёжен.
static CTRL: AtomicBool = AtomicBool::new(false);
static ALT: AtomicBool = AtomicBool::new(false);
static SHIFT: AtomicBool = AtomicBool::new(false);

fn track_modifier(vk: u16, down: bool) {
    let flag = match vk {
        0xA2 | 0xA3 | 0x11 => &CTRL,  // левый / правый / общий Ctrl
        0xA4 | 0xA5 | 0x12 => &ALT,
        0xA0 | 0xA1 | 0x10 => &SHIFT,
        _ => return,
    };
    flag.store(down, Ordering::Relaxed);
}

/// Перехват живёт в своём потоке с циклом сообщений и переставляется каждые 2 секунды.
/// Если перехват хоть раз ответил медленно (компьютер занят запуском браузера), Windows молча снимает его
/// навсегда и никак об этом не сообщает; переустановка возвращает его.
pub fn install_keyboard_hook(proxy: EventLoopProxy<UserEvent>) {
    let _ = PROXY.set(Mutex::new(proxy));
    std::thread::spawn(|| unsafe {
        let install = || SetWindowsHookExW(WH_KEYBOARD_LL, Some(keyboard_hook), GetModuleHandleW(std::ptr::null()), 0);
        let mut hook = install();
        crate::debug_log!("перехват клавиатуры: {hook:?}");
        SetTimer(std::ptr::null_mut(), 0, 2000, None);
        let mut message: MSG = std::mem::zeroed();
        while GetMessageW(&mut message, std::ptr::null_mut(), 0, 0) > 0 {
            if message.message == WM_TIMER {
                let fresh = install();
                crate::debug_log!("перехват переставлен: {fresh:?}");
                if !fresh.is_null() {
                    UnhookWindowsHookEx(hook);
                    hook = fresh;
                }
            }
            DispatchMessageW(&message);
        }
    });
}

unsafe extern "system" fn keyboard_hook(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code >= 0 {
        let key = unsafe { &*(lparam as *const KBDLLHOOKSTRUCT) };
        let vk = key.vkCode as u16;
        let down = wparam as u32 == WM_KEYDOWN || wparam as u32 == WM_SYSKEYDOWN;
        track_modifier(vk, down);
        // при зажатом Ctrl Windows не ставит признак ALTDOWN — поэтому и свой учёт Alt
        let alt = key.flags & LLKHF_ALTDOWN != 0 || ALT.load(Ordering::Relaxed);
        let ctrl = CTRL.load(Ordering::Relaxed);
        let shift = SHIFT.load(Ordering::Relaxed);
        crate::debug_log!("клавиша {vk:#x} нажата={down} ctrl={ctrl} alt={alt} shift={shift}");

        if down && vk == VK_Q && ctrl && alt && shift {
            if let Some(proxy) = PROXY.get() {
                send(&proxy.lock().unwrap(), UserEvent::Exit);
            }
            return 1;
        }
        let blocked = vk == VK_LWIN
            || vk == VK_RWIN
            || (alt && (vk == VK_TAB || vk == VK_ESCAPE || vk == VK_F4))
            || (ctrl && vk == VK_ESCAPE); // Ctrl+Esc — меню «Пуск», Ctrl+Shift+Esc — диспетчер задач
        if blocked {
            return 1;
        }
    }
    unsafe { CallNextHookEx(std::ptr::null_mut(), code, wparam, lparam) }
}

/// Имя файла программы, которой принадлежит окно: «chrome.exe».
fn process_name(pid: u32) -> Option<String> {
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return None;
        }
        let mut buffer = [0u16; 1024];
        let mut size = buffer.len() as u32;
        let ok = QueryFullProcessImageNameW(handle, PROCESS_NAME_WIN32, buffer.as_mut_ptr(), &mut size);
        CloseHandle(handle);
        if ok == 0 {
            return None;
        }
        let path = String::from_utf16_lossy(&buffer[..size as usize]);
        path.rsplit('\\').next().map(|name| name.to_lowercase())
    }
}

/// Меню «Пуск», поиск, центр уведомлений.
/// searchhost — Windows 11; searchapp и searchui — меню «Пуск» и поиск Windows 10 (новые и старые сборки).
const SHELL_MENUS: &[&str] = &[
    "searchhost.exe", "searchapp.exe", "searchui.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
];

/// Как назвать программу для учителя.
fn friendly_name(name: &str) -> String {
    match name {
        "searchhost.exe" | "searchapp.exe" | "searchui.exe" | "startmenuexperiencehost.exe" => "меню «Пуск»".into(),
        "shellexperiencehost.exe" => "панель уведомлений".into(),
        "taskmgr.exe" => "диспетчер задач".into(),
        "explorer.exe" => "проводник или рабочий стол".into(),
        _ => name.into(),
    }
}

fn tap(vk: u16) {
    let key = |flags| INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 { ki: KEYBDINPUT { wVk: vk, wScan: 0, dwFlags: flags, time: 0, dwExtraInfo: 0 } },
    };
    let inputs = [key(0), key(KEYEVENTF_KEYUP)];
    unsafe { SendInput(inputs.len() as u32, inputs.as_ptr(), std::mem::size_of::<INPUT>() as i32) };
}

fn press_escape() {
    tap(VK_ESCAPE);
    sleep(100);
}

/// Windows не даёт фоновой программе забрать фокус; после нажатия Alt — даёт.
fn take_focus(hwnd: HWND) {
    unsafe {
        if IsIconic(hwnd) != 0 {
            ShowWindow(hwnd, SW_RESTORE);
        }
        tap(VK_MENU);
        BringWindowToTop(hwnd);
        SetForegroundWindow(hwnd);
    }
}

/// Раз в 300 мс: кто впереди? Своя программа или разрешённая — хорошо; чужая — вернуть тест и сообщить учителю.
pub fn start_watchdog(hwnd: isize, allowed: Vec<String>, proxy: EventLoopProxy<UserEvent>) {
    std::thread::spawn(move || {
        let hwnd = hwnd as HWND;
        let own_pid = unsafe { GetCurrentProcessId() };
        let mut last_reported = String::new();
        let mut on_top = None;
        // поверх всех окон — тогда тест не перекроет даже окно с правами администратора (диспетчер задач),
        // которое Windows не даёт убрать; но разрешённую программу (Excel) должно быть видно
        let mut set_on_top = |top: bool| {
            if on_top != Some(top) {
                let order = if top { HWND_TOPMOST } else { HWND_NOTOPMOST };
                unsafe { SetWindowPos(hwnd, order, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE) };
                on_top = Some(top);
            }
        };
        let own_desk = crate::desktop::is_child();
        let mut left_desk = false;
        loop {
            sleep(300);
            if own_desk {
                // на экране обычный стол Windows (ушёл через Ctrl+Alt+Del → «Диспетчер задач»): вернуть стол теста.
                // None — сам экран Ctrl+Alt+Del: туда программам доступа нет, ждём, куда ученик пойдёт дальше
                match crate::desktop::input_desktop() {
                    Some(name) if name != crate::desktop::DESK => {
                        crate::desktop::return_to_test_desk();
                        if !left_desk {
                            send(&proxy, UserEvent::ForeignWindow("рабочий стол Windows".into()));
                            left_desk = true;
                        }
                        continue;
                    }
                    Some(_) => left_desk = false,
                    None => {}
                }
            }
            let front = unsafe { GetForegroundWindow() };
            if front.is_null() || front == hwnd {
                set_on_top(true);
                last_reported.clear();
                continue;
            }
            let mut pid = 0u32;
            unsafe { GetWindowThreadProcessId(front, &mut pid) };
            if pid == own_pid {
                continue; // свои окна: выбор файла для загрузки ответа и т. п.
            }
            let name = process_name(pid).unwrap_or_else(|| "неизвестная программа".into());
            // окна WebView2 (выпадающие списки, загрузки) и разрешённые программы
            if name == "msedgewebview2.exe" || allowed.contains(&name) {
                set_on_top(name == "msedgewebview2.exe");
                last_reported.clear();
                continue;
            }
            set_on_top(true);
            if SHELL_MENUS.contains(&name.as_str()) {
                press_escape(); // меню «Пуск» и поиск не уступают фокус, но закрываются по Esc
            }
            take_focus(hwnd);
            if name != last_reported {
                send(&proxy, UserEvent::ForeignWindow(friendly_name(&name)));
                last_reported = name;
            }
        }
    });
}
