//! LibreOffice (или Excel) для заданий с файлом: страница теста (`static/js/office.js`) просит открыть файл
//! задания, программа открывает его на столе теста и держит окна документа под шапкой теста — как вкладки:
//! «Тест» и документы. Щелчок по шапке — впереди снова тест (`lock.rs`: разрешённая программа уступает тесту).
//!
//! - Файл — в `%LOCALAPPDATA%\Тестирование\office\<ученик>\<задание>` под настоящим именем; уже есть — это
//!   работа ученика (программа могла закрыться посреди теста), не трогаем. Папка у каждого ученика своя:
//!   на школьных компьютерах одна учётная запись на всех, следующий ученик не должен получить чужой файл.
//! - Свой профиль LibreOffice на время теста: иначе файл уйдёт в уже запущенный LibreOffice на обычном
//!   столе Windows, и ученик его не увидит. Заодно пустые «Недавние документы» и без «Совета дня».
//! - Все процессы — в Job Object: закончился тест или вышла программа — LibreOffice закрывается.
//!
//! Протокол — в начале `office.js`.
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicIsize, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, HWND, LPARAM, RECT};
use windows_sys::Win32::System::JobObjects::{AssignProcessToJobObject, IsProcessInJob};
use windows_sys::Win32::System::Registry::{HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
use windows_sys::Win32::System::Threading::{
    CreateProcessW, OpenProcess, ResumeThread, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, PROCESS_INFORMATION,
    PROCESS_QUERY_LIMITED_INFORMATION, STARTUPINFOW,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    BringWindowToTop, EnumWindows, GetClassNameW, GetForegroundWindow, GetWindow, GetWindowRect, GetWindowTextW,
    GetWindowThreadProcessId, IsIconic, IsWindowVisible, IsZoomed, SetWindowPos, ShowWindow, GW_OWNER, HWND_TOP,
    SWP_NOACTIVATE, SW_RESTORE, SetForegroundWindow, SystemParametersInfoW, SPI_GETWORKAREA,
};

use crate::json;
use crate::python::{put_task_file, registry_string, JobObject, Page};
use crate::{debug_log, server};

/// Чем открывать файлы задания.
#[derive(Clone, Debug, PartialEq)]
enum Kind {
    LibreOffice,
    Excel,
}

#[derive(Clone, Debug)]
struct Office {
    exe: PathBuf,
    kind: Kind,
}

/// Окно документа — вкладка в шапке теста.
#[derive(Clone, Debug, PartialEq)]
struct Tab {
    hwnd: isize,
    name: String,
    kind: &'static str, // calc | writer | other — для значка
    active: bool,
}

#[derive(Default)]
struct State {
    page: Option<Page>,
    /// Высота шапки теста в пикселях экрана: окна документов — ниже неё, шапка с вкладками видна всегда.
    top: i32,
    job: Option<JobObject>,
    tabs: Vec<Tab>,
    /// Окна, которые уже видели: новое окно (только что открыли файл) выводим вперёд один раз.
    seen: Vec<isize>,
}

static STATE: Mutex<Option<State>> = Mutex::new(None);
static TEST_WINDOW: AtomicIsize = AtomicIsize::new(0);

fn with_state<R>(f: impl FnOnce(&mut State) -> R) -> R {
    let mut guard = STATE.lock().unwrap();
    f(guard.get_or_insert_with(State::default))
}

/// Окно теста — для размера и места окон документов. Запускает слежку за окнами документов.
pub fn init(test_window: isize) {
    TEST_WINDOW.store(test_window, Ordering::Relaxed);
    std::thread::spawn(|| loop {
        std::thread::sleep(Duration::from_millis(250));
        watch();
    });
}

/// Сообщение страницы (`office:` уже отрезано). Вызывается в потоке окна — ничего долгого здесь.
pub fn handle(message: &str, origin: &str, page: &Page) {
    let Some(message) = json::parse(message) else {
        debug_log!("office: непонятное сообщение {message}");
        return;
    };
    if let Some(top) = message.id("top") {
        with_state(|s| s.top = top as i32);
    }
    match message.str("type") {
        Some("hello") => {
            with_state(|s| {
                s.page = Some(page.clone());
                s.tabs.clear(); // страница новая — вкладки нарисовать заново
            });
            let page = page.clone();
            std::thread::spawn(move || {
                let office = find();
                debug_log!("office: {office:?}");
                let (calc, writer) = match office.as_ref().map(|o| &o.kind) {
                    Some(Kind::LibreOffice) => ("LibreOffice Calc", "LibreOffice Writer"),
                    Some(Kind::Excel) => ("Excel", ""),
                    None => ("", ""),
                };
                script(&page, format!("ready({{calc: {}, writer: {}}})", json::quote(calc), json::quote(writer)));
            });
        }
        Some("open") => {
            let Some(task) = message.id("task") else { return };
            let student = message.id("student").unwrap_or(0);
            let Some(file) = message.get("file") else { return };
            let (Some(url), Some(name)) = (file.str("url"), file.str("name")) else { return };
            let (url, name, origin, page) = (url.to_string(), name.to_string(), origin.to_string(), page.clone());
            std::thread::spawn(move || match open(student, task, &url, &name, &origin) {
                Ok(()) => script(&page, format!("opened({task})")),
                Err(message) => script(&page, format!("failed({task}, {})", json::quote(&message))),
            });
        }
        Some("show") => match message.id("id") {
            Some(0) => bring_forward(TEST_WINDOW.load(Ordering::Relaxed)),
            Some(hwnd) => show(hwnd as isize),
            None => {}
        },
        _ => {}
    }
}

/// Тест закончился (открылась оценка): закрыть LibreOffice вместе со всеми документами.
pub fn close_all() {
    if let Some(job) = with_state(|s| s.job.take()) {
        job.terminate();
    }
}

fn script(page: &Page, call: String) {
    page(format!("window.studentOffice && window.studentOffice.{call}"));
}

// --- открыть файл ---------------------------------------------------------------------

/// Папка документов: `%LOCALAPPDATA%\Тестирование\office` (в тестах — временная, профиль не трогаем).
fn work_root() -> PathBuf {
    if cfg!(test) { std::env::temp_dir().join("student_app_office_test") } else { server::settings_dir().join("office") }
}

fn open(student: u64, task: u64, url: &str, name: &str, origin: &str) -> Result<(), String> {
    let office = find().ok_or("На этом компьютере нет LibreOffice — обратитесь к учителю")?;
    let dir = student_dir(&work_root(), student).join(task.to_string());
    std::fs::create_dir_all(&dir).map_err(|e| format!("Не удалось создать папку для файла: {e}"))?;
    let file = put_task_file(origin, url, name, &dir, true)?;
    let command = match office.kind {
        Kind::LibreOffice => {
            let profile = work_root().join("profile");
            prepare_profile(&profile);
            format!(
                "{} -env:UserInstallation={} --nologo --norestore {}{}",
                quote(&office.exe.to_string_lossy()),
                file_url(&profile),
                calc_filter(&file),
                quote(&file.to_string_lossy())
            )
        }
        // /x — отдельный Excel: иначе файл уйдёт в Excel, уже открытый на обычном столе
        Kind::Excel => format!("{} /x {}", quote(&office.exe.to_string_lossy()), quote(&file.to_string_lossy())),
    };
    debug_log!("office: {command}");
    let job = with_state(|s| {
        if s.job.is_none() {
            s.job = JobObject::create(None);
        }
        s.job.as_ref().map(|j| j.handle() as isize)
    });
    launch(&command, &dir, job.map(|h| h as HANDLE))
}

/// Папка ученика `student`. Первый файл нового ученика — папки прежних учеников больше не нужны
/// (их тесты закончились), удаляем; свой профиль LibreOffice (`profile`) остаётся.
fn student_dir(root: &Path, student: u64) -> PathBuf {
    let dir = root.join(format!("ученик {student}"));
    if !dir.exists() {
        for entry in std::fs::read_dir(root).into_iter().flatten().flatten() {
            if entry.file_name().to_string_lossy().starts_with("ученик ") {
                let _ = std::fs::remove_dir_all(entry.path());
            }
        }
    }
    dir
}

/// Запуск «спящим», в Job Object и только потом — работать: soffice.exe сразу запускает soffice.bin,
/// и тот не должен успеть родиться вне Job Object.
fn launch(command: &str, dir: &Path, job: Option<HANDLE>) -> Result<(), String> {
    let mut command: Vec<u16> = command.encode_utf16().chain(Some(0)).collect();
    let dir: Vec<u16> = dir.to_string_lossy().encode_utf16().chain(Some(0)).collect();
    unsafe {
        let mut startup: STARTUPINFOW = std::mem::zeroed();
        startup.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
        let mut info: PROCESS_INFORMATION = std::mem::zeroed();
        let ok = CreateProcessW(
            std::ptr::null(),
            command.as_mut_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            0,
            CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
            std::ptr::null(),
            dir.as_ptr(),
            &startup,
            &mut info,
        );
        if ok == 0 {
            return Err(format!("Не удалось запустить LibreOffice (ошибка {})", std::io::Error::last_os_error()));
        }
        if job.is_some_and(|job| AssignProcessToJobObject(job, info.hProcess) == 0) {
            debug_log!("office: Job Object не подключился");
        }
        ResumeThread(info.hThread);
        CloseHandle(info.hThread);
        CloseHandle(info.hProcess);
    }
    Ok(())
}

/// `.txt` LibreOffice сам открывает в Writer, а на странице кнопка — «Открыть в LibreOffice Calc»:
/// числа из файла задания нужны в таблице. Фильтр «Текст CSV» — Calc с окном «Импорт текста».
fn calc_filter(file: &Path) -> &'static str {
    let txt = file.extension().is_some_and(|e| e.eq_ignore_ascii_case("txt"));
    if txt { "\"--infilter=Text - txt - csv (StarCalc)\" " } else { "" }
}

fn quote(text: &str) -> String {
    format!("\"{text}\"")
}

/// `file:///C:/Users/%D0%A3.../profile` — адрес папки для `-env:UserInstallation`.
fn file_url(path: &Path) -> String {
    let path = path.to_string_lossy().replace('\\', "/");
    let mut url = String::from("file:///");
    for byte in path.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' | b'/' | b':' => url.push(byte as char),
            _ => url.push_str(&format!("%{byte:02X}")),
        }
    }
    url
}

/// Новый профиль LibreOffice — без «Совета дня» и проверки обновлений. Дальше LibreOffice ведёт его сам.
fn prepare_profile(profile: &Path) {
    let settings = profile.join("user").join("registrymodifications.xcu");
    if settings.exists() {
        return;
    }
    let _ = std::fs::create_dir_all(settings.parent().unwrap());
    let _ = std::fs::write(&settings, PROFILE_SETTINGS);
}

const PROFILE_SETTINGS: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="ShowTipOfTheDay" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Jobs/Jobs/org.openoffice.Office.Jobs:Job['UpdateCheck']/Arguments"><prop oor:name="AutoCheckEnabled" oor:op="fuse"><value>false</value></prop></item>
</oor:items>
"#;

// --- где LibreOffice ------------------------------------------------------------------

/// Найденное запоминаем; не нашли — ищем снова в следующий раз (вдруг поставили во время урока).
static FOUND: OnceLock<Office> = OnceLock::new();

fn find() -> Option<Office> {
    if let Some(office) = FOUND.get() {
        return Some(office.clone());
    }
    let office = libre_office().map(|exe| Office { exe, kind: Kind::LibreOffice })
        .or_else(|| excel().map(|exe| Office { exe, kind: Kind::Excel }))?;
    Some(FOUND.get_or_init(|| office).clone())
}

/// Реестр установщика LibreOffice (`UNO\InstallPath` — папка `program`), потом обычные папки.
fn libre_office() -> Option<PathBuf> {
    let from_registry = [HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER]
        .into_iter()
        .filter_map(|root| registry_string(root, r"SOFTWARE\LibreOffice\UNO\InstallPath", None))
        .map(|dir| PathBuf::from(dir).join("soffice.exe"));
    let usual = ["ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"]
        .into_iter()
        .filter_map(std::env::var_os)
        .map(|dir| PathBuf::from(dir).join("LibreOffice").join("program").join("soffice.exe"));
    from_registry.chain(usual).find(|exe| exe.is_file())
}

fn excel() -> Option<PathBuf> {
    [HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER]
        .into_iter()
        .filter_map(|root| registry_string(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\excel.exe", None))
        .map(PathBuf::from)
        .find(|exe| exe.is_file())
}

// --- окна документов: вкладки и место под шапкой ----------------------------------------

/// Раз в 250 мс: какие окна документов открыты, какое впереди; окна — на своё место под шапкой.
fn watch() {
    let Some(job) = with_state(|s| s.job.as_ref().map(|j| j.handle() as isize)) else { return };
    let all = job_windows(job as HANDLE);
    let frames: Vec<(isize, String)> =
        all.iter().filter(|w| w.document).map(|w| (w.hwnd, w.title.clone())).collect();
    let front = unsafe { GetForegroundWindow() } as isize;
    let tabs: Vec<Tab> = frames
        .iter()
        .map(|(hwnd, title)| {
            let (name, kind) = tab_name(title);
            Tab { hwnd: *hwnd, name, kind, active: *hwnd == front }
        })
        .collect();
    let slot = slot(with_state(|s| s.top));
    let new: Vec<&Window> = with_state(|s| {
        let new = all.iter().filter(|w| !s.seen.contains(&w.hwnd)).collect();
        s.seen = all.iter().map(|w| w.hwnd).collect();
        new
    });
    for (hwnd, _) in &frames {
        place(*hwnd, slot);
    }
    // только что открыли файл — документ сразу впереди; диалог («Импорт текста» для .csv и .txt) — тоже,
    // иначе он окажется позади окна теста, и ученик его не увидит
    for window in new {
        if window.document {
            show(window.hwnd);
        } else {
            bring_forward(window.hwnd);
        }
    }
    let changed = with_state(|s| {
        if s.tabs == tabs {
            return None;
        }
        s.tabs = tabs.clone();
        s.page.clone()
    });
    if let Some(page) = changed {
        let items: Vec<String> = tabs
            .iter()
            .map(|t| format!("{{id: {}, name: {}, kind: '{}', active: {}}}", t.hwnd, json::quote(&t.name), t.kind, t.active))
            .collect();
        script(&page, format!("tabs([{}])", items.join(", ")));
    }
}

/// «Числа.xlsx — LibreOffice Calc» (LibreOffice 7.6+ — длинное тире, раньше и Excel — дефис) → («Числа.xlsx», calc).
fn tab_name(title: &str) -> (String, &'static str) {
    let (name, app) = title.rsplit_once(" — ").or_else(|| title.rsplit_once(" - ")).unwrap_or((title, title));
    let kind = if app.contains("Calc") || app.contains("Excel") {
        "calc"
    } else if app.contains("Writer") || app.contains("Word") {
        "writer"
    } else {
        "other"
    };
    (name.trim().to_string(), kind)
}

/// Место окна документа: весь экран теста под шапкой, но не под панелью задач Windows
/// (на своём столе теста её нет, а с `--no-desktop` она закрыла бы низ документа).
fn slot(top: i32) -> RECT {
    let mut rect: RECT = unsafe { std::mem::zeroed() };
    let mut work: RECT = unsafe { std::mem::zeroed() };
    unsafe {
        GetWindowRect(TEST_WINDOW.load(Ordering::Relaxed) as HWND, &mut rect);
        if SystemParametersInfoW(SPI_GETWORKAREA, 0, &mut work as *mut RECT as *mut _, 0) != 0
            && work.right > work.left
        {
            rect.left = rect.left.max(work.left);
            rect.right = rect.right.min(work.right);
            rect.bottom = rect.bottom.min(work.bottom);
        }
    }
    rect.top += top.max(0);
    rect
}

fn place(hwnd: isize, slot: RECT) {
    let hwnd = hwnd as HWND;
    unsafe {
        if IsIconic(hwnd) != 0 || slot.right <= slot.left || slot.bottom <= slot.top {
            return;
        }
        if IsZoomed(hwnd) != 0 {
            ShowWindow(hwnd, SW_RESTORE); // развёрнутое на весь экран закрыло бы шапку с вкладками
        }
        let mut now: RECT = std::mem::zeroed();
        GetWindowRect(hwnd, &mut now);
        if (now.left, now.top, now.right, now.bottom) != (slot.left, slot.top, slot.right, slot.bottom) {
            SetWindowPos(
                hwnd,
                HWND_TOP,
                slot.left,
                slot.top,
                slot.right - slot.left,
                slot.bottom - slot.top,
                SWP_NOACTIVATE,
            );
        }
    }
}

fn show(hwnd: isize) {
    let slot = slot(with_state(|s| s.top));
    unsafe {
        if IsIconic(hwnd as HWND) != 0 {
            ShowWindow(hwnd as HWND, SW_RESTORE);
        }
    }
    place(hwnd, slot);
    bring_forward(hwnd);
}

/// Окно — вперёд. Без нажатия Alt, как в `lock::take_focus`: Alt на окне «Импорт текста» открывает его
/// системное меню («Переместить, Размер…»), на документе — меню LibreOffice. Он и не нужен: ученик только что
/// щёлкнул по окну теста (кнопка, вкладка), программа впереди, и Windows разрешает ей вывести вперёд другое окно.
fn bring_forward(hwnd: isize) {
    let hwnd = hwnd as HWND;
    unsafe {
        if IsIconic(hwnd) != 0 {
            ShowWindow(hwnd, SW_RESTORE);
        }
        BringWindowToTop(hwnd);
        SetForegroundWindow(hwnd);
    }
}

/// Видимое окно с заголовком из нашего Job Object.
struct Window {
    hwnd: isize,
    title: String,
    /// Главное окно документа LibreOffice / Excel — вкладка; иначе диалог (у него есть владелец).
    document: bool,
}

fn job_windows(job: HANDLE) -> Vec<Window> {
    struct Search {
        job: HANDLE,
        found: Vec<Window>,
    }
    unsafe extern "system" fn each(hwnd: HWND, data: LPARAM) -> windows_sys::core::BOOL {
        let search = unsafe { &mut *(data as *mut Search) };
        unsafe {
            if IsWindowVisible(hwnd) == 0 {
                return 1;
            }
            let title = window_text(hwnd, false);
            if title.is_empty() {
                return 1;
            }
            let mut pid = 0u32;
            GetWindowThreadProcessId(hwnd, &mut pid);
            let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if process.is_null() {
                return 1;
            }
            let mut inside = 0;
            IsProcessInJob(process, search.job, &mut inside);
            CloseHandle(process);
            if inside != 0 {
                let class = window_text(hwnd, true);
                let document = (class == "SALFRAME" || class == "XLMAIN") && GetWindow(hwnd, GW_OWNER).is_null();
                search.found.push(Window { hwnd: hwnd as isize, title, document });
            }
        }
        1
    }
    let mut search = Search { job, found: Vec::new() };
    unsafe { EnumWindows(Some(each), &mut search as *mut Search as LPARAM) };
    search.found.sort_by_key(|w| w.hwnd); // порядок вкладок не прыгает
    search.found
}

unsafe fn window_text(hwnd: HWND, class: bool) -> String {
    let mut buffer = [0u16; 512];
    let len = unsafe {
        if class {
            GetClassNameW(hwnd, buffer.as_mut_ptr(), buffer.len() as i32)
        } else {
            GetWindowTextW(hwnd, buffer.as_mut_ptr(), buffer.len() as i32)
        }
    };
    String::from_utf16_lossy(&buffer[..len.max(0) as usize])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tab_names() {
        assert_eq!(tab_name("Числа.xlsx — LibreOffice Calc"), ("Числа.xlsx".to_string(), "calc"));
        assert_eq!(tab_name("Числа - копия.xlsx — LibreOffice Calc"), ("Числа - копия.xlsx".to_string(), "calc"));
        assert_eq!(tab_name("Числа.xlsx - LibreOffice Calc"), ("Числа.xlsx".to_string(), "calc"));
        assert_eq!(tab_name("Ответ.odt - LibreOffice Writer"), ("Ответ.odt".to_string(), "writer"));
        assert_eq!(tab_name("Числа.xlsx - Excel"), ("Числа.xlsx".to_string(), "calc"));
        assert_eq!(tab_name("Таблица - копия.ods - LibreOffice Calc"), ("Таблица - копия.ods".to_string(), "calc"));
        assert_eq!(tab_name("LibreOffice"), ("LibreOffice".to_string(), "other"));
    }

    #[test]
    fn each_student_has_own_folder() {
        let root = std::env::temp_dir().join("student_app_office_students_test");
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("profile")).unwrap();
        let first = student_dir(&root, 7).join("3");
        std::fs::create_dir_all(&first).unwrap();
        std::fs::write(first.join("Числа.xlsx"), "правки ученика 7").unwrap();
        // тот же ученик (программа закрылась и открылась снова) — его файл на месте
        assert!(student_dir(&root, 7).join("3").join("Числа.xlsx").exists());
        // следующий ученик — своя пустая папка, файл прежнего удалён, профиль LibreOffice на месте
        let next = student_dir(&root, 8);
        assert!(!next.exists());
        assert!(!root.join("ученик 7").exists());
        assert!(root.join("profile").exists());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn profile_url() {
        let url = file_url(Path::new(r"C:\Users\ученик\AppData\Local\Тестирование\office\profile"));
        assert!(url.starts_with("file:///C:/Users/%D1%83%D1%87"), "{url}");
        assert!(url.ends_with("/office/profile"), "{url}");
        assert!(!url.contains(' ') && !url.contains('\\'));
        assert_eq!(file_url(Path::new(r"C:\Program Files\x")), "file:///C:/Program%20Files/x");
    }

    /// Настоящий LibreOffice этого компьютера (если есть): находится, открывается в своём профиле;
    /// новый документ — окно документа (вкладка), .csv — сначала диалог импорта (вперёд, но не вкладка);
    /// всё закрывается вместе с Job Object.
    #[test]
    fn real_libre_office() {
        let Some(office) = find() else { return };
        if office.kind != Kind::LibreOffice {
            return;
        }
        let _ = std::fs::remove_dir_all(work_root());
        let dir = work_root().join("1");
        std::fs::create_dir_all(&dir).unwrap();
        let csv = dir.join("Проба.csv");
        std::fs::write(&csv, "1;2\n3;4\n").unwrap();
        let profile = work_root().join("profile");
        prepare_profile(&profile);
        // окно «Импорт текста» модальное: пока оно открыто, LibreOffice не берёт следующий файл (ученик сначала
        // нажмёт «ОК») — поэтому каждая проверка в своём запуске
        let phase = |args: &str, what: &dyn Fn(&Window) -> bool, why: &str| {
            let job = JobObject::create(None).unwrap();
            for args in ["--calc", args] {
                let command = format!(
                    "{} -env:UserInstallation={} --nologo --norestore {args}",
                    quote(&office.exe.to_string_lossy()),
                    file_url(&profile)
                );
                launch(&command, &dir, Some(job.handle())).unwrap();
            }
            let mut seen = Vec::new();
            for _ in 0..160 {
                std::thread::sleep(Duration::from_millis(250));
                seen = job_windows(job.handle());
                if seen.iter().any(what) {
                    break;
                }
            }
            let titles: Vec<String> = seen.iter().map(|w| w.title.clone()).collect();
            job.terminate();
            std::thread::sleep(Duration::from_millis(1500)); // профиль освобождается не сразу
            assert!(job_windows(job.handle()).is_empty(), "{why}: не закрылось");
            assert!(seen.iter().any(|w| w.document && w.title.contains("LibreOffice Calc")), "{why}: нет Calc: {titles:?}");
            assert!(seen.iter().any(what), "{why}: {titles:?}");
            assert!(!titles.iter().any(|t| t.contains("Writer")), "{why}: открылся Writer: {titles:?}");
        };
        let txt = dir.join("Проба.txt");
        std::fs::write(&txt, "12\n34\n").unwrap();
        // .txt — в Calc (окно импорта или таблица), а не в Writer
        phase(&format!("{}{}", calc_filter(&txt), quote(&txt.to_string_lossy())),
              &|w| w.title.contains("Проба.txt"), ".txt");
        phase(&quote(&csv.to_string_lossy()), &|w| !w.document && w.title.contains("Проба.csv"), ".csv: диалог импорта");
        assert!(profile.join("user").exists());
    }
}
