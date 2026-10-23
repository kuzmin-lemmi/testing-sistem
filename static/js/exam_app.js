// Страница теста в программе ученика (student_app): масштаб задания, разделители колонок и вывода, клавиши.
// Стили — static/css/exam_app.css. Подключается только для программы ученика.
//
// Задание — картинка-скриншот. Чётче всего она пиксель в пиксель с экраном («1:1»): ширина картинки
// делится на devicePixelRatio (масштаб Windows 125 % и т. п.). «По ширине» — как 1:1, но не шире колонки.
(function () {
    'use strict';

    const STORE = 'exam_app_v1';
    const STEPS = [0.5, 0.67, 0.75, 0.85, 1, 1.15, 1.3, 1.5, 1.75, 2, 2.5, 3];

    function load() {
        try { return JSON.parse(localStorage.getItem(STORE) || '{}') || {}; } catch (_) { return {}; }
    }
    function save(patch) {
        try { localStorage.setItem(STORE, JSON.stringify(Object.assign(load(), patch))); } catch (_) { /* без памяти */ }
    }

    const state = Object.assign({ zoom: 'fit', split: null, outH: null }, load());
    const body = document.body;

    // --- масштаб задания ---------------------------------------------------------

    function natural(img) {
        return img.naturalWidth / (window.devicePixelRatio || 1);
    }

    function room(card) {
        const box = card.querySelector('.task-image');
        const style = getComputedStyle(box);
        return box.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    }

    function currentScale(card, img) {
        if (state.zoom !== 'fit') return state.zoom;
        return Math.min(1, room(card) / natural(img));
    }

    function applyZoom(card) {
        const img = card.querySelector('.task-image img');
        if (!img || !img.naturalWidth || card.style.display === 'none') return;
        const width = state.zoom === 'fit' ? Math.min(natural(img), room(card)) : natural(img) * state.zoom;
        img.style.width = Math.max(40, Math.round(width)) + 'px';
        const tools = card.querySelector('.task-tools');
        if (tools) {
            tools.querySelector('.zoom-value').textContent = Math.round(currentScale(card, img) * 100) + '%';
            tools.querySelector('[data-zoom="fit"]').classList.toggle('active', state.zoom === 'fit');
            tools.querySelector('[data-zoom="1"]').classList.toggle('active', state.zoom === 1);
        }
    }

    function applyAll() {
        document.querySelectorAll('.task-card').forEach(applyZoom);
    }

    function setZoom(card, value) {
        if (value === 'in' || value === 'out') {
            const img = card.querySelector('.task-image img');
            const now = currentScale(card, img);
            const next = value === 'in' ? STEPS.find(s => s > now + 0.01) : [...STEPS].reverse().find(s => s < now - 0.01);
            if (next === undefined) return;
            value = next;
        }
        state.zoom = value;
        save({ zoom: value });
        applyAll();
    }

    function addTools(card) {
        const text = card.querySelector('.task-text');
        if (!text) return;
        const tools = document.createElement('div');
        tools.className = 'task-tools';
        tools.innerHTML =
            '<button type="button" data-zoom="out" title="Мельче (Ctrl + колесо мыши)">−</button>' +
            '<span class="zoom-value"></span>' +
            '<button type="button" data-zoom="in" title="Крупнее (Ctrl + колесо мыши)">+</button>' +
            '<button type="button" data-zoom="fit" title="Не шире колонки">По ширине</button>' +
            '<button type="button" data-zoom="1" title="Пиксель в пиксель — самый чёткий текст">1:1</button>' +
            '<span class="spacer"></span>';
        const attachment = text.querySelector('.attachment');
        if (attachment) tools.appendChild(attachment);  // «Скачать файл данных» — в ту же строку
        text.prepend(tools);
        tools.addEventListener('click', event => {
            const button = event.target.closest('[data-zoom]');
            if (!button) return;
            const value = button.dataset.zoom;
            setZoom(card, value === 'fit' || value === 'in' || value === 'out' ? value : Number(value));
        });
        text.addEventListener('wheel', event => {
            if (!event.ctrlKey) return;
            event.preventDefault();
            setZoom(card, event.deltaY < 0 ? 'in' : 'out');
        }, { passive: false });

        const img = text.querySelector('.task-image img');
        if (img) {
            img.removeAttribute('onclick');  // увеличение по щелчку не нужно: есть масштаб
            img.onclick = null;
            if (img.complete) applyZoom(card); else img.addEventListener('load', () => applyZoom(card));
        }
        // колонку меняют разделитель и размер окна — картинка «по ширине» следует за ней
        new ResizeObserver(() => { if (state.zoom === 'fit') applyZoom(card); }).observe(text);
    }

    // --- разделители ---------------------------------------------------------------

    function drag(gutter, kind, onMove, onDone) {
        gutter.addEventListener('pointerdown', event => {
            event.preventDefault();
            gutter.setPointerCapture(event.pointerId);
            gutter.classList.add('dragging');
            body.classList.add('resizing', kind === 'col' ? 'resizing-col' : 'resizing-row');
            const move = e => onMove(e);
            const up = () => {
                gutter.removeEventListener('pointermove', move);
                gutter.removeEventListener('pointerup', up);
                gutter.removeEventListener('pointercancel', up);
                gutter.classList.remove('dragging');
                body.classList.remove('resizing', 'resizing-col', 'resizing-row');
                onDone();
            };
            gutter.addEventListener('pointermove', move);
            gutter.addEventListener('pointerup', up);
            gutter.addEventListener('pointercancel', up);
        });
    }

    function setSplit(percent) {
        state.split = Math.min(75, Math.max(25, percent));
        body.style.setProperty('--split', state.split + '%');
    }

    function setOutput(px) {
        state.outH = Math.max(40, Math.round(px));
        body.style.setProperty('--out-h', state.outH + 'px');
    }

    function addGutters(card) {
        if (!card.classList.contains('with-py')) return;
        const column = document.createElement('div');
        column.className = 'split-gutter';
        column.title = 'Перетащите, чтобы поменять ширину задания и редактора';
        card.querySelector('.py-task').before(column);
        drag(column, 'col', event => {
            const rect = card.getBoundingClientRect();
            setSplit((event.clientX - rect.left) / rect.width * 100);
        }, () => save({ split: state.split }));

        const output = card.querySelector('.py-task-output');
        if (!output) return;
        const row = document.createElement('div');
        row.className = 'output-gutter';
        row.title = 'Перетащите, чтобы поменять высоту вывода';
        output.before(row);
        drag(row, 'row', event => {
            const bodyRect = output.parentElement.getBoundingClientRect();
            const input = output.parentElement.querySelector('.py-task-input');
            const below = input && !input.hidden ? input.offsetHeight : 0;
            setOutput(Math.min(bodyRect.bottom - below - event.clientY, bodyRect.height - 120));
        }, () => save({ outH: state.outH }));
    }

    // --- клавиши: Ctrl+PageUp / Ctrl+PageDown — предыдущее и следующее задание ---------

    document.addEventListener('keydown', event => {
        if (!event.ctrlKey || event.altKey || event.shiftKey) return;
        if (event.key !== 'PageUp' && event.key !== 'PageDown') return;
        if (typeof goToTask !== 'function' || typeof currentTask === 'undefined') return;
        event.preventDefault();
        goToTask(currentTask + (event.key === 'PageDown' ? 1 : -1));
    });

    function start() {
        if (state.split) setSplit(state.split);
        if (state.outH) setOutput(state.outH);
        document.querySelectorAll('.task-card').forEach(card => { addTools(card); addGutters(card); });
        document.querySelectorAll('.nav-buttons .nav-btn').forEach(button => {
            if (button.textContent.includes('Назад')) button.title = 'Предыдущее задание (Ctrl + PageUp)';
            if (button.textContent.includes('Далее')) button.title = 'Следующее задание (Ctrl + PageDown)';
        });
        // переход к другому заданию: карточка стала видимой — посчитать масштаб её картинки
        new MutationObserver(applyAll).observe(document.querySelector('.content'), {
            subtree: true, attributes: true, attributeFilter: ['style'],
        });
        window.addEventListener('resize', applyAll);
    }

    // после defer-скриптов (py_editor.js создаёт редакторы) — разделитель вывода встаёт под готовый редактор
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
