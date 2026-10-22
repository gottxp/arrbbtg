"""Conector proprio da Lighter (especificacao, secao 6): a principal (cota em USDC) e a Lighter Robinhood (USDG).

A Lighter nao passa pelo ccxt: o ccxt nao cobre a Lighter Robinhood, e a especificacao pede um conector
proprio usando a API publica dela. A conversa e pelo cliente HTTP/WebSocket do aiohttp (vem com o ccxt).
A leitura das mensagens e a juncao do livro (funcoes puras) ficam em scanner/coletores/lighter_formatos.py;
aqui ficam o cliente (REST + WebSocket) e o coletor. A saude, as quedas, o limite de assinaturas e os
descartes sao os mesmos do ColetorCcxt (ColetorComSaude, em base.py).

Como funciona (documentacao oficial: https://apidocs.lighter.xyz, lida em 22/10/2026):
  - Mercados (REST, GET /api/v1/orderBookDetails): so os perpetuos com status "active", no estilo do ccxt
    ("ETH/USDC:USDC" na principal, "ETH/USDG:USDG" na Robinhood; "1000PEPE" vira PEPE x1000, como na MEXC).
  - Uma conexao WebSocket so por instancia, sempre so leitura (?readonly=true): o scanner nunca opera, e
    assim funciona tambem a partir de regiao restrita.
  - Etapa 1 (tickers): o canal market_stats/all traz o melhor bid/ask, o volume do dia, a variacao e o
    funding. Se ele so trouxer mercados sem bid/ask (como as amostras reais do ccxt de nov/2025), a etapa 1
    passa a vir dos LIVROS: o coletor assina o order_book de cada par da lista (dentro de
    limites_assinatura, pelo volume) e o melhor bid/ask de cada livro vira o ticker; o volume e a variacao
    continuam vindo do market_stats. Se o bid/ask voltar no market_stats, a etapa 1 volta a vir dele.
  - Etapa 2 (livros): order_book/{market_id}. Livro completo ("subscribed/order_book") e mudancas
    ("update/order_book"), juntadas no livro guardado; begin_nonce diferente do nonce anterior = sequencia
    quebrada: o livro sai do estado e e pedido de novo (cancelar e assinar). Livro completo que nao chega
    (tempo_max_livro_completo_lighter_s, ou a Lighter respondendo "ja assinado"/"nao assinado") tambem e
    pedido de novo. Repetindo sem nenhuma mudanca conferida no meio, a espera cresce (1s, 2s, 4s...).
  - Heartbeat: {"type": "ping"} a cada intervalo_ping_lighter_s; sem nenhuma mensagem por
    pings_sem_resposta_lighter intervalos, a conexao e dada como morta e o coletor reconecta.
  - Funding: o current_funding_rate do market_stats, ja em % (a Lighter cobra de hora em hora). Funding e
    taxa taker vao direto para o DadosLentos (receber_funding e receber_taxas).

O formato das mensagens segue a documentacao (as fixtures em tests/dados/ tambem); precisa ser conferido com
uma mensagem real no PC do dono.
"""

import asyncio
import contextlib
import json
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import aiohttp

from scanner.coletores.base import DICA_LIMITE, ColetorComSaude
from scanner.coletores.lighter_formatos import (
    CANAL_TICKERS,
    COTACAO_DA_INSTANCIA,
    CORRETORAS_LIGHTER,
    ERROS_INOFENSIVOS,
    IGNORADA,
    INTERVALO_FUNDING_LIGHTER_H,
    INVALIDA,
    JA_ASSINADO,
    NOME_DA_INSTANCIA,
    QUEBRADA,
    SEM_NONCE,
    ErroApiLighter,
    LivroJuntado,
    e_lighter,
    endereco_somente_leitura,
    itens_do_market_stats,
    ler_inteiro,
    ler_numero,
    livro_de_lighter,
    livro_do_erro,
    markets_de_lighter,
    nome_da_instancia,
    ticker_de_lighter,
)
from scanner.coletores.reconexao import espera_reconexao, parece_limite_de_assinatura
from scanner.config.carregar import Config
from scanner.estado.mercados import EstadoMercados
from scanner.modelos import Livro, Ticker
from scanner.normalizador import ParNormalizado, converter_livro, normalizar, prioridade_cotacao

# Nomes que outros modulos e os testes importam daqui (os formatos moram em lighter_formatos.py).
__all__ = [
    "CORRETORAS_LIGHTER", "INTERVALO_FUNDING_LIGHTER_H", "ClienteLighter", "ColetorLighter", "ConexaoFechada",
    "ErroApiLighter", "criar_cliente_lighter", "criar_coletor_lighter", "e_lighter", "endereco_somente_leitura",
    "livro_de_lighter", "markets_de_lighter", "ticker_de_lighter",
]

registro = logging.getLogger("scanner.coletores")


class ConexaoFechada(ConnectionError):
    """A conexao WebSocket da Lighter fechou (ou ficou muda)."""


# ---------------------------------------------------------------------------
# Rede (aiohttp)
# ---------------------------------------------------------------------------


def _contexto_ssl() -> ssl.SSLContext:
    """Os certificados do certifi, como o ccxt usa; sem ele, os do sistema.

    O certifi nao e uma biblioteca a mais: e dependencia do proprio ccxt (vem instalado com ele), e o ccxt
    monta o contexto SSL do aiohttp exatamente assim. Usar a mesma lista de certificados faz a Lighter
    conectar nos mesmos PCs em que as corretoras do ccxt conectam.
    """
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
    sempre aberto so leitura (?readonly=true). tempo_max_s: tempo_max_rede_lighter_s da Config.
    """

    def __init__(self, instancia: Literal["principal", "robinhood"], url_rest: str, url_ws: str,
                 tempo_max_s: float = Config.tempo_max_rede_lighter_s):
        nome_da_instancia(instancia)
        self.instancia = instancia
        self.cotacao = COTACAO_DA_INSTANCIA[instancia]
        self.url_rest = url_rest.rstrip("/")
        self.url_ws = endereco_somente_leitura(url_ws)
        self.tempo_max_s = tempo_max_s
        self.markets: dict = {}  # {simbolo: market} (veja markets_de_lighter); vazio ate o primeiro load_markets
        self.has: dict = {}
        self._sessao: aiohttp.ClientSession | None = None
        self._inativos_no_log: int | None = None

    def _sessao_http(self) -> aiohttp.ClientSession:
        if self._sessao is None or self._sessao.closed:
            tempo = aiohttp.ClientTimeout(total=None, connect=self.tempo_max_s, sock_connect=self.tempo_max_s)
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
        async with self._sessao_http().get(url, timeout=aiohttp.ClientTimeout(total=self.tempo_max_s)) as resposta:
            texto = await resposta.text()
            if resposta.status != 200:
                raise ErroApiLighter(f"orderBookDetails respondeu HTTP {resposta.status}: {texto[:200]}")
        try:
            dados = json.loads(texto)
        except ValueError as erro:
            raise ErroApiLighter(f"orderBookDetails nao respondeu JSON ({erro})") from erro
        codigo = ler_inteiro(dados.get("code")) if isinstance(dados, dict) else None
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
        async with asyncio.timeout(self.tempo_max_s):
            ws = await self._sessao_http().ws_connect(
                self.url_ws, autoping=True, heartbeat=None, timeout=aiohttp.ClientWSTimeout(ws_close=self.tempo_max_s))
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
        return ClienteLighter(instancia, cfg.url_rest_lighter, cfg.url_ws_lighter, cfg.tempo_max_rede_lighter_s)
    if instancia == "robinhood":
        return ClienteLighter(instancia, cfg.url_rest_lighter_rh, cfg.url_ws_lighter_rh, cfg.tempo_max_rede_lighter_s)
    raise ValueError(f"'{nome}' nao e uma corretora da Lighter (use lighter ou lighter_rh)")


# ---------------------------------------------------------------------------
# Coletor
# ---------------------------------------------------------------------------


@dataclass
class _LivroAberto:
    """Um livro assinado na conexao atual: o livro juntado e o controle dos pedidos de livro completo."""

    juntado: LivroJuntado = field(default_factory=LivroJuntado)
    quebras: int = 0  # quebras seguidas sem nenhuma mudanca conferida entre elas (espera crescente)
    espera: float = 0.0  # espera antes de assinar de novo depois da ultima quebra
    cancelar: bool = True  # cancelar antes de assinar de novo (False: a Lighter disse que nao estava assinado)
    mudancas_sem_completo: int = 0  # mudancas que chegaram antes do livro completo (para avisar no log)
    pedido_em: float | None = None  # quando o livro foi pedido (tempo maximo do livro completo); None = esperando


class ColetorLighter(ColetorComSaude):
    """Acompanha uma instancia da Lighter: tickers (market_stats/all ou livros) e livros (order_book) numa conexao.

    Oferece ao __main__ o mesmo que o ColetorCcxt (veja ColetorDoScanner em base.py).

    instancia: "principal" (corretora "lighter", USDC) ou "robinhood" ("lighter_rh", USDG).
    cliente: o ClienteLighter (ou um falso nos testes): `markets` e `conectar_ws()`.
    estado: EstadoMercados opcional; com ele cada mensagem registra a conexao viva e cada ticker/livro valido
        vai para la, com o ativo normalizado e o preco de 1 unidade do ativo.
    dados_lentos: o DadosLentos que recebe o funding (market_stats) e a taxa taker (orderBookDetails).
    relogio: hora atual em segundos (os testes passam um relogio falso).
    esperar: espera da reconexao e do livro pedido de novo (padrao asyncio.sleep; os testes trocam).
    esperar_ping: espera entre dois pings (separada, para os testes controlarem o ping).
    Os outros vem da Config: espera_max_reconexao_s, limites_assinatura, intervalo_ping_lighter_s,
    mensagens_sem_bid_ask, pings_sem_resposta_lighter, niveis_livro_lighter,
    tempo_max_livro_completo_lighter_s e mudancas_sem_livro_completo_lighter.

    A conexao vive enquanto manter_tickers roda: se ele for reiniciado (lista de pares mudou), a conexao e
    refeita e os livros desejados sao assinados de novo (cada um com um livro completo novo).
    """

    _TEXTO_DA_CONEXAO = "a conexao da lighter"
    _ERROS_DE_CONEXAO = (aiohttp.ClientError, OSError)

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
        pings_sem_resposta: int = Config.pings_sem_resposta_lighter,
        niveis_por_lado: int = Config.niveis_livro_lighter,
        tempo_max_livro_completo_s: float = Config.tempo_max_livro_completo_lighter_s,
        mudancas_sem_livro_completo: int = Config.mudancas_sem_livro_completo_lighter,
    ):
        super().__init__(nome_da_instancia(instancia), estado, relogio, limite_assinaturas, espera_max_reconexao_s)
        self.instancia = instancia
        self._cliente = cliente
        self._dados_lentos = dados_lentos
        self._esperar = esperar
        self._esperar_ping = esperar_ping
        self._intervalo_ping_s = intervalo_ping_s
        self._silencio_max_s = intervalo_ping_s * pings_sem_resposta
        self._max_sem_bid_ask = mensagens_sem_bid_ask
        self._niveis = niveis_por_lado
        self._tempo_max_completo_s = tempo_max_livro_completo_s
        self._max_sem_completo = mudancas_sem_livro_completo
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
        self._pedidos_etapa2: set[str] = set()  # livros que a etapa 2 pediu
        self._livros_etapa1: set[str] = set()  # livros que sao a etapa 1 (so com a etapa 1 pelos livros)
        self._livros_desejados: set[str] = set()  # os dois juntos, dentro do limite (sobrevive a quedas)
        self._livros: dict[str, _LivroAberto] = {}  # assinados na conexao atual
        self._reassinaturas: dict[str, asyncio.Task] = {}  # simbolo -> nova assinatura esperando a espera
        self._reassinar_pendentes: list[str] = []  # livros a pedir de novo depois da mensagem em tratamento
        self._ajuste_pendente = False  # a lista de livros mudou (troca da etapa 1): assinar/cancelar a diferenca
        self._volumes_conhecidos: dict[str, float] = {}  # simbolo -> volume 24h (do market_stats)
        self._estatisticas: dict[str, tuple[float | None, float | None]] = {}  # simbolo -> (volume, variacao)
        # Etapa 1 pelos livros (quando o market_stats nao traz bid/ask).
        self._tickers_pelos_livros = False
        self._tickers_do_livro: set[str] = set()  # simbolos cujo ticker no estado veio do livro
        self._seguidas_sem_bid_ask = 0
        self._pares_sem_bid_ask: set[str] = set()
        # Log.
        self._avisados: set[str] = set()
        self._erros_avisados: set = set()
        self._traceback_tratar: set[str] = set()
        self._avisou_sem_nonce = False
        self._sem_completo_avisado: set[str] = set()  # simbolos ja avisados de mudancas sem o livro completo

    # --- etapa 1 e conexao ---------------------------------------------------

    async def manter_tickers(self, simbolos: list[str], ao_ticker: Callable[[Ticker], None] | None = None) -> None:
        """Etapa 1 com reconexao: abre a conexao, acompanha os tickers de `simbolos` e volta sozinho a cada queda.

        Queda (rede, a Lighter fechou, conexao muda): o estado esquece a corretora (marcar_desconectada), o painel
        mostra "reconectando" e o coletor espera 1s, 2s, 4s... (ate espera_max_reconexao_s). O primeiro
        ticker depois da queda conta a reconexao, e a proxima queda volta a esperar 1s. Os livros desejados
        sao assinados de novo na conexao nova. Cancelamento (Ctrl+C, fechar()) sobe para quem chamou.
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
                    self._queda_da_conexao(erro, espera, tentativa)
                    await self._esperar(espera)
        finally:
            self._tarefas.discard(tarefa)

    async def _conectar_e_ler(self, simbolos_ticker: list[str]) -> None:
        """Uma conexao: abre, assina o market_stats/all e os livros desejados, e le ate cair (levanta o erro)."""
        self._simbolos_ticker = set(simbolos_ticker)
        if self._tickers_pelos_livros:
            self._livros_etapa1 = self._conhecidos(self._simbolos_ticker, avisar=False)
        self._livros_desejados = self._livros_dentro_do_limite(self._pedidos_etapa2 | self._livros_etapa1)
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
            self._reassinar_pendentes.clear()
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
        """Le e trata as mensagens ate a conexao cair (ou ficar muda por pings_sem_resposta intervalos de ping)."""
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
            elif mensagem.get("type") == "ping":  # o servidor tambem pode pingar: responde
                await conexao.enviar({"type": "pong"})
            else:
                try:
                    self._tratar(mensagem, agora)
                except Exception:  # defeito ao tratar esta mensagem: so ela fica de fora
                    self._erro_ao_tratar(mensagem)
            self._conferir_livros_atrasados(agora)
            await self._fazer_pendentes(conexao)

    async def _fazer_pendentes(self, conexao) -> None:
        """Depois de cada mensagem: ajusta a lista de livros (troca da etapa 1) e pede de novo os livros quebrados."""
        if self._ajuste_pendente:
            self._ajuste_pendente = False
            await self._ajustar_livros()
        while self._reassinar_pendentes:
            await self._reassinar(conexao, self._reassinar_pendentes.pop(0))

    def _tratar(self, mensagem: dict, agora: float) -> None:
        """Trata uma mensagem (o que precisa mandar algo a Lighter fica nos pendentes)."""
        if "error" in mensagem:
            self._erro_do_servidor(mensagem.get("error"))
            return
        canal = mensagem.get("channel")
        if not isinstance(canal, str):
            return  # "connected", "pong", "unsubscribed"...: so contam como mensagem
        if canal.startswith("market_stats:"):
            self._tratar_estatisticas(mensagem, agora)
        elif canal.startswith("order_book:"):
            self._tratar_livro(canal.partition(":")[2], mensagem, agora)

    def _tratar_estatisticas(self, mensagem: dict, agora: float) -> None:
        """market_stats: ticker de cada par da lista (se trouxer bid/ask), funding, volume e variacao de todos."""
        itens = itens_do_market_stats(mensagem)
        if not itens:
            return
        self._chegou_ticker(agora)
        self._indexar_se_preciso()
        total = com_bid_ask = 0
        sem_bid_ask: list[str] = []
        for stats in itens:
            simbolo = self._simbolo_do_id.get(str(stats.get("market_id")))
            if simbolo is None:
                continue  # mercado fora do indice (inativo, ou novo antes da recarga da lista)
            self._guardar_estatisticas(simbolo, stats)
            if simbolo not in self._simbolos_ticker and simbolo not in self._simbolos_cotacao:
                continue
            total += 1
            try:
                ticker = ticker_de_lighter(self.nome, simbolo, stats, agora)
                if ticker is not None or (ler_numero(stats.get("best_bid_price")) is not None
                                          and ler_numero(stats.get("best_ask_price")) is not None):
                    com_bid_ask += 1
                else:
                    sem_bid_ask.append(simbolo)
                if ticker is None:
                    if not self._tickers_pelos_livros:  # com a etapa 1 pelos livros, sem bid/ask aqui e o normal
                        self._descartes["tickers"] += 1
                        registro.debug("%s %s: ticker descartado (sem bid/ask, preco invalido ou cruzado)",
                                       self.nome, simbolo)
                    continue
                self._entregar_ticker(ticker)
            except Exception:  # defeito ao tratar este ticker: so ele fica de fora
                self._descartes["tickers"] += 1
                self._erro_ao_tratar_ticker(simbolo)
        horario = ler_numero(mensagem.get("timestamp"))
        if horario is not None:
            self._registrar_atraso(agora, horario / 1000)  # a Lighter manda ms
        self._conferir_bid_ask(total, com_bid_ask, sem_bid_ask)

    def _guardar_estatisticas(self, simbolo: str, stats: dict) -> None:
        """Volume (limite de assinaturas e ticker pelos livros), variacao e funding de um mercado."""
        volume = ler_numero(stats.get("daily_quote_token_volume"))
        if volume is not None:
            self._volumes_conhecidos[simbolo] = volume
        self._estatisticas[simbolo] = (volume, ler_numero(stats.get("daily_price_change")))
        par = self._pares.get(simbolo)
        funding = ler_numero(stats.get("current_funding_rate"))  # ja em %, por hora (a proxima cobranca)
        if par is not None and funding is not None and self._dados_lentos is not None:
            self._dados_lentos.receber_funding(self.nome, par.ativo, funding, INTERVALO_FUNDING_LIGHTER_H)

    def _conferir_bid_ask(self, total: int, com_bid_ask: int, sem_bid_ask: list[str]) -> None:
        """Troca a etapa 1 para os livros (ou de volta) conforme o market_stats traz bid/ask ou nao.

        Troca para os livros: mensagens_sem_bid_ask mensagens seguidas em que nenhum par da lista veio com
        bid/ask, de pelo menos min(mensagens_sem_bid_ask, pares da lista) pares diferentes (um par iliquido
        repetindo nao troca a etapa 1 de todos, como na Task 15). Volta: basta um par com bid/ask.
        """
        if total == 0:
            return
        if com_bid_ask:
            self._seguidas_sem_bid_ask = 0
            self._pares_sem_bid_ask.clear()
            if self._tickers_pelos_livros:
                self._voltar_ao_market_stats()
            return
        if self._tickers_pelos_livros:
            return
        self._seguidas_sem_bid_ask += 1
        self._pares_sem_bid_ask.update(sem_bid_ask)
        exigidos = min(self._max_sem_bid_ask, len(self._simbolos_ticker | self._simbolos_cotacao))
        if self._seguidas_sem_bid_ask >= self._max_sem_bid_ask and len(self._pares_sem_bid_ask) >= exigidos:
            self._passar_aos_livros()

    def _passar_aos_livros(self) -> None:
        self._tickers_pelos_livros = True
        self._livros_etapa1 = self._conhecidos(self._simbolos_ticker, avisar=False)
        self._ajuste_pendente = True
        registro.warning("%s: o market_stats nao traz best_bid_price/best_ask_price (%d mensagens seguidas, %d "
                         "pares): a etapa 1 passa a vir dos livros (order_book de cada par da lista, dentro de "
                         "limites_assinatura); o volume e a variacao continuam vindo do market_stats. Confira o "
                         "formato com uma mensagem real", self.nome, self._seguidas_sem_bid_ask,
                         len(self._pares_sem_bid_ask))

    def _voltar_ao_market_stats(self) -> None:
        self._tickers_pelos_livros = False
        self._livros_etapa1 = set()
        self._tickers_do_livro.clear()  # os tickers agora vem do market_stats: cancelar o livro nao os apaga
        self._ajuste_pendente = True
        registro.info("%s: o market_stats voltou a trazer bid/ask: a etapa 1 volta a vir dele (os livros que so "
                      "serviam a etapa 1 sao cancelados)", self.nome)

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

    def _ticker_do_livro(self, livro: Livro, agora: float) -> None:
        """Etapa 1 pelos livros: o melhor bid/ask do livro vira o ticker (volume e variacao do market_stats)."""
        volume, variacao = self._estatisticas.get(livro.simbolo_original, (None, None))
        self._chegou_ticker(agora)
        self._tickers_do_livro.add(livro.simbolo_original)
        self._entregar_ticker(Ticker(corretora=self.nome, mercado="futuro", simbolo_original=livro.simbolo_original,
                                     bid=livro.bids[0][0], ask=livro.asks[0][0], recebido_em=agora,
                                     volume_24h_usd=volume, variacao_24h_pct=variacao))

    def _esquecer_ticker_do_livro(self, simbolo: str) -> None:
        """O livro de onde vinha o ticker parou: o ticker sai do estado tambem (nao vale mais)."""
        if simbolo not in self._tickers_do_livro:
            return
        self._tickers_do_livro.discard(simbolo)
        par = self._pares.get(simbolo)
        if par is not None and self._estado is not None:
            self._estado.remover_ticker(self.nome, "futuro", par.ativo, simbolo_original=simbolo)

    # --- etapa 2: livros -----------------------------------------------------

    async def iniciar(self, simbolos: list[str], ao_atualizar: Callable[[Livro], None]) -> None:
        """Modo simples (imprimir_precos): acompanha o livro dos simbolos e entrega cada Livro cru ao callback.

        Sem reconexao: se a conexao cair, o erro sobe para quem chamou. Ao terminar (erro ou cancelamento),
        a conexao e o cliente sao sempre fechados.
        """
        self._ao_atualizar = ao_atualizar
        try:
            self._pedidos_etapa2 = self._conhecidos(simbolos)
            await self._conectar_e_ler([])
        finally:
            await self.fechar()

    async def assinar_livros(self, simbolos: set[str], ao_atualizar: Callable[[Livro], None] | None = None) -> None:
        """Etapa 2: deixa o livro aberto exatamente para `simbolos` (mais os da etapa 1, se ela vier dos livros).

        Assina os novos primeiro e cancela ("unsubscribe") os que sairam; o livro que sai deixa o estado na
        hora. Sem conexao agora, so anota: a proxima conexao assina os desejados. Pode ser chamada a cada ciclo.
        Com limites_assinatura, ficam os de maior volume 24h.
        ao_atualizar: recebe o Livro cru (guardado; so precisa vir na primeira vez). Sem ele, precisa de estado.
        """
        if ao_atualizar is not None:
            self._ao_atualizar = ao_atualizar
        if self._ao_atualizar is None and self._estado is None:
            raise ValueError("assinar_livros precisa de um ao_atualizar ou de um estado para guardar os livros")
        if self._fechado:
            return
        self._pedidos_etapa2 = self._conhecidos(simbolos)
        await self._ajustar_livros()

    async def _ajustar_livros(self) -> None:
        """Recalcula os livros desejados (etapa 2 + etapa 1 pelos livros, no limite) e assina/cancela a diferenca."""
        desejados = self._livros_dentro_do_limite(self._pedidos_etapa2 | self._livros_etapa1)
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

    def _conhecidos(self, simbolos, avisar: bool = True) -> set[str]:
        """Os simbolos que a Lighter tem (e que vao ao estado); os outros avisam no log (uma vez cada)."""
        self._indexar_se_preciso()
        conhecidos = set()
        for simbolo in simbolos:
            if simbolo in self._id_do_simbolo and simbolo in self._pares:
                conhecidos.add(simbolo)
            elif avisar and simbolo not in self._avisados:
                self._avisados.add(simbolo)
                registro.warning("%s %s: par que a %s nao tem (ou inativo); o livro dele nao e assinado",
                                 self.nome, simbolo, self.nome)
        return conhecidos

    def _volume_conhecido(self, simbolo: str) -> float | None:
        return self._volumes_conhecidos.get(simbolo)

    def _livros_abertos(self) -> set[str]:
        return set(self._livros_desejados)

    async def _assinar(self, conexao, simbolo: str) -> None:
        """Assina o livro do simbolo nesta conexao (se ainda for desejado e nao estiver assinado)."""
        canal = self._canal_do_livro(simbolo)
        if canal is None or simbolo in self._livros or simbolo not in self._livros_desejados:
            return
        aberto = self._livros[simbolo] = _LivroAberto()
        await self._enviar_assinatura(conexao, canal, aberto)

    async def _enviar_assinatura(self, conexao, canal: str, aberto: _LivroAberto) -> None:
        aberto.pedido_em = self._relogio()  # a partir daqui corre o tempo maximo do livro completo
        await conexao.enviar({"type": "subscribe", "channel": canal})

    def _largar_livro(self, simbolo: str) -> None:
        """Esquece o livro do simbolo nesta conexao e tira do estado o que ele tinha entregado."""
        tarefa = self._reassinaturas.pop(simbolo, None)
        if tarefa is not None:
            tarefa.cancel()
        if simbolo in self._reassinar_pendentes:
            self._reassinar_pendentes.remove(simbolo)
        self._livros.pop(simbolo, None)
        self._remover_livro_do_estado(simbolo)
        self._esquecer_ticker_do_livro(simbolo)

    def _tratar_livro(self, ident: str, mensagem: dict, agora: float) -> None:
        """order_book: livro completo ou mudancas (veja LivroJuntado em lighter_formatos.py)."""
        tipo = mensagem.get("type")
        if tipo not in ("subscribed/order_book", "update/order_book"):
            return  # ex.: "unsubscribed", a confirmacao do cancelamento
        simbolo = self._simbolo_do_id.get(ident)
        aberto = self._livros.get(simbolo) if simbolo is not None else None
        if aberto is None:
            return  # nao assinado (ou acabou de ser cancelado): mensagem atrasada, ignorada
        dados = mensagem.get("order_book")
        if not isinstance(dados, dict):
            self._descartes["livros"] += 1
            return
        if tipo == "subscribed/order_book":  # o livro completo (o primeiro depois de assinar)
            if not aberto.juntado.receber_completo(dados):
                self._quebrou(simbolo, aberto, "livro completo com nivel invalido", "livros")
                return
            aberto.mudancas_sem_completo = 0
        else:  # "update/order_book": so as mudancas
            resultado, detalhe = aberto.juntado.juntar_mudancas(dados)
            if resultado == IGNORADA:
                self._mudanca_sem_completo(simbolo, aberto)
                return
            if resultado in (QUEBRADA, INVALIDA):
                self._quebrou(simbolo, aberto, detalhe, "sequencia_quebrada" if resultado == QUEBRADA else "livros")
                return
            if resultado == SEM_NONCE:
                self._avisar_sem_nonce()
            else:
                aberto.quebras = 0  # a sequencia foi conferida: a proxima quebra pede o livro na hora
        self._entregar_juntado(simbolo, aberto, mensagem, agora)

    def _mudanca_sem_completo(self, simbolo: str, aberto: _LivroAberto) -> None:
        """Mudanca antes do livro completo: ignorada. Muitas seguidas (mudancas_sem_livro_completo) avisam uma vez.

        Depois de pedir o livro de novo, umas poucas mudancas velhas ainda chegam (normal). Muitas querem dizer
        que a Lighter nao manda o livro completo como "subscribed/order_book" (formato a conferir).
        """
        aberto.mudancas_sem_completo += 1
        registro.debug("%s %s: mudanca antes do livro completo; ignorada", self.nome, simbolo)
        if aberto.mudancas_sem_completo == self._max_sem_completo and simbolo not in self._sem_completo_avisado:
            self._sem_completo_avisado.add(simbolo)
            registro.warning("%s %s: %d mudancas de livro sem o livro completo (subscribed/order_book) antes: o livro "
                             "nao entra no scanner. Confira o formato com uma mensagem real", self.nome, simbolo,
                             aberto.mudancas_sem_completo)

    def _avisar_sem_nonce(self) -> None:
        if not self._avisou_sem_nonce:
            self._avisou_sem_nonce = True
            registro.warning("%s: mudanca de livro sem begin_nonce/nonce: a continuidade do livro NAO esta sendo "
                             "conferida (uma mudanca perdida nao seria percebida); as mudancas sao aplicadas assim "
                             "mesmo. Confira o formato com uma mensagem real", self.nome)

    def _quebrou(self, simbolo: str, aberto: _LivroAberto, motivo: str, tipo: str | None,
                 cancelar: bool = True) -> None:
        """O livro deixou de ser confiavel (ou nao chegou): sai do estado e um livro completo novo e pedido.

        tipo: o descarte contado ("sequencia_quebrada", "livros") ou None (o livro completo nao chegou).
        cancelar: cancelar antes de assinar de novo (False quando a Lighter disse que nao estava assinado).
        Pede na hora; se quebrar de novo sem nenhuma mudanca conferida no meio, espera 1s, 2s, 4s... (ate
        espera_max_reconexao_s) antes de pedir, para nao ficar pedindo sem parar.
        """
        if tipo is not None:
            self._descartes[tipo] += 1
        self._remover_livro_do_estado(simbolo)
        self._esquecer_ticker_do_livro(simbolo)
        aberto.juntado.esvaziar()
        aberto.mudancas_sem_completo = 0
        aberto.espera = 0.0 if aberto.quebras == 0 else espera_reconexao(aberto.quebras - 1, self._espera_max_s)
        aberto.quebras += 1
        aberto.cancelar = cancelar
        aberto.pedido_em = None  # o tempo maximo do livro completo recomeca quando o pedido sair
        quando = "agora" if aberto.espera <= 0 else f"em {aberto.espera:g}s"
        if tipo == "sequencia_quebrada":
            texto = "sequencia do livro quebrada (%s); o livro saiu do scanner e um livro completo novo sera pedido %s"
        elif tipo == "livros":
            texto = "livro com dado invalido (%s); o livro saiu do scanner e um livro completo novo sera pedido %s"
        else:
            texto = "%s; o livro completo sera pedido de novo %s"
        registro.warning("%s %s: " + texto, self.nome, simbolo, motivo, quando)
        if simbolo not in self._reassinar_pendentes:
            self._reassinar_pendentes.append(simbolo)

    def _conferir_livros_atrasados(self, agora: float) -> None:
        """Livro pedido ha mais de tempo_max_livro_completo_lighter_s sem o livro completo: pede de novo."""
        for simbolo, aberto in list(self._livros.items()):
            if aberto.juntado.pronto or aberto.pedido_em is None or simbolo in self._reassinaturas:
                continue
            if agora - aberto.pedido_em > self._tempo_max_completo_s:
                self._quebrou(simbolo, aberto, f"o livro completo nao chegou em {self._tempo_max_completo_s:g}s", None)

    async def _reassinar(self, conexao, simbolo: str) -> None:
        """Pede o livro de novo: cancela (se for o caso) e assina, na hora ou depois da espera."""
        aberto = self._livros.get(simbolo)
        canal = self._canal_do_livro(simbolo)
        if aberto is None or canal is None or conexao is not self._conexao:
            return
        if aberto.cancelar:  # a Lighter recusa assinar por cima de uma assinatura viva
            await conexao.enviar({"type": "unsubscribe", "channel": canal})
        if aberto.espera <= 0:
            await self._enviar_assinatura(conexao, canal, aberto)
            return
        anterior = self._reassinaturas.pop(simbolo, None)
        if anterior is not None:
            anterior.cancel()
        self._reassinaturas[simbolo] = asyncio.create_task(self._assinar_depois(conexao, simbolo, aberto.espera),
                                                           name=f"livro {self.nome} {simbolo}")

    async def _assinar_depois(self, conexao, simbolo: str, espera: float) -> None:
        try:
            await self._esperar(espera)
            aberto, canal = self._livros.get(simbolo), self._canal_do_livro(simbolo)
            if conexao is self._conexao and aberto is not None and canal is not None and not self._fechado:
                await self._enviar_assinatura(conexao, canal, aberto)
        except Exception as erro:  # a conexao caiu: a proxima assina os desejados
            registro.debug("%s %s: nao consegui pedir o livro de novo (%s: %s)", self.nome, simbolo,
                           type(erro).__name__, erro)
        finally:
            if self._reassinaturas.get(simbolo) is asyncio.current_task():
                del self._reassinaturas[simbolo]

    def _entregar_juntado(self, simbolo: str, aberto: _LivroAberto, mensagem: dict, agora: float) -> None:
        """Monta o Livro guardado e entrega (se for valido); com a etapa 1 pelos livros, o ticker tambem."""
        pronto = aberto.juntado.montar(self.nome, simbolo, agora, ler_numero(mensagem.get("timestamp")), self._niveis)
        if pronto is None:
            self._descartes["livros"] += 1
            registro.debug("%s %s: livro descartado (vazio, preco invalido ou cruzado)", self.nome, simbolo)
            return
        if pronto.ts_corretora is not None:
            self._registrar_atraso(agora, pronto.ts_corretora)
        self._entregar_livro(pronto)
        if self._tickers_pelos_livros and simbolo in self._simbolos_ticker:
            self._ticker_do_livro(pronto, agora)

    def _entregar_livro(self, livro: Livro) -> None:
        if self._estado is not None:
            par = self._pares.get(livro.simbolo_original)
            if par is not None:
                self._estado.atualizar_livro(converter_livro(livro, par), ativo=par.ativo)
                self._livros_no_estado[livro.simbolo_original] = ("futuro", par.ativo)
        if self._ao_atualizar is not None:
            self._ao_atualizar(livro)

    # --- interface que o __main__ usa (veja ColetorDoScanner em base.py) --------

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
        validos = set(self._pares)
        self._pedidos_etapa2 &= validos
        self._livros_etapa1 &= validos
        for simbolo in sorted(self._livros_desejados - validos):
            self._livros_desejados.discard(simbolo)
            self._largar_livro(simbolo)
        if self._dados_lentos is not None:
            self._dados_lentos.receber_taxas(self.nome, markets)

    def _erro_do_servidor(self, erro) -> None:
        """Mensagem de erro da Lighter ({"error": {"code", "message"}}): vai ao log, uma vez por codigo.

        30003 (ja assinado) e 30002 (nao assinado) sao normais, salvo se citarem o livro de um par que ainda
        espera o livro completo: entao o livro e pedido de novo (o completo nao vira sozinho).
        """
        codigo = ler_inteiro(erro.get("code")) if isinstance(erro, dict) else None
        texto = str(erro.get("message")) if isinstance(erro, dict) else str(erro)
        if codigo in ERROS_INOFENSIVOS:
            registro.debug("%s: aviso da Lighter (codigo %s: %s)", self.nome, codigo, texto)
            self._livro_citado_no_aviso(codigo, texto)
            return
        chave = codigo if codigo is not None else texto[:80]
        if chave in self._erros_avisados:
            registro.debug("%s: erro da Lighter de novo (codigo %s: %s)", self.nome, codigo, texto)
            return
        self._erros_avisados.add(chave)
        dica = DICA_LIMITE if parece_limite_de_assinatura(Exception(texto)) else ""
        registro.warning("%s: a Lighter respondeu com erro (codigo %s: %s); os proximos com este codigo so aparecem "
                         "no log detalhado.%s", self.nome, codigo, texto, dica)

    def _livro_citado_no_aviso(self, codigo: int, texto: str) -> None:
        ident = livro_do_erro(texto)
        simbolo = self._simbolo_do_id.get(ident) if ident is not None else None
        aberto = self._livros.get(simbolo) if simbolo is not None else None
        if aberto is None or aberto.juntado.pronto or aberto.pedido_em is None or simbolo in self._reassinaturas:
            return
        ja_assinado = codigo == JA_ASSINADO
        motivo = ("a Lighter diz que o livro ja estava assinado" if ja_assinado
                  else "a Lighter diz que o livro nao estava assinado") + " e o livro completo nao chegou"
        self._quebrou(simbolo, aberto, motivo, None, cancelar=ja_assinado)

    def _erro_ao_tratar(self, mensagem: dict) -> None:
        """Log do erro ao tratar uma mensagem: com traceback na primeira vez do canal; depois so no log detalhado."""
        canal = str(mensagem.get("channel") or mensagem.get("type") or "?").partition(":")[0]
        if canal in self._traceback_tratar:
            registro.debug("%s: erro ao tratar uma mensagem do canal %s de novo; descartada", self.nome, canal)
            return
        self._traceback_tratar.add(canal)
        registro.warning("%s: erro ao tratar uma mensagem do canal %s; ela foi descartada e a conexao segue (os "
                         "proximos erros deste canal so aparecem no log detalhado)", self.nome, canal, exc_info=True)


def criar_coletor_lighter(nome: str, cliente, cfg: Config, estado: EstadoMercados | None, dados_lentos,
                          relogio: Callable[[], float] = time.time) -> ColetorLighter:
    """O ColetorLighter da corretora `nome` ("lighter" ou "lighter_rh") com os ajustes da Config."""
    if not e_lighter(nome):
        raise ValueError(f"'{nome}' nao e uma corretora da Lighter (use lighter ou lighter_rh)")
    return ColetorLighter(
        CORRETORAS_LIGHTER[nome], cliente, estado=estado, relogio=relogio, dados_lentos=dados_lentos,
        espera_max_reconexao_s=cfg.espera_max_reconexao_s, limite_assinaturas=cfg.limites_assinatura.get(nome),
        intervalo_ping_s=cfg.intervalo_ping_lighter_s, mensagens_sem_bid_ask=cfg.mensagens_sem_bid_ask,
        pings_sem_resposta=cfg.pings_sem_resposta_lighter, niveis_por_lado=cfg.niveis_livro_lighter,
        tempo_max_livro_completo_s=cfg.tempo_max_livro_completo_lighter_s,
        mudancas_sem_livro_completo=cfg.mudancas_sem_livro_completo_lighter,
    )
