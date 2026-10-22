//! Установка: первый запуск (с флешки, из папки) копирует программу в профиль ученика
//! (`%LOCALAPPDATA%\Programs\Тестирование\Тестирование.exe`) и кладёт на рабочий стол ярлык «Тестирование».
//! Новая версия, запущенная так же, заменяет старую. Ничего лишнего на рабочем столе, права администратора не нужны.
//!
//! Не устанавливаем: уже запущены из установленной папки, `--portable`, `--server` (проверка), отладочная сборка.
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::debug_log;

pub const NAME: &str = "Тестирование";

pub fn install_dir() -> Option<PathBuf> {
    Some(PathBuf::from(std::env::var_os("LOCALAPPDATA")?).join("Programs").join(NAME))
}

fn same_file(a: &Path, b: &Path) -> bool {
    match (a.canonicalize(), b.canonicalize()) {
        (Ok(a), Ok(b)) => a == b,
        _ => false,
    }
}

/// Some(код) — установили и отработали установленной копией: этому процессу выйти с этим кодом.
pub fn install_if_needed() -> Option<i32> {
    let args: Vec<String> = std::env::args().collect();
    let skip = ["--portable", "--server", crate::desktop::ON_DESK];
    if cfg!(debug_assertions) || args.iter().any(|a| skip.contains(&a.as_str())) {
        return None;
    }
    let current = std::env::current_exe().ok()?;
    let dir = install_dir()?;
    let target = dir.join(format!("{NAME}.exe"));
    if same_file(&current, &target) {
        return None;
    }
    std::fs::create_dir_all(&dir).ok()?;
    if let Err(error) = std::fs::copy(&current, &target) {
        // установленная копия сейчас запущена — работаем отсюда
        debug_log!("не удалось установить в {}: {error}", target.display());
        return None;
    }
    create_shortcut(&target);
    debug_log!("установлено: {}", target.display());
    let status = Command::new(&target).args(&args[1..]).status().ok()?;
    Some(status.code().unwrap_or(0))
}

/// Ярлык «Тестирование» на рабочем столе — через WScript.Shell (PowerShell есть в любой Windows 10/11).
fn create_shortcut(target: &Path) {
    let target = target.to_string_lossy().replace('\'', "''");
    let folder = target.rsplit_once('\\').map(|(dir, _)| dir.to_string()).unwrap_or_default();
    let script = format!(
        "$path = Join-Path ([Environment]::GetFolderPath('Desktop')) '{NAME}.lnk'; \
         $link = (New-Object -ComObject WScript.Shell).CreateShortcut($path); \
         $link.TargetPath = '{target}'; $link.WorkingDirectory = '{folder}'; \
         $link.IconLocation = '{target},0'; $link.Description = '{NAME}'; $link.Save()"
    );
    // -EncodedCommand (UTF-16LE в base64): русские имена и кавычки доходят без искажений
    let utf16: Vec<u8> = script.encode_utf16().flat_map(|unit| unit.to_le_bytes()).collect();
    let result = Command::new("powershell")
        .args(["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", &base64(&utf16)])
        .creation_flags_no_window()
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null()) // PowerShell пишет туда служебный «прогресс»
        .status();
    debug_log!("ярлык на рабочем столе: {result:?}");
}

fn base64(data: &[u8]) -> String {
    const ABC: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in data.chunks(3) {
        let n = chunk.iter().enumerate().fold(0u32, |acc, (i, &b)| acc | (b as u32) << (16 - 8 * i));
        for i in 0..4 {
            out.push(if i <= chunk.len() { ABC[(n >> (18 - 6 * i) & 63) as usize] as char } else { '=' });
        }
    }
    out
}

trait NoWindow {
    fn creation_flags_no_window(&mut self) -> &mut Self;
}

impl NoWindow for Command {
    fn creation_flags_no_window(&mut self) -> &mut Self {
        use std::os::windows::process::CommandExt;
        self.creation_flags(0x0800_0000) // CREATE_NO_WINDOW: без мелькающего чёрного окна
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn base64_matches_standard() {
        assert_eq!(super::base64(b"Man"), "TWFu");
        assert_eq!(super::base64(b"Ma"), "TWE=");
        assert_eq!(super::base64(b"M"), "TQ==");
    }
}
