"""
Каталог вариантов: Класс → Тема → Комплект.

Комплект — несколько вариантов одной работы (например, «Алфавитный подход, 20 минут» — варианты 1–4).
Класс, тему, комплект и время учитель задаёт в карточке варианта или перетаскиванием; в файле .variant
из LocalShot они приходят готовыми («паспорт»). Для вариантов без паспорта место угадывается по названию.
"""
import re

NO_GRADE = 'Без класса'
NO_TOPIC = 'Без темы'
MAX_FIELD = 80

_GRADE = re.compile(r'(?<!\d)(1[01]|[1-9])\s*(?:-?й\s*)?(?:класс|кл\b)', re.I)
_GRADE_CODE = re.compile(r'^(1[01]|[1-9])\s*[А-ЯЁA-Z]?(?:[_\s(]|$)', re.I)  # «10Б_29_09», «7б», «9 класс»
_MINUTES = re.compile(r'(\d{1,3})\s*мин', re.I)
_VARIANT_NO = re.compile(r'\s*[-–—,]?\s*(?:вариант|вар\.?)\s*№?\s*\d+\s*$', re.I)
_KNOWN_TOPICS = [
    (re.compile(r'систем\w*\s+счислени', re.I), 'Системы счисления'),
    (re.compile(r'алфавитн', re.I), 'Алфавитный подход'),
    (re.compile(r'кодирован', re.I), 'Кодирование информации'),
    (re.compile(r'логик|истинност', re.I), 'Логика'),
    (re.compile(r'(?<![а-яё])егэ(?![а-яё])', re.I), 'ЕГЭ'),
    (re.compile(r'(?<![а-яё])огэ(?![а-яё])', re.I), 'ОГЭ'),
]


def clean_field(value):
    value = re.sub(r'\s+', ' ', str(value or '')).strip()
    return value[:MAX_FIELD]


def clean_minutes(value):
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    return minutes if 1 <= minutes <= 300 else None


def grade_label(value):
    """«7» → «7 класс»; пусто → ''; остальное как есть (например, «ЕГЭ»)."""
    value = clean_field(value)
    if re.fullmatch(r'(1[01]|[1-9])', value):
        return f'{value} класс'
    match = re.fullmatch(r'(1[01]|[1-9])\s*(?:-?й\s*)?(?:класс|кл\.?)', value, re.I)
    return f'{match.group(1)} класс' if match else value


def kit_from_name(name):
    """«Алфавитный подход (7 класс, 20 минут) - вариант 3» → «Алфавитный подход (7 класс, 20 минут)».
    Если в названии нет «вариант N» — '' (вариант сам по себе, не комплект)."""
    name = clean_field(name)
    base = clean_field(_VARIANT_NO.sub('', name))
    return base if base and base != name else ''


def join_kits(items):
    """items: [{'name', 'kit'}]. Вариант без «вариант N», чьё название совпадает с комплектом других
    («Алфавитный подход» + «Алфавитный подход - вариант 2»), тоже входит в этот комплект."""
    kits = {item['kit'] for item in items if item.get('kit')}
    for item in items:
        if not item.get('kit') and clean_field(item.get('name')) in kits:
            item['kit'] = clean_field(item.get('name'))
    return items


def guess(name):
    """Место в каталоге по названию варианта (для старых вариантов и файлов без паспорта)."""
    name = clean_field(name)
    grade = ''
    match = _GRADE.search(name) or _GRADE_CODE.search(name)
    if match:
        grade = f'{match.group(1)} класс'
    topic = ''
    for pattern, title in _KNOWN_TOPICS:
        if pattern.search(name):
            topic = title
            break
    if topic == 'ЕГЭ' and not grade:
        grade = 'ЕГЭ'
    if topic == 'ОГЭ' and not grade:
        grade = '9 класс'
    if not topic:
        base = kit_from_name(name) or name
        base = re.split(r'\s+[-–—]\s+|\s*\(', base)[0].strip()
        if base and not _GRADE_CODE.match(base) and not _GRADE.search(base):
            topic = base
    minutes = _MINUTES.search(name)
    return {'grade': grade, 'topic': topic, 'kit': kit_from_name(name),
            'minutes': clean_minutes(minutes.group(1)) if minutes else None}


def from_manifest(raw, name):
    """«Паспорт» из manifest.json (поле catalog). Пустые поля дополняются догадкой по названию."""
    guessed = guess(name)
    if not isinstance(raw, dict):
        return guessed
    return {
        'grade': grade_label(raw.get('grade')) or guessed['grade'],
        'topic': clean_field(raw.get('topic')) or guessed['topic'],
        'kit': clean_field(raw.get('kit')) or guessed['kit'],
        'minutes': clean_minutes(raw.get('minutes')) or guessed['minutes'],
    }


_SHORT_NO = re.compile(r'(?:^|[\s·(—–-])(?:В|вариант|вар\.?)\s*№?\s*(\d+)\b', re.I)


def variant_no(name):
    """Номер варианта из названия: «… · В2», «… — вариант 3» → 2, 3; нет номера → None."""
    found = _SHORT_NO.findall(name or '')
    return int(found[-1]) if found else None


def kit_title(v):
    """Заголовок комплекта: название варианта без «· В2 (простой)» / «— вариант 2»; если не вышло — сам комплект."""
    name = v.get('name') or ''
    base = re.sub(r'\s*·\s*В\d+\b.*$', '', name)
    base = clean_field(_VARIANT_NO.sub('', base))
    return base if base and base != name else (v.get('kit') or name)


def build_works(variants):
    """Список «работ» для страницы вариантов: комплект (несколько вариантов) или одиночный вариант.
    Новые — сверху. [{key, title, grade, topic, kit, date, variants, minutes, tasks, runs, percent, launchable}]"""
    groups = {}
    for v in variants:
        kit = v.get('kit') or ''
        key = ('kit', v.get('grade') or '', v.get('topic') or '', kit) if kit else ('one', v.get('id'))
        groups.setdefault(key, []).append(v)
    works = []
    for key, items in groups.items():
        items.sort(key=lambda v: (variant_no(v.get('name')) or 999, (v.get('name') or '').casefold()))
        for i, v in enumerate(items, 1):
            v['short'] = f"В{variant_no(v.get('name')) or i}"
        first = items[0]
        stats = [v['stats'] for v in items if v.get('stats')]
        runs = sum(s['runs'] for s in stats)
        counts = {v.get('tasks_count') for v in items}
        is_kit = key[0] == 'kit'
        works.append({
            'key': f"kit-{first.get('id')}" if is_kit else f"one-{first.get('id')}",
            'title': (kit_title(first) if is_kit and len(items) > 1 else first.get('name')) or '',
            'is_kit': is_kit and len(items) > 1,
            'grade': first.get('grade') or '', 'topic': first.get('topic') or '', 'kit': first.get('kit') or '',
            'date': max(str(v.get('created_at') or '') for v in items),
            'variants': items,
            'minutes': next((v.get('minutes') for v in items if v.get('minutes')), None),
            'tasks': next(iter(counts)) if len(counts) == 1 else None,
            'used': sum(v.get('used_count') or 0 for v in items),
            'runs': runs,
            'percent': round(sum(s['percent'] * s['runs'] for s in stats) / runs) if runs else None,
            'launchable': is_kit and len(items) > 1 and len(counts) == 1,
        })
    for w in works:
        # в списке класс уже виден на значке, а тема — в начале названия: не повторяем
        w['list_title'] = _CLASS_PREFIX.sub('', w['title']) or w['title']
        w['topic_in_title'] = bool(w['topic']) and w['list_title'].startswith(w['topic'])
    works.sort(key=lambda w: w['date'], reverse=True)
    return works


_CLASS_PREFIX = re.compile(r'^\s*(?:1[01]|[1-9])\s*кл\.?\s*·\s*', re.I)


def grade_short(label):
    """«7 класс» → «7»; «ЕГЭ» → «ЕГЭ»; '' → '—'."""
    match = re.match(r'(\d+)', label or '')
    return match.group(1) if match else (label or '—')


def _grade_order(label):
    match = re.match(r'(\d+)', label or '')
    if match:
        return (0, int(match.group(1)), label)
    if not label:
        return (2, 0, '')
    return (1, 0, label.casefold())


def build_tree(variants):
    """[{grade, title, topics: [{topic, title, kits: [{kit, title, is_kit, variants, minutes, launchable}]}]}]"""
    tree = {}
    for v in variants:
        grade = v.get('grade') or ''
        topic = v.get('topic') or ''
        kit = v.get('kit') or ''
        key = kit or f'\x00{v.get("id")}'  # вариант без комплекта — отдельной строкой
        tree.setdefault(grade, {}).setdefault(topic, {}).setdefault(key, []).append(v)

    def title_of(groups, key):
        return key if not key.startswith('\x00') else (groups[key][0].get('name') or '')

    result = []
    for grade in sorted(tree, key=_grade_order):
        topics = []
        for topic in sorted(tree[grade], key=lambda t: (t == '', t.casefold())):
            groups = tree[grade][topic]
            kits = []
            for key in sorted(groups, key=lambda k: title_of(groups, k).casefold()):
                items = sorted(groups[key], key=lambda v: (len(v.get('name') or ''), (v.get('name') or '').casefold()))
                counts = {v.get('tasks_count') for v in items}
                minutes = next((v.get('minutes') for v in items if v.get('minutes')), None)
                is_kit = not key.startswith('\x00')
                kits.append({'kit': key if is_kit else '', 'title': title_of(groups, key), 'is_kit': is_kit,
                             'variants': items, 'minutes': minutes,
                             'launchable': is_kit and len(items) > 1 and len(counts) == 1})
            topics.append({'topic': topic, 'title': topic or NO_TOPIC, 'kits': kits,
                           'count': sum(len(k['variants']) for k in kits)})
        result.append({'grade': grade, 'title': grade or NO_GRADE, 'topics': topics,
                       'count': sum(t['count'] for t in topics)})
    return result
