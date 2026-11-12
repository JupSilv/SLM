import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ConfigExampleTests(unittest.TestCase):
    def run_config(self, dotenv, code):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            shutil.copyfile(ROOT / "config.example.py", path / "config.py")
            (path / ".env").write_text(dotenv, encoding="utf-8")
            environment = os.environ.copy()
            for name in (
                "IQ_EMAIL", "IQ_PASSWORD", "IQ_ACCOUNT_TYPE", "IQ_ENABLE_REAL_TRADING",
                "ENTRY_AMOUNT", "STOP_LOSS", "STOP_WIN", "MIN_PAYOUT", "MAX_PAIRS",
                "ORDER_DISPATCH_SECOND", "POST_WIN_LIMIT", "POST_WIN_COOLDOWN_MINUTES",
                "POST_LOSS_COOLDOWN_MINUTES",
            ):
                environment.pop(name, None)
            return subprocess.run(
                [sys.executable, "-c", code], cwd=path, env=environment,
                capture_output=True, text=True, timeout=15,
            )

    def test_defaults_require_credentials_and_disable_real(self):
        result = self.run_config("", """
from config import Config
assert Config.IQ_EMAIL == '' and Config.IQ_PASSWORD == ''
assert Config.IQ_ACCOUNT_TYPE == 'PRACTICE'
assert not Config.IQ_ENABLE_REAL_TRADING
try:
    Config.validate()
except ValueError as error:
    assert 'IQ_EMAIL' in str(error)
else:
    raise AssertionError('missing credentials accepted')
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dotenv_credentials_and_real_confirmation(self):
        result = self.run_config(
            "IQ_EMAIL=test@example.invalid\nIQ_PASSWORD='example password'\n"
            "IQ_ACCOUNT_TYPE=REAL\n",
            """
from config import Config
assert Config.IQ_EMAIL == 'test@example.invalid'
assert Config.IQ_PASSWORD == 'example password'
try:
    Config.validate()
except ValueError as error:
    assert 'IQ_ENABLE_REAL_TRADING' in str(error)
else:
    raise AssertionError('REAL enabled without confirmation')
""",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_config(
            "IQ_EMAIL=test@example.invalid\nIQ_PASSWORD='example password'\n"
            "IQ_ACCOUNT_TYPE=REAL\nIQ_ENABLE_REAL_TRADING=YES\n",
            "from config import Config; Config.validate()",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_example_exposes_configuration_members(self):
        tree = ast.parse((ROOT / "config.example.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "Config")
        members = {
            target.id for node in cls.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        }
        self.assertTrue({
            "IQ_EMAIL", "IQ_PASSWORD", "IQ_ACCOUNT_TYPE", "IQ_ENABLE_REAL_TRADING",
            "ENTRY_AMOUNT", "STOP_LOSS", "STOP_WIN", "MIN_PAYOUT", "MAX_PAIRS",
            "ORDER_DISPATCH_SECOND", "POST_WIN_LIMIT", "POST_WIN_COOLDOWN_MINUTES",
            "POST_LOSS_COOLDOWN_MINUTES",
        } <= members)


if __name__ == "__main__":
    unittest.main()
