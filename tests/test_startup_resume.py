import time
import unittest
from unittest.mock import Mock, patch

from app.risk import ConservativeRiskManager
from trading_worker import AtivoTradingWorker


def worker(risk, rank=0, priority=None, count=None):
    return AtivoTradingWorker(
        Mock(), {"ativo": "EURUSD"}, risk, Mock(), Mock(), Mock(), Mock(),
        startup_fallback=True, startup_rank=rank,
        startup_priority=priority, startup_count=count,
    )


class StartupResumeTests(unittest.TestCase):
    def test_only_manual_resume_enables_observation_contingency(self):
        risk = ConservativeRiskManager(initial_balance=100, entry_amount=2)
        risk.definir_pausa_manual(True)
        risk.definir_pausa_manual(False)
        candidate = worker(risk)
        self.assertTrue(candidate._startup_fallback_available())
        token, _ = risk.reservar_entrada("EURUSD", time.time())
        self.assertIsNotNone(token)
        risk.cancelar_reserva_entrada(token)
        risk.registrar_resultado_ordem("loss", "LOSS", -2, "EURUSD")
        risk._cooldown_global_ate = time.time() - 1
        self.assertEqual(risk.obter_estado_operacional(), "DEMO_OBSERVATION")
        self.assertFalse(candidate._startup_fallback_available())

    def test_manual_resume_does_not_bypass_active_cooldown(self):
        risk = ConservativeRiskManager(initial_balance=100, entry_amount=2)
        risk.registrar_resultado_ordem("loss", "LOSS", -2, "EURUSD")
        risk.definir_pausa_manual(True)
        risk.definir_pausa_manual(False)
        self.assertFalse(risk.contingencia_apos_pausa_manual())
        token, _ = risk.reservar_entrada("EURUSD", time.time())
        self.assertIsNone(token)
        risk._cooldown_global_ate = time.time() - 1
        self.assertFalse(risk.contingencia_apos_pausa_manual())

    def test_result_after_resume_clears_manual_contingency(self):
        risk = ConservativeRiskManager(initial_balance=100, entry_amount=2)
        risk.definir_pausa_manual(True)
        risk.definir_pausa_manual(False)
        risk.registrar_resultado_ordem("older-order", "LOSS", -2, "GBPUSD")
        risk._cooldown_global_ate = time.time() - 1
        self.assertFalse(risk.contingencia_apos_pausa_manual())

    def test_priority_wraps_after_all_candidates(self):
        risk = ConservativeRiskManager(initial_balance=100, entry_amount=2)
        priority, count = [0], [2]
        first = worker(risk, 0, priority, count)
        second = worker(risk, 1, priority, count)
        first._advance_startup_priority()
        self.assertEqual(priority[0], 1)
        second._advance_startup_priority()
        self.assertEqual(priority[0], 0)

    def test_blocked_risk_does_not_consume_startup_attempt(self):
        risk = Mock()
        risk.deve_parar.return_value = (False, "OK")
        risk.modo_observacao.return_value = False
        risk.obter_estado_operacional.return_value = "NORMAL"
        risk.reservar_entrada.return_value = (None, "bloqueado")
        risk.calcular_stake.return_value = 2
        candidate = worker(risk)
        candidate.broker.check_connect.return_value = True
        candidate.broker.get_balance.return_value = 100
        candidate.has_open_order.return_value = False
        candidate.analyzer.analisar_ativo.return_value = None
        candidate.analyzer.analisar_contingencia_m15.return_value = {
            "sinal": "CALL", "payout": 80, "setup": "TEST",
            "executar_em": 1800000000, "expira_em": 1800000060,
            "humor_mercado": {"favoravel": True},
        }
        candidate._monitorar_sinal_pulado = Mock(
            side_effect=lambda *args: setattr(candidate, "running", False)
        )
        with (
            patch("trading_worker._timestamp_broker", return_value=1800000000),
            patch("trading_worker._horario_disparo", return_value=1800000000),
            patch("trading_worker.registrar_candidato_db", return_value=time.time()),
            patch("trading_worker.atualizar_decisao_sinal"),
            patch("trading_worker.atualizar_status_ordem_db"),
            patch("trading_worker.time.sleep", side_effect=RuntimeError("unexpected retry")),
        ):
            candidate._executar_loop()
        self.assertFalse(candidate.startup_order_claimed.is_set())
        candidate.broker.buy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
