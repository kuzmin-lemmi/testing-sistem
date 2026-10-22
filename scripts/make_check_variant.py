"""Пробный вариант «Проверка в школе» (5 заданий) для docs/ПЛАН_ПРОВЕРКИ_В_ШКОЛЕ.md. Ответы считаются кодом.

Готовый файл уже лежит в docs («Проверка в школе.variant»); скрипт нужен, только чтобы вариант изменить.
Нужны LocalShot (его variant.py пишет файл .variant) и Pillow — удобно запускать Python из окружения LocalShot:

    <папка LocalShot>\\.venv\\Scripts\\python.exe scripts\\make_check_variant.py "docs\\Проверка в школе.variant" <папка LocalShot>
"""
import io
import math
import random
import sys
import zipfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, sys.argv[2] if len(sys.argv) > 2 else r'C:\Users\admin\Desktop\LocalShot')
from variant import Task, Variant, png_bytes, MODE_MULTILINE

out = Path(sys.argv[1])
FONT = ImageFont.truetype(r'C:\Windows\Fonts\segoeui.ttf', 30)
BOLD = ImageFont.truetype(r'C:\Windows\Fonts\segoeuib.ttf', 32)

def card(number, lines):
    width, pad, step = 1100, 40, 46
    image = Image.new('RGB', (width, pad * 2 + step * (len(lines) + 1)), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((pad, pad), f'Задание {number}', font=BOLD, fill='#1e3a8a')
    for i, line in enumerate(lines, 1):
        draw.text((pad, pad + step * i), line, font=FONT, fill='black')
    return png_bytes(image)

def xlsx(numbers):
    """Минимальная книга Excel: числа в столбце A."""
    rows = ''.join(f'<row r="{i}"><c r="A{i}"><v>{n}</v></c></row>' for i, n in enumerate(numbers, 1))
    files = {
        '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Числа" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{rows}</sheetData></worksheet>',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()

random.seed(2026)
numbers = [random.randint(10, 99) for _ in range(30)]
answers = {
    1: str(2 ** 10),
    2: [str(math.gcd(12, 18)), str(12 * 18 // math.gcd(12, 18))],
    3: '\n'.join(str(d) for d in range(1, 7) if 6 % d == 0),
    4: str(sum(numbers)),
    5: str(bin(255).count('1')),
}
tasks = [
    Task(card(1, ['Сколько будет 2 в степени 10?']), [answers[1]]),
    Task(card(2, ['Найдите НОД и НОК чисел 12 и 18.', 'В первое поле — НОД, во второе — НОК.']), answers[2]),
    Task(card(3, ['Выпишите все делители числа 6 в порядке возрастания,', 'каждый на отдельной строке.']),
         [answers[3]], mode=MODE_MULTILINE),
    Task(card(4, ['В файле «Числа.xlsx» в столбце A записаны 30 чисел.', 'Найдите их сумму.',
                  'Файл скачайте под заданием и откройте в Excel или LibreOffice.']),
         [answers[4]], attachment_name='Числа.xlsx', attachment=xlsx(numbers)),
    Task(card(5, ['Сколько единиц в двоичной записи числа 255?']), [answers[5]]),
]
variant = Variant(tasks=tasks, catalog={'grade': '11', 'topic': 'Проверка в школе', 'minutes': 20})
variant.save(out)
check = Variant.load(out)
print('сохранён:', out.name, '— заданий', len(check))
for i, task in enumerate(check.tasks, 1):
    print(i, task.mode, repr(task.answers), task.attachment_name or '')
