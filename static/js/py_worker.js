let pyodide = null;
let pyodideBaseUrl = null;

function post(type, payload = {}) {
  self.postMessage({ type, ...payload });
}

function formatSyntaxError(error) {
  const line = error && error.lineno ? error.lineno : '?';
  const msg = error && error.msg ? error.msg : 'Синтаксическая ошибка';
  const cls = (error && error.constructor && error.constructor.name) || 'SyntaxError';
  return `${cls}: ${msg} (строка ${line})`;
}

async function ensureRuntime(baseUrl) {
  if (pyodide) return;
  pyodideBaseUrl = baseUrl;
  importScripts(`${baseUrl}pyodide.js`);
  pyodide = await self.loadPyodide({ indexURL: baseUrl });
}

async function runCode({ code, attachment }) {
  if (!pyodide) {
    throw new Error('Pyodide runtime is not initialized');
  }

  pyodide.globals.set('__ege_code__', code);
  await pyodide.runPythonAsync(`
import ast
__ege_syntax_ok__ = True
__ege_syntax_error__ = ''
try:
    ast.parse(__ege_code__)
except SyntaxError as e:
    __ege_syntax_ok__ = False
    __ege_syntax_error__ = f"{e.__class__.__name__}: {e.msg} (строка {e.lineno if e.lineno is not None else '?'})"
`);

  const syntaxOk = Boolean(pyodide.globals.get('__ege_syntax_ok__'));
  const syntaxErr = String(pyodide.globals.get('__ege_syntax_error__') || '');
  if (!syntaxOk) {
    post('syntax_error', { summary: syntaxErr, details: syntaxErr });
    return;
  }

  if (attachment && attachment.name && attachment.buffer) {
    const data = new Uint8Array(attachment.buffer);
    pyodide.FS.writeFile(attachment.name, data);
  }

  pyodide.setStdout({
    batched: (text) => post('output_chunk', { channel: 'stdout', text: String(text || '') }),
  });
  pyodide.setStderr({
    batched: (text) => post('output_chunk', { channel: 'stderr', text: String(text || '') }),
  });

  try {
    await pyodide.runPythonAsync(code);
    post('run_complete');
  } catch (error) {
    const details = String(error || 'Ошибка выполнения');
    const summary = details.split('\n').filter(Boolean).slice(-1)[0] || details;
    post('runtime_error', { summary, details });
  }
}

self.onmessage = async (event) => {
  const msg = event.data || {};
  const type = msg.type;
  try {
    if (type === 'init') {
      await ensureRuntime(msg.baseUrl);
      post('init_ready', { baseUrl: pyodideBaseUrl });
      return;
    }

    if (type === 'run') {
      await runCode({ code: msg.code || '', attachment: msg.attachment || null });
      return;
    }

    if (type === 'ping') {
      post('pong');
    }
  } catch (error) {
    post('worker_error', { details: String(error || 'Ошибка worker') });
  }
};
