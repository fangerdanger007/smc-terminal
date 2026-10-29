"""
SignalEngine — модуль торговых сигналов и расчёта рисков.

Конвейер для одного инструмента (Top-Down Analysis):
    1. HTF-синхронизация: bias H4 задаёт направление, D1 не должен быть против.
    2. Поиск POI (OB / FVG) на D1/H4/H1, которую цена ТЕСТИРУЕТ прямо сейчас (проверка по M15).
    3. Поиск свипа ликвидности (EQL/EQH/Range Deviation/SFP) перед входом, дотянувшегося до POI.
    4. LTF-подтверждение: BOS или Sponsored Candle на M15 после свипа.
    5. Расчёт Entry / SL (за тенью свипа) / TP (противоположная ликвидность) и RR.
    6. Скоринг Q_total по 5 критериям; фильтры RR >= 3 и Q >= 60.

Модуль не зависит от FastAPI/Pydantic: возвращает обычные dataclass, а API-слой
сам преобразует их в Pydantic-схемы.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from app.config import TF_SECONDS, Asset, EngineConfig, Timeframe
from app.engine.types import (
    FVG,
    AnalysisResult,
    LiquiditySweep,
    OrderBlock,
    Side,
)

M15_MS = TF_SECONDS[Timeframe.M15] * 1000

# Баллы за качество свипа. По ТЗ: EQL/EQH или Range Deviation = 100, нет свипа = 0.
# Обычный SFP за свинг (без EQ/рейнджа) засчитывается частично — это осознанное смягчение.
SWEEP_SCORES: dict[str, float] = {"EQL": 100.0, "EQH": 100.0, "RANGE": 100.0, "SWING": 70.0}
SWEEP_PRIORITY: dict[str, int] = {"EQL": 0, "EQH": 0, "RANGE": 1, "SWING": 2}

SWEEP_RU: dict[str, str] = {
    "EQL": "ликвидности EQL",
    "EQH": "ликвидности EQH",
    "RANGE": "ликвидности за границей рейнджа (Deviation)",
    "SWING": "ликвидности за свингом (SFP)",
}


# --------------------------------------------------------------------------- #
# Результирующие структуры
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Poi:
    """Точка интереса: зона OB или FVG на конкретном таймфрейме."""

    tf: Timeframe
    kind: str                # "OB" | "FVG"
    side: Side
    top: float
    bottom: float
    mid: float
    ob: OrderBlock | None = None
    fvg: FVG | None = None


@dataclass(slots=True)
class ScoreBreakdown:
    htf_sync: float
    sweep: float
    fvg: float
    law_of_power: float
    ltf_confirm: float
    total: float


@dataclass(slots=True)
class SignalCandidate:
    signal_id: str
    ticker: str
    market: str
    direction: str                   # LONG | SHORT
    quality_score: float
    risk_level: str                  # LOW | MEDIUM
    timeframe_sync: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    risk_reward_ratio: float
    suggested_position_risk_percent: float
    reason: str
    entry_type: str                  # LIMIT | MARKET
    poi: dict
    sweep: dict
    amd_phase: str
    law_of_power_ratio: float | None
    score_breakdown: ScoreBreakdown
    generated_ts: int                # ts последней обработанной M15-свечи, мс
    verdict: str = ""


@dataclass(slots=True)
class EvaluationOutcome:
    signal: SignalCandidate | None
    rejections: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Движок
# --------------------------------------------------------------------------- #
class SignalEngine:
    def __init__(self, config: EngineConfig | None = None) -> None:
        self.cfg = config or EngineConfig()

    # ------------------------------------------------------------------ #
    def evaluate(self, asset: Asset, analyses: Mapping[Timeframe, AnalysisResult]) -> EvaluationOutcome:
        """Полная оценка инструмента. Возвращает лучший сетап (или причины отказа)."""
        d1, h4, m15 = analyses[Timeframe.D1], analyses[Timeframe.H4], analyses[Timeframe.M15]
        rejections: list[str] = []

        if not m15.candles or len(m15.candles) < 30 or len(h4.candles) < 30:
            return EvaluationOutcome(None, ["Недостаточно данных для анализа"])

        side = h4.bias.to_side()
        if side is None:
            return EvaluationOutcome(None, ["H4 bias нейтральный — направления нет"])
        d1_side = d1.bias.to_side()
        if d1_side is side.opposite:
            return EvaluationOutcome(None, [f"Контртренд: H4 {h4.bias.value}, D1 {d1.bias.value}"])

        pois = self._find_pois(side, analyses)
        if not pois:
            return EvaluationOutcome(None, ["Нет POI (OB/FVG), которую цена тестирует сейчас"])

        best: tuple[float, float, SignalCandidate] | None = None
        any_sweep = False
        rr_fail: float | None = None
        low_q: float | None = None

        for poi in pois:
            for sweep in self._find_sweeps(side, poi, analyses):
                any_sweep = True
                outcome = self._build_setup(asset, side, poi, sweep, analyses, d1_side)
                if isinstance(outcome, str):
                    rejections.append(outcome)
                    continue
                kind, value = outcome
                if kind == "rr":
                    rr_fail = max(rr_fail or 0.0, value)  # type: ignore[arg-type]
                    continue
                cand: SignalCandidate = value  # type: ignore[assignment]
                if cand.quality_score < self.cfg.min_quality:
                    low_q = max(low_q or 0.0, cand.quality_score)
                    continue
                key = (cand.quality_score, cand.risk_reward_ratio)
                if best is None or key > (best[0], best[1]):
                    best = (key[0], key[1], cand)

        if best is not None:
            return EvaluationOutcome(best[2], [])

        if not any_sweep:
            rejections.append("Нет свипа ликвидности перед POI (или он вне окна/не дотянулся до зоны)")
        if rr_fail is not None:
            rejections.append(f"RR {rr_fail:.2f} < {self.cfg.min_rr:.1f} — сетап отсеян по риск/прибыль")
        if low_q is not None:
            rejections.append(f"Q_total {low_q:.0f}% < {self.cfg.min_quality:.0f}% — NO TRADE")
        return EvaluationOutcome(None, rejections or ["Сетап не сформирован"])

    # ------------------------------------------------------------------ #
    # POI
    # ------------------------------------------------------------------ #
    def _find_pois(self, side: Side, analyses: Mapping[Timeframe, AnalysisResult]) -> list[Poi]:
        """OB/FVG нужной стороны на H4/H1/D1, которые цена тестирует (по последним M15 барам)."""
        m15 = analyses[Timeframe.M15]
        window_start = m15.last_ts - self.cfg.poi_touch_window_m15 * M15_MS
        pois: list[Poi] = []

        for tf in (Timeframe.H4, Timeframe.H1, Timeframe.D1):
            a = analyses[tf]
            tf_ms = TF_SECONDS[tf] * 1000
            for ob in a.order_blocks:
                if ob.side is not side or ob.status == "INVALIDATED":
                    continue
                # «Свежесть»: до текущего теста OB не касались
                if ob.first_touch_index is not None:
                    touch_end = a.candles[ob.first_touch_index].ts + tf_ms
                    if touch_end < window_start:
                        continue
                if self._in_play(side, ob.top, ob.bottom, m15):
                    pois.append(Poi(tf, "OB", side, ob.top, ob.bottom, ob.mid, ob=ob))
            for fvg in a.fvgs:
                if fvg.side is not side or fvg.status == "FILLED":
                    continue
                if self._in_play(side, fvg.top, fvg.bottom, m15):
                    pois.append(Poi(tf, "FVG", side, fvg.top, fvg.bottom, fvg.ce, fvg=fvg))

        order = {Timeframe.D1: 0, Timeframe.H4: 1, Timeframe.H1: 2}
        pois.sort(key=lambda p: (order[p.tf], 0 if p.kind == "OB" else 1))
        return pois

    def _in_play(self, side: Side, top: float, bottom: float, m15: AnalysisResult) -> bool:
        """Зона «в игре»: за последние N M15-баров цена зашла в неё, и зона не пробита закрытием."""
        window = m15.candles[-self.cfg.poi_touch_window_m15 :]
        touched = any(k.low <= top and k.high >= bottom for k in window)
        alive = m15.last_close >= bottom if side is Side.BULL else m15.last_close <= top
        return touched and alive

    # ------------------------------------------------------------------ #
    # Свипы
    # ------------------------------------------------------------------ #
    def _find_sweeps(
        self, side: Side, poi: Poi, analyses: Mapping[Timeframe, AnalysisResult]
    ) -> list[tuple[Timeframe, LiquiditySweep]]:
        """
        Свипы нужной стороны за последние sweep_window_hours (M15/H1/H4),
        тень которых дотянулась до POI: для лонга extreme <= poi.top, для шорта extreme >= poi.bottom.
        """
        m15 = analyses[Timeframe.M15]
        window_start = m15.last_ts - int(self.cfg.sweep_window_hours * 3600 * 1000)
        found: list[tuple[Timeframe, LiquiditySweep]] = []
        for tf in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
            for sw in analyses[tf].sweeps:
                if sw.side is not side or sw.ts < window_start:
                    continue
                reaches = sw.extreme <= poi.top if side is Side.BULL else sw.extreme >= poi.bottom
                if reaches:
                    found.append((tf, sw))
        found.sort(key=lambda x: (SWEEP_PRIORITY[x[1].source], -x[1].ts))
        return found

    # ------------------------------------------------------------------ #
    # Построение сетапа
    # ------------------------------------------------------------------ #
    def _build_setup(
        self,
        asset: Asset,
        side: Side,
        poi: Poi,
        sweep_ref: tuple[Timeframe, LiquiditySweep],
        analyses: Mapping[Timeframe, AnalysisResult],
        d1_side: Side | None,
    ) -> str | tuple[str, object]:
        """
        Возвращает:
            str                      — причина отказа (диагностика),
            ("rr", best_rr)          — RR ниже порога,
            ("ok", SignalCandidate)  — готовый кандидат (Q ещё не отфильтрован по порогу).
        """
        cfg = self.cfg
        d1, h4, h1, m15 = (analyses[t] for t in (Timeframe.D1, Timeframe.H4, Timeframe.H1, Timeframe.M15))
        sweep_tf, sweep = sweep_ref
        is_long = side is Side.BULL
        last_close = m15.last_close
        atr15 = m15.atr or 0.0

        # --- LTF подтверждение: BOS / SC после свипа ---------------------------------
        conf_start = m15.last_ts - cfg.confirm_window_m15 * M15_MS
        bos = next(
            (b for b in reversed(m15.breaks) if b.side is side and b.ts > sweep.ts and b.ts >= conf_start), None
        )
        sc = next(
            (s for s in reversed(m15.sponsored) if s.side is side and s.ts >= sweep.ts and s.ts >= conf_start), None
        )
        confirmed = bos is not None or sc is not None
        confirm_label = "BOS_Confirmed" if bos else "SC_Confirmed" if sc else "Unconfirmed"

        # --- Entry ---------------------------------------------------------------------
        entry, entry_type, entry_src = self._pick_entry(side, poi, sweep, m15)
        if cfg.entry_mode == "aggressive":
            inside = poi.bottom <= last_close <= poi.top + 0.25 * atr15 if is_long else (
                poi.bottom - 0.25 * atr15 <= last_close <= poi.top
            )
            if not inside:
                return "Aggressive: закрытие M15 не вернулось внутрь POI"
            entry, entry_type, entry_src = last_close, "MARKET", "close M15 внутри POI"
        elif (is_long and entry >= last_close) or (not is_long and entry <= last_close):
            entry, entry_type = last_close, "MARKET"      # лимитный уровень уже пройден ценой

        # --- Stop Loss: за тенью свипа + буфер ---------------------------------------
        buffer = max(cfg.sl_buffer_atr * atr15, entry * cfg.sl_buffer_pct)
        sl = sweep.extreme - buffer if is_long else sweep.extreme + buffer
        risk = entry - sl if is_long else sl - entry
        if risk <= 0:
            return "Entry за стопом — сетап некорректен"
        if (is_long and last_close <= sl) or (not is_long and last_close >= sl):
            return "Цена уже пробила уровень стопа"

        # --- Take Profit: противоположная ликвидность / Range 1.0 ------------------
        targets = self._tp_candidates(side, entry, analyses)
        tp1 = tp2 = None
        tp_label = ""
        best_rr = 0.0
        for i, (price, label) in enumerate(targets):
            reward = price - entry if is_long else entry - price
            rr = reward / risk
            best_rr = max(best_rr, rr)
            if rr >= cfg.min_rr and tp1 is None:
                tp1, tp_label = price, label
                if i + 1 < len(targets):
                    tp2 = targets[i + 1][0]
                break
        if tp1 is None:
            return ("rr", best_rr)
        if (is_long and last_close >= tp1) or (not is_long and last_close <= tp1):
            return "Цена уже достигла TP1"
        rr = (tp1 - entry) / risk if is_long else (entry - tp1) / risk

        # --- Скоринг Q_total -----------------------------------------------------------
        score = self._score(side, poi, sweep, analyses, d1_side, confirmed)
        q = score.total
        if q >= cfg.high_quality:
            risk_level, pos_risk, label = "LOW", cfg.base_position_risk_pct, "Высокая вероятность (Идеальный вход)"
        else:
            risk_level, pos_risk, label = "MEDIUM", cfg.reduced_position_risk_pct, "Умеренный сетап (риск позиции −50%)"

        lop = self._poi_lop(poi, analyses, side)
        direction = "LONG" if is_long else "SHORT"
        sync = f"D1-{d1.bias.value} / H4-{h4.bias.value} / M15-{confirm_label}"
        reason = self._reason(poi, sweep, sweep_tf, confirm_label, lop.ratio if lop else None, d1, h4, rr, entry_src, tp_label)
        p = _round_price(entry)
        verdict = (
            f"{direction} @ {p} | SL: {_round_price(sl)} | TP: {_round_price(tp1)} | "
            f"RR: {rr:.2f} | Quality: {q:.0f}% ({label})"
        )

        cand = SignalCandidate(
            signal_id=f"{asset.ticker}:{direction}:{sweep.ts}:{poi.tf.value}:{poi.kind}",
            ticker=asset.ticker,
            market=asset.market.value,
            direction=direction,
            quality_score=round(q, 1),
            risk_level=risk_level,
            timeframe_sync=sync,
            entry_price=_round_price(entry),
            stop_loss=_round_price(sl),
            take_profit_1=_round_price(tp1),
            take_profit_2=_round_price(tp2) if tp2 is not None else None,
            risk_reward_ratio=round(rr, 2),
            suggested_position_risk_percent=pos_risk,
            reason=reason,
            entry_type=entry_type,
            poi={"timeframe": poi.tf.value, "type": poi.kind, "top": _round_price(poi.top),
                 "bottom": _round_price(poi.bottom), "ce": _round_price(poi.mid)},
            sweep={"timeframe": sweep_tf.value, "source": sweep.source, "level": _round_price(sweep.level),
                   "extreme": _round_price(sweep.extreme), "ts": sweep.ts},
            amd_phase=m15.amd_phase,
            law_of_power_ratio=lop.ratio if lop else None,
            score_breakdown=score,
            generated_ts=m15.last_ts,
            verdict=verdict,
        )
        return ("ok", cand)

    # ------------------------------------------------------------------ #
    def _pick_entry(
        self, side: Side, poi: Poi, sweep: LiquiditySweep, m15: AnalysisResult
    ) -> tuple[float, str, str]:
        """
        Консервативный вход: уровень 0.5 (или верхняя граница) LTF Order Block, возникшего после свипа;
        иначе CE свежего LTF FVG; иначе 0.5 старшей POI.
        """
        is_long = side is Side.BULL
        for ob in sorted(m15.order_blocks, key=lambda o: -o.index):
            if ob.side is side and ob.status != "INVALIDATED" and ob.ts >= sweep.ts - M15_MS:
                if self.cfg.ltf_entry_level == "edge":
                    return (ob.top if is_long else ob.bottom), "LIMIT", "верхняя граница LTF OB" if is_long else "нижняя граница LTF OB"
                return ob.mid, "LIMIT", "0.5 LTF Order Block"
        for fvg in sorted(m15.fvgs, key=lambda f: -f.index):
            if fvg.side is side and fvg.status != "FILLED" and fvg.ts >= sweep.ts:
                return fvg.ce, "LIMIT", "CE (0.5) LTF FVG"
        return poi.mid, "LIMIT", f"0.5 {poi.kind} {poi.tf.value}"

    def _tp_candidates(
        self, side: Side, entry: float, analyses: Mapping[Timeframe, AnalysisResult]
    ) -> list[tuple[float, str]]:
        """Цели: несъеденные EQH (лонг) / EQL (шорт) и Range High/Low старших ТФ, по возрастанию дистанции."""
        is_long = side is Side.BULL
        out: list[tuple[float, str]] = []
        for tf in (Timeframe.H1, Timeframe.H4, Timeframe.D1):
            a = analyses[tf]
            for eq in a.equal_levels:
                if eq.swept:
                    continue
                if is_long and eq.kind == "EQH" and eq.level > entry:
                    out.append((eq.level, f"EQH {tf.value}"))
                if not is_long and eq.kind == "EQL" and eq.level < entry:
                    out.append((eq.level, f"EQL {tf.value}"))
            if tf in (Timeframe.H4, Timeframe.D1) and a.range:
                lvl = a.range.high if is_long else a.range.low
                if (is_long and lvl > entry) or (not is_long and lvl < entry):
                    out.append((lvl, f"Range {'High' if is_long else 'Low'} {tf.value}"))
        out.sort(key=lambda t: abs(t[0] - entry))
        dedup: list[tuple[float, str]] = []
        for price, label in out:
            if dedup and abs(price - dedup[-1][0]) <= entry * 0.0005:
                continue
            dedup.append((price, label))
        return dedup

    # ------------------------------------------------------------------ #
    # Скоринг
    # ------------------------------------------------------------------ #
    def _poi_lop(self, poi: Poi, analyses: Mapping[Timeframe, AnalysisResult], side: Side):
        """Закон Силы на ТФ POI (fallback — H4). Импульс должен быть по направлению сделки."""
        for tf in (poi.tf, Timeframe.H4):
            lop = analyses[tf].law_of_power
            if lop is not None and lop.side is side:
                return lop
        return None

    def _score(
        self,
        side: Side,
        poi: Poi,
        sweep: LiquiditySweep,
        analyses: Mapping[Timeframe, AnalysisResult],
        d1_side: Side | None,
        confirmed: bool,
    ) -> ScoreBreakdown:
        cfg = self.cfg

        # 1. HTF Sync: H4 уже совпал со стороной; D1 совпал = 100, D1 нейтрален = 60 (контртренд отсеян ранее)
        s_htf = 100.0 if d1_side is side else 60.0

        # 2. Liquidity Sweep
        s_sweep = SWEEP_SCORES.get(sweep.source, 0.0)

        # 3. Imbalance/FVG: лучший из (FVG, породивший OB | сама FVG-POI | свежая LTF FVG после свипа)
        def fvg_pts(f: FVG | None) -> float:
            if f is None:
                return 0.0
            return {"FRESH": 100.0, "PARTIAL": 50.0}.get(f.status, 0.0)

        candidates = [fvg_pts(poi.ob.fvg if poi.ob else None), fvg_pts(poi.fvg)]
        for tf in (Timeframe.M15, Timeframe.H1):
            for f in analyses[tf].fvgs:
                if f.side is side and f.ts >= sweep.ts:
                    candidates.append(fvg_pts(f))
        s_fvg = max(candidates)

        # 4. Law of Power
        lop = self._poi_lop(poi, analyses, side)
        if lop is None or lop.ratio < 1.0:
            s_lop = 0.0
        elif lop.ratio < 1.5:
            s_lop = 60.0
        else:
            s_lop = 100.0

        # 5. LTF подтверждение
        s_ltf = 100.0 if confirmed else 0.0

        total = (
            cfg.w_htf_sync * s_htf + cfg.w_sweep * s_sweep + cfg.w_fvg * s_fvg
            + cfg.w_law_of_power * s_lop + cfg.w_ltf_confirm * s_ltf
        )
        return ScoreBreakdown(s_htf, s_sweep, s_fvg, s_lop, s_ltf, round(total, 2))

    # ------------------------------------------------------------------ #
    # Текст
    # ------------------------------------------------------------------ #
    def _reason(
        self, poi: Poi, sweep: LiquiditySweep, sweep_tf: Timeframe, confirm: str,
        ratio: float | None, d1: AnalysisResult, h4: AnalysisResult, rr: float, entry_src: str, tp_label: str,
    ) -> str:
        zone = "Order Block" if poi.kind == "OB" else "FVG"
        parts = [
            f"Тест зоны {zone} на {poi.tf.value} ({_round_price(poi.bottom)}–{_round_price(poi.top)}) "
            f"после съёма {SWEEP_RU.get(sweep.source, 'ликвидности')} ({sweep_tf.value}).",
        ]
        if confirm == "BOS_Confirmed":
            parts.append("Подтверждён BOS на M15.")
        elif confirm == "SC_Confirmed":
            parts.append("Подтверждение: Sponsored Candle на M15.")
        else:
            parts.append("Подтверждения на M15 пока нет — вход лимитным ордером.")
        if ratio is not None:
            if ratio >= 1.0:
                parts.append(f"Коррекция заняла в {ratio:.1f} раз больше баров, чем импульс.")
            else:
                parts.append(f"Коррекция короче импульса (ratio {ratio:.1f}) — слабый сигнал силы.")
        parts.append(f"HTF: D1 {d1.bias.value}, H4 {h4.bias.value}. Вход: {entry_src}. Цель: {tp_label}, RR 1:{rr:.1f}.")
        return " ".join(parts)


def _round_price(p: float) -> float:
    """Адаптивное округление: чем дешевле актив, тем больше знаков."""
    if p >= 1000:
        return round(p, 2)
    if p >= 1:
        return round(p, 4)
    return round(p, 6)
