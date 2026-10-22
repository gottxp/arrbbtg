"""Ferramenta de conferencia: mostra no terminal os precos que o scanner esta lendo.

Como usar (no terminal, na pasta do projeto):

    python -m scanner.ferramentas.imprimir_precos bitget
    python -m scanner.ferramentas.imprimir_precos mexc gate bitget bingx xt hyperliquid
    python -m scanner.ferramentas.imprimir_precos kraken krakenfutures lighter lighter_rh

Ela conecta na(s) corretora(s) e, a cada 5 segundos, escreve o bid (melhor preco de
compra) e o ask (melhor preco de venda) de BTC, ETH e SOL, no spot e no futuro.
Cada corretora mostra os pares que ela tem, com o mesmo simbolo que o scanner usa (ex.: na
Hyperliquid, "BTC/USDC:USDC", em USDC), e so os mercados ligados em mercados_por_corretora
do config.yaml (a Hyperliquid, so o futuro). A Lighter (lighter e lighter_rh) usa o conector proprio
do scanner, e nao o ccxt; os enderecos dela vem do config.yaml. Compare com o site da corretora: os
precos tem que bater. Com varias corretoras, uma que cair aparece como "parou" e as outras continuam.
Ctrl+C para parar. --config escolhe outro config.yaml (padrao: o da pasta, se existir).
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from decimal import Decimal

from scanner.coletores.base import Coletor
from scanner.coletores.ccxt_coletor import ColetorCcxt, mercado_do_simbolo
from scanner.coletores.fabrica import criar_corretora
from scanner.coletores.lighter import CORRETORAS_LIGHTER, ColetorLighter, e_lighter
from scanner.config.carregar import MERCADOS, Config, ErroConfig, carregar_config
from scanner.modelos import Livro

# Ativos mostrados e intervalo padrao entre uma impressao e outra (muda com --intervalo).
ATIVOS = ("BTC", "ETH", "SOL")
INTERVALO_PADRAO_S = 5


def simbolos_da_corretora(coletor: ColetorCcxt) -> list[str]:
    """Os simbolos de BTC, ETH e SOL (spot e futuro) que o scanner usa nesta corretora, os spot primeiro.

    Sao os mesmos que vao para o estado do scanner: a cotacao preferida (USDT, depois USDC...) e so os
    mercados do coletor (mercados_por_corretora). Os markets precisam estar carregados (load_markets).
    """
    chaves = coletor.simbolos_por_chave()
    return [chaves[(mercado, ativo)] for mercado in MERCADOS for ativo in ATIVOS if (mercado, ativo) in chaves]


async def preparar(exchanges: dict, cfg: Config) -> tuple[list[ColetorCcxt], dict[str, list[str]], list[str]]:
    """Carrega a lista de mercados de cada corretora e escolhe os pares dela (veja simbolos_da_corretora).

    exchanges: {nome: corretora do ccxt}. Devolve (coletores, {nome: simbolos}, erros). A corretora que nao
    carrega a lista ou nao tem nenhum dos pares vira uma linha em `erros`, e fechada e fica de fora.
    """
    nomes = list(exchanges)
    resultados = await asyncio.gather(*(exchanges[n].load_markets() for n in nomes), return_exceptions=True)
    coletores, simbolos, erros = [], {}, []
    for nome, resultado in zip(nomes, resultados):
        if isinstance(resultado, BaseException):
            erros.append(f"{nome}: nao consegui carregar a lista de mercados ({type(resultado).__name__}: {resultado})")
            await exchanges[nome].close()
            continue
        if e_lighter(nome):  # Task 16: conector proprio da Lighter (so futuros)
            coletor = ColetorLighter(CORRETORAS_LIGHTER[nome], exchanges[nome],
                                     intervalo_ping_s=cfg.intervalo_ping_lighter_s)
        else:
            coletor = ColetorCcxt(nome, exchanges[nome], mercados=cfg.mercados_da_corretora(nome))
        pares = simbolos_da_corretora(coletor)
        if not pares:
            mercados = ", ".join(cfg.mercados_da_corretora(nome))
            erros.append(f"{nome}: nenhum par de {', '.join(ATIVOS)} nos mercados {mercados}")
            await exchanges[nome].close()
            continue
        coletores.append(coletor)
        simbolos[nome] = pares
    return coletores, simbolos, erros


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
    simbolos: list[str] | dict[str, list[str]],
    intervalo_s: float,
    escrever: Callable[[str], None] = print,
) -> None:
    """Liga os coletores e, a cada `intervalo_s`, escreve o ultimo preco de cada simbolo de cada corretora.

    simbolos: a mesma lista para todas, ou {corretora: [simbolos]} (cada uma com os seus pares).
    Se um coletor cair, ele aparece como "parou" e os outros continuam. Termina quando
    todos terminarem (se algum caiu com erro, o ultimo erro sobe para quem chamou) ou
    quando for cancelada.
    """
    ultimos: dict[tuple[str, str], Livro] = {}  # (corretora, simbolo) -> livro mais recente

    def pares_de(nome: str) -> list[str]:
        return simbolos.get(nome, []) if isinstance(simbolos, dict) else simbolos

    def guardador(nome: str) -> Callable[[Livro], None]:
        def guardar(livro: Livro) -> None:
            ultimos[(nome, livro.simbolo_original)] = livro
        return guardar

    coletas = {asyncio.create_task(c.iniciar(pares_de(c.nome), guardador(c.nome))): c for c in coletores}
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
                    for simbolo in pares_de(coletor.nome):
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
    parser.add_argument("corretoras", nargs="+", metavar="corretora",
                        help="nome(s) da(s) corretora(s), ex.: mexc gate bitget bingx xt hyperliquid kraken "
                             "krakenfutures lighter lighter_rh")
    parser.add_argument(
        "--intervalo",
        type=float,
        default=INTERVALO_PADRAO_S,
        help=f"segundos entre uma impressao e outra (padrao: {INTERVALO_PADRAO_S})",
    )
    parser.add_argument("--config", default=None,
                        help="config.yaml de onde vem mercados_por_corretora (padrao: ./config.yaml, se existir)")
    args = parser.parse_args(argv)
    if args.intervalo <= 0:
        parser.error("--intervalo precisa ser maior que zero")

    try:
        cfg = _ler_config(args.config)
    except ErroConfig as erro:
        print(f"Erro na configuracao: {erro}", file=sys.stderr)
        return 1

    try:
        nomes = list(dict.fromkeys(args.corretoras))  # tira nomes repetidos, mantendo a ordem
        exchanges = {nome: criar_corretora(nome, cfg) for nome in nomes}  # a Lighter pelo conector proprio
        asyncio.run(_conferir(exchanges, cfg, args.intervalo))
    except KeyboardInterrupt:
        print("\nEncerrado.")
    except Exception as erro:  # mostra o motivo em uma linha, sem o textao do traceback
        print(f"Erro: {type(erro).__name__}: {erro}", file=sys.stderr)
        return 1
    return 0


def _ler_config(caminho: str | None) -> Config:
    """O config.yaml pedido; sem pedir, o ./config.yaml se existir, senao os padroes."""
    if caminho is None:
        caminho = "config.yaml"
        if not os.path.exists(caminho):
            return Config()
    return carregar_config(caminho)


async def _conferir(exchanges: dict, cfg: Config, intervalo_s: float) -> None:
    """Escolhe os pares de cada corretora e imprime os precos ate Ctrl+C (veja preparar e imprimir_precos)."""
    coletores, simbolos, erros = await preparar(exchanges, cfg)
    for erro in erros:
        print(f"Erro: {erro}", file=sys.stderr)
    if not coletores:
        raise RuntimeError("nenhuma corretora pronta para mostrar precos")
    await imprimir_precos(coletores, simbolos, intervalo_s)


if __name__ == "__main__":
    sys.exit(main())
