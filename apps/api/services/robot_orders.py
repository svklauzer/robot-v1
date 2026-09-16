"""Какие ордера на бирже — робота (#manual-orders-2026-09-16).

Владелец торгует на тех же биржах руками. Всё, что робот читает с биржи
(стопы, открытые ордера, сверка, гейт переключения биржи), обязано отличать
свои ордера от ручных: ручные робот не снимает, не закрывает и не считает
расхождением.

Признак — номер ордера клиента, который робот ставит сам:

  • OKX (буквы и цифры, до 32 символов): префикс `rbt`;
  • HTX-своп (только целое число, ccxt переводит его через float): 15 цифр,
    начинающихся с `770` — меньше 2^53, доходит до биржи без искажений.

Ордер из интерфейса биржи номера клиента не имеет или имеет свой — роботу он
чужой. Позиции номера не несут: принадлежность позиции робот знает только по
своему учёту (Position/Signal), а на бирже различает её по режиму маржи.
"""
from __future__ import annotations

import uuid

ALNUM_PREFIX = "rbt"
NUMERIC_PREFIX = "770"
NUMERIC_LENGTH = 15


def alnum_client_id(purpose: str) -> str:
    tag = "".join(ch for ch in str(purpose) if ch.isalnum())[:6] or "ord"
    return f"{ALNUM_PREFIX}{tag}{uuid.uuid4().hex}"[:32]


def numeric_client_id() -> str:
    tail_len = NUMERIC_LENGTH - len(NUMERIC_PREFIX)
    tail = uuid.uuid4().int % (10 ** tail_len)
    return f"{NUMERIC_PREFIX}{tail:0{tail_len}d}"


def is_robot_client_id(value) -> bool:
    text = str(value or "")
    if not text:
        return False
    if text.startswith(ALNUM_PREFIX):
        return True
    return text.isdigit() and len(text) == NUMERIC_LENGTH and text.startswith(NUMERIC_PREFIX)


def order_client_id(order: dict) -> str | None:
    """Номер клиента из ордера ccxt: унифицированное поле или сырое биржевое."""
    if not isinstance(order, dict):
        return None
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    for value in (
        order.get("clientOrderId"),
        info.get("clOrdId"), info.get("algoClOrdId"),
        info.get("client_order_id"), info.get("algo_client_order_id"),
        info.get("client-order-id"),
    ):
        if value not in (None, ""):
            return str(value)
    return None


def is_robot_order(order: dict) -> bool:
    return is_robot_client_id(order_client_id(order))
