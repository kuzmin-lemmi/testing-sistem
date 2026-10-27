// LibreOffice для заданий с файлом — только в программе ученика (student_app/src/office.rs): кнопка
// «Открыть в LibreOffice Calc» над заданием и вкладки «Тест · Числа.xlsx» в шапке. Окна документов программа
// ставит под шапку теста, поэтому шапка с вкладками видна всегда; щелчок по шапке — впереди снова тест.
//
// Страница → программа: window.ipc.postMessage('office:' + JSON), где JSON —
//   {type: 'hello', top}                       — чем открывать? top — низ шапки в пикселях экрана
//   {type: 'layout', top}                      — шапка поменяла высоту (размер окна)
//   {type: 'open', task, file: {url, name}, top}
//   {type: 'show', id}                         — вкладка: id окна документа, 0 — тест
// Программа → страница: window.studentOffice.ready({calc, writer}) — названия программ, '' — нечем
//   | opened(task) | failed(task, message) | tabs([{id, name, kind: 'calc' | 'writer' | 'other', active}])
(function () {
    'use strict';

    if (!window.ipc || typeof window.ipc.postMessage !== 'function') return;
    const send = message => window.ipc.postMessage('office:' + JSON.stringify(message));
    const KIND = { xlsx: 'calc', xls: 'calc', xlsm: 'calc', ods: 'calc', csv: 'calc', txt: 'calc',
                   odt: 'writer', docx: 'writer', doc: 'writer', rtf: 'writer' };
    const ICON = { calc: '📊', writer: '📝', other: '📄' };
    const buttons = {};  // task -> {button, status, link}
    let tabsBox = null;

    // Работа в LibreOffice — не уход со страницы теста: это нужная для задания разрешённая программа.
    // awaySuppressed и awayTimer — со страницы теста (учёт уходов); чужую программу ловит сама программа ученика.
    function inOffice(on) {
        if (typeof awaySuppressed === 'undefined') return;
        awaySuppressed = on;
        if (on && typeof awayTimer !== 'undefined') clearTimeout(awayTimer);
    }
    window.addEventListener('focus', () => inOffice(false));

    function top() {
        const header = document.querySelector('.header');
        return header ? Math.round(header.getBoundingClientRect().bottom * (window.devicePixelRatio || 1)) : 0;
    }

    function setup() {
        document.querySelectorAll('.task-card').forEach(card => {
            const link = card.querySelector('.attachment');
            if (!link) return;
            const name = link.dataset.name || '';
            const kind = KIND[(name.split('.').pop() || '').toLowerCase()];
            if (!kind) return;
            const task = Number(card.dataset.taskId);
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'office-open';
            button.hidden = true;
            button.dataset.kind = kind;
            const status = document.createElement('span');
            status.className = 'office-status';
            link.before(button, status);
            buttons[task] = { button, status, link, name, label: '' };
            button.addEventListener('click', () => {
                button.disabled = true;
                button.textContent = 'Открывается…';
                status.textContent = '';
                inOffice(true);
                send({ type: 'open', task, file: { url: link.getAttribute('href'), name }, top: top() });
            });
        });

        tabsBox = document.createElement('div');
        tabsBox.className = 'office-tabs';
        tabsBox.hidden = true;
        const nav = document.querySelector('.header .task-nav');
        if (nav) nav.after(tabsBox); else document.querySelector('.header').append(tabsBox);
        tabsBox.addEventListener('click', event => {
            const tab = event.target.closest('[data-id]');
            if (!tab) return;
            if (tab.dataset.id !== '0') inOffice(true);
            send({ type: 'show', id: Number(tab.dataset.id) });
        });

        send({ type: 'hello', top: top() });
        window.addEventListener('resize', () => send({ type: 'layout', top: top() }));
    }

    function label(entry) {
        return `${ICON[entry.button.dataset.kind]} Открыть в ${entry.label}`;
    }

    window.studentOffice = {
        ready(apps) {
            Object.values(buttons).forEach(entry => {
                const app = (apps || {})[entry.button.dataset.kind];
                if (!app) return;  // нечем открыть — остаётся «Скачать файл данных»
                entry.label = app;
                entry.button.textContent = label(entry);
                entry.button.title = `Файл «${entry.name}» откроется поверх теста; вернуться — вкладка «Тест» вверху`;
                entry.button.hidden = false;
                entry.link.hidden = true;  // на столе теста скачанный файл открыть нечем
            });
        },
        opened(task) {
            const entry = buttons[task];
            if (!entry) return;
            entry.button.disabled = false;
            entry.button.textContent = label(entry);
        },
        failed(task, message) {
            inOffice(false);
            const entry = buttons[task];
            if (!entry) return;
            entry.button.disabled = false;
            entry.button.textContent = label(entry);
            entry.status.textContent = message;
        },
        tabs(list) {
            if (list.some(t => t.active)) inOffice(true);  // впереди документ (в том числе после Alt+Tab)
            if (!tabsBox) return;
            tabsBox.textContent = '';
            tabsBox.hidden = !list.length;
            if (!list.length) return;
            const make = (id, text, active, title) => {
                const tab = document.createElement('button');
                tab.type = 'button';
                tab.className = 'office-tab' + (active ? ' active' : '');
                tab.dataset.id = id;
                tab.textContent = text;
                tab.title = title;
                tabsBox.append(tab);
            };
            make(0, '📄 Тест', !list.some(t => t.active), 'Вернуться к заданиям');
            list.forEach(t => make(t.id, `${ICON[t.kind] || ICON.other} ${t.name}`, t.active, t.name));
        },
    };

    // после exam_app.js: он переносит «Скачать файл данных» в строку над заданием
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup); else setup();
})();
