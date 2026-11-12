import copy
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
import threading
from PySide6.QtCore import QThread, Signal
import iqoptionapi.constants as OP_code
from config import Config
from iqoptionapi.stable_api import IQ_Option
from iqoptionapi.expiration import get_expiration_time
from database import (
    atualizar_decisao_sinal,
    atualizar_resultado_sinal,
    atualizar_status_ordem_db,
    carregar_estado_risco,
    iniciar_banco,
    registrar_candidato_db,
    salvar_estado_risco,
)

# Lock global para evitar conflitos de requisições simultâneas na API da corretora
broker_lock = threading.Lock()
sell_response_lock = threading.Lock()

ORDER_DISPATCH_SECOND = Config.ORDER_DISPATCH_SECOND
if ORDER_DISPATCH_SECOND not in {"58", "59", "00"}:
    raise ValueError("ORDER_DISPATCH_SECOND deve ser 58, 59 ou 00.")


def _horario_disparo(horario_execucao, segundo_disparo):
    if segundo_disparo == 0:
        return horario_execucao
    return horario_execucao - (60 - segundo_disparo)


def _timestamp_broker(broker):
    obter_timestamp = getattr(broker, "get_server_timestamp", None)
    if callable(obter_timestamp):
        return float(obter_timestamp())
    obter_timestamp = getattr(broker, "get_ts", None)
    if callable(obter_timestamp):
        return float(obter_timestamp())
    return time.time()


def _registrar_falha_sem_dados_sensiveis(contexto, erro):
    pontos = []
    traceback_atual = erro.__traceback__
    while traceback_atual is not None:
        frame = traceback_atual.tb_frame
        pontos.append(
            f"{os.path.basename(frame.f_code.co_filename)}:"
            f"{traceback_atual.tb_lineno} ({frame.f_code.co_name})"
        )
        traceback_atual = traceback_atual.tb_next
    logging.error(
        "%s | tipo=%s | locais=%s | mensagem omitida para proteger dados",
        contexto,
        type(erro).__name__,
        " -> ".join(pontos),
    )


class BinaryOrderMonitor(QThread):
    """Monitors one Binary order independently of its asset-analysis worker."""
    log_append = Signal(str)
    _EXPECTED_PNL_KEYS = frozenset({
        "expected_profit",
        "profit_expected",
        "profit",
        "pnl",
        "pnl_realized",
        "profit_loss",
    })
    _SELL_PNL_KEYS = frozenset({
        "sell_profit",
        "profit_after_sale",
        "profit_after_sell",
        "sell_pnl",
    })
    _POSITION_DIAGNOSTIC_KEY_PARTS = (
        "profit",
        "pnl",
        "price",
        "sell",
        "buyback",
        "quote",
        "bid",
        "ask",
        "amount",
        "invest",
        "status",
        "order_id",
        "external_id",
        "option_id",
        "instrument",
        "close",
    )

    def __init__(
        self,
        broker,
        ativo,
        order_id,
        stake,
        expires_at,
        timestamp_sinal,
        payout=None,
        on_result=None,
        record_result=True,
        instrument_type="binary-option",
    ):
        super().__init__()
        self.broker = broker
        self.ativo = ativo
        self.order_id = order_id
        self.stake = float(stake)
        self.payout = float(payout) if payout is not None else None
        self.expires_at = int(expires_at)
        self.timestamp_sinal = timestamp_sinal
        self.on_result = on_result
        self.record_result = record_result
        self.instrument_type = instrument_type
        self.running = True
        self.completed = threading.Event()
        self.order_closed = threading.Event()

    def _notificar_resultado(self, resultado, pnl):
        if self.on_result is None:
            return
        try:
            self.on_result(resultado, pnl)
        except Exception as erro:
            logging.exception(
                "Falha processando resultado da ordem %s.", self.order_id
            )
            self.log_append.emit(
                f"⚠️ [RESULTADO {self.ativo}] Não foi possível processar "
                f"o resultado da ordem {self.order_id}: {erro}"
            )

    def _atualizacao_posicao(self):
        try:
            return self.broker.api.order_async.get(int(self.order_id), {}).get(
                "position-changed"
            )
        except (TypeError, ValueError):
            return None

    def _pnl_da_atualizacao(self, atualizacao):
        if not isinstance(atualizacao, dict):
            return {}
        valores = {}

        def coletar(campos, profundidade=0):
            if profundidade >= 6:
                return
            for chave, valor in campos.items():
                chave_normalizada = str(chave).lower().replace("-", "_")
                if chave_normalizada in self._EXPECTED_PNL_KEYS:
                    categoria = "L/P esperado"
                elif chave_normalizada in self._SELL_PNL_KEYS:
                    categoria = "L/P por venda"
                else:
                    categoria = None
                if categoria is not None and categoria not in valores:
                    try:
                        valores[categoria] = float(valor)
                    except (TypeError, ValueError):
                        pass
                if isinstance(valor, dict):
                    coletar(valor, profundidade + 1)
                elif isinstance(valor, (list, tuple)):
                    for item in valor:
                        if isinstance(item, dict):
                            coletar(item, profundidade + 1)

        coletar(atualizacao)
        return valores

    def _campos_diagnostico_posicao(self, atualizacao):
        if not isinstance(atualizacao, dict):
            return "sem atualização de posição recebida"

        campos = []

        def coletar(valor, caminho="", profundidade=0):
            if len(campos) >= 24 or profundidade >= 4:
                return
            if isinstance(valor, dict):
                for chave, item in valor.items():
                    nome = str(chave)
                    caminho_item = f"{caminho}.{nome}" if caminho else nome
                    nome_normalizado = nome.lower().replace("-", "_")
                    if any(
                        parte in nome_normalizado
                        for parte in self._POSITION_DIAGNOSTIC_KEY_PARTS
                    ) and not isinstance(item, (dict, list, tuple)):
                        texto = repr(item)
                        campos.append(f"{caminho_item}={texto[:120]}")
                    if isinstance(item, (dict, list, tuple)):
                        coletar(item, caminho_item, profundidade + 1)
                    if len(campos) >= 24:
                        return
            elif isinstance(valor, (list, tuple)):
                for indice, item in enumerate(valor[:3]):
                    coletar(item, f"{caminho}[{indice}]", profundidade + 1)
                    if len(campos) >= 24:
                        return

        coletar(atualizacao)
        if not campos:
            return "nenhum campo financeiro/estado reconhecido no evento"
        resumo = "; ".join(campos)
        return resumo[:1600]

    def _solicitar_atualizacoes_frequentes(self, atualizacao):
        mensagem = atualizacao.get("msg", {}) if isinstance(atualizacao, dict) else {}
        position_id = (
            mensagem.get("id") or int(self.order_id)
            if isinstance(mensagem, dict)
            else int(self.order_id)
        )
        self.broker.api.send_websocket_request(
            name="subscribeMessage",
            msg={
                "name": "subscribe-positions",
                "version": "1.0",
                "body": {"frequency": "frequent", "ids": [position_id]},
            },
        )

    def _vender_com_timeout(self, timeout=5):
        api = self.broker.api
        with sell_response_lock:
            with broker_lock:
                api.sold_options_respond = None
                api.sell_option(self.order_id)

            deadline = time.monotonic() + timeout
            while api.sold_options_respond is None and self.running:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timeout aguardando confirmação de venda da ordem {self.order_id}."
                    )
                time.sleep(0.05)

            if not self.running:
                return False, None
            resposta = api.sold_options_respond

        if isinstance(resposta, dict):
            if resposta.get("status") == 2000:
                return True, resposta
            mensagem = resposta.get("msg")
            ordem = (
                mensagem.get(str(self.order_id))
                if isinstance(mensagem, dict)
                else None
            )
            if (
                isinstance(ordem, dict)
                and str(ordem.get("id")) == str(self.order_id)
                and str(ordem.get("buyback_state", "")).lower()
                in {"sold", "closed"}
            ):
                return True, resposta
        return False, resposta

    def _buscar_historico(self, timeout=8):
        api = self.broker.api
        with broker_lock:
            api.position_history_v2 = None
            api.get_position_history_v2(self.instrument_type, 10, 0, 0, 0)
            deadline = time.monotonic() + timeout
            while api.position_history_v2 is None and self.running:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timeout buscando histórico Binary.")
                time.sleep(0.05)
            if not self.running:
                return False, None
            resposta = api.position_history_v2
        if resposta.get("status") == 2000:
            return True, resposta.get("msg")
        return False, None

    def _resultado_final(self):
        self.log_append.emit(
            f"[SESSÃO - {self.ativo}] Consultando resultado da ordem {self.order_id}..."
        )
        veredito = "INDETERMINADO"
        for _ in range(5):
            if not self.running:
                return
            try:
                sucesso, historico = self._buscar_historico()
                if sucesso and historico and "positions" in historico:
                    for posicao in historico["positions"]:
                        if (
                            str(posicao.get("external_id")) == str(self.order_id)
                            or str(posicao.get("id")) == str(self.order_id)
                        ):
                            pnl_real = float(posicao.get("close_profit", 0)) - float(
                                posicao.get("invest", 0)
                            )
                            veredito = (
                                "WIN" if pnl_real > 0 else
                                "LOSS" if pnl_real < 0 else "DRAW"
                            )
                            self.log_append.emit(
                                f"🏆 [RESULTADO - {self.ativo}] Ordem {self.order_id}: "
                                f"{veredito} (PnL: {pnl_real:+.2f})."
                            )
                            if self.record_result:
                                try:
                                    atualizar_resultado_sinal(
                                        self.timestamp_sinal,
                                        self.ativo,
                                        veredito,
                                        origem="REAL",
                                    )
                                except Exception:
                                    logging.exception(
                                        "Falha salvando o resultado da ordem %s.",
                                        self.order_id,
                                    )
                                    self.log_append.emit(
                                        f"⚠️ [RESULTADO {self.ativo}] Resultado da "
                                        f"ordem {self.order_id} foi confirmado, mas "
                                        "não foi salvo no banco."
                                    )
                            self._notificar_resultado(veredito, pnl_real)
                            return
                self.log_append.emit(
                    f"⚠️ [MONITORAMENTO {self.ativo}] Ordem {self.order_id} "
                    "ainda não consta no histórico."
                )
            except Exception as erro:
                logging.exception(
                    "Falha consultando o histórico da ordem %s.", self.order_id
                )
                self.log_append.emit(
                    f"⚠️ [MONITORAMENTO {self.ativo}] Erro consultando ordem "
                    f"{self.order_id}: {erro}"
                )
            time.sleep(2)

        if self.record_result:
            try:
                atualizar_resultado_sinal(
                    self.timestamp_sinal,
                    self.ativo,
                    veredito,
                    origem="REAL",
                )
            except Exception:
                logging.exception(
                    "Falha salvando resultado indeterminado da ordem %s.",
                    self.order_id,
                )
        self._notificar_resultado(veredito, 0.0)
        self.log_append.emit(
            f"⚠️ [RESULTADO {self.ativo}] Ordem {self.order_id} sem resultado "
            "confirmado; marcada como INDETERMINADO."
        )

    def run(self):
        try:
            self._monitorar()
        except Exception as erro:
            logging.exception(
                "Falha monitorando ordem %s do ativo %s.",
                self.order_id,
                self.ativo,
            )
            self.log_append.emit(
                f"⚠️ [MONITORAMENTO {self.ativo}] Falha na ordem "
                f"{self.order_id}: {erro}"
            )
            if self.running:
                try:
                    if self.record_result:
                        atualizar_resultado_sinal(
                            self.timestamp_sinal,
                            self.ativo,
                            "INDETERMINADO",
                            origem="REAL",
                        )
                    self._notificar_resultado("INDETERMINADO", 0.0)
                except Exception:
                    logging.exception(
                        "Falha registrando resultado indeterminado da ordem %s.",
                        self.order_id,
                    )
        finally:
            self.completed.set()

    def _monitorar(self):
        target = (
            self.stake * (self.payout / 100) * 0.50
            if self.payout is not None and 0 < self.payout <= 100
            else 0.0
        )
        if target <= 0:
            self.log_append.emit(
                f"⚠️ [MONITORAMENTO P/L - {self.ativo}] Ordem {self.order_id}: "
                "alvo de venda antecipada desativado; payout Binary ausente ou inválido."
            )
        self.log_append.emit(
            f"[MONITORAMENTO P/L - {self.ativo}] Ordem {self.order_id} "
            f"independente; payout observado {self.payout!r}% | "
            f"alvo de venda +R$ {target:.2f} "
            "(50% do lucro potencial estimado)."
        )

        last_update = None
        next_subscribe = 0.0
        next_wait_log = 0.0
        opened_at = time.monotonic()
        sale_attempted = False
        closed_early = False
        position_closed = False
        server_now = 0

        while self.running:
            now = time.monotonic()
            if now >= next_subscribe:
                try:
                    with broker_lock:
                        self._solicitar_atualizacoes_frequentes(
                            self._atualizacao_posicao()
                        )
                    next_subscribe = now + 45
                except Exception as erro:
                    logging.exception(
                        "Falha inscrevendo atualizações da ordem %s.",
                        self.order_id,
                    )
                    self.log_append.emit(
                        f"⚠️ [MONITORAMENTO P/L - {self.ativo}] Não foi possível "
                        f"inscrever a ordem {self.order_id}: {erro}"
                    )
                    next_subscribe = now + 10

            update = self._atualizacao_posicao()
            new_update = update is not None and update != last_update
            if new_update:
                last_update = copy.deepcopy(update)
                values = self._pnl_da_atualizacao(update)
                if values:
                    pnl_text = " | ".join(
                        f"{name}: {value:+.2f}" for name, value in values.items()
                    )
                    self.log_append.emit(
                        f"[MONITORAMENTO P/L - {self.ativo}] Ordem {self.order_id}: "
                        f"{pnl_text} | alvo: +{target:.2f}."
                    )
                else:
                    self.log_append.emit(
                        f"[MONITORAMENTO P/L - {self.ativo}] Ordem {self.order_id}: "
                        f"aguardando valor de P/L da corretora | alvo: +{target:.2f}. "
                        f"Diagnóstico da posição: "
                        f"{self._campos_diagnostico_posicao(update)}"
                    )

                message = update.get("msg", {})
                if isinstance(message, dict) and message.get("status") == "closed":
                    position_closed = True
                    break

                if (
                    target > 0
                    and values.get("L/P por venda", -1) >= target
                    and not sale_attempted
                    and now - opened_at >= 3
                ):
                    sale_attempted = True
                    self.log_append.emit(
                        f"🎯 [ALVO DE VENDA - {self.ativo}] Ordem {self.order_id} "
                        f"atingiu +{target:.2f}; solicitando encerramento."
                    )
                    try:
                        confirmed, response = self._vender_com_timeout()
                        if confirmed:
                            closed_early = True
                            self.log_append.emit(
                                f"✅ [VENDA ANTECIPADA - {self.ativo}] "
                                f"Ordem {self.order_id} encerrada pela corretora."
                            )
                            break
                        self.log_append.emit(
                            f"⚠️ [VENDA ANTECIPADA - {self.ativo}] Ordem "
                            f"{self.order_id} não encerrada; resposta: {response!r}"
                        )
                    except Exception as erro:
                        logging.exception(
                            "Falha solicitando venda antecipada da ordem %s.",
                            self.order_id,
                        )
                        self.log_append.emit(
                            f"⚠️ [VENDA ANTECIPADA - {self.ativo}] Falha na ordem "
                            f"{self.order_id}: {erro}"
                        )
            else:
                if now >= next_wait_log:
                    self.log_append.emit(
                        f"[MONITORAMENTO P/L - {self.ativo}] Ordem {self.order_id}: "
                        f"sem nova atualização de P/L da corretora; alvo de venda "
                        f"+{target:.2f}. A ordem permanece aberta."
                    )
                    next_wait_log = now + 15

            with broker_lock:
                server_now = int(_timestamp_broker(self.broker))
            if server_now >= self.expires_at + 5:
                break
            time.sleep(1)

        if self.running and (
            closed_early or position_closed or server_now >= self.expires_at + 5
        ):
            try:
                self._resultado_final()
            finally:
                self.order_closed.set()

    def stop(self):
        self.running = False


class AtivoTradingWorker(QThread):
    """Dedicated analyzer for one asset; each accepted order gets its own monitor."""
    log_append = Signal(str)
    MAX_MARTINGALE_LEVEL = 2

    def __init__(
        self,
        broker,
        item_ativo,
        risk_manager,
        analyzer,
        decision_engine,
        order_monitor_starter,
        has_open_order,
        startup_fallback=False,
        startup_order_claimed=None,
        startup_order_lock=None,
        startup_rank=0,
        startup_priority=None,
        startup_count=None,
        time_machine=None,
    ):
        super().__init__()
        self.broker = broker
        self.item = item_ativo
        self.ativo = item_ativo['ativo']
        self.risk = risk_manager
        self.analyzer = analyzer
        self.decision_engine = decision_engine
        self.order_monitor_starter = order_monitor_starter
        self.has_open_order = has_open_order
        self.startup_fallback = startup_fallback
        self.startup_order_claimed = (
            startup_order_claimed
            if startup_order_claimed is not None
            else threading.Event()
        )
        self.startup_order_lock = startup_order_lock or threading.Lock()
        self.startup_rank = startup_rank
        self.startup_priority = (
            startup_priority if startup_priority is not None else [0]
        )
        self.startup_count = startup_count if startup_count is not None else [1]
        self.time_machine = time_machine
        self.running = True
        self.last_heartbeat = time.monotonic()
        self._cooldown_log_until = 0.0
        
        # ✅ NOVO: Controle de Martingale e resiliência
        self.martingale_ativo = False
        self.martingale_nivel = 1
        self.martingale_stake_base = None
        self.martingale_prejuizo_acum = 0.0
        self.martingale_pnl_acum = 0.0
        self.martingale_tentativas = 0
        self.martingale_meta_ciclo = None
        
        # Timestamp da última ação crítica (para detectar travamentos)
        self.last_action_timestamp = time.monotonic()
        self.last_api_call_timestamp = time.monotonic()
        
        # Lock para operações críticas de Martingale
        self._martingale_lock = threading.Lock()
        
        # Estado do log de Martingale
        self._ultimo_log_martingale = 0.0

    def _startup_fallback_available(self):
        if (
            not self.startup_fallback
            or self.startup_order_claimed.is_set()
            or (
                self.risk.modo_observacao()
                and not self.risk.contingencia_apos_pausa_manual()
            )
        ):
            return False
        with self.startup_order_lock:
            return self.startup_priority[0] == self.startup_rank

    def _advance_startup_priority(self):
        with self.startup_order_lock:
            if (
                not self.startup_order_claimed.is_set()
                and self.startup_priority[0] == self.startup_rank
            ):
                self.startup_priority[0] = (
                    self.startup_priority[0] + 1
                ) % max(1, self.startup_count[0])

    def _claim_startup_order(self):
        with self.startup_order_lock:
            if (
                self.startup_order_claimed.is_set()
                or self.startup_priority[0] != self.startup_rank
            ):
                return False
            self.startup_order_claimed.set()
            return True

    def _aguardar_proxima_analise(self):
        with broker_lock:
            minuto_atual = _timestamp_broker(self.broker) // 60

        while self.running:
            self.last_heartbeat = time.monotonic()
            time.sleep(0.1)
            with broker_lock:
                novo_minuto = _timestamp_broker(self.broker) // 60
            if novo_minuto != minuto_atual:
                return

    def _monitorar_sinal_pulado(self, timestamp_sinal, sinal):
        horario_execucao = int(sinal["executar_em"])
        horario_fechamento = int(
            sinal.get("expira_em", horario_execucao + 60)
        )
        self.log_append.emit(
            f"[DEMO] Sinal virtual registrado: {self.ativo} "
            f"{sinal['sinal']} | setup={sinal.get('setup')} | "
            f"payout={sinal.get('payout')}% | "
            f"confiança={sinal.get('confianca', sinal.get('score'))}. "
            "Resultado ainda não confirmado."
        )

        while self.running:
            with broker_lock:
                agora_servidor = int(_timestamp_broker(self.broker))
            restante = horario_fechamento + 5 - agora_servidor
            if restante <= 0:
                break
            time.sleep(min(0.5, restante))

        if not self.running:
            return

        try:
            quantidade_candles = max(
                3, (horario_fechamento - horario_execucao) // 60 + 2
            )
            with broker_lock:
                candles = self._buscar_candles_com_timeout(
                    self.ativo, 60, quantidade_candles, horario_fechamento + 5
                )
            candle_entrada = next(
                (
                    item
                    for item in (candles or [])
                    if self._timestamp_candle(item) == horario_execucao
                ),
                None,
            )
            candle_fechamento = next(
                (
                    item
                    for item in (candles or [])
                    if self._timestamp_candle(item) == horario_fechamento - 60
                ),
                None,
            )
            if candle_entrada is None or candle_fechamento is None:
                resultado = "PAPER_INDETERMINADO"
                self.log_append.emit(
                    f"⚠️ [OBSERVAÇÃO {self.ativo}] Candles de entrada/expiração Binary "
                    "não encontrados; "
                    "sinal virtual não será usado para aprendizado."
                )
            else:
                abertura = float(candle_entrada["open"])
                fechamento = float(candle_fechamento["close"])
                if fechamento == abertura:
                    resultado = "PAPER_DRAW"
                elif (
                    sinal["sinal"] == "CALL" and fechamento > abertura
                ) or (
                    sinal["sinal"] == "PUT" and fechamento < abertura
                ):
                    resultado = "PAPER_WIN"
                else:
                    resultado = "PAPER_LOSS"
                self.log_append.emit(
                    f"[OBSERVAÇÃO {self.ativo}] Resultado virtual: {resultado}."
                )
            atualizar_resultado_sinal(
                timestamp_sinal, self.ativo, resultado, origem="SIMULADO"
            )
        except Exception as erro:
            logging.exception(
                "Falha calculando resultado virtual para %s.", self.ativo
            )
            atualizar_resultado_sinal(
                timestamp_sinal,
                self.ativo,
                "PAPER_INDETERMINADO",
                origem="SIMULADO",
            )
            self.log_append.emit(
                f"⚠️ [OBSERVAÇÃO {self.ativo}] Não foi possível medir o resultado virtual: {erro}"
            )

    def _iniciar_ciclo_martingale(self, stake_base, meta_ganho=None):
        del stake_base, meta_ganho
        self.log_append.emit(
            "[MARTINGALE] Recuperação desativada por configuração."
        )
        return False

    def _obter_stake_martingale(self, stake_base, nivel):
        del stake_base, nivel
        raise RuntimeError("Martingale está desativado.")

    def _registrar_tentativa_martingale(self, resultado, pnl, nivel):
        del resultado, pnl, nivel
        return False

    def _deve_encerrar_martingale(self):
        return True, "Martingale desativado por configuração."

    def reset_martingale_ciclo(self):
        """Reset seguro do ciclo Martingale."""
        with self._martingale_lock:
            if self.martingale_ativo:
                self.log_append.emit(
                    f"✅ [MARTINGALE FINALIZADO - {self.ativo}] "
                    f"Total de tentativas: {self.martingale_tentativas} | "
                    f"PnL líquido: R$ {self.martingale_pnl_acum:+.2f} | "
                    f"Próxima operação: STAKE BASE (sem Martingale)"
                )
            
            self.martingale_ativo = False
            self.martingale_nivel = 1
            self.martingale_stake_base = None
            self.martingale_prejuizo_acum = 0.0
            self.martingale_pnl_acum = 0.0
            self.martingale_tentativas = 0
            self.martingale_meta_ciclo = None

    def _log_status_entrada(self, stake, eh_martingale=False, nivel_mg=1):
        """Log descritivo antes de executar uma entrada."""
        tipo_operacao = f"MG{nivel_mg}" if eh_martingale else "BASE"
        banca_atual = self.broker.get_balance()
        meta = self.martingale_meta_ciclo if eh_martingale else None
        
        self.log_append.emit(
            f"➡️  [{self.ativo}] Preparando entrada | "
            f"Tipo: {tipo_operacao} | Stake: R$ {stake:.2f} | "
            f"Banca: R$ {banca_atual:.2f} | "
            f"{'Meta MG: R$ ' + f'{meta:.2f}' if meta else ''}"
        )
        self.last_action_timestamp = time.monotonic()

    def _log_resultado_ciclo(self, resultado, pnl, eh_martingale=False):
        """Log descritivo após resultado de uma entrada."""
        tipo = "MG" if eh_martingale else "BASE"
        emoji = "✅" if resultado == "WIN" else "❌" if resultado == "LOSS" else "➖"
        
        self.log_append.emit(
            f"{emoji} [{self.ativo}] Resultado {tipo}: {resultado} | "
            f"P/L: R$ {pnl:+.2f} | "
            f"{'PnL Ciclo: R$ ' + f'{self.martingale_pnl_acum:+.2f}' if eh_martingale else 'Banca atualizada'}"
        )

    def _executar_recuperacao(
            self,
            resultado,
            pnl,
            timestamp_sinal,
            sinal,
            stake_base,
            expiracao_minutos,
            instrument_type,
            nivel=1,
            prejuizos_acumulados=0.0,
            pnl_acumulado=0.0,
        ):
        del (
            timestamp_sinal,
            sinal,
            stake_base,
            expiracao_minutos,
            instrument_type,
            nivel,
            prejuizos_acumulados,
            pnl_acumulado,
        )
        self.reset_martingale_ciclo()
        if resultado == "LOSS":
            self.log_append.emit(
                f"[MARTINGALE] Recuperação desativada por configuração. "
                f"Prejuízo real registrado: R$ {pnl:.2f}; aguardando novo sinal."
            )
        elif resultado == "WIN":
            self.log_append.emit(
                "[MARTINGALE] WIN confirmado; nenhum ciclo de recuperação iniciado."
            )
        elif resultado == "INDETERMINADO":
            self.risk.bloquear_indeterminado(
                "Resultado da ordem não foi reconciliado."
            )

    @staticmethod
    def _classificar_pnl_ciclo(pnl):
        return "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "DRAW"

    def _finalizar_ciclo_martingale(self, resultado, timestamp_sinal):
        del resultado, timestamp_sinal

    def _buscar_candles_com_timeout(self, ativo, intervalo, quantidade, fim):
        active_id = OP_code.ACTIVES.get(ativo)
        if active_id is None:
            raise ValueError(f"Ativo sem identificador na API: {ativo}")

        api = self.broker.api
        api.candles.candles_data = None
        api.getcandles(active_id, intervalo, quantidade, fim)
        deadline = time.monotonic() + 8
        while api.candles.candles_data is None and self.running:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timeout ao buscar candle de {ativo}.")
            time.sleep(0.05)
        if not self.running:
            return None
        return api.candles.candles_data

    @staticmethod
    def _timestamp_candle(candle):
        timestamp = int(candle.get("from", 0))
        return timestamp // 1000 if timestamp > 10**12 else timestamp

    def run(self):
        try:
            self._executar_loop()
        except Exception as erro:
            logging.exception(
                "Falha fatal na thread de análise do ativo %s.", self.ativo
            )
            self.log_append.emit(
                f"⚠️ [THREAD {self.ativo}] Encerrada por falha inesperada: {erro}"
            )

    def _executar_loop(self):
        while self.running:
            try:
                self.last_heartbeat = time.monotonic()
                parada_diaria, motivo_parada = self.risk.deve_parar()
                if parada_diaria:
                    self.log_append.emit(
                        f"🛑 [RISCO {self.ativo}] {motivo_parada}; "
                        "análise de entradas suspensa."
                    )
                    break

                with broker_lock:
                    if not self.broker.check_connect():
                        conectado, motivo = self.broker.connect()
                        if not conectado:
                            logging.error(
                                "[%s] Reconexão recusada: %s", self.ativo, motivo
                            )
                        time.sleep(3)
                        continue
                    banca_atual = self.broker.get_balance()
                
                # Executa a análise técnica isolada deste ativo
                sinal = None
                try:
                    with broker_lock:
                        sinal = self.analyzer.analisar_ativo(self.item)
                    self.last_heartbeat = time.monotonic()
                except Exception as e:
                    logging.exception("Falha na análise do ativo %s.", self.ativo)
                    self.log_append.emit(
                        f"⚠️ [ANÁLISE {self.ativo}] Falha ao analisar o ativo: {e}"
                    )

                contingencia_startup = False
                if (
                    not sinal
                    and self._startup_fallback_available()
                ):
                    try:
                        with broker_lock:
                            sinal = self.analyzer.analisar_contingencia_m15(
                                self.item
                            )
                        contingencia_startup = sinal is not None
                        if sinal is None:
                            self._advance_startup_priority()
                    except Exception as erro:
                        self._advance_startup_priority()
                        logging.exception(
                            "Falha ao gerar contingência de startup para %s.",
                            self.ativo,
                        )
                        self.log_append.emit(
                            f"⚠️ [CONTINGÊNCIA {self.ativo}] "
                            f"Não foi possível gerar entrada: {erro}"
                        )

                if not sinal:
                    self.log_append.emit(
                        f"[SEM SINAL - {self.ativo}] Aguardando a próxima vela para analisar novamente."
                    )
                    self._aguardar_proxima_analise()
                    continue

                humor = sinal.get("humor_mercado")
                if not isinstance(humor, dict):
                    humor = sinal.get("analise", {}).get("humor_mercado")

                if not isinstance(humor, dict) or not humor.get("favoravel"):
                    if self._startup_fallback_available():
                        self._advance_startup_priority()
                    decisao = "PULAR_HUMOR"
                    motivo = (
                        humor.get("motivo", "Avaliação local indisponível.")
                        if isinstance(humor, dict)
                        else "Avaliação local de humor indisponível."
                    )
                elif self.has_open_order and self.has_open_order(self.ativo):
                    if self._startup_fallback_available():
                        self._advance_startup_priority()
                    decisao = "PULAR_ORDEM_ABERTA"
                    motivo = (
                        "Já existe uma ordem deste ativo em monitoramento; "
                        "nenhuma nova ordem será aberta até seu fechamento."
                    )
                elif contingencia_startup:
                    decisao = "OPERAR_CONTINGENCIA_STARTUP"
                    motivo = (
                        "Primeira ordem do processo: fallback autorizado pela "
                        "tendência M15 do melhor ativo selecionado; filtro SLM "
                        "e retração M5 não aplicados."
                    )
                else:
                    try:
                        decisao, motivo = self.decision_engine.decidir(sinal)
                    except Exception as erro:
                        logging.exception(
                            "Falha na decisão do sinal para %s.", self.ativo
                        )
                        decisao = "PULAR_ERRO"
                        motivo = f"Falha ao carregar dados para decisão: {erro}"

                    if (
                        decisao.startswith("PULAR")
                        and decisao != "PULAR_ORDEM_ABERTA"
                        and decisao != "PULAR_HUMOR"
                        and self._startup_fallback_available()
                    ):
                        try:
                            with broker_lock:
                                sinal_contingencia = (
                                    self.analyzer.analisar_contingencia_m15(
                                        self.item
                                    )
                                )
                            if sinal_contingencia is not None:
                                humor_contingencia = sinal_contingencia.get(
                                    "humor_mercado"
                                )
                                if (
                                    isinstance(humor_contingencia, dict)
                                    and humor_contingencia.get("favoravel")
                                ):
                                    sinal = sinal_contingencia
                                    contingencia_startup = True
                                    decisao = "OPERAR_CONTINGENCIA_STARTUP"
                                    motivo = (
                                        "Primeira ordem do processo: sinal técnico "
                                        "ou decisão do SLM não aprovado; fallback "
                                        "M15 autorizado pelo filtro de humor local."
                                    )
                                else:
                                    decisao = "PULAR_HUMOR"
                                    motivo = (
                                        humor_contingencia.get(
                                            "motivo",
                                            "Avaliação local indisponível.",
                                        )
                                        if isinstance(humor_contingencia, dict)
                                        else "Avaliação local de humor indisponível."
                                    )
                                    self._advance_startup_priority()
                            else:
                                self._advance_startup_priority()
                        except Exception as erro:
                            self._advance_startup_priority()
                            logging.exception(
                                "Falha gerando contingência de startup para %s "
                                "após rejeição do SLM.",
                                self.ativo,
                            )
                            self.log_append.emit(
                                f"⚠️ [CONTINGÊNCIA {self.ativo}] "
                                f"Falha após rejeição do sinal: {erro}"
                            )

                if not decisao.startswith("PULAR") and self.time_machine is not None:
                    try:
                        veto, detalhe = self.time_machine.evaluate(
                            sinal, contingencia_startup
                        )
                    except Exception:
                        logging.exception(
                            "Falha no filtro da máquina do tempo para %s.", self.ativo
                        )
                        veto = False
                        detalhe = (
                            "Falha no filtro da máquina do tempo; "
                            "filtro não aplicado. Consulte o log operacional."
                        )
                    self.log_append.emit(f"[MÁQUINA DO TEMPO {self.ativo}] {detalhe}")
                    if veto:
                        decisao = "PULAR_MAQUINA_DO_TEMPO"
                        motivo = detalhe
                        if contingencia_startup:
                            self._advance_startup_priority()

                analise_sinal = sinal.get("analise")
                dados_analise = (
                    dict(analise_sinal)
                    if isinstance(analise_sinal, dict)
                    else {}
                )
                dados_analise.setdefault(
                    "confianca",
                    sinal.get("confianca", sinal.get("score")),
                )
                timestamp_sinal = registrar_candidato_db(
                    self.ativo,
                    sinal["sinal"],
                    sinal["payout"],
                    sinal["setup"],
                    dados_analise,
                )
                atualizar_decisao_sinal(
                    timestamp_sinal, self.ativo, decisao, motivo
                )
                self.log_append.emit(
                    f"[DECISÃO {self.ativo}] {decisao}: {motivo}"
                )
                self.analyzer.logger.info(
                    f"[DECISÃO {self.ativo}] {decisao}: {motivo}"
                )

                if decisao == "PULAR_ORDEM_ABERTA":
                    self._aguardar_proxima_analise()
                    continue
                if decisao.startswith("PULAR"):
                    self._monitorar_sinal_pulado(timestamp_sinal, sinal)
                    continue

                self.log_append.emit(f"🎯 [SINAL RETIDO - {self.ativo}] Direção: {sinal['sinal']} | Payout: {sinal['payout']}%")
                self.log_append.emit(f"[SINCRO - {self.ativo}] Aguardando o instante de execução previsto...")

                horario_execucao = int(sinal["executar_em"])
                segundo_disparo = int(ORDER_DISPATCH_SECOND)
                horario_disparo = _horario_disparo(
                    horario_execucao, segundo_disparo
                )
                horario_disparo_local = datetime.fromtimestamp(
                    horario_disparo
                ).astimezone().strftime("%H:%M:%S")
                self.log_append.emit(
                    f"[SINCRO - {self.ativo}] Envio programado para "
                    f"{horario_disparo_local} (segundo {ORDER_DISPATCH_SECOND}); "
                    "aguardando relógio do broker."
                )
                while self.running:
                    with broker_lock:
                        ts_loop = _timestamp_broker(self.broker)
                    if ts_loop >= horario_disparo:
                        break
                    time.sleep(0.005)

                if not self.running:
                    break

                with broker_lock:
                    ts_disparo = _timestamp_broker(self.broker)
                atraso_disparo = ts_disparo - horario_disparo
                if atraso_disparo > 2:
                    motivo_atraso = (
                        f"Janela de envio no segundo {ORDER_DISPATCH_SECOND} "
                        f"perdida: atraso={atraso_disparo * 1000:.0f} ms."
                    )
                    atualizar_status_ordem_db(
                        timestamp_sinal, self.ativo, "PULADO", motivo=motivo_atraso
                    )
                    self.log_append.emit(
                        f"⚠️ [JANELA DE ENVIO PERDIDA {self.ativo}] "
                        f"{motivo_atraso}"
                    )
                    self._monitorar_sinal_pulado(timestamp_sinal, sinal)
                    continue

                # The broker's buy() calculates the matching expiry from duration.
                stake_atual = self.risk.calcular_stake(banca_atual)
                expiracao_minutos = int(sinal.get("expiracao_minutos", 15))
                estado_antes_reserva = self.risk.obter_estado_operacional()
                token_reserva, motivo_bloqueio = self.risk.reservar_entrada(
                    self.ativo, timestamp_sinal
                )
                if token_reserva is None:
                    atualizar_status_ordem_db(
                        timestamp_sinal,
                        self.ativo,
                        "PULADO",
                        motivo=motivo_bloqueio,
                    )
                    self.log_append.emit(
                        f"⏸️ [ENTRADA CANCELADA {self.ativo}] "
                        f"{motivo_bloqueio}."
                    )
                    self._monitorar_sinal_pulado(timestamp_sinal, sinal)
                    continue
                if contingencia_startup and not self._claim_startup_order():
                    self.risk.cancelar_reserva_entrada(token_reserva)
                    motivo = (
                        "Outra ordem do processo foi iniciada antes desta "
                        "contingência; entrada duplicada evitada."
                    )
                    atualizar_status_ordem_db(
                        timestamp_sinal, self.ativo, "PULADO", motivo=motivo
                    )
                    self.log_append.emit(
                        f"[CONTINGÊNCIA {self.ativo}] {motivo}"
                    )
                    self._aguardar_proxima_analise()
                    continue
                if estado_antes_reserva == "DEMO_OBSERVATION":
                    self.log_append.emit(
                        "[RETOMADA] Filtros aprovados. Próxima entrada permitida "
                        "com stake base."
                    )
                if contingencia_startup:
                    self.log_append.emit(
                        f"⚠️ [CONTINGÊNCIA STARTUP {self.ativo}] Tentativa única "
                        f"de ordem {sinal['sinal']} por tendência M15; "
                        "stake segue o gerenciador de risco."
                    )
                self.log_append.emit(
                    f"🚀 [DISPARO BINARY - {self.ativo}] Enviando ordem via "
                    f"buy() no segundo {ORDER_DISPATCH_SECOND} (atraso "
                    f"{atraso_disparo * 1000:.0f} ms): "
                    f"R$ {stake_atual} | duração={expiracao_minutos}min"
                )
                
                try:
                    with broker_lock:
                        timestamp_compra = int(_timestamp_broker(self.broker))
                        expira_em, indice_expiracao = get_expiration_time(
                            timestamp_compra, expiracao_minutos
                        )
                        instrument_type = (
                            "turbo-option"
                            if indice_expiracao < 5
                            else "binary-option"
                        )
                        retorno = self.broker.buy(
                            stake_atual,
                            self.ativo,
                            sinal["sinal"].lower(),
                            expiracao_minutos,
                        )
                except Exception:
                    self.risk.bloquear_indeterminado(
                        "Envio da ordem sem confirmação da corretora."
                    )
                    raise
                self.log_append.emit(
                    f"[EXPIRAÇÃO {self.ativo}] API selecionou "
                    f"{instrument_type}; "
                    f"vencimento estimado="
                    f"{datetime.fromtimestamp(expira_em).strftime('%H:%M:%S')}."
                )
                
                sucesso_ordem = False
                id_da_ordem = None
                motivo_rejeicao = None
                if isinstance(retorno, tuple) and len(retorno) >= 2:
                    sucesso_ordem = bool(retorno[0])
                    if sucesso_ordem:
                        id_da_ordem = retorno[1]
                    else:
                        motivo_rejeicao = retorno[1]
                elif isinstance(retorno, bool):
                    sucesso_ordem = retorno
                else:
                    motivo_rejeicao = repr(retorno)

                if sucesso_ordem and id_da_ordem:
                    try:
                        self.risk.confirmar_entrada(
                            token_reserva, id_da_ordem
                        )
                    except Exception:
                        logging.exception(
                            "Ordem %s aceita, mas falhou a confirmação da reserva.",
                            id_da_ordem,
                        )
                        self.risk.bloquear_indeterminado(
                            "Ordem aceita sem reserva de risco confirmada.",
                            order_id=id_da_ordem,
                        )
                    self.startup_order_claimed.set()
                    try:
                        atualizar_status_ordem_db(
                            timestamp_sinal,
                            self.ativo,
                            "EXECUTANDO",
                            order_id=id_da_ordem,
                        )
                    except Exception:
                        logging.exception(
                            "Ordem %s aceita, mas falhou a atualização no SQLite.",
                            id_da_ordem,
                        )
                    self.log_append.emit(
                        f"✅ [SUCESSO BINARY - {self.ativo}] Ordem aceita! ID: "
                        f"{id_da_ordem}. Aguardando expiração às "
                        f"{datetime.fromtimestamp(expira_em).strftime('%H:%M:%S')}..."
                    )

                    def continuar_ordem_base(
                        resultado,
                        pnl,
                        order_id=str(id_da_ordem),
                    ):
                        self.last_api_call_timestamp = time.monotonic()
                        self.last_action_timestamp = time.monotonic()
                        self._log_resultado_ciclo(resultado, pnl)
                        estado_financeiro_antes = (
                            self.risk.obter_status_resumido()
                        )
                        try:
                            resultado_persistido = (
                                self.risk.registrar_resultado_ordem(
                                    order_id, resultado, pnl, self.ativo
                                )
                            )
                        except Exception:
                            self.risk.bloquear_indeterminado(
                                "Falha ao reconciliar o resultado da ordem.",
                                order_id=order_id,
                            )
                            raise
                        if not resultado_persistido:
                            self.log_append.emit(
                                "[RISCO] Resultado não pôde ser persistido; "
                                "entradas permanecerão bloqueadas."
                            )
                        estado_financeiro_depois = (
                            self.risk.obter_status_resumido()
                        )
                        referencia_antes = estado_financeiro_antes.get(
                            "saldo_referencia_stop"
                        )
                        referencia_depois = estado_financeiro_depois.get(
                            "saldo_referencia_stop"
                        )
                        if (
                            referencia_antes is not None
                            and referencia_depois is not None
                            and referencia_depois > referencia_antes
                        ):
                            self.log_append.emit(
                                f"[GESTÃO] High-water mark atualizado: "
                                f"R$ {referencia_antes:.2f} -> "
                                f"R$ {referencia_depois:.2f}."
                            )
                        estado_risco = self.risk.obter_estado_operacional()
                        if estado_risco == "POST_WIN_PROTECTION":
                            self.log_append.emit(
                                "[GESTÃO] Dois WINs consecutivos. "
                                "Proteção pós-WIN ativada."
                            )
                        elif estado_risco == "POST_LOSS_COOLDOWN":
                            self.log_append.emit(
                                "[PROTEÇÃO] LOSS confirmado; cooldown global ativado."
                            )
                        elif estado_risco == "DAILY_TARGET_REACHED":
                            self.log_append.emit(
                                "[META] Objetivo financeiro atingido. "
                                "Operações reais suspensas."
                            )
                        elif estado_risco == "INDETERMINATE_BLOCK":
                            self.log_append.emit(
                                "[RISCO] Resultado indeterminado; novas entradas "
                                "bloqueadas até reconciliação."
                            )
                        self._executar_recuperacao(
                            resultado,
                            pnl,
                            None,
                            None,
                            stake_atual,
                            expiracao_minutos,
                            instrument_type,
                        )

                    try:
                        self.order_monitor_starter(
                            self.broker,
                            self.ativo,
                            id_da_ordem,
                            stake_atual,
                            expira_em,
                            timestamp_sinal,
                            float(sinal["payout"]),
                            instrument_type=instrument_type,
                            on_result=continuar_ordem_base,
                        )
                    except Exception:
                        self.risk.bloquear_indeterminado(
                            "Ordem aceita sem monitoramento ativo.",
                            order_id=id_da_ordem,
                        )
                        raise
                elif sucesso_ordem:
                    self.risk.confirmar_entrada(token_reserva, None)
                    self.startup_order_claimed.set()
                    atualizar_status_ordem_db(
                        timestamp_sinal,
                        self.ativo,
                        "INDETERMINADO",
                        motivo="Corretora indicou aceite, mas não retornou ID.",
                    )
                    self.log_append.emit(
                        f"⚠️ [ORDEM {self.ativo}] A corretora indicou aceite, mas não retornou ID; resultado não poderá ser monitorado. Resposta: {retorno!r}"
                    )
                else:
                    self.risk.cancelar_reserva_entrada(token_reserva)
                    motivo = (
                        str(motivo_rejeicao)
                        if motivo_rejeicao not in (None, "")
                        else "A API não retornou o motivo (possível timeout)."
                    )
                    atualizar_status_ordem_db(
                        timestamp_sinal,
                        self.ativo,
                        "REJEITADO",
                        motivo=motivo,
                    )
                    self.log_append.emit(
                        f"❌ [REJEITADA - {self.ativo}] Servidor recusou a entrada. Motivo retornado: {motivo}"
                    )
                    if contingencia_startup:
                        self.log_append.emit(
                            f"⚠️ [CONTINGÊNCIA STARTUP {self.ativo}] "
                            "A corretora não confirmou a entrada. Para evitar "
                            "ordem duplicada após resposta ambígua, não haverá "
                            "nova tentativa automática nesta inicialização."
                        )

                self._aguardar_proxima_analise()

            except Exception as e:
                logging.exception("Erro inesperado no ciclo do ativo %s.", self.ativo)
                self.log_append.emit(f"⚠️ [AVISO THREAD {self.ativo}] Erro no ciclo: {e}")
                time.sleep(5)

    def stop(self):
        self.running = False


class DeskubuddyTradingEngine(QThread):
    """Orquestrador Principal (Dispatcher) que gerencia as sub-threads de cada ativo."""
    status_changed = Signal(str, str)
    log_message = Signal(str)
    erro_disparado = Signal(str)

    def __init__(
        self,
        email=None,
        password=None,
        account_type=None,
        resume_manual_pause=False,
    ):
        super().__init__()
        self.email = Config.IQ_EMAIL if email is None else email
        self.password = Config.IQ_PASSWORD if password is None else password
        self.account_type = (
            Config.IQ_ACCOUNT_TYPE if account_type is None else account_type
        ).upper()
        self.resume_manual_pause = resume_manual_pause
        self.running = True
        self.sub_threads = []
        self.order_monitors = []
        self._workers_lock = threading.Lock()
        self._worker_configs = {}
        self._worker_restart_attempts = {}
        self._next_worker_restart = {}
        self._worker_started_at = {}
        self._heartbeat_warnings = {}
        self._startup_order_claimed = threading.Event()
        self._startup_order_lock = threading.Lock()
        self._startup_priority = [0]
        self._startup_count = [0]
        self._risk_manager = None
        self._risk_halt_announced = False
        self._time_machine = None

    def run(self):
        try:
            self._run_engine()
        except Exception as erro:
            _registrar_falha_sem_dados_sensiveis(
                "Erro fatal no dispatcher DeskubuddyTradingEngine", erro
            )
            self.erro_disparado.emit(
                "Erro fatal no dispatcher. Consulte o log operacional."
            )
            self.relay_log(
                "⚠️ [DISPATCHER] Falha inesperada; consulte o log operacional."
            )
        finally:
            if self._time_machine is not None:
                self._time_machine.stop()
            try:
                self._stop_sub_threads()
            except Exception:
                logging.exception("Falha ao encerrar as threads de análise.")
            try:
                self._stop_order_monitors()
            except Exception:
                logging.exception("Falha ao encerrar os monitores de ordens.")
            if self._time_machine is not None:
                self._time_machine.wait()

    def _run_engine(self):
        Config.validate(self.email, self.password, self.account_type)
        print("\n" + "="*60)
        print(f" INTERFACE MULTI-THREAD DESKUBUDDY | {datetime.now().strftime('%H:%M:%S')}")
        print("="*60)
        print("[LOG] Conectando ao servidor...")
        self.status_changed.emit("Conectando", "Validando credenciais e sincronizando histórico real...")
        iniciar_banco()
        
        try:
            broker = IQ_Option(self.email, self.password)
            check, _ = broker.connect()
            if not check:
                self.erro_disparado.emit(
                    "Falha na autenticação com a corretora."
                )
                return
            broker.change_balance(self.account_type)
            print(f"[CONEXÃO] Autenticado! Iniciando orquestração em paralelo...")
            
            if not hasattr(broker, "get_ts"):
                obter_timestamp = getattr(
                    broker, "get_server_timestamp", None
                )
                broker.get_ts = (
                    obter_timestamp if callable(obter_timestamp) else time.time
                )
                
        except Exception as erro:
            _registrar_falha_sem_dados_sensiveis(
                "Falha durante conexão/sincronização com a corretora", erro
            )
            self.erro_disparado.emit(
                "Falha na conexão/sincronização com a corretora."
            )
            return

        from app.analyzer import MarketAnalyzer
        from app.decision_engine import OllamaTradeDecisionEngine
        from app.risk import ConservativeRiskManager
        from app.analytics import PerformanceTracker
        from app.pair_selector import DynamicPairSelector
        from app.live_time_machine import LiveTimeMachine

        tracker = PerformanceTracker()
        with broker_lock:
            banca_inicial = float(broker.get_balance())
        risk_manager = ConservativeRiskManager(
            pct_risco_por_operacao=0.02,
            stop_loss_diario=0.10,
            initial_balance=banca_inicial,
            cooldown_losses=3,
            cooldown_minutes=30,
            state_loader=carregar_estado_risco,
            state_saver=salvar_estado_risco,
            profit_target=Config.STOP_WIN,
            entry_amount=Config.ENTRY_AMOUNT,
            account_type=self.account_type,
            post_win_limit=Config.POST_WIN_LIMIT,
            post_win_cooldown_minutes=Config.POST_WIN_COOLDOWN_MINUTES,
            post_loss_cooldown_minutes=Config.POST_LOSS_COOLDOWN_MINUTES,
        )
        if self.resume_manual_pause:
            risk_manager.definir_pausa_manual(False)
        self._risk_manager = risk_manager
        analyzer = MarketAnalyzer(broker, None, tracker.logger)
        decision_engine = OllamaTradeDecisionEngine()
        pair_selector = DynamicPairSelector(broker, tracker.logger)
        self._time_machine = LiveTimeMachine(broker, self.account_type, broker_lock)
        self._time_machine.log_message.connect(self.relay_log)
        self._time_machine.start()

        msg_sucesso = f"Operador: Julio\nConta: {self.account_type}\nModo: MULTI-THREAD PARALELO"
        self.status_changed.emit("Monitorando o mercado", msg_sucesso)

        proxima_atualizacao = datetime.now()
        while self.running:
            agora = datetime.now()
            parada_diaria, motivo_parada = risk_manager.deve_parar()
            if parada_diaria:
                if not self._risk_halt_announced:
                    self._risk_halt_announced = True
                    self.relay_log(
                        f"🛑 [PROTEÇÃO DIÁRIA] {motivo_parada}; "
                        "novas entradas suspensas. Aguardando ordens abertas."
                    )
                    self.status_changed.emit(
                        "Proteção diária",
                        f"{motivo_parada}\nAguardando ordens abertas encerrarem.",
                    )
                    with self._workers_lock:
                        for worker in self.sub_threads:
                            worker.stop()

                with self._workers_lock:
                    self._remover_monitores_finalizados()
                    ordens_pendentes = any(
                        monitor.isRunning() or not monitor.completed.is_set()
                        for monitor in self.order_monitors
                    )
                if not ordens_pendentes:
                    break

                self._supervisionar_workers()
                time.sleep(1)
                continue

            if agora >= proxima_atualizacao:
                ativos = []
                try:
                    with broker_lock:
                        timestamp_broker = int(_timestamp_broker(broker))
                        horario_broker = datetime.fromtimestamp(
                            timestamp_broker, timezone.utc
                        )
                        ativos = pair_selector.obter_ativos_operaveis(
                            horario_broker
                        )
                except Exception as erro:
                    logging.exception("Falha atualizando a lista de ativos.")
                    pair_selector.last_catalog_success = False
                    self.relay_log(f"⚠️ [ATIVOS] Não foi possível atualizar a lista: {erro}")

                consulta_valida = pair_selector.last_catalog_success
                if consulta_valida and self.running:
                    self._start_workers(
                        broker, ativos, risk_manager, analyzer, decision_engine
                    )
                    if not ativos:
                        self.relay_log(
                            "[ATIVOS] Nenhum par elegível nesta consulta; "
                            "workers anteriores encerrados e nova tentativa em 15 segundos."
                        )
                elif self.running:
                    self.relay_log(
                        "[ATIVOS] Consulta indisponível; mantendo os workers ativos "
                        "e tentando novamente em 15 segundos."
                    )

                proxima_hora = agora.replace(
                    minute=0, second=0, microsecond=0
                ) + timedelta(hours=1)
                if consulta_valida and ativos:
                    proxima_atualizacao = proxima_hora
                else:
                    proxima_atualizacao = min(
                        agora + timedelta(seconds=15), proxima_hora
                    )

            try:
                with broker_lock:
                    if not broker.check_connect():
                        conectado, motivo = broker.connect()
                        if not conectado:
                            logging.error(
                                "Reconexão do dispatcher recusada: %s", motivo
                            )
                            self.relay_log(
                                f"⚠️ [DISPATCHER] Reconexão recusada: {motivo}"
                            )
            except Exception as e:
                logging.exception("Falha verificando/reconectando a corretora.")
                self.relay_log(
                    f"⚠️ [DISPATCHER] Falha ao verificar/reconectar: {e}"
                )
            try:
                self._supervisionar_workers()
            except Exception:
                logging.exception("Falha supervisionando workers de ativos.")
                self.relay_log(
                    "⚠️ [WATCHDOG] Falha ao supervisionar workers; "
                    "verifique o log detalhado."
                )
            with self._workers_lock:
                self._remover_monitores_finalizados()
            time.sleep(1)

    def _start_workers(self, broker, ativos, risk_manager, analyzer, decision_engine):
        if self._time_machine is not None:
            self._time_machine.set_assets(ativos)
        with self._startup_order_lock:
            self._startup_count[0] = len(ativos)
            if not self._startup_order_claimed.is_set():
                self._startup_priority[0] = 0

        configuracoes_desejadas = {}
        for indice, item in enumerate(ativos):
            ativo = item["ativo"]
            configuracoes_desejadas[ativo] = (
                broker,
                item,
                risk_manager,
                analyzer,
                decision_engine,
                True,
                indice,
            )

        with self._workers_lock:
            ativos_removidos = set(self._worker_configs) - set(
                configuracoes_desejadas
            )
            for ativo in ativos_removidos:
                self._worker_configs.pop(ativo, None)
                self._worker_restart_attempts.pop(ativo, None)
                self._next_worker_restart.pop(ativo, None)
                self._worker_started_at.pop(ativo, None)
                self._heartbeat_warnings.pop(ativo, None)
                for worker in self.sub_threads:
                    if worker.ativo == ativo:
                        worker.stop()

            self.sub_threads = [
                worker
                for worker in self.sub_threads
                if worker.isRunning() or worker.ativo in configuracoes_desejadas
            ]

            for ativo, config in configuracoes_desejadas.items():
                if not self.running:
                    break
                self._worker_configs[ativo] = config
                worker = next(
                    (
                        item
                        for item in self.sub_threads
                        if item.ativo == ativo
                    ),
                    None,
                )
                if worker is not None and worker.isRunning():
                    if worker.running:
                        worker.item = config[1]
                        worker.startup_rank = config[6]
                    continue
                if worker is not None:
                    self.sub_threads.remove(worker)
                try:
                    self._iniciar_worker(ativo, config)
                except Exception:
                    logging.exception("Falha iniciando worker do ativo %s.", ativo)
                    self.relay_log(
                        f"⚠️ [DISPATCHER {ativo}] Não foi possível iniciar worker; "
                        "o watchdog tentará novamente."
                    )
                else:
                    print(f"[DISPATCHER] Sub-thread paralela ativa para: {ativo}")

    def _iniciar_worker(self, ativo, config):
        (
            broker,
            item,
            risk_manager,
            analyzer,
            decision_engine,
            startup_fallback,
            startup_rank,
        ) = config
        worker = AtivoTradingWorker(
            broker,
            item,
            risk_manager,
            analyzer,
            decision_engine,
            self._start_order_monitor,
            self._has_open_order,
            startup_fallback=startup_fallback,
            startup_order_claimed=self._startup_order_claimed,
            startup_order_lock=self._startup_order_lock,
            startup_rank=startup_rank,
            startup_priority=self._startup_priority,
            startup_count=self._startup_count,
            time_machine=self._time_machine,
        )
        worker.log_append.connect(self.relay_log)
        self.sub_threads.append(worker)
        self._worker_started_at[ativo] = time.monotonic()
        worker.start()

    def _supervisionar_workers(self):
        agora = time.monotonic()
        with self._workers_lock:
            if not self.running:
                return
            if (
                self._risk_manager is not None
                and self._risk_manager.deve_parar()[0]
            ):
                for worker in self.sub_threads:
                    worker.stop()
                return

            for ativo, config in list(self._worker_configs.items()):
                worker = next(
                    (item for item in self.sub_threads if item.ativo == ativo),
                    None,
                )
                if worker is not None and worker.isRunning():
                    iniciado_em = self._worker_started_at.get(ativo, agora)
                    if agora - iniciado_em >= 60:
                        self._worker_restart_attempts.pop(ativo, None)
                    atraso_heartbeat = agora - worker.last_heartbeat
                    ultimo_aviso = self._heartbeat_warnings.get(ativo, 0.0)
                    if (
                        atraso_heartbeat >= 90
                        and agora - ultimo_aviso >= 60
                    ):
                        logging.warning(
                            "Heartbeat vencido do worker %s (%.0fs); "
                            "thread ainda está viva e não será duplicada.",
                            ativo,
                            atraso_heartbeat,
                        )
                        self.relay_log(
                            f"⚠️ [WATCHDOG {ativo}] Sem progresso há "
                            f"{atraso_heartbeat:.0f}s; thread ainda viva. "
                            "Não será iniciada outra para evitar ordens duplicadas."
                        )
                        self._heartbeat_warnings[ativo] = agora
                    continue

                if worker is not None:
                    self.sub_threads.remove(worker)

                if ativo not in self._next_worker_restart:
                    tentativas = self._worker_restart_attempts.get(ativo, 0) + 1
                    self._worker_restart_attempts[ativo] = tentativas
                    atraso = min(5 * (2 ** (tentativas - 1)), 60)
                    self._next_worker_restart[ativo] = agora + atraso
                    logging.error(
                        "Worker do ativo %s encerrou inesperadamente; "
                        "reinício em %ss (tentativa %s).",
                        ativo,
                        atraso,
                        tentativas,
                    )
                    self.relay_log(
                        f"⚠️ [WATCHDOG {ativo}] Worker encerrado; "
                        f"reinício em {atraso}s (tentativa {tentativas})."
                    )
                    continue

                if agora >= self._next_worker_restart[ativo]:
                    try:
                        self._iniciar_worker(ativo, config)
                    except Exception:
                        tentativas = self._worker_restart_attempts.get(ativo, 0) + 1
                        self._worker_restart_attempts[ativo] = tentativas
                        atraso = min(5 * (2 ** (tentativas - 1)), 60)
                        self._next_worker_restart[ativo] = agora + atraso
                        logging.exception(
                            "Falha reiniciando worker do ativo %s; nova tentativa "
                            "em %ss.",
                            ativo,
                            atraso,
                        )
                        self.relay_log(
                            f"⚠️ [WATCHDOG {ativo}] Falha ao reiniciar; "
                            f"nova tentativa em {atraso}s."
                        )
                    else:
                        self._next_worker_restart.pop(ativo, None)
                        self._heartbeat_warnings.pop(ativo, None)
                        self.relay_log(f"[WATCHDOG {ativo}] Worker reiniciado.")

    def _has_open_order(self, ativo):
        with self._workers_lock:
            return any(
                monitor.ativo == ativo and not monitor.order_closed.is_set()
                for monitor in self.order_monitors
            )

    def _start_order_monitor(
        self,
        broker,
        ativo,
        order_id,
        stake,
        expires_at,
        timestamp_sinal,
        payout=None,
        on_result=None,
        record_result=True,
        instrument_type="binary-option",
    ):
        monitor = BinaryOrderMonitor(
            broker,
            ativo,
            order_id,
            stake,
            expires_at,
            timestamp_sinal,
            payout,
            on_result=on_result,
            record_result=record_result,
            instrument_type=instrument_type,
        )
        monitor.log_append.connect(self.relay_log)
        with self._workers_lock:
            self.order_monitors.append(monitor)
            monitor.start()
            if not self.running:
                monitor.stop()
        return monitor

    def _remover_monitores_finalizados(self):
        self.order_monitors = [
            monitor for monitor in self.order_monitors
            if monitor.isRunning() or not monitor.completed.is_set()
        ]

    def relay_log(self, texto):
        mensagem = f"[{datetime.now().strftime('%H:%M:%S')}] {texto}"
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        mensagem_console = mensagem.encode(
            encoding, errors="backslashreplace"
        ).decode(encoding)
        print(mensagem_console)
        self.log_message.emit(mensagem)

    def stop(self):
        if self._time_machine is not None:
            self._time_machine.stop()
        if self._risk_manager is not None:
            self._risk_manager.definir_pausa_manual(True)
        with self._workers_lock:
            self.running = False
            workers = list(self.sub_threads)
            monitors = list(self.order_monitors)

        print("[DISPATCHER] Interrompendo todas as sub-threads de moedas...")
        for worker in workers:
            worker.stop()
        for monitor in monitors:
            monitor.stop()

    def _stop_sub_threads(self):
        with self._workers_lock:
            workers = list(self.sub_threads)
            self.sub_threads.clear()
            self._worker_configs.clear()
            self._worker_restart_attempts.clear()
            self._next_worker_restart.clear()
            self._worker_started_at.clear()
            self._heartbeat_warnings.clear()

        for worker in workers:
            worker.stop()
        for worker in workers:
            if worker.isRunning():
                worker.wait()

    def _stop_order_monitors(self):
        with self._workers_lock:
            monitors = list(self.order_monitors)
            self.order_monitors.clear()

        for monitor in monitors:
            monitor.stop()
        for monitor in monitors:
            if monitor.isRunning():
                monitor.wait()