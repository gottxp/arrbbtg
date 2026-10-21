"""Ferramenta de conferencia: mostra no terminal os precos que o scanner esta lendo.

Como usar (no terminal, na pasta do projeto):

    python -m scanner.ferramentas.imprimir_precos bitget
    python -m scanner.ferramentas.imprimir_precos mexc gate bitget

Ela conecta na(s) corretora(s) e, a cada 5 segundos, escreve o bid (melhor preco de
compra) e o ask (melhor preco de venda) de BTC, ETH e SOL, no spot e no futuro.
Compare com o site da corretora: os precos tem que bater. Com varias corretoras, uma
que cair aparece como "parou" e as outras continuam. Ctrl+C para parar.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
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
    coletores: Sequence[Coletor],
    simbolos: list[str],
    intervalo_s: float,
    escrever: Callable[[str], None] = print,
) -> None:
    """Liga os coletores e, a cada `intervalo_s`, escreve o ultimo preco de cada simbolo de cada corretora.

    Se um coletor cair, ele aparece como "parou" e os outros continuam. Termina quando
    todos terminarem (se algum caiu com erro, o ultimo erro sobe para quem chamou) ou
    quando for cancelada.
    """
    ultimos: dict[tuple[str, str], Livro] = {}  # (corretora, simbolo) -> livro mais recente

    def guardador(nome: str) -> Callable[[Livro], None]:
        def guardar(livro: Livro) -> None:
            ultimos[(nome, livro.simbolo_original)] = livro
        return guardar

    coletas = {asyncio.create_task(c.iniciar(simbolos, guardador(c.nome))): c for c in coletores}
    vivas = set(coletas)
    paradas: dict[str, str] = {}  # corretora -> motivo de ter parado
    ultimo_erro: BaseException | None = None
    relogio = asyncio.get_running_loop().time
    proxima_impressao = relogio() + intervalo_s
    try:
        while vivas:
            # Espera ate a hora de imprimir; se um coletor terminar antes, acorda na hora para registrar.
            terminadas, vivas = await asyncio.wait(
                vivas, timeout=max(0.0, proxima_impressao - relogio()), return_when=asyncio.FIRST_COMPLETED
            )
            for coleta in terminadas:
                erro = None if coleta.cancelled() else coleta.exception()
                if erro is not None:
                    ultimo_erro = erro
                paradas[coletas[coleta].nome] = f"{type(erro).__name__}: {erro}" if erro else "terminou sem erro"
            if not vivas:
                break
            if relogio() >= proxima_impressao:
                proxima_impressao = relogio() + intervalo_s  # sem recuperar impressoes perdidas (ex.: PC suspenso)
                escrever(f"--- {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} ---")
                for coletor in coletores:
                    if coletor.nome in paradas:
                        escrever(f"{coletor.nome:<8} parou: {paradas[coletor.nome]}")
                        continue
                    for simbolo in simbolos:
                        livro = ultimos.get((coletor.nome, simbolo))
                        escrever(formatar_linha(livro) if livro else _linha_sem_dados(coletor.nome, simbolo))
        if ultimo_erro is not None:
            raise ultimo_erro  # todos pararam e pelo menos um foi por erro
    finally:
        for coleta in coletas:
            coleta.cancel()  # cada coletor fecha a sua corretora ao ser cancelado
        await asyncio.gather(*coletas, return_exceptions=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scanner.ferramentas.imprimir_precos",
        description="Mostra os precos (bid/ask) de BTC, ETH e SOL, no spot e no futuro, de uma ou mais corretoras.",
    )
    parser.add_argument("corretoras", nargs="+", metavar="corretora", help="nome(s) da(s) corretora(s) no ccxt, ex.: mexc gate bitget")
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
        nomes = list(dict.fromkeys(args.corretoras))  # tira nomes repetidos, mantendo a ordem
        coletores = [ColetorCcxt(nome, criar_exchange_ccxt(nome)) for nome in nomes]
        asyncio.run(imprimir_precos(coletores, simbolos_padrao(), args.intervalo))
    except KeyboardInterrupt:
        print("\nEncerrado.")
    except Exception as erro:  # mostra o motivo em uma linha, sem o textao do traceback
        print(f"Erro: {type(erro).__name__}: {erro}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
