"""
FastAPI-приложение: REST API торгового терминала.

Запуск:  uvicorn app.main:app --host 0.0.0.0 --port 8000
Документация: /docs (Swagger), /redoc

ВАЖНО: запускайте ОДИН процесс (workers=1) — фоновый сканер и хранилище сигналов живут в памяти процесса.
Для горизонтального масштабирования вынесите сканер в отдельный сервис, а сигналы — в Redis (см. README).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.config import ASSETS, ASSETS_BY_TICKER, Asset, Market, Timeframe, get_settings
from app.data.aggregator import DataAggregator
from app.data.providers import ProviderError
from app.engine.analyzer import MarketAnalyzer, analysis_to_dict
from app.engine.pipeline import run_pipeline
from app.scanner import ScannerWorker, SignalStore
from app.schemas import AssetOut, CandleOut, ScannerStatus, ScanResult, SignalOut

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("smc.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Старт: агрегатор данных + хранилище + фоновый сканер. Стоп: корректное закрытие."""
    settings = get_settings()
    aggregator = DataAggregator(settings)
    store = SignalStore(settings.signal_max_age_hours)
    worker = ScannerWorker(settings, aggregator, store)

    app.state.settings = settings
    app.state.aggregator = aggregator
    app.state.store = store
    app.state.worker = worker

    if settings.scanner_enabled:
        worker.start()
    try:
        yield
    finally:
        await worker.stop()
        await aggregator.close()


app = FastAPI(
    title="SMC Trading Terminal API",
    version="1.0.0",
    description="Автоматический сканер рынка по Smart Money стратегии с модулем расчёта рисков.",
    lifespan=lifespan,
@app.get("/")
async def root():
    return {
        "status": "online",
        "message": "SMC Terminal Backend is running",
        "endpoints": {
            "health": "/health",
            "tickers": "/api/v1/tickers",
            "active_signals": "/api/v1/signals/active",
            "scanner_status": "/api/v1/scanner/status",
            "docs": "/docs"
        }
    }
    )

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Вспомогательное
# --------------------------------------------------------------------------- #
def _asset(ticker: str) -> Asset:
    asset = ASSETS_BY_TICKER.get(ticker.upper())
    if asset is None:
        raise HTTPException(404, f"Тикер {ticker!r} не найден. Доступные: {', '.join(ASSETS_BY_TICKER)}")
    return asset


def _tf(value: str) -> Timeframe:
    try:
        return Timeframe(value.upper())
    except ValueError:
        raise HTTPException(422, "tf должен быть одним из: D1, H4, H1, M15") from None


# --------------------------------------------------------------------------- #
# Служебные
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/v1/tickers", response_model=list[AssetOut], tags=["market"])
async def list_tickers(market: Market | None = None) -> list[AssetOut]:
    """Список поддерживаемых инструментов (для строки поиска терминала)."""
    return [AssetOut(ticker=a.ticker, name=a.name, market=a.market) for a in ASSETS if market in (None, a.market)]


@app.get("/api/v1/scanner/status", response_model=ScannerStatus, tags=["system"])
async def scanner_status() -> ScannerStatus:
    return app.state.worker.status()


@app.post("/api/v1/scanner/run", status_code=202, tags=["system"])
async def run_scan_now() -> dict:
    """Запустить внеочередной проход сканера (не ждёт завершения)."""
    asyncio.create_task(app.state.worker.scan_once())
    return {"status": "scheduled"}


# --------------------------------------------------------------------------- #
# Сигналы
# --------------------------------------------------------------------------- #
@app.get("/api/v1/signals/active", response_model=list[SignalOut], tags=["signals"])
async def active_signals(
    market: Market | None = Query(None, description="Фильтр по рынку"),
    direction: Literal["LONG", "SHORT"] | None = Query(None),
    min_quality: float = Query(60.0, ge=0, le=100, description="Минимальный Q_total, %"),
    limit: int = Query(50, ge=1, le=200),
) -> list[SignalOut]:
    """Актуальные торговые сигналы (Q_total >= 60%, RR >= 3), отсортированы по качеству."""
    out: list[SignalOut] = []
    for item in app.state.store.all():
        c = item.candidate
        if market and c.market != market.value:
            continue
        if direction and c.direction != direction:
            continue
        if c.quality_score < min_quality:
            continue
        out.append(SignalOut.from_candidate(c, item.created_at, item.updated_at))
        if len(out) >= limit:
            break
    return out


@app.get("/api/v1/signals/{ticker}", response_model=SignalOut, tags=["signals"])
async def signal_for_ticker(ticker: str) -> SignalOut:
    """Активный сигнал по тикеру из кэша сканера."""
    asset = _asset(ticker)
    item = app.state.store.get(asset.ticker)
    if item is None:
        raise HTTPException(404, f"Активного сигнала по {asset.ticker} нет")
    return SignalOut.from_candidate(item.candidate, item.created_at, item.updated_at)


@app.get("/api/v1/scan/{ticker}", response_model=ScanResult, tags=["signals"])
async def scan_ticker(ticker: str) -> ScanResult:
    """Разовый анализ тикера «здесь и сейчас» + причины отказа, если сетапа нет (для Risk Panel / отладки)."""
    asset = _asset(ticker)
    state = app.state
    try:
        candles = await state.aggregator.get_all_timeframes(asset)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    analyses, outcome = await asyncio.to_thread(run_pipeline, asset, candles, state.settings.engine)
    signal = None
    if outcome.signal:
        now = datetime.now(timezone.utc)
        signal = SignalOut.from_candidate(outcome.signal, now, now)
    return ScanResult(
        ticker=asset.ticker,
        biases={tf.value: a.bias.value for tf, a in analyses.items()},
        signal=signal,
        rejections=outcome.rejections,
    )


# --------------------------------------------------------------------------- #
# Данные для графика (Lightweight Charts)
# --------------------------------------------------------------------------- #
@app.get("/api/v1/candles/{ticker}", response_model=list[CandleOut], tags=["chart"])
async def candles(ticker: str, tf: str = "H4", limit: int = Query(500, ge=30, le=1000)) -> list[CandleOut]:
    """Закрытые свечи для построения графика. Время `ts` — миллисекунды UTC."""
    asset, timeframe = _asset(ticker), _tf(tf)
    try:
        data = await app.state.aggregator.get_candles(asset, timeframe, limit)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    return [CandleOut(ts=c.ts, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume) for c in data]


@app.get("/api/v1/analysis/{ticker}", tags=["chart"])
async def analysis(ticker: str, tf: str = "H4", limit: int = Query(500, ge=30, le=1000)) -> dict:
    """
    Все оверлеи для графика: свинги (с метками HH/HL/LH/LL), BOS/BMS, свипы, FVG (с CE 0.5),
    Order Block, Sponsored Candle, EQH/EQL, Range (0.0/0.5/1.0), Law of Power, фаза AMD.
    Индексы (`index`) ссылаются на позицию в массиве свечей из /api/v1/candles с теми же tf и limit.
    """
    asset, timeframe = _asset(ticker), _tf(tf)
    state = app.state
    try:
        data = await state.aggregator.get_candles(asset, timeframe, limit)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    result = await asyncio.to_thread(lambda: MarketAnalyzer(data, timeframe.value, state.settings.engine).analyze())
    return analysis_to_dict(result)
