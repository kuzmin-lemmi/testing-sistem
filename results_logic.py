"""
Результаты тестирования: подсчёт баллов, показ ответов (любого вида) и выгрузка в Excel.

Все страницы берут данные отсюда, чтобы одинаково считать «верно из скольких» и одинаково
показывать ответы: одно поле, несколько полей, ответ из нескольких строк, загруженный файл.
"""
import io
import re
import zipfile
from xml.sax.saxutils import escape

from models import GradeCriteria, Student, TestSession, Variant


def plural(number, one, few, many):
    """plural(3, 'задание', 'задания', 'заданий') -> 'задания'."""
    number = abs(int(number or 0)) % 100
    if 11 <= number <= 19:
        return many
    last = number % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def format_away(student):
    """Уходы ученика со страницы теста: '—' если не уходил, иначе '3 раза · 2 мин 10 с'."""
    count = int(student.get('leave_count') or 0)
    if not count:
        return '—'
    seconds = int(student.get('leave_seconds') or 0)
    minutes, sec = divmod(seconds, 60)
    spent = f'{minutes} мин {sec} с' if minutes else f'{sec} с'
    return f'{count} {plural(count, "раз", "раза", "раз")} · {spent}'


def tasks_word(number):
    return f'{number} {plural(number, "задание", "задания", "заданий")}'


# ---------------------------------------------------------------------------
# Ответы

def _clean(value):
    if value is None:
        return ''
    return str(value).strip()


def is_text_task(task):
    return int(task.get('answer_count') or 0) == 0 and task.get('answer_kind') != 'file_upload'


def is_file_task(task):
    return task.get('answer_kind') == 'file_upload'


def expected_answers(task):
    """Правильный ответ задания списком строк (для «несколько строк» — один элемент с переводами строк)."""
    if is_file_task(task):
        return []
    count = int(task.get('answer_count') or 0)
    if count == 0:
        return [_clean(task.get('answer_text'))] if _clean(task.get('answer_text')) else []
    values = [_clean(task.get('answer_1'))]
    if count >= 2:
        values.append(_clean(task.get('answer_2')))
    if count > 2:
        extra = [line.strip() for line in str(task.get('answer_text') or '').splitlines()]
        values += [extra[i] if i < len(extra) else '' for i in range(count - 2)]
    return values


def given_answers(task, answer):
    """Ответ ученика тем же видом, что expected_answers. Пустой список — ученик не ответил."""
    if not answer:
        return []
    if is_file_task(task):
        return [answer['upload_name']] if answer.get('upload_name') else []
    count = int(task.get('answer_count') or 0)
    if count == 0:
        text = _clean(answer.get('answer_text'))
        return [text] if text else []
    values = [_clean(answer.get('answer_1'))]
    if count >= 2:
        values.append(_clean(answer.get('answer_2')))
    if count > 2:
        extra = [line.strip() for line in str(answer.get('answer_text') or '').splitlines()]
        values += [extra[i] if i < len(extra) else '' for i in range(count - 2)]
    return values if any(values) else []


def format_answer(values, task=None, empty='—'):
    """Список ответов -> строка для экрана: «1, 2», многострочный ответ через « / »."""
    if not values:
        return empty
    if task is not None and is_text_task(task):
        lines = [line.strip() for line in values[0].splitlines() if line.strip()]
        return ' / '.join(lines) if lines else empty
    return ', '.join(value if value else '—' for value in values)


def format_expected(task):
    if is_file_task(task):
        return 'проверяется вручную'
    return format_answer(expected_answers(task), task, empty='не задан')


def format_given(task, answer):
    return format_answer(given_answers(task, answer), task)


# ---------------------------------------------------------------------------
# Баллы

def session_criteria(test_session):
    if not test_session:
        return None
    return {key: test_session.get(key) for key in ('grade_5_min', 'grade_4_min', 'grade_3_min')}


def student_score(student, test_session=None, tasks=None):
    """Баллы ученика: только задания его варианта (ответы на другие задания не считаются)."""
    if test_session is None:
        test_session = TestSession.get_by_id(student['session_id'])
    if tasks is None:
        tasks = Variant.get_tasks(student['variant_id'])
    answers = {a['task_id']: a for a in Student.get_answers(student['id'])}
    cells = []
    for task in tasks:
        answer = answers.get(task['id'])
        given = given_answers(task, answer)
        cells.append({
            'task': task,
            'answer': answer,
            'given': format_answer(given, task),
            'answered': bool(given),
            'correct': bool(answer and answer.get('is_correct')),
            'marked': answer.get('is_correct') if answer else None,
        })
    correct = sum(1 for cell in cells if cell['correct'])
    total = len(tasks)
    return {
        'cells': cells,
        'correct': correct,
        'answered': sum(1 for cell in cells if cell['answered']),
        'total': total,
        'percent': round(100 * correct / total) if total else 0,
        'grade': GradeCriteria.calculate_grade(correct, total, session_criteria(test_session)) if total else None,
    }


def session_results(test_session):
    """Все ученики тестирования, сгруппированные по вариантам (у каждого варианта свои задания)."""
    students = sorted(TestSession.get_students(test_session['id']),
                      key=lambda s: ((s.get('last_name') or '').casefold(), (s.get('first_name') or '').casefold()))
    groups = {}
    for student in students:
        group = groups.get(student['variant_id'])
        if group is None:
            variant = Variant.get_by_id(student['variant_id']) or {'name': 'Удалён'}
            group = groups[student['variant_id']] = {
                'variant_id': student['variant_id'],
                'variant_name': variant['name'],
                'label': student.get('variant_label'),
                'tasks': Variant.get_tasks(student['variant_id']),
                'students': [],
            }
        score = student_score(student, test_session, group['tasks'])
        group['students'].append({'student': student, **score})
    result = list(groups.values())
    for group in result:
        group['solved'] = [sum(1 for row in group['students'] if row['cells'][i]['correct'])
                           for i in range(len(group['tasks']))]
    result.sort(key=lambda g: (g['label'] or '', g['variant_name']))
    scored = [row for group in result for row in group['students']]
    summary = {
        'students': len(scored),
        'average_percent': round(sum(r['percent'] for r in scored) / len(scored)) if scored else 0,
        'grades': {grade: sum(1 for r in scored if r['grade'] == grade) for grade in (5, 4, 3, 2)},
    }
    return result, summary


def grade_minimums(total, percents):
    """Пороги оценок в заданиях из процентов: (5, 4, 3). Не меньше 1 и по убыванию."""
    total = max(1, int(total))
    values = []
    for percent in percents:
        value = -(-total * int(percent) // 100)  # округление вверх
        values.append(min(total, max(1, value)))
    five, four, three = values
    four = min(four, five)
    three = min(three, four)
    return five, four, three


# ---------------------------------------------------------------------------
# Excel (.xlsx без внешних библиотек)

_DEFAULT, _HEADER, _RIGHT, _WRONG, _EMPTY, _TITLE, _BOLD, _PLAIN = range(8)

_STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="14"/><name val="Calibri"/></font></fonts>
<fills count="6"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFDCE6F5"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFC6EFCE"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFC7CE"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFEDEDED"/></patternFill></fill></fills>
<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFBFBFBF"/></left><right style="thin"><color rgb="FFBFBFBF"/></right>
<top style="thin"><color rgb="FFBFBFBF"/></top><bottom style="thin"><color rgb="FFBFBFBF"/></bottom><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="8">
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFill="1" applyBorder="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs></styleSheet>'''

_ILLEGAL_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]')


def _column(number):
    name = ''
    number += 1
    while number:
        number, rest = divmod(number - 1, 26)
        name = chr(65 + rest) + name
    return name


def _cell(row, column, value, style):
    """int — число; всё остальное — текст (ответ «012» остаётся «012»)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return f'<c r="{_column(column)}{row}" s="{style}"><v>{value}</v></c>'
    text = _ILLEGAL_XML.sub('', str(value))
    return (f'<c r="{_column(column)}{row}" s="{style}" t="inlineStr">'
            f'<is><t xml:space="preserve">{escape(text)}</t></is></c>')


def _sheet(rows, widths, frozen_rows=0, frozen_cols=0):
    body = []
    for number, cells in enumerate(rows, 1):
        parts = [_cell(number, col, cell[0], cell[1]) for col, cell in enumerate(cells) if cell is not None]
        body.append(f'<row r="{number}">{"".join(parts)}</row>')
    columns = ''.join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths, 1))
    pane = ''
    if frozen_rows or frozen_cols:
        attrs = (f' xSplit="{frozen_cols}"' if frozen_cols else '') + (f' ySplit="{frozen_rows}"' if frozen_rows else '')
        kind = 'bottomRight' if frozen_rows and frozen_cols else 'bottomLeft' if frozen_rows else 'topRight'
        pane = f'<pane{attrs} topLeftCell="{_column(frozen_cols)}{frozen_rows + 1}" activePane="{kind}" state="frozen"/>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetViews><sheetView workbookViewId="0">{pane}</sheetView></sheetViews>'
            f'<cols>{columns}</cols><sheetData>{"".join(body)}</sheetData></worksheet>')


def build_workbook(title, subtitle, groups):
    """Excel: лист «Результаты» (ученик × задание, цветом) и лист «Правильные ответы»."""
    width = max((len(g['tasks']) for g in groups), default=0)
    rows = [[(title, _TITLE)], [(subtitle, _PLAIN)], []]
    for group in groups:
        if len(groups) > 1:
            rows.append([(f'Вариант {group["label"] or ""}: {group["variant_name"]}'.replace(' :', ':'), _BOLD)])
        header = [('№', _HEADER), ('Ученик', _HEADER)] + [(n, _HEADER) for n in range(1, len(group['tasks']) + 1)]
        header += [('Верно', _HEADER), ('Всего', _HEADER), ('%', _HEADER), ('Оценка', _HEADER), ('Уходы', _HEADER)]
        rows.append(header)
        for number, row in enumerate(group['students'], 1):
            student = row['student']
            line = [(number, _EMPTY), (f'{student["last_name"]} {student["first_name"]}', _DEFAULT)]
            for cell in row['cells']:
                if cell['correct']:
                    line.append((cell['given'] if cell['answered'] else '✓', _RIGHT))
                elif cell['answered']:
                    line.append((cell['given'], _WRONG))
                else:
                    line.append(('—', _EMPTY))
            line += [(row['correct'], _BOLD), (row['total'], _BOLD), (row['percent'], _BOLD),
                     (row['grade'] if row['grade'] is not None else '', _BOLD),
                     (format_away(student), _DEFAULT)]
            rows.append(line)
        if group['students']:
            rows.append([None, ('Решили', _BOLD)] + [(n, _BOLD) for n in group['solved']])
        rows.append([])
    frozen = 4 if len(groups) == 1 else 0
    sheet1 = _sheet(rows, [5, 28] + [12] * width + [8, 8, 6, 8, 18], frozen_rows=frozen, frozen_cols=2 if frozen else 0)

    answers = [[('Вариант', _HEADER), ('№', _HEADER), ('Правильный ответ', _HEADER), ('Решили', _HEADER)]]
    for group in groups:
        for index, task in enumerate(group['tasks']):
            answers.append([(group['variant_name'], _DEFAULT), (index + 1, _EMPTY), (format_expected(task), _DEFAULT),
                            (f'{group["solved"][index]} из {len(group["students"])}', _EMPTY)])
    sheet2 = _sheet(answers, [28, 5, 50, 14], frozen_rows=1)

    names = ('Результаты', 'Правильные ответы')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in (1, 2))
                     + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                     '</Types>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                 '</Relationships>')
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                + ''.join(f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, 1))
                + '</sheets></workbook>')
    workbook_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in (1, 2))
                     + '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                     '</Relationships>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', content_types)
        archive.writestr('_rels/.rels', root_rels)
        archive.writestr('xl/workbook.xml', workbook)
        archive.writestr('xl/_rels/workbook.xml.rels', workbook_rels)
        archive.writestr('xl/styles.xml', _STYLES)
        archive.writestr('xl/worksheets/sheet1.xml', sheet1)
        archive.writestr('xl/worksheets/sheet2.xml', sheet2)
    return buffer.getvalue()
