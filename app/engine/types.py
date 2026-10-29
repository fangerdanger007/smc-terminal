"""
Структуры данных Smart Money движка (чистые dataclass, без внешних зависимостей).

Соглашение по сторонам:
    Side.BULL — бычья сущность (бычий OB/FVG, свип sell-side ликвидности с разворотом вверх, BOS вверх)
    Side.BEAR — медвежья сущность
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"

    @property
    def opposite(self) -> "Side":
        return Side.BEAR if self is Side.BULL else Side.BULL


class Bias(str, Enum):
    BULLISH = "Bullish"
    BEARISH = "Bearish"
    NEUTRAL = "Neutral"

    @classmethod
    def from_side(cls, side: Side | None) -> "Bias":
        if side is Side.BULL:
            return cls.BULLISH
        if side is Side.BEAR:
            return cls.BEARISH
        return cls.NEUTRAL

    def to_side(self) -> Side | None:
        if self is Bias.BULLISH:
            return Side.BULL
        if self is Bias.BEARISH:
            return Side.BEAR
        return None


@dataclass(slots=True, frozen=True)
class Candle:
    ts: int          # время открытия свечи, миллисекунды UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_bull(self) -> bool:
        return self.close > self.open

    @property
    def is_bear(self) -> bool:
        return self.close < self.open

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass(slots=True)
class Swing:
    index: int
    ts: int
    price: float
    kind: str            # "HIGH" | "LOW"
    label: str = ""      # HH / LH / HL / LL (относительно предыдущего свинга того же типа)


@dataclass(slots=True)
class StructureBreak:
    """Слом структуры по закрытию ТЕЛА свечи."""

    index: int
    ts: int
    kind: str            # "BOS" (продолжение) | "BMS" (разворот, смена тренда)
    side: Side           # направление слома
    level: float         # пробитый уровень
    swing_index: int     # индекс пробитого свинга


@dataclass(slots=True)
class LiquiditySweep:
    """Съём ликвидности: тень за уровень без закрытия телом (SFP / Swinp)."""

    index: int
    ts: int
    side: Side           # BULL = снята sell-side ликвидность (лои), ожидается рост; BEAR = buy-side
    level: float         # снятый уровень
    extreme: float       # экстремум тени (SL ставится за него)
    source: str          # "SWING" | "EQL" | "EQH" | "RANGE"
    close: float = 0.0   # закрытие свечи свипа (возврат внутрь)


@dataclass(slots=True)
class FVG:
    index: int           # индекс 3-й свечи (момент образования)
    impulse_index: int   # индекс 2-й (импульсной) свечи
    ts: int
    side: Side
    top: float
    bottom: float
    ce: float            # Consequent Encroachment — уровень 0.5
    status: str = "FRESH"      # FRESH | PARTIAL | FILLED
    fill_ratio: float = 0.0    # доля заполнения гэпа 0..1
    first_touch_index: int | None = None


@dataclass(slots=True)
class OrderBlock:
    index: int           # индекс свечи OB
    ts: int
    side: Side
    top: float
    bottom: float
    mid: float           # уровень 0.5 (mean threshold)
    impulse_index: int
    bos_index: int
    fvg: FVG | None = None
    volume_ok: bool = True
    status: str = "FRESH"          # FRESH | TESTED | INVALIDATED
    first_touch_index: int | None = None
    last_touch_index: int | None = None


@dataclass(slots=True)
class SponsoredCandle:
    index: int
    ts: int
    side: Side
    top: float           # SC уточняется до ТЕЛА свечи
    bottom: float
    swept_level: float


@dataclass(slots=True)
class EqualLevels:
    kind: str            # "EQH" | "EQL"
    level: float         # EQH: максимум кластера, EQL: минимум кластера (где лежат стопы)
    indices: list[int]
    count: int
    swept: bool = False
    swept_index: int | None = None
    swept_by_close: bool = False   # True — уровень пробит телом (это не свип, а BOS-подобный пробой)


@dataclass(slots=True)
class RangeInfo:
    high: float          # 1.0
    low: float           # 0.0
    eq: float            # 0.5
    high_index: int
    low_index: int
    eq_reactions: int    # число реакций цены на 0.5
    valid: bool          # валидный рейндж = есть реакция на 0.5 и достаточная длительность
    deviation_high: bool # был ложный пробой Range High
    deviation_low: bool  # был ложный пробой Range Low
    position: str        # "PREMIUM" | "DISCOUNT" | "EQUILIBRIUM"


@dataclass(slots=True)
class LawOfPower:
    side: Side           # сторона ИМПУЛЬСА
    impulse_bars: int
    correction_bars: int
    ratio: float         # N_corr / N_imp
    impulse_start: int
    impulse_end: int
    correction_end: int
    correction_ongoing: bool


@dataclass(slots=True)
class AnalysisResult:
    timeframe: str
    bias: Bias
    last_index: int
    last_ts: int
    last_close: float
    atr: float
    swings: list[Swing] = field(default_factory=list)
    major_swings: list[Swing] = field(default_factory=list)
    breaks: list[StructureBreak] = field(default_factory=list)
    sweeps: list[LiquiditySweep] = field(default_factory=list)
    fvgs: list[FVG] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    sponsored: list[SponsoredCandle] = field(default_factory=list)
    equal_levels: list[EqualLevels] = field(default_factory=list)
    range: RangeInfo | None = None
    law_of_power: LawOfPower | None = None
    amd_phase: str = "UNDEFINED"     # ACCUMULATION | MANIPULATION | DISTRIBUTION | UNDEFINED
    candles: list[Candle] = field(default_factory=list, repr=False)
