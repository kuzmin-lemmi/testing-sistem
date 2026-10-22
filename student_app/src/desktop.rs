//! Отдельный рабочий стол Windows для теста — как в Safe Exam Browser.
//!
//! На новом столе нет проводника, панели задач и меню «Пуск»: Win ничего не открывает, Alt+Tab переключает
//! только окна теста, блокировать клавиши не нужно. Перехват клавиатуры с этим не справился: пока впереди окно
//! WebView2, Windows не показывает нажатия перехватчикам (проверено на настоящем компьютере ученика).
//!
//! Устройство: программа запускает свою копию с `--on-desk` на новом столе, переключает экран на него и ждёт.
//! Копия закрылась (выход учителя, `--exit-after`, сбой) — экран возвращается на обычный стол.
//! `--no-desktop` — по-старому, без своего стола.
use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;

use windows_sys::Win32::Foundation::{CloseHandle, GENERIC_ALL, HANDLE, WAIT_TIMEOUT};
use windows_sys::Win32::System::Console::{GetStdHandle, STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE};
use windows_sys::Win32::System::StationsAndDesktops::{
    CloseDesktop, CreateDesktopW, GetUserObjectInformationW, OpenDesktopW, OpenInputDesktop, SwitchDesktop,
    DESKTOP_READOBJECTS, DESKTOP_SWITCHDESKTOP, HDESK, UOI_NAME,
};
use windows_sys::Win32::System::Threading::{
    CreateProcessW, GetExitCodeProcess, TerminateProcess, WaitForSingleObject, PROCESS_INFORMATION,
    STARTF_USESTDHANDLES, STARTUPINFOW,
};

use crate::debug_log;

pub const DESK: &str = "TestingStudentDesk";
pub const ON_DESK: &str = "--on-desk";

fn wide(text: &str) -> Vec<u16> {
    OsStr::new(text).encode_wide().chain(Some(0)).collect()
}

fn quote(arg: &str) -> String {
    if arg.is_empty() || arg.contains([' ', '\t', '"']) {
        format!("\"{}\"", arg.replace('"', "\\\""))
    } else {
        arg.to_string()
    }
}

/// Запущены на столе теста (копия) или без своего стола.
pub fn is_child() -> bool {
    std::env::args().any(|a| a == ON_DESK)
}

/// Обычный запуск: создать стол, запустить на нём копию, переключить экран, дождаться её, вернуть экран.
/// None — работать по-старому в этом же процессе (`--no-desktop`, мы и есть копия, или стол не создался).
pub fn launch() -> Option<i32> {
    let args: Vec<String> = std::env::args().collect();
    if is_child() || args.iter().any(|a| a == "--no-desktop") {
        return None;
    }
    let exit_after: Option<u64> = args
        .iter()
        .position(|a| a == "--exit-after")
        .and_then(|i| args.get(i + 1))
        .and_then(|v| v.parse().ok());
    unsafe {
        let name = wide(DESK);
        let desk = CreateDesktopW(name.as_ptr(), std::ptr::null(), std::ptr::null(), 0, GENERIC_ALL,
                                  std::ptr::null());
        if desk.is_null() {
            debug_log!("свой рабочий стол не создался — работаем на обычном");
            return None;
        }
        let exe = std::env::current_exe().ok()?.to_string_lossy().into_owned();
        let mut command = quote(&exe);
        for arg in &args[1..] {
            command.push(' ');
            command.push_str(&quote(arg));
        }
        command.push(' ');
        command.push_str(ON_DESK);
        let mut command = wide(&command);
        let mut desk_name = wide(DESK);
        let mut startup: STARTUPINFOW = std::mem::zeroed();
        startup.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
        startup.lpDesktop = desk_name.as_mut_ptr();
        // диагностика (STUDENT_APP_DEBUG) идёт в тот же файл, что у этого процесса
        startup.dwFlags = STARTF_USESTDHANDLES;
        startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
        startup.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
        startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);
        let mut process: PROCESS_INFORMATION = std::mem::zeroed();
        let started = CreateProcessW(std::ptr::null(), command.as_mut_ptr(), std::ptr::null(), std::ptr::null(),
                                     1, 0, std::ptr::null(), std::ptr::null(), &startup, &mut process);
        if started == 0 {
            debug_log!("копия на своём столе не запустилась — работаем на обычном");
            CloseDesktop(desk);
            return None;
        }
        CloseHandle(process.hThread);
        SwitchDesktop(desk);
        debug_log!("экран переключён на стол теста");

        // страховка для проверки на своём компьютере: копия не закрылась сама — закрыть её
        let deadline = exit_after.map(|s| std::time::Instant::now() + std::time::Duration::from_secs(s + 15));
        while WaitForSingleObject(process.hProcess, 500) == WAIT_TIMEOUT {
            if deadline.is_some_and(|d| std::time::Instant::now() > d) {
                TerminateProcess(process.hProcess, 1);
            }
        }
        let mut code = 0u32;
        GetExitCodeProcess(process.hProcess, &mut code);
        CloseHandle(process.hProcess);
        switch_to("Default");
        CloseDesktop(desk);
        debug_log!("экран возвращён на обычный стол");
        Some(code as i32)
    }
}

fn switch_to(name: &str) -> bool {
    unsafe {
        let name = wide(name);
        let desk = OpenDesktopW(name.as_ptr(), 0, 0, DESKTOP_SWITCHDESKTOP);
        if desk.is_null() {
            return false;
        }
        let ok = SwitchDesktop(desk) != 0;
        CloseDesktop(desk);
        ok
    }
}

/// Имя стола, который сейчас на экране. None — экран Ctrl+Alt+Del или блокировки: к нему доступа нет.
pub fn input_desktop() -> Option<String> {
    unsafe {
        let desk: HDESK = OpenInputDesktop(0, 0, DESKTOP_READOBJECTS);
        if desk.is_null() {
            return None;
        }
        let mut buffer = [0u16; 256];
        let mut needed = 0u32;
        let ok = GetUserObjectInformationW(desk as HANDLE, UOI_NAME, buffer.as_mut_ptr().cast(),
                                           (buffer.len() * 2) as u32, &mut needed);
        CloseDesktop(desk);
        if ok == 0 {
            return None;
        }
        let len = buffer.iter().position(|&c| c == 0).unwrap_or(buffer.len());
        Some(String::from_utf16_lossy(&buffer[..len]))
    }
}

/// Ученик ушёл на обычный стол (через Ctrl+Alt+Del → «Диспетчер задач» и т. п.) — вернуть стол теста.
pub fn return_to_test_desk() -> bool {
    switch_to(DESK)
}
