// Общие скрипты системы тестирования ЕГЭ

// Подтверждение удаления
function confirmDelete(message) {
    return confirm(message || 'Вы уверены, что хотите удалить этот элемент?');
}

// Копирование текста в буфер
function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(function() {
        alert('Скопировано: ' + text);
    }, function(err) {
        console.error('Ошибка копирования: ', err);
    });
}

// Форматирование времени
function formatTime(seconds) {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return mins.toString().padStart(2, '0') + ':' + secs.toString().padStart(2, '0');
}

// Автоскрытие алертов
document.addEventListener('DOMContentLoaded', function() {
    const alerts = document.querySelectorAll('.alert');
    alerts.forEach(function(alert) {
        setTimeout(function() {
            alert.style.opacity = '0';
            alert.style.transition = 'opacity 0.5s';
            setTimeout(function() {
                alert.remove();
            }, 500);
        }, 5000);
    });
});
