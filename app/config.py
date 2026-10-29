"""
Глобальная конфигурация: таймфреймы, реестр тикеров, настройки сканера и движка.

Все параметры можно переопределить переменными окружения (см. .env.example).
Модуль намеренно не зависит от сторонних библиотек.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


# --------------------------------------------------------------------------- #
# Базовые перечисления
# --------------------------------------------------------------------------- #
class Market(str, Enum):
    CRYPTO = "CRYPTO"
    STOCKS = "STOCKS"
    COMMODITIES = "COMMODITIES"


class Timeframe(str, Enum):
    D1 = "D1"
    H4 = "H4"
    H1 = "H1"
    M15 = "M15"


TF_SECONDS: dict[Timeframe, int] = {
    Timeframe.M15: 15 * 60,
    Timeframe.H1: 60 * 60,
    Timeframe.H4: 4 * 60 * 60,
    Timeframe.D1: 24 * 60 * 60,
}

# Порядок Top-Down анализа: от старшего к младшему
TDA_ORDER: tuple[Timeframe, ...] = (Timeframe.D1, Timeframe.H4, Timeframe.H1, Timeframe.M15)


# --------------------------------------------------------------------------- #
# Реестр инструментов
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Asset:
    """
    Описание торгового инструмента.

    `symbols` — тикер у каждого провайдера данных (имена тикеров у бирж разные).
    `providers` — цепочка провайдеров по приоритету: если первый недоступен,
    агрегатор автоматически переходит к следующему.
    """

    ticker: str
    market: Market
    name: str
    providers: tuple[str, ...]
    symbols: dict[str, str] = field(default_factory=dict)


def _crypto(ticker: str, name: str) -> Asset:
    return Asset(
        ticker=ticker,
        market=Market.CRYPTO,
        name=name,
        providers=("binance", "bybit", "yahoo"),
        symbols={"binance": f"{ticker}USDT", "bybit": f"{ticker}USDT", "yahoo": f"{ticker}-USD"},
    )


def _stock(ticker: str, name: str) -> Asset:
    return Asset(
        ticker=ticker,
        market=Market.STOCKS,
        name=name,
        providers=("alpaca", "yahoo"),  # alpaca пропускается, если не заданы ключи
        symbols={"alpaca": ticker, "yahoo": ticker},
    )


def _commodity(ticker: str, name: str, yahoo_symbol: str) -> Asset:
    return Asset(
        ticker=ticker,
        market=Market.COMMODITIES,
        name=name,
        providers=("yahoo",),
        symbols={"yahoo": yahoo_symbol},
    )


# ВАЖНО: на Yahoo нет спота XAUUSD/WTI — используются фьючерсы (GC=F, CL=F).
# Цены фьючерса отличаются от спота на величину премии/контанго.
# Для точного спота подключите платного провайдера (OANDA, Twelve Data, Polygon).
ASSETS: tuple[Asset, ...] = (
    _crypto("BTC", "Bitcoin"),
    _crypto("ETH", "Ethereum"),
    _crypto("SOL", "Solana"),
    _crypto("BNB", "BNB"),
    _crypto("XRP", "XRP"),
    _commodity("XAUUSD", "Gold (GC=F)", "GC=F"),
    _commodity("XAGUSD", "Silver (SI=F)", "SI=F"),
    _commodity("CL", "Crude Oil WTI (CL=F)", "CL=F"),
    _stock("TSLA", "Tesla"),
    _stock("AAPL", "Apple"),
    _stock("NVDA", "NVIDIA"),
    _stock("MSFT", "Microsoft"),
    _stock("SPY", "S&P 500 ETF"),
)

ASSETS_BY_TICKER: dict[str, Asset] = {a.ticker: a for a in ASSETS}


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #
def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class EngineConfig:
    """Параметры Smart Money движка и риск-модуля."""

    # --- Анализатор -------------------------------------------------------- #
    atr_period: int = 14
    strict_swing_rule: bool = True      # правило 3-й свечи для свингов (см. ТЗ)
    fvg_displacement_atr: float = 1.0   # 2-я свеча FVG >= N * ATR ("большая" свеча)
    fvg_min_gap_atr: float = 0.10       # минимальный размер гэпа в ATR
    ob_lookback: int = 5                # сколько свечей назад искать OB перед импульсом
    ob_bos_window: int = 4              # BOS должен случиться не позже N свечей после импульса
    require_volume: bool = True         # "чистый объём" для OB (если объём доступен)
    volume_mult: float = 1.0            # объём импульса >= mult * средний объём за 20 свечей
    eq_tolerance_atr: float = 0.25      # допуск равенства EQH/EQL в ATR
    eq_min_separation: int = 3          # минимум баров между членами EQ-кластера
    major_leg_atr: float = 1.5          # минимальный размер ноги (ZigZag) для Range/LoP
    range_min_bars: int = 10            # минимальная длительность рейнджа

    # --- Сигналы ----------------------------------------------------------- #
    min_rr: float = 3.0                 # фильтр RR (жёсткий)
    optimal_rr: float = 4.0
    min_quality: float = 60.0           # ниже — NO TRADE
    high_quality: float = 80.0          # выше — "Идеальный вход"
    entry_mode: str = "conservative"    # conservative | aggressive
    ltf_entry_level: str = "mid"        # для LTF OB: "mid" (0.5) | "edge" (верхняя граница для лонга)
    sweep_window_hours: float = 12.0    # давность свипа, учитываемого в сетапе
    poi_touch_window_m15: int = 32      # POI должна быть протестирована за N последних M15 баров
    confirm_window_m15: int = 24        # давность LTF BOS / SC
    sl_buffer_atr: float = 0.10         # буфер SL в ATR(M15)
    sl_buffer_pct: float = 0.0001       # минимальный буфер SL в % от цены (спред/проскальзывание)
    base_position_risk_pct: float = 1.0  # риск на сделку при Q >= 80
    reduced_position_risk_pct: float = 0.5  # при 60 <= Q < 80

    # Веса критериев Q_total (сумма = 1.0)
    w_htf_sync: float = 0.30
    w_sweep: float = 0.25
    w_fvg: float = 0.20
    w_law_of_power: float = 0.15
    w_ltf_confirm: float = 0.10


@dataclass(frozen=True)
class Settings:
    """Параметры приложения (данные, воркер, API)."""

    scan_interval_seconds: int = field(default_factory=lambda: _env_int("SCAN_INTERVAL_SECONDS", 300))
    scanner_enabled: bool = field(default_factory=lambda: _env_bool("SCANNER_ENABLED", True))
    candles_limit: int = field(default_factory=lambda: _env_int("CANDLES_LIMIT", 500))
    max_concurrency: int = field(default_factory=lambda: _env_int("MAX_CONCURRENCY", 6))
    http_timeout: float = field(default_factory=lambda: _env_float("HTTP_TIMEOUT", 12.0))
    signal_max_age_hours: float = field(default_factory=lambda: _env_float("SIGNAL_MAX_AGE_HOURS", 24.0))

    binance_base_url: str = field(default_factory=lambda: os.getenv("BINANCE_BASE_URL", "https://api.binance.com"))
    bybit_base_url: str = field(default_factory=lambda: os.getenv("BYBIT_BASE_URL", "https://api.bybit.com"))
    alpaca_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret: str = field(default_factory=lambda: os.getenv("ALPACA_API_SECRET", ""))
    alpaca_feed: str = field(default_factory=lambda: os.getenv("ALPACA_FEED", "iex"))  # iex (free) | sip

    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip())
    )

    # TTL кэша свечей по таймфреймам (секунды)
    cache_ttl: dict[Timeframe, int] = field(
        default_factory=lambda: {
            Timeframe.M15: 45,
            Timeframe.H1: 120,
            Timeframe.H4: 600,
            Timeframe.D1: 1800,
        }
    )

    engine: EngineConfig = field(default_factory=EngineConfig)


def get_settings() -> Settings:
    return Settings()
