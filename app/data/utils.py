"""Чистые функции для работы со свечами (без сетевых зависимостей — легко тестируются)."""
from __future__ import annotations

from app.config import TF_SECONDS, Timeframe
from app.engine.types import Candle


def drop_unclosed(candles: list[Candle], tf: Timeframe, now_ms: int) -> list[Candle]:
    """
    Оставляет только ЗАКРЫТЫЕ свечи (ts + длительность ТФ <= now).
    Анализ по незакрытой свече приводит к «перерисовке» сигналов, поэтому она отбрасывается.
    """
    tf_ms = TF_SECONDS[tf] * 1000
    return [c for c in candles if c.ts + tf_ms <= now_ms]


def resample(candles: list[Candle], target_seconds: int) -> list[Candle]:
    """
    Агрегирует свечи меньшего ТФ в больший (например, H1 -> H4).
    Корзины выровнены по UTC (00:00, 04:00, 08:00 ...). Для акций США сессия
    не совпадает с границами UTC, поэтому первая/последняя корзина дня могут быть неполными —
    для точного H4 по акциям используйте Alpaca (нативный 4Hour).
    """
    bucket_ms = target_seconds * 1000
    out: list[Candle] = []
    cur_start: int | None = None
    o = h = l = c = v = 0.0
    for k in candles:
        start = (k.ts // bucket_ms) * bucket_ms
        if cur_start is None or start != cur_start:
            if cur_start is not None:
                out.append(Candle(cur_start, o, h, l, c, v))
            cur_start, o, h, l, c, v = start, k.open, k.high, k.low, k.close, k.volume
        else:
            h, l, c, v = max(h, k.high), min(l, k.low), k.close, v + k.volume
    if cur_start is not None:
        out.append(Candle(cur_start, o, h, l, c, v))
    return out


def dedupe_sorted(candles: list[Candle]) -> list[Candle]:
    """Сортировка по времени и удаление дублей (на случай пересечения страниц API)."""
    by_ts = {c.ts: c for c in candles}
    return [by_ts[ts] for ts in sorted(by_ts)]
