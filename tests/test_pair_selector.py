import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from app.pair_selector import DynamicPairSelector


class PairSelectorTests(unittest.TestCase):
    def setUp(self):
        self.names = [
            "EURUSD", "EURGBP", "EURJPY", "EURCAD", "EURCHF",
            "GBPUSD", "GBPJPY", "GBPCAD", "GBPCHF", "USDJPY",
            "USDCAD", "USDCHF", "AUDUSD", "NZDUSD", "AUDJPY",
        ]
        actives = {}
        for index, name in enumerate(self.names, 1):
            actives[str(index)] = {
                "name": f"front.{name}-op",
                "enabled": True,
                "is_suspended": False,
                "option": {"profit": {"commission": 20}},
                "id": index,
            }
        actives["closed"] = {
            "name": "front.NZDJPY-op",
            "enabled": False,
            "id": 100,
            "option": {"profit": {"commission": 20}},
        }
        actives["otc"] = {
            "name": "front.EURUSD-OTC",
            "enabled": True,
            "id": 101,
            "option": {"profit": {"commission": 20}},
        }
        self.broker = Mock()
        self.broker.get_all_init_v2.return_value = {
            "binary": {"actives": actives}
        }
        self.now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)

    def select(self, **kwargs):
        selector = DynamicPairSelector(self.broker, Mock(), **kwargs)
        with (
            patch("app.pair_selector.active_sessions", return_value=[]),
            patch("app.pair_selector.local_session_hours", return_value={}),
            patch("app.pair_selector.obter_historico_otc_simulado", return_value=[]),
            patch.dict("app.pair_selector.OP_code.ACTIVES", {}, clear=False),
        ):
            result = selector.obter_ativos_operaveis(self.now)
        self.assertTrue(selector.last_catalog_success)
        return result

    def test_selects_at_most_ten_pairs_by_default(self):
        result = self.select()
        self.assertEqual(len(result), 10)
        self.assertEqual(len({item["ativo"] for item in result}), 10)
        self.assertTrue(
            {item["ativo"] for item in result}
            <= {f"{name}-OP" for name in self.names}
        )

    def test_unlimited_selection_can_be_requested_explicitly(self):
        result = self.select(limit=None)
        self.assertEqual(len(result), 15)
        self.assertEqual(
            {item["ativo"] for item in result},
            {f"{name}-OP" for name in self.names},
        )

    def test_explicit_limit_still_caps_selection(self):
        result = self.select(limit=5)
        self.assertEqual(len(result), 5)
        self.assertEqual(len({item["ativo"] for item in result}), 5)

    def test_rejects_nonpositive_explicit_limit(self):
        with self.assertRaises(ValueError):
            DynamicPairSelector(self.broker, Mock(), limit=0)


if __name__ == "__main__":
    unittest.main()
