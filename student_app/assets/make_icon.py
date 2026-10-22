r"""Значок программы «Тестирование»: фиолетовый градиент, белый лист с заданиями, зелёная галочка.

    <LocalShot>\.venv\Scripts\python.exe assets\make_icon.py   (нужен Pillow) -> assets\icon.ico, assets\icon.png
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

S = 1024
here = Path(__file__).parent


def gradient(size, top, bottom):
    image = Image.new('RGB', (size, size))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        t = y / (size - 1)
        draw.line([(0, y), (size, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return image


def rounded_mask(size, radius):
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


icon = Image.new('RGBA', (S, S), (0, 0, 0, 0))
icon.paste(gradient(S, (99, 102, 241), (139, 92, 246)), (0, 0), rounded_mask(S, 220))

# тень листа
shadow = Image.new('RGBA', (S, S), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle([262, 182, 782, 862], radius=70, fill=(30, 20, 80, 110))
icon.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(28)))

draw = ImageDraw.Draw(icon)
draw.rounded_rectangle([242, 150, 762, 830], radius=70, fill=(255, 255, 255, 255))
# строки заданий: кружок-номер и строка
for i, y in enumerate((300, 450, 600)):
    draw.ellipse([320, y - 32, 384, y + 32], fill=(199, 210, 254, 255))
    draw.rounded_rectangle([420, y - 22, 680 - i * 70, y + 22], radius=22, fill=(203, 213, 225, 255))

# зелёная галочка в круге
cx, cy, r = 720, 760, 170
draw.ellipse([cx - r - 18, cy - r - 18, cx + r + 18, cy + r + 18], fill=(255, 255, 255, 255))
draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(16, 185, 129, 255))
draw.line([(cx - 85, cy + 5), (cx - 22, cy + 70), (cx + 95, cy - 65)], fill=(255, 255, 255, 255), width=58, joint='curve')

icon.save(here / 'icon.png')
icon.save(here / 'icon.ico', sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
print('icon.ico, icon.png')
