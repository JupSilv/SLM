import logging
import random
from datetime import datetime

import iqoptionapi.constants as OP_code
from app.sessions import (
    active_sessions,
    as_utc,
    local_session_hours,
    preferred_currencies,
    session_ids_for,
    session_names,
)
from database import obter_historico_otc_simulado

PAIR_SELECTION_LIMIT = 10
logger = logging.getLogger(__name__)


_CURRENCY_CODES = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD
    BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE
    CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD GBP GEL GHS GIP GMD GNF GTQ
    GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR
    KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK
    MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB
    PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD
    SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS
    UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW
    """.split()
)


def is_currency_pair(name):
    pair = str(name).upper()
    if pair.endswith("-OTC"):
        pair = pair[:-4]
    elif pair.endswith("-OP"):
        pair = pair[:-3]
    return (
        len(pair) == 6
        and pair.isalpha()
        and pair[:3] in _CURRENCY_CODES
        and pair[3:] in _CURRENCY_CODES
        and pair[:3] != pair[3:]
    )


class DynamicPairSelector:
    MAX_CURRENCY_EXPOSURE = 3
    SESSION_CURRENCY_BOOST = 3.0
    OTC_HISTORY_MIN_SAMPLES = 5
    OTC_HISTORY_SCORE_WEIGHT = 10.0
    OTC_HISTORY_SCORE_CAP = 5.0

    def __init__(self, broker, logger, limit=PAIR_SELECTION_LIMIT):
        if limit is not None and limit < 1:
            raise ValueError("O limite de pares deve ser maior que zero.")
        self.broker = broker
        self.logger = logger
        self.limit = limit
        self.last_catalog_success = False

    @staticmethod
    def is_otc_period(now=None):
        momento_utc = as_utc(now)
        return momento_utc.weekday() >= 5 or not 5 <= momento_utc.hour < 17

    @staticmethod
    def _as_bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
        return False

    @classmethod
    def _select_diversified(cls, candidates, limit):
        if limit <= 0:
            return []

        selected = []
        deferred = []
        currency_exposure = {}

        for candidate in candidates:
            pair = candidate["ativo"]
            if pair.endswith("-OTC"):
                pair = pair[:-4]
            currencies = (pair[:3], pair[3:])
            if all(
                currency_exposure.get(currency, 0) < cls.MAX_CURRENCY_EXPOSURE
                for currency in currencies
            ):
                selected.append(candidate)
                for currency in currencies:
                    currency_exposure[currency] = (
                        currency_exposure.get(currency, 0) + 1
                    )
                if len(selected) == limit:
                    return selected
            else:
                deferred.append(candidate)

        selected.extend(deferred[: limit - len(selected)])
        return selected

    @staticmethod
    def _historico_otc_por_sessao(historico, session_ids, utc_hour):
        sessoes_atuais = set(session_ids) or {"off_hours"}
        desempenho = {}
        for item in historico:
            try:
                timestamp = datetime.fromisoformat(item["timestamp"])
                timestamp_utc = as_utc(timestamp)
                if timestamp_utc.hour != utc_hour:
                    continue
                sessoes_historicas = set(
                    session_ids_for(timestamp_utc)
                ) or {"off_hours"}
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "Registro simulado OTC ignorado por timestamp inválido: %r",
                    item.get("timestamp"),
                )
                continue
            if not sessoes_atuais.intersection(sessoes_historicas):
                continue
            ativo = str(item.get("ativo", "")).upper()
            if not ativo.endswith("-OTC"):
                continue
            resultado = item.get("resultado")
            if resultado not in {"PAPER_WIN", "PAPER_LOSS"}:
                continue
            wins, losses = desempenho.setdefault(ativo, [0, 0])
            if resultado == "PAPER_WIN":
                wins += 1
            else:
                losses += 1
            desempenho[ativo] = [wins, losses]
        return desempenho

    def _ordenar_por_prioridade(
        self, candidatos, moedas_prioritarias, historico_otc
    ):
        detalhes = {}
        for candidato in candidatos:
            ativo = candidato["ativo"]
            par = ativo[:-4] if ativo.endswith("-OTC") else ativo
            moedas = (par[:3], par[3:])
            moedas_correspondentes = tuple(
                moeda for moeda in moedas if moeda in moedas_prioritarias
            )
            boost_sessao = (
                len(moedas_correspondentes) * self.SESSION_CURRENCY_BOOST
            )

            wins, losses = historico_otc.get(ativo, (0, 0))
            amostras = wins + losses
            boost_historico = 0.0
            taxa_acerto = None
            if amostras >= self.OTC_HISTORY_MIN_SAMPLES:
                taxa_acerto = wins / amostras
                taxa_suavizada = (wins + 3) / (amostras + 6)
                boost_historico = max(
                    -self.OTC_HISTORY_SCORE_CAP,
                    min(
                        self.OTC_HISTORY_SCORE_CAP,
                        (taxa_suavizada - 0.5) * self.OTC_HISTORY_SCORE_WEIGHT,
                    ),
                )

            detalhes[ativo] = {
                "moedas_correspondentes": moedas_correspondentes,
                "boost_sessao": boost_sessao,
                "amostras_simuladas": amostras,
                "taxa_acerto_simulada": taxa_acerto,
                "boost_historico": boost_historico,
                "score": candidato["payout"] + boost_sessao + boost_historico,
            }

        return (
            sorted(
                candidatos,
                key=lambda item: (
                    -detalhes[item["ativo"]]["score"],
                    -item["payout"],
                    item["ativo"],
                ),
            ),
            detalhes,
        )

    def obter_ativos_operaveis(self, now=None):
        self.last_catalog_success = False
        momento_utc = as_utc(now)
        momento_local = momento_utc.astimezone()
        periodo_otc = self.is_otc_period(momento_utc)
        sessoes_ativas = active_sessions(momento_utc)
        moedas_prioritarias = preferred_currencies(sessoes_ativas)
        nomes_sessoes = session_names(sessoes_ativas)
        sessoes_locais = local_session_hours(momento_utc)
        self.logger.info(
            "Varredura por sessão: ativas=%s | UTC=%s | local=%s | "
            "moedas priorizadas=%s | horários locais=%s.",
            ", ".join(nomes_sessoes) or "nenhuma sessão cambial regular",
            momento_utc.isoformat(timespec="seconds"),
            momento_local.isoformat(timespec="seconds"),
            ", ".join(sorted(moedas_prioritarias)) or "nenhuma",
            "; ".join(
                f"{nome}={horario}" for nome, horario in sessoes_locais.items()
            ),
        )
        try:
            catalogo = self.broker.get_all_init_v2()
        except Exception as erro:
            self.logger.warning(f"Falha ao consultar catálogo de ativos binários: {erro}")
            return []

        if not isinstance(catalogo, dict):
            self.logger.warning("Resposta inválida ao consultar catálogo de ativos binários.")
            return []

        catalogo = catalogo.get("result", catalogo)
        if not isinstance(catalogo, dict):
            self.logger.warning("Resposta inválida ao consultar catálogo de ativos binários.")
            return []
        binary_data = catalogo.get("binary")
        if not isinstance(binary_data, dict):
            self.logger.warning("Catálogo binário não contém estrutura válida de ativos.")
            return []
        binary_actives = binary_data.get("actives")
        if not isinstance(binary_actives, dict):
            self.logger.warning("Catálogo binário não contém mapa válido de ativos.")
            return []
        self.last_catalog_success = True

        candidatos_regulares = []
        candidatos_otc = []
        contagem = {
            "binary": 0,
            "regulares": 0,
            "otc": 0,
            "abertos_regulares": 0,
            "abertos_otc": 0,
        }
        nomes_catalogo = []
        for catalog_id, detalhe in binary_actives.items():
            if not isinstance(detalhe, dict):
                continue
            
            # Pega o nome bruto ex: 'front.CADCHF-op' -> vira 'CADCHF-OP'
            nome_bruto = str(detalhe.get("name", "")).rsplit(".", 1)[-1].upper()
            if not is_currency_pair(nome_bruto):
                continue
            
            contagem["binary"] += 1
            nomes_catalogo.append(nome_bruto)
            
            eh_otc = nome_bruto.endswith("-OTC")
            tipo_ativo = "otc" if eh_otc else "regulares"
            contagem[tipo_ativo] += 1

            if not self._as_bool(detalhe.get("enabled")) or self._as_bool(
                detalhe.get("is_suspended")
            ):
                continue
            contagem[f"abertos_{tipo_ativo}"] += 1

            try:
                payout = 100.0 - float(
                    detalhe["option"]["profit"]["commission"]
                )
            except (KeyError, TypeError, ValueError):
                continue
            if payout <= 0:
                continue
                
            active_id = detalhe.get("active_id", detalhe.get("id", catalog_id))
            
            # ATIVO LIMPO para a IQ Option (ex: CADCHF) ou mantido com sufixo se necessário pela API
            # Se a API aceita o ticker limpo no OP_code, use o nome base:
            ativo_limpo = nome_bruto[:-4] if eh_otc else (nome_bruto[:-3] if nome_bruto.endswith("-OP") else nome_bruto)
            
            try:
                OP_code.ACTIVES[nome_bruto] = int(active_id)
                OP_code.ACTIVES[ativo_limpo] = int(active_id)
            except (TypeError, ValueError):
                if nome_bruto not in OP_code.ACTIVES:
                    continue
                    
            candidato = {
                "ativo": nome_bruto, # ou ativo_limpo dependendo de como o worker consome
                "tipo": "binary",
                "payout": payout,
                "expiracao_minutos": 15,
            }
            (candidatos_otc if eh_otc else candidatos_regulares).append(candidato)
            
        historico_otc = {}
        if candidatos_otc:
            try:
                historico = obter_historico_otc_simulado()
                historico_otc = self._historico_otc_por_sessao(
                    historico,
                    [sessao["id"] for sessao in sessoes_ativas],
                    momento_utc.hour,
                )
            except Exception:
                self.logger.exception(
                    "Falha consultando histórico simulado OTC; "
                    "seleção seguirá sem esse peso."
                )

        candidatos_regulares, detalhes_regulares = self._ordenar_por_prioridade(
            candidatos_regulares, moedas_prioritarias, {}
        )
        candidatos_otc, detalhes_otc = self._ordenar_por_prioridade(
            candidatos_otc, moedas_prioritarias, historico_otc
        )
        if periodo_otc:
            candidatos_elegiveis = candidatos_otc
            categoria = "OTC"
        else:
            candidatos_elegiveis = candidatos_regulares
            categoria = "regulares"
        candidatos_unicos = list(
            {
                candidato["ativo"]: candidato
                for candidato in candidatos_elegiveis
            }.values()
        )
        selecionados = random.sample(
            candidatos_unicos,
            len(candidatos_unicos) if self.limit is None
            else min(self.limit, len(candidatos_unicos)),
        )

        if selecionados:
            detalhes = {**detalhes_regulares, **detalhes_otc}
            resumo = ", ".join(
                f"{item['ativo']} ({item['payout']:.1f}%, "
                f"score={detalhes[item['ativo']]['score']:.1f})"
                for item in selecionados
            )
            self.logger.info(
                f"Sorteio de {len(selecionados)} pares {categoria}: {resumo}"
            )
        else:
            regulares = [
                nome for nome in nomes_catalogo
                if is_currency_pair(nome) and not nome.endswith("-OTC")
            ]
            otc_exemplos = [
                nome for nome in nomes_catalogo
                if is_currency_pair(nome) and nome.endswith("-OTC")
            ]
            exemplo_regulares = ", ".join(regulares[:5]) or "nenhum no mapa binary"
            exemplo_otc = ", ".join(otc_exemplos[:5]) or "nenhum no mapa binary"
            self.logger.warning(
                f"Nenhum par selecionável para {categoria}. "
                f"Catálogo: {contagem}; exemplos regulares: {exemplo_regulares}; "
                f"exemplos OTC: {exemplo_otc}."
            )
        return selecionados
