"""Helpers de teste compartilhados (as proximas tarefas acrescentam os seus aqui)."""

import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urljoin

from fastapi.testclient import TestClient

from scanner.armazenamento import Banco
from scanner.coletores.ccxt_coletor import mercado_do_simbolo
from scanner.coletores.dados_lentos import TaxaDesconhecida
from scanner.config.carregar import Config
from scanner.estado.mercados import EstadoMercados
from scanner.metricas.janela import Metricas
from scanner.modelos import Candidato, Livro, Ponta
from scanner.motor import Motor, chave_de
from scanner.sinais import Evento


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
# Markets das corretoras da fase 2 (Task 15)
# ---------------------------------------------------------------------------

# Markets no formato que o ccxt 4.5.89 entrega (saida do parse_market de cada corretora, conferida
# rodando o parse_market do ccxt instalado com os exemplos de resposta das docstrings dele).
# So os campos que o scanner le, mais o `id` e o `baseName` (este so existe na Hyperliquid).

# ccxt/hyperliquid.py, parse_market: o ccxt poe o codigo da moeda em MAIUSCULA (safe_currency faz
# upper()), entao o perpetuo "kPEPE" vira base "KPEPE", simbolo "KPEPE/USDC:USDC"; o nome original
# fica em `baseName`. Todos os perpetuos da Hyperliquid cotam e liquidam em USDC; taker 0,045%.
HL_KPEPE = {"id": "11", "symbol": "KPEPE/USDC:USDC", "base": "KPEPE", "baseName": "kPEPE", "quote": "USDC",
            "settle": "USDC", "type": "swap", "spot": False, "swap": True, "future": False, "option": False,
            "linear": True, "inverse": False, "contractSize": 1.0, "taker": 0.00045, "active": True}
HL_BTC = {**HL_KPEPE, "id": "0", "symbol": "BTC/USDC:USDC", "base": "BTC", "baseName": "BTC"}
# ccxt/hyperliquid.py, fetch_spot_markets: a Hyperliquid tambem tem spot. "UBTC" (BTC embrulhado) vira
# "BTC/USDC" pelo spotCurrencyMapping, e o USDT0 vira "USDT/USDC". baseName = nome original do token.
HL_SPOT_BTC = {"id": "@142", "symbol": "BTC/USDC", "base": "BTC", "baseName": "UBTC", "quote": "USDC",
               "settle": None, "type": "spot", "spot": True, "swap": False, "future": False, "option": False,
               "linear": None, "inverse": None, "contractSize": None, "taker": 0.0007, "active": True}
HL_SPOT_USDT = {**HL_SPOT_BTC, "id": "@166", "symbol": "USDT/USDC", "base": "USDT", "baseName": "USDT0"}

# ccxt/bingx.py, parse_market: id "1000PEPE-USDT" (swap) e "PEPE-USDT" (spot); taker da tabela fixa do
# ccxt (spot 0,1%, swap 0,05%).
BINGX_1000PEPE = {"id": "1000PEPE-USDT", "symbol": "1000PEPE/USDT:USDT", "base": "1000PEPE", "quote": "USDT",
                  "settle": "USDT", "type": "swap", "spot": False, "swap": True, "future": False, "option": False,
                  "linear": True, "inverse": False, "contractSize": 1.0, "taker": 0.0005, "active": True}
BINGX_PEPE_SPOT = {"id": "PEPE-USDT", "symbol": "PEPE/USDT", "base": "PEPE", "quote": "USDT", "settle": None,
                   "type": "spot", "spot": True, "swap": False, "future": False, "option": False,
                   "linear": None, "inverse": None, "contractSize": None, "taker": 0.001, "active": True}
BINGX_BTC = {**BINGX_1000PEPE, "id": "BTC-USDT", "symbol": "BTC/USDT:USDT", "base": "BTC"}
BINGX_BTC_USDC = {**BINGX_BTC, "id": "BTC-USDC", "symbol": "BTC/USDC:USDC", "quote": "USDC", "settle": "USDC"}

# ccxt/xt.py, parse_market: ids em minuscula ("1000pepe_usdt"), base em maiuscula; o taker do futuro vem
# da API (takerFee de cada par) e o do spot nao vem (None).
XT_1000PEPE = {**BINGX_1000PEPE, "id": "1000pepe_usdt", "taker": 0.0006}
XT_PEPE_SPOT = {**BINGX_PEPE_SPOT, "id": "pepe_usdt", "taker": None}


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


# ---------------------------------------------------------------------------
# Esperas sem dormir de verdade (Task 14)
# ---------------------------------------------------------------------------


async def esperar_ate(condicao, timeout: float = 2.0) -> None:
    """Espera (cedendo a vez ao resto do programa) ate a condicao ficar verdadeira; falha se passar do timeout."""
    limite = asyncio.get_running_loop().time() + timeout
    while not condicao():
        if asyncio.get_running_loop().time() > limite:
            raise AssertionError("a condicao nao ficou verdadeira a tempo")
        await asyncio.sleep(0.005)


class EsperaFalsa:
    """Troca o asyncio.sleep das esperas (reconexao, intervalos): anota quantos segundos esperaria e volta na hora.

    `esperas` guarda cada pedido, em ordem. So cede a vez (asyncio.sleep(0)), entao o teste nao dorme.
    bloquear=True: a espera nunca termina (para testar o cancelamento no meio dela).
    parar_depois: depois de tantas esperas, a proxima levanta CancelledError (encerra um laco infinito).
    """

    def __init__(self, bloquear: bool = False, parar_depois: int | None = None):
        self.esperas: list[float] = []
        self._bloquear = bloquear
        self._parar_depois = parar_depois

    async def __call__(self, segundos: float) -> None:
        if self._parar_depois is not None and len(self.esperas) >= self._parar_depois:
            raise asyncio.CancelledError
        self.esperas.append(segundos)
        if self._bloquear:
            await asyncio.Event().wait()
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Corretora do ccxt falsa (Task 15; usada tambem na fase 3)
# ---------------------------------------------------------------------------


class CorretoraFalsa:
    """Imita a corretora do ccxt.pro com filas: cada watch_* devolve o proximo item da fila dele.

    Filas: (metodo, "spot" | "futuro") para watch_tickers / watch_bids_asks, ("watch_ticker", simbolo) e
    ("livro", simbolo). Item que e excecao e levantado. Fila vazia: a chamada espera (como o ccxt esperando
    a proxima mensagem). `pedidos` guarda (metodo, simbolos, params) de cada chamada e `canais`, o canal
    que a opcao do ccxt pedia na hora do pedido ((metodo, simbolos, options[...]["method"])), como a XT le.
    """

    def __init__(self, markets, has=None, funding=None):
        self.markets = {m["symbol"]: m for m in markets} if isinstance(markets, list) else markets
        self.proximos_markets = None
        self.has = {"watchTickers": True, "fetchFundingRates": True} if has is None else has
        self.funding = dict(funding or {})
        self.pedidos = []
        self.canais = []
        self.options = {}
        self.chamadas_funding = 0
        self._filas = {}

    def fila(self, chave) -> asyncio.Queue:
        if chave not in self._filas:
            self._filas[chave] = asyncio.Queue()
        return self._filas[chave]

    def entregar(self, chave, *itens) -> None:
        for item in itens:
            self.fila(chave).put_nowait(item)

    async def _proximo(self, chave):
        item = await self.fila(chave).get()
        if isinstance(item, BaseException):
            raise item
        return item

    def pedidos_de(self, metodo) -> list:
        return [(simbolos, params) for nome, simbolos, params in self.pedidos if nome == metodo]

    async def load_markets(self, reload=False, params=None):
        await asyncio.sleep(0)
        if self.proximos_markets is not None:
            self.markets, self.proximos_markets = self.proximos_markets, None
        return self.markets

    async def watch_tickers(self, symbols=None, params=None):
        self.pedidos.append(("watch_tickers", tuple(symbols), params))
        self.canais.append(("watch_tickers", tuple(symbols), (self.options.get("watchTickers") or {}).get("method")))
        return await self._proximo(("watch_tickers", mercado_do_simbolo(symbols[0])))

    async def watch_bids_asks(self, symbols=None, params=None):
        self.pedidos.append(("watch_bids_asks", tuple(symbols), params))
        return await self._proximo(("watch_bids_asks", mercado_do_simbolo(symbols[0])))

    async def watch_ticker(self, symbol, params=None):
        self.pedidos.append(("watch_ticker", (symbol,), params))
        self.canais.append(("watch_ticker", (symbol,), (self.options.get("watchTicker") or {}).get("method")))
        return await self._proximo(("watch_ticker", symbol))

    async def watch_order_book(self, symbol, limit=None, params=None):
        self.pedidos.append(("watch_order_book", (symbol,), params))
        return await self._proximo(("livro", symbol))

    async def un_watch_order_book(self, symbol, params=None):
        await asyncio.sleep(0)

    async def fetch_tickers(self, symbols=None, params=None):
        await asyncio.sleep(0)
        return {}

    async def fetch_funding_rates(self, symbols=None, params=None):
        self.chamadas_funding += 1
        await asyncio.sleep(0)
        return dict(self.funding)

    async def fetch_funding_rate(self, symbol, params=None):
        await asyncio.sleep(0)
        return self.funding[symbol]

    async def close(self):
        pass


def tk(bid=99.9, ask=100.0, volume=5e6):
    """Ticker no formato do ccxt."""
    return {"bid": bid, "ask": ask, "quoteVolume": volume, "percentage": 1.0, "timestamp": None}


def ob(bid=99.9, ask=100.0, qtd=10.0):
    """Livro no formato do ccxt, um nivel de cada lado."""
    return {"bids": [[bid, qtd]], "asks": [[ask, qtd]], "timestamp": None}


# ---------------------------------------------------------------------------
# Lighter falsa (Task 16): conexao WebSocket e cliente REST sem rede
# ---------------------------------------------------------------------------


class WsLighterFalso:
    """Imita a conexao WebSocket da Lighter (a interface do _ConexaoWs: enviar, receber, fechar), sem rede.

    `enviadas` guarda cada mensagem que o coletor mandou, em ordem. `entregar(*itens)` poe mensagens na fila
    que `receber` devolve; item que e excecao e levantado (ex.: ConnectionResetError = a conexao caiu). Fila
    vazia: `receber` espera, como a conexao de verdade esperando a proxima mensagem.
    respostas: {canal: [mensagens da 1a assinatura, mensagens da 2a, ...]}: quando o coletor assina o canal
        (ex.: "order_book/0"), as mensagens da vez entram na fila (acabou a lista: repete a ultima).
    """

    def __init__(self, respostas: dict | None = None):
        self.enviadas: list[dict] = []
        self.respostas = {canal: list(vezes) for canal, vezes in (respostas or {}).items()}
        self._assinaturas: dict[str, int] = {}
        self._fila: asyncio.Queue = asyncio.Queue()
        self.fechada = False

    def entregar(self, *itens) -> None:
        for item in itens:
            self._fila.put_nowait(item)

    def vazia(self) -> bool:
        return self._fila.empty()

    async def enviar(self, dados: dict) -> None:
        if self.fechada:
            raise ConnectionResetError("conexao falsa ja fechada")
        self.enviadas.append(dados)
        canal = dados.get("channel")
        if dados.get("type") == "subscribe" and canal in self.respostas and self.respostas[canal]:
            vez = self._assinaturas.get(canal, 0)
            self._assinaturas[canal] = vez + 1
            vezes = self.respostas[canal]
            self.entregar(*vezes[min(vez, len(vezes) - 1)])

    async def receber(self):
        item = await self._fila.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def fechar(self) -> None:
        if not self.fechada:
            self.fechada = True
            self._fila.put_nowait(ConnectionResetError("conexao falsa fechada"))

    def canais(self, tipo: str = "subscribe") -> list[str]:
        """Os canais das mensagens enviadas de um tipo ("subscribe", "unsubscribe"), em ordem."""
        return [d.get("channel") for d in self.enviadas if d.get("type") == tipo]

    def pings(self) -> int:
        return sum(1 for d in self.enviadas if d.get("type") == "ping")


class ClienteLighterFalso:
    """Imita o ClienteLighter (REST + fabrica de conexoes WebSocket) sem rede.

    markets: no formato que o ClienteLighter entrega (veja markets_de_lighter).
    conexoes: o que cada `conectar_ws` devolve, em ordem: um WsLighterFalso ou uma excecao (a conexao nao
        abriu). Acabou a lista: `conectar_ws` espera para sempre (o coletor fica tentando, sem rede).
    """

    def __init__(self, markets: dict, conexoes: list | None = None):
        self.markets = markets
        self.proximos_markets = None
        self.has: dict = {}
        self._conexoes = list(conexoes or [])
        self.conectou = 0
        self.fechado = False
        self.cargas = 0

    async def load_markets(self, reload=False, params=None):
        await asyncio.sleep(0)
        self.cargas += 1
        if self.proximos_markets is not None:
            self.markets, self.proximos_markets = self.proximos_markets, None
        return self.markets

    async def conectar_ws(self):
        self.conectou += 1
        await asyncio.sleep(0)
        if not self._conexoes:
            await asyncio.Event().wait()
        item = self._conexoes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.fechado = True


# ---------------------------------------------------------------------------
# Historico de sinais pronto no banco (Task 17: metricas de validacao)
# ---------------------------------------------------------------------------

# 2026-10-20 00:00:00 UTC, em segundos desde 1970.
INICIO_20_OUT = 1792454400
# Os sinais de `banco_com_sinais` nascem a partir das 10:00 UTC de 2026-10-20, um por minuto.
NASCIMENTO_PADRAO = INICIO_20_OUT + 10 * 3600


def iso_utc(segundos: float) -> str:
    """Segundos desde 1970 -> "2026-10-20T10:00:00.000Z" (o formato das datas dos sinais)."""
    return datetime.fromtimestamp(segundos, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def banco_com_sinais(
    tmp_path,
    duracoes_s: list,
    rp_liquido: list,
    modo: str = "lucro",
    nascimento: float = NASCIMENTO_PADRAO,
    banco: Banco | None = None,
) -> Banco:
    """Banco (arquivo `validacao.db` em `tmp_path`) com um sinal por item de `duracoes_s`, ja gravado como o motor grava.

    O sinal i nasce em `nascimento + 60*i` (segundos desde 1970; padrao 2026-10-20 10:00 UTC), tem RP liquido
    `rp_liquido[i]` e se encerra `duracoes_s[i]` segundos depois. Duracao None = o sinal ainda esta aberto.
    banco: acrescenta os sinais num Banco que ja existe (ex.: os dois modos no mesmo banco).
    """
    if len(duracoes_s) != len(rp_liquido):
        raise ValueError("duracoes_s e rp_liquido precisam ter o mesmo tamanho")
    banco = Banco(str(tmp_path / "validacao.db")) if banco is None else banco
    for i, (duracao, rp) in enumerate(zip(duracoes_s, rp_liquido)):
        nasceu = nascimento + 60 * i
        sinal = {
            "id": f"T17-{modo}-{int(nasceu)}-{i}",
            "par": "BTC/USDT",
            "tipo": "futuro_futuro",
            "modo": modo,
            "rp_liquido_pct": rp,
            "detectado_em": iso_utc(nasceu),
            "encerrado_em": None,
        }
        banco.gravar_evento(Evento("novo", sinal, registrar_historico=True))
        if duracao is not None:
            encerrado = {**sinal, "encerrado_em": iso_utc(nasceu + duracao)}
            banco.gravar_evento(Evento("encerrado", encerrado, registrar_historico=True))
    return banco


# ---------------------------------------------------------------------------
# Painel (API): cliente de teste no endereco da propria maquina e cabecalho da senha
# ---------------------------------------------------------------------------

# O endereco que o navegador usa no PC (e pelo tunel SSH). Sem senha, o painel so aceita os nomes da
# propria maquina no cabecalho Host (o "testserver" padrao do TestClient seria recusado com 400).
BASE_PAINEL = "http://localhost:8000"


class ClienteDoPainel(TestClient):
    """TestClient no endereco BASE_PAINEL, inclusive no WebSocket (o original sempre usa ws://testserver)."""

    def __init__(self, app, **opcoes):
        super().__init__(app, base_url=BASE_PAINEL, **opcoes)

    def websocket_connect(self, url, *argumentos, **opcoes):
        return super().websocket_connect(urljoin(BASE_PAINEL.replace("http", "ws", 1), url), *argumentos, **opcoes)


def basic(usuario: str, senha: str) -> dict:
    """O cabecalho Authorization do HTTP Basic (para o WebSocket: o TestClient so monta o do HTTP sozinho)."""
    return {"Authorization": "Basic " + base64.b64encode(f"{usuario}:{senha}".encode()).decode()}
