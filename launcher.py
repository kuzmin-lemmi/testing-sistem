"""
Запуск системы тестирования без чёрного окна.

Запускать так:  pythonw launcher.py   (или из собранной программы — просто «Тестирование.exe»).
Что делает:
  * если система уже запущена — только открывает страницу учителя;
  * иначе запускает сервер, ставит значок у часов (если доступен) и открывает страницу учителя;
  * всё, что раньше печаталось в окно, пишется в файл Данные/logs/server.log;
  * при проблеме показывает понятное окно, а не пропадает молча.
"""
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

if getattr(sys, 'frozen', False) is False:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TITLE = 'Система тестирования'


def message(text, error=True, ask=False):
    """Окно сообщения Windows; работает без консоли. ask=True — вопрос «Да/Нет», возвращает True при «Да»."""
    try:
        import ctypes
        flags = 0x04 | 0x20 if ask else (0x10 if error else 0x40)  # YESNO|вопрос / ошибка / информация
        return ctypes.windll.user32.MessageBoxW(0, text, TITLE, flags | 0x40000) == 6
    except Exception:
        print(text)
        return False


def port_state(port):
    """'ours' — уже работает наша система, 'busy' — порт занят чем-то другим, 'free' — свободен."""
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            pass
    except OSError:
        return 'free'
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/test/alive', timeout=2) as answer:
            return 'ours' if answer.read().strip() == b'ok' else 'busy'
    except Exception:
        return 'busy'


def tray_image():
    from PIL import Image, ImageDraw
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, 62, 62), fill=(79, 70, 229, 255))
    draw.line((18, 34, 28, 44, 47, 22), fill=(255, 255, 255, 255), width=7, joint='curve')
    return image


def start_tray(url, port, has_active_test):
    """Значок у часов: «Открыть» и «Остановить». Если pystray не установлен — работаем без значка."""
    try:
        import pystray
    except Exception:
        return False

    def open_page(icon=None, item=None):
        webbrowser.open(url)

    def stop(icon, item):
        if has_active_test() and not message('Сейчас идёт тестирование! Если остановить систему, у учеников оборвётся тест.\n\nОстановить?', ask=True):
            return
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(pystray.MenuItem('Открыть систему', open_page, default=True),
                        pystray.MenuItem('Остановить систему', stop))
    icon = pystray.Icon('testing', tray_image(), TITLE, menu)
    icon.run_detached()
    return True


def main():
    try:
        import config
        state = port_state(config.PORT)
        url = f'http://127.0.0.1:{config.PORT}'
        if state == 'ours':
            if '--no-browser' not in sys.argv:
                webbrowser.open(url)
            return
        if state == 'busy':
            message(f'Порт {config.PORT} занят другой программой, система тестирования не может запуститься.\n'
                    'Закройте другие программы тестирования или перезагрузите компьютер.')
            return

        import activity
        activity.setup_logging()
        import server
        from werkzeug.serving import make_server
        server.prepare_startup()
        httpd = make_server(config.HOST, config.PORT, server.app, threaded=True)

        def has_active_test():
            try:
                return bool(server.TestSession.get_active())
            except Exception:
                return False

        start_tray(url, config.PORT, has_active_test)
        if '--no-browser' not in sys.argv:
            threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)), daemon=True).start()
        httpd.serve_forever()
    except ImportError as error:
        message(f'Не хватает компонентов программы ({error}).\nЗапустите файл УСТАНОВКА.bat — он всё установит.')
    except Exception as error:
        try:
            import logging
            logging.getLogger('testing').exception('Не удалось запустить систему')
        except Exception:
            pass
        message(f'Система не запустилась: {error}\n\nПодробности — в файле Данные\\logs\\server.log. '
                'Его можно отправить разработчику.')


if __name__ == '__main__':
    main()
