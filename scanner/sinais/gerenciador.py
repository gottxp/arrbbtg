"""Gerenciador de sinais (especificacao, secoes 4 e 5).

O motor entrega, a cada ciclo, a lista de candidatos que passaram nos filtros. O gerenciador
compara essa lista com os sinais que ja estavam ativos e diz o que mudou:

- "novo": a chave nao estava ativa;
- "atualizado": a chave continua ativa (os valores sao renovados);
- "encerrado": a chave estava ativa e sumiu da lista.

Cada `Evento` traz o sinal ja no formato JSON da spec (secao 5) e se ele deve ser gravado
no historico (Task 9) ou so mostrado na tabela ao vivo (Task 11).

Anti-repeticao: se o mesmo sinal some e volta em menos de `anti_repeticao_s` segundos
(contados da ultima vez em que foi visto), nao e uma oportunidade nova: o evento "novo"
reaproveita o `id` e o `detectado_em` do registro anterior e sai com
`registrar_historico=False`. Assim o historico tem um unico registro, que o "encerrado"
seguinte fecha.

Tudo em memoria e sem rede. O horario vem de fora (`agora`, em segundos desde 1970),
o que deixa o gerenciador facil de testar.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from scanner.modelos import Candidato, Ponta


@dataclass
class Evento:
    """Uma mudanca num sinal, para quem grava o historico e para quem transmite ao painel."""

    tipo: Literal["novo", "atualizado", "encerrado"]
    sinal: dict  # o sinal no formato JSON da spec (secao 5)
    registrar_historico: bool  # True: grava/fecha o registro do historico; False: so mostra ao vivo


@dataclass
class _Ativo:
    """O que o gerenciador guarda de cada sinal ativo (ou recem-encerrado, para a anti-repeticao)."""

    candidato: Candidato  # os ultimos valores vistos
    id: str
    detectado_em: float  # segundos desde 1970
    atualizado_em: float  # segundos desde 1970; e tambem "a ultima vez que foi visto"


# ---------------------------------------------------------------------------
# Datas e numeros no formato do JSON
# ---------------------------------------------------------------------------


def _em_milissegundos(segundos: float) -> int:
    """Segundos desde 1970 -> milissegundos inteiros, arredondados (nao cortados).

    Arredondar evita perder 1 ms por erro de float (ex.: 0.345 pode virar 0.34499...).
    """
    return round(segundos * 1000)


def _iso_utc(segundos: float) -> str:
    """Segundos desde 1970 -> "2026-10-20T15:04:12.345Z" (ISO 8601, UTC, com milissegundos)."""
    seg, ms = divmod(_em_milissegundos(segundos), 1000)
    momento = datetime.fromtimestamp(seg, tz=timezone.utc)
    return momento.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def _compacto_utc(segundos: float) -> str:
    """Segundos desde 1970 -> "20261020T150412Z" (usado no id). Mesmo segundo do `_iso_utc`."""
    seg = _em_milissegundos(segundos) // 1000
    return datetime.fromtimestamp(seg, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _numero(valor):
    """Prepara um numero para o JSON: NaN e infinito viram None, e -0.0 vira 0.0.

    None, texto e bool passam como estao. Inteiros tambem (o JSON da spec tem `invertidas` e
    `funding_intervalo_h` inteiros).
    """
    if isinstance(valor, float):
        if not math.isfinite(valor):
            return None
        return valor + 0.0  # -0.0 + 0.0 = 0.0 (qualquer outro valor nao muda)
    return valor


def _ponta_json(ponta: Ponta) -> dict:
    """Uma ponta no formato da spec: as mesmas chaves da `Ponta`."""
    return {
        "corretora": ponta.corretora,
        "mercado": ponta.mercado,
        "simbolo_original": ponta.simbolo_original,
        "preco": _numero(ponta.preco),
        "lote_usd": _numero(ponta.lote_usd),
        "lote_medio_usd": _numero(ponta.lote_medio_usd),
        "volume_24h_usd": _numero(ponta.volume_24h_usd),
        "funding_pct": _numero(ponta.funding_pct),
        "funding_intervalo_h": _numero(ponta.funding_intervalo_h),
        "limite_ordem_usd": _numero(ponta.limite_ordem_usd),
        "variacao_24h_pct": _numero(ponta.variacao_24h_pct),
        "taxa_taker_pct": _numero(ponta.taxa_taker_pct),
    }


def _metricas_json(candidato: Candidato, periodo_h: float) -> dict | None:
    """As metricas do periodo no formato da spec; None enquanto o candidato nao tem amostras."""
    m = candidato.metricas_periodo
    if m is None:
        return None
    return {
        "periodo_h": _numero(periodo_h),
        "abertura_media_pct": _numero(m.abertura_media),
        "fechamento_medio_pct": _numero(m.fechamento_medio),
        "maior_abertura_pct": _numero(m.maior_abertura),
        "maior_fechamento_pct": _numero(m.maior_fechamento),
        "invertidas": _numero(m.invertidas),
    }


class GerenciadorSinais:
    """Acompanha os sinais ativos e transforma cada ciclo do motor em eventos."""

    def __init__(self, anti_repeticao_s: float, periodo_h: float = 4):
        self.anti_repeticao_s = anti_repeticao_s
        self.periodo_h = periodo_h  # vai em metricas_periodo.periodo_h (a `Metricas` nao guarda o periodo)
        # Sinais ativos agora, pela chave. O dict guarda a ordem em que foram detectados.
        self._ativos: dict[tuple, _Ativo] = {}
        # Sinais que sumiram ha pouco, pela chave: so servem para a anti-repeticao.
        self._encerrados: dict[tuple, _Ativo] = {}

    # --- API ---------------------------------------------------------------

    def processar(self, candidatos: list[Candidato], agora: float) -> list[Evento]:
        """Compara os candidatos de agora com os sinais ativos e devolve os eventos.

        Ordem dos eventos (sempre a mesma): primeiro os encerrados, na ordem em que foram
        detectados; depois os novos e atualizados, na ordem de `candidatos`.
        Se a mesma chave aparecer mais de uma vez em `candidatos`, vale a ultima.
        """
        self._esquecer_encerrados_antigos(agora)

        # Uma entrada por chave, na ordem em que apareceram.
        por_chave: dict[tuple, Candidato] = {}
        for candidato in candidatos:
            por_chave[candidato.chave] = candidato

        eventos: list[Evento] = []

        # 1) Quem estava ativo e nao veio agora: encerrado.
        for chave in [c for c in self._ativos if c not in por_chave]:
            ativo = self._ativos.pop(chave)
            self._encerrados[chave] = ativo  # lembra, para a anti-repeticao
            eventos.append(Evento("encerrado", self._sinal(ativo, encerrado_em=agora), registrar_historico=True))

        # 2) Quem veio agora: atualizado (ja estava ativo) ou novo.
        for chave, candidato in por_chave.items():
            ativo = self._ativos.get(chave)
            if ativo is not None:
                ativo.candidato = candidato
                ativo.atualizado_em = agora
                eventos.append(Evento("atualizado", self._sinal(ativo), registrar_historico=False))
                continue

            anterior = self._encerrados.pop(chave, None)
            if anterior is not None and agora - anterior.atualizado_em < self.anti_repeticao_s:
                # Voltou logo: e o mesmo sinal, com o mesmo registro no historico.
                ativo = _Ativo(candidato, anterior.id, anterior.detectado_em, agora)
                registrar = False
            else:
                ativo = _Ativo(candidato, self._novo_id(candidato, agora), agora, agora)
                registrar = True
            self._ativos[chave] = ativo
            eventos.append(Evento("novo", self._sinal(ativo), registrar_historico=registrar))

        return eventos

    def ativos(self) -> list[dict]:
        """Os sinais ativos agora, no formato JSON da spec, na ordem em que foram detectados."""
        return [self._sinal(ativo) for ativo in self._ativos.values()]

    # --- Internos ------------------------------------------------------------

    def _esquecer_encerrados_antigos(self, agora: float) -> None:
        """Tira da memoria quem sumiu ha `anti_repeticao_s` ou mais: esses nunca mais seriam reaproveitados.

        Serve so para a memoria nao crescer sem fim; a regra da anti-repeticao em si fica na
        checagem de `processar`.
        """
        velhos = [c for c, a in self._encerrados.items() if agora - a.atualizado_em >= self.anti_repeticao_s]
        for chave in velhos:
            del self._encerrados[chave]

    @staticmethod
    def _novo_id(candidato: Candidato, detectado_em: float) -> str:
        """Id do sinal: ATIVO-tipo-corretora_long-corretora_short-AAAAMMDDTHHMMSSZ (do `detectado_em`).

        Ex.: AKAM-futuro_futuro-bitget-deepcoin-20261020T150412Z
        """
        return (
            f"{candidato.ativo}-{candidato.tipo}-{candidato.ponta_long.corretora}-"
            f"{candidato.ponta_short.corretora}-{_compacto_utc(detectado_em)}"
        )

    def _sinal(self, ativo: _Ativo, encerrado_em: float | None = None) -> dict:
        """Monta o sinal no formato JSON da spec (secao 5). Cada chamada devolve um dict novo."""
        c = ativo.candidato
        return {
            "id": ativo.id,
            "par": c.par,
            "tipo": c.tipo,
            "modo": c.modo,
            "etiquetas": list(c.etiquetas),
            "suspeito": c.suspeito,
            "aviso_cotacao": c.aviso_cotacao,
            "ponta_long": _ponta_json(c.ponta_long),
            "ponta_short": _ponta_json(c.ponta_short),
            "spread_abertura_pct": _numero(c.spread_abertura_pct),
            "spread_fechamento_pct": _numero(c.spread_fechamento_pct),
            "metricas_periodo": _metricas_json(c, self.periodo_h),
            "fr_yield_8h_pct": _numero(c.fr_yield_8h_pct),
            "rp_bruto_pct": _numero(c.rp_bruto_pct),
            "custo_taxas_pct": _numero(c.custo_taxas_pct),
            "funding_esperado_pct": _numero(c.funding_esperado_pct),
            "rp_liquido_pct": _numero(c.rp_liquido_pct),
            "custo_por_1000_usd": _numero(c.custo_por_1000_usd),
            "tamanho_referencia_usd": _numero(c.tamanho_referencia_usd),
            "detectado_em": _iso_utc(ativo.detectado_em),
            "atualizado_em": _iso_utc(ativo.atualizado_em),
            "encerrado_em": None if encerrado_em is None else _iso_utc(encerrado_em),
        }
