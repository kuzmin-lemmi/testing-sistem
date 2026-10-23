//! Python ученика: страница теста (`static/js/py_editor.js`) присылает код, программа выполняет его
//! установленным у ученика Python и возвращает вывод. Протокол — в начале `py_editor.js`
//! и в `docs/ПЛАН_PYTHON_УЧЕНИКА.md`.
//!
//! - Код — в `%LOCALAPPDATA%\Тестирование\python\<задание>\solution.py`, рядом — файл задания под его
//!   настоящим именем: ученик пишет `open('17.txt')`, как в IDLE.
//! - Процесс — в Job Object: «Остановить», 30 с и закрытие программы убивают и его потомков;
//!   память на всё — 1 ГБ (зацикленный `append` получает `MemoryError`, а не вешает компьютер).
//! - Вывод уходит странице кусками не чаще раза в 50 мс: `print` в цикле не заваливает окно.
use std::io::{Read, Write};
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
    TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_JOB_MEMORY,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Registry::{
    RegCloseKey, RegEnumKeyExW, RegGetValueW, RegOpenKeyExW, HKEY, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, KEY_READ,
    RRF_RT_REG_SZ,
};

use crate::json;
use crate::{debug_log, server};

const TIME_LIMIT: Duration = Duration::from_secs(if cfg!(test) { 2 } else { 30 });
const MEMORY_LIMIT: usize = 1024 * 1024 * 1024;
/// Символов на один запуск — как у страницы; дальше вывод читаем и выбрасываем, чтобы процесс не встал.
const OUTPUT_LIMIT: usize = 200_000;
const FLUSH_EVERY: Duration = Duration::from_millis(50);
const CODE_FILE: &str = "solution.py";
const CREATE_NO_WINDOW: u32 = 0x0800_0000; // иначе на столе теста мигнёт чёрное окно консоли

/// Идущий запуск. Один на всю программу: страница не даёт запустить второй, пока идёт первый.
struct Running {
    run: u64,
    stdin: Option<ChildStdin>,
    stop: Arc<AtomicBool>,
}

static RUNNING: Mutex<Option<Running>> = Mutex::new(None);

/// Куда отдавать вызовы `window.studentPython.…`: в программе — событием в поток окна, в тестах — в список.
pub type Page = Arc<dyn Fn(String) + Send + Sync>;

/// Папка запусков: `%LOCALAPPDATA%\Тестирование\python` (в тестах — временная, профиль не трогаем).
fn work_root() -> PathBuf {
    if cfg!(test) { std::env::temp_dir().join("student_app_python_test") } else { server::settings_dir().join("python") }
}

/// Сообщение страницы (`py:` уже отрезано). Вызывается в потоке окна — ничего долгого здесь.
pub fn handle(message: &str, origin: &str, page: &Page) {
    let Some(message) = json::parse(message) else {
        debug_log!("python: непонятное сообщение {message}");
        return;
    };
    match message.str("type") {
        Some("hello") => {
            stop_all(); // страница загрузилась заново: прежний запуск ей уже не нужен
            let page = page.clone();
            std::thread::spawn(move || match find() {
                Some(python) => script(&page, format!("ready({{version: {}}})", json::quote(&python.version))),
                None => script(&page, "missing()".into()),
            });
        }
        Some("run") => {
            let (Some(run), Some(task), Some(code)) = (message.id("run"), message.id("task"), message.str("code")) else {
                return;
            };
            let file = message.get("file").and_then(|f| Some((f.str("url")?.to_string(), f.str("name")?.to_string())));
            stop_all();
            let stop = Arc::new(AtomicBool::new(false));
            *RUNNING.lock().unwrap() = Some(Running { run, stdin: None, stop: stop.clone() });
            let job = Job { run, task, code: code.to_string(), file, origin: origin.to_string(), stop };
            let page = page.clone();
            std::thread::spawn(move || {
                let result = execute(&job, &page);
                finish(&page, run, result);
            });
        }
        Some("stdin") => {
            let (Some(run), Some(text)) = (message.id("run"), message.str("text")) else { return };
            let mut guard = RUNNING.lock().unwrap();
            if let Some(stdin) = guard.as_mut().filter(|r| r.run == run).and_then(|r| r.stdin.as_mut()) {
                let _ = stdin.write_all(format!("{text}\n").as_bytes()).and_then(|_| stdin.flush());
            }
        }
        Some("stop") => {
            if let Some(running) = RUNNING.lock().unwrap().as_ref().filter(|r| Some(r.run) == message.id("run")) {
                running.stop.store(true, Ordering::Relaxed);
            }
        }
        _ => {}
    }
}

/// Остановить идущий запуск (ушли со страницы теста, новый запуск, страница перезагрузилась).
pub fn stop_all() {
    if let Some(running) = RUNNING.lock().unwrap().take() {
        running.stop.store(true, Ordering::Relaxed);
    }
}

fn script(page: &Page, call: String) {
    page(format!("window.studentPython && window.studentPython.{call}"));
}

struct Job {
    run: u64,
    task: u64,
    code: String,
    file: Option<(String, String)>,
    origin: String,
    stop: Arc<AtomicBool>,
}

/// Чем закончился запуск — для `exit(run, {...})` на странице.
#[derive(Debug, PartialEq)]
enum Outcome {
    Done { code: i32, seconds: f64 },
    Stopped { seconds: f64 },
    Timeout { seconds: f64 },
    Failed(String),
}

fn finish(page: &Page, run: u64, outcome: Outcome) {
    let mut guard = RUNNING.lock().unwrap();
    if guard.as_ref().is_some_and(|r| r.run == run) {
        *guard = None;
    }
    drop(guard);
    let result = match outcome {
        Outcome::Done { code, seconds } => format!("{{code: {code}, seconds: {seconds:.3}, reason: 'done'}}"),
        Outcome::Stopped { seconds } => format!("{{code: 1, seconds: {seconds:.3}, reason: 'stopped'}}"),
        Outcome::Timeout { seconds } => format!("{{code: 1, seconds: {seconds:.3}, reason: 'timeout'}}"),
        Outcome::Failed(message) => format!("{{code: 1, seconds: 0, reason: 'failed', message: {}}}", json::quote(&message)),
    };
    debug_log!("python: запуск {run} — {result}");
    script(page, format!("exit({run}, {result})"));
}

fn execute(job: &Job, page: &Page) -> Outcome {
    let Some(python) = find() else {
        script(page, "missing()".into());
        return Outcome::Failed("Python не установлен на этом компьютере — обратитесь к учителю".into());
    };
    let dir = work_root().join(job.task.to_string());
    if let Err(e) = std::fs::create_dir_all(&dir) {
        return Outcome::Failed(format!("Не удалось создать папку для программы: {e}"));
    }
    if let Some(Err(message)) = job.file.as_ref().map(|(url, name)| put_task_file(&job.origin, url, name, &dir)) {
        return Outcome::Failed(message);
    }
    if std::fs::write(dir.join(CODE_FILE), &job.code).is_err() {
        return Outcome::Failed("Не удалось сохранить код программы".into());
    }
    if job.stop.load(Ordering::Relaxed) {
        return Outcome::Stopped { seconds: 0.0 };
    }

    let mut child = match Command::new(&python.exe)
        .args(["-u", "-X", "utf8", CODE_FILE])
        .current_dir(&dir)
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
    {
        Ok(child) => child,
        Err(e) => return Outcome::Failed(format!("Не удалось запустить Python: {e}")),
    };
    let started = Instant::now();
    let limits = JobObject::new(&child);
    if let Some(running) = RUNNING.lock().unwrap().as_mut().filter(|r| r.run == job.run) {
        running.stdin = child.stdin.take();
    }

    let output = Arc::new(Mutex::new(Output::default()));
    let readers = [
        reader(child.stdout.take().map(|s| Box::new(s) as Box<dyn Read + Send>), Stream::Out, &dir, &output),
        reader(child.stderr.take().map(|s| Box::new(s) as Box<dyn Read + Send>), Stream::Err, &dir, &output),
    ];

    let mut last_flush = Instant::now();
    let outcome = loop {
        let seconds = started.elapsed().as_secs_f64();
        if let Ok(Some(status)) = child.try_wait() {
            break Outcome::Done { code: status.code().unwrap_or(1), seconds };
        }
        if job.stop.load(Ordering::Relaxed) {
            kill(&mut child, &limits);
            break Outcome::Stopped { seconds };
        }
        if started.elapsed() >= TIME_LIMIT {
            kill(&mut child, &limits);
            break Outcome::Timeout { seconds: TIME_LIMIT.as_secs_f64() };
        }
        if last_flush.elapsed() >= FLUSH_EVERY {
            flush(page, job.run, &output);
            last_flush = Instant::now();
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    // программа кончилась, а запущенные ею процессы ещё держат вывод — тоже закрываем
    kill(&mut child, &limits);
    if let Some(running) = RUNNING.lock().unwrap().as_mut().filter(|r| r.run == job.run) {
        running.stdin = None;
    }
    for reader in readers.into_iter().flatten() {
        let _ = reader.join();
    }
    flush(page, job.run, &output);
    outcome
}

fn kill(child: &mut Child, limits: &Option<JobObject>) {
    match limits {
        Some(job) => job.terminate(),
        None => {
            let _ = child.kill();
        }
    }
    let _ = child.wait();
}

/// Файл задания — из кеша (`python\files`, скачан один раз за тест) заново в папку задания:
/// ученик мог его испортить своей программой.
fn put_task_file(origin: &str, url: &str, name: &str, dir: &Path) -> Result<(), String> {
    let name = safe_file_name(name).ok_or_else(|| format!("Неподходящее имя файла задания «{name}»"))?;
    let cache_dir = work_root().join("files");
    let _ = std::fs::create_dir_all(&cache_dir);
    let cached = cache_dir.join(safe_file_name(url.rsplit('/').next().unwrap_or("")).unwrap_or_else(|| "file".into()));
    if std::fs::metadata(&cached).map(|m| m.len() == 0).unwrap_or(true) {
        if origin.is_empty() {
            return Err(format!("Не удалось скачать файл задания «{name}»: нет связи с учителем"));
        }
        server::http_download(origin, url, &cached)
            .map_err(|e| format!("Не удалось скачать файл задания «{name}» ({e}) — обратитесь к учителю"))?;
    }
    std::fs::copy(&cached, dir.join(&name)).map_err(|e| format!("Не удалось подготовить файл задания «{name}»: {e}"))?;
    Ok(())
}

/// Имя файла без папок и запрещённых в Windows знаков. `None` — пустое, `..` или занято кодом ученика.
fn safe_file_name(name: &str) -> Option<String> {
    let base = name.rsplit(['/', '\\']).next().unwrap_or("");
    let clean: String =
        base.chars().map(|c| if "<>:\"|?*".contains(c) || (c as u32) < 0x20 { '_' } else { c }).collect();
    let clean = clean.trim().trim_end_matches('.').to_string();
    (!clean.is_empty() && !clean.eq_ignore_ascii_case(CODE_FILE)).then_some(clean)
}

// --- вывод ---------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Debug)]
enum Stream {
    Out,
    Err,
}

#[derive(Default)]
struct Output {
    pending: Vec<(Stream, String)>,
    total: usize,
}

impl Output {
    fn push(&mut self, stream: Stream, text: &str) {
        let room = OUTPUT_LIMIT.saturating_sub(self.total);
        if room == 0 || text.is_empty() {
            return;
        }
        let text: String = text.chars().take(room).collect();
        self.total += text.chars().count();
        match self.pending.last_mut() {
            Some((last, buffer)) if *last == stream => buffer.push_str(&text),
            _ => self.pending.push((stream, text)),
        }
    }
}

fn flush(page: &Page, run: u64, output: &Mutex<Output>) {
    let pending = std::mem::take(&mut output.lock().unwrap().pending);
    for (stream, text) in pending {
        let stream = if stream == Stream::Err { "err" } else { "out" };
        script(page, format!("output({run}, '{stream}', {})", json::quote(&text)));
    }
}

/// Поток, читающий stdout или stderr. Ошибки (stderr) отдаём целыми строками, чтобы убрать из них
/// путь к папке ученика: `File "solution.py", line 3`, как при запуске на компьютере учителя.
fn reader(
    pipe: Option<Box<dyn Read + Send>>,
    stream: Stream,
    dir: &Path,
    output: &Arc<Mutex<Output>>,
) -> Option<std::thread::JoinHandle<()>> {
    let mut pipe = pipe?;
    let dir = dir.to_path_buf();
    let output = output.clone();
    Some(std::thread::spawn(move || {
        let mut decoder = Utf8Decoder::default();
        let mut line = String::new();
        let mut buffer = [0u8; 8192];
        loop {
            let read = match pipe.read(&mut buffer) {
                Ok(0) | Err(_) => break,
                Ok(n) => n,
            };
            let text = decoder.feed(&buffer[..read]);
            if stream == Stream::Out {
                output.lock().unwrap().push(stream, &text);
                continue;
            }
            line.push_str(&text);
            if let Some(end) = line.rfind('\n') {
                let complete: String = line.drain(..=end).collect();
                output.lock().unwrap().push(stream, &clean_traceback(&complete, &dir));
            }
        }
        line.push_str(&decoder.finish());
        output.lock().unwrap().push(stream, &clean_traceback(&line, &dir));
    }))
}

/// Байты → текст: русская буква может разорваться между двумя чтениями. `\r` выбрасываем —
/// на Windows Python пишет `\r\n`, странице нужен `\n`.
#[derive(Default)]
struct Utf8Decoder {
    rest: Vec<u8>,
}

impl Utf8Decoder {
    fn feed(&mut self, bytes: &[u8]) -> String {
        self.rest.extend_from_slice(bytes);
        let valid = match std::str::from_utf8(&self.rest) {
            Ok(_) => self.rest.len(),
            Err(e) if e.error_len().is_none() => e.valid_up_to(), // хвост — начало буквы, ждём остальное
            Err(_) => self.rest.len(),                            // мусор — пусть станет «�»
        };
        let text = String::from_utf8_lossy(&self.rest[..valid]).replace('\r', "");
        self.rest.drain(..valid);
        text
    }

    fn finish(&mut self) -> String {
        String::from_utf8_lossy(&std::mem::take(&mut self.rest)).replace('\r', "")
    }
}

fn clean_traceback(text: &str, dir: &Path) -> String {
    let dir = dir.to_string_lossy();
    text.replace(&format!("{dir}\\"), "").replace(&format!("{}/", dir.replace('\\', "/")), "")
}

// --- Job Object ------------------------------------------------------------------

/// Процесс ученика и все его потомки: убить разом и ограничить память.
struct JobObject(HANDLE);

// Описатель объекта ядра: им можно пользоваться из любого потока.
unsafe impl Send for JobObject {}

impl JobObject {
    /// `None` — Windows не дала создать (тогда останавливаем только сам процесс).
    fn new(child: &Child) -> Option<Self> {
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return None;
            }
            let job = JobObject(handle);
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_JOB_MEMORY;
            limits.JobMemoryLimit = MEMORY_LIMIT;
            let set = SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const _,
                std::mem::size_of_val(&limits) as u32,
            );
            if set == 0 || AssignProcessToJobObject(handle, child.as_raw_handle() as HANDLE) == 0 {
                debug_log!("python: Job Object не подключился");
                return None;
            }
            Some(job)
        }
    }

    fn terminate(&self) {
        unsafe { TerminateJobObject(self.0, 1) };
    }
}

impl Drop for JobObject {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) }; // KILL_ON_JOB_CLOSE: что осталось — закроется
    }
}

// --- где Python ------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct Python {
    exe: PathBuf,
    version: String,
}

/// Найденный Python запоминаем; не нашли — ищем снова в следующий раз (вдруг поставили во время урока).
static FOUND: Mutex<Option<Python>> = Mutex::new(None);

fn find() -> Option<Python> {
    if let Some(python) = FOUND.lock().unwrap().clone() {
        return Some(python);
    }
    let python = candidates().into_iter().find_map(|exe| {
        let version = version_of(&exe)?;
        Some(Python { exe, version })
    });
    debug_log!("python: {python:?}");
    if let Some(python) = &python {
        *FOUND.lock().unwrap() = Some(python.clone());
    }
    python
}

/// Где может быть Python, по порядку: лаунчер `py` → реестр (установщик python.org) → `PATH`.
fn candidates() -> Vec<PathBuf> {
    let mut list = Vec::new();
    if let Some(out) = run_quick(Command::new("py").args(["-3", "-c", "import sys; print(sys.executable)"])) {
        list.extend(out.lines().next().map(|l| PathBuf::from(l.trim())));
    }
    list.extend(registry_pythons());
    if let Some(path) = std::env::var_os("PATH") {
        list.extend(
            std::env::split_paths(&path)
                .filter(|dir| !is_store_stub_dir(dir))
                .map(|dir| dir.join("python.exe"))
                .filter(|exe| exe.is_file()),
        );
    }
    let mut unique = Vec::new();
    for exe in list {
        if !exe.as_os_str().is_empty() && !unique.contains(&exe) {
            unique.push(exe);
        }
    }
    unique
}

/// `%LOCALAPPDATA%\Microsoft\WindowsApps`: `python.exe` там — заглушка, которая открывает Microsoft Store.
fn is_store_stub_dir(dir: &Path) -> bool {
    dir.to_string_lossy().to_lowercase().trim_end_matches('\\').ends_with("\\microsoft\\windowsapps")
}

fn version_of(exe: &Path) -> Option<String> {
    let out = run_quick(Command::new(exe).args(["-c", "import sys; print(sys.version.split()[0])"]))?;
    let version = out.trim().to_string();
    version.starts_with('3').then_some(version)
}

/// Короткая проверка (до 5 с): вывод, если программа завершилась без ошибки.
fn run_quick(command: &mut Command) -> Option<String> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .ok()?;
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => break,
            Ok(None) if started.elapsed() < Duration::from_secs(5) => std::thread::sleep(Duration::from_millis(20)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
    let mut out = String::new();
    child.stdout.take()?.read_to_string(&mut out).ok()?;
    Some(out)
}

/// `Software\Python\PythonCore\<версия>\InstallPath` в HKCU и HKLM — сначала новые версии.
fn registry_pythons() -> Vec<PathBuf> {
    let mut found: Vec<(Vec<u32>, PathBuf)> = Vec::new();
    for root in [HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE] {
        for version in registry_subkeys(root, r"Software\Python\PythonCore") {
            let key = format!(r"Software\Python\PythonCore\{version}\InstallPath");
            let exe = registry_string(root, &key, Some("ExecutablePath"))
                .map(PathBuf::from)
                .or_else(|| registry_string(root, &key, None).map(|dir| PathBuf::from(dir).join("python.exe")));
            if let Some(exe) = exe.filter(|e| e.is_file()) {
                found.push((version_numbers(&version), exe));
            }
        }
    }
    found.sort_by(|a, b| b.0.cmp(&a.0));
    found.into_iter().map(|(_, exe)| exe).collect()
}

/// «3.12-32» → [3, 12, 32]: для сортировки версий.
fn version_numbers(version: &str) -> Vec<u32> {
    version.split(|c: char| !c.is_ascii_digit()).filter_map(|p| p.parse().ok()).collect()
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}

fn registry_subkeys(root: HKEY, path: &str) -> Vec<String> {
    let mut names = Vec::new();
    unsafe {
        let mut key: HKEY = std::ptr::null_mut();
        if RegOpenKeyExW(root, wide(path).as_ptr(), 0, KEY_READ, &mut key) != 0 {
            return names;
        }
        for index in 0.. {
            let mut name = [0u16; 256];
            let mut len = name.len() as u32;
            let status = RegEnumKeyExW(
                key,
                index,
                name.as_mut_ptr(),
                &mut len,
                std::ptr::null(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            );
            if status != 0 {
                break;
            }
            names.push(String::from_utf16_lossy(&name[..len as usize]));
        }
        RegCloseKey(key);
    }
    names
}

fn registry_string(root: HKEY, path: &str, value: Option<&str>) -> Option<String> {
    let path = wide(path);
    let value = value.map(wide);
    let value_ptr = value.as_ref().map_or(std::ptr::null(), |v| v.as_ptr());
    let mut data = [0u16; 1024];
    let mut size = (data.len() * 2) as u32;
    let status = unsafe {
        RegGetValueW(
            root,
            path.as_ptr(),
            value_ptr,
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            data.as_mut_ptr() as *mut _,
            &mut size,
        )
    };
    if status != 0 {
        return None;
    }
    let len = (size as usize / 2).saturating_sub(1); // без завершающего нуля
    let text = String::from_utf16_lossy(&data[..len]).trim().to_string();
    (!text.is_empty()).then_some(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decoder_keeps_split_letters() {
        let bytes = "Привет\r\n".as_bytes();
        let mut decoder = Utf8Decoder::default();
        let mut text = decoder.feed(&bytes[..3]); // «П» и половина «р»
        text += &decoder.feed(&bytes[3..]);
        text += &decoder.finish();
        assert_eq!(text, "Привет\n");
    }

    #[test]
    fn traceback_without_student_folder() {
        let dir = Path::new(r"C:\Users\ученик\AppData\Local\Тестирование\python\17");
        let err = "Traceback (most recent call last):\n  File \"C:\\Users\\ученик\\AppData\\Local\\Тестирование\\python\\17\\solution.py\", line 3, in <module>\n";
        assert_eq!(
            clean_traceback(err, dir),
            "Traceback (most recent call last):\n  File \"solution.py\", line 3, in <module>\n"
        );
    }

    #[test]
    fn output_limit() {
        let mut output = Output::default();
        output.push(Stream::Out, "a");
        output.push(Stream::Out, "b");
        output.push(Stream::Err, "c");
        assert_eq!(output.pending, vec![(Stream::Out, "ab".to_string()), (Stream::Err, "c".to_string())]);
        output.push(Stream::Out, &"ж".repeat(OUTPUT_LIMIT));
        assert_eq!(output.total, OUTPUT_LIMIT);
        output.push(Stream::Out, "лишнее");
        assert_eq!(output.total, OUTPUT_LIMIT);
    }

    #[test]
    fn file_names() {
        assert_eq!(safe_file_name("17.txt").as_deref(), Some("17.txt"));
        assert_eq!(safe_file_name(r"..\..\Windows\win.ini").as_deref(), Some("win.ini"));
        assert_eq!(safe_file_name("Данные: 26?.txt").as_deref(), Some("Данные_ 26_.txt"));
        assert_eq!(safe_file_name(".."), None);
        assert_eq!(safe_file_name("solution.py"), None);
        assert_eq!(safe_file_name(""), None);
    }

    #[test]
    fn store_stub_and_versions() {
        assert!(is_store_stub_dir(Path::new(r"C:\Users\u\AppData\Local\Microsoft\WindowsApps")));
        assert!(is_store_stub_dir(Path::new(r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\")));
        assert!(!is_store_stub_dir(Path::new(r"C:\Python312")));
        let mut versions = vec![version_numbers("3.9"), version_numbers("3.12-32"), version_numbers("3.12")];
        versions.sort();
        assert_eq!(versions, vec![vec![3, 9], vec![3, 12], vec![3, 12, 32]]);
    }

    /// Настоящий Python этого компьютера (если есть): находится, печатает, падает с ошибкой.
    #[test]
    fn real_python() {
        let Some(python) = find() else { return };
        assert!(python.version.starts_with('3'));
        let out = run_quick(Command::new(&python.exe).args(["-X", "utf8", "-c", "print('привет')"])).unwrap();
        assert_eq!(out.trim(), "привет");
        assert!(run_quick(Command::new(&python.exe).args(["-c", "1/0"])).is_none());
    }

    fn spawn_in_job(python: &Python, code: &str) -> (Child, JobObject) {
        let child = Command::new(&python.exe)
            .args(["-c", code])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .unwrap();
        let job = JobObject::new(&child).expect("Job Object");
        (child, job)
    }

    /// Больше 1 ГБ — `MemoryError` у ученика, а не зависший компьютер.
    #[test]
    fn memory_limit() {
        let Some(python) = find() else { return };
        let (child, _job) = spawn_in_job(&python, "x = bytearray(2 * 1024 ** 3)");
        let out = child.wait_with_output().unwrap();
        assert!(!out.status.success());
        assert!(String::from_utf8_lossy(&out.stderr).contains("MemoryError"));
    }

    /// «Остановить» убивает и процессы, которые запустила программа ученика: иначе вывод не закроется.
    #[test]
    fn terminate_kills_children() {
        let Some(python) = find() else { return };
        let code = "import subprocess, sys, time\n\
                    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n\
                    print('ok', flush=True)\n\
                    time.sleep(60)";
        let (mut child, job) = spawn_in_job(&python, code);
        let mut stdout = child.stdout.take().unwrap();
        let mut first = [0u8; 2];
        stdout.read_exact(&mut first).unwrap(); // потомок уже запущен
        let started = Instant::now();
        kill(&mut child, &Some(job));
        let mut rest = Vec::new();
        stdout.read_to_end(&mut rest).unwrap(); // закрылся — значит, и потомок убит
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    /// Система учителя на минуту: на любой запрос отдаёт файл задания «1 2 3».
    fn teacher_with_file() -> String {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        std::thread::spawn(move || {
            for mut stream in listener.incoming().flatten() {
                let mut request = [0u8; 1024];
                let _ = stream.read(&mut request);
                let _ = stream.write_all(b"HTTP/1.0 200 OK\r\nContent-Length: 5\r\n\r\n1 2 3");
            }
        });
        origin
    }

    /// Всё, как со страницы: hello → run → вывод → exit, ввод, остановка, время, файл задания, второй запуск.
    /// Один тест: запуск в программе один на всех, параллельные тесты мешали бы друг другу.
    #[test]
    fn page_conversation() {
        if find().is_none() {
            return;
        }
        let _ = std::fs::remove_dir_all(work_root());
        let origin = teacher_with_file();
        let calls: Arc<Mutex<Vec<String>>> = Arc::default();
        let page: Page = {
            let calls = calls.clone();
            Arc::new(move |call| calls.lock().unwrap().push(call))
        };
        let send = |message: String| handle(&message, &origin, &page);
        let run = |id: u64, code: &str, file: &str| {
            send(format!(r#"{{"type":"run","run":{id},"task":{id},"code":{},"file":{file}}}"#, json::quote(code)))
        };
        // все вызовы запуска id (вывод и exit) одной строкой, когда он закончился
        let wait = |id: u64, until: &str| -> String {
            let started = Instant::now();
            loop {
                let mine: Vec<String> =
                    calls.lock().unwrap().iter().filter(|c| c.contains(&format!("({id}, "))).cloned().collect();
                let text = mine.join("\n");
                if text.contains(until) {
                    return text;
                }
                assert!(started.elapsed() < Duration::from_secs(20), "запуск {id} не дождался «{until}»: {text}");
                std::thread::sleep(Duration::from_millis(20));
            }
        };

        send(r#"{"type":"hello"}"#.into());
        let started = Instant::now();
        while !calls.lock().unwrap().iter().any(|c| c.contains("ready({version: \"3.")) {
            assert!(started.elapsed() < Duration::from_secs(20), "нет ready: {:?}", calls.lock().unwrap());
            std::thread::sleep(Duration::from_millis(20));
        }

        run(1, "print('привет')\nprint(2 + 2)", "null");
        let text = wait(1, "exit(1, ");
        assert!(text.contains("привет") && text.contains("4"), "{text}");
        assert!(text.contains("code: 0") && text.contains("reason: 'done'"), "{text}");
        assert!(!text.contains("\\r"), "{text}");

        run(2, "x = 1\n1 / 0", "null");
        let text = wait(2, "exit(2, ");
        assert!(text.contains("'err'") && text.contains("ZeroDivisionError"), "{text}");
        assert!(text.contains("File \\\"solution.py\\\", line 2"), "{text}");
        assert!(text.contains("code: 1") && text.contains("reason: 'done'"), "{text}");

        run(3, "name = input('Имя: ')\nprint(name * 2)", "null");
        wait(3, "Имя: ");
        send(r#"{"type":"stdin","run":3,"text":"ab"}"#.into());
        let text = wait(3, "exit(3, ");
        assert!(text.contains("abab") && text.contains("code: 0"), "{text}");

        run(4, "while True: pass", "null");
        std::thread::sleep(Duration::from_millis(300));
        send(r#"{"type":"stop","run":4}"#.into());
        assert!(wait(4, "exit(4, ").contains("reason: 'stopped'"));

        run(5, "import time\ntime.sleep(10)", "null");
        assert!(wait(5, "exit(5, ").contains("reason: 'timeout'"));

        let file = r#"{"url":"/attachments/abc123.txt","name":"17.txt"}"#;
        run(6, "print(open('17.txt').read().split())", file);
        assert!(wait(6, "exit(6, ").contains("['1', '2', '3']"));
        // ученик испортил файл своей программой — к следующему запуску он снова как у учителя
        run(7, "open('17.txt', 'w').write('x')", file);
        wait(7, "exit(7, ");
        run(8, "print(open('17.txt').read())", file);
        assert!(wait(8, "exit(8, ").contains("1 2 3"));

        // страница перезагрузилась посреди запуска и запустила снова: прежний останавливается
        run(9, "while True: pass", "null");
        std::thread::sleep(Duration::from_millis(300));
        run(10, "print('второй')", "null");
        assert!(wait(9, "exit(9, ").contains("reason: 'stopped'"));
        assert!(wait(10, "exit(10, ").contains("второй"));
    }
}
