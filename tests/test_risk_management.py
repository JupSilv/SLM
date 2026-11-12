import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import database
from app.executor import AsyncExecutionEngine
from app.risk import ConservativeRiskManager
from config import Config


def build_risk(**kwargs):
    options = {
        "initial_balance": 60.0,
        "profit_target": 100.0,
        "entry_amount": 10.0,
        "account_type": "PRACTICE",
        "post_win_limit": 2,
        "post_win_cooldown_minutes": 1,
        "post_loss_cooldown_minutes": 1,
    }
    options.update(kwargs)
    return ConservativeRiskManager(**options)


class RiskManagementTests(unittest.TestCase):
    def test_two_confirmed_wins_activate_global_protection(self):
        risk = build_risk()

        risk.registrar_resultado_ordem("order-1", "WIN", 1.0, "EURUSD")
        self.assertEqual(risk.obter_estado_operacional(), "NORMAL")
        risk.registrar_resultado_ordem("order-2", "WIN", 1.0, "GBPUSD")

        self.assertEqual(
            risk.obter_estado_operacional(), "POST_WIN_PROTECTION"
        )
        token, motivo = risk.reservar_entrada("USDJPY", time.time())
        self.assertIsNone(token)
        self.assertIn("cooldown", motivo.lower())

    def test_draw_from_an_already_open_order_does_not_clear_win_protection(self):
        risk = build_risk()
        risk.registrar_resultado_ordem("order-1", "WIN", 1.0, "EURUSD")
        risk.registrar_resultado_ordem("order-2", "WIN", 1.0, "GBPUSD")

        risk.registrar_resultado_ordem("order-3", "DRAW", 0.0, "USDJPY")

        self.assertEqual(
            risk.obter_estado_operacional(), "POST_WIN_PROTECTION"
        )

    def test_cooldown_requires_a_new_signal_and_keeps_base_stake(self):
        risk = build_risk()
        risk.registrar_resultado_ordem("order-1", "WIN", 1.0, "EURUSD")
        risk.registrar_resultado_ordem("order-2", "WIN", 1.0, "EURUSD")
        risk._cooldown_global_ate = time.time() - 1

        self.assertEqual(risk.obter_estado_operacional(), "DEMO_OBSERVATION")
        token, _ = risk.reservar_entrada("GBPUSD")
        self.assertIsNone(token)
        token, motivo = risk.reservar_entrada("GBPUSD", time.time())

        self.assertIsNotNone(token, motivo)
        self.assertEqual(risk.calcular_stake(65.9), 2.0)
        self.assertEqual(risk.obter_estado_operacional(), "NORMAL")
        risk.cancelar_reserva_entrada(token)

    def test_win_does_not_start_recovery_and_loss_enters_cooldown(self):
        risk = build_risk()
        risk.registrar_resultado_ordem("order-win", "WIN", 1.2, "EURUSD")
        self.assertEqual(risk.obter_estado_operacional(), "NORMAL")

        risk.registrar_resultado_ordem("order-loss", "LOSS", -2.0, "EURUSD")
        self.assertEqual(
            risk.obter_estado_operacional(), "POST_LOSS_COOLDOWN"
        )
        token, _ = risk.reservar_entrada("EURUSD", time.time())
        self.assertIsNone(token)

    def test_draw_breaks_win_streak_and_indeterminate_blocks_until_reconciled(self):
        risk = build_risk()
        risk.registrar_resultado_ordem("order-1", "WIN", 1.0, "EURUSD")
        risk.registrar_resultado_ordem("order-2", "DRAW", 0.0, "EURUSD")
        self.assertEqual(risk._vitorias_consecutivas, 0)
        self.assertEqual(risk.obter_estado_operacional(), "NORMAL")

        risk.registrar_resultado_ordem(
            "order-3", "INDETERMINADO", 0.0, "EURUSD"
        )
        self.assertEqual(
            risk.obter_estado_operacional(), "INDETERMINATE_BLOCK"
        )
        self.assertFalse(
            risk.autorizar_entrada("EURUSD", time.time())[0]
        )
        self.assertTrue(
            risk.reconciliar_resultado(
                "order-3", "LOSS", -2.0, "EURUSD"
            )
        )
        self.assertEqual(
            risk.obter_estado_operacional(), "POST_LOSS_COOLDOWN"
        )

    def test_session_target_high_water_and_stake_are_independent(self):
        risk = build_risk(profit_target=5.9)
        risk.registrar_resultado_ordem("order-1", "WIN", 5.9, "EURUSD")

        self.assertEqual(risk.lucro_acumulado_sessao, 5.9)
        self.assertEqual(risk.saldo_referencia_stop, 65.9)
        self.assertEqual(risk.calcular_stake(65.9), 2.0)
        self.assertEqual(
            risk.obter_estado_operacional(), "DAILY_TARGET_REACHED"
        )
        self.assertFalse(risk.autorizar_entrada("EURUSD", time.time())[0])

    def test_duplicate_order_result_does_not_double_count_pnl(self):
        risk = build_risk()
        self.assertTrue(
            risk.registrar_resultado_ordem("same-order", "WIN", 5.9, "EURUSD")
        )
        self.assertTrue(
            risk.registrar_resultado_ordem("same-order", "WIN", 5.9, "EURUSD")
        )

        self.assertEqual(risk.lucro_acumulado_sessao, 5.9)

    def test_concurrent_workers_cannot_reserve_the_same_entry(self):
        risk = build_risk()
        barrier = threading.Barrier(3)
        reservations = []
        result_lock = threading.Lock()

        def reserve():
            barrier.wait()
            token, _ = risk.reservar_entrada("EURUSD", time.time())
            with result_lock:
                reservations.append(token)

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        tokens = [token for token in reservations if token is not None]
        self.assertEqual(len(tokens), 1)
        risk.cancelar_reserva_entrada(tokens[0])

    def test_open_order_prevents_duplicate_asset_entry(self):
        risk = build_risk()
        token, _ = risk.reservar_entrada("EURUSD", time.time())
        self.assertTrue(risk.confirmar_entrada(token, "open-order"))

        duplicate, _ = risk.reservar_entrada("EURUSD", time.time())
        other_asset, _ = risk.reservar_entrada("GBPUSD", time.time())

        self.assertIsNone(duplicate)
        self.assertIsNotNone(other_asset)
        risk.cancelar_reserva_entrada(other_asset)

    def test_state_persists_by_account_and_restores_financial_reference(self):
        previous_db_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            database.DB_PATH = str(Path(directory) / "risk.db")
            try:
                database.iniciar_banco()
                risk = build_risk(
                    state_loader=database.carregar_estado_risco,
                    state_saver=database.salvar_estado_risco,
                )
                risk.registrar_resultado_ordem(
                    "practice-win", "WIN", 5.9, "EURUSD"
                )

                restored = build_risk(
                    initial_balance=65.9,
                    profit_target=200.0,
                    state_loader=database.carregar_estado_risco,
                    state_saver=database.salvar_estado_risco,
                )
                real_account = build_risk(
                    initial_balance=500.0,
                    account_type="REAL",
                    state_loader=database.carregar_estado_risco,
                    state_saver=database.salvar_estado_risco,
                )

                self.assertEqual(restored.lucro_acumulado_sessao, 5.9)
                self.assertEqual(restored.saldo_referencia_stop, 65.9)
                self.assertEqual(restored.meta_lucro_sessao, 100.0)
                self.assertEqual(real_account.lucro_acumulado_sessao, 0.0)
                self.assertEqual(real_account.banca_inicial_sessao, 500.0)
            finally:
                database.DB_PATH = previous_db_path

    def test_pending_order_reservation_restores_as_indeterminate_block(self):
        previous_db_path = database.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            database.DB_PATH = str(Path(directory) / "risk.db")
            try:
                database.iniciar_banco()
                risk = build_risk(
                    state_loader=database.carregar_estado_risco,
                    state_saver=database.salvar_estado_risco,
                )
                token, _ = risk.reservar_entrada("EURUSD", time.time())
                self.assertIsNotNone(token)

                restarted = build_risk(
                    state_loader=database.carregar_estado_risco,
                    state_saver=database.salvar_estado_risco,
                )

                self.assertEqual(
                    restarted.obter_estado_operacional(),
                    "INDETERMINATE_BLOCK",
                )
                self.assertFalse(
                    restarted.autorizar_entrada("EURUSD", time.time())[0]
                )
            finally:
                database.DB_PATH = previous_db_path

    def test_blocked_signal_never_calls_broker_order_method(self):
        class FakeBroker:
            def __init__(self):
                self.buy_calls = 0

            def get_server_timestamp(self):
                return time.time()

            def get_balance(self):
                raise AssertionError("Blocked signal must not read/send an order.")

            def buy(self, *_args):
                self.buy_calls += 1
                return True, "unexpected"

        risk = build_risk()
        risk.registrar_pnl_ordem(100.0)
        broker = FakeBroker()
        tracker = Mock()
        executor = AsyncExecutionEngine(broker, risk, tracker)
        dispatch_setting = Config.ORDER_DISPATCH_SECOND
        Config.ORDER_DISPATCH_SECOND = "00"
        try:
            executor._process_execution(
                {
                    "executar_em": int(time.time()),
                    "tipo": "binary",
                    "ativo": "EURUSD",
                    "sinal": "CALL",
                    "setup": "test",
                    "payout": 80,
                }
            )
        finally:
            Config.ORDER_DISPATCH_SECOND = dispatch_setting

        self.assertEqual(broker.buy_calls, 0)

    def test_real_account_requires_explicit_confirmation(self):
        previous = Config.IQ_ENABLE_REAL_TRADING
        Config.IQ_ENABLE_REAL_TRADING = False
        try:
            with self.assertRaisesRegex(ValueError, "bloqueada"):
                Config.validate("test@example.invalid", "test-password", "REAL")
            Config.validate("test@example.invalid", "test-password", "PRACTICE")
        finally:
            Config.IQ_ENABLE_REAL_TRADING = previous


if __name__ == "__main__":
    unittest.main()
