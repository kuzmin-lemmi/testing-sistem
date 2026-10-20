"""Переименование вариантов по стандарту (октябрь 2026).

Стандарт:  «<N> кл · <Тема> · <Суть работы> · В<N>»   (В<N> — только если вариантов в комплекте несколько)
           Комплект: «<Тема> · <Суть работы> (<дд.мм>)»
Перед изменением делается резервная копия базы, а все замены пишутся в журнал CSV рядом с ней.

Запуск:  python scripts/rename_variants_2026_10.py [путь к database.db] [--apply]
Без --apply только показывает, что будет изменено.
"""
import csv
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

SPEC = {
    # id: (класс, тема, суть, номер варианта или None, комплект-суть или None)
    12: ('9 класс', 'ОГЭ', 'вариант 1', None, None),
    13: ('11 класс', 'ЕГЭ №17', '4 задачи', None, None),
    65: ('8 класс', 'Системы счисления', 'проверочная 2-8-16', None, None),
    66: ('11 класс', 'ЕГЭ сборный', '№1, 2, 4, 6 · 10 заданий', None, None),
    69: ('8 класс', 'Системы счисления', 'средний (20 мин)', None, None),
    75: ('11 класс', 'ЕГЭ №8', 'базовый', None, None),
    77: ('11 класс', 'ЕГЭ сборный', '№13, 14, 17, 23 · ДЗ 1', None, None),
    94: ('11 класс', 'ЕГЭ №19–21', 'теория игр (КЕГЭ)', None, None),
    95: ('11 класс', 'ЕГЭ №17', 'пары и тройки', None, None),
}
KITS = {
    # (ids по порядку вариантов): (класс, тема, суть, [подписи вариантов])
    (70, 71, 73, 74): ('7 класс', 'Алфавитный подход', 'проверочная 20 мин', None),
    (67, 72, 76): ('8 класс', 'Системы счисления', 'сложный', None),
    (78, 79, 80): ('8 класс', 'Системы счисления', 'сложение и умножение', [' (простой)', ' (средний)', ' (сложный)']),
    (81, 82): ('11 класс', 'ЕГЭ сборный', '№1, 2, 4, 6, 8 · 40 мин', None),
    (85, 84, 83): ('7 класс', 'Алфавитный подход', 'повышенный', None),
    (89, 88, 87, 86): ('8 класс', 'Системы счисления', 'переводы и арифметика', None),
    (92, 91, 90): ('7 класс', 'Графическая информация', 'объём изображения', None),
    (97, 98, 99, 96): ('11 класс', 'ЕГЭ сборный', '№1, 2, 4, 6, 8 · средний', None),
}
GROUP = re.compile(r'^\s*(1[01]|[5-9])\s*([А-ЯЁA-Z])?', re.I)


def short_grade(grade):
    m = re.match(r'(\d+)', grade or '')
    return f'{m.group(1)} кл' if m else (grade or 'без класса')


def local_day(created_at):
    dt = datetime.fromisoformat(str(created_at)).replace(tzinfo=timezone.utc).astimezone()
    return dt.strftime('%d.%m')


def plan(conn):
    rows = {r['id']: r for r in conn.execute('SELECT id, name, grade, topic, kit, created_at FROM variants')}
    out = {}
    for vid, (grade, topic, what, _, _) in SPEC.items():
        if vid in rows:
            out[vid] = (f'{short_grade(grade)} · {topic} · {what}', grade, topic, '')
    for ids, (grade, topic, what, labels) in KITS.items():
        ids = [i for i in ids if i in rows]
        if not ids:
            continue
        kit = f'{topic} · {what} ({local_day(rows[ids[0]]["created_at"])})'
        for n, vid in enumerate(ids, 1):
            label = labels[n - 1] if labels else ''
            out[vid] = (f'{short_grade(grade)} · {topic} · {what} · В{n}{label}', grade, topic, kit)
    # уроки (февраль–май, «7Г», «10Б (2)», «9класс»…): «7 кл · 7Г · урок 06.02»
    for vid, r in rows.items():
        if vid in out:
            continue
        m = GROUP.match(r['name'] or '')
        if not m:
            print('  без правила, оставлено как есть:', vid, r['name'])
            continue
        grade = r['grade'] or f'{m.group(1)} класс'
        group = f' · {m.group(1)}{m.group(2).upper()}' if m.group(2) and not re.match(r'\s*\d+\s*кл', r['name'], re.I) else ''
        out[vid] = (f'{short_grade(grade)}{group} · урок {local_day(r["created_at"])}', grade, r['topic'] or '', r['kit'] or '')
    # одинаковые названия (два урока в один день) — добавляем (2), (3)
    seen = {}
    for vid in sorted(out):
        name = out[vid][0]
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            out[vid] = (f'{name} ({seen[name]})',) + out[vid][1:]
    return rows, out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    db = args[0] if args else os.path.join(os.path.dirname(__file__), '..', 'data', 'database.db')
    apply = '--apply' in sys.argv
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows, out = plan(conn)
    changes = [(vid, rows[vid], new) for vid, new in sorted(out.items())
               if (rows[vid]['name'], rows[vid]['grade'] or '', rows[vid]['topic'] or '', rows[vid]['kit'] or '') != new]
    for vid, old, new in changes:
        print(f'{vid:>4}  {old["name"]!s:<58} → {new[0]}   [{new[1]} | {new[2] or "—"} | {new[3] or "—"}]')
    print(f'Изменений: {len(changes)}')
    if not apply:
        print('Пробный запуск. Для записи добавьте --apply')
        return
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(db)), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(backup_dir, f'before-rename-{stamp}.db')
    dst = sqlite3.connect(backup)
    conn.backup(dst)
    dst.close()
    with open(os.path.join(backup_dir, f'rename-{stamp}.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f, delimiter=';')
        w.writerow(['id', 'было', 'стало', 'класс было', 'класс', 'тема было', 'тема', 'комплект было', 'комплект'])
        for vid, old, new in changes:
            w.writerow([vid, old['name'], new[0], old['grade'], new[1], old['topic'], new[2], old['kit'], new[3]])
    with conn:
        for vid, _, (name, grade, topic, kit) in changes:
            conn.execute('UPDATE variants SET name = ?, grade = ?, topic = ?, kit = ? WHERE id = ?',
                         (name, grade or None, topic or None, kit or None, vid))
    print('Готово. Резервная копия:', backup)


if __name__ == '__main__':
    main()
