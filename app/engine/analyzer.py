"""
MarketAnalyzer — Smart Money движок.

По массиву свечей одного таймфрейма вычисляет:
    1. Свинги (правило 3 свечей)                       -> find_swings()
    2. Структуру рынка, BOS / BMS, свипы (SFP)          -> build_structure()
    3. FVG (Imbalance) и уровень 0.5 (CE)               -> find_fvgs()
    4. Order Block (OB + FVG + поглощение + BOS)        -> find_order_blocks()
    5. EQH / EQL (пулы ликвидности)                     -> find_equal_levels()
    6. Sponsored Candle (SC)                            -> find_sponsored_candles()
    7. Range (0.0 / 0.5 / 1.0) и Deviation              -> find_range()
    8. Закон Силы (Law of Power)                        -> compute_law_of_power()
    9. Фаза AMD / PO3                                   -> detect_amd_phase()

Модуль не имеет внешних зависимостей и детерминирован: одинаковый вход -> одинаковый выход.
Анализ работает ТОЛЬКО по закрытым свечам (ответственность вызывающего кода).
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Sequence

from app.config import EngineConfig
from app.engine.types import (
    FVG,
    AnalysisResult,
    Bias,
    Candle,
    EqualLevels,
    LawOfPower,
    LiquiditySweep,
    OrderBlock,
    RangeInfo,
    Side,
    SponsoredCandle,
    StructureBreak,
    Swing,
)

MIN_CANDLES = 30


class MarketAnalyzer:
    def __init__(
        self,
        candles: Sequence[Candle],
        timeframe: str = "H1",
        config: EngineConfig | None = None,
    ) -> None:
        self.c: list[Candle] = list(candles)
        self.n: int = len(self.c)
        self.tf = timeframe
        self.cfg = config or EngineConfig()

        self.atr_series: list[float] = self._compute_atr()
        self.swings: list[Swing] = []
        self.major_swings: list[Swing] = []
        self.breaks: list[StructureBreak] = []
        self.sweeps: list[LiquiditySweep] = []
        self.fvgs: list[FVG] = []
        self.order_blocks: list[OrderBlock] = []
        self.sponsored: list[SponsoredCandle] = []
        self.equal_levels: list[EqualLevels] = []
        self.range: RangeInfo | None = None
        self.law_of_power: LawOfPower | None = None
        self.trend: Side | None = None
        self.amd_phase: str = "UNDEFINED"

    # ------------------------------------------------------------------ #
    # Публичный вход
    # ------------------------------------------------------------------ #
    def analyze(self) -> AnalysisResult:
        """Запускает полный конвейер анализа и возвращает AnalysisResult."""
        if self.n < MIN_CANDLES:
            return self._result(Bias.NEUTRAL)

        self.find_swings()
        self.major_swings = self._zigzag()
        self.build_structure()
        self.find_fvgs()
        self.find_order_blocks()
        self.find_equal_levels()      # также достраивает список свипов (EQH/EQL)
        self.find_sponsored_candles()
        self.find_range()
        bias = self.determine_bias()
        self.compute_law_of_power(bias)
        self.detect_amd_phase()
        return self._result(bias)

    # ------------------------------------------------------------------ #
    # Вспомогательное
    # ------------------------------------------------------------------ #
    def _compute_atr(self) -> list[float]:
        """ATR (простая средняя TR за N свечей; для первых свечей — расширяющееся окно)."""
        period = self.cfg.atr_period
        tr: list[float] = []
        for i, k in enumerate(self.c):
            if i == 0:
                tr.append(k.high - k.low)
            else:
                pc = self.c[i - 1].close
                tr.append(max(k.high - k.low, abs(k.high - pc), abs(k.low - pc)))
        out: list[float] = []
        running = 0.0
        for i, t in enumerate(tr):
            running += t
            if i >= period:
                running -= tr[i - period]
            out.append(running / min(i + 1, period))
        return out

    def _result(self, bias: Bias) -> AnalysisResult:
        last = self.c[-1] if self.c else None
        return AnalysisResult(
            timeframe=self.tf,
            bias=bias,
            last_index=self.n - 1,
            last_ts=last.ts if last else 0,
            last_close=last.close if last else 0.0,
            atr=self.atr_series[-1] if self.atr_series else 0.0,
            swings=self.swings,
            major_swings=self.major_swings,
            breaks=self.breaks,
            sweeps=self.sweeps,
            fvgs=self.fvgs,
            order_blocks=self.order_blocks,
            sponsored=self.sponsored,
            equal_levels=self.equal_levels,
            range=self.range,
            law_of_power=self.law_of_power,
            amd_phase=self.amd_phase,
            candles=self.c,
        )

    # ------------------------------------------------------------------ #
    # 1. Свинги
    # ------------------------------------------------------------------ #
    def find_swings(self) -> list[Swing]:
        """
        Swing High / Swing Low по правилу 3 свечей (см. ТЗ):
          High: центральная свеча имеет максимальный high среди трёх, а 3-я свеча
                закрывается ниже ОТКРЫТИЯ 2-й свечи.
          Low:  зеркально — 3-я свеча закрывается выше открытия 2-й.
        Свинги нормализуются: два одноимённых подряд сворачиваются в более экстремальный,
        поэтому последовательность всегда чередуется High/Low.
        Свинг i подтверждается закрытием свечи i+1 (не используем будущее).
        """
        c, strict = self.c, self.cfg.strict_swing_rule
        raw: list[Swing] = []
        for i in range(1, self.n - 1):
            p, k, nx = c[i - 1], c[i], c[i + 1]
            is_high = k.high > p.high and k.high >= nx.high and (not strict or nx.close < k.open)
            is_low = k.low < p.low and k.low <= nx.low and (not strict or nx.close > k.open)
            if is_high and is_low:
                continue
            if is_high:
                raw.append(Swing(i, k.ts, k.high, "HIGH"))
            elif is_low:
                raw.append(Swing(i, k.ts, k.low, "LOW"))

        merged: list[Swing] = []
        for s in raw:
            if merged and merged[-1].kind == s.kind:
                better = s.price > merged[-1].price if s.kind == "HIGH" else s.price < merged[-1].price
                if better:
                    merged[-1] = s
                continue
            merged.append(s)

        last_high: float | None = None
        last_low: float | None = None
        for s in merged:
            if s.kind == "HIGH":
                if last_high is not None:
                    s.label = "HH" if s.price > last_high else "LH"
                last_high = s.price
            else:
                if last_low is not None:
                    s.label = "HL" if s.price > last_low else "LL"
                last_low = s.price
        self.swings = merged
        return merged

    def _zigzag(self) -> list[Swing]:
        """Фильтр значимых свингов: нога между свингами >= major_leg_atr * ATR (для Range и Law of Power)."""
        out: list[Swing] = []
        for s in self.swings:
            if not out:
                out.append(s)
                continue
            last = out[-1]
            if s.kind == last.kind:
                if (s.kind == "HIGH" and s.price > last.price) or (s.kind == "LOW" and s.price < last.price):
                    out[-1] = s
                continue
            if abs(s.price - last.price) >= self.cfg.major_leg_atr * self.atr_series[s.index]:
                out.append(s)
        return out

    # ------------------------------------------------------------------ #
    # 2. Структура рынка: BOS / BMS / свипы
    # ------------------------------------------------------------------ #
    def build_structure(self) -> None:
        """
        Один проход по свечам:
          * close (тело) за последним свингом  -> BOS (по тренду) или BMS (против тренда);
          * только тень за свингом, close внутри -> Liquidity Sweep (SFP), НЕ слом структуры.
        Каждый свинг учитывается один раз (пробит или снят).
        """
        c = self.c
        ptr = 0
        act_high: Swing | None = None
        act_low: Swing | None = None
        swept: set[tuple[str, int]] = set()
        trend: Side | None = None

        for i in range(self.n):
            while ptr < len(self.swings) and self.swings[ptr].index + 1 <= i:
                s = self.swings[ptr]
                ptr += 1
                if s.kind == "HIGH":
                    act_high = s
                else:
                    act_low = s
            k = c[i]

            if act_high is not None:
                if k.close > act_high.price:
                    kind = "BMS" if trend is Side.BEAR else "BOS"
                    self.breaks.append(StructureBreak(i, k.ts, kind, Side.BULL, act_high.price, act_high.index))
                    trend = Side.BULL
                    act_high = None
                elif k.high > act_high.price and ("HIGH", act_high.index) not in swept:
                    swept.add(("HIGH", act_high.index))
                    self.sweeps.append(
                        LiquiditySweep(i, k.ts, Side.BEAR, act_high.price, k.high, "SWING", k.close)
                    )

            if act_low is not None:
                if k.close < act_low.price:
                    kind = "BMS" if trend is Side.BULL else "BOS"
                    self.breaks.append(StructureBreak(i, k.ts, kind, Side.BEAR, act_low.price, act_low.index))
                    trend = Side.BEAR
                    act_low = None
                elif k.low < act_low.price and ("LOW", act_low.index) not in swept:
                    swept.add(("LOW", act_low.index))
                    self.sweeps.append(
                        LiquiditySweep(i, k.ts, Side.BULL, act_low.price, k.low, "SWING", k.close)
                    )
        self.trend = trend

    def determine_bias(self) -> Bias:
        """
        Bias = направление последнего слома структуры (BOS/BMS).
        Если сломов ещё не было — смотрим на последовательность значимых свингов (HH+HL / LH+LL).
        """
        if self.trend is not None:
            return Bias.from_side(self.trend)
        highs = [s for s in self.major_swings if s.kind == "HIGH"][-2:]
        lows = [s for s in self.major_swings if s.kind == "LOW"][-2:]
        if len(highs) == 2 and len(lows) == 2:
            if highs[1].price > highs[0].price and lows[1].price > lows[0].price:
                return Bias.BULLISH
            if highs[1].price < highs[0].price and lows[1].price < lows[0].price:
                return Bias.BEARISH
        return Bias.NEUTRAL

    # ------------------------------------------------------------------ #
    # 3. FVG
    # ------------------------------------------------------------------ #
    def find_fvgs(self) -> list[FVG]:
        """
        3-свечной имбаланс. Свеча 2 — «большая» (range >= fvg_displacement_atr * ATR) и направлена по импульсу;
        между high свечи 1 и low свечи 3 (для бычьего) остаётся гэп. Рассчитывается CE = 0.5 гэпа.
        Статус заполнения: FRESH (не тронут) / PARTIAL (протестирован) / FILLED (закрыт тенью полностью).
        """
        c, cfg = self.c, self.cfg
        for i in range(2, self.n):
            c1, c2, c3 = c[i - 2], c[i - 1], c[i]
            atr = self.atr_series[i - 1]
            if atr <= 0 or c2.range < cfg.fvg_displacement_atr * atr:
                continue
            min_gap = cfg.fvg_min_gap_atr * atr
            if c2.is_bull and c3.low - c1.high > min_gap:
                fvg = FVG(i, i - 1, c3.ts, Side.BULL, top=c3.low, bottom=c1.high, ce=(c3.low + c1.high) / 2)
            elif c2.is_bear and c1.low - c3.high > min_gap:
                fvg = FVG(i, i - 1, c3.ts, Side.BEAR, top=c1.low, bottom=c3.high, ce=(c1.low + c3.high) / 2)
            else:
                continue
            self._update_fvg_fill(fvg)
            self.fvgs.append(fvg)
        return self.fvgs

    def _update_fvg_fill(self, fvg: FVG) -> None:
        size = fvg.top - fvg.bottom
        if size <= 0:
            return
        extreme: float | None = None
        for j in range(fvg.index + 1, self.n):
            k = self.c[j]
            if fvg.side is Side.BULL:
                if k.low <= fvg.top:
                    if fvg.first_touch_index is None:
                        fvg.first_touch_index = j
                    extreme = k.low if extreme is None else min(extreme, k.low)
                    if k.low <= fvg.bottom:
                        fvg.status, fvg.fill_ratio = "FILLED", 1.0
                        return
            else:
                if k.high >= fvg.bottom:
                    if fvg.first_touch_index is None:
                        fvg.first_touch_index = j
                    extreme = k.high if extreme is None else max(extreme, k.high)
                    if k.high >= fvg.top:
                        fvg.status, fvg.fill_ratio = "FILLED", 1.0
                        return
        if extreme is not None:
            ratio = (fvg.top - extreme) / size if fvg.side is Side.BULL else (extreme - fvg.bottom) / size
            fvg.fill_ratio = max(0.0, min(1.0, ratio))
            fvg.status = "PARTIAL"

    # ------------------------------------------------------------------ #
    # 4. Order Block
    # ------------------------------------------------------------------ #
    def find_order_blocks(self) -> list[OrderBlock]:
        """
        OB = последняя противоположная свеча перед агрессивным импульсом, который:
          (a) поглощает её (закрытие импульса за экстремумом OB),
          (b) оставляет FVG,
          (c) ломает структуру (BOS/BMS в том же направлении),
          (d) идёт на «чистом объёме» (если данные по объёму есть).
        Зона OB = диапазон свечи (high..low), уровень 0.5 — mid.
        Статус: FRESH (не тестировался) / TESTED / INVALIDATED (закрытие телом за дальней границей).
        """
        c, cfg = self.c, self.cfg
        seen: set[tuple[int, Side]] = set()
        for fvg in self.fvgs:
            m, side = fvg.impulse_index, fvg.side
            j = None
            for jj in range(m - 1, max(-1, m - 1 - cfg.ob_lookback), -1):
                cd = c[jj]
                if (side is Side.BULL and cd.is_bear) or (side is Side.BEAR and cd.is_bull):
                    j = jj
                    break
            if j is None or (j, side) in seen:
                continue

            imp, ob = c[m], c[j]
            engulf = imp.close > ob.high if side is Side.BULL else imp.close < ob.low
            if not engulf:
                continue
            bos = next(
                (b for b in self.breaks if b.side is side and m <= b.index <= fvg.index + cfg.ob_bos_window),
                None,
            )
            if bos is None:
                continue
            volume_ok = self._volume_ok(m)
            if cfg.require_volume and not volume_ok:
                continue

            block = OrderBlock(
                index=j,
                ts=ob.ts,
                side=side,
                top=ob.high,
                bottom=ob.low,
                mid=(ob.high + ob.low) / 2,
                impulse_index=m,
                bos_index=bos.index,
                fvg=fvg,
                volume_ok=volume_ok,
            )
            self._update_ob_status(block)
            self.order_blocks.append(block)
            seen.add((j, side))
        self.order_blocks.sort(key=lambda o: o.index)
        return self.order_blocks

    def _volume_ok(self, m: int) -> bool:
        """Объём импульсной свечи >= volume_mult * средний объём за 20 предыдущих. Нет данных -> True."""
        vols = [self.c[k].volume for k in range(max(0, m - 20), m)]
        avg = sum(vols) / len(vols) if vols else 0.0
        if avg <= 0:
            return True
        return self.c[m].volume >= self.cfg.volume_mult * avg

    def _update_ob_status(self, ob: OrderBlock) -> None:
        for k in range(ob.impulse_index + 1, self.n):
            cd = self.c[k]
            if ob.side is Side.BULL:
                if cd.close < ob.bottom:
                    ob.status = "INVALIDATED"
                    return
                touched = cd.low <= ob.top
            else:
                if cd.close > ob.top:
                    ob.status = "INVALIDATED"
                    return
                touched = cd.high >= ob.bottom
            if touched:
                if ob.first_touch_index is None:
                    ob.first_touch_index = k
                ob.last_touch_index = k
                ob.status = "TESTED"

    # ------------------------------------------------------------------ #
    # 5. EQH / EQL
    # ------------------------------------------------------------------ #
    def find_equal_levels(self) -> list[EqualLevels]:
        """
        Кластеры равных максимумов/минимумов (где копятся стопы ритейла).
        Допуск равенства = eq_tolerance_atr * ATR. Из кластера исключаются члены, чью ликвидность
        цена уже забрала до формирования последнего члена.
        Для каждого кластера определяется съём: тень за уровень без закрытия = SFP (свип),
        закрытие телом = пробой. Свипы EQ добавляются в общий список self.sweeps.
        """
        c, cfg = self.c, self.cfg
        tol = cfg.eq_tolerance_atr * self.atr_series[-1]
        for kind in ("HIGH", "LOW"):
            pts = [s for s in self.swings if s.kind == kind]
            clusters: list[list[Swing]] = []
            for s in pts:
                for cl in clusters:
                    mean = sum(x.price for x in cl) / len(cl)
                    if abs(s.price - mean) <= tol:
                        cl.append(s)
                        break
                else:
                    clusters.append([s])

            for cl in clusters:
                if len(cl) < 2:
                    continue
                cl = sorted(cl, key=lambda s: s.index)
                newest = cl[-1]
                valid = [newest]
                for m in reversed(cl[:-1]):
                    window = c[m.index + 1 : newest.index]
                    if kind == "HIGH":
                        taken = any(k.high > m.price + tol for k in window)
                    else:
                        taken = any(k.low < m.price - tol for k in window)
                    if taken:
                        break
                    valid.append(m)
                valid.sort(key=lambda s: s.index)
                if len(valid) < 2 or valid[-1].index - valid[0].index < cfg.eq_min_separation:
                    continue

                prices = [s.price for s in valid]
                level = max(prices) if kind == "HIGH" else min(prices)
                eq = EqualLevels(
                    kind="EQH" if kind == "HIGH" else "EQL",
                    level=level,
                    indices=[s.index for s in valid],
                    count=len(valid),
                )
                for k in range(valid[-1].index + 1, self.n):
                    cd = c[k]
                    if kind == "HIGH" and cd.high > level:
                        eq.swept, eq.swept_index, eq.swept_by_close = True, k, cd.close > level
                        break
                    if kind == "LOW" and cd.low < level:
                        eq.swept, eq.swept_index, eq.swept_by_close = True, k, cd.close < level
                        break
                self.equal_levels.append(eq)

        # Свипы EQ -> общий список свипов (апгрейд источника, если свеча уже учтена как SWING)
        for eq in self.equal_levels:
            if not eq.swept or eq.swept_by_close or eq.swept_index is None:
                continue
            side = Side.BEAR if eq.kind == "EQH" else Side.BULL
            k = c[eq.swept_index]
            existing = next((s for s in self.sweeps if s.index == eq.swept_index and s.side is side), None)
            if existing:
                existing.source, existing.level = eq.kind, eq.level
            else:
                extreme = k.high if side is Side.BEAR else k.low
                self.sweeps.append(LiquiditySweep(eq.swept_index, k.ts, side, eq.level, extreme, eq.kind, k.close))
        self.sweeps.sort(key=lambda s: s.index)
        self.equal_levels.sort(key=lambda e: e.indices[-1])
        return self.equal_levels

    # ------------------------------------------------------------------ #
    # 6. Sponsored Candle
    # ------------------------------------------------------------------ #
    def find_sponsored_candles(self) -> list[SponsoredCandle]:
        """
        SC = манипулятивная свеча, которая (1) снимает ликвидность, (2) поглощает предыдущую свечу
        в обратном направлении и (3) оставляет FVG. Зона SC уточняется до ТЕЛА свечи (без теней).
        """
        for sw in self.sweeps:
            k = sw.index
            if k < 1:
                continue
            cd, prev = self.c[k], self.c[k - 1]
            if sw.side is Side.BULL:
                if not cd.is_bull or cd.close <= prev.body_high:
                    continue
            else:
                if not cd.is_bear or cd.close >= prev.body_low:
                    continue
            if not any(f.impulse_index == k and f.side is sw.side for f in self.fvgs):
                continue
            self.sponsored.append(SponsoredCandle(k, cd.ts, sw.side, cd.body_high, cd.body_low, sw.level))
        return self.sponsored

    # ------------------------------------------------------------------ #
    # 7. Range
    # ------------------------------------------------------------------ #
    def find_range(self) -> RangeInfo | None:
        """
        Рейндж = последняя пара значимых свингов (Range High 1.0 / Range Low 0.0), equilibrium 0.5.
        Валиден, если: прошло >= range_min_bars, цена внутри, и была реакция на 0.5
        (свеча коснулась 0.5 и закрылась с той же стороны, с которой пришла).
        Deviation = тень за границу рейнджа без закрытия (граница сформирована ложным пробоем).
        Свипы, совпавшие с границей ВАЛИДНОГО рейнджа, получают источник "RANGE".
        """
        highs = [s for s in self.major_swings if s.kind == "HIGH"]
        lows = [s for s in self.major_swings if s.kind == "LOW"]
        if not highs or not lows:
            return None
        h, l = highs[-1], lows[-1]
        if h.price <= l.price:
            return None

        eq = (h.price + l.price) / 2
        start, later = min(h.index, l.index), max(h.index, l.index)
        reactions = 0
        for k in range(later + 1, self.n):
            cd = self.c[k]
            if cd.low <= eq <= cd.high and ((cd.open > eq and cd.close > eq) or (cd.open < eq and cd.close < eq)):
                reactions += 1

        close = self.c[-1].close
        inside = l.price <= close <= h.price
        valid = inside and reactions >= 1 and (self.n - 1 - start) >= self.cfg.range_min_bars

        atr = self.atr_series[-1]
        dev_high = any(s.side is Side.BEAR and s.index >= start and s.extreme >= h.price - 0.1 * atr for s in self.sweeps)
        dev_low = any(s.side is Side.BULL and s.index >= start and s.extreme <= l.price + 0.1 * atr for s in self.sweeps)

        band = 0.05 * (h.price - l.price)
        position = "PREMIUM" if close > eq + band else "DISCOUNT" if close < eq - band else "EQUILIBRIUM"
        self.range = RangeInfo(
            high=h.price, low=l.price, eq=eq, high_index=h.index, low_index=l.index,
            eq_reactions=reactions, valid=valid, deviation_high=dev_high, deviation_low=dev_low,
            position=position,
        )

        if valid:
            for s in self.sweeps:
                if s.source != "SWING" or s.index < start:
                    continue
                if (s.side is Side.BEAR and s.extreme >= h.price - 0.1 * atr) or (
                    s.side is Side.BULL and s.extreme <= l.price + 0.1 * atr
                ):
                    s.source = "RANGE"
        return self.range

    # ------------------------------------------------------------------ #
    # 8. Закон Силы
    # ------------------------------------------------------------------ #
    def compute_law_of_power(self, bias: Bias) -> LawOfPower | None:
        """
        Law of Power: N_imp — число баров последней импульсной ноги по тренду,
        N_corr — число баров коррекции после неё (до текущей свечи, если коррекция идёт).
        Здоровый тренд: N_corr > N_imp (ratio = N_corr / N_imp).
        Ноги берутся по значимым свингам (ZigZag), чтобы не считать микрошум.
        """
        side = bias.to_side()
        ms = self.major_swings
        if side is None or len(ms) < 3:
            return None
        start_kind, end_kind = ("LOW", "HIGH") if side is Side.BULL else ("HIGH", "LOW")
        last = ms[-1]
        if last.kind == end_kind:               # импульс закончился на last, коррекция идёт
            start, end = ms[-2], last
            corr_end, ongoing = self.n - 1, True
        else:                                    # коррекция уже завершилась свингом last
            start, end = ms[-3], ms[-2]
            corr_end, ongoing = last.index, False
        if start.kind != start_kind or end.kind != end_kind:
            return None
        if (side is Side.BULL and end.price <= start.price) or (side is Side.BEAR and end.price >= start.price):
            return None

        n_imp = max(1, end.index - start.index)
        n_corr = max(0, corr_end - end.index)
        self.law_of_power = LawOfPower(
            side=side,
            impulse_bars=n_imp,
            correction_bars=n_corr,
            ratio=round(n_corr / n_imp, 4),
            impulse_start=start.index,
            impulse_end=end.index,
            correction_end=corr_end,
            correction_ongoing=ongoing,
        )
        return self.law_of_power

    # ------------------------------------------------------------------ #
    # 9. AMD / PO3
    # ------------------------------------------------------------------ #
    def detect_amd_phase(self) -> str:
        """
        DISTRIBUTION  — после свипа последовал слом структуры в сторону, обратную свипу (недавно);
        MANIPULATION  — свип произошёл недавно, подтверждающего слома ещё нет;
        ACCUMULATION  — валидный рейндж, цена внутри, свежих свипов нет;
        иначе UNDEFINED.
        """
        n1 = self.n - 1
        last_sweep = self.sweeps[-1] if self.sweeps else None
        last_break = self.breaks[-1] if self.breaks else None
        if (
            last_sweep and last_break
            and last_break.index > last_sweep.index
            and last_break.side is last_sweep.side
            and n1 - last_break.index <= 30
        ):
            self.amd_phase = "DISTRIBUTION"
        elif last_sweep and n1 - last_sweep.index <= 10:
            self.amd_phase = "MANIPULATION"
        elif self.range and self.range.valid:
            self.amd_phase = "ACCUMULATION"
        else:
            self.amd_phase = "UNDEFINED"
        return self.amd_phase


# ---------------------------------------------------------------------- #
# Сериализация для API / фронтенда (оверлеи графика)
# ---------------------------------------------------------------------- #
def analysis_to_dict(result: AnalysisResult) -> dict[str, Any]:
    """AnalysisResult -> dict (без массива свечей); Enum сохраняются как str-подклассы и сериализуются в JSON."""
    data = asdict(result)
    data.pop("candles", None)
    return data
