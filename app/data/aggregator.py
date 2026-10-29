"""
DataAggregator — единая точка получения свечей.

    * маршрутизация инструмента по цепочке провайдеров с автоматическим фолбэком;
    * повторные попытки с экспоненциальной паузой;
    * кэш с TTL по таймфреймам (чтобы не превышать лимиты бирж);
    * ограничение параллелизма (Semaphore);
    * отбрасывание незакрытой свечи (никаких «перерисовок»).
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.config import TDA_ORDER, Asset, Settings, Timeframe
from app.data.providers import (
    AlpacaProvider,
    BaseProvider,
    BinanceProvider,
    BybitProvider,
    ProviderError,
    YahooProvider,
)
from app.data.utils import drop_unclosed
from app.engine.types import Candle

log = logging.getLogger("smc.data")


class DataAggregator:
    RETRIES = 2
    BACKOFF = 0.7  # сек, умножается на 2**attempt

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.http_timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SMC-Terminal/1.0)"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        providers: list[BaseProvider] = [
            BinanceProvider(self.client, settings),
            BybitProvider(self.client, settings),
            YahooProvider(self.client, settings),
            AlpacaProvider(self.client, settings),
        ]
        self.providers: dict[str, BaseProvider] = {p.name: p for p in providers if p.available}
        self._cache: dict[tuple[str, Timeframe], tuple[float, list[Candle]]] = {}
        self._sem = asyncio.Semaphore(settings.max_concurrency)

    async def close(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ #
    async def get_candles(
        self, asset: Asset, tf: Timeframe, limit: int | None = None, use_cache: bool = True
    ) -> list[Candle]:
        """Закрытые свечи инструмента на таймфрейме (от старых к новым)."""
        limit = limit or self.settings.candles_limit
        key = (asset.ticker, tf)
        now = time.time()
        cached = self._cache.get(key)
        if use_cache and cached and cached[0] > now and len(cached[1]) >= min(limit, 60):
            return cached[1][-limit:]

        errors: list[str] = []
        for pname in asset.providers:
            provider = self.providers.get(pname)
            symbol = asset.symbols.get(pname)
            if provider is None or symbol is None:
                continue
            try:
                raw = await self._fetch_with_retry(provider, symbol, tf, limit)
            except ProviderError as exc:
                errors.append(str(exc))
                log.warning("%s %s: %s — пробуем следующий источник", asset.ticker, tf.value, exc)
                continue
            candles = drop_unclosed(raw, tf, int(now * 1000))
            if len(candles) < 30:
                errors.append(f"{pname}: слишком мало закрытых свечей ({len(candles)})")
                continue
            self._cache[key] = (now + self.settings.cache_ttl[tf], candles)
            return candles[-limit:]

        # Все источники недоступны: если есть устаревший кэш — лучше он, чем ничего (помечаем в логах)
        if cached:
            log.error("%s %s: все источники недоступны, используем устаревший кэш", asset.ticker, tf.value)
            return cached[1][-limit:]
        raise ProviderError(f"{asset.ticker} {tf.value}: " + "; ".join(errors or ["нет доступных провайдеров"]))

    async def get_all_timeframes(self, asset: Asset, limit: int | None = None) -> dict[Timeframe, list[Candle]]:
        """Свечи по всем ТФ (D1, H4, H1, M15) параллельно."""
        results = await asyncio.gather(*(self.get_candles(asset, tf, limit) for tf in TDA_ORDER))
        return dict(zip(TDA_ORDER, results))

    # ------------------------------------------------------------------ #
    async def _fetch_with_retry(
        self, provider: BaseProvider, symbol: str, tf: Timeframe, limit: int
    ) -> list[Candle]:
        last: ProviderError | None = None
        for attempt in range(self.RETRIES + 1):
            try:
                async with self._sem:
                    return await provider.fetch_klines(symbol, tf, limit)
            except ProviderError as exc:
                last = exc
                if attempt < self.RETRIES:
                    await asyncio.sleep(self.BACKOFF * (2**attempt))
        assert last is not None
        raise last
