"""Локальная книга OKX по каналу `books` (#okx-depth-2026-09-12).

07.09 фид стакана перевели на OKX `books5` — ровно пять уровней — и сломали
живые метрики, которые книгу читают: `depth_thinness` (доля первых пяти во всей
книге) стала тождественно 1.0, `wall_share` и `obi` по пяти уровням поехали.
Фид закрепили на HTX (`OB_EXCHANGE=htx`), а торговля при этом идёт на OKX.

Канал `books` отдаёт до 400 уровней: первое сообщение — снимок, дальше —
изменения. Книгу приходится вести самим:

  * `action=snapshot` — книга заменяется целиком;
  * `action=update` — уровни меняются, размер "0" удаляет уровень;
  * `prevSeqId` обязан совпасть с последним `seqId`, иначе пропущено сообщение
    и книга неверна — пересборка;
  * `checksum` — CRC32 (со знаком, 32 бита) строки из 25 лучших уровней с каждой
    стороны, чередованием «цена:размер» бид, аск, бид, аск…; если с одной
    стороны уровней меньше, остаток другой дописывается подряд. Цены и размеры
    берутся строками РОВНО как пришли — "0.10" и "0.1" дают разные суммы.
    Несовпадение — пересборка.

Наружу книга отдаётся обрезанной до той же глубины, что у HTX `step0`
(`OB_BOOK_LEVELS`, 150), — чтобы метрики «доля первых N во всей книге»
считались по сопоставимой книге.
"""
from __future__ import annotations

import zlib
from decimal import Decimal, InvalidOperation

_CHECKSUM_DEPTH = 25


def _dec(value: str) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def okx_checksum(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> int:
    """CRC32 по правилу OKX: bids по убыванию, asks по возрастанию, по 25."""
    parts: list[str] = []
    top_b, top_a = bids[:_CHECKSUM_DEPTH], asks[:_CHECKSUM_DEPTH]
    for i in range(max(len(top_b), len(top_a))):
        if i < len(top_b):
            parts.append(f"{top_b[i][0]}:{top_b[i][1]}")
        if i < len(top_a):
            parts.append(f"{top_a[i][0]}:{top_a[i][1]}")
    crc = zlib.crc32(":".join(parts).encode()) & 0xFFFFFFFF
    return crc - (1 << 32) if crc >= (1 << 31) else crc


class OkxLocalBook:
    """Книга одного инструмента. `apply()` → False значит «пересобрать»."""

    def __init__(self) -> None:
        self._bids: dict[Decimal, tuple[str, str]] = {}
        self._asks: dict[Decimal, tuple[str, str]] = {}
        self.seq: int | None = None
        self.ready = False

    def reset(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self.seq = None
        self.ready = False

    @staticmethod
    def _load(side: dict, levels: list) -> None:
        for level in levels or []:
            try:
                price, size = str(level[0]), str(level[1])
            except (IndexError, TypeError):
                continue
            key = _dec(price)
            if key is None:
                continue
            if _dec(size) == 0:
                side.pop(key, None)
            else:
                side[key] = (price, size)

    def sorted_bids(self) -> list[tuple[str, str]]:
        return [self._bids[k] for k in sorted(self._bids, reverse=True)]

    def sorted_asks(self) -> list[tuple[str, str]]:
        return [self._asks[k] for k in sorted(self._asks)]

    def apply(self, action: str, data: dict) -> bool:
        """Применяет сообщение канала `books`. False — книга неверна."""
        if action == "snapshot":
            self.reset()
        elif not self.ready:
            # Изменение до снимка: не к чему применять.
            return False
        else:
            prev = data.get("prevSeqId")
            if prev is not None and int(prev) != -1 and self.seq is not None \
                    and int(prev) != int(self.seq):
                return False

        self._load(self._bids, data.get("bids"))
        self._load(self._asks, data.get("asks"))
        if data.get("seqId") is not None:
            self.seq = int(data["seqId"])

        expected = data.get("checksum")
        if expected is not None and okx_checksum(self.sorted_bids(), self.sorted_asks()) != int(expected):
            return False
        self.ready = True
        return True

    def top(self, levels: int) -> tuple[list[list[float]], list[list[float]]]:
        """Книга для потребителей: [[цена, размер]] числами, обрезанная."""
        bids = [[float(p), float(s)] for p, s in self.sorted_bids()[:levels]]
        asks = [[float(p), float(s)] for p, s in self.sorted_asks()[:levels]]
        return bids, asks
