import json
import statistics
import threading
import ollama
from database import obter_historico_comparavel, obter_historico_global_recente


class OllamaTradeDecisionEngine:
    MIN_COMPARABLE_RESULTS = 5
    OLLAMA_TIMEOUT_SECONDS = 10.0
    INFERENCE_LOCK_TIMEOUT_SECONDS = 1.0

    def __init__(self, model="llama3.2:1b"):
        self.model = model
        self.client = ollama.Client(timeout=self.OLLAMA_TIMEOUT_SECONDS)
        self._inference_lock = threading.Lock()

    def decidir(self, sinal):
        historico_real = obter_historico_comparavel(
            sinal["ativo"],
            sinal["sinal"],
            sinal["setup"],
            limite=20,
            origem="REAL",
        )
        historico_simulado = obter_historico_comparavel(
            sinal["ativo"],
            sinal["sinal"],
            sinal["setup"],
            limite=20,
            origem="SIMULADO",
        )
        wins = sum(item["resultado"] == "WIN" for item in historico_real)
        losses = sum(item["resultado"] == "LOSS" for item in historico_real)
        draws = sum(item["resultado"] == "DRAW" for item in historico_real)
        decisive = wins + losses

        simulated_wins = sum(
            item["resultado"] == "PAPER_WIN" for item in historico_simulado
        )
        simulated_losses = sum(
            item["resultado"] == "PAPER_LOSS" for item in historico_simulado
        )
        simulated_decisive = simulated_wins + simulated_losses

        if decisive < self.MIN_COMPARABLE_RESULTS:
            return (
                "OPERAR_ANALYZER",
                f"Histórico real comparável insuficiente ({decisive}/"
                f"{self.MIN_COMPARABLE_RESULTS} resultados decisivos); "
                f"{simulated_decisive} resultados simulados ficam separados "
                "e não contam para a decisão.",
            )

        win_rate = wins / decisive
        payout = float(sinal["payout"])
        break_even = 1 / (1 + payout / 100)
        expected_value = win_rate * payout / 100 - (1 - win_rate)

        if expected_value <= 0:
            return (
                "PULAR_EXPECTATIVA",
                f"Expectativa histórica real negativa: {win_rate:.1%} de acerto; "
                f"equilíbrio estimado em {break_even:.1%} para payout de "
                f"{payout:.1f}%.",
            )

        ultimos_erros = self._resumir_erros(historico_real)
        payouts = [
            float(item["payout"])
            for item in historico_real
            if item.get("payout") is not None
        ]
        payout_medio = statistics.fmean(payouts) if payouts else None
        amplitudes_perdas = [
            amplitude
            for item in historico_real
            if item["resultado"] == "LOSS"
            for amplitude in [self._amplitude_relativa(item.get("analise", {}))]
            if amplitude is not None
        ]
        amplitude_media_perdas = (
            statistics.fmean(amplitudes_perdas) if amplitudes_perdas else None
        )

        # Pega o histórico global recente para o SLM aprender com o comportamento macro
        historico_global = obter_historico_global_recente(limite=25)
        total_global_losses = sum(1 for item in historico_global if item["resultado"] in ("LOSS", "PAPER_LOSS"))
        total_global_wins = sum(1 for item in historico_global if item["resultado"] in ("WIN", "PAPER_WIN"))

        dados_decisao = {
            "sinal_atual": {
                "ativo": sinal["ativo"],
                "direcao": sinal["sinal"],
                "setup": sinal["setup"],
                "payout": payout,
                "analise": sinal.get("analise", {}),
            },
            "historico_especifco_ativo": {
                "amostras_decisivas": decisive,
                "taxa_acerto": round(win_rate, 4),
                "ultimas_derrotas": ultimos_erros,
            },
            "aprendizado_macro_global": {
                "total_operacoes_analisadas": len(historico_global),
                "vitorias_recentes_gerais": total_global_wins,
                "derrotas_recentes_gerais": total_global_losses,
                "ultimos_resultados_gerais": historico_global[:10] # Amostra das 10 operações mais recentes da banca inteira
            }
        }

        prompt = (
            "Você é um Risk Manager e analista quantitativo sênior focado em alta assertividade (>70%). "
            "Além de avaliar o sinal técnico atual e o histórico específico do ativo, analise o 'aprendizado_macro_global' "
            "para entender o comportamento recente da banca (contas real e practice/demo). "
            "Se houver um padrão de perdas recorrentes na macro-operação que indique condições de risco sistêmico, "
            "você deve retornar 'PULAR'. Seja extremamente rigoroso para proteger o capital. "
            "Responda exclusivamente em formato JSON puro, sem textos adicionais, seguindo esta estrutura exata: "
            '{"decisao": "OPERAR" ou "PULAR", "motivo": "Análise crítica justificando com base no ativo e no macro"}. '
            f"Dados operacionais e histórico de aprendizado global: {json.dumps(dados_decisao, ensure_ascii=False, sort_keys=True)}"
        )

        if not self._inference_lock.acquire(
            timeout=self.INFERENCE_LOCK_TIMEOUT_SECONDS
        ):
            return (
                "PULAR_OLLAMA",
                "Decisão suspensa: Ollama ocupado e limite de espera pelo "
                "processamento foi excedido.",
            )

        try:
            resposta = self.client.generate(
                model=self.model,
                prompt=prompt,
                format="json",
                options={"temperature": 0.1, "num_predict": 150},
            )
            resultado = json.loads(resposta.response)
            decisao = resultado.get("decisao")
            motivo = resultado.get("motivo")

            if decisao not in ("OPERAR", "PULAR") or not isinstance(motivo, str) or not motivo.strip():
                raise ValueError("Ollama retornou uma decisão fora do formato esperado.")
        except Exception as erro:
            return (
                "PULAR_OLLAMA",
                f"Decisão automática suspensa: falha ou resposta inválida do Ollama: {erro}",
            )
        finally:
            self._inference_lock.release()

        if decisao == "PULAR":
            return "PULAR_OLLAMA", motivo.strip()
        return "OPERAR_OLLAMA", motivo.strip()

    @staticmethod
    def _resumir_erros(historico):
        erros = []
        for item in historico:
            if item["resultado"] != "LOSS":
                continue
            analise = item.get("analise") or {}
            amplitude_relativa = OllamaTradeDecisionEngine._amplitude_relativa(analise)
            erros.append({
                "timestamp": item.get("timestamp"),
                "payout": item.get("payout"),
                "motivo_registrado": item.get("motivo"),
                "contexto_tecnico": {
                    chave: analise.get(chave)
                    for chave in (
                        "tendencia_m15",
                        "distancia_fibo_618",
                        "amplitude_janela_m5",
                        "fechamento_m5",
                    )
                    if analise.get(chave) is not None
                },
                "amplitude_relativa_m5_pct": (
                    round(amplitude_relativa, 4)
                    if amplitude_relativa is not None else None
                ),
            })
            if len(erros) == 5:
                break
        return erros

    @staticmethod
    def _amplitude_relativa(analise):
        try:
            fechamento = abs(float(analise["fechamento_m5"]))
            amplitude = float(analise["amplitude_janela_m5"])
        except (KeyError, TypeError, ValueError):
            return None
        if fechamento == 0:
            return None
        return amplitude / fechamento * 100