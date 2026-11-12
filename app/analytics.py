# app/analytics.py
import logging
import json
import math
from datetime import datetime
from pathlib import Path

class PerformanceTracker:
    def __init__(self, log_dir="logs"):
        Path(log_dir).mkdir(exist_ok=True)
        self.log_file = Path(log_dir) / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        self.stats_file = Path(log_dir) / f"metrics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        self.setup_logger()
        
        # Base de dados estatísticos
        self.trades = []
        self.wins = 0
        self.losses = 0
        self.draws = 0
        self.total_pnl = 0.0
        self.peak_pnl = 0.0
        self.max_drawdown = 0.0

    def setup_logger(self):
        self.logger = logging.getLogger("TradingEngine")
        self.logger.setLevel(logging.INFO)
        
        formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        
        # Log em arquivo
        fh = logging.FileHandler(self.log_file)
        fh.setFormatter(formatter)
        
        # Log em console
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        
        self.logger.addHandler(fh)
        self.logger.addHandler(ch)

    def log_trade(self, asset, setup, direction, stake, payout, result, pnl):
        if result == 'win':
            self.wins += 1
        elif result == 'loss':
            self.losses += 1
        else:
            self.draws += 1
            
        self.total_pnl += pnl
        
        # Atualização de Drawdown
        if self.total_pnl > self.peak_pnl:
            self.peak_pnl = self.total_pnl
        drawdown = self.peak_pnl - self.total_pnl
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

        trade_data = {
            'timestamp': datetime.now().isoformat(),
            'asset': asset,
            'setup': setup,
            'direction': direction,
            'stake': stake,
            'payout': payout,
            'result': result,
            'pnl': pnl
        }
        self.trades.append(trade_data)
        
        # Emite relatório no log
        winrate, profit_factor, expectancy = self.calculate_metrics()
        
        self.logger.info(
            f"TRADE REGISTRADO | {asset} | {direction} | Result: {result.upper()} ({pnl:+.2f}) | "
            f"WinRate: {winrate:.1f}% | ProfitFactor: {profit_factor:.2f} | Expectancy: R$ {expectancy:+.2f} | "
            f"MaxDD: R$ {self.max_drawdown:.2f}"
        )
        self._export_json()

    def calculate_metrics(self):
        total_finished = self.wins + self.losses
        if total_finished == 0:
            return 0.0, 0.0, 0.0
            
        winrate = (self.wins / total_finished) * 100.0
        
        gains = sum(t['pnl'] for t in self.trades if t['pnl'] > 0)
        losses = abs(sum(t['pnl'] for t in self.trades if t['pnl'] < 0))
        profit_factor = (gains / losses) if losses > 0 else (gains if gains > 0 else 0.0)
        
        # Expectativa Matemática per Trade: E = (Win% * AvgWin) - (Loss% * AvgLoss)
        avg_win = (gains / self.wins) if self.wins > 0 else 0.0
        avg_loss = (losses / self.losses) if self.losses > 0 else 0.0
        win_prob = self.wins / total_finished
        loss_prob = self.losses / total_finished
        
        expectancy = (win_prob * avg_win) - (loss_prob * avg_loss)
        
        return winrate, profit_factor, expectancy

    def _export_json(self):
        winrate, pf, exp = self.calculate_metrics()
        data = {
            'summary': {
                'total_trades': len(self.trades),
                'wins': self.wins,
                'losses': self.losses,
                'draws': self.draws,
                'win_rate': round(winrate, 2),
                'profit_factor': round(pf, 2),
                'expectancy_per_trade': round(exp, 2),
                'total_pnl': round(self.total_pnl, 2),
                'max_drawdown': round(self.max_drawdown, 2)
            },
            'trades': self.trades
        }
        with open(self.stats_file, 'w') as f:
            json.dump(data, f, indent=4)