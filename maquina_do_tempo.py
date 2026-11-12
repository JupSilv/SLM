import argparse
import json
import logging
import math
import sys
import time
from pathlib import Path

from app.time_machine import INTERVALS, simulate, validate_dataset


def collect(broker, asset, days, end):
    import iqoptionapi.constants as constants

    catalog = broker.get_all_init_v2()
    if not isinstance(catalog, dict):
        raise ValueError("Catálogo da corretora indisponível.")
    catalog = catalog.get("result", catalog)
    if not isinstance(catalog, dict):
        raise ValueError("Catálogo da corretora inválido.")
    binary = catalog.get("binary")
    if not isinstance(binary, dict) or not isinstance(binary.get("actives"), dict):
        raise ValueError("Catálogo binário indisponível.")
    matches = [
        (key, value) for key, value in binary["actives"].items()
        if isinstance(value, dict)
        and str(value.get("name", "")).rsplit(".", 1)[-1].upper() == asset
    ]
    if len(matches) != 1:
        raise ValueError("Ativo não encontrado ou duplicado no catálogo binário.")
    key, info = matches[0]
    payout = 100.0 - float(info["option"]["profit"]["commission"])
    if not math.isfinite(payout) or not 0 < payout <= 100:
        raise ValueError("Payout atual inválido para o ativo.")
    snapshot_time = int(time.time())
    constants.ACTIVES[asset] = int(info.get("active_id", info.get("id", key)))
    return collect_candles(broker, asset, payout, days, end, snapshot_time)


def collect_candles(broker, asset, payout, days, end, snapshot_time):
    start = end - days * 86400
    warmup_start = start - 60 * 900
    candles = {}
    for interval in INTERVALS:
        end_cursor = end
        by_time = {}
        while end_cursor > warmup_start:
            batch = broker.get_candles(asset, interval, 1000, end_cursor)
            if not isinstance(batch, list) or not batch:
                raise ValueError(f"Corretora não retornou candles {interval}s.")
            before = len(by_time)
            for candle in batch:
                timestamp = int(candle["from"])
                if timestamp > 10**12:
                    timestamp //= 1000
                if warmup_start <= timestamp and timestamp + interval <= end:
                    by_time[timestamp] = {**candle, "from": timestamp}
            timestamps = [int(candle["from"]) for candle in batch]
            oldest = min(
                timestamp // 1000 if timestamp > 10**12 else timestamp
                for timestamp in timestamps
            )
            if oldest >= end_cursor or (len(by_time) == before and oldest > warmup_start):
                raise ValueError(f"Paginação de candles {interval}s sem progresso.")
            end_cursor = oldest
        candles[str(interval)] = [by_time[key] for key in sorted(by_time)]
    data = {
        "format": "deskubuddy-candles-v1",
        "asset": asset,
        "start": start,
        "end": end,
        "payout_snapshot_percent": payout,
        "payout_snapshot_time": snapshot_time,
        "candles": candles,
    }
    validate_dataset(data)
    return data


def main():
    parser = argparse.ArgumentParser(description="Coleta separada e simulação offline; nunca envia ordens.")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("coletar", help="Conecta à corretora somente para catálogo e candles.")
    fetch.add_argument("--ativo", required=True, help="Nome exato no catálogo, ex.: EURUSD-OP")
    fetch.add_argument("--dias", type=int, default=3)
    fetch.add_argument("--saida", required=True)
    replay = sub.add_parser("simular", help="Simula offline um arquivo previamente coletado.")
    replay.add_argument("--dados", required=True)
    replay.add_argument("--saida", required=True)
    args = parser.parse_args()
    try:
        output = Path(args.saida)
        if output.exists():
            raise ValueError("Arquivo de saída já existe; escolha outro nome.")
        if args.command == "coletar":
            if not 1 <= args.dias <= 30:
                raise ValueError("--dias deve estar entre 1 e 30.")
            from config import Config
            from iqoptionapi.stable_api import IQ_Option

            if not Config.IQ_EMAIL or not Config.IQ_PASSWORD:
                raise ValueError("Configure IQ_EMAIL e IQ_PASSWORD no ambiente ou .env.")
            print("Coleta de dados: conexão à corretora, sem envio de ordens.")
            broker = IQ_Option(Config.IQ_EMAIL, Config.IQ_PASSWORD)
            connected, _ = broker.connect()
            if not connected:
                raise RuntimeError("Autenticação recusada ou verificação adicional necessária.")
            data = collect(broker, args.ativo.strip().upper(), args.dias, int(time.time()))
        else:
            with Path(args.dados).open(encoding="utf-8") as source:
                data = simulate(json.load(source))
        with output.open("x", encoding="utf-8") as destination:
            json.dump(data, destination, ensure_ascii=False, indent=2, allow_nan=False)
        print(f"Arquivo salvo: {output}")
        if args.command == "simular":
            print(json.dumps(data["summary"], ensure_ascii=False, indent=2))
            print("Payout da coleta é hipótese para o passado; veja limitations no relatório.")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        logging.error("Máquina do tempo: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
