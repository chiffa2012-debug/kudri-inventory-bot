"""Разбор того, что мастер набирает пальцем в телефоне.

Никаких требований к формату мы не предъявляем: человек стоит у полки и пишет
как удобно — «1500», «1 500», «1500,50», «1500 руб». Всё это одно и то же
число, и просить «введите корректно» вместо того, чтобы понять, — верный
способ добиться того, что учёт вести перестанут.
"""

import re

# Пробелы любых видов, включая неразрывный: телефонная клавиатура вставляет
# именно его при автоформатировании.
_SPACES = re.compile(r"[\s  ]+")
_ALLOWED = re.compile(r"^-?\d+(?:\.\d+)?$")
_BARCODE = re.compile(r"\d{4,16}")


def parse_number(text: str) -> float | None:
    """Число из свободного текста. None, если это не число."""
    cleaned = _SPACES.sub("", str(text or ""))
    if not cleaned:
        return None

    # Хвосты вроде «руб», «₽», «шт» отбрасываем — смысла они не меняют.
    cleaned = re.sub(r"[a-zA-Zа-яА-ЯёЁ₽.]+$", "", cleaned)
    cleaned = cleaned.replace(",", ".")

    if not _ALLOWED.match(cleaned):
        return None

    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_quantity(text: str) -> float | None:
    """Количество: строго больше нуля, дробное разрешено (0.5 литра краски)."""
    value = parse_number(text)
    if value is None or value <= 0:
        return None
    return value


def parse_price(text: str) -> float | None:
    """Цена за единицу: ноль допустим — подарок или проба."""
    value = parse_number(text)
    if value is None or value < 0:
        return None
    return value


def looks_like_invite_code(text: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", _SPACES.sub("", str(text or ""))))


def parse_barcode_like(text: str) -> str | None:
    """Вырезаем наиболее вероятный штрихкод из распознанного текста.

    Формат не единый: бывают пробелы, знаки препинания и разные длины кода.
    Держим ограничение 4..16 цифр, чтобы не схватить случайные числа из цены.
    """

    cleaned = re.sub(r"\D", " ", str(text or ""))
    for token in re.split(r"\s+", cleaned.strip()):
        if not token:
            continue
        if _BARCODE.fullmatch(token):
            return token
    return None


def format_amount(value: float) -> str:
    """13.0 показываем как «13», 0.5 — как «0.5»: лишние нули мешают читать."""
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"
