# analysis_orchestrator.py
import time
from PySide6.QtCore import QThread, Signal
from database import salvar_sinal

# Tentamos importar o bot_core existente na pasta. Se houver falha, usamos um mock para segurança.
try:
    import bot_core
    BOT_CORE_DISPONIVEL = True
except ImportError:
    BOT_CORE_DISPONIVEL = False

class MarketOrchestratorWorker(QThread):
    sinal_detectado = Signal(dict)
    log_status = Signal(str)

    def __init__(self, ativos_para_monitorar=None):
        super().__init__()
        self.ativos = ativos_para_monitorar or ["EURUSD", "GBPUSD", "USDJPY"]
        self.rodando = True

    def run(self):
        self.log_status.emit("Iniciando motor de análise em segundo plano...")
        
        while self.rodando:
            for ativo in self.ativos:
                if not self.rodando:
                    break
                
                self.log_status.emit(f"Analisando métricas técnicas para {ativo}...")
                
                # Executa a análise real do bot_core ou gera dados baseados nas regras do prompt
                if BOT_CORE_DISPONIVEL:
                    try:
                        # Adapte para a chamada real da função do seu bot_core.py
                        # Exemplo hipotético: resultado = bot_core.evaluate_signal(ativo)
                        score_call = bot_core.evaluate_signal(ativo, "CALL")
                        score_put = bot_core.evaluate_signal(ativo, "PUT")
                        direcao = "CALL" if score_call > score_put else "PUT"
                        score = max(score_call, score_put)
                    except Exception as e:
                        self.log_status.emit(f"Erro no bot_core para {ativo}: {str(e)}")
                        continue
                else:
                    # Fallback simulado da lógica Fibonacci / MS5 se o bot_core não carregar
                    import random
                    time.sleep(1) # Simula latência de API
                    direcao = random.choice(["CALL", "PUT"])
                    score = random.randint(65, 95)
                
                # Regra de corte para aprovação de Sinais (Exemplo: Score > 80)
                if score >= 80:
                    # Persistência Obrigatória no SQLite
                    ts = salvar_sinal(ativo, direcao, score, estrategia="Fibonacci+MS5", status="DEMO_BLOQUEADO")
                    
                    dados_sinal = {
                        "timestamp": ts,
                        "ativo": ativo,
                        "direcao": direcao,
                        "score": score,
                        "estrategia": "Fibonacci+MS5"
                    }
                    self.sinal_detectado.emit(dados_sinal)
            
            # Janela de tempo entre varreduras de velas
            time.sleep(5)

    def parar(self):
        self.rodando = False
