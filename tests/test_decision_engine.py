import unittest
from unittest.mock import Mock, patch

from app.decision_engine import OllamaTradeDecisionEngine


class DecisionEngineTests(unittest.TestCase):
    def test_positive_history_reaches_ollama_with_macro_summary(self):
        signal = {
            "ativo": "EURUSD",
            "sinal": "CALL",
            "setup": "TEST",
            "payout": 80,
        }
        history = [{"resultado": "WIN", "payout": 80}] * 5
        response = Mock(response='{"decisao": "OPERAR", "motivo": "Aprovado"}')
        with (
            patch("app.decision_engine.ollama.Client") as client,
            patch(
                "app.decision_engine.obter_historico_comparavel",
                side_effect=[history, []],
            ),
            patch(
                "app.decision_engine.obter_historico_global_recente",
                return_value=[{"resultado": "LOSS"}],
            ),
        ):
            client.return_value.generate.return_value = response
            decision, reason = OllamaTradeDecisionEngine().decidir(signal)

        self.assertEqual((decision, reason), ("OPERAR_OLLAMA", "Aprovado"))
        prompt = client.return_value.generate.call_args.kwargs["prompt"]
        self.assertIn('"derrotas_recentes_gerais": 1', prompt)


if __name__ == "__main__":
    unittest.main()
