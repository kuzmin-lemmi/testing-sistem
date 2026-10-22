// Собирает static/js/vendor/codemirror.min.js и кладёт рядом тексты лицензий всех вошедших пакетов (все MIT).
import { build } from 'esbuild';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const outfile = join(here, '..', '..', 'static', 'js', 'vendor', 'codemirror.min.js');

const result = await build({
  entryPoints: [join(here, 'editor.js')],
  bundle: true,
  minify: true,
  format: 'iife',
  target: 'chrome100',
  outfile,
  metafile: true,
  banner: { js: '/*! Редактор Python для системы тестирования: CodeMirror 6 и Lezer (MIT), лицензии — codemirror.LICENSE.txt */' },
});

const packages = new Set();
for (const input of Object.keys(result.metafile.inputs)) {
  const match = input.match(/node_modules\/((?:@[^/]+\/)?[^/]+)/);
  if (match) packages.add(match[1]);
}
const notices = [...packages].sort().map(name => {
  const dir = join(here, 'node_modules', name);
  const { version, license } = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'));
  const file = ['LICENSE', 'LICENSE.md', 'LICENSE.txt'].map(f => join(dir, f)).find(existsSync);
  return `${name}@${version} (${license})\n\n${file ? readFileSync(file, 'utf8').trim() : ''}\n`;
});
writeFileSync(outfile.replace(/\.min\.js$/, '.LICENSE.txt'),
  'Сторонние пакеты в codemirror.min.js\n\n' + notices.join('\n' + '-'.repeat(72) + '\n\n'));
console.log(`codemirror.min.js: ${packages.size} пакетов`);
