import unittest
from unittest.mock import Mock, patch

from trading_worker import DeskubuddyTradingEngine


class EngineDiagnosticsTests(unittest.TestCase):
    def test_dispatcher_reports_exception_location_without_message(self):
        engine = DeskubuddyTradingEngine(
            email="user@example.invalid", password="secret", account_type="PRACTICE"
        )
        engine._run_engine = Mock(side_effect=RuntimeError("secret"))

        with self.assertLogs(level="ERROR") as captured:
            engine.run()

        output = "\n".join(captured.output)
        self.assertIn("tipo=RuntimeError", output)
        self.assertIn("trading_worker.py:", output)
        self.assertNotIn("secret", output)

    def test_connection_failure_reports_location_without_message(self):
        engine = DeskubuddyTradingEngine(
            email="user@example.invalid", password="secret", account_type="PRACTICE"
        )
        broker = Mock()
        broker.connect.side_effect = ValueError("secret")

        with (
            patch("trading_worker.iniciar_banco"),
            patch("trading_worker.IQ_Option", return_value=broker),
            self.assertLogs(level="ERROR") as captured,
        ):
            engine._run_engine()

        output = "\n".join(captured.output)
        self.assertIn("tipo=ValueError", output)
        self.assertIn("trading_worker.py:", output)
        self.assertNotIn("secret", output)
        broker.buy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
