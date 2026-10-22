"""Helpers de teste compartilhados (as proximas tarefas acrescentam os seus aqui)."""

from dataclasses import replace

from scanner.coletores.dados_lentos import TaxaDesconhecida
from scanner.config.carregar import Config
from scanner.estado.mercados import EstadoMercados
from scanner.metricas.janela import Metricas
from scanner.modelos import Candidato, Livro, Ponta
from scanner.motor import Motor, chave_de


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


# ---------------------------------------------------------------------------
# Motor de teste (Task 10): estado preenchido a mao e dados lentos falsos
# ---------------------------------------------------------------------------

# Distancia entre o melhor bid e o melhor ask nos livros de teste: 0,1% do preco da ponta.
SPREAD_DO_LIVRO = 0.001
# Quanto vale (em $) o melhor nivel de cada lado dos livros de teste: bem acima da referencia de $100.
LOTE_PADRAO_USD = 1000.0


class DadosLentosFalso:
    """Imita o DadosLentos sem rede: `taxas` {(corretora, mercado): %} e `fundings` {(corretora, ativo): (%, h)}.

    Como o de verdade, `taxa_taker` levanta TaxaDesconhecida quando nao ha taxa.
    """

    def __init__(self, taxas: dict | None = None, fundings: dict | None = None):
        self.taxas = dict(taxas or {})
        self.fundings = dict(fundings or {})

    def funding(self, corretora: str, ativo: str):
        return self.fundings.get((corretora, ativo))

    def taxa_taker(self, corretora: str, mercado: str) -> float:
        if (corretora, mercado) in self.taxas:
            return self.taxas[(corretora, mercado)]
        raise TaxaDesconhecida(f"sem taxa taker para {corretora}/{mercado}")


def colocar_ponta(
    estado: EstadoMercados,
    corretora: str,
    mercado: str,
    ativo: str,
    preco: float,
    lado: str,
    agora: float,
    cotacao: str = "USDT",
    lote_usd: float = LOTE_PADRAO_USD,
    volume_24h_usd: float = 1_000_000.0,
    variacao_24h_pct: float = 1.0,
) -> None:
    """Poe no estado o livro e o ticker de uma ponta, e registra a mensagem da corretora em `agora`.

    `preco` e o preco que a ponta executa: o melhor ask se `lado` = "long" (compra), o melhor bid
    se "short" (vende). O outro lado do livro fica SPREAD_DO_LIVRO pior. Um nivel so por lado,
    valendo `lote_usd` (padrao LOTE_PADRAO_USD).
    Simbolo no padrao do ccxt: "BTC/USDT" no spot e "BTC/USDT:USDT" no futuro.
    """
    quantidade = lote_usd / preco
    if lado == "long":
        asks, bids = [(preco, quantidade)], [(preco * (1 - SPREAD_DO_LIVRO), quantidade)]
    else:
        bids, asks = [(preco, quantidade)], [(preco * (1 + SPREAD_DO_LIVRO), quantidade)]
    simbolo = f"{ativo}/{cotacao}" if mercado == "spot" else f"{ativo}/{cotacao}:{cotacao}"
    estado.atualizar_livro(livro(corretora, mercado, simbolo, agora, bids=bids, asks=asks), ativo=ativo)
    estado.atualizar_ticker(corretora, mercado, ativo, bids[0][0], asks[0][0], agora, volume_24h_usd,
                            variacao_24h_pct, simbolo_original=simbolo, cotacao=cotacao)
    estado.registrar_mensagem(corretora, agora)


def motor_falso(
    spot=None,
    futuro=None,
    long_futuro=None,
    short_futuro=None,
    taxas=0.06,
    f_medio=None,
    funding_short=None,
    funding_long=None,
    silencio_futuro_s=0,
    *,
    agora: float = 10.0,
    config: Config | None = None,
    banco=None,
    cotacoes: dict | None = None,
):
    """Motor com o estado ja preenchido em `agora` (padrao 10s), sem rede.

    Cada ponta e (corretora, ativo, preco): `spot` e `long_futuro` sao pontas long (compram no ask
    = preco); `futuro` e `short_futuro` sao pontas short (vendem no bid = preco). Livros com $1000
    no melhor nivel de cada lado (veja colocar_ponta).
    taxas: taxa taker (%) de todas as pontas; None = nenhuma taxa conhecida (TaxaDesconhecida).
    f_medio: pre-carrega uma amostra com F = f_medio em `agora` na combinacao long x short; o motor
        so amostra de novo `amostra_s` depois, entao o F medio em `agora` e exatamente f_medio.
    funding_short / funding_long: (taxa %, intervalo h) do futuro da ponta short / long.
    silencio_futuro_s: a(s) corretora(s) do futuro mandaram a ultima mensagem ha tantos segundos.
    config: Config usada (padrao: `cfg`); banco e cotacoes vao direto para o Motor.
    """
    config = cfg if config is None else config
    estado = EstadoMercados(config.max_silencio_conexao_s, config.max_idade_livro_s)
    dados = DadosLentosFalso()
    pernas = []  # (corretora, mercado, ativo, preco, lado)
    for ponta_dada, mercado, lado in ((spot, "spot", "long"), (futuro, "futuro", "short"),
                                      (long_futuro, "futuro", "long"), (short_futuro, "futuro", "short")):
        if ponta_dada is not None:
            corretora, ativo, preco = ponta_dada
            pernas.append((corretora, mercado, ativo, preco, lado))

    for corretora, mercado, ativo, preco, lado in pernas:
        momento = agora - silencio_futuro_s if mercado == "futuro" else agora
        colocar_ponta(estado, corretora, mercado, ativo, preco, lado, momento)
        if taxas is not None:
            dados.taxas[(corretora, mercado)] = taxas
        funding = funding_long if lado == "long" else funding_short
        if mercado == "futuro" and funding is not None:
            dados.fundings[(corretora, ativo)] = funding

    motor = Motor(config, estado, dados, banco=banco, cotacoes=lambda: dict(cotacoes or {}))
    if f_medio is not None:
        longs = [p for p in pernas if p[4] == "long"]
        shorts = [p for p in pernas if p[4] == "short"]
        (c_long, m_long, ativo, p_long, _), (c_short, m_short, _, p_short, _) = longs[0], shorts[0]
        a = (p_short - p_long) / p_long * 100  # A dos precos de execucao das pontas
        chave = chave_de((ativo, c_long, m_long, c_short, m_short))
        motor.precarregar_amostra(chave, agora, a, f_medio, LOTE_PADRAO_USD, LOTE_PADRAO_USD)
    return motor
