"""
Асинхронные клиенты рыночных данных (K-lines).

    BinanceProvider — крипта (основной источник)
    BybitProvider   — крипта (резерв)
    YahooProvider   — акции США, фьючерсы на сырьё/золото (бесплатно, без ключа)
    AlpacaProvider  — акции США (нужны ключи ALPACA_API_KEY / ALPACA_API_SECRET)

Все провайдеры возвращают list[Candle], отсортированный по времени по возрастанию.
Последняя свеча может быть НЕЗАКРЫТОЙ — фильтрацию делает DataAggregator.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

import httpx

from app.config import TF_SECONDS, Settings, Timeframe
from app.data.utils import dedupe_sorted, resample
from app.engine.types import Candle


class ProviderError(Exception):
    """Ошибка получения данных от провайдера (сеть, формат ответа, лимиты)."""


class BaseProvider(ABC):
    name: str = "base"

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    @property
    def available(self) -> bool:
        """Провайдер можно использовать (например, заданы ключи API)."""
        return True

    @abstractmethod
    async def fetch_klines(self, symbol: str, tf: Timeframe, limit: int) -> list[Candle]:
        ...

    async def _get_json(self, url: str, **kwargs) -> dict | list:
        try:
            r = await self.client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: сетевая ошибка {exc!r}") from exc
        if r.status_code == 429:
            raise ProviderError(f"{self.name}: rate limit (429)")
        if r.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {r.status_code} {r.text[:200]}")
        try:
            return r.json()
        except ValueError as exc:
            raise ProviderError(f"{self.name}: невалидный JSON") from exc


# --------------------------------------------------------------------------- #
class BinanceProvider(BaseProvider):
    """Spot K-lines: GET /api/v3/klines (лимит 1000 свечей за запрос)."""

    name = "binance"
    INTERVALS = {Timeframe.M15: "15m", Timeframe.H1: "1h", Timeframe.H4: "4h", Timeframe.D1: "1d"}

    async def fetch_klines(self, symbol: str, tf: Timeframe, limit: int) -> list[Candle]:
        data = await self._get_json(
            f"{self.settings.binance_base_url}/api/v3/klines",
            params={"symbol": symbol, "interval": self.INTERVALS[tf], "limit": min(limit, 1000)},
        )
        try:
            return [
                Candle(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
                for k in data  # type: ignore[union-attr]
            ]
        except (TypeError, ValueError, IndexError) as exc:
            raise ProviderError(f"binance: неожиданный формат ответа для {symbol}") from exc


class BybitProvider(BaseProvider):
    """V5 market kline (spot). Ответ идёт от новых свечей к старым — разворачиваем."""

    name = "bybit"
    INTERVALS = {Timeframe.M15: "15", Timeframe.H1: "60", Timeframe.H4: "240", Timeframe.D1: "D"}

    async def fetch_klines(self, symbol: str, tf: Timeframe, limit: int) -> list[Candle]:
        data = await self._get_json(
            f"{self.settings.bybit_base_url}/v5/market/kline",
            params={"category": "spot", "symbol": symbol, "interval": self.INTERVALS[tf], "limit": min(limit, 1000)},
        )
        try:
            if data["retCode"] != 0:  # type: ignore[index]
                raise ProviderError(f"bybit: {data['retMsg']}")  # type: ignore[index]
            rows = data["result"]["list"]  # type: ignore[index]
            candles = [
                Candle(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in rows
            ]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ProviderError(f"bybit: неожиданный формат ответа для {symbol}") from exc
        return dedupe_sorted(candles)


# --------------------------------------------------------------------------- #
class YahooProvider(BaseProvider):
    """
    Yahoo Finance chart API (неофициальный, но стабильный).
    Нативных 4h нет -> H4 собирается ресемплингом из 60m.
    Ограничения Yahoo: 15m — не глубже 60 дней, 60m — до 730 дней.
    """

    name = "yahoo"
    BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
    # (interval, range)
    PARAMS = {
        Timeframe.M15: ("15m", "30d"),
        Timeframe.H1: ("60m", "180d"),
        Timeframe.H4: ("60m", "365d"),   # ресемплинг в H4
        Timeframe.D1: ("1d", "2y"),
    }

    async def fetch_klines(self, symbol: str, tf: Timeframe, limit: int) -> list[Candle]:
        interval, rng = self.PARAMS[tf]
        data = await self._get_json(
            f"{self.BASE}/{symbol}",
            params={"interval": interval, "range": rng, "includePrePost": "false"},
        )
        try:
            chart = data["chart"]  # type: ignore[index]
            if chart.get("error"):
                raise ProviderError(f"yahoo: {chart['error']}")
            res = chart["result"][0]
            quote = res["indicators"]["quote"][0]
            candles: list[Candle] = []
            for ts, o, h, l, c, v in zip(
                res["timestamp"], quote["open"], quote["high"], quote["low"], quote["close"], quote["volume"]
            ):
                if None in (o, h, l, c):      # Yahoo отдаёт null для пустых баров
                    continue
                candles.append(Candle(int(ts) * 1000, float(o), float(h), float(l), float(c), float(v or 0)))
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise ProviderError(f"yahoo: неожиданный формат ответа для {symbol}") from exc

        candles = dedupe_sorted(candles)
        if tf is Timeframe.H4:
            candles = resample(candles, TF_SECONDS[Timeframe.H4])
        return candles[-limit:]


class AlpacaProvider(BaseProvider):
    """
    Alpaca Market Data v2 (акции США). Бесплатный фид — IEX (ALPACA_FEED=iex), платный — sip.
    Запрашиваем sort=desc + limit, чтобы получить именно ПОСЛЕДНИЕ N баров, затем разворачиваем.
    """

    name = "alpaca"
    BASE = "https://data.alpaca.markets/v2/stocks"
    TIMEFRAMES = {Timeframe.M15: "15Min", Timeframe.H1: "1Hour", Timeframe.H4: "4Hour", Timeframe.D1: "1Day"}
    LOOKBACK_DAYS = {Timeframe.M15: 45, Timeframe.H1: 150, Timeframe.H4: 450, Timeframe.D1: 1000}

    @property
    def available(self) -> bool:
        return bool(self.settings.alpaca_key and self.settings.alpaca_secret)

    async def fetch_klines(self, symbol: str, tf: Timeframe, limit: int) -> list[Candle]:
        start = datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS[tf])
        data = await self._get_json(
            f"{self.BASE}/{symbol}/bars",
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret,
            },
            params={
                "timeframe": self.TIMEFRAMES[tf],
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": min(limit, 10000),
                "sort": "desc",
                "adjustment": "split",
                "feed": self.settings.alpaca_feed,
            },
        )
        try:
            bars = data.get("bars") or []  # type: ignore[union-attr]
            candles = [
                Candle(
                    int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp() * 1000),
                    float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b.get("v", 0)),
                )
                for b in bars
            ]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError(f"alpaca: неожиданный формат ответа для {symbol}") from exc
        return dedupe_sorted(candles)
