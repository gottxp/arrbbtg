"""Coletor que usa o ccxt (versao "pro", com WebSocket) para ler os livros de ofertas."""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Literal

import ccxt.pro as ccxtpro

from scanner.coletores.base import Coletor
from scanner.modelos import Livro

registro = logging.getLogger("scanner.coletores")


def mercado_do_simbolo(simbolo: str) -> Literal["spot", "futuro"]:
    """No ccxt, o spot e "BTC/USDT" e o futuro perpetuo e "BTC/USDT:USDT" (com o ":")."""
    return "futuro" if ":" in simbolo else "spot"


def livro_de_ccxt(
    corretora: str,
    mercado: Literal["spot", "futuro"],
    simbolo: str,
    ob: dict,
    agora: float,
) -> Livro | None:
    """Transforma o livro entregue pelo ccxt em um Livro.

    Devolve None (livro descartado) se ele for vazio, tiver preco zero ou
    negativo, ou se o melhor bid for maior ou igual ao melhor ask.
    """
    # Copia nivel por nivel: o ccxt reaproveita e altera o mesmo livro a cada mensagem.
    bids = [(float(nivel[0]), float(nivel[1])) for nivel in ob.get("bids") or []]
    asks = [(float(nivel[0]), float(nivel[1])) for nivel in ob.get("asks") or []]

    if not bids or not asks:
        return None
    # "not (preco > 0)" tambem pega preco invalido (NaN), que "preco <= 0" deixaria passar.
    if any(not (preco > 0) for preco, _ in bids + asks):
        return None
    if bids[0][0] >= asks[0][0]:  # livro cruzado: melhor compra paga mais que a melhor venda
        return None

    timestamp_ms = ob.get("timestamp")
    return Livro(
        corretora=corretora,
        mercado=mercado,
        simbolo_original=simbolo,
        bids=bids,
        asks=asks,
        recebido_em=agora,
        ts_corretora=None if timestamp_ms is None else timestamp_ms / 1000,  # o ccxt entrega em ms
    )


def criar_exchange_ccxt(nome: str):
    """Monta o objeto da corretora no ccxt pro (ex.: "bitget"). Nao acessa a rede."""
    if nome not in ccxtpro.exchanges:
        raise ValueError(f"corretora '{nome}' nao existe no ccxt (nome com erro de digitacao?)")
    return getattr(ccxtpro, nome)({"enableRateLimit": True})


class ColetorCcxt(Coletor):
    """Acompanha os livros de uma corretora pelo `watch_order_book` do ccxt.

    `exchange` e o objeto da corretora do ccxt (veja criar_exchange_ccxt). Ele e
    passado de fora para os testes poderem usar um objeto falso, sem internet.
    """

    def __init__(self, nome: str, exchange):
        super().__init__(nome)
        self._exchange = exchange

    async def iniciar(self, simbolos: list[str], ao_atualizar: Callable[[Livro], None]) -> None:
        """Acompanha todos os simbolos ao mesmo tempo, cada um na sua tarefa.

        Se um simbolo der erro, os outros sao cancelados e o erro sobe para quem
        chamou. Ao terminar (erro ou cancelamento), a corretora e sempre fechada.
        """
        tarefas = [asyncio.create_task(self._acompanhar_livro(s, ao_atualizar)) for s in simbolos]
        try:
            await asyncio.gather(*tarefas)
        finally:
            for tarefa in tarefas:
                tarefa.cancel()
            await asyncio.gather(*tarefas, return_exceptions=True)
            await self._exchange.close()

    async def _acompanhar_livro(self, simbolo: str, ao_atualizar: Callable[[Livro], None]) -> None:
        """Repete para sempre: espera o livro mudar e entrega o livro novo, se for valido."""
        mercado = mercado_do_simbolo(simbolo)
        while True:
            ob = await self._exchange.watch_order_book(simbolo)
            livro = livro_de_ccxt(self.nome, mercado, simbolo, ob, time.time())
            if livro is None:
                registro.debug("%s %s: livro descartado (vazio, preco invalido ou cruzado)", self.nome, simbolo)
                continue
            ao_atualizar(livro)
