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
