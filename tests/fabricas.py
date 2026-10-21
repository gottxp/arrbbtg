"""Helpers de teste compartilhados (as proximas tarefas acrescentam os seus aqui)."""

from scanner.modelos import Livro


def livro(
    corretora: str,
    mercado: str,
    ativo: str,
    recebido_em: float,
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    ts_corretora: float | None = None,
) -> Livro:
    """Livro de teste. O nome do par original ja e o `ativo` (ex.: "BTC").

    Sem `bids`/`asks`, usa dois niveis por lado: melhor bid 100.0 e melhor ask 100.1.
    """
    if bids is None:
        bids = [(100.0, 10.0), (99.9, 20.0)]
    if asks is None:
        asks = [(100.1, 10.0), (100.2, 20.0)]
    return Livro(
        corretora=corretora,
        mercado=mercado,
        simbolo_original=ativo,
        bids=list(bids),
        asks=list(asks),
        recebido_em=recebido_em,
        ts_corretora=ts_corretora,
    )


def market_ccxt(
    simbolo: str,
    base: str,
    quote: str = "USDT",
    tipo: str | None = None,
    tamanho: float = 1,
    inverso: bool = False,
    taker: float | None = 0.0006,
) -> dict:
    """Um market no formato do ccxt (so os campos que o scanner le), com os flags de tipo do ccxt.

    tipo: "spot", "swap" (perpetuo), "future" (com vencimento) ou "option". Sem ele, vale
    "swap" se o simbolo tem ":" e "spot" se nao. Como no ccxt, `linear` e `inverse` so
    existem (True/False) nos derivativos; no spot ficam None.
    """
    if tipo is None:
        tipo = "swap" if ":" in simbolo else "spot"
    derivativo = tipo != "spot"
    return {
        "symbol": simbolo,
        "base": base,
        "quote": quote,
        "contractSize": tamanho if derivativo else None,
        "taker": taker,
        "spot": tipo == "spot",
        "swap": tipo == "swap",
        "future": tipo == "future",
        "option": tipo == "option",
        "linear": (not inverso) if derivativo else None,
        "inverse": inverso if derivativo else None,
    }
