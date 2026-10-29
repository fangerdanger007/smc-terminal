# SMC Trading Terminal — Backend (FastAPI)

Автоматический сканер рынка по Smart Money стратегии: тянет свечи (D1/H4/H1/M15), находит сетапы
и отдаёт готовые сигналы с Entry / SL / TP, RR и скорингом качества `Q_total`.

## Быстрый старт

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # при необходимости добавьте ключи Alpaca
uvicorn app.main:app --port 8000
# Swagger: http://localhost:8000/docs
pytest -q                       # или: python -m tests.test_engine
```

Docker: `cp .env.example .env && docker compose up --build -d`

> Запускайте **один** процесс uvicorn (`--workers 1`): фоновый сканер и хранилище сигналов — в памяти процесса.

## Структура

```
app/
  config.py            # Market/Timeframe, реестр тикеров (ASSETS), EngineConfig (все пороги и веса), Settings
  main.py              # FastAPI, lifespan (старт/стоп сканера), эндпоинты
  schemas.py           # Pydantic-схемы ответов (формат Signal из ТЗ + расширения)
  scanner.py           # ScannerWorker (фоновый цикл) + SignalStore
  data/
    providers.py       # Binance, Bybit, Yahoo, Alpaca (async, httpx)
    aggregator.py      # фолбэк между источниками, ретраи, кэш с TTL, только закрытые свечи
    utils.py           # resample H1->H4, drop_unclosed, dedupe
  engine/              # чистый Python, без внешних зависимостей
    types.py           # dataclass: Candle, Swing, FVG, OrderBlock, ...
    analyzer.py        # MarketAnalyzer (swings, BOS/BMS, sweeps, FVG, OB, EQH/EQL, SC, Range, Law of Power, AMD)
    signals.py         # SignalEngine: POI + sweep + confirm -> Entry/SL/TP/RR -> Q_total
    pipeline.py        # свечи -> анализ по ТФ -> оценка
tests/                 # 17 тестов
```

## API

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/v1/signals/active?market=&direction=&min_quality=&limit=` | Активные сигналы (массив JSON), по убыванию качества |
| GET | `/api/v1/signals/{ticker}` | Сигнал по тикеру из кэша сканера |
| GET | `/api/v1/scan/{ticker}` | Разовый анализ «сейчас» + **причины отказа**, если сетапа нет |
| GET | `/api/v1/analysis/{ticker}?tf=H4` | Оверлеи графика: свинги, BOS/BMS, FVG(+CE), OB, EQH/EQL, Range, Law of Power |
| GET | `/api/v1/candles/{ticker}?tf=H4&limit=500` | Закрытые свечи для Lightweight Charts |
| GET | `/api/v1/tickers` | Список инструментов для поиска |
| GET | `/api/v1/scanner/status` · POST `/api/v1/scanner/run` | Состояние и внеочередной запуск сканера |

Пример формата ответа `/api/v1/signals/active` (значения иллюстративные; поля из ТЗ + расширение `signal_id`, `take_profit_2`, `entry_type`,
`verdict`, `amd_phase`, `poi`, `sweep`, `score_breakdown`, `created_at`):

```json
{
  "ticker": "XAUUSD", "market": "COMMODITIES", "direction": "LONG",
  "quality_score": 88.0, "risk_level": "LOW",
  "timeframe_sync": "D1-Bullish / H4-Bullish / M15-BOS_Confirmed",
  "entry_price": 2742.5, "stop_loss": 2730.73, "take_profit_1": 2790.0,
  "risk_reward_ratio": 4.03, "suggested_position_risk_percent": 1.0,
  "reason": "Тест зоны Order Block на H4 ... Коррекция заняла в 1.8 раз больше баров, чем импульс."
}
```

## Логика сигнала (кратко)

1. **Направление:** bias H4 (последний BOS/BMS). D1 против — отказ (контртренд).
2. **POI:** бычьи/медвежьи OB и FVG на D1/H4/H1, которые цена тестирует (последние 32 M15 бара), OB — «свежие».
3. **Sweep:** EQL/EQH, Range Deviation или SFP за последние 12 ч, тень которого дотянулась до POI.
4. **Подтверждение M15:** BOS или Sponsored Candle после свипа.
5. **Entry:** 0.5 LTF OB → CE LTF FVG → 0.5 старшей POI (режим `aggressive` — закрытие M15 внутри POI).
6. **SL:** за тенью свипа + буфер `max(0.1·ATR(M15), 0.01 %)`.
7. **TP1:** ближайшая несъеденная ликвидность (EQH/EQL на H1/H4/D1, Range High/Low на H4/D1), дающая **RR ≥ 3**; TP2 — следующая цель.
8. **Q_total** = 30·HTF + 25·Sweep + 20·FVG + 15·LoP + 10·LTF. `≥80` LOW (риск 1.0 %), `60–80` MEDIUM (риск 0.5 %), `<60` — сигнал отбрасывается.

## Принятые трактовки (где ТЗ допускает варианты) — настраиваются в `EngineConfig` / `signals.py`

- **Bias** таймфрейма = направление последнего слома структуры; нет сломов — последовательность HH/HL либо LH/LL.
- **Range** = последняя пара значимых свингов (ZigZag, нога ≥ 1.5·ATR); валиден при реакции на 0.5 и длительности ≥ 10 баров.
- **HTF Sync:** D1 и H4 по сделке = 100 баллов; D1 нейтрален = 60 (контртренд отсеивается жёстко).
- **Sweep-балл:** EQL/EQH/Range Deviation = 100, обычный SFP за свинг = 70 (в ТЗ только 0/100 — это смягчение).
- **FVG-балл:** лучший из (FVG, породивший OB; сама FVG-POI; свежая LTF FVG после свипа). FVG, породивший OB, к моменту возврата цены обычно уже заполнен, поэтому учитывается и LTF-имбаланс.
- **Law of Power** считается по последней значимой импульсной ноге и коррекции после неё на ТФ POI (запасной — H4).
- **Объём OB** проверяется только если у инструмента есть данные об объёме.

## Ограничения (важно знать)

- **Запросы к биржам не проверялись вживую** (у среды разработки не было сети): провайдеры написаны по документации API,
  движок покрыт тестами на синтетических данных. Перед выкладкой прогоните `GET /api/v1/scan/BTC` и `/api/v1/scan/TSLA`.
- Движок **не бэктестировался** на истории: пороги и веса взяты из ТЗ. Прежде чем торговать по сигналам — прогоните бэктест.
  Это инструмент анализа, а не финансовая рекомендация.
- **Золото/нефть** берутся с Yahoo как фьючерсы (`GC=F`, `CL=F`), цены отличаются от спота XAUUSD. Для спота нужен платный провайдер.
- **H4 по акциям** через Yahoo собирается из H1 по границам UTC (сессия США им не кратна); для точного H4 используйте Alpaca.
- Обновление данных — **опрос по таймеру**, не WebSocket. Для M15 этого достаточно (сигналы строятся по закрытым свечам).
- Хранилище сигналов — in-memory; при перезапуске пересоздаётся при следующем скане. Для продакшена — Redis/Postgres.
- Нет аутентификации API и rate limiting — добавьте API-ключ/прокси перед публикацией в интернет.

## Следующие шаги

WebSocket-стрим сигналов для фронтенда, Binance WS для свежих свечей, бэктест-модуль (hit-rate по Q_total),
Redis-хранилище, уведомления (Telegram/Webhook).
