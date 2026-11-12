import logging
import math
import threading
import time
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING


logger = logging.getLogger(__name__)


class ConservativeRiskManager:
    """Thread-safe, persisted controls for one account's daily trading session."""

    STATE_VERSION = 4

    def __init__(
        self,
        pct_risco_por_operacao=0.02,
        stop_loss_diario=0.11,
        stop_win_diario=0.10,
        initial_balance=None,
        profit_lock_floor_pct=0.08,
        profit_lock_ratio=0.80,
        cooldown_losses=3,
        cooldown_minutes=10,
        state_loader=None,
        state_saver=None,
        profit_target=None,
        entry_amount=None,
        account_type="PRACTICE",
        post_win_limit=2,
        post_win_cooldown_minutes=10,
        post_loss_cooldown_minutes=10,
    ):
        self.pct_risco = pct_risco_por_operacao
        self.stop_loss_pct = stop_loss_diario
        self.cooldown_losses = cooldown_losses
        self.cooldown_seconds = cooldown_minutes * 60
        self.meta_lucro_sessao = profit_target
        self.valor_stake_base = entry_amount
        self.account_type = str(account_type).upper()
        self.limite_vitorias_consecutivas = post_win_limit
        self.cooldown_pos_win_seconds = post_win_cooldown_minutes * 60
        self.cooldown_pos_loss_seconds = post_loss_cooldown_minutes * 60
        self.banca_inicial_sessao = initial_balance
        self.saldo_referencia_stop = initial_balance
        self.lucro_acumulado_sessao = 0.0
        self.lucro_maximo_sessao = 0.0
        self.session_date = date.today().isoformat()
        self._lock = threading.RLock()
        self._state_loader = state_loader
        self._state_saver = state_saver
        self._perdas_consecutivas_por_ativo = {}
        self._cooldown_ate_por_ativo = {}
        self._motivo_parada = None
        self._estado_operacional = "NORMAL"
        self._vitorias_consecutivas = 0
        self._cooldown_global_ate = 0.0
        self._entrada_reservada = None
        self._ordens_abertas = {}
        self._resultados_processados = set()
        self._ordens_indeterminadas = set()
        self._contingencia_retomada_manual = False
        self.ignorar_trava_stop = False
        self._timestamp_reset_trava = None

        if self.banca_inicial_sessao is not None and self.banca_inicial_sessao <= 0:
            raise ValueError("A banca inicial da sessão deve ser maior que zero.")
        if not 0 < self.stop_loss_pct < 1:
            raise ValueError("O percentual de stop loss deve estar entre 0 e 1.")
        if (
            cooldown_losses < 1
            or cooldown_minutes < 1
            or post_win_limit < 1
            or post_win_cooldown_minutes < 1
            or post_loss_cooldown_minutes < 1
        ):
            raise ValueError("Cooldown exige limite de perdas e duração positivos.")
        if self.account_type not in {"PRACTICE", "REAL"}:
            raise ValueError("Tipo de conta inválido para o estado de risco.")
        if self.meta_lucro_sessao is not None and self.meta_lucro_sessao <= 0:
            raise ValueError("A meta financeira deve ser positiva.")
        if self.valor_stake_base is not None and self.valor_stake_base <= 0:
            raise ValueError("A stake base deve ser positiva.")

        if self._state_loader is not None:
            try:
                estado = self._state_loader(
                    self.session_date, self.account_type
                )
            except Exception:
                logger.exception("Falha carregando estado diário de risco do SQLite.")
                self._motivo_parada = "Estado diário de risco indisponível"
                self._estado_operacional = "INDETERMINATE_BLOCK"
            else:
                if estado is not None:
                    self._restaurar_estado(estado)
                elif (
                    self.banca_inicial_sessao is not None
                    and not self._persistir_estado()
                ):
                    self._estado_operacional = "INDETERMINATE_BLOCK"

    def _restaurar_estado(self, estado):
        try:
            if not isinstance(estado, dict):
                raise ValueError("Estado diário possui estrutura inválida.")
            tipo_conta_estado = estado.get("account_type")
            if (
                tipo_conta_estado is not None
                and str(tipo_conta_estado).upper() != self.account_type
            ):
                raise ValueError(
                    "Estado financeiro pertence a outro tipo de conta."
                )
            saldo_inicial = float(estado["banca_inicial"])
            lucro = float(estado["lucro"])
            pico = float(estado["pico_lucro"])
            perdas = estado.get("perdas_por_ativo", {})
            cooldowns = estado.get("cooldown_ate_por_ativo", {})
            motivo = estado.get("motivo_parada")
            versao = int(estado.get("versao", 1))
            if not all(math.isfinite(valor) for valor in (saldo_inicial, lucro, pico)):
                raise ValueError("Estado diário contém valores não finitos.")
            if saldo_inicial <= 0 or not isinstance(perdas, dict) or not isinstance(
                cooldowns, dict
            ):
                raise ValueError("Estado diário possui estrutura inválida.")
            if any(
                not math.isfinite(float(timestamp))
                for timestamp in cooldowns.values()
            ):
                raise ValueError("Cooldown persistido contém valor inválido.")
            if versao < self.STATE_VERSION:
                saldo_corretora_atual = self.banca_inicial_sessao
                if saldo_corretora_atual is None:
                    raise ValueError(
                        "Saldo atual necessário para migrar o estado diário de risco."
                    )
                saldo_inicial = float(saldo_corretora_atual) - lucro
                if not math.isfinite(saldo_inicial) or saldo_inicial <= 0:
                    raise ValueError(
                        "Não foi possível migrar a banca inicial do estado diário."
                    )
                # Legacy states could persist a profit-lock halt; discard that halt
                # and establish a fresh loss reference from the live broker balance.
                if motivo and str(motivo).startswith("TRAVA DE LUCRO"):
                    motivo = None
                saldo_referencia_stop = saldo_corretora_atual
                logger.warning(
                    "Estado de risco legado migrado; a banca inicial e a "
                    "referência do stop foram recalibradas com o saldo atual "
                    "da conta."
                )
            else:
                saldo_referencia_stop = float(
                    estado.get(
                        "saldo_referencia_stop",
                        saldo_inicial + max(pico, 0.0),
                    )
                )
                if not math.isfinite(saldo_referencia_stop) or saldo_referencia_stop <= 0:
                    raise ValueError("Referência de stop diário inválida.")
                if self.banca_inicial_sessao is not None:
                    saldo_referencia_stop = max(
                        saldo_referencia_stop, float(self.banca_inicial_sessao)
                    )
                    saldo_inicial = float(self.banca_inicial_sessao) - lucro
                    if not math.isfinite(saldo_inicial) or saldo_inicial <= 0:
                        raise ValueError(
                            "Não foi possível reconciliar o saldo diário com a corretora."
                        )
            self.banca_inicial_sessao = saldo_inicial
            self.saldo_referencia_stop = saldo_referencia_stop
            self.lucro_acumulado_sessao = lucro
            self.lucro_maximo_sessao = max(pico, lucro, 0.0)
            self._perdas_consecutivas_por_ativo = {
                str(ativo): int(contagem)
                for ativo, contagem in perdas.items()
            }
            self._cooldown_ate_por_ativo = {
                str(ativo): float(timestamp)
                for ativo, timestamp in cooldowns.items()
            }
            self._motivo_parada = motivo
            self.meta_lucro_sessao = estado.get(
                "meta_lucro_sessao", self.meta_lucro_sessao
            )
            if (
                estado.get("meta_lucro_sessao") is not None
                and self.meta_lucro_sessao != estado["meta_lucro_sessao"]
            ):
                logger.warning(
                    "Meta financeira configurada difere da meta salva; "
                    "a sessão mantém o valor persistido."
                )
            self._estado_operacional = estado.get("estado_operacional", "NORMAL")
            self._vitorias_consecutivas = int(
                estado.get("vitorias_consecutivas", 0)
            )
            self._cooldown_global_ate = float(
                estado.get("cooldown_global_ate", 0.0)
            )
            if (
                self._vitorias_consecutivas < 0
                or not math.isfinite(self._cooldown_global_ate)
            ):
                raise ValueError("Estado de sequência/cooldown inválido.")
            resultados_processados = estado.get("resultados_processados", [])
            ordens_indeterminadas = estado.get("ordens_indeterminadas", [])
            ordens_abertas = estado.get("ordens_abertas", {})
            if (
                not isinstance(resultados_processados, list)
                or not isinstance(ordens_indeterminadas, list)
                or not isinstance(ordens_abertas, dict)
            ):
                raise ValueError("Estado de ordens persistido é inválido.")
            self._resultados_processados = {
                str(order_id) for order_id in resultados_processados
            }
            self._ordens_indeterminadas = {
                str(order_id)
                for order_id in ordens_indeterminadas
            }
            self._ordens_abertas = {
                str(order_id): str(ativo)
                for order_id, ativo in ordens_abertas.items()
            }
            reserva_pendente = estado.get("entrada_reservada")
            if reserva_pendente is not None:
                if (
                    not isinstance(reserva_pendente, (list, tuple))
                    or len(reserva_pendente) != 2
                ):
                    raise ValueError("Reserva de entrada persistida é inválida.")
                self._entrada_reservada = (
                    str(reserva_pendente[0]),
                    str(reserva_pendente[1]),
                )
            if self._ordens_abertas:
                self._estado_operacional = "INDETERMINATE_BLOCK"
                self._ordens_indeterminadas.update(self._ordens_abertas)
                self._motivo_parada = (
                    "Ordem aberta antes da reinicialização; reconciliação necessária."
                )
            if self._entrada_reservada is not None:
                self._estado_operacional = "INDETERMINATE_BLOCK"
                self._motivo_parada = (
                    "Envio interrompido durante reinicialização; "
                    "reconciliação necessária."
                )
                self._entrada_reservada = None
            estados_validos = {
                "NORMAL",
                "POST_WIN_PROTECTION",
                "POST_LOSS_COOLDOWN",
                "DEMO_OBSERVATION",
                "DAILY_TARGET_REACHED",
                "DAILY_STOP_LOSS",
                "MANUAL_PAUSE",
                "INDETERMINATE_BLOCK",
            }
            if self._estado_operacional not in estados_validos:
                raise ValueError("Estado operacional persistido é inválido.")
            if self.meta_lucro_sessao is not None and (
                not math.isfinite(float(self.meta_lucro_sessao))
                or float(self.meta_lucro_sessao) <= 0
            ):
                raise ValueError("Meta financeira persistida é inválida.")
            self._atualizar_estado_financeiro()
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.exception("Estado diário de risco inválido; novas entradas bloqueadas.")
            self._motivo_parada = "Estado diário de risco inválido"
            self._estado_operacional = "INDETERMINATE_BLOCK"

    def _estado_serializavel(self):
        return {
            "versao": self.STATE_VERSION,
            "account_type": self.account_type,
            "banca_inicial": self.banca_inicial_sessao,
            "saldo_referencia_stop": self.saldo_referencia_stop,
            "lucro": self.lucro_acumulado_sessao,
            "pico_lucro": self.lucro_maximo_sessao,
            "meta_lucro_sessao": self.meta_lucro_sessao,
            "perdas_por_ativo": self._perdas_consecutivas_por_ativo,
            "cooldown_ate_por_ativo": self._cooldown_ate_por_ativo,
            "motivo_parada": self._motivo_parada,
            "estado_operacional": self._estado_operacional,
            "vitorias_consecutivas": self._vitorias_consecutivas,
            "cooldown_global_ate": self._cooldown_global_ate,
            "resultados_processados": sorted(self._resultados_processados),
            "ordens_indeterminadas": sorted(self._ordens_indeterminadas),
            "ordens_abertas": self._ordens_abertas,
            "entrada_reservada": self._entrada_reservada,
        }

    def _persistir_estado(self):
        if self._state_saver is None:
            return True
        try:
            self._state_saver(
                self.session_date,
                self._estado_serializavel(),
                self.account_type,
            )
        except Exception:
            logger.exception("Falha persistindo estado diário de risco no SQLite.")
            self._motivo_parada = "Falha ao persistir estado diário de risco"
            self._estado_operacional = "INDETERMINATE_BLOCK"
            return False
        return True

    def _atualizar_estado_financeiro(self):
        if (
            self.meta_lucro_sessao is not None
            and self.lucro_acumulado_sessao >= float(self.meta_lucro_sessao)
        ):
            self._estado_operacional = "DAILY_TARGET_REACHED"
            self._motivo_parada = (
                f"Meta financeira atingida: R$ {self.lucro_acumulado_sessao:.2f} "
                f"de R$ {float(self.meta_lucro_sessao):.2f}"
            )
            return

        if self.banca_inicial_sessao is None:
            return
        banca_atual = max(
            0.0, self.banca_inicial_sessao + self.lucro_acumulado_sessao
        )
        if self.saldo_referencia_stop is None:
            self.saldo_referencia_stop = banca_atual
        limite_stop = self.saldo_referencia_stop * (1 - self.stop_loss_pct)
        if banca_atual <= limite_stop:
            self._estado_operacional = "DAILY_STOP_LOSS"
            self._motivo_parada = (
                f"STOP LOSS DINÂMICO atingido (limite R$ {limite_stop:.2f}; "
                f"referência R$ {self.saldo_referencia_stop:.2f})"
            )

    def _atualizar_estado_protecao(self):
        if (
            self._estado_operacional
            in {"POST_WIN_PROTECTION", "POST_LOSS_COOLDOWN"}
            and time.time() >= self._cooldown_global_ate
        ):
            self._estado_operacional = "DEMO_OBSERVATION"
            self._motivo_parada = None

    def _atualizar_parada(self):
        self._atualizar_estado_financeiro()
        self._atualizar_estado_protecao()
        if self._estado_operacional in {
            "DAILY_TARGET_REACHED",
            "DAILY_STOP_LOSS",
            "MANUAL_PAUSE",
            "INDETERMINATE_BLOCK",
        }:
            return True, self._motivo_parada or self._estado_operacional
        return False, "OK"

    @staticmethod
    def _timestamp_sinal(timestamp_sinal):
        if timestamp_sinal is None:
            return None
        if isinstance(timestamp_sinal, (int, float)):
            return float(timestamp_sinal)
        try:
            return datetime.fromisoformat(str(timestamp_sinal)).timestamp()
        except ValueError:
            return None

    def _autorizar_entrada_locked(self, ativo, timestamp_sinal=None):
        parada, motivo = self._atualizar_parada()
        if parada:
            return False, motivo
        restante = self._cooldown_global_ate - time.time()
        if restante > 0:
            return (
                False,
                f"[PROTEÇÃO] Entrada bloqueada durante cooldown "
                f"({restante / 60:.1f} minutos restantes).",
            )
        restante_ativo = self._cooldown_ate_por_ativo.get(ativo, 0.0) - time.time()
        if restante_ativo > 0:
            return (
                False,
                f"COOLDOWN {ativo}: restam {restante_ativo / 60:.1f} minutos",
            )
        if self._estado_operacional == "DEMO_OBSERVATION":
            timestamp = self._timestamp_sinal(timestamp_sinal)
            if timestamp is None or timestamp < self._cooldown_global_ate:
                return (
                    False,
                    "[RETOMADA] Aguardando um novo sinal após o cooldown.",
                )
        if self._entrada_reservada is not None:
            return False, "[RISCO] Outro worker está enviando uma ordem."
        if any(ativo_aberto == ativo for ativo_aberto in self._ordens_abertas.values()):
            return False, "[RISCO] Já existe uma ordem aberta neste ativo."
        return True, "OK"

    def autorizar_entrada(self, ativo, timestamp_sinal=None):
        with self._lock:
            return self._autorizar_entrada_locked(ativo, timestamp_sinal)

    def reservar_entrada(self, ativo, timestamp_sinal=None):
        with self._lock:
            autorizado, motivo = self._autorizar_entrada_locked(
                ativo, timestamp_sinal
            )
            if not autorizado:
                return None, motivo
            token = uuid.uuid4().hex
            self._entrada_reservada = (token, ativo)
            if self._estado_operacional == "DEMO_OBSERVATION":
                self._estado_operacional = "NORMAL"
                self._motivo_parada = None
            if not self._persistir_estado():
                self._entrada_reservada = None
                return None, "Falha ao persistir a reserva de entrada."
            self._contingencia_retomada_manual = False
            return token, "OK"

    def cancelar_reserva_entrada(self, token):
        with self._lock:
            if self._entrada_reservada is None:
                return False
            if self._entrada_reservada[0] != token:
                return False
            self._entrada_reservada = None
            self._persistir_estado()
            return True

    def confirmar_entrada(self, token, order_id):
        if order_id is None:
            self.bloquear_indeterminado(
                "A corretora aceitou a ordem sem retornar um identificador."
            )
            return False
        with self._lock:
            if (
                self._entrada_reservada is None
                or self._entrada_reservada[0] != token
            ):
                raise RuntimeError("Reserva de entrada ausente ou inválida.")
            _, ativo = self._entrada_reservada
            self._entrada_reservada = None
            self._ordens_abertas[str(order_id)] = ativo
            self._persistir_estado()
            return True

    def bloquear_indeterminado(self, motivo, order_id=None):
        with self._lock:
            self._contingencia_retomada_manual = False
            self._estado_operacional = "INDETERMINATE_BLOCK"
            self._motivo_parada = str(motivo)
            if order_id is not None:
                self._ordens_indeterminadas.add(str(order_id))
            self._entrada_reservada = None
            self._persistir_estado()

    def _registrar_resultado_confirmado(self, resultado, lucro, ativo):
        lucro = float(lucro)
        if not math.isfinite(lucro):
            raise ValueError("O resultado da ordem deve ser finito.")
        resultado = str(resultado).upper()
        if resultado not in {"WIN", "LOSS", "DRAW"}:
            raise ValueError("Resultado confirmado deve ser WIN, LOSS ou DRAW.")

        self._atualizar_estado_protecao()
        self.lucro_acumulado_sessao += lucro
        self.lucro_maximo_sessao = max(
            self.lucro_maximo_sessao, self.lucro_acumulado_sessao
        )
        banca_atual = self.banca_inicial_sessao + self.lucro_acumulado_sessao
        if self.saldo_referencia_stop is None or banca_atual > self.saldo_referencia_stop:
            self.saldo_referencia_stop = banca_atual

        if resultado == "WIN":
            self._perdas_consecutivas_por_ativo.pop(ativo, None)
            self._vitorias_consecutivas += 1
            if self._vitorias_consecutivas >= self.limite_vitorias_consecutivas:
                self._estado_operacional = "POST_WIN_PROTECTION"
                self._cooldown_global_ate = (
                    time.time() + self.cooldown_pos_win_seconds
                )
                self._motivo_parada = (
                    "[GESTÃO] Dois WINs consecutivos. "
                    "Proteção pós-WIN ativada."
                )
        else:
            self._vitorias_consecutivas = 0
            if resultado == "LOSS":
                perdas = self._perdas_consecutivas_por_ativo.get(ativo, 0) + 1
                self._perdas_consecutivas_por_ativo[ativo] = perdas
                if perdas >= self.cooldown_losses:
                    self._cooldown_ate_por_ativo[ativo] = (
                        time.time() + self.cooldown_seconds
                    )
                self._estado_operacional = "POST_LOSS_COOLDOWN"
                self._cooldown_global_ate = (
                    time.time() + self.cooldown_pos_loss_seconds
                )
                self._motivo_parada = (
                    "[PROTEÇÃO] LOSS confirmado. Cooldown global ativado."
                )
            elif self._estado_operacional not in {
                "MANUAL_PAUSE",
                "INDETERMINATE_BLOCK",
                "POST_WIN_PROTECTION",
                "POST_LOSS_COOLDOWN",
                "DEMO_OBSERVATION",
            }:
                self._estado_operacional = "NORMAL"
                self._motivo_parada = None

        self._atualizar_estado_financeiro()

    def registrar_resultado_ordem(self, order_id, resultado, lucro, ativo):
        resultado = str(resultado).upper()
        if resultado not in {"WIN", "LOSS", "DRAW", "INDETERMINADO"}:
            self.bloquear_indeterminado(
                f"Resultado de ordem não reconhecido: {resultado}."
            )
            return False
        with self._lock:
            order_key = str(order_id) if order_id is not None else None
            if order_key is not None and order_key in self._resultados_processados:
                return True
            self._contingencia_retomada_manual = False
            if order_key is not None:
                self._ordens_abertas.pop(order_key, None)
                self._ordens_indeterminadas.discard(order_key)
            if resultado == "INDETERMINADO":
                self._estado_operacional = "INDETERMINATE_BLOCK"
                self._motivo_parada = (
                    "Resultado INDETERMINADO; reconciliação obrigatória."
                )
                if order_key is not None:
                    self._ordens_indeterminadas.add(order_key)
            else:
                self._registrar_resultado_confirmado(resultado, lucro, ativo)
                if order_key is not None:
                    self._resultados_processados.add(order_key)
                if (
                    self._estado_operacional == "INDETERMINATE_BLOCK"
                    and not self._ordens_indeterminadas
                ):
                    self._estado_operacional = "DEMO_OBSERVATION"
            return self._persistir_estado()

    def reconciliar_resultado(self, order_id, resultado, lucro, ativo):
        resultado = str(resultado).upper()
        if resultado not in {"WIN", "LOSS", "DRAW"}:
            raise ValueError("Reconciliação exige WIN, LOSS ou DRAW confirmado.")
        with self._lock:
            if self._estado_operacional != "INDETERMINATE_BLOCK":
                return False
            if order_id is not None:
                order_key = str(order_id)
                self._ordens_indeterminadas.discard(order_key)
                self._ordens_abertas.pop(order_key, None)
            if self._ordens_indeterminadas:
                return False
            self._estado_operacional = "DEMO_OBSERVATION"
            self._motivo_parada = None
            return self.registrar_resultado_ordem(
                order_id, resultado, lucro, ativo
            )

    def calcular_stake(self, banca_atual):
        with self._lock:
            banca_atual = float(banca_atual)
            if not math.isfinite(banca_atual) or banca_atual <= 0:
                raise ValueError("A banca atual deve ser finita e maior que zero.")
            if self.banca_inicial_sessao is None:
                self.banca_inicial_sessao = banca_atual
                self.saldo_referencia_stop = banca_atual
                self._persistir_estado()
            stake_risco = self.banca_inicial_sessao * self.pct_risco
            if self.valor_stake_base is not None:
                stake_risco = min(stake_risco, self.valor_stake_base)
            stake = round(max(2.0, stake_risco), 2)
            if stake > banca_atual:
                raise ValueError("Stake base excede o saldo atual disponível.")
            return stake

    def registrar_pnl_ordem(self, lucro):
        lucro = float(lucro)
        if not math.isfinite(lucro):
            raise ValueError("O resultado da ordem deve ser finito.")
        with self._lock:
            if self.banca_inicial_sessao is None:
                raise ValueError(
                    "Saldo inicial necessário para registrar PnL."
                )
            self.lucro_acumulado_sessao += lucro
            self.lucro_maximo_sessao = max(
                self.lucro_maximo_sessao, self.lucro_acumulado_sessao
            )
            banca_atual = self.banca_inicial_sessao + self.lucro_acumulado_sessao
            if self.saldo_referencia_stop is None or banca_atual > self.saldo_referencia_stop:
                self.saldo_referencia_stop = banca_atual
            self._atualizar_estado_financeiro()
            return self._persistir_estado()

    def registrar_resultado_ciclo(self, status, ativo):
        resultado = str(status).upper()
        if resultado not in {"WIN", "LOSS", "DRAW"}:
            return
        with self._lock:
            if resultado == "WIN":
                self._perdas_consecutivas_por_ativo.pop(ativo, None)
            elif resultado == "LOSS":
                perdas = self._perdas_consecutivas_por_ativo.get(ativo, 0) + 1
                self._perdas_consecutivas_por_ativo[ativo] = perdas
                if perdas >= self.cooldown_losses:
                    self._cooldown_ate_por_ativo[ativo] = (
                        time.time() + self.cooldown_seconds
                    )
            self._persistir_estado()

    def registrar_resultado(self, status, lucro, ativo=None):
        if ativo is None:
            self.registrar_pnl_ordem(lucro)
            return
        self.registrar_resultado_ordem(None, status, lucro, ativo)

    def restante_cooldown(self, ativo):
        with self._lock:
            expiracao = self._cooldown_ate_por_ativo.get(ativo, 0.0)
            restante = expiracao - time.time()
            if restante <= 0:
                if expiracao:
                    self._cooldown_ate_por_ativo.pop(ativo, None)
                    self._persistir_estado()
                return 0.0
            return restante

    def obter_estado_operacional(self):
        with self._lock:
            self._atualizar_parada()
            return self._estado_operacional

    def modo_observacao(self):
        return self.obter_estado_operacional() in {
            "POST_WIN_PROTECTION",
            "POST_LOSS_COOLDOWN",
            "DEMO_OBSERVATION",
        }

    def contingencia_apos_pausa_manual(self):
        with self._lock:
            self._atualizar_parada()
            return (
                self._contingencia_retomada_manual
                and self._estado_operacional == "DEMO_OBSERVATION"
                and self._cooldown_global_ate <= time.time()
            )

    def definir_pausa_manual(self, pausada):
        with self._lock:
            if pausada:
                self._contingencia_retomada_manual = False
                if self._estado_operacional in {
                    "DAILY_TARGET_REACHED",
                    "DAILY_STOP_LOSS",
                    "INDETERMINATE_BLOCK",
                }:
                    return False
                self._estado_operacional = "MANUAL_PAUSE"
                self._motivo_parada = "Pausa manual solicitada."
            elif self._estado_operacional == "MANUAL_PAUSE":
                self._estado_operacional = "DEMO_OBSERVATION"
                self._motivo_parada = None
                self._contingencia_retomada_manual = (
                    self._cooldown_global_ate <= time.time()
                )
            return self._persistir_estado()

    def calcular_stake_recuperacao(
        self, prejuizo, lucro_alvo, payout_percentual, banca_atual
    ):
        valores = (prejuizo, lucro_alvo, payout_percentual, banca_atual)
        try:
            prejuizo, lucro_alvo, payout_percentual, banca_atual = (
                Decimal(str(valor)) for valor in valores
            )
        except (InvalidOperation, ValueError) as erro:
            raise ValueError("Valores inválidos para calcular a recuperação.") from erro

        if not all(
            valor.is_finite()
            for valor in (prejuizo, lucro_alvo, payout_percentual, banca_atual)
        ):
            raise ValueError("Valores não finitos para calcular a recuperação.")
        if prejuizo <= 0 or lucro_alvo < 0 or banca_atual <= 0:
            raise ValueError("Prejuízo, lucro-alvo ou banca fora do intervalo válido.")
        if payout_percentual <= 0 or payout_percentual > 100:
            raise ValueError("O payout deve estar entre 0 e 100.")

        payout = payout_percentual / Decimal("100")
        stake = ((prejuizo + lucro_alvo) / payout).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )
        if stake > banca_atual:
            return None
        return float(stake)

    def deve_parar(self, banca_atual=None):
        with self._lock:
            if self.banca_inicial_sessao is None and banca_atual is not None:
                self.banca_inicial_sessao = float(banca_atual)
                self.saldo_referencia_stop = self.banca_inicial_sessao
            return self._atualizar_parada()

    # ✅ NOVO: Método explícito e seguro para reset da trava de risco
    def reset_trava_stop(self):
        """
        Reset seguro da trava de stop loss dinâmico.
        
        Use quando precisar limpar a parada por stop loss após confirmar
        que foi falso positivo ou após validação manual.
        
        ⚠️ NÃO use sem confirmação explícita do usuário!
        
        Returns:
            tuple: (sucesso: bool, mensagem: str)
        """
        with self._lock:
            if self._estado_operacional != "DAILY_STOP_LOSS":
                return False, "Nenhuma trava de stop loss ativa para resetar."
            motivo_anterior = self._motivo_parada
            banca_atual = (
                self.banca_inicial_sessao + self.lucro_acumulado_sessao
            )
            if banca_atual <= 0:
                return False, "Saldo atual inválido para redefinir a referência."
            referencia_anterior = (
                self.saldo_referencia_stop or banca_atual
            )
            self.saldo_referencia_stop = banca_atual
            self._motivo_parada = None
            self._estado_operacional = "DEMO_OBSERVATION"
            self.ignorar_trava_stop = False
            self._timestamp_reset_trava = time.time()
            self._persistir_estado()
            
            logger.warning(
                "[RESET TRAVA] Referência de stop recalibrada de R$ %.2f "
                "para R$ %.2f após confirmação manual. Motivo anterior: %s",
                referencia_anterior,
                banca_atual,
                motivo_anterior,
            )
            return True, f"Referência recalibrada. Motivo anterior: {motivo_anterior}"

    def obter_motivo_parada(self):
        """Retorna o motivo atual de parada sem efeitos colaterais."""
        with self._lock:
            parada, motivo = self._atualizar_parada()
            return motivo if parada else None

    def obter_status_resumido(self):
        """Retorna resumo do estado para logging descritivo."""
        with self._lock:
            banca_atual = (
                self.banca_inicial_sessao + self.lucro_acumulado_sessao
                if self.banca_inicial_sessao else 0.0
            )
            parada, motivo = self._atualizar_parada()
            return {
                "banca_inicial": self.banca_inicial_sessao,
                "banca_atual": banca_atual,
                "saldo_referencia_stop": self.saldo_referencia_stop,
                "lucro_acumulado": self.lucro_acumulado_sessao,
                "lucro_maximo": self.lucro_maximo_sessao,
                "meta_lucro_sessao": self.meta_lucro_sessao,
                "estado_operacional": self._estado_operacional,
                "vitorias_consecutivas": self._vitorias_consecutivas,
                "parada": parada,
                "motivo_parada": motivo,
                "cooldowns_ativos": len([
                    t for t in self._cooldown_ate_por_ativo.values()
                    if t > time.time()
                ]),
            }
