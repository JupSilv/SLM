# app/executor.py
import time
import threading

from config import Config
from iqoptionapi.expiration import get_expiration_time


class AsyncExecutionEngine:
    def __init__(self, broker, risk_manager, tracker):
        self.broker = broker
        self.risk = risk_manager
        self.tracker = tracker
        self.logger = tracker.logger

    def execute_signal_async(self, sinal):
        """Dispara a execução em uma thread dedicada para não travar o loop principal de varredura."""
        t = threading.Thread(target=self._process_execution, args=(sinal,))
        t.daemon = True
        t.start()

    def _process_execution(self, sinal):
        # 1. Sincroniza o envio com o relógio do broker.
        horario_entrada = int(sinal["executar_em"])
        segundo_disparo = int(Config.ORDER_DISPATCH_SECOND)
        if segundo_disparo not in {0, 58, 59}:
            raise ValueError(
                "ORDER_DISPATCH_SECOND deve ser 58, 59 ou 00."
            )
        horario_disparo = (
            horario_entrada
            if segundo_disparo == 0
            else horario_entrada - (60 - segundo_disparo)
        )
        while True:
            obter_timestamp = getattr(
                self.broker, "get_server_timestamp", None
            )
            ts_servidor = (
                float(obter_timestamp())
                if callable(obter_timestamp)
                else float(self.broker.get_ts())
            )
            if ts_servidor >= horario_disparo:
                break
            time.sleep(0.005)

        atraso = ts_servidor - horario_disparo
        if atraso > 2:
            self.logger.warning(
                f"Janela de envio no segundo {segundo_disparo:02d} perdida; "
                f"atraso={atraso * 1000:.0f} ms."
            )
            return

        tipo = sinal["tipo"]
        ativo = sinal["ativo"]
        direcao = sinal["sinal"].lower()
        token, motivo = self.risk.reservar_entrada(
            ativo, sinal.get("timestamp")
        )
        if token is None:
            self.logger.info("Entrada bloqueada: %s", motivo)
            return

        # 2. Risco e Dimensionamento
        try:
            banca = self.broker.get_balance()
            stake = self.risk.calcular_stake(banca)
        except Exception:
            self.risk.cancelar_reserva_entrada(token)
            raise

        if stake < 2.0:
            self.logger.warning(f"Stake R$ {stake:.2f} abaixo do mínimo da corretora.")
            self.risk.cancelar_reserva_entrada(token)
            return

        # 3. Disparo da ordem
        try:
            if tipo == 'digital':
                status, order_id = self.broker.buy_digital_spot(
                    ativo, stake, direcao, 1
                )
                expira_em = int(sinal["expira_em"])
            else:
                expiracao_minutos = int(sinal.get("expiracao_minutos", 1))
                agora_broker = (
                    float(obter_timestamp())
                    if callable(obter_timestamp)
                    else float(self.broker.get_ts())
                )
                expira_em, _ = get_expiration_time(
                    int(agora_broker), expiracao_minutos
                )
                status, order_id = self.broker.buy(
                    stake, ativo, direcao, expiracao_minutos
                )
        except Exception:
            self.risk.bloquear_indeterminado(
                "Envio da ordem sem confirmação da corretora."
            )
            raise

        if not status or not order_id:
            self.logger.error(f"Falha na abertura da ordem no ativo {ativo}")
            if status:
                self.risk.confirmar_entrada(token, None)
            else:
                self.risk.cancelar_reserva_entrada(token)
            return

        try:
            self.risk.confirmar_entrada(token, order_id)
        except Exception:
            self.risk.bloquear_indeterminado(
                "Ordem aceita sem reserva de risco confirmada.",
                order_id=order_id,
            )
            raise
        self.logger.info(f"ORDEM ENVIADA | Ativo: {ativo} | ID: {order_id} | Stake: R$ {stake:.2f}")

        # 4. Checagem Não-Bloqueante com polling
        try:
            self._poll_result(
                order_id,
                tipo,
                ativo,
                sinal['setup'],
                sinal['sinal'],
                stake,
                sinal['payout'],
                int(expira_em),
            )
        except Exception:
            self.risk.bloquear_indeterminado(
                "Falha monitorando o resultado da ordem.",
                order_id=order_id,
            )
            raise

    def _poll_result(
        self, order_id, tipo, ativo, setup, direcao, stake, payout, expira_em
    ):
        # Aguarda o vencimento escolhido pelo analyzer, com pequena margem de liquidação.
        agora = int(self.broker.get_ts())
        time.sleep(max(0, int(expira_em) - agora) + 5)
        
        resultado, pnl = 'indeterminado', 0.0
        for _ in range(15):
            try:
                if tipo == 'digital':
                    res = self.broker.check_win_digital_v2_timeout(str(order_id), timeout=3)
                else:
                    res = self.broker.check_win_binary_historico(str(order_id), timeout=3)

                if res is not None:
                    if isinstance(res, (int, float)):
                        if res > 0:
                            resultado, pnl = 'win', res
                        elif res < 0:
                            resultado, pnl = 'loss', res
                        else:
                            resultado, pnl = 'draw', 0.0
                        break
                    elif isinstance(res, tuple):
                        st, val = res[0], res[1]
                        if st in ['win', True]:
                            resultado, pnl = 'win', abs(val)
                        elif st in ['loss', False]:
                            resultado, pnl = 'loss', -abs(val)
                        else:
                            resultado, pnl = 'draw', 0.0
                        break
            except Exception as e:
                self.logger.error(f"Erro ao checar resultado ({order_id}): {e}")
            time.sleep(1)

        # Atualiza o gerenciador de risco e o log estatístico
        self.risk.registrar_resultado_ordem(order_id, resultado, pnl, ativo)
        self.tracker.log_trade(ativo, setup, direcao, stake, payout, resultado, pnl)