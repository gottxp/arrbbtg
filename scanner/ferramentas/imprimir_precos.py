"""Ferramenta de conferencia: mostra no terminal os precos que o scanner esta lendo.

Como usar (no terminal, na pasta do projeto):

    python -m scanner.ferramentas.imprimir_precos bitget

Ela conecta na corretora e, a cada 5 segundos, escreve o bid (melhor preco de
compra) e o ask (melhor preco de venda) de BTC, ETH e SOL, no spot e no futuro.
Compare com o site da corretora: os precos tem que bater. Ctrl+C para parar.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from scanner.coletores.base import Coletor
from scanner.coletores.ccxt_coletor import ColetorCcxt, criar_exchange_ccxt, mercado_do_simbolo
from scanner.modelos import Livro

# Ativos mostrados e intervalo padrao entre uma impressao e outra (muda com --intervalo).
ATIVOS = ("BTC", "ETH", "SOL")
INTERVALO_PADRAO_S = 5


def simbolos_padrao() -> list[str]:
    """Spot ("BTC/USDT") e futuro perpetuo ("BTC/USDT:USDT") de cada ativo."""
    return [f"{ativo}/USDT" for ativo in ATIVOS] + [f"{ativo}/USDT:USDT" for ativo in ATIVOS]


def texto_preco(preco: float) -> str:
    """Preco por extenso: 0.00000123 em vez de 1.23e-06 (que confunde na hora de conferir)."""
    return format(Decimal(repr(preco)), "f")


def formatar_linha(livro: Livro) -> str:
    """Uma linha com o melhor bid e o melhor ask do livro."""
    bid = texto_preco(livro.bids[0][0])
    ask = texto_preco(livro.asks[0][0])
    return f"{livro.corretora:<8} {livro.mercado:<7} {livro.simbolo_original:<16} bid {bid:<14} ask {ask}"


def _linha_sem_dados(corretora: str, simbolo: str) -> str:
    return f"{corretora:<8} {mercado_do_simbolo(simbolo):<7} {simbolo:<16} sem dados ainda"


async def imprimir_precos(
    coletor: Coletor,
    simbolos: list[str],
    intervalo_s: float,
    escrever: Callable[[str], None] = print,
) -> None:
    """Liga o coletor e, a cada `intervalo_s`, escreve o ultimo preco de cada simbolo.

    So termina se o coletor terminar (erro sobe para quem chamou) ou for cancelado.
    """
    ultimos: dict[str, Livro] = {}  # simbolo -> livro mais recente

    def guardar(livro: Livro) -> None:
        ultimos[livro.simbolo_original] = livro

    coleta = asyncio.create_task(coletor.iniciar(simbolos, guardar))
    try:
        while True:
            # Espera o intervalo; se o coletor terminar antes, acorda na hora.
            terminou, _ = await asyncio.wait({coleta}, timeout=intervalo_s)
            if terminou:
                coleta.result()  # se o coletor caiu com erro, o erro sobe aqui
                return
            escrever(f"--- {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} ---")
            for simbolo in simbolos:
                livro = ultimos.get(simbolo)
                escrever(formatar_linha(livro) if livro else _linha_sem_dados(coletor.nome, simbolo))
    finally:
        coleta.cancel()  # o coletor fecha a corretora ao ser cancelado
        await asyncio.gather(coleta, return_exceptions=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scanner.ferramentas.imprimir_precos",
        description="Mostra os precos (bid/ask) de BTC, ETH e SOL, no spot e no futuro, de uma corretora.",
    )
    parser.add_argument("corretora", help="nome da corretora no ccxt, ex.: bitget")
    parser.add_argument(
        "--intervalo",
        type=float,
        default=INTERVALO_PADRAO_S,
        help=f"segundos entre uma impressao e outra (padrao: {INTERVALO_PADRAO_S})",
    )
    args = parser.parse_args(argv)
    if args.intervalo <= 0:
        parser.error("--intervalo precisa ser maior que zero")

    try:
        coletor = ColetorCcxt(args.corretora, criar_exchange_ccxt(args.corretora))
        asyncio.run(imprimir_precos(coletor, simbolos_padrao(), args.intervalo))
    except KeyboardInterrupt:
        print("\nEncerrado.")
    except Exception as erro:  # mostra o motivo em uma linha, sem o textao do traceback
        print(f"Erro: {type(erro).__name__}: {erro}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
