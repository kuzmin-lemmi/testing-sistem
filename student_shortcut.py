"""
Ярлык для компьютеров учеников.

Ярлык — один маленький файл «Тестирование.html». Его один раз копируют на рабочий стол ученика.
Он лежит на компьютере ученика, поэтому открывается всегда, даже если система у учителя ещё не
запущена: показывает «Подождите» и сам переходит на страницу входа, как только компьютер учителя
отвечает. Так ученик не видит ошибку браузера.
"""
import socket


def local_addresses():
    """IPv4-адреса этого компьютера в локальной сети (без 127.*, 169.254.* и 0.*), самый вероятный первым."""
    found = []

    def add(address):
        if address and not address.startswith(('127.', '169.254.', '0.')) and address not in found:
            found.append(address)

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        probe.connect(('10.255.255.255', 1))  # пакеты не отправляются: так выбирается нужная сетевая карта
        add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return found


def student_urls(port):
    """Адреса компьютера учителя: сначала по имени (оно не меняется), потом по IP-адресам."""
    urls = []
    name = socket.gethostname().strip()
    if name:
        urls.append(f'http://{name}:{port}')
    urls.extend(f'http://{address}:{port}' for address in local_addresses())
    return urls or [f'http://127.0.0.1:{port}']
