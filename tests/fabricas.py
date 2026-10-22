"""Helpers de teste compartilhados (as proximas tarefas acrescentam os seus aqui)."""

from dataclasses import replace

from scanner.config.carregar import Config
from scanner.metricas.janela import Metricas
from scanner.modelos import Candidato, Livro, Ponta


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


# ---------------------------------------------------------------------------
# Config, Ponta, Metricas e Candidato de teste (Task 7)
# ---------------------------------------------------------------------------

# Config com todos os padroes (tamanho de referencia $100, RP minimo 0,2%, custo maximo Farm -0,1%...).
cfg = Config()


def cfg_com(**campos) -> Config:
    """Copia da `cfg` padrao com os `campos` trocados. Ex.: cfg_com(corretoras_farm=["lighter"])."""
    return replace(cfg, **campos)


def metricas(**campos) -> Metricas:
    """Metricas do periodo de teste (valores saudaveis); `campos` troca os que o teste quiser."""
    base = dict(
        abertura_media=0.50,
        fechamento_medio=-0.10,
        maior_abertura=0.60,
        maior_fechamento=-0.05,
        media_10_maiores_abertura=0.55,
        media_10_maiores_fechamento=-0.07,
        invertidas=0,
    )
    base.update(campos)
    return Metricas(**base)


def ponta(corretora: str, mercado: str = "futuro", ativo: str = "BTC", **campos) -> Ponta:
    """Ponta de teste saudavel: lotes de $1000, volume grande, funding conhecido (so no futuro).

    `campos` troca qualquer campo da Ponta. Na ponta spot os campos de funding ficam None.
    """
    futuro = mercado == "futuro"
    base = dict(
        corretora=corretora,
        mercado=mercado,
        simbolo_original=f"{ativo}USDT",
        preco=100.0,
        lote_usd=1000.0,
        lote_medio_usd=1000.0,
        volume_24h_usd=1_000_000.0,
        funding_pct=0.01 if futuro else None,
        funding_intervalo_h=8 if futuro else None,
        limite_ordem_usd=1_000_000.0,
        variacao_24h_pct=1.0,
        taxa_taker_pct=0.06,
    )
    base.update(campos)
    return Ponta(**base)


def candidato(
    rp_liquido: float = 0.40,
    par: str = "BTC/USDT",
    short: str = "gate",
    lote_long: float = 1000,
    suspeito: bool = False,
    long: str = "bitget",
    lote_short: float = 1000,
    mercado_long: str = "futuro",
    mercado_short: str = "futuro",
    campos_long: dict | None = None,
    campos_short: dict | None = None,
    **outros,
) -> Candidato:
    """Candidato de teste valido (futuro x futuro, bitget x `short`), com valores saudaveis.

    `campos_long` / `campos_short` trocam campos de uma ponta (ex.: campos_short={"volume_24h_usd": 10});
    `outros` troca campos do proprio Candidato (ex.: spread_abertura_pct=2.0, metricas_periodo=None).
    O RP bruto padrao e o liquido mais as taxas (0,24%), como na formula.
    """
    ativo = par.split("/")[0]
    ponta_long = ponta(long, mercado_long, ativo, **{"lote_usd": lote_long, **(campos_long or {})})
    ponta_short = ponta(short, mercado_short, ativo, **{"lote_usd": lote_short, **(campos_short or {})})
    campos = dict(
        par=par,
        tipo="spot_futuro" if mercado_long == "spot" else "futuro_futuro",
        modo="lucro",
        etiquetas=[],
        suspeito=suspeito,
        aviso_cotacao=False,
        ponta_long=ponta_long,
        ponta_short=ponta_short,
        spread_abertura_pct=0.50,
        spread_fechamento_pct=-0.10,
        metricas_periodo=metricas(),
        fr_yield_8h_pct=0.01,
        rp_bruto_pct=rp_liquido + 0.24,
        custo_taxas_pct=0.24,
        funding_esperado_pct=0.0,
        rp_liquido_pct=rp_liquido,
        custo_por_1000_usd=None,
        tamanho_referencia_usd=100,
    )
    campos.update(outros)
    return Candidato(**campos)
