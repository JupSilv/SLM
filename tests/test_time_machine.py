import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.time_machine import HistoricalBroker, simulate
from maquina_do_tempo import collect


def candle(timestamp, price=1.0, close=None):
    close = price if close is None else close
    return {
        "from": timestamp,
        "open": price,
        "close": close,
        "min": min(price, close),
        "max": max(price, close),
    }


class TimeMachineTests(unittest.TestCase):
    def test_historical_broker_never_returns_unclosed_or_future_candles(self):
        broker = HistoricalBroker({60: {0: candle(0), 60: candle(60), 120: candle(120)}})
        broker.now = 121
        self.assertEqual(
            [c["from"] for c in broker.get_candles("A", 60, 10, 999)],
            [0, 60],
        )

    def test_historical_broker_rejects_gaps(self):
        broker = HistoricalBroker({60: {0: candle(0), 120: candle(120)}})
        broker.now = 180
        self.assertEqual(broker.get_candles("A", 60, 10, 180), [])

    def test_simulation_outcome_and_missing_expiry_are_distinct(self):
        data = {
            "format": "deskubuddy-candles-v1",
            "asset": "EURUSD-OP",
            "start": 300,
            "end": 1000,
            "payout_snapshot_percent": 80,
            "payout_snapshot_time": 1000,
            "candles": {
                "60": [candle(360, 1, 1.1)],
                "300": [candle(0), candle(300), candle(600)],
                "900": [candle(0)],
            },
        }

        def fake_signal(analyzer, item):
            entry = (analyzer.broker.now // 60 + 1) * 60
            return {
                "sinal": "CALL",
                "executar_em": entry,
                "expira_em": entry + 60,
                "humor_mercado": {"favoravel": True},
            }

        with (
            patch("app.time_machine.MarketAnalyzer.analisar_ativo", fake_signal),
            patch("app.time_machine.MarketAnalyzer.analisar_contingencia_m15", fake_signal),
        ):
            report = simulate(data)
        summary = report["summary"]["regra_m5_e_contexto"]
        self.assertEqual(summary["WIN"], 1)
        self.assertEqual(summary["INDETERMINADO"], 2)
        self.assertEqual(summary["pnl_por_unidade_de_stake"], 0.8)
        self.assertEqual(summary["taxa_acerto_decisiva"], 1.0)

    def test_invalid_prices_are_rejected(self):
        data = {
            "format": "deskubuddy-candles-v1", "asset": "EURUSD-OP",
            "start": 300, "end": 1000, "payout_snapshot_percent": 80,
            "payout_snapshot_time": 1000,
            "candles": {"60": [candle(360, 0)], "300": [candle(300)], "900": [candle(0)]},
        }
        with self.assertRaises(ValueError):
            simulate(data)

    def test_signal_with_missing_analysis_candles_is_not_counted(self):
        data = {
            "format": "deskubuddy-candles-v1", "asset": "EURUSD-OP",
            "start": 300, "end": 602, "payout_snapshot_percent": 80,
            "payout_snapshot_time": 602,
            "candles": {"60": [candle(240)], "300": [candle(0)], "900": [candle(0)]},
        }

        def fake_signal(analyzer, item):
            analyzer.broker.get_candles(item["ativo"], 60, 30, analyzer.broker.now)
            return {
                "sinal": "CALL", "setup": "TEST", "executar_em": 360,
                "expira_em": 420, "humor_mercado": {"favoravel": True},
            }

        with (
            patch("app.time_machine.MarketAnalyzer.analisar_ativo", fake_signal),
            patch("app.time_machine.MarketAnalyzer.analisar_contingencia_m15", fake_signal),
        ):
            report = simulate(data)
        self.assertEqual(report["results"], [])
        self.assertEqual(report["summary"]["regra_m5_e_contexto"]["dados_ausentes_ou_lacunas"], 1)

    def test_real_strategy_replays_candles_offline(self):
        def prices(interval):
            return [
                candle(timestamp, 1 + timestamp / 10000000,
                       1.00001 + timestamp / 10000000)
                for timestamp in range(0, 45900, interval)
            ]

        data = {
            "format": "deskubuddy-candles-v1", "asset": "EURUSD-OP",
            "start": 45000, "end": 45900, "payout_snapshot_percent": 80,
            "payout_snapshot_time": 45900,
            "candles": {str(i): prices(i) for i in (60, 300, 900)},
        }
        report = simulate(data)
        summary = report["summary"]["contingencia_m15_hipotetica"]
        self.assertEqual(summary["avaliacoes"], 3)
        self.assertEqual(summary["sinais"], 3)
        self.assertEqual(summary["WIN"], 3)

    def test_simulation_cli_writes_report_without_network(self):
        data = {
            "format": "deskubuddy-candles-v1", "asset": "EURUSD-OP",
            "start": 300, "end": 1000, "payout_snapshot_percent": 80,
            "payout_snapshot_time": 1000,
            "candles": {"60": [candle(360)], "300": [candle(300)], "900": [candle(0)]},
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.json"
            destination = Path(directory) / "report.json"
            source.write_text(json.dumps(data), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "maquina_do_tempo.py", "simular",
                 "--dados", str(source), "--saida", str(destination)],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(report["format"], "deskubuddy-time-machine-v1")
            self.assertEqual(
                report["summary"]["regra_m5_e_contexto"]["dados_ausentes_ou_lacunas"],
                1,
            )

    def test_collection_reads_catalog_and_candles_without_orders(self):
        broker = Mock()
        broker.get_all_init_v2.return_value = {"binary": {"actives": {
            "1": {"name": "front.EURUSD-OP", "id": 1,
                  "option": {"profit": {"commission": 20}}}
        }}}
        broker.get_candles.side_effect = lambda asset, interval, count, end: [
            candle((end // interval - i - 1) * interval) for i in range(1000)
        ]
        with patch.dict("iqoptionapi.constants.ACTIVES", {}, clear=False):
            result = collect(broker, "EURUSD-OP", 1, 1800000000)
        self.assertEqual(result["payout_snapshot_percent"], 80)
        self.assertGreater(len(result["candles"]["60"]), 1000)
        broker.buy.assert_not_called()
        broker.buy_digital_spot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
