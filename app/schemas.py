"""Pydantic-схемы API (Pydantic v2). Основной формат — Signal из ТЗ + расширенные поля для UI."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import Market
from app.engine.signals import SignalCandidate


class PoiOut(BaseModel):
    timeframe: str
    type: Literal["OB", "FVG"]
    top: float
    bottom: float
    ce: float = Field(description="Уровень 0.5 зоны")


class SweepOut(BaseModel):
    timeframe: str
    source: Literal["EQL", "EQH", "RANGE", "SWING"]
    level: float
    extreme: float = Field(description="Экстремум тени свипа — за него ставится стоп")
    ts: int


class ScoreBreakdownOut(BaseModel):
    """Баллы 0..100 по каждому критерию Q_total (веса: 30/25/20/15/10)."""

    htf_sync: float
    sweep: float
    fvg: float
    law_of_power: float
    ltf_confirm: float
    total: float


class SignalOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "ticker": "XAUUSD",
            "market": "COMMODITIES",
            "direction": "LONG",
            "quality_score": 88,
            "risk_level": "LOW",
            "timeframe_sync": "D1-Bullish / H4-Bullish / M15-BOS_Confirmed",
            "entry_price": 2742.50,
            "stop_loss": 2731.00,
            "take_profit_1": 2777.00,
            "risk_reward_ratio": 3.0,
            "suggested_position_risk_percent": 1.0,
            "reason": "Тест зоны Order Block на H4 после съёма EQL ликвидности. Подтверждён BOS на M15.",
        }
    })

    # --- Поля из ТЗ ---------------------------------------------------------
    ticker: str
    market: Market
    direction: Literal["LONG", "SHORT"]
    quality_score: float = Field(ge=0, le=100)
    risk_level: Literal["LOW", "MEDIUM"]
    timeframe_sync: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    risk_reward_ratio: float
    suggested_position_risk_percent: float
    reason: str

    # --- Расширение для терминала ---------------------------------------------
    signal_id: str
    take_profit_2: float | None = None
    entry_type: Literal["LIMIT", "MARKET"]
    verdict: str = Field(description="Готовая строка вердикта для Risk Panel")
    amd_phase: str
    law_of_power_ratio: float | None = None
    poi: PoiOut
    sweep: SweepOut
    score_breakdown: ScoreBreakdownOut
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_candidate(cls, c: SignalCandidate, created_at: datetime, updated_at: datetime) -> "SignalOut":
        return cls(
            ticker=c.ticker,
            market=Market(c.market),
            direction=c.direction,  # type: ignore[arg-type]
            quality_score=c.quality_score,
            risk_level=c.risk_level,  # type: ignore[arg-type]
            timeframe_sync=c.timeframe_sync,
            entry_price=c.entry_price,
            stop_loss=c.stop_loss,
            take_profit_1=c.take_profit_1,
            risk_reward_ratio=c.risk_reward_ratio,
            suggested_position_risk_percent=c.suggested_position_risk_percent,
            reason=c.reason,
            signal_id=c.signal_id,
            take_profit_2=c.take_profit_2,
            entry_type=c.entry_type,  # type: ignore[arg-type]
            verdict=c.verdict,
            amd_phase=c.amd_phase,
            law_of_power_ratio=c.law_of_power_ratio,
            poi=PoiOut(**c.poi),
            sweep=SweepOut(**c.sweep),
            score_breakdown=ScoreBreakdownOut(
                htf_sync=c.score_breakdown.htf_sync,
                sweep=c.score_breakdown.sweep,
                fvg=c.score_breakdown.fvg,
                law_of_power=c.score_breakdown.law_of_power,
                ltf_confirm=c.score_breakdown.ltf_confirm,
                total=c.score_breakdown.total,
            ),
            created_at=created_at,
            updated_at=updated_at,
        )


class ScanResult(BaseModel):
    """Результат разового сканирования /api/v1/scan/{ticker} (с диагностикой отказов)."""

    ticker: str
    biases: dict[str, str]
    signal: SignalOut | None
    rejections: list[str]


class AssetOut(BaseModel):
    ticker: str
    name: str
    market: Market


class CandleOut(BaseModel):
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class ScannerStatus(BaseModel):
    running: bool
    interval_seconds: int
    last_scan_started: datetime | None
    last_scan_finished: datetime | None
    last_scan_duration_sec: float | None
    assets_total: int
    assets_ok: int
    active_signals: int
    errors: dict[str, str]
