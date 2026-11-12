import logging
import json
from pathlib import Path
from datetime import datetime

class TradingMetricsLogger:
    def __init__(self, log_dir="logs"):
        Path(log_dir).mkdir(exist_ok=True)
        self.log_file = Path(log_dir) / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[
                logging.FileHandler(Path(log_dir) / "app.log", encoding="utf-8"),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger("QuantBot")

        self.stats = {
            "total_sinais": 0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "pnl_total": 0.0,
            "win_rate": 0.0
        }

    def log_trade_result(self, trade_data: dict):
        self.stats["total_trades"] += 1
        res = trade_data["resultado"]
        lucro = trade_data["lucro"]

        if res == "win":
            self.stats["wins"] += 1
        elif res == "loss":
            self.stats["losses"] += 1
        else:
            self.stats["draws"] += 1

        self.stats["pnl_total"] += lucro
        
        valid_trades = self.stats["wins"] + self.stats["losses"]
        if valid_trades > 0:
            self.stats["win_rate"] = (self.stats["wins"] / valid_trades) * 100

        # Grava snapshot em JSONL
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": datetime.now().isoformat(), "trade": trade_data, "stats": self.stats.copy()}) + "\n")

        self.logger.info(
            f" [RESULTADO] {trade_data['asset']} -> {res.upper()} (R$ {lucro:+.2f}) | "
            f"Win Rate: {self.stats['win_rate']:.1f}% | PnL Acumulado: R$ {self.stats['pnl_total']:+.2f}"
        )