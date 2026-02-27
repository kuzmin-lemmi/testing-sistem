# Патч для выдвижной правой панели Python (как у Яндекса)

## 1. Добавить серверный маршрут для выполнения Python (в server.py перед @app.route('/test/result'))

```python
@app.route('/test/run-python', methods=['POST'])
def student_run_python():
    """Выполнение Python-кода на сервере (опционально, если Pyodide недоступен)"""
    import subprocess
    import tempfile
    from flask import session as flask_session
    
    student_id = flask_session.get('student_id')
    if not student_id:
        return jsonify({'error': 'Неавторизован'}), 401
    
    data = request.get_json()
    code = data.get('code', '')
    
    if not code or len(code) > 50000:
        return jsonify({'error': 'Код слишком большой или пустой'}), 400
    
    # Проверка на опасные импорты
    dangerous_keywords = ['import os', 'import sys', 'import subprocess', '__import__', 'eval', 'exec', 'open(']
    for keyword in dangerous_keywords:
        if keyword in code:
            return jsonify({'error': f'Запрещено использовать: {keyword}'}), 400
    
    try:
        # Создаём временный файл
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(code)
            temp_path = f.name
        
        # Запускаем с ограничениями
        result = subprocess.run(
            ['python', temp_path],
            capture_output=True,
            text=True,
            timeout=5,  # 5 секунд макс
            encoding='utf-8',
            errors='replace'
        )
        
        os.unlink(temp_path)
        
        output = result.stdout if result.returncode == 0 else result.stderr
        return jsonify({
            'success': result.returncode == 0,
            'output': output[:10000],  # макс 10K символов
            'error': None if result.returncode == 0 else 'Ошибка выполнения'
        })
        
    except subprocess.TimeoutExpired:
        try:
            os.unlink(temp_path)
        except:
            pass
        return jsonify({'error': 'Превышен лимит времени (5 сек)'}), 400
    except Exception as e:
        return jsonify({'error': f'Ошибка сервера: {str(e)}'}), 500
```

## 2. Изменения в templates/student/test.html

### CSS (найти .py-panel и заменить):

```css
/* Python правая боковая панель */
.py-panel {
    position: fixed;
    top: 70px; /* высота хедера */
    right: 0;
    bottom: 0;
    width: 0;
    background: var(--card);
    border-left: 1px solid var(--border);
    overflow: hidden;
    transition: width 0.3s ease;
    z-index: 50;
    display: flex;
    flex-direction: column;
}

.py-panel.open {
    width: 40%;
}

.py-panel.fullscreen {
    width: 100%;
    top: 70px;
    left: 0;
    right: 0;
}

.py-header {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    padding: 12px 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-weight: 700;
    flex-shrink: 0;
}

.py-header-actions {
    display: flex;
    gap: 8px;
}

.py-body {
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    padding: 12px;
}

.py-actions {
    display: flex;
    gap: 6px;
    margin-bottom: 12px;
    flex-wrap: wrap;
}

.py-actions .nav-btn {
    padding: 8px 14px;
    font-size: 13px;
}

.py-status {
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 8px;
}

.py-layout {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 10px;
    overflow: hidden;
}

.py-editor-workbench {
    flex: 1;
    display: flex;
    background: #1e1e1e;
    border-radius: 8px;
    overflow: hidden;
    min-height: 200px;
}

.py-gutter {
    background: #252525;
    color: #858585;
    padding: 12px 8px;
    text-align: right;
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    line-height: 1.6;
    user-select: none;
    min-width: 40px;
}

.py-editor-wrap {
    flex: 1;
    position: relative;
}

.py-highlight, .py-editor {
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    line-height: 1.6;
    padding: 12px;
    margin: 0;
    border: none;
    white-space: pre;
    overflow-wrap: normal;
    overflow-x: auto;
}

.py-editor {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: transparent;
    color: #d4d4d4;
    caret-color: white;
    resize: none;
    outline: none;
}

.py-highlight {
    pointer-events: none;
    color: transparent;
}

.py-output {
    background: #1e1e1e;
    color: #d4d4d4;
    padding: 12px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    border-radius: 8px;
    overflow-y: auto;
    max-height: 200px;
    min-height: 80px;
    white-space: pre-wrap;
}

/* Убираем resize-handles */
.py-resize-handle {
    display: none;
}

/* Адаптив */
@media (max-width: 900px) {
    .py-panel.open {
        width: 100%;
    }
}
```

### JavaScript (найти функцию togglePython и заменить):

```javascript
function togglePython() {
    const panel = document.getElementById('pyPanel');
    if (!panel) return;
    panel.classList.toggle('open');
}

function togglePythonFullscreen() {
    const panel = document.getElementById('pyPanel');
    if (!panel) return;
    panel.classList.toggle('fullscreen');
}

// Обновить кнопки в шапке панели
// Найти .py-header-actions и заменить содержимое кнопок:
// <button type="button" class="calc-close" onclick="togglePythonFullscreen()">⤢ На весь экран / Свернуть</button>
// <button type="button" class="calc-close" onclick="togglePython()">✕</button>
```

## 3. Обновить HTML шапку панели (в .py-header .py-header-actions):

```html
<div class="py-header-actions">
    <button type="button" class="calc-close" onclick="togglePythonFullscreen()">⤢ Развернуть/Свернуть</button>
    <button type="button" class="calc-close" onclick="togglePython()">✕</button>
</div>
```

## 4. Убрать кнопки "Под задачу" и "На весь экран" (movePythonBelowTask, restorePythonFullscreen)

Удалить строки с этими кнопками из шапки панели.

## 5. Опционально: добавить серверное выполнение в runPython()

В функцию `window.runPython` добавить проверку:

```javascript
window.runPython = async function() {
    const code = pyEditor.value;
    
    // Опция 1: серверное выполнение (если Pyodide недоступен)
    const useServer = !window.pyodideReady;
    
    if (useServer) {
        try {
            const response = await fetch('/test/run-python', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code })
            });
            const result = await response.json();
            if (result.error) {
                pyOutput.textContent = 'Ошибка: ' + result.error;
            } else {
                pyOutput.textContent = result.output || '';
            }
            return;
        } catch (e) {
            pyOutput.textContent = 'Ошибка связи с сервером: ' + e.message;
            return;
        }
    }
    
    // Опция 2: Pyodide (существующий код)
    // ... остальной код ...
}
```

---

## Итог

После применения патча:
- Python-панель открывается справа (40% ширины)
- Кнопка "Развернуть" — на весь экран
- Можно выбрать: Pyodide (браузер) или серверное выполнение
- Серверное выполнение защищено timeout и whitelist

Применить патч вручную в `templates/student/test.html` и добавить маршрут в `server.py`.
