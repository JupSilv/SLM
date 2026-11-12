import bisect
import logging
import math
from collections import Counter

from app.analyzer import MarketAnalyzer


INTERVALS = (60, 300, 900)


def validate_dataset(data):
    if not isinstance(data, dict) or data.get("format") != "deskubuddy-candles-v1":
        raise ValueError("Formato de dados históricos inválido.")
    if not isinstance(data.get("asset"), str) or not data["asset"]:
        raise ValueError("Ativo histórico ausente.")
    payout = float(data["payout_snapshot_percent"])
    if not math.isfinite(payout) or not 0 < payout <= 100:
        raise ValueError("Payout da coleta inválido.")
    start, end = int(data["start"]), int(data["end"])
    if start >= end:
        raise ValueError("Período histórico inválido.")
    result = {}
    for interval in INTERVALS:
        candles = data["candles"][str(interval)]
        if not isinstance(candles, list) or not candles:
            raise ValueError(f"Candles {interval}s ausentes.")
        values = {}
        for candle in candles:
            timestamp = int(candle["from"])
            if timestamp in values or timestamp % interval:
                raise ValueError(f"Timestamp duplicado ou desalinhado em {interval}s.")
            prices = [float(candle[key]) for key in ("open", "max", "min", "close")]
            if not all(math.isfinite(price) and price > 0 for price in prices):
                raise ValueError(f"Preço inválido em {interval}s.")
            opening, high, low, close = prices
            if low > min(opening, close) or high < max(opening, close):
                raise ValueError(f"OHLC inválido em {interval}s.")
            values[timestamp] = candle
        result[interval] = values
    return start, end, payout, result


class HistoricalBroker:
    def __init__(self, candles):
        self.candles = candles
        self.timestamps = {interval: sorted(values) for interval, values in candles.items()}
        self.now = 0
        self.missing_data = False

    def get_ts(self):
        return self.now

    def get_candles(self, asset, interval, count, endtime):
        timestamps = self.timestamps[interval]
        effective_end = min(endtime, self.now)
        end = bisect.bisect_right(timestamps, effective_end - interval)
        selected = timestamps[max(0, end - count):end]
        if selected and any(b - a != interval for a, b in zip(selected, selected[1:])):
            self.missing_data = True
            return []
        if selected and selected[-1] + interval != (int(effective_end) // interval) * interval:
            self.missing_data = True
            return []
        if not selected:
            self.missing_data = True
        if len(selected) < {900: 50, 300: 21, 60: 30}.get(interval, 0):
            self.missing_data = True
        return [self.candles[interval][timestamp] for timestamp in selected]


def simulate(data):
    start, end, payout, candles = validate_dataset(data)
    broker = HistoricalBroker(candles)
    logger = logging.getLogger("deskubuddy.time_machine")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    analyzer = MarketAnalyzer(broker, None, logger)
    item = {"ativo": data["asset"], "tipo": "binary", "payout": payout}
    scenarios = {
        "regra_m5_e_contexto": Counter(),
        "contingencia_m15_hipotetica": Counter(),
    }
    results = []
    for candle_start in sorted(candles[300]):
        now = candle_start + 301
        if not start <= now < end:
            continue
        broker.now = now
        for name, analyze in (
            ("regra_m5_e_contexto", analyzer.analisar_ativo),
            ("contingencia_m15_hipotetica", analyzer.analisar_contingencia_m15),
        ):
            stats = scenarios[name]
            stats["avaliacoes"] += 1
            broker.missing_data = False
            signal = analyze(item)
            if broker.missing_data:
                stats["dados_ausentes_ou_lacunas"] += 1
                continue
            if signal is None:
                stats["sem_sinal_tecnico"] += 1
                continue
            if not signal.get("humor_mercado", {}).get("favoravel"):
                stats["contexto_desfavoravel"] += 1
                continue
            stats["sinais"] += 1
            entry = int(signal["executar_em"])
            expiry = int(signal["expira_em"])
            opening = candles[60].get(entry)
            closing = candles[60].get(expiry - 60)
            if expiry > end or opening is None or closing is None:
                outcome, pnl = "INDETERMINADO", None
            else:
                delta = float(closing["close"]) - float(opening["open"])
                if delta == 0:
                    outcome, pnl = "DRAW", 0.0
                elif (delta > 0) == (signal["sinal"] == "CALL"):
                    outcome, pnl = "WIN", payout / 100
                else:
                    outcome, pnl = "LOSS", -1.0
            stats[outcome] += 1
            results.append({
                "cenario": name,
                "avaliado_em": now,
                "entrada_em": entry,
                "expira_em": expiry,
                "direcao": signal["sinal"],
                "setup": signal.get("setup"),
                "resultado": outcome,
                "pnl_por_unidade_de_stake": pnl,
            })
    if not any(stats["avaliacoes"] for stats in scenarios.values()):
        raise ValueError("Nenhum candle M5 fechado no período solicitado.")
    summary = {}
    for name, stats in scenarios.items():
        wins, losses = stats["WIN"], stats["LOSS"]
        summary[name] = {
            **dict(stats),
            "taxa_acerto_decisiva": wins / (wins + losses) if wins + losses else None,
            "pnl_por_unidade_de_stake": wins * payout / 100 - losses,
        }
    return {
        "format": "deskubuddy-time-machine-v1",
        "asset": data["asset"],
        "start": start,
        "end": end,
        "payout_snapshot_percent": payout,
        "payout_snapshot_time": data["payout_snapshot_time"],
        "limitations": [
            "Payout atual do catalogo binary aplicado hipoteticamente ao passado; "
            "nao e payout historico nem garante payout turbo.",
            "Entrada na abertura M1 e fechamento na expiracao sao aproximacoes.",
            "Nao simula Ollama, fila, risco, stop, venda antecipada ou execucao real.",
            "Um teste por fechamento M5; a contingencia e hipotetica em cada teste.",
        ],
        "summary": summary,
        "results": results,
    }
