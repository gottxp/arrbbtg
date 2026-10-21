"""Tipos de dados compartilhados entre os modulos do scanner."""

from dataclasses import dataclass
from typing import Literal


@dataclass
class Livro:
    """Foto do livro de ofertas de um par numa corretora, no momento em que chegou.

    Cada nivel e uma tupla (preco, quantidade). Os bids vem do maior preco
    para o menor, e os asks do menor para o maior: o primeiro de cada lado
    e sempre o melhor preco.
    """

    corretora: str  # ex.: "bitget"
    mercado: Literal["spot", "futuro"]
    simbolo_original: str  # nome do par como o coletor recebeu, ex.: "BTC/USDT:USDT"
    bids: list[tuple[float, float]]  # ofertas de compra
    asks: list[tuple[float, float]]  # ofertas de venda
    recebido_em: float  # quando o scanner recebeu (segundos desde 1970, relogio local)
    ts_corretora: float | None  # horario informado pela corretora (segundos); None se nao informou


@dataclass
class Ticker:
    """Melhor bid e melhor ask de um par, vindos do stream leve de tickers (etapa 1).

    Os valores estao como a corretora entrega (ex.: "1000PEPE" em preco de 1000 unidades).
    """

    corretora: str  # ex.: "mexc"
    mercado: Literal["spot", "futuro"]
    simbolo_original: str  # nome do par como o coletor recebeu, ex.: "BTC/USDT:USDT"
    bid: float  # melhor preco de compra
    ask: float  # melhor preco de venda
    recebido_em: float  # quando o scanner recebeu (segundos desde 1970, relogio local)
    volume_24h_usd: float | None  # volume das ultimas 24h na moeda de cotacao (aprox. USD); None se nao veio
    variacao_24h_pct: float | None  # variacao do preco em 24h, em %; None se nao veio
