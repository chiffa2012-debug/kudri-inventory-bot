"""Проверка разбора того, что мастер набирает пальцем.

Запуск: python test_parsing.py
Без фреймворков — одного файла достаточно, чтобы поймать обидное:
«1 500» с неразрывным пробелом или «450,50» с запятой не должны отвергаться.
"""

from bot.parsing import (
    format_amount,
    looks_like_invite_code,
    parse_number,
    parse_price,
    parse_quantity,
)


def test_plain_numbers():
    assert parse_number("3") == 3
    assert parse_number("1500") == 1500
    assert parse_number("0") == 0


def test_separators_and_spaces():
    assert parse_number("1 500") == 1500, "обычный пробел"
    assert parse_number("1 500") == 1500, "неразрывный пробел с телефона"
    assert parse_number("450,50") == 450.5, "запятая как разделитель"
    assert parse_number("450.50") == 450.5


def test_units_are_ignored():
    assert parse_number("1500 руб") == 1500
    assert parse_number("1500₽") == 1500
    assert parse_number("3 шт") == 3


def test_garbage_rejected():
    assert parse_number("много") is None
    assert parse_number("") is None
    assert parse_number("три штуки") is None
    assert parse_number("1500-2000") is None


def test_quantity_must_be_positive():
    assert parse_quantity("2") == 2
    assert parse_quantity("0.5") == 0.5, "полбанки краски — законное количество"
    assert parse_quantity("0") is None, "ноль штук привезти нельзя"
    assert parse_quantity("-1") is None


def test_price_allows_zero():
    assert parse_price("0") == 0, "проба или подарок"
    assert parse_price("450") == 450
    assert parse_price("-5") is None


def test_invite_code():
    assert looks_like_invite_code("123456")
    assert looks_like_invite_code("123 456"), "пробел при копировании кода"
    assert not looks_like_invite_code("12345")
    assert not looks_like_invite_code("1234567")
    assert not looks_like_invite_code("абвгде")


def test_format_amount():
    assert format_amount(13.0) == "13", "лишний ноль мешает читать"
    assert format_amount(0.5) == "0.5"
    assert format_amount(-228) == "-228"


if __name__ == "__main__":
    passed = 0
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
            passed += 1
            print(f"  ok  {name}")
    print(f"\nВсе {passed} проверок прошли.")
