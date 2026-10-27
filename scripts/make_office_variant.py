"""Пробный вариант «Проверка LibreOffice» (4 задания с файлами) — открыть файл задания в LibreOffice Calc
из программы ученика, посчитать формулой, вписать ответ. Ответы считаются кодом.

Готовый файл уже лежит в docs («Проверка LibreOffice.variant»); скрипт нужен, только чтобы вариант изменить.
Нужны LocalShot (его variant.py пишет файл .variant), Pillow и LibreOffice (из .xlsx делает .ods и проверяет,
что сам LibreOffice читает каждый файл и получает те же ответы):

    <папка LocalShot>\\.venv\\Scripts\\python.exe scripts\\make_office_variant.py "docs\\Проверка LibreOffice.variant" <папка LocalShot>
"""
import csv
import io
import random
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, sys.argv[2] if len(sys.argv) > 2 else r'C:\Users\admin\Desktop\LocalShot')
from variant import Task, Variant, png_bytes

out = Path(sys.argv[1])
SOFFICE = Path(r'C:\Program Files\LibreOffice\program\soffice.exe')
FONT = ImageFont.truetype(r'C:\Windows\Fonts\segoeui.ttf', 30)
BOLD = ImageFont.truetype(r'C:\Windows\Fonts\segoeuib.ttf', 32)
OPEN = 'Откройте файл кнопкой «Открыть в LibreOffice Calc» над заданием.'


def card(number, lines):
    width, pad, step = 1100, 40, 46
    image = Image.new('RGB', (width, pad * 2 + step * (len(lines) + 1)), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((pad, pad), f'Задание {number}', font=BOLD, fill='#1e3a8a')
    for i, line in enumerate(lines, 1):
        draw.text((pad, pad + step * i), line, font=FONT, fill='black')
    return png_bytes(image)


def column(index):
    return 'ABCDEFGHIJ'[index]


def xlsx(header, rows, sheet='Лист1'):
    """Минимальная книга Excel: строка заголовков и строки чисел."""
    def cell(ref, value):
        if isinstance(value, str):
            return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'
        return f'<c r="{ref}"><v>{value}</v></c>'
    lines = [header] + rows
    xml_rows = ''.join(
        f'<row r="{r}">' + ''.join(cell(f'{column(c)}{r}', v) for c, v in enumerate(line)) + '</row>'
        for r, line in enumerate(lines, 1))
    files = {
        '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{xml_rows}</sheetData></worksheet>',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def libre_office(work, args):
    """LibreOffice без окна, в своём профиле (не мешает открытому LibreOffice)."""
    profile = (work / 'profile').as_uri()
    subprocess.run([str(SOFFICE), f'-env:UserInstallation={profile}', '--headless', '--norestore', *args],
                   check=True, cwd=work, timeout=180, capture_output=True)


def to_ods(work, name, data):
    (work / name).write_bytes(data)
    libre_office(work, ['--convert-to', 'ods', '--outdir', str(work / 'ods'), name])
    return (work / 'ods' / (Path(name).stem + '.ods')).read_bytes()


def read_back(work, name, data, options=None):
    """Что увидит LibreOffice: файл → CSV через сам LibreOffice → строки."""
    source = work / 'check' / name
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(data)
    target = 'csv:Text - txt - csv (StarCalc):44,34,76' + (f',{options}' if options else '')
    # .txt LibreOffice сам открыл бы в Writer — как и программа ученика, просим Calc
    calc = ['--infilter=Text - txt - csv (StarCalc)'] if name.endswith('.txt') else []
    libre_office(work, [*calc, '--convert-to', target, '--outdir', str(work / 'back'), str(source)])
    text = (work / 'back' / (source.stem + '.csv')).read_text(encoding='utf-8-sig')
    return list(csv.reader(io.StringIO(text)))


random.seed(2027)
numbers = [random.randint(1, 999) for _ in range(40)]
temps = [(random.randint(-15, 5), random.randint(-5, 25)) for _ in range(30)]
goods = ['Тетрадь', 'Ручка', 'Карандаш', 'Линейка', 'Ластик', 'Маркер', 'Пенал', 'Альбом', 'Краски', 'Клей']
shop = [(name, random.randint(15, 300), random.randint(1, 12)) for name in goods]
lines = [random.randint(100, 9999) for _ in range(50)]

answers = {
    1: str(sum(n > 500 for n in numbers)),
    2: [str(min(a for a, _ in temps)), str(max(b for _, b in temps))],
    3: str(sum(price * count for _, price, count in shop)),
    4: str(sum(n % 2 == 0 for n in lines)),
}

with tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp)
    numbers_xlsx = xlsx(['Число'], [[n] for n in numbers], 'Числа')
    temps_ods = to_ods(work, 'Температура.xlsx', xlsx(['Утро', 'День'], [list(t) for t in temps], 'Температура'))
    # разделитель — запятая: её LibreOffice в окне «Импорт текста» выбирает сам, ученику ничего не менять
    shop_csv = ('Товар,Цена,Количество\r\n' + ''.join(f'{n},{p},{c}\r\n' for n, p, c in shop)).encode('utf-8-sig')
    lines_txt = ''.join(f'{n}\r\n' for n in lines).encode('ascii')

    # проверка: сам LibreOffice читает файлы и получает те же ответы
    seen = read_back(work, 'Числа.xlsx', numbers_xlsx)
    assert str(sum(int(r[0]) > 500 for r in seen[1:])) == answers[1], 'Числа.xlsx'
    seen = read_back(work, 'Температура.ods', temps_ods)
    assert [str(min(int(r[0]) for r in seen[1:])), str(max(int(r[1]) for r in seen[1:]))] == answers[2], 'ods'
    seen = read_back(work, 'Покупки.csv', shop_csv)
    assert str(sum(int(r[1]) * int(r[2]) for r in seen[1:])) == answers[3], 'Покупки.csv'
    assert seen[1][0] == 'Тетрадь', f'кодировка .csv: {seen[1][0]!r}'
    seen = read_back(work, 'Числа.txt', lines_txt)
    assert str(sum(int(r[0]) % 2 == 0 for r in seen)) == answers[4], 'Числа.txt'

tasks = [
    Task(card(1, ['В файле «Числа.xlsx» в столбце A записаны 40 чисел.',
                  'Сколько из них больше 500?', OPEN]),
         [answers[1]], attachment_name='Числа.xlsx', attachment=numbers_xlsx),
    Task(card(2, ['В файле «Температура.ods» — температура за 30 дней:',
                  'в столбце A утром, в столбце B днём.',
                  'В первое поле — самую низкую утреннюю температуру,',
                  'во второе — самую высокую дневную.', OPEN]),
         answers[2], attachment_name='Температура.ods', attachment=temps_ods),
    Task(card(3, ['В файле «Покупки.csv» — товары: цена и количество.',
                  'Сколько стоят все покупки вместе (цена × количество)?',
                  'При открытии появится окно «Импорт текста» — нажмите «ОК».', OPEN]),
         [answers[3]], attachment_name='Покупки.csv', attachment=shop_csv),
    Task(card(4, ['В файле «Числа.txt» — 50 чисел, по одному в строке.',
                  'Сколько из них чётных?',
                  'При открытии появится окно «Импорт текста» — нажмите «ОК».', OPEN]),
         [answers[4]], attachment_name='Числа.txt', attachment=lines_txt),
]
variant = Variant(tasks=tasks, catalog={'grade': '11', 'topic': 'Проверка LibreOffice', 'minutes': 20})
variant.save(out)
check = Variant.load(out)
print('сохранён:', out.name, '— заданий', len(check))
for i, task in enumerate(check.tasks, 1):
    print(i, repr(task.answers), task.attachment_name or '', len(task.attachment or b''), 'байт')
