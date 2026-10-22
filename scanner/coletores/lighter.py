"""Conector proprio da Lighter (especificacao, secao 6): a principal (cota em USDC) e a Lighter Robinhood (USDG).

A Lighter nao passa pelo ccxt: o ccxt nao cobre a Lighter Robinhood, e a especificacao pede um conector
proprio usando a API publica dela. A conversa e pelo cliente HTTP/WebSocket do aiohttp (vem com o ccxt).

Como funciona (documentacao oficial: https://apidocs.lighter.xyz, lida em 22/10/2026):
  - Mercados (REST, GET /api/v1/orderBookDetails): so os perpetuos (`order_book_details`) com status
    "active". A Lighter e so futuros (especificacao, secao 2). Cada um vira um market no estilo do ccxt
    ("ETH/USDC:USDC" na principal, "ETH/USDG:USDG" na Robinhood), para o normalizador e o resto do scanner
    funcionarem igual as outras corretoras ("1000PEPE" vira PEPE x1000, como na MEXC).
  - Uma conexao WebSocket so por instancia, sempre so leitura (?readonly=true): o scanner nunca opera, e
    assim funciona tambem a partir de regiao restrita.
  - Etapa 1 (tickers): o canal market_stats/all (todos os mercados numa assinatura) traz o melhor bid/ask,
    o volume do dia na moeda de cotacao, a variacao do dia e o funding. Toda mensagem registra a conexao viva.
  - Etapa 2 (livros): o canal order_book/{market_id}. A primeira mensagem ("subscribed/order_book") e o livro
    completo; as seguintes ("update/order_book") so as mudancas, juntadas no livro guardado (tamanho 0 = o
    nivel sai). Continuidade: o begin_nonce de cada mudanca precisa ser o nonce da anterior; quebrou, o livro
    sai do estado e e pedido de novo (cancelar e assinar). Cancelar = mensagem "unsubscribe".
  - Heartbeat: {"type": "ping"} a cada intervalo_ping_lighter_s (o servidor fecha a conexao que passa 2 min
    sem receber nada). Sem nenhuma mensagem por dois intervalos de ping (nem o pong), a conexao e dada como
    morta e o coletor reconecta.
  - Funding: o current_funding_rate do market_stats (estimativa da proxima cobranca), ja em %; a Lighter
    cobra de hora em hora. Funding e taxa taker vao direto para o DadosLentos (receber_funding e
    receber_taxas): o laco REST do DadosLentos so sabe ler as corretoras do ccxt.

Robustez (especificacao, secao 8), igual ao ColetorCcxt: conexao que cai volta sozinha com espera crescente
(1s, 2s, 4s... ate espera_max_reconexao_s), a saude e as reconexoes vao para o estado, descartes contados,
atraso medio pelo horario que a Lighter informa, limite de assinaturas de livros pelo volume 24h e a lista de
mercados recarregada pelo __main__ (load_markets(reload=True)).

O formato das mensagens segue a documentacao (as fixtures em tests/dados/ tambem); precisa ser conferido com
uma mensagem real no PC do dono.
"""

import asyncio
import contextlib
import heapq
import json
import logging
import math
import ssl
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import aiohttp

from scanner.coletores.base import Coletor
from scanner.coletores.ccxt_coletor import DICA_LIMITE, TIPOS_DE_DESCARTE
from scanner.coletores.reconexao import (
    MARGEM_PARA_TROCAR_LIVRO,
    espera_reconexao,
    parece_limite_de_assinatura,
    selecionar_pares,
)
from scanner.config.carregar import Config
from scanner.estado.mercados import EstadoMercados
from scanner.modelos import Livro, Ticker
from scanner.normalizador import ParNormalizado, converter_livro, normalizar, prioridade_cotacao

registro = logging.getLogger("scanner.coletores")

# Nome da corretora no scanner (config.yaml) -> instancia da Lighter.
CORRETORAS_LIGHTER = {"lighter": "principal", "lighter_rh": "robinhood"}
NOME_DA_INSTANCIA = {instancia: nome for nome, instancia in CORRETORAS_LIGHTER.items()}
# Moeda de cotacao (e de garantia) de cada instancia: USDC na principal, USDG na Robinhood (docs/lighter-rh).
COTACAO_DA_INSTANCIA = {"principal": "USDC", "robinhood": "USDG"}
# A Lighter cobra funding a cada hora cheia (docs.lighter.xyz/trading/funding). O intervalo_funding_h do
# config.yaml vale sobre este.
INTERVALO_FUNDING_LIGHTER_H = 1
# Canal da etapa 1: as estatisticas de todos os mercados numa assinatura so.
CANAL_TICKERS = "market_stats/all"
# Tempo maximo de uma chamada REST e da abertura da conexao WebSocket, em segundos (o mesmo padrao do ccxt).
# Detalhe tecnico da conexao, nao limite de negocio.
TEMPO_MAX_REDE_S = 10
# Sem nenhuma mensagem por tantos intervalos de ping (nem a resposta do ping), a conexao esta morta.
PINGS_SEM_RESPOSTA = 2
# Niveis de cada lado que vao ao estado (os melhores). O motor so come o livro ate o tamanho de referencia
# ($100 por padrao) e a Lighter manda o livro inteiro (milhares de niveis nos pares grandes): copiar todos a
# cada mudanca gastaria CPU a toa. Detalhe tecnico; se o livro nao chegar ao tamanho, o sinal e descartado.
NIVEIS_POR_LADO = 100
# Erros que a Lighter manda e que nao sao problema: 30003 = ja estava assinado, 30002 = nao estava assinado.
ERROS_INOFENSIVOS = frozenset({30002, 30003})


class ErroApiLighter(Exception):
    """A API REST da Lighter respondeu erro ou algo num formato que o scanner nao entende."""


class ConexaoFechada(ConnectionError):
    """A conexao WebSocket da Lighter fechou (ou ficou muda)."""


def e_lighter(nome: str) -> bool:
    """True se a corretora usa o conector proprio da Lighter ("lighter" ou "lighter_rh")."""
    return nome in CORRETORAS_LIGHTER


def _nome_da_instancia(instancia: str) -> str:
    if instancia not in NOME_DA_INSTANCIA:
        raise ValueError(f"instancia da Lighter desconhecida: {instancia!r} (use principal ou robinhood)")
    return NOME_DA_INSTANCIA[instancia]


def endereco_somente_leitura(url: str) -> str:
    """O endereco do WebSocket com ?readonly=true (so leitura; vale tambem em regiao restrita)."""
    base, interrogacao, consulta = url.partition("?")
    if not interrogacao:
        return f"{url}?readonly=true"
    if "readonly=true" in consulta.split("&"):
        return url
    return f"{url}&readonly=true"


def _numero(valor) -> float | None:
    """Numero real de um campo da Lighter (ela manda precos em texto, ex.: "2064.50"), ou None.

    bool, NaN, infinito e texto que nao e numero contam como None.
    """
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        numero = float(valor)
    elif isinstance(valor, str):
        try:
            numero = float(valor)
        except ValueError:
            return None
    else:
        return None
    return numero if math.isfinite(numero) else None


def _inteiro(valor) -> int | None:
    """Numero inteiro (int ou texto com digitos, ex.: market_id e nonce), ou None. bool nao conta."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, str) and valor.strip().lstrip("-").isdigit():
        return int(valor)
    return None


def _passo(decimais) -> float | None:
    """10^-decimais (o menor passo de preco ou tamanho), ou None se nao veio."""
    casas = _inteiro(decimais)
    return None if casas is None or casas < 0 else 10.0 ** -casas


# ---------------------------------------------------------------------------
# Formatos da API (funcoes puras)
# ---------------------------------------------------------------------------


def markets_de_lighter(resposta: dict, cotacao: str) -> tuple[dict, int]:
    """Os perpetuos ativos do orderBookDetails no estilo do ccxt, e quantos inativos ficaram de fora.

    cotacao: "USDC" (principal) ou "USDG" (Robinhood). Devolve ({simbolo: market}, inativos), com o simbolo
    como o ccxt escreveria um perpetuo ("ETH/USDC:USDC"). O spot (`spot_order_book_details`) nao entra.
    Cada market guarda o market_id em "id", a taxa taker em "taker" (fracao, como o ccxt) e os decimais em
    "precision"; o item original fica em "info". Item malformado (sem simbolo ou sem market_id) e pulado.

    Taxa: o taker_fee vem em % (como os outros campos de taxa da Lighter, ex.: liquidation_fee "1.0000" = 1%);
    "0.0000" e a conta Standard (0%). Taxa ilegivel vira None (o motor descarta por falta de taxa).
    """
    detalhes = resposta.get("order_book_details") if isinstance(resposta, dict) else None
    if not isinstance(detalhes, list):
        raise ErroApiLighter("a resposta do orderBookDetails veio sem a lista order_book_details (a API mudou?)")
    markets: dict[str, dict] = {}
    inativos = 0
    for detalhe in detalhes:
        if not isinstance(detalhe, dict):
            continue
        base, ident = detalhe.get("symbol"), _inteiro(detalhe.get("market_id"))
        if not isinstance(base, str) or not base or "/" in base or ident is None:
            continue
        if detalhe.get("market_type") not in (None, "perp"):
            continue
        if detalhe.get("status") != "active":
            inativos += 1
            continue
        taxa_pct = _numero(detalhe.get("taker_fee"))
        simbolo = f"{base}/{cotacao}:{cotacao}"
        markets[simbolo] = {
            "id": str(ident), "symbol": simbolo, "base": base, "quote": cotacao, "settle": cotacao,
            "type": "swap", "spot": False, "swap": True, "future": False, "option": False,
            "linear": True, "inverse": False, "contractSize": 1.0, "active": True,
            "taker": taxa_pct / 100 if taxa_pct is not None and taxa_pct >= 0 else None,
            "precision": {"amount": _passo(detalhe.get("size_decimals")),
                          "price": _passo(detalhe.get("price_decimals"))},
            "info": detalhe,
        }
    return markets, inativos


def _niveis(lista) -> dict[float, float] | None:
    """{preco: tamanho} dos niveis [{"price": "...", "size": "..."}] da Lighter, ou None se algum for invalido.

    Preco precisa ser numero real maior que zero; tamanho, numero real maior ou igual a zero (nas mudancas,
    0 = o nivel sai). Lado que nao veio conta como vazio.
    """
    if lista is None:
        return {}
    if not isinstance(lista, list):
        return None
    niveis = {}
    for nivel in lista:
        if not isinstance(nivel, dict):
            return None
        preco, tamanho = _numero(nivel.get("price")), _numero(nivel.get("size"))
        if preco is None or tamanho is None or preco <= 0 or tamanho < 0:
            return None
        niveis[preco] = tamanho
    return niveis


def _montar_livro(corretora: str, simbolo: str, bids: dict[float, float], asks: dict[float, float],
                  agora: float, ts_ms: float | None) -> Livro | None:
    """O Livro com os NIVEIS_POR_LADO melhores niveis de cada lado, em ordem (melhor preco primeiro), ou None.

    bids/asks: {preco: tamanho}, so com tamanho maior que zero. As mesmas regras do livro_de_ccxt: lado
    vazio, preco <= 0 (ja barrado em _niveis) ou melhor bid maior ou igual ao melhor ask descartam o livro.
    """
    if not bids or not asks:
        return None
    precos_bids = heapq.nlargest(NIVEIS_POR_LADO, bids)
    precos_asks = heapq.nsmallest(NIVEIS_POR_LADO, asks)
    if precos_bids[0] >= precos_asks[0]:  # cruzado: a melhor compra paga mais que a melhor venda
        return None
    return Livro(corretora=corretora, mercado="futuro", simbolo_original=simbolo,
                 bids=[(p, bids[p]) for p in precos_bids], asks=[(p, asks[p]) for p in precos_asks],
                 recebido_em=agora, ts_corretora=None if ts_ms is None else ts_ms / 1000)  # a Lighter manda ms


def livro_de_lighter(msg: dict, instancia: str, agora: float, simbolo: str | None = None) -> Livro | None:
    """Transforma uma mensagem do canal order_book com o livro COMPLETO em um Livro (ou None, descartado).

    msg: a primeira mensagem depois de assinar ("subscribed/order_book"), no formato documentado:
        {"channel": "order_book:0", "order_book": {"bids": [{"price", "size"}], "asks": [...], "nonce", ...},
         "timestamp": ms, "type": ...}.
    instancia: "principal" (corretora "lighter") ou "robinhood" ("lighter_rh").
    simbolo: o simbolo do scanner (ex.: "ETH/USDC:USDC"); sem ele, o market_id do canal ("0").
    As mesmas regras de descarte do livro_de_ccxt: vazio (ou um lado so), preco zero, negativo ou invalido,
    cruzado (bid >= ask). Os niveis sao postos em ordem; os de tamanho 0 nao entram, e so os NIVEIS_POR_LADO
    melhores de cada lado ficam. Precos crus, como a Lighter manda (o coletor converte "1000PEPE" para 1
    unidade do PEPE ao guardar no estado).
    """
    corretora = _nome_da_instancia(instancia)
    dados = msg.get("order_book") if isinstance(msg, dict) else None
    if not isinstance(dados, dict):
        return None
    bids, asks = _niveis(dados.get("bids")), _niveis(dados.get("asks"))
    if bids is None or asks is None:
        return None
    if simbolo is None:
        simbolo = str(msg.get("channel") or "").partition(":")[2]
    return _montar_livro(corretora, simbolo, _sem_zeros(bids), _sem_zeros(asks), agora, _numero(msg.get("timestamp")))


def _sem_zeros(niveis: dict[float, float]) -> dict[float, float]:
    """Os niveis com tamanho maior que zero (no livro completo, tamanho 0 nao e nivel)."""
    return {preco: tamanho for preco, tamanho in niveis.items() if tamanho > 0}


def ticker_de_lighter(corretora: str, simbolo: str, stats: dict, agora: float) -> Ticker | None:
    """O Ticker de um mercado do market_stats, ou None se faltar bid/ask valido (zero, cruzado, ilegivel).

    Volume = daily_quote_token_volume (na moeda de cotacao, USDC ou USDG); variacao = daily_price_change (%).
    """
    bid, ask = _numero(stats.get("best_bid_price")), _numero(stats.get("best_ask_price"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or bid >= ask:
        return None
    return Ticker(corretora=corretora, mercado="futuro", simbolo_original=simbolo, bid=bid, ask=ask, recebido_em=agora,
                  volume_24h_usd=_numero(stats.get("daily_quote_token_volume")),
                  variacao_24h_pct=_numero(stats.get("daily_price_change")))


# ---------------------------------------------------------------------------
# Rede (aiohttp)
# ---------------------------------------------------------------------------


def _contexto_ssl() -> ssl.SSLContext:
    """Certificados do certifi (vem com o ccxt, que usa os mesmos); sem ele, os do sistema."""
    try:
        import certifi
    except ImportError:  # pragma: no cover - o certifi vem com o ccxt
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class _ConexaoWs:
    """A conexao WebSocket aberta: enviar um objeto JSON, receber o proximo, fechar."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self._ws = ws
        self._trava = asyncio.Lock()  # um envio por vez (ping, assinaturas e pong saem de tarefas diferentes)

    async def enviar(self, dados: dict) -> None:
        async with self._trava:
            await self._ws.send_str(json.dumps(dados))

    async def receber(self):
        """O proximo objeto JSON que chegou (None se a mensagem nao era JSON). Conexao fechada: ConexaoFechada."""
        while True:
            mensagem = await self._ws.receive()
            if mensagem.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                try:
                    return json.loads(mensagem.data)
                except ValueError:
                    return None
            if mensagem.type == aiohttp.WSMsgType.ERROR:
                raise ConexaoFechada(f"erro na conexao WebSocket ({self._ws.exception()})")
            if mensagem.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                raise ConexaoFechada(f"a Lighter fechou a conexao (codigo {self._ws.close_code})")
            # PING/PONG do proprio protocolo WebSocket: o aiohttp ja responde; segue esperando.

    async def fechar(self) -> None:
        await self._ws.close()


class ClienteLighter:
    """A API publica de uma instancia da Lighter: lista de mercados (REST) e a conexao WebSocket.

    Faz o papel do "exchange" do ccxt para o __main__: `markets`, `load_markets(reload=...)` e `close()`.
    Nao acessa a rede ao ser criado; a sessao HTTP do aiohttp nasce no primeiro uso.
    instancia: "principal" ou "robinhood". url_rest / url_ws: da Config (url_rest_lighter...). O WebSocket e
    sempre aberto so leitura (?readonly=true).
    """

    def __init__(self, instancia: Literal["principal", "robinhood"], url_rest: str, url_ws: str,
                 tempo_max_s: float = TEMPO_MAX_REDE_S):
        _nome_da_instancia(instancia)
        self.instancia = instancia
        self.cotacao = COTACAO_DA_INSTANCIA[instancia]
        self.url_rest = url_rest.rstrip("/")
        self.url_ws = endereco_somente_leitura(url_ws)
        self.markets: dict = {}  # {simbolo: market} (veja markets_de_lighter); vazio ate o primeiro load_markets
        self.has: dict = {}
        self._tempo_max_s = tempo_max_s
        self._sessao: aiohttp.ClientSession | None = None
        self._inativos_no_log: int | None = None

    def _sessao_http(self) -> aiohttp.ClientSession:
        if self._sessao is None or self._sessao.closed:
            tempo = aiohttp.ClientTimeout(total=None, connect=self._tempo_max_s, sock_connect=self._tempo_max_s)
            self._sessao = aiohttp.ClientSession(timeout=tempo, connector=aiohttp.TCPConnector(ssl=_contexto_ssl()))
        return self._sessao

    async def load_markets(self, reload: bool = False, params=None) -> dict:
        """Le os perpetuos ativos (GET /api/v1/orderBookDetails). Ja carregados e sem reload: devolve os mesmos.

        Erro de HTTP ou resposta fora do formato: ErroApiLighter (o __main__ registra e tenta de novo).
        Cada recarga troca `markets` por um dicionario novo (o coletor percebe e refaz o indice).
        """
        if self.markets and not reload:
            return self.markets
        url = f"{self.url_rest}/api/v1/orderBookDetails"
        async with self._sessao_http().get(url, timeout=aiohttp.ClientTimeout(total=self._tempo_max_s)) as resposta:
            texto = await resposta.text()
            if resposta.status != 200:
                raise ErroApiLighter(f"orderBookDetails respondeu HTTP {resposta.status}: {texto[:200]}")
        try:
            dados = json.loads(texto)
        except ValueError as erro:
            raise ErroApiLighter(f"orderBookDetails nao respondeu JSON ({erro})") from erro
        codigo = _inteiro(dados.get("code")) if isinstance(dados, dict) else None
        if codigo is not None and codigo != 200:
            raise ErroApiLighter(f"orderBookDetails respondeu code {codigo}: {dados.get('message')}")
        markets, inativos = markets_de_lighter(dados, self.cotacao)
        if inativos != self._inativos_no_log:
            self._inativos_no_log = inativos
            registro.info("%s: %d perpetuos ativos; %d inativos ficam de fora", NOME_DA_INSTANCIA[self.instancia],
                          len(markets), inativos)
        self.markets = markets
        return markets

    async def conectar_ws(self) -> _ConexaoWs:
        """Abre a conexao WebSocket so leitura (no maximo tempo_max_s para abrir)."""
        async with asyncio.timeout(self._tempo_max_s):
            ws = await self._sessao_http().ws_connect(
                self.url_ws, autoping=True, heartbeat=None, timeout=aiohttp.ClientWSTimeout(ws_close=self._tempo_max_s))
        return _ConexaoWs(ws)

    async def close(self) -> None:
        """Fecha a sessao HTTP (e com ela as conexoes). Pode ser chamada mais de uma vez."""
        sessao, self._sessao = self._sessao, None
        if sessao is not None and not sessao.closed:
            await sessao.close()


def criar_cliente_lighter(nome: str, cfg: Config) -> ClienteLighter:
    """O ClienteLighter da corretora `nome` ("lighter" ou "lighter_rh"), com os enderecos da Config. Sem rede."""
    instancia = CORRETORAS_LIGHTER.get(nome)
    if instancia == "principal":
        return ClienteLighter(instancia, cfg.url_rest_lighter, cfg.url_ws_lighter)
    if instancia == "robinhood":
        return ClienteLighter(instancia, cfg.url_rest_lighter_rh, cfg.url_ws_lighter_rh)
    raise ValueError(f"'{nome}' nao e uma corretora da Lighter (use lighter ou lighter_rh)")


def _erro_de_conexao(erro: BaseException) -> bool:
    """True para erro da conexao (rede, tempo esgotado, a Lighter fechou): nesses casos o traceback nao ajuda."""
    return isinstance(erro, (aiohttp.ClientError, OSError))


# ---------------------------------------------------------------------------
# Coletor
# ---------------------------------------------------------------------------


@dataclass
class _LivroAberto:
    """O livro de um par assinado na conexao atual: o livro completo com as mudancas juntadas."""

    bids: dict[float, float] = field(default_factory=dict)  # preco -> tamanho
    asks: dict[float, float] = field(default_factory=dict)
    nonce: int | None = None  # nonce da ultima mensagem aplicada
    pronto: bool = False  # ja chegou o livro completo desta assinatura (antes dele, mudanca nao vale)
    quebras: int = 0  # quebras seguidas sem nenhuma mudanca conferida entre elas (espera crescente)
    espera: float = 0.0  # espera antes de assinar de novo depois da ultima quebra
    mudancas_sem_completo: int = 0  # mudancas que chegaram antes do livro completo (para avisar no log)


class ColetorLighter(Coletor):
    """Acompanha uma instancia da Lighter: tickers (market_stats/all) e livros (order_book) numa conexao so.

    Oferece ao __main__ o mesmo que o ColetorCcxt: manter_tickers (com reconexao), assinar_livros,
    simbolos_por_chave, cotacoes_dos_simbolos, markets, usar_para_cotacao, tirar_descartes e fechar.

    instancia: "principal" (corretora "lighter", USDC) ou "robinhood" ("lighter_rh", USDG).
    cliente: o ClienteLighter (ou um falso nos testes): `markets` e `conectar_ws()`.
    estado: EstadoMercados opcional; com ele cada mensagem registra a conexao viva e cada ticker/livro valido
        vai para la, com o ativo normalizado e o preco de 1 unidade do ativo.
    dados_lentos: o DadosLentos que recebe o funding (market_stats) e a taxa taker (orderBookDetails).
    relogio: hora atual em segundos (os testes passam um relogio falso).
    espera_max_reconexao_s: maior espera entre duas tentativas de reconexao (da Config).
    esperar: espera da reconexao e da nova assinatura de livro (padrao asyncio.sleep; os testes trocam).
    esperar_ping: espera entre dois pings (separada, para os testes controlarem o ping).
    limite_assinaturas: maximo de livros abertos ao mesmo tempo (limites_assinatura da Config); None = sem limite.
    intervalo_ping_s: intervalo_ping_lighter_s da Config.
    mensagens_sem_bid_ask: mensagens seguidas do market_stats sem bid/ask para o log dar ERRO (uma vez).

    A conexao vive enquanto manter_tickers roda: se ele for reiniciado (lista de pares mudou), a conexao e
    refeita e os livros desejados sao assinados de novo (cada um com um livro completo novo).
    """

    def __init__(
        self,
        instancia: Literal["principal", "robinhood"],
        cliente,
        estado: EstadoMercados | None = None,
        relogio: Callable[[], float] = time.time,
        dados_lentos=None,
        espera_max_reconexao_s: float = Config.espera_max_reconexao_s,
        esperar: Callable[[float], Awaitable] = asyncio.sleep,
        esperar_ping: Callable[[float], Awaitable] = asyncio.sleep,
        limite_assinaturas: int | None = None,
        intervalo_ping_s: float = Config.intervalo_ping_lighter_s,
        mensagens_sem_bid_ask: int = Config.mensagens_sem_bid_ask,
    ):
        super().__init__(_nome_da_instancia(instancia))
        self.instancia = instancia
        self._cliente = cliente
        self._estado = estado
        self._relogio = relogio
        self._dados_lentos = dados_lentos
        self._espera_max_s = espera_max_reconexao_s
        self._esperar = esperar
        self._esperar_ping = esperar_ping
        self._limite = limite_assinaturas
        self._intervalo_ping_s = intervalo_ping_s
        self._silencio_max_s = intervalo_ping_s * PINGS_SEM_RESPOSTA
        self._max_sem_bid_ask = mensagens_sem_bid_ask
        self._ao_atualizar: Callable[[Livro], None] | None = None
        self._ao_ticker: Callable[[Ticker], None] | None = None
        # Indice dos mercados (refeito quando o cliente recarrega a lista).
        self._markets_indexados = None
        self._pares: dict[str, ParNormalizado] = {}  # simbolo -> par, so dos que vao ao estado (um por chave)
        self._vencedor_da_chave: dict[tuple[str, str], str] = {}  # ("futuro", ativo) -> simbolo
        self._simbolo_do_id: dict[str, str] = {}  # market_id (texto) -> simbolo
        self._id_do_simbolo: dict[str, str] = {}
        # Conexao e livros.
        self._conexao = None  # a conexao aberta agora (None = desconectado)
        self._fechado = False
        self._tarefas: set[asyncio.Task] = set()  # manter_tickers em andamento (o fechar() para)
        self._simbolos_ticker: set[str] = set()
        self._simbolos_cotacao: set[str] = set()
        self._livros_desejados: set[str] = set()  # o que a etapa 2 pediu (sobrevive a quedas)
        self._livros: dict[str, _LivroAberto] = {}  # assinados na conexao atual
        self._livros_no_estado: dict[str, str] = {}  # simbolo -> ativo do livro guardado no estado
        self._reassinaturas: dict[str, asyncio.Task] = {}  # simbolo -> nova assinatura esperando (quebra repetida)
        self._volumes_conhecidos: dict[str, float] = {}  # simbolo -> volume 24h (do market_stats)
        # Saude e log.
        self._ticker_desde_o_inicio = False
        self._caiu_em: float | None = None
        self._queda_com_traceback = False
        self._descartes: Counter = Counter()
        self._avisados: set[str] = set()
        self._erros_avisados: set = set()
        self._traceback_tratar: set[str] = set()
        self._traceback_ticker: set[str] = set()
        self._fora_do_limite = 0
        self._seguidas_sem_bid_ask = 0
        self._avisou_sem_bid_ask = False
        self._avisou_sem_nonce = False
        self._sem_completo_avisado: set[str] = set()  # simbolos ja avisados de mudancas sem o livro completo

    # --- etapa 1 e conexao ---------------------------------------------------

    async def manter_tickers(self, simbolos: list[str], ao_ticker: Callable[[Ticker], None] | None = None) -> None:
        """Etapa 1 com reconexao: abre a conexao, acompanha os tickers de `simbolos` e volta sozinho a cada queda.

        Queda (rede, a Lighter fechou, conexao muda): o estado esquece a corretora (marcar_desconectada), o painel
        mostra "reconectando" e o coletor espera 1s, 2s, 4s... (ate espera_max_reconexao_s). O primeiro
        market_stats depois da queda conta a reconexao, e a proxima queda volta a esperar 1s. Os livros que a
        etapa 2 pediu sao assinados de novo na conexao nova. Cancelamento (Ctrl+C, fechar()) sobe para quem chamou.
        Sem nenhum simbolo, termina na hora (como o ColetorCcxt).
        """
        if ao_ticker is not None:
            self._ao_ticker = ao_ticker
        simbolos = list(simbolos)
        if not simbolos:
            return
        tarefa = asyncio.current_task()
        self._tarefas.add(tarefa)
        tentativa = 0
        try:
            while not self._fechado:
                self._ticker_desde_o_inicio = False
                try:
                    await self._conectar_e_ler(simbolos)
                except Exception as erro:  # cancelamento nao entra aqui (nao e Exception)
                    if self._fechado:
                        return
                    if self._ticker_desde_o_inicio:
                        tentativa = 0  # tinha voltado a funcionar: a espera recomeca de 1s
                    espera = espera_reconexao(tentativa, self._espera_max_s)
                    tentativa += 1
                    self._queda(erro, espera, tentativa)
                    await self._esperar(espera)
        finally:
            self._tarefas.discard(tarefa)

    async def _conectar_e_ler(self, simbolos_ticker: list[str]) -> None:
        """Uma conexao: abre, assina o market_stats/all e os livros desejados, e le ate cair (levanta o erro)."""
        self._simbolos_ticker = set(simbolos_ticker)
        conexao = await self._cliente.conectar_ws()
        self._conexao = conexao
        ping = asyncio.create_task(self._pingar(conexao), name=f"ping {self.nome}")
        try:
            await conexao.enviar({"type": "subscribe", "channel": CANAL_TICKERS})
            for simbolo in sorted(self._livros_desejados):
                await self._assinar(conexao, simbolo)
            await self._ler(conexao)
        finally:
            if self._conexao is conexao:
                self._conexao = None
            ping.cancel()
            esperando = [ping, *self._reassinaturas.values()]
            for simbolo in list(self._livros):
                self._largar_livro(simbolo)  # esta conexao nao atualiza mais nenhum livro
            await asyncio.gather(*esperando, return_exceptions=True)
            with contextlib.suppress(Exception):
                await conexao.fechar()

    async def _pingar(self, conexao) -> None:
        """A cada intervalo_ping_s manda {"type": "ping"}. Se o envio falhar, fecha a conexao (o leitor reconecta)."""
        try:
            while True:
                await self._esperar_ping(self._intervalo_ping_s)
                await conexao.enviar({"type": "ping"})
        except Exception as erro:  # cancelamento nao entra aqui
            registro.debug("%s: o ping nao foi enviado (%s: %s); fechando a conexao para reconectar",
                           self.nome, type(erro).__name__, erro)
            with contextlib.suppress(Exception):
                await conexao.fechar()

    async def _ler(self, conexao) -> None:
        """Le e trata as mensagens ate a conexao cair (ou ficar muda por mais de dois intervalos de ping)."""
        while True:
            try:
                async with asyncio.timeout(self._silencio_max_s):
                    mensagem = await conexao.receber()
            except TimeoutError:
                raise ConexaoFechada(f"nenhuma mensagem em {self._silencio_max_s:g}s (nem a resposta do ping); "
                                     "a conexao parece morta") from None
            agora = self._relogio()
            self._registrar_mensagem(agora)  # toda mensagem conta: a conexao esta viva
            if not isinstance(mensagem, dict):
                registro.debug("%s: mensagem que nao e um objeto JSON; ignorada", self.nome)
                continue
            if mensagem.get("type") == "ping":  # o servidor tambem pode pingar: responde
                await conexao.enviar({"type": "pong"})
                continue
            try:
                reassinar = self._tratar(mensagem, agora)
            except Exception:  # defeito ao tratar esta mensagem: so ela fica de fora
                self._erro_ao_tratar(mensagem)
                continue
            if reassinar is not None:
                await self._reassinar(conexao, reassinar)

    def _tratar(self, mensagem: dict, agora: float) -> str | None:
        """Trata uma mensagem. Devolve o simbolo cujo livro precisa ser pedido de novo (ou None)."""
        if "error" in mensagem:
            self._erro_do_servidor(mensagem.get("error"))
            return None
        canal = mensagem.get("channel")
        if not isinstance(canal, str):
            return None  # "connected", "pong", "unsubscribed"...: so contam como mensagem
        if canal.startswith("market_stats:"):
            self._tratar_estatisticas(mensagem, agora)
            return None
        if canal.startswith("order_book:"):
            return self._tratar_livro(canal.partition(":")[2], mensagem, agora)
        return None

    def _tratar_estatisticas(self, mensagem: dict, agora: float) -> None:
        """market_stats: ticker de cada par da lista, funding e volume de todos os mercados conhecidos."""
        dados = mensagem.get("market_stats")
        if isinstance(dados, dict) and "market_id" in dados:
            itens = [dados]  # canal de um mercado so (market_stats/{id})
        elif isinstance(dados, dict):
            itens = list(dados.values())  # market_stats/all: {market_id: estatisticas}
        else:
            itens = []
        if not itens:
            return
        self._chegou_ticker(agora)
        self._indexar_se_preciso()
        total = com_bid_ask = 0
        for stats in itens:
            if not isinstance(stats, dict):
                continue
            simbolo = self._simbolo_do_id.get(str(stats.get("market_id")))
            if simbolo is None:
                continue  # mercado fora do indice (inativo, ou novo antes da recarga da lista)
            self._guardar_funding_e_volume(simbolo, stats)
            if simbolo not in self._simbolos_ticker and simbolo not in self._simbolos_cotacao:
                continue
            total += 1
            try:
                ticker = ticker_de_lighter(self.nome, simbolo, stats, agora)
                if ticker is not None or (_numero(stats.get("best_bid_price")) is not None
                                          and _numero(stats.get("best_ask_price")) is not None):
                    com_bid_ask += 1
                if ticker is None:
                    self._descartes["tickers"] += 1
                    registro.debug("%s %s: ticker descartado (sem bid/ask, preco invalido ou cruzado)",
                                   self.nome, simbolo)
                    continue
                self._entregar_ticker(ticker)
            except Exception:  # defeito ao tratar este ticker: so ele fica de fora
                self._descartes["tickers"] += 1
                self._erro_ao_tratar_ticker(simbolo)
        horario = _numero(mensagem.get("timestamp"))
        if horario is not None:
            self._registrar_atraso(agora, horario / 1000)  # a Lighter manda ms
        self._conferir_bid_ask(total, com_bid_ask)

    def _guardar_funding_e_volume(self, simbolo: str, stats: dict) -> None:
        volume = _numero(stats.get("daily_quote_token_volume"))
        if volume is not None:
            self._volumes_conhecidos[simbolo] = volume  # para o limite de assinaturas de livros
        par = self._pares.get(simbolo)
        funding = _numero(stats.get("current_funding_rate"))  # ja em %, por hora (a proxima cobranca)
        if par is not None and funding is not None and self._dados_lentos is not None:
            self._dados_lentos.receber_funding(self.nome, par.ativo, funding, INTERVALO_FUNDING_LIGHTER_H)

    def _conferir_bid_ask(self, total: int, com_bid_ask: int) -> None:
        """ERRO no log (uma vez) se o market_stats parar de trazer bid/ask por mensagens_sem_bid_ask mensagens."""
        if total == 0:
            return
        if com_bid_ask:
            if self._avisou_sem_bid_ask:
                registro.info("%s: o market_stats voltou a trazer bid/ask", self.nome)
            self._seguidas_sem_bid_ask = 0
            self._avisou_sem_bid_ask = False
            return
        self._seguidas_sem_bid_ask += 1
        if self._seguidas_sem_bid_ask >= self._max_sem_bid_ask and not self._avisou_sem_bid_ask:
            self._avisou_sem_bid_ask = True
            registro.error("%s: %d mensagens seguidas do market_stats sem best_bid_price/best_ask_price: a "
                           "corretora fica sem tickers (e sem sinais). Confira o formato com uma mensagem real",
                           self.nome, self._seguidas_sem_bid_ask)

    def _entregar_ticker(self, ticker: Ticker) -> None:
        if self._estado is not None:
            if ticker.simbolo_original in self._simbolos_cotacao:
                self._estado.atualizar_cotacao(self.nome, ticker.simbolo_original, ticker.bid, ticker.ask,
                                               ticker.recebido_em)
            par = self._pares.get(ticker.simbolo_original)
            if par is not None:  # preco de 1 unidade do ativo: "1000PEPE" divide por 1000
                self._estado.atualizar_ticker(
                    self.nome, "futuro", par.ativo, ticker.bid / par.multiplicador, ticker.ask / par.multiplicador,
                    ticker.recebido_em, ticker.volume_24h_usd, ticker.variacao_24h_pct,
                    simbolo_original=ticker.simbolo_original, cotacao=par.cotacao,
                )
        if self._ao_ticker is not None:
            self._ao_ticker(ticker)

    # --- etapa 2: livros -----------------------------------------------------

    async def iniciar(self, simbolos: list[str], ao_atualizar: Callable[[Livro], None]) -> None:
        """Modo simples (imprimir_precos): acompanha o livro dos simbolos e entrega cada Livro cru ao callback.

        Sem reconexao: se a conexao cair, o erro sobe para quem chamou. Ao terminar (erro ou cancelamento),
        a conexao e o cliente sao sempre fechados.
        """
        self._ao_atualizar = ao_atualizar
        try:
            self._livros_desejados = self._conhecidos(simbolos)
            await self._conectar_e_ler([])
        finally:
            await self.fechar()

    async def assinar_livros(self, simbolos: set[str], ao_atualizar: Callable[[Livro], None] | None = None) -> None:
        """Etapa 2: deixa o livro aberto exatamente para `simbolos` (os de maior volume, se houver limite).

        Assina os novos primeiro e cancela ("unsubscribe") os que sairam; o livro que sai deixa o estado na
        hora. Sem conexao agora, so anota: a proxima conexao assina os desejados. Pode ser chamada a cada ciclo.
        ao_atualizar: recebe o Livro cru (guardado; so precisa vir na primeira vez). Sem ele, precisa de estado.
        """
        if ao_atualizar is not None:
            self._ao_atualizar = ao_atualizar
        if self._ao_atualizar is None and self._estado is None:
            raise ValueError("assinar_livros precisa de um ao_atualizar ou de um estado para guardar os livros")
        if self._fechado:
            return
        desejados = self._livros_dentro_do_limite(self._conhecidos(simbolos))
        saindo = sorted(self._livros_desejados - desejados)
        entrando = sorted(desejados - self._livros_desejados)
        self._livros_desejados = desejados
        for simbolo in saindo:
            self._largar_livro(simbolo)  # parou de valer: sai do estado antes mesmo de a Lighter confirmar
        conexao = self._conexao
        if conexao is None:
            return
        try:
            for simbolo in entrando:
                if conexao is not self._conexao:
                    return  # a conexao caiu no meio: a proxima assina os desejados
                await self._assinar(conexao, simbolo)
            for simbolo in saindo:
                canal = self._canal_do_livro(simbolo)
                if conexao is not self._conexao:
                    return
                if canal is not None:  # None: o mercado saiu da lista (a assinatura morre com a conexao)
                    await conexao.enviar({"type": "unsubscribe", "channel": canal})
        except Exception as erro:  # a conexao caiu no meio: quem reconecta (manter_tickers) assina de novo
            registro.debug("%s: nao consegui ajustar os livros agora (%s: %s); a conexao sera refeita",
                           self.nome, type(erro).__name__, erro)

    def _conhecidos(self, simbolos) -> set[str]:
        """Os simbolos que a Lighter tem (e que vao ao estado); os outros avisam no log (uma vez cada)."""
        self._indexar_se_preciso()
        conhecidos = set()
        for simbolo in simbolos:
            if simbolo in self._id_do_simbolo and simbolo in self._pares:
                conhecidos.add(simbolo)
            elif simbolo not in self._avisados:
                self._avisados.add(simbolo)
                registro.warning("%s %s: par que a %s nao tem (ou inativo); o livro dele nao e assinado",
                                 self.nome, simbolo, self.nome)
        return conhecidos

    def _livros_dentro_do_limite(self, desejados: set[str]) -> set[str]:
        """Os livros que cabem no limite de assinaturas: os de maior volume 24h, com preferencia aos ja abertos."""
        if self._limite is None or len(desejados) <= self._limite:
            self._avisar_limite(0, len(desejados))
            return desejados
        volumes = {s: self._volumes_conhecidos[s] for s in desejados if s in self._volumes_conhecidos}
        escolhidos, fora = selecionar_pares(sorted(desejados), volumes, self._limite,
                                            abertos=self._livros_desejados, margem=MARGEM_PARA_TROCAR_LIVRO)
        self._avisar_limite(fora, len(desejados))
        return set(escolhidos)

    def _avisar_limite(self, fora: int, total: int) -> None:
        """Registra quantos livros ficaram de fora do limite, so quando esse numero muda."""
        if fora == self._fora_do_limite:
            return
        self._fora_do_limite = fora
        if fora:
            registro.warning("%s: limite de %d assinaturas de livros (limites_assinatura): %d de %d pares ficaram "
                             "de fora (os de menor volume 24h)", self.nome, self._limite, fora, total)
        else:
            registro.info("%s: todos os pares de livros cabem de novo no limite de assinaturas", self.nome)

    async def _assinar(self, conexao, simbolo: str) -> None:
        """Assina o livro do simbolo nesta conexao (se ainda for desejado e nao estiver assinado)."""
        canal = self._canal_do_livro(simbolo)
        if canal is None or simbolo in self._livros or simbolo not in self._livros_desejados:
            return
        self._livros[simbolo] = _LivroAberto()
        await conexao.enviar({"type": "subscribe", "channel": canal})

    def _largar_livro(self, simbolo: str) -> None:
        """Esquece o livro do simbolo nesta conexao e tira do estado o que ele tinha entregado."""
        tarefa = self._reassinaturas.pop(simbolo, None)
        if tarefa is not None:
            tarefa.cancel()
        self._livros.pop(simbolo, None)
        self._remover_livro_do_estado(simbolo)

    def _tratar_livro(self, ident: str, mensagem: dict, agora: float) -> str | None:
        """order_book: livro completo ou mudancas. Devolve o simbolo se o livro precisa ser pedido de novo."""
        tipo = mensagem.get("type")
        if tipo not in ("subscribed/order_book", "update/order_book"):
            return None  # ex.: "unsubscribed", a confirmacao do cancelamento
        simbolo = self._simbolo_do_id.get(ident)
        livro = self._livros.get(simbolo) if simbolo is not None else None
        if livro is None:
            return None  # nao assinado (ou acabou de ser cancelado): mensagem atrasada, ignorada
        dados = mensagem.get("order_book")
        if not isinstance(dados, dict):
            self._descartes["livros"] += 1
            return None
        if tipo == "subscribed/order_book":  # o livro completo (o primeiro depois de assinar)
            bids, asks = _niveis(dados.get("bids")), _niveis(dados.get("asks"))
            if bids is None or asks is None:
                return self._quebrou(simbolo, livro, "livro completo com nivel invalido", "livros")
            livro.bids, livro.asks = _sem_zeros(bids), _sem_zeros(asks)
            livro.nonce = _inteiro(dados.get("nonce"))
            livro.pronto = True
            livro.mudancas_sem_completo = 0
        else:  # "update/order_book": so as mudancas
            if not livro.pronto:
                self._mudanca_sem_completo(simbolo, livro)
                return None
            inicio = _inteiro(dados.get("begin_nonce"))
            if inicio is None or livro.nonce is None:
                self._avisar_sem_nonce()
            elif inicio != livro.nonce:
                return self._quebrou(simbolo, livro, f"begin_nonce {inicio}, esperado {livro.nonce}",
                                     "sequencia_quebrada")
            bids, asks = _niveis(dados.get("bids")), _niveis(dados.get("asks"))
            if bids is None or asks is None:
                return self._quebrou(simbolo, livro, "mudanca com nivel invalido", "livros")
            for lado, mudancas in ((livro.bids, bids), (livro.asks, asks)):
                for preco, tamanho in mudancas.items():
                    if tamanho == 0:
                        lado.pop(preco, None)  # tamanho 0: o nivel sai
                    else:
                        lado[preco] = tamanho  # o tamanho e o total novo do nivel
            livro.nonce = _inteiro(dados.get("nonce"))
            if inicio is not None:
                livro.quebras = 0  # a sequencia foi conferida: a proxima quebra pede o livro na hora
        self._entregar_juntado(simbolo, livro, mensagem, agora)
        return None

    def _mudanca_sem_completo(self, simbolo: str, livro: _LivroAberto) -> None:
        """Mudanca antes do livro completo: ignorada. Muitas seguidas (mensagens_sem_bid_ask) avisam uma vez.

        Depois de pedir o livro de novo, umas poucas mudancas velhas ainda chegam (normal). Muitas querem dizer
        que a Lighter nao manda o livro completo como "subscribed/order_book" (formato a conferir).
        """
        livro.mudancas_sem_completo += 1
        registro.debug("%s %s: mudanca antes do livro completo; ignorada", self.nome, simbolo)
        if livro.mudancas_sem_completo == self._max_sem_bid_ask and simbolo not in self._sem_completo_avisado:
            self._sem_completo_avisado.add(simbolo)
            registro.warning("%s %s: %d mudancas de livro sem o livro completo (subscribed/order_book) antes: o livro "
                             "nao entra no scanner. Confira o formato com uma mensagem real", self.nome, simbolo,
                             livro.mudancas_sem_completo)

    def _avisar_sem_nonce(self) -> None:
        if not self._avisou_sem_nonce:
            self._avisou_sem_nonce = True
            registro.warning("%s: mudanca de livro sem begin_nonce/nonce: nao da para conferir a sequencia "
                             "(confira o formato com uma mensagem real); as mudancas sao aplicadas assim mesmo",
                             self.nome)

    def _quebrou(self, simbolo: str, livro: _LivroAberto, motivo: str, tipo: str) -> str:
        """O livro deixou de ser confiavel: sai do estado, o guardado e esquecido e um livro completo novo e pedido.

        Pede na hora; se quebrar de novo sem nenhuma mudanca conferida no meio, espera 1s, 2s, 4s... (ate
        espera_max_reconexao_s) antes de pedir, para nao ficar pedindo sem parar.
        """
        self._descartes[tipo] += 1
        self._remover_livro_do_estado(simbolo)
        livro.bids, livro.asks, livro.nonce, livro.pronto = {}, {}, None, False
        livro.espera = 0.0 if livro.quebras == 0 else espera_reconexao(livro.quebras - 1, self._espera_max_s)
        livro.quebras += 1
        quando = "agora" if livro.espera <= 0 else f"em {livro.espera:g}s"
        if tipo == "sequencia_quebrada":
            registro.warning("%s %s: sequencia do livro quebrada (%s); o livro saiu do scanner e um livro completo "
                             "novo sera pedido %s", self.nome, simbolo, motivo, quando)
        else:
            registro.warning("%s %s: livro com dado invalido (%s); o livro saiu do scanner e um livro completo novo "
                             "sera pedido %s", self.nome, simbolo, motivo, quando)
        return simbolo

    async def _reassinar(self, conexao, simbolo: str) -> None:
        """Cancela e assina de novo o livro (a Lighter recusa assinar por cima de uma assinatura viva)."""
        livro = self._livros.get(simbolo)
        canal = self._canal_do_livro(simbolo)
        if livro is None or canal is None:
            return
        await conexao.enviar({"type": "unsubscribe", "channel": canal})
        if livro.espera <= 0:
            await conexao.enviar({"type": "subscribe", "channel": canal})
            return
        anterior = self._reassinaturas.pop(simbolo, None)
        if anterior is not None:
            anterior.cancel()
        self._reassinaturas[simbolo] = asyncio.create_task(self._assinar_depois(conexao, simbolo, livro.espera),
                                                           name=f"livro {self.nome} {simbolo}")

    async def _assinar_depois(self, conexao, simbolo: str, espera: float) -> None:
        try:
            await self._esperar(espera)
            canal = self._canal_do_livro(simbolo)
            if conexao is self._conexao and simbolo in self._livros and canal is not None and not self._fechado:
                await conexao.enviar({"type": "subscribe", "channel": canal})
        except Exception as erro:  # a conexao caiu: a proxima assina os desejados
            registro.debug("%s %s: nao consegui pedir o livro de novo (%s: %s)", self.nome, simbolo,
                           type(erro).__name__, erro)
        finally:
            if self._reassinaturas.get(simbolo) is asyncio.current_task():
                del self._reassinaturas[simbolo]

    def _entregar_juntado(self, simbolo: str, livro: _LivroAberto, mensagem: dict, agora: float) -> None:
        """Monta o Livro com o que esta guardado e entrega (se for valido)."""
        pronto = _montar_livro(self.nome, simbolo, livro.bids, livro.asks, agora, _numero(mensagem.get("timestamp")))
        if pronto is None:
            self._descartes["livros"] += 1
            registro.debug("%s %s: livro descartado (vazio, preco invalido ou cruzado)", self.nome, simbolo)
            return
        if pronto.ts_corretora is not None:
            self._registrar_atraso(agora, pronto.ts_corretora)
        self._entregar_livro(pronto)

    def _entregar_livro(self, livro: Livro) -> None:
        if self._estado is not None:
            par = self._pares.get(livro.simbolo_original)
            if par is not None:
                self._estado.atualizar_livro(converter_livro(livro, par), ativo=par.ativo)
                self._livros_no_estado[livro.simbolo_original] = par.ativo
        if self._ao_atualizar is not None:
            self._ao_atualizar(livro)

    # --- interface que o __main__ usa (igual ao ColetorCcxt) -------------------

    def simbolos_por_chave(self) -> dict[tuple[str, str], str]:
        """Para cada ("futuro", ativo), o simbolo cujos dados vao ao estado.

        Ex.: {("futuro", "PEPE"): "1000PEPE/USDC:USDC"}. O __main__ usa para saber quais tickers pedir.
        """
        self._indexar_se_preciso()
        return dict(self._vencedor_da_chave)

    def cotacoes_dos_simbolos(self, simbolos) -> set[str]:
        """As moedas de cotacao dos simbolos que vao ao estado ({"USDC"} na principal, {"USDG"} na Robinhood)."""
        self._indexar_se_preciso()
        return {self._pares[simbolo].cotacao for simbolo in simbolos if simbolo in self._pares}

    def markets(self) -> dict:
        """Os markets carregados ({simbolo: market}); vazio se ainda nao carregou."""
        return getattr(self._cliente, "markets", None) or {}

    def usar_para_cotacao(self, simbolos) -> None:
        """Simbolos cujo ticker tambem alimenta as cotacoes (a Lighter nao tem spot: na pratica, nenhum)."""
        self._simbolos_cotacao = set(simbolos)

    def tirar_descartes(self) -> dict[str, int]:
        """Quantos livros e tickers foram descartados (e sequencias quebradas) desde a ultima vez; zera a conta."""
        resumo = {tipo: self._descartes[tipo] for tipo in TIPOS_DE_DESCARTE}
        self._descartes.clear()
        return resumo

    async def fechar(self) -> None:
        """Para tudo, fecha a conexao e o cliente. Depois disso nada novo comeca."""
        self._fechado = True
        atual = asyncio.current_task()
        tarefas = [t for t in self._tarefas if t is not atual] + list(self._reassinaturas.values())
        for tarefa in tarefas:
            tarefa.cancel()
        await asyncio.gather(*tarefas, return_exceptions=True)
        conexao, self._conexao = self._conexao, None
        if conexao is not None:
            with contextlib.suppress(Exception):
                await conexao.fechar()
        await self._cliente.close()

    # --- apoio ---------------------------------------------------------------

    def _canal_do_livro(self, simbolo: str) -> str | None:
        """O canal do livro do simbolo ("order_book/0"), ou None se o mercado nao esta (mais) na lista."""
        ident = self._id_do_simbolo.get(simbolo)
        return None if ident is None else f"order_book/{ident}"

    def _indexar_se_preciso(self) -> None:
        """Refaz o indice se a lista de mercados do cliente mudou (primeira vez ou recarga)."""
        markets = getattr(self._cliente, "markets", None)
        if markets is not self._markets_indexados:
            self._indexar(markets or {})
            self._markets_indexados = markets

    def _indexar(self, markets: dict) -> None:
        """Escolhe o simbolo de cada ("futuro", ativo) (mesma regra do ColetorCcxt) e passa a taxa ao DadosLentos."""
        melhores: dict[tuple[str, str], tuple[tuple, str, ParNormalizado]] = {}
        ids: dict[str, str] = {}
        for simbolo, market in markets.items():
            try:
                ident = str(market["id"])
                par = normalizar(self.nome, market)
                ordem = (*prioridade_cotacao(par.cotacao), simbolo)
            except Exception as erro:  # market malformado: so ele fica de fora
                if simbolo not in self._avisados:
                    self._avisados.add(simbolo)
                    registro.warning("%s %s: mercado com dados invalidos (%s: %s); este par fica de fora",
                                     self.nome, simbolo, type(erro).__name__, erro)
                continue
            if market.get("active") is False:
                continue
            ids[ident] = simbolo
            chave = ("futuro", par.ativo)
            if chave not in melhores or ordem < melhores[chave][0]:
                melhores[chave] = (ordem, simbolo, par)
        self._simbolo_do_id = ids
        self._id_do_simbolo = {simbolo: ident for ident, simbolo in ids.items()}
        self._vencedor_da_chave = {chave: simbolo for chave, (_, simbolo, _) in melhores.items()}
        self._pares = {simbolo: par for _, simbolo, par in melhores.values()}
        # Mercado que saiu da lista (recarga): o livro dele sai do estado e deixa de ser desejado. A assinatura
        # na Lighter morre com a conexao; as mensagens dele sao ignoradas (o market_id nao esta mais no indice).
        for simbolo in sorted(self._livros_desejados - set(self._pares)):
            self._livros_desejados.discard(simbolo)
            self._largar_livro(simbolo)
        if self._dados_lentos is not None:
            self._dados_lentos.receber_taxas(self.nome, markets)

    def _remover_livro_do_estado(self, simbolo: str) -> None:
        ativo = self._livros_no_estado.pop(simbolo, None)
        if ativo is not None and self._estado is not None:
            self._estado.remover_livro(self.nome, "futuro", ativo, simbolo_original=simbolo)

    def _registrar_mensagem(self, agora: float) -> None:
        if self._estado is not None:
            self._estado.registrar_mensagem(self.nome, agora)

    def _registrar_atraso(self, agora: float, horario_da_corretora: float) -> None:
        if self._estado is not None:
            self._estado.registrar_atraso(self.nome, agora - horario_da_corretora, agora)

    def _chegou_ticker(self, agora: float) -> None:
        """Chegou um market_stats: se a corretora estava fora, conta a reconexao (o estado so conta uma vez)."""
        self._ticker_desde_o_inicio = True
        if self._estado is not None:
            self._estado.marcar_reconectada(self.nome)
        if self._caiu_em is not None:
            fora_s = agora - self._caiu_em
            self._caiu_em = None
            registro.info("%s: tickers de volta depois de %.1fs fora", self.nome, fora_s)

    def _queda(self, erro: Exception, espera: float, tentativa: int) -> None:
        """A conexao caiu: o estado esquece a corretora e o log registra a queda e a espera."""
        if self._caiu_em is None:
            self._caiu_em = self._relogio()
        if self._estado is not None:
            self._estado.marcar_desconectada(self.nome)
        self._livros_no_estado.clear()  # o estado ja apagou os livros da corretora
        dica = DICA_LIMITE if parece_limite_de_assinatura(erro) else ""
        # Erro que nao e de conexao pode ser defeito do proprio scanner: traceback na primeira vez.
        com_traceback = not _erro_de_conexao(erro) and not self._queda_com_traceback
        if com_traceback:
            self._queda_com_traceback = True
        registro.warning("%s: a conexao da lighter caiu (%s: %s); tentando de novo em %gs (tentativa %d).%s",
                         self.nome, type(erro).__name__, erro, espera, tentativa, dica,
                         exc_info=erro if com_traceback else False)

    def _erro_do_servidor(self, erro) -> None:
        """Mensagem de erro da Lighter ({"error": {"code", "message"}}): vai ao log, uma vez por codigo."""
        codigo = _inteiro(erro.get("code")) if isinstance(erro, dict) else None
        texto = str(erro.get("message")) if isinstance(erro, dict) else str(erro)
        if codigo in ERROS_INOFENSIVOS:
            registro.debug("%s: aviso da Lighter sem problema (codigo %s: %s)", self.nome, codigo, texto)
            return
        chave = codigo if codigo is not None else texto[:80]
        if chave in self._erros_avisados:
            registro.debug("%s: erro da Lighter de novo (codigo %s: %s)", self.nome, codigo, texto)
            return
        self._erros_avisados.add(chave)
        dica = DICA_LIMITE if parece_limite_de_assinatura(Exception(texto)) else ""
        registro.warning("%s: a Lighter respondeu com erro (codigo %s: %s); os proximos com este codigo so aparecem "
                         "no log detalhado.%s", self.nome, codigo, texto, dica)

    def _erro_ao_tratar(self, mensagem: dict) -> None:
        """Log do erro ao tratar uma mensagem: com traceback na primeira vez do canal; depois so no log detalhado."""
        canal = str(mensagem.get("channel") or mensagem.get("type") or "?").partition(":")[0]
        if canal in self._traceback_tratar:
            registro.debug("%s: erro ao tratar uma mensagem do canal %s de novo; descartada", self.nome, canal)
            return
        self._traceback_tratar.add(canal)
        registro.warning("%s: erro ao tratar uma mensagem do canal %s; ela foi descartada e a conexao segue (os "
                         "proximos erros deste canal so aparecem no log detalhado)", self.nome, canal, exc_info=True)

    def _erro_ao_tratar_ticker(self, simbolo: str) -> None:
        if simbolo in self._traceback_ticker:
            registro.debug("%s %s: erro ao tratar o ticker de novo; descartado", self.nome, simbolo)
            return
        self._traceback_ticker.add(simbolo)
        registro.warning("%s %s: erro ao tratar o ticker; so ele foi descartado e o resto segue (os proximos erros "
                         "deste par so aparecem no log detalhado)", self.nome, simbolo, exc_info=True)


def criar_coletor_lighter(nome: str, cliente, cfg: Config, estado: EstadoMercados | None, dados_lentos,
                          relogio: Callable[[], float] = time.time) -> ColetorLighter:
    """O ColetorLighter da corretora `nome` ("lighter" ou "lighter_rh") com os ajustes da Config."""
    if not e_lighter(nome):
        raise ValueError(f"'{nome}' nao e uma corretora da Lighter (use lighter ou lighter_rh)")
    return ColetorLighter(
        CORRETORAS_LIGHTER[nome], cliente, estado=estado, relogio=relogio, dados_lentos=dados_lentos,
        espera_max_reconexao_s=cfg.espera_max_reconexao_s, limite_assinaturas=cfg.limites_assinatura.get(nome),
        intervalo_ping_s=cfg.intervalo_ping_lighter_s, mensagens_sem_bid_ask=cfg.mensagens_sem_bid_ask,
    )
