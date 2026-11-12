import threading
import time
import unittest
from unittest.mock import Mock, patch

from app.live_time_machine import CorrelatedCandleReader, LiveTimeMachine
from iqoptionapi.ws.received.candles import candles
from trading_worker import AtivoTradingWorker


SCENARIO = "regra_m5_e_contexto"
CONTINGENCY = "contingencia_m15_hipotetica"
SIGNAL = {
    "ativo": "EURUSD",
    "sinal": "CALL",
    "setup": "Fibo",
    "payout": 80,
}


def result(scenario=SCENARIO, outcome="LOSS", direction="CALL", setup="Fibo", index=0):
    return {
        "cenario": scenario,
        "resultado": outcome,
        "direcao": direction,
        "setup": setup,
        "avaliado_em": 1800000001 + index * 300,
        "entrada_em": 1800000060 + index * 300,
        "expira_em": 1800000120 + index * 300,
    }


def machine(account="PRACTICE"):
    broker = Mock()
    broker.get_ts.return_value = 1800000000
    return LiveTimeMachine(broker, account, threading.Lock())


class CorrelatedReaderTests(unittest.TestCase):
    def test_isolates_response_and_ignores_late_simulation_messages(self):
        broker = Mock()
        broker.api = Mock(spec=["send_websocket_request", "candles"])
        broker.api.candles = Mock()
        broker.api.candles.candles_data = ["legacy"]
        stop = threading.Event()
        reader = CorrelatedCandleReader(broker, stop, threading.Lock())

        def send(name, body, request_id):
            candles(broker.api, {
                "name": "candles", "request_id": request_id,
                "msg": {"candles": [{"from": 120}]},
            })

        broker.api.send_websocket_request.side_effect = send
        with patch.dict("app.live_time_machine.constants.ACTIVES", {"EURUSD": 1}):
            self.assertEqual(reader.get_candles("EURUSD", 60, 1, 180), [{"from": 120}])
        candles(broker.api, {
            "name": "candles", "request_id": "deskubuddy-tm-late",
            "msg": {"candles": ["wrong"]},
        })
        self.assertEqual(broker.api.candles.candles_data, ["legacy"])
        self.assertEqual(broker.api._deskubuddy_tm_pending, {})
        broker.buy.assert_not_called()

    def test_timeout_removes_pending_and_reconnect_uses_new_api(self):
        broker = Mock()
        broker.api = Mock(spec=["send_websocket_request"])
        reader = CorrelatedCandleReader(broker, threading.Event(), threading.Lock(), 0.01)
        with patch.dict("app.live_time_machine.constants.ACTIVES", {"EURUSD": 1}):
            with self.assertRaises(TimeoutError):
                reader.get_candles("EURUSD", 60, 1, 180)
            self.assertEqual(broker.api._deskubuddy_tm_pending, {})
            broker.api = Mock(spec=["send_websocket_request"])
            def send(name, body, request_id):
                candles(broker.api, {"name": "candles", "request_id": request_id,
                                     "msg": {"candles": []}})
            broker.api.send_websocket_request.side_effect = send
            self.assertEqual(reader.get_candles("EURUSD", 60, 1, 180), [])

    def test_lock_timeout_does_not_send_and_removes_pending(self):
        broker = Mock()
        broker.api = Mock(spec=["send_websocket_request"])
        lock = threading.Lock()
        lock.acquire()
        try:
            reader = CorrelatedCandleReader(broker, threading.Event(), lock, 0.01)
            with patch.dict("app.live_time_machine.constants.ACTIVES", {"EURUSD": 1}):
                with self.assertRaises(TimeoutError):
                    reader.get_candles("EURUSD", 60, 1, 180)
            broker.api.send_websocket_request.assert_not_called()
            self.assertEqual(broker.api._deskubuddy_tm_pending, {})
        finally:
            lock.release()


class LiveTimeMachineTests(unittest.TestCase):
    def install(self, instance, rows, timestamp=None):
        instance._reports["EURUSD"] = (
            time.monotonic() if timestamp is None else timestamp,
            {"results": rows},
        )

    def test_veto_requires_twenty_comparable_decisive_results(self):
        instance = machine()
        rows = [result(index=i) for i in range(19)]
        rows += [result(outcome="DRAW"), result(direction="PUT"), result(setup="other")]
        self.install(instance, rows)
        veto, message = instance.evaluate(SIGNAL, False)
        self.assertFalse(veto)
        self.assertIn("19/20", message)
        self.install(instance, rows + [result(index=20)])
        self.assertTrue(instance.evaluate(SIGNAL, False)[0])

    def test_break_even_and_strategy_direction_are_separate(self):
        instance = machine()
        rows = [result(outcome="WIN" if i < 12 else "LOSS", index=i) for i in range(20)]
        rows += [result(CONTINGENCY, "LOSS", setup="Startup", index=i) for i in range(20)]
        self.install(instance, rows)
        self.assertFalse(instance.evaluate(SIGNAL, False)[0])
        self.assertTrue(instance.evaluate({**SIGNAL, "setup": "Startup"}, True)[0])
        self.assertFalse(instance.evaluate({**SIGNAL, "sinal": "PUT"}, False)[0])
        self.assertTrue(instance.evaluate({**SIGNAL, "payout": 50}, False)[0])

    def test_both_accounts_use_same_filter_and_ignore_stale_results(self):
        for account in ("PRACTICE", "REAL"):
            with self.subTest(account=account):
                instance = machine(account)
                self.install(instance, [result(index=i) for i in range(19)])
                veto, detail = instance.evaluate(SIGNAL, False)
                self.assertFalse(veto)
                self.assertIn("19/20", detail)
                rows = [result(index=i) for i in range(20)]
                self.install(instance, rows)
                self.assertTrue(instance.evaluate(SIGNAL, False)[0])
                self.install(instance, [
                    result(outcome="WIN" if i < 12 else "LOSS", index=i)
                    for i in range(20)
                ])
                self.assertFalse(instance.evaluate(SIGNAL, False)[0])
                self.install(instance, rows, time.monotonic() - instance.MAX_AGE_SECONDS - 1)
                veto, detail = instance.evaluate(SIGNAL, False)
                self.assertFalse(veto)
                self.assertIn("desatualizada", detail)
                instance._reports.clear()
                self.assertFalse(instance.evaluate(SIGNAL, False)[0])

    def test_real_contingency_uses_only_its_own_comparable_results(self):
        instance = machine("REAL")
        rows = [result(index=i) for i in range(20)]
        rows += [
            result(CONTINGENCY, "WIN", setup="Startup", index=i)
            for i in range(20)
        ]
        self.install(instance, rows)
        self.assertFalse(instance.evaluate({**SIGNAL, "setup": "Startup"}, True)[0])
        rows[-1] = result(CONTINGENCY, "LOSS", setup="Startup", index=19)
        for i in range(10):
            rows[20 + i] = result(CONTINGENCY, "LOSS", setup="Startup", index=i)
        self.assertTrue(instance.evaluate({**SIGNAL, "setup": "Startup"}, True)[0])

    def test_live_entry_and_outcome_are_reported_once_and_never_placed(self):
        instance = machine()
        messages = []
        instance._log = messages.append
        row = result()
        instance._announce("EURUSD", [row], row["entrada_em"] - 1)
        self.assertEqual(messages, [])
        instance._announce("EURUSD", [row], row["entrada_em"])
        instance._announce("EURUSD", [row], row["entrada_em"] + 1)
        instance._announce("EURUSD", [row], row["expira_em"])
        instance._announce("EURUSD", [row], row["expira_em"] + 1)
        self.assertEqual(len(messages), 2)
        self.assertIn("ENTRADA SIMULADA", messages[0])
        self.assertIn("RESULTADO SIMULADO", messages[1])
        instance.broker.buy.assert_not_called()

    def test_refresh_reuses_history_and_adds_only_closed_candles(self):
        instance = machine()
        item = {"ativo": "EURUSD", "tipo": "binary", "payout": 80}
        instance.set_assets([item])
        initial = 1800000000

        def candle(timestamp):
            return {"from": timestamp, "open": 1, "close": 1, "min": 1, "max": 1}

        data = {
            "format": "deskubuddy-candles-v1", "asset": "EURUSD",
            "start": initial - 3 * 86400, "end": initial,
            "payout_snapshot_percent": 80, "payout_snapshot_time": initial,
            "candles": {str(i): [candle(initial - i)] for i in (60, 300, 900)},
        }
        instance._reader.get_candles = Mock(
            side_effect=lambda asset, interval, count, end: [
                candle((end // interval) * interval - interval),
                candle((end // interval) * interval),
            ]
        )
        report = {"results": []}
        with (
            patch("app.live_time_machine.collect_candles", return_value=data) as collect,
            patch("app.live_time_machine.simulate", return_value=report) as simulate,
        ):
            instance._refresh(item)
            instance.broker.get_ts.return_value = initial + 120
            instance._refresh(item)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(instance._reader.get_candles.call_count, 3)
        updated = simulate.call_args.args[0]
        self.assertEqual(updated["end"], initial + 120)
        for interval in (60, 300, 900):
            self.assertTrue(all(
                candle_["from"] + interval <= updated["end"]
                for candle_ in updated["candles"][str(interval)]
            ))
        instance.broker.buy.assert_not_called()

    def test_startup_veto_prevents_reservation_and_buy(self):
        risk = Mock()
        risk.deve_parar.return_value = (False, "OK")
        risk.modo_observacao.return_value = False
        broker = Mock()
        broker.check_connect.return_value = True
        broker.get_balance.return_value = 100
        analyzer = Mock()
        analyzer.analisar_ativo.return_value = None
        analyzer.analisar_contingencia_m15.return_value = {
            **SIGNAL, "setup": "Startup", "executar_em": 1800000060,
            "expira_em": 1800000120, "humor_mercado": {"favoravel": True},
        }
        filter_ = Mock()
        filter_.evaluate.return_value = (True, "veto")
        worker = AtivoTradingWorker(
            broker, {"ativo": "EURUSD"}, risk, analyzer, Mock(), Mock(),
            Mock(return_value=False), startup_fallback=True, time_machine=filter_,
        )
        worker._monitorar_sinal_pulado = Mock(
            side_effect=lambda *args: setattr(worker, "running", False)
        )
        with (
            patch("trading_worker.registrar_candidato_db", return_value="ts"),
            patch("trading_worker.atualizar_decisao_sinal") as update,
        ):
            worker._executar_loop()
        self.assertEqual(update.call_args.args[2], "PULAR_MAQUINA_DO_TEMPO")
        self.assertTrue(filter_.evaluate.call_args.args[1])
        risk.reservar_entrada.assert_not_called()
        broker.buy.assert_not_called()
        self.assertFalse(worker.startup_order_claimed.is_set())

    def test_real_veto_blocks_normal_and_startup_before_broker_buy(self):
        for contingency in (False, True):
            with self.subTest(contingency=contingency):
                risk = Mock()
                risk.deve_parar.return_value = (False, "OK")
                risk.modo_observacao.return_value = False
                broker = Mock()
                broker.check_connect.return_value = True
                broker.get_balance.return_value = 100
                analyzer = Mock()
                signal = {
                    **SIGNAL, "executar_em": 1800000060, "expira_em": 1800000120,
                    "humor_mercado": {"favoravel": True},
                }
                analyzer.analisar_ativo.return_value = None if contingency else signal
                analyzer.analisar_contingencia_m15.return_value = signal
                decision = Mock()
                decision.decidir.return_value = ("OPERAR_ANALYZER", "sinal aprovado")
                filter_ = machine("REAL")
                scenario = CONTINGENCY if contingency else SCENARIO
                self.install(filter_, [result(scenario, index=i) for i in range(20)])
                worker = AtivoTradingWorker(
                    broker, {"ativo": "EURUSD"}, risk, analyzer, decision, Mock(),
                    Mock(return_value=False), startup_fallback=contingency,
                    time_machine=filter_,
                )
                worker._monitorar_sinal_pulado = Mock(
                    side_effect=lambda *args: setattr(worker, "running", False)
                )
                with (
                    patch("trading_worker.registrar_candidato_db", return_value="ts"),
                    patch("trading_worker.atualizar_decisao_sinal") as update,
                ):
                    worker._executar_loop()
                self.assertEqual(update.call_args.args[2], "PULAR_MAQUINA_DO_TEMPO")
                risk.reservar_entrada.assert_not_called()
                broker.buy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
