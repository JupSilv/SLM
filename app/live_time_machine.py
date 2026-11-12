import logging
import math
import threading
import time
import uuid

from PySide6.QtCore import QThread, Signal
import iqoptionapi.constants as constants

from app.time_machine import INTERVALS, simulate, validate_dataset
from maquina_do_tempo import collect_candles


class CorrelatedCandleReader:
    """Keep simulation responses separate from the legacy shared candle slot."""

    def __init__(self, broker, stop_event, send_lock, timeout=8):
        self.broker = broker
        self.stop_event = stop_event
        self.send_lock = send_lock
        self.timeout = timeout

    def get_candles(self, asset, interval, count, endtime):
        api = self.broker.api
        if not hasattr(api, "_deskubuddy_tm_lock"):
            api._deskubuddy_tm_lock = threading.Lock()
            api._deskubuddy_tm_pending = {}
        active_id = constants.ACTIVES.get(asset)
        if active_id is None:
            raise ValueError(f"Ativo sem identificador na API: {asset}")
        request_id = "deskubuddy-tm-" + uuid.uuid4().hex
        request = {"event": threading.Event(), "data": None}
        with api._deskubuddy_tm_lock:
            api._deskubuddy_tm_pending[request_id] = request
        try:
            body = {
                "name": "get-candles",
                "version": "2.0",
                "body": {
                    "active_id": int(active_id),
                    "split_normalization": True,
                    "size": interval,
                    "to": int(endtime),
                    "count": count,
                    "": int(active_id),
                },
            }
            deadline = time.monotonic() + self.timeout
            while not self.send_lock.acquire(timeout=0.1):
                if self.stop_event.is_set():
                    raise InterruptedError("Coleta interrompida.")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Tempo esgotado aguardando envio de candles.")
            try:
                if self.stop_event.is_set():
                    raise InterruptedError("Coleta interrompida.")
                api.send_websocket_request("sendMessage", body, request_id)
            finally:
                self.send_lock.release()
            while not request["event"].wait(0.1):
                if self.stop_event.is_set():
                    raise InterruptedError("Coleta interrompida.")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Tempo esgotado buscando candles de {asset}.")
            if not isinstance(request["data"], list):
                raise ValueError(f"Resposta de candles inválida para {asset}.")
            return request["data"]
        finally:
            with api._deskubuddy_tm_lock:
                api._deskubuddy_tm_pending.pop(request_id, None)


class LiveTimeMachine(QThread):
    log_message = Signal(str)
    DAYS = 3
    MIN_RESULTS = 20
    REFRESH_SECONDS = 30
    MAX_AGE_SECONDS = 900

    def __init__(self, broker, account_type, send_lock):
        super().__init__()
        self.broker = broker
        self.account_type = account_type.upper()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._assets = {}
        self._reports = {}
        self._datasets = {}
        self._announced = {}
        self._started_at = int(broker.get_ts())
        self._reader = CorrelatedCandleReader(broker, self._stop, send_lock)

    def _log(self, message):
        logging.info(message)
        self.log_message.emit(message)

    def set_assets(self, items):
        with self._lock:
            self._assets = {item["ativo"]: dict(item) for item in items}
            for asset in set(self._reports) - set(self._assets):
                self._reports.pop(asset, None)

    def stop(self):
        self._stop.set()

    def evaluate(self, signal, contingency):
        scenario = (
            "contingencia_m15_hipotetica" if contingency
            else "regra_m5_e_contexto"
        )
        asset = signal["ativo"]
        with self._lock:
            record = self._reports.get(asset)
        if record is None or time.monotonic() - record[0] > self.MAX_AGE_SECONDS:
            return False, "Máquina do tempo indisponível ou desatualizada; filtro não aplicado."
        matches = [
            result for result in record[1]["results"]
            if result["cenario"] == scenario
            and result["direcao"] == signal["sinal"]
            and result["setup"] == signal["setup"]
            and result["resultado"] in ("WIN", "LOSS")
        ]
        n = len(matches)
        if n < self.MIN_RESULTS:
            return False, f"Máquina do tempo: amostra comparável insuficiente ({n}/{self.MIN_RESULTS}); filtro não aplicado."
        payout = float(signal["payout"])
        if not math.isfinite(payout) or not 0 < payout <= 100:
            raise ValueError("Payout inválido para filtro da máquina do tempo.")
        wins = sum(result["resultado"] == "WIN" for result in matches)
        rate = wins / n
        threshold = 1 / (1 + payout / 100)
        detail = (
            f"Máquina do tempo {scenario}: {wins}/{n} WIN ({rate:.1%}); "
            f"equilíbrio estimado {threshold:.1%}; payout atual hipotético."
        )
        return rate < threshold, detail

    def _announce(self, asset, results, now):
        for result in results:
            if result["avaliado_em"] < self._started_at:
                continue
            key = (asset, result["cenario"], result["avaliado_em"])
            if result["entrada_em"] > now:
                continue
            previous = self._announced.get(key)
            if previous is None:
                delay = max(0, now - result["entrada_em"])
                self._log(
                    f"[ENTRADA SIMULADA {asset}] {result['cenario']} "
                    f"{result['direcao']} | entrada={result['entrada_em']} "
                    f"| expiração={result['expira_em']} | atraso de registro={delay}s; "
                    "nenhuma ordem enviada."
                )
                previous = "PENDENTE"
            outcome = result["resultado"]
            if now >= result["expira_em"] and outcome != previous:
                self._log(
                    f"[RESULTADO SIMULADO {asset}] {result['cenario']} "
                    f"{result['direcao']} | {outcome} | "
                    "estimativa por candles, não resultado da corretora."
                )
                previous = outcome
            self._announced[key] = previous
        cutoff = now - self.DAYS * 86400
        for key in list(self._announced):
            if key[2] < cutoff:
                del self._announced[key]

    def _refresh(self, item):
        asset = item["ativo"]
        refresh_started = time.monotonic()
        now = int(self.broker.get_ts())
        data = self._datasets.get(asset)
        if data is None or now <= data["end"] or now - data["end"] > 59000:
            data = collect_candles(
                self._reader, asset, float(item["payout"]), self.DAYS, now, now
            )
        else:
            start = now - self.DAYS * 86400
            warmup_start = start - 60 * 900
            updated = {}
            for interval in INTERVALS:
                count = (now - data["end"]) // interval + 3
                batch = self._reader.get_candles(asset, interval, count, now)
                if not batch:
                    raise ValueError(f"Corretora não retornou candles {interval}s.")
                by_time = {
                    candle["from"]: candle
                    for candle in data["candles"][str(interval)]
                    if candle["from"] >= warmup_start
                }
                for candle in batch:
                    timestamp = int(candle["from"])
                    if timestamp > 10**12:
                        timestamp //= 1000
                    if warmup_start <= timestamp and timestamp + interval <= now:
                        by_time[timestamp] = {**candle, "from": timestamp}
                updated[str(interval)] = [by_time[key] for key in sorted(by_time)]
            data = {
                **data,
                "start": start,
                "end": now,
                "payout_snapshot_percent": float(item["payout"]),
                "payout_snapshot_time": now,
                "candles": updated,
            }
            validate_dataset(data)
        for interval in INTERVALS:
            latest = data["candles"][str(interval)][-1]["from"] + interval
            expected = (now // interval) * interval
            if latest < expected - interval:
                raise ValueError(f"Candles {interval}s desatualizados para {asset}.")
        if self._stop.is_set():
            return
        report = simulate(data)
        with self._lock:
            if asset not in self._assets:
                return
            self._reports[asset] = (refresh_started, report)
        self._datasets[asset] = data
        self._announce(asset, report["results"], now)

    def run(self):
        self._log(
            "[MÁQUINA DO TEMPO] Simulação automática iniciada; "
            f"filtro adicional ativo em {self.account_type}."
        )
        next_refresh = {}
        while not self._stop.is_set():
            with self._lock:
                assets = list(self._assets.values())
            active_names = {item["ativo"] for item in assets}
            for asset in (set(self._datasets) | set(next_refresh)) - active_names:
                self._datasets.pop(asset, None)
                next_refresh.pop(asset, None)
            now = time.monotonic()
            due = min(
                assets, key=lambda item: next_refresh.get(item["ativo"], 0),
                default=None,
            )
            if due is None or next_refresh.get(due["ativo"], 0) > now:
                self._stop.wait(1)
                continue
            asset = due["ativo"]
            try:
                first = asset not in self._datasets
                self._refresh(due)
                if first and asset in self._datasets:
                    self._log(
                        f"[MÁQUINA DO TEMPO {asset}] Histórico de {self.DAYS} dias "
                        "carregado; M5 e M15 separados. Payout histórico é hipótese."
                    )
            except InterruptedError:
                if not self._stop.is_set():
                    raise
            except Exception:
                with self._lock:
                    self._reports.pop(asset, None)
                self._datasets.pop(asset, None)
                logging.exception("Falha na máquina do tempo para %s.", asset)
                self.log_message.emit(
                    f"[MÁQUINA DO TEMPO {asset}] Falha na coleta/simulação; "
                    "filtro indisponível. Consulte o log operacional."
                )
            next_refresh[asset] = time.monotonic() + self.REFRESH_SECONDS
