"""
Фоновый сканер рынка.

    SignalStore    — хранилище активных сигналов (in-memory; для продакшена см. README: Redis/Postgres)
    ScannerWorker  — asyncio-задача: каждые N секунд обходит все инструменты, тянет свечи,
                     гонит анализ в потоке (CPU-bound) и обновляет хранилище.

Свечи берутся ТОЛЬКО закрытые, поэтому результат пересчёта между закрытиями свечей не «дрожит».
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import ASSETS, Asset, Settings
from app.data.aggregator import DataAggregator
from app.data.providers import ProviderError
from app.engine.pipeline import run_pipeline
from app.engine.signals import EvaluationOutcome, SignalCandidate
from app.schemas import ScannerStatus

log = logging.getLogger("smc.scanner")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class StoredSignal:
    candidate: SignalCandidate
    created_at: datetime
    updated_at: datetime


class SignalStore:
    """
    Один активный сигнал на тикер. `created_at` сохраняется, пока сетап тот же (одинаковый signal_id).
    Сигнал старше max_age не показывается и повторно не создаётся (защита от «залипших» сетапов).
    """

    def __init__(self, max_age_hours: float) -> None:
        self.max_age = timedelta(hours=max_age_hours)
        self._items: dict[str, StoredSignal] = {}
        self._expired: set[str] = set()

    def update(self, ticker: str, cand: SignalCandidate | None) -> None:
        now = _now()
        if cand is None or cand.signal_id in self._expired:
            self._items.pop(ticker, None)
            return
        prev = self._items.get(ticker)
        if prev and prev.candidate.signal_id == cand.signal_id:
            if now - prev.created_at > self.max_age:
                self._expired.add(cand.signal_id)
                self._items.pop(ticker, None)
                return
            self._items[ticker] = StoredSignal(cand, prev.created_at, now)
        else:
            self._items[ticker] = StoredSignal(cand, now, now)

    def all(self) -> list[StoredSignal]:
        items = list(self._items.values())
        items.sort(key=lambda s: (s.candidate.quality_score, s.candidate.risk_reward_ratio), reverse=True)
        return items

    def get(self, ticker: str) -> StoredSignal | None:
        return self._items.get(ticker)

    def __len__(self) -> int:
        return len(self._items)


class ScannerWorker:
    def __init__(self, settings: Settings, aggregator: DataAggregator, store: SignalStore) -> None:
        self.settings = settings
        self.aggregator = aggregator
        self.store = store
        self._task: asyncio.Task | None = None
        self._scan_lock = asyncio.Lock()
        self.last_started: datetime | None = None
        self.last_finished: datetime | None = None
        self.last_duration: float | None = None
        self.assets_ok = 0
        self.errors: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="smc-scanner")
            log.info("Сканер запущен, интервал %s c", self.settings.scan_interval_seconds)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # воркер не должен падать из-за одной ошибки
                log.exception("Сбой цикла сканирования")
            await asyncio.sleep(self.settings.scan_interval_seconds)

    # ------------------------------------------------------------------ #
    async def scan_once(self) -> None:
        """Один полный проход по всем инструментам (параллельно, с ограничением конкурентности)."""
        async with self._scan_lock:                 # два скана одновременно не запускаем
            t0 = time.monotonic()
            self.last_started = _now()
            errors: dict[str, str] = {}
            ok = 0

            async def one(asset: Asset) -> None:
                nonlocal ok
                try:
                    outcome = await self.scan_asset(asset)
                except ProviderError as exc:
                    errors[asset.ticker] = str(exc)
                    return                           # при ошибке данных прежний сигнал не трогаем
                except Exception as exc:
                    log.exception("Ошибка анализа %s", asset.ticker)
                    errors[asset.ticker] = f"{type(exc).__name__}: {exc}"
                    return
                ok += 1
                self.store.update(asset.ticker, outcome.signal)

            await asyncio.gather(*(one(a) for a in ASSETS))
            self.errors = errors
            self.assets_ok = ok
            self.last_finished = _now()
            self.last_duration = round(time.monotonic() - t0, 2)
            log.info("Скан завершён: %d/%d ок, сигналов: %d, %.1f c", ok, len(ASSETS), len(self.store), self.last_duration)

    async def scan_asset(self, asset: Asset) -> EvaluationOutcome:
        candles = await self.aggregator.get_all_timeframes(asset)
        _, outcome = await asyncio.to_thread(run_pipeline, asset, candles, self.settings.engine)
        return outcome

    # ------------------------------------------------------------------ #
    def status(self) -> ScannerStatus:
        return ScannerStatus(
            running=self._task is not None and not self._task.done(),
            interval_seconds=self.settings.scan_interval_seconds,
            last_scan_started=self.last_started,
            last_scan_finished=self.last_finished,
            last_scan_duration_sec=self.last_duration,
            assets_total=len(ASSETS),
            assets_ok=self.assets_ok,
            active_signals=len(self.store),
            errors=self.errors,
        )
