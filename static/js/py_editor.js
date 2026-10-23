// Редактор Python под заданием — только в программе ученика (student_app): код выполняется установленным
// у ученика Python, программа и страница обмениваются сообщениями.
//
// Страница → программа: window.ipc.postMessage('py:' + JSON), где JSON —
//   {type: 'hello'}                                  — какой Python установлен?
//   {type: 'run', run, task, code, file: {url, name} | null}
//   {type: 'stdin', run, text}                       — строка для input()
//   {type: 'stop', run}
// Программа → страница: window.studentPython.ready({version}) | missing() | output(run, 'out'|'err', text)
//   | exit(run, {code, seconds, reason}), где reason: 'done' | 'stopped' | 'timeout' | 'failed' (и message).
(function () {
    'use strict';

    const config = window.PY_TASK_CONFIG || { studentId: 0, files: {} };
    const STORE_KEY = `py_editor_v1:${config.studentId}`;  // тот же, что у боковой панели: код не теряется
    const OUTPUT_LIMIT = 200000;  // символов: огромный print в цикле не должен вешать страницу
    const SAVE_DELAY = 400;

    const send = window.ipc && typeof window.ipc.postMessage === 'function'
        ? message => window.ipc.postMessage('py:' + JSON.stringify(message))
        : null;

    let python = null;    // {version} — когда программа ответит; false — Python не найден
    let active = null;    // запущенная программа: {run, taskId}
    let runCounter = 0;
    const blocks = {};    // taskId -> состояние блока

    function loadStore() {
        try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {}; } catch (_) { return {}; }
    }

    function saveCode(taskId, code) {
        try {
            const store = loadStore();
            store[String(taskId)] = code;
            localStorage.setItem(STORE_KEY, JSON.stringify(store));
        } catch (_) { /* хранилище недоступно — код живёт до перезагрузки */ }
    }

    function paused() {
        return typeof isPaused !== 'undefined' && isPaused;  // isPaused — со страницы теста
    }

    function setStatus(block, text, kind) {
        block.status.textContent = text;
        block.status.className = 'py-task-status' + (kind ? ' ' + kind : '');
    }

    function setRunning(block, running) {
        block.runBtn.disabled = running;
        block.stopBtn.disabled = !running;
        block.inputForm.hidden = !running;
    }

    function appendOutput(block, text, cls) {
        if (block.outputSize >= OUTPUT_LIMIT) return;
        if (block.outputSize + text.length > OUTPUT_LIMIT) {
            text = text.slice(0, OUTPUT_LIMIT - block.outputSize);
            block.outputSize = OUTPUT_LIMIT;
            write(block, text, cls);
            write(block, '\n… вывод слишком большой, дальше не показан\n', 'note');
            return;
        }
        block.outputSize += text.length;
        write(block, text, cls);
    }

    function write(block, text, cls) {
        const out = block.output;
        const last = out.lastChild;
        // подряд идущий вывод одного вида — в один узел, иначе тысячи узлов
        if (last && (last.nodeType === Node.TEXT_NODE ? !cls : last.className === cls)) {
            if (last.nodeType === Node.TEXT_NODE) last.appendData(text); else last.textContent += text;
        } else if (cls) {
            const span = document.createElement('span');
            span.className = cls;
            span.textContent = text;
            out.appendChild(span);
        } else {
            out.appendChild(document.createTextNode(text));
        }
        out.scrollTop = out.scrollHeight;
    }

    function clearOutput(block) {
        block.output.textContent = '';
        block.outputSize = 0;
        block.stdout = '';
    }

    function seconds(value) {
        if (value < 0.05) return '< 0,1 с';
        return (Math.round(value * 10) / 10).toLocaleString('ru-RU') + ' с';
    }

    function run(taskId) {
        const block = blocks[taskId];
        if (!block) return;
        if (paused()) { setStatus(block, 'Пауза учителя — запуск недоступен', 'error'); return; }
        if (!send) { setStatus(block, 'Запуск работает только в программе ученика', 'error'); return; }
        if (python === false) { setStatus(block, 'Python не установлен на этом компьютере — обратитесь к учителю', 'error'); return; }
        if (active) {
            const where = active.taskId === taskId ? '' : ' (в другом задании)';
            setStatus(block, 'Программа уже выполняется' + where + ' — сначала остановите её', 'error');
            return;
        }
        const code = block.editor.getValue();
        if (!code.trim()) { setStatus(block, 'Напишите код, потом запускайте', 'error'); return; }
        saveCode(taskId, code);
        clearOutput(block);
        active = { run: ++runCounter, taskId };
        setRunning(block, true);
        setStatus(block, 'Выполняется…', 'running');
        send({ type: 'run', run: active.run, task: taskId, code, file: config.files[String(taskId)] || null });
    }

    function stop(taskId) {
        if (active && active.taskId === taskId) send({ type: 'stop', run: active.run });
    }

    function insertAnswer(taskId) {
        const block = blocks[taskId];
        if (paused()) return;
        const lines = block.stdout.split('\n').map(line => line.trim()).filter(Boolean);
        if (!lines.length) { setStatus(block, 'Нечего вставлять: программа ничего не вывела', 'error'); return; }
        const value = lines[lines.length - 1];
        const field = document.querySelector(`textarea[data-task-id="${taskId}"][data-answer="text"]`)
            || document.querySelector(`input[data-task-id="${taskId}"][data-answer="1"]`);
        if (!field) return;
        field.value = value;
        if (typeof scheduleSave === 'function') scheduleSave(taskId);  // со страницы теста
        setStatus(block, `В ответ вставлено: ${value}`);
    }

    function open(taskId, focus) {
        const block = blocks[taskId];
        block.body.hidden = false;
        block.toggle.setAttribute('aria-expanded', 'true');
        if (!block.editor) {
            let timer = null;
            block.editor = window.TestingEditor.create(block.editorBox, {
                doc: loadStore()[String(taskId)] || '',
                placeholder: 'Пиши код здесь. Запуск — F5 или Ctrl+Enter',
                onRun: () => run(taskId),
                onChange: code => {
                    clearTimeout(timer);
                    timer = setTimeout(() => saveCode(taskId, code), SAVE_DELAY);
                },
            });
            if (python) setStatus(block, `Python ${python.version}`);
        }
        if (focus) block.editor.focus();
    }

    function close(taskId) {
        const block = blocks[taskId];
        block.body.hidden = true;
        block.toggle.setAttribute('aria-expanded', 'false');
    }

    function setup(root) {
        const taskId = Number(root.dataset.taskId);
        const q = name => root.querySelector(`[data-py="${name}"]`);
        const block = {
            root, toggle: q('toggle'), body: root.querySelector('.py-task-body'), editorBox: q('editor'),
            output: q('output'), status: q('status'), runBtn: q('run'), stopBtn: q('stop'), inputForm: q('input'),
            editor: null, outputSize: 0, stdout: '',
        };
        blocks[taskId] = block;
        block.toggle.addEventListener('click', () => (block.body.hidden ? open(taskId, true) : close(taskId)));
        block.runBtn.addEventListener('click', () => run(taskId));
        block.stopBtn.addEventListener('click', () => stop(taskId));
        q('insert').addEventListener('click', () => insertAnswer(taskId));
        q('clear').addEventListener('click', () => {
            if (block.editor) { block.editor.setValue(''); saveCode(taskId, ''); block.editor.focus(); }
            if (!(active && active.taskId === taskId)) clearOutput(block);
        });
        block.inputForm.addEventListener('submit', event => {
            event.preventDefault();
            const field = block.inputForm.querySelector('input');
            if (!active || active.taskId !== taskId) return;
            appendOutput(block, field.value + '\n', 'in');  // как в IDLE: введённое видно в выводе
            send({ type: 'stdin', run: active.run, text: field.value });
            field.value = '';
        });
        // раскладка программы ученика (exam_app.css) — редактор всегда рядом с заданием; иначе — если код уже был
        if (document.body.classList.contains('app-layout') || (loadStore()[String(taskId)] || '').trim()) open(taskId, false);
    }

    window.studentPython = {
        ready(info) {
            python = info || { version: '' };
            Object.values(blocks).forEach(block => {
                if (block.editor && !active) setStatus(block, `Python ${python.version}`);
            });
        },
        missing() {
            python = false;
            Object.values(blocks).forEach(block => setStatus(block, 'Python не установлен на этом компьютере — обратитесь к учителю', 'error'));
        },
        output(runId, stream, text) {
            if (!active || active.run !== runId) return;
            const block = blocks[active.taskId];
            if (stream === 'out') block.stdout += text;
            appendOutput(block, text, stream === 'err' ? 'err' : '');
        },
        exit(runId, result) {
            if (!active || active.run !== runId) return;
            const block = blocks[active.taskId];
            active = null;
            setRunning(block, false);
            const r = result || {};
            if (r.reason === 'stopped') setStatus(block, 'Остановлено');
            else if (r.reason === 'timeout') setStatus(block, `Остановлено: программа работала дольше ${seconds(r.seconds || 0)}`, 'error');
            else if (r.reason === 'failed') setStatus(block, r.message || 'Не удалось запустить Python', 'error');
            else if (r.code) setStatus(block, `Завершено с ошибкой за ${seconds(r.seconds || 0)}`, 'error');
            else setStatus(block, `Готово за ${seconds(r.seconds || 0)}`);
        },
    };

    document.querySelectorAll('.py-task').forEach(setup);
    // последние нажатия (ещё в задержке сохранения) — не терять при перезагрузке и сдаче работы
    window.addEventListener('pagehide', () => {
        Object.entries(blocks).forEach(([taskId, block]) => { if (block.editor) saveCode(taskId, block.editor.getValue()); });
    });
    if (send) send({ type: 'hello' });
})();
