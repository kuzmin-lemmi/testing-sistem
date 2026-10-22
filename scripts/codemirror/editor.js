// Редактор Python для страницы теста. Собирается в static/js/vendor/codemirror.min.js (npm run build)
// и даёт странице одну функцию: window.TestingEditor.create(родитель, настройки).
import { EditorState, RangeSetBuilder } from '@codemirror/state';
import {
  EditorView, Decoration, ViewPlugin, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter,
  highlightSpecialChars, drawSelection, rectangularSelection, placeholder as placeholderText,
} from '@codemirror/view';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import {
  HighlightStyle, syntaxHighlighting, syntaxTree, indentOnInput, indentUnit, bracketMatching,
} from '@codemirror/language';
import { highlightSelectionMatches, search, searchKeymap } from '@codemirror/search';
import { closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete';
import { python } from '@codemirror/lang-python';
import { tags as t } from '@lezer/highlight';

// Цвета IDLE Classic — те же, что у прежней подсветки на странице теста
const idleColors = HighlightStyle.define([
  { tag: [t.keyword, t.controlKeyword, t.operatorKeyword, t.definitionKeyword, t.moduleKeyword, t.bool, t.null],
    color: '#ff7700', fontWeight: 'bold' },
  { tag: [t.string, t.special(t.string)], color: '#008000' },
  { tag: t.number, color: '#0000cc' },
  { tag: t.comment, color: '#dd0000', fontStyle: 'italic' },
  { tag: [t.function(t.definition(t.variableName)), t.definition(t.className)], color: '#0000ff' },
]);

// Встроенные функции IDLE красит фиолетовым; в грамматике они обычные имена, поэтому ищем их сами
const BUILTINS = new Set(('abs all any ascii bin bool bytearray bytes callable chr complex dict dir divmod enumerate '
  + 'eval exec filter float format frozenset getattr globals hasattr hash help hex id input int isinstance '
  + 'issubclass iter len list locals map max min next object oct open ord pow print range repr reversed round '
  + 'set setattr slice sorted str sum super tuple type vars zip').split(' '));
const builtinMark = Decoration.mark({ class: 'cm-py-builtin' });

const builtinHighlight = ViewPlugin.fromClass(class {
  constructor(view) { this.decorations = this.build(view); }
  update(update) {
    if (update.docChanged || update.viewportChanged || syntaxTree(update.startState) !== syntaxTree(update.state)) {
      this.decorations = this.build(update.view);
    }
  }
  build(view) {
    const builder = new RangeSetBuilder();
    for (const { from, to } of view.visibleRanges) {
      syntaxTree(view.state).iterate({
        from, to,
        enter: node => {
          if (node.name === 'VariableName' && BUILTINS.has(view.state.doc.sliceString(node.from, node.to))) {
            builder.add(node.from, node.to, builtinMark);
          }
        },
      });
    }
    return builder.finish();
  }
}, { decorations: plugin => plugin.decorations });

const theme = EditorView.theme({
  '&': { fontSize: '15px', backgroundColor: '#ffffff', color: '#111827', height: '100%' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { fontFamily: "'JetBrains Mono', Consolas, 'Courier New', monospace", lineHeight: '1.65' },
  '.cm-content': { padding: '12px 0', caretColor: '#111827' },
  '.cm-gutters': { backgroundColor: '#f3f4f6', color: '#6b7280', border: 'none' },
  '.cm-lineNumbers .cm-gutterElement': { padding: '0 8px 0 12px', minWidth: '28px' },
  '.cm-activeLine': { backgroundColor: '#f8fafc' },
  '.cm-activeLineGutter': { backgroundColor: '#e5e7eb', color: '#111827' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': { backgroundColor: '#c7d2fe' },
  '.cm-matchingBracket': { backgroundColor: '#dbeafe', outline: '1px solid #93c5fd' },
  '.cm-py-builtin': { color: '#900090' },
  '.cm-placeholder': { color: '#9ca3af' },
  '.cm-panels': { backgroundColor: '#f8fafc', color: '#111827' },
  '.cm-panels.cm-panels-bottom': { borderTop: '1px solid #d1d5db' },
  '.cm-searchMatch': { backgroundColor: '#fef08a' },
  '.cm-searchMatch.cm-searchMatch-selected': { backgroundColor: '#fde047' },
});

// Подписи панели поиска (Ctrl+F) по-русски
const russian = EditorState.phrases.of({
  'Find': 'Найти',
  'Replace': 'Заменить',
  'next': 'далее',
  'previous': 'назад',
  'all': 'все',
  'match case': 'учитывать регистр',
  'regexp': 'рег. выражение',
  'by word': 'слово целиком',
  'replace': 'заменить',
  'replace all': 'заменить все',
  'close': 'закрыть',
  'current match': 'текущее совпадение',
  'on line': 'на строке',
  'Go to line': 'Перейти к строке',
  'go': 'перейти',
});

/**
 * Создаёт редактор внутри parent.
 * options: doc — начальный код; placeholder — подсказка в пустом редакторе;
 *          onChange(code) — после каждой правки; onRun() — F5 или Ctrl+Enter (как «Run Module» в IDLE).
 * Возвращает { view, getValue(), setValue(code), focus(), destroy() }.
 */
function create(parent, options = {}) {
  const runKeys = options.onRun
    ? [{ key: 'F5', run: () => { options.onRun(); return true; } },
      { key: 'Mod-Enter', run: () => { options.onRun(); return true; } }]
    : [];
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: options.doc || '',
      extensions: [
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightSpecialChars(),
        history(),
        drawSelection(),
        EditorState.allowMultipleSelections.of(true),
        indentOnInput(),
        indentUnit.of('    '),
        EditorState.tabSize.of(4),
        bracketMatching(),
        closeBrackets(),
        rectangularSelection(),
        highlightActiveLine(),
        highlightSelectionMatches(),
        search({ top: false }),
        python(),
        syntaxHighlighting(idleColors),
        builtinHighlight,
        theme,
        russian,
        options.placeholder ? placeholderText(options.placeholder) : [],
        keymap.of([...runKeys, ...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap,
          indentWithTab]),
        options.onChange
          ? EditorView.updateListener.of(update => { if (update.docChanged) options.onChange(update.state.doc.toString()); })
          : [],
      ],
    }),
  });
  return {
    view,
    getValue: () => view.state.doc.toString(),
    setValue: code => view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: code || '' } }),
    focus: () => view.focus(),
    destroy: () => view.destroy(),
  };
}

window.TestingEditor = { create };
