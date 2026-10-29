"""Склейка анализатора и сигнального движка: свечи по ТФ -> анализ по ТФ -> оценка инструмента."""
from __future__ import annotations

from typing import Mapping, Sequence

from app.config import Asset, EngineConfig, Timeframe
from app.engine.analyzer import MarketAnalyzer
from app.engine.signals import EvaluationOutcome, SignalEngine
from app.engine.types import AnalysisResult, Candle


def analyze_all(
    candles_by_tf: Mapping[Timeframe, Sequence[Candle]], config: EngineConfig
) -> dict[Timeframe, AnalysisResult]:
    """Top-Down анализ: каждый таймфрейм независимо прогоняется через MarketAnalyzer."""
    return {tf: MarketAnalyzer(cs, tf.value, config).analyze() for tf, cs in candles_by_tf.items()}


def run_pipeline(
    asset: Asset,
    candles_by_tf: Mapping[Timeframe, Sequence[Candle]],
    config: EngineConfig,
) -> tuple[dict[Timeframe, AnalysisResult], EvaluationOutcome]:
    """CPU-bound часть сканирования (вызывается в отдельном потоке, чтобы не блокировать event loop)."""
    analyses = analyze_all(candles_by_tf, config)
    outcome = SignalEngine(config).evaluate(asset, analyses)
    return analyses, outcome
