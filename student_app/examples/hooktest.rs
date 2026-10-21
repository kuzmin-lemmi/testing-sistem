//! Проверка: доходят ли нажатия до низкоуровневого перехвата клавиатуры (без окна и WebView2).
use windows_sys::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::UI::WindowsAndMessaging::*;

unsafe extern "system" fn hook(code: i32, w: WPARAM, l: LPARAM) -> LRESULT {
    let key = unsafe { &*(l as *const KBDLLHOOKSTRUCT) };
    eprintln!("key vk={:#x} msg={:#x} flags={:#x}", key.vkCode, w, key.flags);
    unsafe { CallNextHookEx(std::ptr::null_mut(), code, w, l) }
}

fn main() {
    std::thread::spawn(|| { std::thread::sleep(std::time::Duration::from_secs(8)); std::process::exit(0) });
    unsafe {
        let h = SetWindowsHookExW(WH_KEYBOARD_LL, Some(hook), GetModuleHandleW(std::ptr::null()), 0);
        eprintln!("hook {h:?}");
        let mut m: MSG = std::mem::zeroed();
        while GetMessageW(&mut m, std::ptr::null_mut(), 0, 0) > 0 { DispatchMessageW(&m); }
    }
}
