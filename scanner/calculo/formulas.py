"""Formulas de spread, custos, funding e RP. Funcoes puras, tudo em % (0,40 = 0,40%).

Nenhuma funcao acessa a rede ou o disco. O motor (Task 10) usa estas funcoes para
calcular o RP de cada candidato de arbitragem.
"""

import math

# O yield de funding e sempre normalizado para 8 horas (por isso o nome fr_yield_8h).
HORAS_YIELD = 8


def _checar_positivo(valor: float, nome: str) -> None:
    """Levanta ValueError se o denominador nao for positivo (o chamador passa so livros validos)."""
    if valor <= 0:
        raise ValueError(f"{nome} precisa ser maior que 0, veio {valor}")


def _checar_intervalo(intervalo_h: float | None) -> None:
    """Intervalo de funding precisa ser maior que 0 quando informado. None = sem intervalo (spot)."""
    if intervalo_h is not None and intervalo_h <= 0:
        raise ValueError(f"intervalo de funding precisa ser maior que 0, veio {intervalo_h}")


def preco_execucao(niveis: list[tuple[float, float]], tamanho_usd: float) -> float | None:
    """Preco medio de execucao comendo o livro ate `tamanho_usd` (em USD).

    `niveis` sao (preco, quantidade) por 1 unidade do ativo, na ordem em que devem ser
    consumidos: asks crescentes para compra, bids decrescentes para venda. O ultimo nivel
    entra so com a fracao que falta. Resultado = tamanho_usd / unidades totais.

    Devolve None se o livro nao cobre tamanho_usd, se tamanho_usd nao e positivo, ou se
    algum nivel consumido tem preco ou quantidade nao positivos.
    """
    if tamanho_usd <= 0:
        return None
    faltam = tamanho_usd  # USD ainda a preencher; sempre > 0 dentro do laco
    unidades = 0.0  # unidades do ativo ja compradas (ou vendidas)
    for preco, quantidade in niveis:
        if preco <= 0 or quantidade <= 0:
            return None
        nocional = preco * quantidade  # USD que o nivel inteiro cobre
        if nocional >= faltam:
            # Este nivel fecha o tamanho: entra so com a fracao que falta.
            unidades += faltam / preco
            return tamanho_usd / unidades
        unidades += quantidade
        faltam -= nocional
    return None  # o livro acabou antes de cobrir tamanho_usd


def spread_abertura(ask_long: float, bid_short: float) -> float:
    """Spread de abertura em %: quanto o short vende acima do que o long paga.

    (bid_short - ask_long) / ask_long x 100. Levanta ValueError se ask_long nao for positivo.
    """
    _checar_positivo(ask_long, "ask_long")
    return (bid_short - ask_long) / ask_long * 100


def spread_fechamento(bid_long: float, ask_short: float) -> float:
    """Spread de fechamento em %: quanto o long vende acima do que o short recompra.

    (bid_long - ask_short) / bid_long x 100. Levanta ValueError se bid_long nao for positivo.
    """
    _checar_positivo(bid_long, "bid_long")
    return (bid_long - ask_short) / bid_long * 100


def custo_taxas(taxa_long: float, taxa_short: float) -> float:
    """Custo total de taxas em %: entrada e saida nas duas pontas (por isso o 2 x)."""
    return 2 * (taxa_long + taxa_short)


def _funding_de_uma_ponta(fr: float | None, intervalo_h: float | None, horas: float) -> float:
    """Funding de uma ponta: fr x numero de pagamentos completos (floor) no periodo.

    Ponta spot (fr ou intervalo None) nao paga nem recebe funding: conta 0.
    """
    if fr is None or intervalo_h is None:
        return 0.0
    return fr * math.floor(horas / intervalo_h)


def funding_esperado(
    fr_short: float | None,
    int_short_h: float | None,
    fr_long: float | None,
    int_long_h: float | None,
    horas: float,
) -> float:
    """Funding liquido esperado em % no periodo `horas`: recebido na ponta short menos pago na long.

    Cada ponta usa floor(horas / intervalo) pagamentos completos. Ponta spot conta 0.
    """
    _checar_intervalo(int_short_h)
    _checar_intervalo(int_long_h)
    funding_short = _funding_de_uma_ponta(fr_short, int_short_h, horas)
    funding_long = _funding_de_uma_ponta(fr_long, int_long_h, horas)
    return funding_short - funding_long


def _yield_de_uma_ponta(fr: float | None, intervalo_h: float | None) -> float:
    """Yield de 8 horas de uma ponta: fr x 8 / intervalo. Ponta spot (None) conta 0."""
    if fr is None or intervalo_h is None:
        return 0.0
    return fr * HORAS_YIELD / intervalo_h


def fr_yield_8h(
    fr_short: float | None,
    int_short_h: float | None,
    fr_long: float | None,
    int_long_h: float | None,
) -> float:
    """Yield liquido de funding normalizado para 8 horas, em %: ponta short menos ponta long."""
    _checar_intervalo(int_short_h)
    _checar_intervalo(int_long_h)
    return _yield_de_uma_ponta(fr_short, int_short_h) - _yield_de_uma_ponta(fr_long, int_long_h)


def rp_bruto(a_atual: float, f_metrica: float) -> float:
    """RP bruto em %: spread de abertura atual mais o spread de fechamento da metrica (F medio por padrao)."""
    return a_atual + f_metrica


def rp_liquido(rp_bruto: float, custo_taxas: float, margem_slippage: float, funding: float) -> float:
    """RP liquido em %: RP bruto menos taxas e margem de slippage, mais o funding esperado."""
    return rp_bruto - custo_taxas - margem_slippage + funding
