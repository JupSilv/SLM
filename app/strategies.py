import logging
import math
import statistics


logger = logging.getLogger(__name__)


class StrategyEngine:
    EXPIRACAO_PRIORITARIA_MINUTOS = 1
    MARGEM_TOQUE_FIBONACCI = 0.02
    MAX_ROMPIMENTO_FIBONACCI = 0.03
    MAX_EXTENSAO_FECHAMENTO_FIBONACCI = 0.04
    MIN_M15_DIRECTIONAL_EFFICIENCY = 0.20
    MAX_RELATIVE_VOLATILITY = 2.50

    @staticmethod
    def calcular_ema(prices, period):
        if not prices:
            raise ValueError("É necessário ao menos um preço para calcular a EMA.")
        if period < 1:
            raise ValueError("O período da EMA deve ser maior que zero.")
        alpha = 2 / (period + 1)
        ema = [float(prices[0])]
        for price in prices[1:]:
            ema.append(float(price) * alpha + ema[-1] * (1 - alpha))
        return ema[-1]

    @staticmethod
    def checar_tendencia(candles_m15):
        """Define o regime de M15 usando os candles recebidos, que devem estar fechados."""
        closes = [float(c["close"]) for c in candles_m15]
        if len(closes) < 50:
            raise ValueError("São necessários 50 candles M15 fechados para a tendência.")
        ema_50 = StrategyEngine.calcular_ema(closes, 50)
        preco_atual = closes[-1]

        if preco_atual > ema_50:
            return "alta"
        if preco_atual < ema_50:
            return "baixa"
        return "lateral"

    @staticmethod
    def retracao_fibonacci(high, low, direcao="alta"):
        """Calcula os níveis de retração 61,8% e 50% para uma faixa válida."""
        high = float(high)
        low = float(low)
        if high <= low:
            raise ValueError("A máxima deve ser maior que a mínima para calcular Fibonacci.")
        diff = high - low
        if direcao == "alta":
            return {
                "fibo_618": high - diff * 0.618,
                "fibo_500": high - diff * 0.5,
            }
        if direcao == "baixa":
            return {
                "fibo_618": low + diff * 0.618,
                "fibo_500": low + diff * 0.5,
            }
        raise ValueError("A direção deve ser 'alta' ou 'baixa'.")

    @staticmethod
    def volatilidade_relativa(candles, recente=3):
        """Compara a amplitude percentual recente com a mediana anterior."""
        if len(candles) < recente + 5:
            return None

        amplitudes = []
        for candle in candles:
            try:
                close = abs(float(candle["close"]))
                high = float(candle["max"])
                low = float(candle["min"])
            except (KeyError, TypeError, ValueError):
                return None
            if close == 0 or high < low:
                return None
            amplitudes.append((high - low) / close)

        baseline = statistics.median(amplitudes[:-recente])
        if baseline <= 0:
            return None
        amplitude_recente = statistics.fmean(amplitudes[-recente:])
        return amplitude_recente / baseline

    @classmethod
    def selecionar_expiracao(cls, candles_m1, candles_m5, candles_m15):
        """Prioriza expiração de um minuto e retorna volatilidades para auditoria."""
        vola_m1 = cls.volatilidade_relativa(candles_m1)
        vola_m5 = cls.volatilidade_relativa(candles_m5)
        vola_m15 = cls.volatilidade_relativa(candles_m15)

        metricas = {
            "volatilidade_relativa_m1": vola_m1,
            "volatilidade_relativa_m5": vola_m5,
            "volatilidade_relativa_m15": vola_m15,
        }
        return cls.EXPIRACAO_PRIORITARIA_MINUTOS, metricas

    @classmethod
    def avaliar_humor_mercado(cls, candles_m15, metricas):
        if len(candles_m15) < 8:
            return {
                "favoravel": False,
                "motivo": "Dados M15 insuficientes para avaliar o contexto.",
                "eficiencia_direcional_m15": None,
                "volatilidade_relativa_maxima": None,
            }

        try:
            fechamentos = [
                float(candle["close"]) for candle in candles_m15[-8:]
            ]
            if not all(math.isfinite(valor) for valor in fechamentos):
                raise ValueError("Fechamentos M15 não finitos.")
        except (KeyError, TypeError, ValueError):
            return {
                "favoravel": False,
                "motivo": "Dados M15 inválidos para avaliar o contexto.",
                "eficiencia_direcional_m15": None,
                "volatilidade_relativa_maxima": None,
            }

        movimentos = [
            abs(atual - anterior)
            for anterior, atual in zip(fechamentos, fechamentos[1:])
        ]
        movimento_total = sum(movimentos)
        eficiencia = (
            abs(fechamentos[-1] - fechamentos[0]) / movimento_total
            if movimento_total > 0
            else 0.0
        )
        volatilidades = []
        for chave in (
            "volatilidade_relativa_m1",
            "volatilidade_relativa_m5",
            "volatilidade_relativa_m15",
        ):
            valor = metricas.get(chave)
            if valor is None:
                continue
            try:
                valor = float(valor)
            except (TypeError, ValueError):
                continue
            if math.isfinite(valor):
                volatilidades.append(valor)
        volatilidade_maxima = max(volatilidades) if volatilidades else None

        if volatilidade_maxima is not None and (
            volatilidade_maxima > cls.MAX_RELATIVE_VOLATILITY
        ):
            favoravel = False
            motivo = (
                "Volatilidade extrema: "
                f"{volatilidade_maxima:.2f}x do padrão recente "
                f"(limite {cls.MAX_RELATIVE_VOLATILITY:.2f}x)."
            )
        elif eficiencia < cls.MIN_M15_DIRECTIONAL_EFFICIENCY:
            favoravel = False
            motivo = (
                "Direcionalidade M15 fraca: eficiência "
                f"{eficiencia:.2f} abaixo de "
                f"{cls.MIN_M15_DIRECTIONAL_EFFICIENCY:.2f}."
            )
        else:
            favoravel = True
            motivo = (
                f"Contexto local favorável: eficiência M15={eficiencia:.2f}; "
                + (
                    f"volatilidade relativa máxima={volatilidade_maxima:.2f}x."
                    if volatilidade_maxima is not None
                    else "volatilidade relativa indisponível."
                )
            )

        return {
            "favoravel": favoravel,
            "motivo": motivo,
            "eficiencia_direcional_m15": eficiencia,
            "volatilidade_relativa_maxima": volatilidade_maxima,
        }

    @staticmethod
    def avaliar_reversao_m5(candles_m5, tendencia):
        """Exige toque controlado na Fibo e candle M5 fechado confirmando o retorno."""
        if tendencia not in ("alta", "baixa") or len(candles_m5) < 21:
            logger.info(
                "[RETRAÇÃO M5] Avaliação indisponível: tendência=%s; "
                "candles recebidos=%s (mínimo=21).",
                tendencia,
                len(candles_m5),
            )
            return None

        anteriores = candles_m5[-21:-1]
        candle = candles_m5[-1]
        try:
            max_periodo = max(float(item["max"]) for item in anteriores)
            min_periodo = min(float(item["min"]) for item in anteriores)
            abertura = float(candle["open"])
            maxima = float(candle["max"])
            minima = float(candle["min"])
            fechamento = float(candle["close"])
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "[RETRAÇÃO M5] Candle inválido; não foi possível calcular os "
                "valores da retração. tendência=%s.",
                tendencia,
                exc_info=True,
            )
            return None

        amplitude = max_periodo - min_periodo
        if amplitude <= 0 or minima > maxima:
            logger.info(
                "[RETRAÇÃO M5] Faixa inválida: tendência=%s | "
                "máxima_janela=%.8f | mínima_janela=%.8f | "
                "máxima_candle=%.8f | mínima_candle=%.8f | amplitude=%.8f.",
                tendencia,
                max_periodo,
                min_periodo,
                maxima,
                minima,
                amplitude,
            )
            return None

        fibs = StrategyEngine.retracao_fibonacci(
            max_periodo, min_periodo, tendencia
        )
        nivel = fibs["fibo_618"]
        delta = max(
            amplitude * StrategyEngine.MARGEM_TOQUE_FIBONACCI,
            abs(nivel) * 0.000015,
        )
        rompimento_maximo = amplitude * StrategyEngine.MAX_ROMPIMENTO_FIBONACCI
        extensao_maxima_fechamento = (
            amplitude * StrategyEngine.MAX_EXTENSAO_FECHAMENTO_FIBONACCI
        )

        if tendencia == "alta":
            tocou_zona = minima <= nivel + delta
            sem_rompimento = minima >= nivel - rompimento_maximo
            confirmou_retorno = fechamento > nivel and fechamento > abertura
            sem_extensao = fechamento <= nivel + extensao_maxima_fechamento
            direcao = "CALL"
        else:
            tocou_zona = maxima >= nivel - delta
            sem_rompimento = maxima <= nivel + rompimento_maximo
            confirmou_retorno = fechamento < nivel and fechamento < abertura
            sem_extensao = fechamento >= nivel - extensao_maxima_fechamento
            direcao = "PUT"

        if not (
            tocou_zona
            and sem_rompimento
            and confirmou_retorno
            and sem_extensao
        ):
            logger.info(
                "[RETRAÇÃO M5] Rejeição: tendência=%s | abertura=%.8f | "
                "fechamento=%.8f | fibo_618=%.8f | mínima_candle=%.8f | "
                "máxima_candle=%.8f | delta=%.8f | "
                "limite_rompimento=%.8f | limite_extensão=%.8f | "
                "tocou_zona=%s | sem_rompimento=%s | "
                "confirmou_retorno=%s | sem_extensao=%s.",
                tendencia,
                abertura,
                fechamento,
                nivel,
                minima,
                maxima,
                delta,
                rompimento_maximo,
                extensao_maxima_fechamento,
                tocou_zona,
                sem_rompimento,
                confirmou_retorno,
                sem_extensao,
            )
            return None

        return {
            "direcao": direcao,
            "fibo_618": nivel,
            "fibo_500": fibs["fibo_500"],
            "delta_entrada": delta,
            "amplitude_janela_m5": amplitude,
            "maxima_janela_m5": max_periodo,
            "minima_janela_m5": min_periodo,
        }

    @staticmethod
    def analisar_reversao_m5(candles_m5, tendencia):
        """Compatibilidade com chamadores antigos: retorna somente CALL/PUT ou None."""
        avaliacao = StrategyEngine.avaliar_reversao_m5(candles_m5, tendencia)
        return avaliacao["direcao"] if avaliacao else None
