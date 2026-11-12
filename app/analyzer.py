import time
from datetime import datetime

from app.strategies import StrategyEngine


class MarketAnalyzer:
    CANDLE_INTERVALS = {
        "M1": 60,
        "M5": 300,
        "M15": 900,
    }
    MAX_SIGNAL_AGE_SECONDS = 300

    def __init__(self, broker, config, logger):
        self.broker = broker
        self.config = config
        self.logger = logger

    @staticmethod
    def _timestamp_inicio_candle(candle):
        try:
            timestamp = int(candle["from"])
        except (KeyError, TypeError, ValueError):
            return None
        return timestamp // 1000 if timestamp > 10**12 else timestamp

    @classmethod
    def _filtrar_candles_fechados(cls, candles, intervalo, agora):
        fechados = []
        for candle in candles or []:
            inicio = cls._timestamp_inicio_candle(candle)
            if inicio is not None and inicio + intervalo <= agora:
                fechados.append((inicio, candle))
        fechados.sort(key=lambda item: item[0])
        return [candle for _, candle in fechados]

    def _buscar_candles_fechados(self, ativo, intervalo, quantidade, agora):
        candles = self.broker.get_candles(ativo, intervalo, quantidade, agora)
        return self._filtrar_candles_fechados(candles, intervalo, agora)

    @staticmethod
    def _formatar_volatilidade(valor):
        return "indisponível" if valor is None else f"{valor:.4f}x"

    @staticmethod
    def _formatar_preco(candle, campo):
        try:
            return f"{float(candle[campo]):.8f}"
        except (KeyError, TypeError, ValueError):
            return f"{candle.get(campo)!r}"

    def analisar_contingencia_m15(self, item_ativo):
        ativo = item_ativo["ativo"]
        ts_atual = int(self.broker.get_ts())
        candles_m15 = self._buscar_candles_fechados(
            ativo, self.CANDLE_INTERVALS["M15"], 60, ts_atual
        )
        if len(candles_m15) < 50:
            self.logger.warning(
                "[CONTINGÊNCIA STARTUP %s] Sem direção: candles M15 insuficientes "
                "(%s/50).",
                ativo,
                len(candles_m15),
            )
            return None

        tendencia = StrategyEngine.checar_tendencia(candles_m15[-50:])
        if tendencia == "lateral":
            self.logger.warning(
                "[CONTINGÊNCIA STARTUP %s] Sem ordem: tendência M15 lateral, "
                "sem direção CALL/PUT definida.",
                ativo,
            )
            return None

        candles_m1 = []
        candles_m5 = []
        for timeframe, intervalo, quantidade in (
            ("M1", self.CANDLE_INTERVALS["M1"], 30),
            ("M5", self.CANDLE_INTERVALS["M5"], 25),
        ):
            try:
                candles = self._buscar_candles_fechados(
                    ativo, intervalo, quantidade, ts_atual
                )
            except Exception:
                self.logger.exception(
                    "[CONTINGÊNCIA STARTUP %s] Falha buscando candles %s; "
                    "a expiração prioritária de 1 minuto será mantida.",
                    ativo,
                    timeframe,
                )
                candles = []
            if timeframe == "M1":
                candles_m1 = candles
            else:
                candles_m5 = candles

        expiracao_minutos, metricas = StrategyEngine.selecionar_expiracao(
            candles_m1, candles_m5, candles_m15[-50:]
        )
        executar_em = (ts_atual // 60 + 1) * 60
        expira_em = executar_em + expiracao_minutos * 60
        ema_50_m15 = StrategyEngine.calcular_ema(
            [candle["close"] for candle in candles_m15[-50:]], 50
        )
        humor_mercado = StrategyEngine.avaliar_humor_mercado(
            candles_m15[-8:], metricas
        )
        candle_m15 = candles_m15[-1]
        direcao = "CALL" if tendencia == "alta" else "PUT"
        setup = f"StartupM15Trend_BINARY_{expiracao_minutos}M"
        metricas_texto = ", ".join(
            f"{timeframe}={self._formatar_volatilidade(metricas[chave])}"
            for timeframe, chave in (
                ("M1", "volatilidade_relativa_m1"),
                ("M5", "volatilidade_relativa_m5"),
                ("M15", "volatilidade_relativa_m15"),
            )
        )
        self.logger.warning(
            "[CONTINGÊNCIA STARTUP %s] Entrada por tendência M15: direção=%s | "
            "fechamento=%.8f | EMA50=%.8f | volatilidade=[%s] | "
            "expiração=%smin | executar_em=%s | expira_em=%s. "
            "Esta entrada não passou pela retração M5 nem pelo filtro do SLM.",
            ativo,
            direcao,
            float(candle_m15["close"]),
            ema_50_m15,
            metricas_texto,
            expiracao_minutos,
            executar_em,
            expira_em,
        )
        return {
            "ativo": ativo,
            "tipo": item_ativo["tipo"],
            "sinal": direcao,
            "executar_em": executar_em,
            "expira_em": expira_em,
            "expiracao_minutos": expiracao_minutos,
            "payout": float(item_ativo["payout"]),
            "setup": setup,
            "timeframe_referencia": "M15_CONTINGENCIA",
            "fechamento_referencia": ts_atual,
            "analise": {
                "tendencia_m15": tendencia,
                "fechamento_m15": float(candle_m15["close"]),
                "ema_50_m15": ema_50_m15,
                "origem_sinal": "CONTINGENCIA_STARTUP_M15",
                "humor_mercado": humor_mercado,
                **metricas,
            },
            "humor_mercado": humor_mercado,
        }

    def analisar_ativo(self, item_ativo):
        ativo = item_ativo.get("ativo", "<ativo desconhecido>")
        try:
            return self._analisar_ativo(item_ativo)
        except Exception:
            self.logger.exception(
                "[%s] Falha inesperada durante análise técnica do ativo.", ativo
            )
            raise

    def _analisar_ativo(self, item_ativo):
        ativo = item_ativo["ativo"]
        tipo = item_ativo["tipo"]
        ts_atual = int(self.broker.get_ts())

        candles_m15 = self._buscar_candles_fechados(
            ativo, self.CANDLE_INTERVALS["M15"], 60, ts_atual
        )
        if len(candles_m15) < 50:
            self.logger.warning(
                f"[{ativo}] Candles M15 fechados insuficientes para análise "
                f"({len(candles_m15)}/50)."
            )
            return None

        tendencia = StrategyEngine.checar_tendencia(candles_m15[-50:])
        if tendencia == "lateral":
            self.logger.info(
                f"[{ativo}] Descartado: mercado sem tendência clara (lateral em M15)."
            )
            return None

        candles_m5 = self._buscar_candles_fechados(
            ativo, self.CANDLE_INTERVALS["M5"], 25, ts_atual
        )
        if len(candles_m5) < 21:
            self.logger.warning(
                f"[{ativo}] Candles M5 fechados insuficientes para confirmação "
                f"({len(candles_m5)}/21)."
            )
            return None

        candle_referencia = candles_m5[-1]
        inicio_referencia = self._timestamp_inicio_candle(candle_referencia)
        if inicio_referencia is None:
            self.logger.warning(
                f"[{ativo}] Candle M5 sem timestamp; sinal descartado por segurança."
            )
            return None
        fechamento_referencia = inicio_referencia + self.CANDLE_INTERVALS["M5"]
        idade_sinal = ts_atual - fechamento_referencia
        if idade_sinal < 0 or idade_sinal > self.MAX_SIGNAL_AGE_SECONDS:
            self.logger.info(
                f"[{ativo}] Descarte por Idade: idade_sinal={idade_sinal}s "
                f"(limite permitido: {self.MAX_SIGNAL_AGE_SECONDS}s); "
                f"fechamento_referencia={fechamento_referencia} "
                f"({datetime.fromtimestamp(fechamento_referencia).astimezone().isoformat(timespec='seconds')}), "
                f"agora={ts_atual} "
                f"({datetime.fromtimestamp(ts_atual).astimezone().isoformat(timespec='seconds')})."
            )
            return None

        avaliacao = StrategyEngine.avaliar_reversao_m5(candles_m5, tendencia)
        if not avaliacao:
            vela = "/".join(
                self._formatar_preco(candle_referencia, campo)
                for campo in ("open", "max", "min", "close")
            )
            self.logger.info(
                f"[{ativo}] Descarte por Retração M5: tendência={tendencia.upper()} | "
                f"vela={vela} "
                "(abertura/máxima/mínima/fechamento) | "
                f"timestamp_fechamento={fechamento_referencia} | "
                f"idade_sinal={idade_sinal}s."
            )
            return None

        candles_m1 = self._buscar_candles_fechados(
            ativo, self.CANDLE_INTERVALS["M1"], 30, ts_atual
        )
        expiracao_minutos, metricas_volatilidade = StrategyEngine.selecionar_expiracao(
            candles_m1, candles_m5, candles_m15[-50:]
        )

        executar_em = (ts_atual // 60 + 1) * 60
        expira_em = int(executar_em + expiracao_minutos * 60)
        metricas_texto = " | ".join(
            f"{timeframe}={self._formatar_volatilidade(metricas_volatilidade[chave])}"
            for timeframe, chave in (
                ("M1", "volatilidade_relativa_m1"),
                ("M5", "volatilidade_relativa_m5"),
                ("M15", "volatilidade_relativa_m15"),
            )
        )
        executar_em_local = datetime.fromtimestamp(executar_em).astimezone().isoformat(
            timespec="seconds"
        )
        expira_em_local = datetime.fromtimestamp(expira_em).astimezone().isoformat(
            timespec="seconds"
        )
        self.logger.info(
            f"[PRÉ-ANÁLISE {ativo}] Volatilidade relativa: {metricas_texto} | "
            f"expiração prioritária escolhida: {expiracao_minutos}min | "
            f"execução={executar_em} ({executar_em_local}) | "
            f"expiração={expira_em} ({expira_em_local})."
        )
        ema_50_m15 = StrategyEngine.calcular_ema(
            [candle["close"] for candle in candles_m15[-50:]],
            50,
        )
        humor_mercado = StrategyEngine.avaliar_humor_mercado(
            candles_m15[-8:], metricas_volatilidade
        )
        candle_atual = candles_m5[-1]
        setup = (
            f"Fibo618_M5_{tendencia}_BINARY_{expiracao_minutos}M"
        )

        self.logger.info(
            f"[SINAL DETECTADO] {ativo} | Direção: {avaliacao['direcao']} | "
            f"Setup: {setup} | Referência: fechamento M5 "
            f"{time.strftime('%H:%M:%S', time.localtime(fechamento_referencia))} | "
            f"Expiração solicitada: {expiracao_minutos}min | "
            f"Volatilidade relativa: {metricas_texto} | "
            f"Execução={executar_em} ({executar_em_local}) | "
            f"Expiração={expira_em} ({expira_em_local})"
        )

        return {
            "ativo": ativo,
            "tipo": tipo,
            "sinal": avaliacao["direcao"],
            "executar_em": executar_em,
            "expira_em": expira_em,
            "expiracao_minutos": expiracao_minutos,
            "payout": float(item_ativo["payout"]),
            "setup": setup,
            "timeframe_referencia": "M5",
            "fechamento_referencia": fechamento_referencia,
            "analise": {
                "tendencia_m15": tendencia,
                "fechamento_m15": float(candles_m15[-1]["close"]),
                "ema_50_m15": ema_50_m15,
                "abertura_m5": float(candle_atual["open"]),
                "maxima_m5": float(candle_atual["max"]),
                "minima_m5": float(candle_atual["min"]),
                "fechamento_m5": float(candle_atual["close"]),
                "maxima_janela_m5": avaliacao["maxima_janela_m5"],
                "minima_janela_m5": avaliacao["minima_janela_m5"],
                "fibo_618": avaliacao["fibo_618"],
                "fibo_500": avaliacao["fibo_500"],
                "delta_entrada": avaliacao["delta_entrada"],
                "distancia_fibo_618": abs(
                    float(candle_atual["close"]) - avaliacao["fibo_618"]
                ),
                "amplitude_janela_m5": avaliacao["amplitude_janela_m5"],
                "humor_mercado": humor_mercado,
                **metricas_volatilidade,
            },
            "humor_mercado": humor_mercado,
        }
