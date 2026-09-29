// Ученик ничего не обновляет сам: страница следит за состоянием тестирования на сервере
// и переходит куда нужно, когда учитель запускает или завершает тестирование.
//   data-watch="none"   — тестирования нет; ждём запуска
//   data-watch="login"  — форма входа; обновляемся, если запустили другое тестирование или закрыли это
//   data-watch="result" — результат; уходим на вход, когда учитель запустил новое тестирование
(function () {
    'use strict';
    const mode = document.body.dataset.watch;
    if (!mode) return;
    const shown = Number(document.body.dataset.watchSession || 0);
    const delay = mode === 'none' ? 2000 : 3000;

    async function check() {
        try {
            const response = await fetch('/test/state', { cache: 'no-store' });
            if (response.ok) {
                const state = await response.json();
                const current = state.active ? Number(state.session_id) : 0;
                let change = false;
                if (mode === 'none') change = current !== 0;
                else if (mode === 'login') change = current !== shown;
                else if (mode === 'result') change = current !== 0 && current !== shown;
                if (change) {
                    if (mode === 'result') location.href = '/test'; else location.reload();
                    return;
                }
            }
        } catch (error) {
            // сервер учителя временно недоступен — просто пробуем снова
        }
        setTimeout(check, delay);
    }
    setTimeout(check, delay);
})();
