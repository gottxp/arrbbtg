"""Coletor que usa o ccxt (versao "pro", com WebSocket) para ler tickers e livros de ofertas.

Duas etapas na mesma corretora:
  1. `iniciar_tickers`: melhor bid/ask de TODOS os pares, por um stream leve. Alem de
     achar os pares interessantes, o stream de tickers e o "heartbeat" da conexao.
  2. `assinar_livros`: livro completo so dos pares que a etapa 1 apontou. Quem sai da
     lista tem o livro cancelado.
Passando um `EstadoMercados`, o coletor guarda os dados la (ja normalizados) e avisa a
cada mensagem recebida que a conexao esta viva.

Robustez (especificacao, secao 8):
  - `manter_tickers` e a etapa 1 com reconexao: se o stream cair (rede, erro da corretora), o estado
    esquece a corretora (nenhum sinal usa os precos dela e os que dependiam dela sao encerrados), o
    painel mostra "reconectando" e o coletor tenta de novo esperando 1s, 2s, 4s... ate
    `espera_max_reconexao_s`. A primeira mensagem boa conta a reconexao e a espera volta a 1s.
  - Livro que cai tenta de novo sozinho, com a mesma espera crescente (cada simbolo a sua).
  - Limite de assinaturas (`limite_assinaturas`): ficam os pares de maior volume 24h.
  - Descartes (livro ou ticker invalido, sequencia do livro quebrada) sao contados; o __main__
    resume no log. Cada descarte aparece no log detalhado.
  - Atraso dos dados (chegada - horario informado pela corretora) vai para o estado (painel).

Fase 2 de corretoras (Task 15):
  - `mercados`: so os mercados da lista (mercados_por_corretora da Config) entram no scanner; a
    Hyperliquid, por exemplo, fica so com os futuros.
  - `metodo_ticker`: o metodo de ticker que a corretora tenta primeiro (metodo_ticker da Config).
  - Troca automatica: um metodo de ticker que so entrega tickers sem bid/ask (ex.: allMids da
    Hyperliquid) e trocado pelo proximo, com aviso no log; se nenhum servir, o log diz qual mercado
    ficou sem tickers. Nenhuma corretora fica sem tickers em silencio.
  - Simbolos de cotacao (`usar_para_cotacao`): o ticker deles tambem vai para o estado pelo simbolo
    (ex.: "USDC/USDT" da fonte_cotacao), de onde o DadosLentos tira as cotacoes.
"""

import asyncio
import logging
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Literal

import ccxt.pro as ccxtpro
from ccxt.base.errors import InvalidNonce, NetworkError, NotSupported

from scanner.coletores.base import DICA_LIMITE, ColetorComSaude
from scanner.coletores.reconexao import espera_reconexao, parece_limite_de_assinatura, selecionar_pares
from scanner.config.carregar import MERCADOS, METODOS_TICKER, Config
from scanner.estado.mercados import EstadoMercados
from scanner.modelos import Livro, Ticker
from scanner.normalizador import ParNormalizado, converter_livro, mercado_do_market, normalizar, prioridade_cotacao

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


def _numero_ou_none(valor) -> float | None:
    """Numero real (nao NaN, nao infinito) ou None."""
    if isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor):
        return float(valor)
    return None


def ticker_de_ccxt(
    corretora: str,
    mercado: Literal["spot", "futuro"],
    simbolo: str,
    dados: dict,
    agora: float,
) -> Ticker | None:
    """Transforma o ticker entregue pelo ccxt em um Ticker.

    Devolve None (ticker descartado) se faltar bid ou ask, se algum for zero ou
    negativo, ou se o bid for maior ou igual ao ask.
    """
    bid = _numero_ou_none(dados.get("bid"))
    ask = _numero_ou_none(dados.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or bid >= ask:
        return None
    return Ticker(
        corretora=corretora,
        mercado=mercado,
        simbolo_original=simbolo,
        bid=bid,
        ask=ask,
        recebido_em=agora,
        volume_24h_usd=_numero_ou_none(dados.get("quoteVolume")),  # na moeda de cotacao (USDT ~ USD)
        variacao_24h_pct=_numero_ou_none(dados.get("percentage")),  # o ccxt ja entrega em %
    )


def criar_exchange_ccxt(nome: str):
    """Monta o objeto da corretora no ccxt pro (ex.: "bitget"). Nao acessa a rede."""
    if nome not in ccxtpro.exchanges:
        raise ValueError(f"corretora '{nome}' nao existe no ccxt (nome com erro de digitacao?)")
    return getattr(ccxtpro, nome)({"enableRateLimit": True})


# TIPOS_DE_DESCARTE e DICA_LIMITE moram em scanner/coletores/base.py (comuns a todos os coletores).


def _erro_de_conexao(erro: BaseException) -> bool:
    """True para erro da conexao (rede caiu, tempo esgotado, corretora fora do ar ou bloqueando por excesso).

    NetworkError e a familia de erros de rede do ccxt (inclui tempo esgotado e limite de requisicoes);
    OSError cobre os erros de rede do proprio Python. Nesses casos o traceback nao ajuda em nada.
    """
    return isinstance(erro, (NetworkError, OSError))


def _agrupar_por_mercado(simbolos: list[str]) -> list[list[str]]:
    """Os simbolos separados em spot e futuro (o ccxt escolhe o tipo de mercado pelo primeiro da lista)."""
    grupos: dict[str, list[str]] = {"spot": [], "futuro": []}
    for simbolo in simbolos:
        grupos[mercado_do_simbolo(simbolo)].append(simbolo)
    return [grupo for grupo in grupos.values() if grupo]


# Metodos de ticker do ccxt, do preferido ao ultimo recurso: (metodo, chave em exchange.has).
# watch_tickers traz tambem volume e variacao; watch_bids_asks so o melhor bid/ask;
# watch_ticker pede um par por vez (uma assinatura por par). Os nomes sao os de METODOS_TICKER da Config,
# na mesma ordem (um teste confere).
METODOS_DE_TICKER = (
    ("watch_tickers", "watchTickers"),
    ("watch_bids_asks", "watchBidsAsks"),
    ("watch_ticker", "watchTicker"),
)

# Canal de ticker que o ccxt assina em cada corretora e mercado: (corretora, metodo) -> (opcao do ccxt, {mercado:
# canal}). E detalhe da API de cada corretora no ccxt (nao e ajuste do dono).
# XT (ccxt/pro/xt.py): os canais padrao 'tickers' e 'ticker' nao trazem bid/ask; nos contratos, 'agg_tickers' e
# 'agg_ticker' trazem (campos bp/ap). O canal vai pela opcao do ccxt (options["watchTickers"]["method"]) e nao
# pelo params do watch_*: no ccxt 4.5.89 o params "method" tambem troca o "method" da mensagem de assinatura
# (que precisa ser "SUBSCRIBE") e a XT nao assinaria nada. O ccxt le a opcao logo no comeco do watch_*, antes
# de esperar qualquer coisa: o coletor ajusta a opcao imediatamente antes de cada pedido, e assim o spot e o
# futuro (que rodam ao mesmo tempo) usam cada um o seu canal.
CANAIS_DE_TICKER = {
    ("xt", "watch_tickers"): ("watchTickers", {"spot": "tickers", "futuro": "agg_tickers"}),
    ("xt", "watch_ticker"): ("watchTicker", {"spot": "ticker", "futuro": "agg_ticker"}),
}


class SemBidAsk(NotSupported):
    """O metodo de ticker so entregou tickers sem bid/ask: nao serve para a etapa 1 naquele mercado."""


class _UltimoSemBidAsk(Exception):
    """Interno do coletor: o ultimo metodo de ticker que sobrou num mercado so trouxe tickers sem bid/ask.

    Nao e abandono: o mercado espera (espera crescente) e tenta de novo. `metodos`: os que vieram sem bid/ask.
    """

    def __init__(self, metodos: list[str]):
        super().__init__(", ".join(metodos))
        self.metodos = list(metodos)


class _ConferenciaBidAsk:
    """Diz quando um metodo de ticker deve ser abandonado por so trazer tickers sem bid/ask.

    Conta as mensagens seguidas em que NENHUM ticker trouxe bid e ask. Basta um ticker com bid e ask
    para o metodo provar que funciona: dai em diante ele nunca e abandonado (um par iliquido sem bid
    num momento e normal e nao pode derrubar o metodo de todos). Mensagem vazia nao conta.
    simbolos_no_mercado: no um a um (cada mensagem e de um par so), quantos pares o mercado tem; alem das
        `maximo` mensagens, elas precisam ter vindo de min(maximo, simbolos_no_mercado) pares diferentes
        (um par iliquido repetindo nao condena o metodo). 0 = lote (cada mensagem ja traz varios pares).
    """

    def __init__(self, maximo: int, simbolos_no_mercado: int = 0):
        self._maximo = maximo
        self._pares_exigidos = min(maximo, simbolos_no_mercado)
        self._seguidas = 0
        self._pares_sem_bid_ask: set[str] = set()
        self._funcionou = False

    def desistir(self, total: int, com_bid_ask: int, simbolo: str | None = None) -> bool:
        """Anota uma mensagem (`total` tickers, `com_bid_ask` deles com bid e ask). True = abandonar o metodo."""
        if self._funcionou or total == 0:
            return False
        if com_bid_ask:
            self._funcionou = True
            return False
        self._seguidas += 1
        if simbolo is not None:
            self._pares_sem_bid_ask.add(simbolo)
        return self._seguidas >= self._maximo and len(self._pares_sem_bid_ask) >= self._pares_exigidos


class ColetorCcxt(ColetorComSaude):
    """Acompanha uma corretora pelo ccxt: tickers (etapa 1) e livros de ofertas (etapa 2).

    exchange: o objeto da corretora do ccxt (veja criar_exchange_ccxt). Vem de fora
        para os testes usarem um objeto falso, sem internet.
    estado: EstadoMercados opcional. Com ele, cada mensagem recebida registra a conexao
        como viva e cada ticker/livro valido e guardado la, ja com o ativo normalizado e
        os precos por 1 unidade do ativo (mercados lidos de `exchange.markets`). So entram
        no estado o spot e o perpetuo linear (opcoes, futuros com vencimento e contratos
        inversos ficam de fora) e, se um ativo tem mais de uma moeda de cotacao no mesmo
        mercado, so a preferida (USDT, depois USDC, USDG): a chave do estado e unica.
    relogio: devolve a hora atual em segundos; os testes trocam por um relogio falso.
    timeout_desassinar_s: tempo maximo esperando a corretora confirmar o cancelamento de um
        livro (timeout_desassinar_s da Config); passou disso, avisa no log e segue.
    espera_max_reconexao_s: maior espera entre duas tentativas de reconexao (da Config).
    esperar: funcao que espera N segundos (padrao asyncio.sleep); os testes trocam por uma que
        so anota a espera, para nao dormir de verdade.
    limite_assinaturas: maximo de livros abertos ao mesmo tempo (e de tickers, se a corretora so
        assina um par por vez); None = sem limite. Vem de limites_assinatura da Config.
    mercados: os mercados usados ("spot", "futuro"); os outros nem entram no indice (mercados_por_corretora).
    metodo_ticker: o metodo de ticker tentado primeiro (metodo_ticker da Config); None = a ordem de
        METODOS_DE_TICKER. Os metodos antes dele nao sao tentados.
    mensagens_sem_bid_ask: mensagens seguidas so com tickers sem bid/ask para abandonar um metodo.

    Livro que deixa de ser acompanhado (saiu da lista ou a assinatura caiu) sai do estado na
    hora: o ultimo livro recebido nao pode continuar valendo como se fosse atual.

    Os callbacks (`ao_atualizar`, `ao_ticker`) recebem os dados CRUS, como a corretora
    entrega (ex.: preco de "1000PEPE" sem dividir por 1000); sao opcionais com `estado`.
    """

    _TEXTO_DA_CONEXAO = "a conexao dos tickers"
    _ERROS_DE_CONEXAO = (NetworkError, OSError)  # mesma regra de _erro_de_conexao

    def __init__(
        self,
        nome: str,
        exchange,
        estado: EstadoMercados | None = None,
        relogio: Callable[[], float] = time.time,
        timeout_desassinar_s: float = 5,
        espera_max_reconexao_s: float = Config.espera_max_reconexao_s,
        esperar: Callable[[float], Awaitable] = asyncio.sleep,
        limite_assinaturas: int | None = None,
        mercados: tuple[str, ...] = MERCADOS,
        metodo_ticker: str | None = None,
        mensagens_sem_bid_ask: int = Config.mensagens_sem_bid_ask,
    ):
        super().__init__(nome, estado, relogio, limite_assinaturas, espera_max_reconexao_s)
        if metodo_ticker is not None and metodo_ticker not in METODOS_TICKER:
            raise ValueError(f"metodo_ticker '{metodo_ticker}' invalido; use um destes: {', '.join(METODOS_TICKER)}")
        self._exchange = exchange
        self._ao_atualizar: Callable[[Livro], None] | None = None  # guardado para as tarefas de livro
        self._ao_ticker: Callable[[Ticker], None] | None = None
        self._tarefas_livro: dict[str, asyncio.Task] = {}  # simbolo -> tarefa do livro (etapa 2)
        self._markets_indexados = None  # o dict `exchange.markets` de onde saiu o indice abaixo
        self._pares: dict[str, ParNormalizado] = {}  # simbolo -> par, so dos que vao ao estado (um por chave)
        self._vencedor_da_chave: dict[tuple[str, str], str] = {}  # (mercado, ativo) -> simbolo que ocupa a chave
        self._avisados: set[str] = set()  # simbolos ja avisados no log (um aviso so por simbolo)
        self._sem_un_watch = False  # a corretora nao sabe cancelar assinatura de livro (avisado uma vez)
        self._timeout_desassinar_s = timeout_desassinar_s
        # Robustez (Task 14). Saude, quedas, limite e descartes: ColetorComSaude (base.py).
        self._esperar = esperar
        self._fechado = False  # fechar() foi chamado: nada novo comeca
        self._tarefas_ticker: set[asyncio.Task] = set()  # tarefas da etapa 1 em andamento (o fechar() para todas)
        self._livros_que_chegaram: set[str] = set()  # simbolos com livro bom desde a ultima falha
        self._traceback_livro: set[str] = set()  # simbolos cuja falha de livro ja saiu com traceback no log
        self._volumes_conhecidos: dict[str, float] = {}  # simbolo -> ultimo volume 24h visto (sobrevive a quedas)
        # Fase 2 (Task 15).
        self._mercados = tuple(mercados)
        self._metodo_ticker = metodo_ticker
        self._max_sem_bid_ask = mensagens_sem_bid_ask
        self._sem_bid_ask: set[tuple[str, str]] = set()  # (mercado, metodo) que so trouxe tickers sem bid/ask
        self._fora_no_um_a_um: dict[str, int] = {}  # mercado -> pares fora do limite no um a um da troca (ultimo aviso)
        self._avisou_metodo_ausente = False  # o metodo_ticker da config nao existe na corretora (avisado uma vez)
        self._erro_sem_bid_ask: set[str] = set()  # mercados cujo "nenhum metodo traz bid/ask" ja saiu em ERRO
        self._avisou_mercados_fora = False  # a quantidade de pares fora de `mercados` ja foi para o log
        self._simbolos_cotacao: set[str] = set()  # simbolos cujo ticker tambem alimenta as cotacoes

    # --- etapa 1: tickers ----------------------------------------------------

    async def manter_tickers(
        self, simbolos: list[str], ao_ticker: Callable[[Ticker], None] | None = None
    ) -> None:
        """Etapa 1 com reconexao: acompanha os tickers ate ser cancelado, voltando sozinho depois de cada queda.

        Queda (erro de rede, da corretora ou do ccxt): o estado esquece a corretora (marcar_desconectada),
        o painel mostra "reconectando" e o coletor espera espera_reconexao(tentativa) (1s, 2s, 4s... ate
        espera_max_reconexao_s) antes de tentar de novo. A primeira mensagem boa depois da queda conta a
        reconexao no estado, e a proxima queda volta a esperar 1s.
        Cancelamento (Ctrl+C, fechar()) nunca e tratado como queda: sobe para quem chamou.
        Corretora sem nenhum metodo de ticker no ccxt: tentar de novo nao adianta; avisa no log e termina.
        """
        tarefa = asyncio.current_task()
        self._tarefas_ticker.add(tarefa)
        tentativa = 0
        try:
            while not self._fechado:
                self._ticker_desde_o_inicio = False
                try:
                    await self.iniciar_tickers(simbolos, ao_ticker)
                    return  # so termina sozinho quando nao ha nenhum simbolo para acompanhar
                except NotSupported as erro:
                    registro.error("%s: %s; a corretora fica sem tickers (e sem sinais)", self.nome, erro)
                    return
                except Exception as erro:  # cancelamento nao entra aqui (nao e Exception)
                    if self._ticker_desde_o_inicio:
                        tentativa = 0  # tinha voltado a funcionar: a espera recomeca de 1s
                    espera = espera_reconexao(tentativa, self._espera_max_s)
                    tentativa += 1
                    self._queda_da_conexao(erro, espera, tentativa)
                    await self._esperar(espera)
        finally:
            self._tarefas_ticker.discard(tarefa)

    async def iniciar_tickers(
        self, simbolos: list[str], ao_ticker: Callable[[Ticker], None] | None = None
    ) -> None:
        """Acompanha o melhor bid/ask de todos os simbolos ate ser cancelado (ou dar erro).

        Spot e futuro vao em pedidos separados, porque o ccxt escolhe o tipo de mercado
        pelo primeiro simbolo da lista. Toda mensagem registra a conexao no estado.
        Um mercado sem nenhum metodo de ticker no ccxt (NotSupported) avisa no log e deixa o outro seguindo;
        se nenhum mercado tiver, levanta NotSupported. Mercado cujos metodos so trazem tickers sem bid/ask
        nao para: tenta de novo com espera crescente (veja _acompanhar_tickers). Qualquer outro erro sobe (quem
        reconecta e
        `manter_tickers`). Com `limite_assinaturas`, a corretora que so assina um ticker por vez
        acompanha so os pares de maior volume 24h.
        Nao fecha a corretora: os livros da etapa 2 usam a mesma (veja `fechar`).
        """
        if ao_ticker is not None:
            self._ao_ticker = ao_ticker
        grupos = _agrupar_por_mercado(await self._tickers_dentro_do_limite(list(simbolos)))
        sem_metodo: list[NotSupported] = []

        async def acompanhar(grupo: list[str]) -> None:
            try:
                await self._acompanhar_tickers(grupo)
            except NotSupported as erro:
                sem_metodo.append(erro)
                if len(sem_metodo) == len(grupos):
                    raise  # nenhum mercado tem metodo de ticker
                registro.error("%s: nenhum metodo de ticker funciona no %s (%s); o outro mercado segue",
                               self.nome, mercado_do_simbolo(grupo[0]), erro)

        tarefas = [asyncio.create_task(acompanhar(g), name=f"tickers {self.nome} {mercado_do_simbolo(g[0])}")
                   for g in grupos]
        self._tarefas_ticker.update(tarefas)
        try:
            await asyncio.gather(*tarefas)
        finally:
            self._tarefas_ticker.difference_update(tarefas)
            await self._cancelar(tarefas)

    def _metodos_de_ticker(self) -> tuple[tuple[str, str], ...]:
        """Os metodos de ticker na ordem em que sao tentados: o da config (metodo_ticker) e os seguintes."""
        if self._metodo_ticker is None:
            return METODOS_DE_TICKER
        nomes = [metodo for metodo, _ in METODOS_DE_TICKER]
        return METODOS_DE_TICKER[nomes.index(self._metodo_ticker):]

    async def _acompanhar_tickers(self, simbolos: list[str]) -> None:
        """Tenta os metodos de ticker em ordem (veja _tentar_metodos), sem nunca desligar o mercado por bid/ask.

        Se o metodo que sobrou por ultimo tambem so traz tickers sem bid/ask, o mercado nao e abandonado: o
        log avisa (ERRO na primeira vez; depois aviso) e o coletor tenta de novo depois de 1s, 2s, 4s... (ate
        espera_max_reconexao_s). Ex.: as primeiras mensagens da Hyperliquid podem vir sem impactPxs.
        Metodo que o ccxt nao tem em nenhum lugar (NotSupported) continua subindo: tentar de novo nao adianta.
        """
        mercado = mercado_do_simbolo(simbolos[0])
        tentativa = 0
        while True:
            try:
                await self._tentar_metodos(simbolos)
                return  # os metodos so terminam com erro; isto e so uma protecao
            except _UltimoSemBidAsk as erro:
                espera = espera_reconexao(tentativa, self._espera_max_s)
                tentativa += 1
                self._avisar_ultimo_sem_bid_ask(mercado, erro.metodos, espera)
                await self._esperar(espera)

    def _avisar_ultimo_sem_bid_ask(self, mercado: str, metodos: list[str], espera: float) -> None:
        if mercado not in self._erro_sem_bid_ask:
            self._erro_sem_bid_ask.add(mercado)
            registro.error("%s: nenhum metodo de ticker traz bid/ask no %s (so sem bid/ask: %s); tentando de novo em "
                           "%gs. Se nunca funcionar, tire o %s da %s em mercados_por_corretora", self.nome, mercado,
                           ", ".join(metodos), espera, mercado, self.nome)
        else:
            registro.warning("%s: o %s continua sem bid/ask nos tickers (%s); tentando de novo em %gs",
                             self.nome, mercado, ", ".join(metodos), espera)

    async def _tentar_metodos(self, simbolos: list[str]) -> None:
        """Tenta os metodos de ticker em ordem; se um nao servir nesse mercado, passa ao proximo.

        Nao serve: o ccxt nao tem o metodo nesse mercado (NotSupported; ex.: na MEXC o watch_tickers so
        existe no futuro e o watch_bids_asks so no spot) ou o metodo so traz tickers sem bid/ask (SemBidAsk;
        ex.: o allMids da Hyperliquid). O metodo sem bid/ask que tem um seguinte fica anotado e nao e tentado
        de novo depois de uma reconexao. O ultimo que sobra nunca e anotado: levanta _UltimoSemBidAsk e
        `_acompanhar_tickers` tenta de novo depois de uma espera. Se nenhum existir, levanta NotSupported.
        """
        tem = getattr(self._exchange, "has", None) or {}
        mercado = mercado_do_simbolo(simbolos[0])
        ultimo_erro: Exception | None = None
        sem_bid_ask: list[str] = []  # metodos que so trouxeram tickers sem bid/ask neste mercado
        anotado_agora: str | None = None  # o metodo anotado como sem bid/ask nesta passada
        metodos = self._metodos_de_ticker()
        for posicao, (metodo, chave) in enumerate(metodos):
            if not tem.get(chave):
                if metodo == self._metodo_ticker:
                    self._avisar_metodo_ausente(metodo)
                continue
            if (mercado, metodo) in self._sem_bid_ask:
                registro.debug("%s: %s no %s ja so trouxe tickers sem bid/ask; pulando", self.nome, metodo, mercado)
                sem_bid_ask.append(metodo)
                continue
            try:
                if metodo == "watch_ticker":
                    await self._tickers_um_a_um(simbolos)
                else:
                    await self._tickers_em_lote(metodo, simbolos)
            except SemBidAsk as erro:
                sem_bid_ask.append(metodo)
                seguintes = [m for m, c in metodos[posicao + 1:]
                             if tem.get(c) and (mercado, m) not in self._sem_bid_ask]
                if not seguintes:
                    raise _UltimoSemBidAsk(sem_bid_ask) from erro  # o ultimo nunca e abandonado de vez
                self._sem_bid_ask.add((mercado, metodo))
                anotado_agora = metodo
                registro.warning("%s: %s no %s: %s; passando ao proximo metodo (%s)", self.nome, metodo, mercado,
                                 erro, seguintes[0])
            except NotSupported as erro:
                registro.warning("%s: %s nao serve para %s (%s); tentando o proximo metodo",
                                 self.nome, metodo, mercado, erro)
                ultimo_erro = erro
        if sem_bid_ask:
            # Os metodos seguintes nao existiam nesse mercado: o ultimo anotado volta a ser o ultimo e e tentado
            # de novo depois da espera (nunca fica desligado de vez).
            self._sem_bid_ask.discard((mercado, anotado_agora or sem_bid_ask[-1]))
            raise _UltimoSemBidAsk(sem_bid_ask)
        raise ultimo_erro or NotSupported(f"{self.nome}: a corretora nao tem nenhum metodo de ticker no ccxt")

    def _avisar_metodo_ausente(self, metodo: str) -> None:
        if not self._avisou_metodo_ausente:
            self._avisou_metodo_ausente = True
            registro.warning("%s: metodo_ticker %s nao existe nesta corretora no ccxt; usando o proximo da lista",
                             self.nome, metodo)

    def _escolher_canal(self, metodo: str, mercado: str) -> None:
        """Ajusta a opcao do ccxt com o canal de ticker deste mercado (veja CANAIS_DE_TICKER); as outras nao mudam.

        Precisa ser chamada imediatamente antes do watch_* (sem nenhum await no meio).
        """
        entrada = CANAIS_DE_TICKER.get((self.nome, metodo))
        opcoes = getattr(self._exchange, "options", None)
        if entrada is None or not isinstance(opcoes, dict):
            return
        opcao, canais = entrada
        atual = opcoes.get(opcao)
        opcoes[opcao] = {**(atual if isinstance(atual, dict) else {}), "method": canais[mercado]}

    def _sem_bid_ask_demais(self, conferencia: _ConferenciaBidAsk, total: int, com_bid_ask: int,
                            simbolo: str | None = None) -> None:
        """Levanta SemBidAsk se o metodo ja trouxe tickers sem bid/ask por mensagens_sem_bid_ask mensagens seguidas."""
        if conferencia.desistir(total, com_bid_ask, simbolo):
            raise SemBidAsk(f"so trouxe tickers sem bid/ask em {self._max_sem_bid_ask} mensagens seguidas")

    async def _tickers_em_lote(self, metodo: str, simbolos: list[str]) -> None:
        """Repete para sempre: espera a proxima mensagem de tickers (varios pares por vez) e trata.

        Levanta SemBidAsk se as mensagens so trazem tickers sem bid/ask (veja _ConferenciaBidAsk).
        """
        pedir = getattr(self._exchange, metodo)
        mercado = mercado_do_simbolo(simbolos[0])
        conferencia = _ConferenciaBidAsk(self._max_sem_bid_ask)
        while True:
            self._escolher_canal(metodo, mercado)
            tickers = await pedir(simbolos)
            self._sem_bid_ask_demais(conferencia, *self._tratar_tickers(tickers))

    async def _tickers_um_a_um(self, simbolos: list[str]) -> None:
        """Ultimo recurso: uma assinatura de ticker por simbolo.

        Cada simbolo e uma assinatura separada. Erro de um simbolo so (ex.: par que a corretora tirou)
        tenta de novo so ele, com espera crescente, sem derrubar os outros. Erro de conexao (a rede
        caiu) sobe: e o stream da corretora que caiu, e quem reconecta e `manter_tickers`.
        NotSupported tambem sobe (o mercado nao tem este metodo; `_acompanhar_tickers` decide), e
        SemBidAsk se os tickers de todos os simbolos so chegam sem bid/ask (uma conta para o mercado).
        """
        simbolos = await self._um_a_um_dentro_do_limite(simbolos)
        mercado = mercado_do_simbolo(simbolos[0])
        conferencia = _ConferenciaBidAsk(self._max_sem_bid_ask, simbolos_no_mercado=len(simbolos))

        async def acompanhar(simbolo: str) -> None:
            falhas = 0  # falhas seguidas deste simbolo, sem nenhum ticker entre elas
            while True:
                try:
                    self._escolher_canal("watch_ticker", mercado)
                    dados = await self._exchange.watch_ticker(simbolo)
                except NotSupported:
                    raise
                except Exception as erro:
                    if _erro_de_conexao(erro):
                        raise
                    self._remover_ticker_do_estado(simbolo)  # parou: o ultimo ticker nao vale mais, como o livro
                    espera = espera_reconexao(falhas, self._espera_max_s)
                    falhas += 1
                    primeira = simbolo not in self._traceback_ticker
                    self._traceback_ticker.add(simbolo)
                    dica = DICA_LIMITE if parece_limite_de_assinatura(erro) else ""
                    registro.warning("%s %s: o ticker deste par parou (%s: %s); tentando de novo em %gs, so ele.%s",
                                     self.nome, simbolo, type(erro).__name__, erro, espera, dica, exc_info=primeira)
                    await self._esperar(espera)
                    continue
                falhas = 0
                self._sem_bid_ask_demais(conferencia, *self._tratar_tickers({simbolo: dados}), simbolo=simbolo)

        tarefas = [asyncio.create_task(acompanhar(s)) for s in simbolos]
        try:
            await asyncio.gather(*tarefas)
        finally:
            await self._cancelar(tarefas)

    async def _um_a_um_dentro_do_limite(self, simbolos: list[str]) -> list[str]:
        """O limite de assinaturas tambem vale quando o um a um entra no lugar de um lote (troca automatica).

        Se a corretora ja comeca pelo um a um, `iniciar_tickers` ja aplicou o limite a lista toda e aqui nada
        muda. Na troca automatica (o lote nao trouxe bid/ask) a lista deste mercado ainda nao foi limitada:
        ficam os pares de maior volume 24h (e sempre os de cotacao, veja _escolher_no_limite).
        """
        if self._limite is None or len(simbolos) <= self._limite:
            return simbolos
        escolhidos, fora = await self._escolher_no_limite(simbolos)
        mercado = mercado_do_simbolo(simbolos[0])
        if self._fora_no_um_a_um.get(mercado) != fora:  # so quando muda (nao a cada reconexao)
            self._fora_no_um_a_um[mercado] = fora
            registro.warning("%s: limite de %d assinaturas de tickers (limites_assinatura) no watch_ticker que entrou "
                             "no lugar do lote no %s: %d de %d pares ficaram de fora (os de menor volume 24h)",
                             self.nome, self._limite, mercado, fora, len(simbolos))
        return escolhidos

    def _tratar_tickers(self, tickers: dict | None) -> tuple[int, int]:
        """Uma mensagem de tickers chegou: registra a conexao e entrega cada ticker valido.

        Um ticker que da erro ao ser tratado (no estado ou no callback) e descartado sozinho: os
        outros da mensagem e as proximas mensagens seguem normalmente.
        O atraso da mensagem usa o horario mais recente que a corretora informou nela (se informou).
        Devolve (quantos tickers vieram, quantos deles com bid e ask), para a troca automatica de metodo.
        """
        agora = self._relogio()
        self._registrar_mensagem(agora)
        self._chegou_ticker(agora)
        mais_recente = None  # maior horario (ms) informado pela corretora nesta mensagem
        total = com_bid_ask = 0
        for simbolo, dados in (tickers or {}).items():
            total += 1
            try:
                ticker = ticker_de_ccxt(self.nome, mercado_do_simbolo(simbolo), simbolo, dados, agora)
                # Ticker valido tem bid e ask; o descartado pode ter vindo com os dois (ex.: cruzado): o metodo serve.
                if ticker is not None or (_numero_ou_none(dados.get("bid")) is not None
                                          and _numero_ou_none(dados.get("ask")) is not None):
                    com_bid_ask += 1
                if ticker is None:
                    self._descartes["tickers"] += 1
                    registro.debug("%s %s: ticker descartado (sem bid/ask, preco invalido ou cruzado)",
                                   self.nome, simbolo)
                    continue
                horario = _numero_ou_none(dados.get("timestamp"))
                if horario is not None and (mais_recente is None or horario > mais_recente):
                    mais_recente = horario
                self._entregar_ticker(ticker)
            except Exception:  # defeito ao tratar este ticker: so ele fica de fora
                self._descartes["tickers"] += 1
                self._erro_ao_tratar_ticker(simbolo)
        if mais_recente is not None:
            self._registrar_atraso(agora, mais_recente / 1000)  # o ccxt entrega em ms
        return total, com_bid_ask

    async def _tickers_dentro_do_limite(self, simbolos: list[str]) -> list[str]:
        """Os simbolos de ticker que cabem no limite de assinaturas (os de maior volume 24h).

        So vale para corretora que assina um ticker por vez (so tem watch_ticker): nas outras um pedido
        so traz todos os pares. O volume vem do estado e, para os pares que ainda nao tem ticker (ex.: ao
        ligar), da API REST (fetch_tickers), uma vez por (re)inicio do stream.
        """
        if self._limite is None or len(simbolos) <= self._limite or not self._so_ticker_um_a_um():
            self._avisar_limite("tickers", 0, len(simbolos))
            return simbolos
        escolhidos, fora = await self._escolher_no_limite(simbolos)
        self._avisar_limite("tickers", fora, len(simbolos))
        return escolhidos

    async def _escolher_no_limite(self, simbolos: list[str]) -> tuple[list[str], int]:
        """(os simbolos de ticker que cabem no limite de assinaturas, quantos ficaram de fora).

        Os pares de cotacao (usar_para_cotacao, ex.: USDC/USDT na fonte) ficam SEMPRE: sem eles as cotacoes
        param. O limite corta so o resto, pelo maior volume 24h, e conta os de cotacao no total.
        """
        fixos = [s for s in simbolos if s in self._simbolos_cotacao]
        outros = [s for s in simbolos if s not in self._simbolos_cotacao]
        escolhidos, fora = selecionar_pares(outros, await self._volumes_dos_tickers(outros), self._limite - len(fixos))
        return fixos + escolhidos, fora

    def _so_ticker_um_a_um(self) -> bool:
        """True se o primeiro metodo de ticker que a corretora tem (na ordem de _metodos_de_ticker) e o watch_ticker.

        Ex.: a BingX (o ccxt so tem watch_ticker) e a Hyperliquid com metodo_ticker: watch_ticker na config.
        """
        tem = getattr(self._exchange, "has", None) or {}
        for metodo, chave in self._metodos_de_ticker():
            if tem.get(chave):
                return metodo == "watch_ticker"
        return False

    async def _volumes_dos_tickers(self, simbolos: list[str]) -> dict[str, float]:
        """Volume 24h de cada simbolo: o do estado e, para os que faltam, o da API REST (se a corretora tiver)."""
        volumes = {}
        for simbolo in simbolos:
            volume = self._volume_conhecido(simbolo)
            if volume is not None:
                volumes[simbolo] = volume
        faltam = [s for s in simbolos if s not in volumes]  # so os nunca vistos vao para a API REST
        tem = getattr(self._exchange, "has", None) or {}
        if not faltam or not tem.get("fetchTickers"):
            return volumes
        procurados = set(faltam)
        for grupo in _agrupar_por_mercado(faltam):
            try:
                resposta = await self._exchange.fetch_tickers(grupo)
            except Exception as erro:  # sem volume, os pares so ficam por ultimo na escolha
                registro.warning("%s: nao consegui o volume 24h pela API (%s: %s); os pares sem volume conhecido "
                                 "ficam por ultimo no limite de assinaturas", self.nome, type(erro).__name__, erro)
                continue
            for simbolo, dados in (resposta or {}).items():
                volume = _numero_ou_none(dados.get("quoteVolume")) if isinstance(dados, dict) else None
                if simbolo in procurados and volume is not None:
                    volumes[simbolo] = volume
                    self._volumes_conhecidos[simbolo] = volume
        return volumes

    def _volume_conhecido(self, simbolo: str) -> float | None:
        """Volume 24h do simbolo: o do ticker guardado no estado (o mais novo) ou o ultimo que o coletor viu.

        O ultimo visto fica no coletor e sobrevive a queda (marcar_desconectada apaga o estado): assim a
        reconexao nao precisa perguntar o volume a API REST de novo.
        """
        if self._estado is not None:
            self._indexar_se_preciso()
            par = self._pares.get(simbolo)
            if par is not None:
                ticker = self._estado.ticker_guardado(self.nome, mercado_do_simbolo(simbolo), par.ativo)
                volume = None if ticker is None else _numero_ou_none(ticker.get("volume_24h_usd"))
                if volume is not None:
                    return volume
        return self._volumes_conhecidos.get(simbolo)

    def _entregar_ticker(self, ticker: Ticker) -> None:
        if ticker.volume_24h_usd is not None:
            self._volumes_conhecidos[ticker.simbolo_original] = ticker.volume_24h_usd
        if self._estado is not None:
            if ticker.simbolo_original in self._simbolos_cotacao:
                # Par de cotacao (ex.: USDC/USDT na fonte_cotacao): guardado pelo simbolo, com o preco cru. Ele
                # pode nao ocupar a chave do ativo no estado (ex.: USDT/USD da Kraken perde para o USDT/EUR).
                self._estado.atualizar_cotacao(self.nome, ticker.simbolo_original, ticker.bid, ticker.ask,
                                               ticker.recebido_em)
                self._indexar_se_preciso()
                par = self._pares.get(ticker.simbolo_original)  # sem aviso: pode estar aqui so pela cotacao
            else:
                par = self._par_do_simbolo(ticker.simbolo_original)
            if par is not None:  # preco de 1 unidade do ativo: "1000PEPE" divide por 1000
                self._estado.atualizar_ticker(
                    self.nome, ticker.mercado, par.ativo,
                    ticker.bid / par.multiplicador, ticker.ask / par.multiplicador,
                    ticker.recebido_em, ticker.volume_24h_usd, ticker.variacao_24h_pct,
                    simbolo_original=ticker.simbolo_original, cotacao=par.cotacao,
                )
        if self._ao_ticker is not None:
            self._ao_ticker(ticker)

    # --- livros --------------------------------------------------------------

    async def iniciar(self, simbolos: list[str], ao_atualizar: Callable[[Livro], None]) -> None:
        """Acompanha o livro de todos os simbolos ao mesmo tempo, cada um na sua tarefa.

        Modo simples (usado pelo imprimir_precos): se um simbolo der erro, os outros sao
        cancelados e o erro sobe para quem chamou. Ao terminar (erro ou cancelamento),
        a corretora e sempre fechada. Para o servico de longa duracao, que escolhe os
        livros aos poucos e nao pode cair por causa de um par, use `assinar_livros`.
        """
        self._ao_atualizar = ao_atualizar
        tarefas = [asyncio.create_task(self._acompanhar_livro(s)) for s in simbolos]
        try:
            await asyncio.gather(*tarefas)
        finally:
            await self._cancelar(tarefas)
            await self.fechar()

    async def assinar_livros(
        self, simbolos: set[str], ao_atualizar: Callable[[Livro], None] | None = None
    ) -> None:
        """Etapa 2: deixa o livro completo aberto exatamente para `simbolos`.

        Comeca os que ainda nao estao abertos e cancela os que sairam da lista; os que
        continuam nao sao reiniciados. Pode ser chamada de novo a cada ciclo. Um simbolo
        que der erro sai do estado e tenta de novo sozinho, com espera crescente, sem
        atrapalhar os outros (veja _acompanhar_livro_isolado). Com `limite_assinaturas`,
        ficam so os de maior volume 24h.

        ao_atualizar: recebe o Livro cru. Fica guardado, entao so precisa ser passado na
        primeira vez. Sem ele, o coletor precisa ter um `estado` para guardar os livros.
        """
        if ao_atualizar is not None:
            self._ao_atualizar = ao_atualizar
        if self._ao_atualizar is None and self._estado is None:
            raise ValueError("assinar_livros precisa de um ao_atualizar ou de um estado para guardar os livros")

        if self._fechado:
            return
        desejados = self._livros_dentro_do_limite(set(simbolos))
        saindo = {s: self._tarefas_livro.pop(s) for s in list(self._tarefas_livro) if s not in desejados}
        await self._cancelar(list(saindo.values()))
        for simbolo in saindo:  # parou de chegar: o ultimo livro nao vale mais (nem enquanto o un_watch demora)
            self._remover_livro_do_estado(simbolo)
        # Os novos primeiro: o livro de um par que acabou de ficar interessante nao espera cancelamento nenhum.
        for simbolo in sorted(desejados):
            if simbolo not in self._tarefas_livro:
                self._tarefas_livro[simbolo] = asyncio.create_task(
                    self._acompanhar_livro_isolado(simbolo), name=f"livro {self.nome} {simbolo}"
                )
        # Cancelar a tarefa nao basta: o ccxt guarda a assinatura ate o un_watch_order_book, e sem ele
        # as assinaturas se acumulam ate o limite da conexao. Vao todos ao mesmo tempo (cada um com tempo
        # maximo), para nao somar esperas; os livros novos ja estao chegando enquanto isso.
        await asyncio.gather(*(self._desassinar_livro(s) for s in saindo))

    def _livros_abertos(self) -> set[str]:
        """Os livros abertos agora (preferencia no limite de assinaturas da base)."""
        return set(self._tarefas_livro)

    async def _desassinar_livro(self, simbolo: str) -> None:
        """Pede ao ccxt para largar a assinatura do livro do simbolo. Erro vai para o log, nunca sobe.

        Verifica o metodo e nao `exchange.has`: a Bitget tem `un_watch_order_book` mas `has` diz None.
        O ccxt base levanta NotSupported nas corretoras sem suporte; nesse caso avisa uma vez e para de tentar.
        Espera a confirmacao da corretora no maximo `timeout_desassinar_s`: sem resposta, avisa e segue
        (a tarefa local ja foi cancelada e o livro ja saiu do estado).
        """
        metodo = getattr(self._exchange, "un_watch_order_book", None)
        if metodo is None or self._sem_un_watch:
            return
        prazo = asyncio.timeout(self._timeout_desassinar_s)
        try:
            async with prazo:
                await metodo(simbolo)
        except TimeoutError as erro:
            if prazo.expired():  # foi o nosso tempo maximo que acabou
                registro.warning("%s %s: a corretora nao confirmou o cancelamento do livro em %ss; seguindo sem "
                                 "esperar", self.nome, simbolo, self._timeout_desassinar_s)
            else:  # o TimeoutError veio de dentro do ccxt (outro problema, com a mensagem dele)
                registro.warning("%s %s: nao consegui cancelar a assinatura do livro (un_watch_order_book: %s: %s)",
                                 self.nome, simbolo, type(erro).__name__, erro)
        except NotSupported:
            if not self._sem_un_watch:
                self._sem_un_watch = True
                registro.warning("%s: a corretora nao suporta un_watch_order_book; as assinaturas de livro "
                                 "dos pares que saem da lista continuam abertas ate fechar a conexao", self.nome)
        except Exception as erro:  # rede, timeout...: o par ja saiu da lista, so registra
            registro.warning("%s %s: nao consegui cancelar a assinatura do livro (un_watch_order_book: %s: %s)",
                             self.nome, simbolo, type(erro).__name__, erro)

    async def fechar(self) -> None:
        """Para tudo (livros e tickers em andamento) e fecha a corretora no ccxt (libera as conexoes).

        Nao chama un_watch_order_book: fechar a corretora derruba as conexoes e com elas as assinaturas.
        Depois de fechar, `assinar_livros` e `manter_tickers` nao comecam mais nada.
        """
        self._fechado = True
        atual = asyncio.current_task()
        tarefas = list(self._tarefas_livro.values()) + [t for t in self._tarefas_ticker if t is not atual]
        self._tarefas_livro.clear()
        await self._cancelar(tarefas)
        await self._exchange.close()

    async def _acompanhar_livro_isolado(self, simbolo: str) -> None:
        """Como _acompanhar_livro, mas um erro aqui so afeta este simbolo, e o coletor tenta de novo sozinho.

        Na falha o livro sai do estado (o ultimo recebido nao vale mais) e o coletor espera 1s, 2s, 4s...
        (ate espera_max_reconexao_s) antes de assinar de novo, enquanto o simbolo continuar na lista
        (sair da lista cancela esta tarefa, inclusive no meio da espera). Um livro bom volta a espera a 1s.
        Sequencia quebrada pede o livro de novo na hora, sem traceback (e esperado); so espera se quebrar
        outra vez antes de chegar um livro bom. Traceback no log so na primeira falha do simbolo.
        """
        falhas = 0  # falhas seguidas que esperaram, sem nenhum livro bom entre elas
        reassinou_na_hora = False  # a sequencia quebrou e o livro ja foi pedido de novo sem esperar
        try:
            while True:
                try:
                    await self._acompanhar_livro(simbolo)
                except Exception as erro:  # cancelamento nao entra aqui (nao e Exception)
                    if self._tarefas_livro.get(simbolo) is not asyncio.current_task():
                        return  # outra tarefa ja cuida deste simbolo: esta so termina, sem mexer no livro dela
                    self._remover_livro_do_estado(simbolo)  # caiu: o ultimo livro nao vale mais
                    if simbolo in self._livros_que_chegaram:
                        self._livros_que_chegaram.discard(simbolo)
                        falhas = 0  # tinha voltado a funcionar: a espera recomeca de 1s
                        reassinou_na_hora = False
                    if isinstance(erro, InvalidNonce) and not reassinou_na_hora:
                        # Sequencia quebrada: pede um livro completo novo na hora (spec, secao 8). A espera
                        # crescente so vale se quebrar de novo sem nenhum livro bom no meio.
                        reassinou_na_hora = True
                        espera = 0.0
                    else:
                        espera = espera_reconexao(falhas, self._espera_max_s)
                        falhas += 1
                    self._registrar_falha_do_livro(simbolo, erro, espera)
                    if espera > 0:
                        await self._esperar(espera)
        finally:
            self._livros_que_chegaram.discard(simbolo)
            # So mexe se esta tarefa ainda e a dona do simbolo. Se saiu da lista, quem cancelou ja
            # removeu; se outra tarefa ja assumiu o simbolo, o livro guardado e o dela.
            if self._tarefas_livro.get(simbolo) is asyncio.current_task():
                del self._tarefas_livro[simbolo]
                self._remover_livro_do_estado(simbolo)

    def _registrar_falha_do_livro(self, simbolo: str, erro: Exception, espera: float) -> None:
        """Log da queda do livro de um simbolo (com traceback so na primeira vez do simbolo)."""
        if isinstance(erro, InvalidNonce):
            # O ccxt achou um buraco na sequencia de atualizacoes do livro (ChecksumError e um InvalidNonce)
            # e desfez a assinatura: assinar de novo pede um livro completo novo. Nem toda corretora avisa:
            # algumas so ressincronizam sozinhas, sem erro.
            self._descartes["sequencia_quebrada"] += 1
            quando = "agora" if espera <= 0 else f"em {espera:g}s"
            registro.warning("%s %s: sequencia do livro quebrada (%s); o livro saiu do scanner e um livro completo "
                             "novo sera pedido %s", self.nome, simbolo, erro, quando)
            return
        # Traceback so na primeira falha do simbolo e so se nao for queda de conexao: quando a rede cai,
        # todos os livros caem juntos e o traceback de cada um so encheria o log.
        primeira = simbolo not in self._traceback_livro and not _erro_de_conexao(erro)
        if primeira:
            self._traceback_livro.add(simbolo)
        dica = DICA_LIMITE if parece_limite_de_assinatura(erro) else ""
        registro.warning("%s %s: livro parou (%s: %s); tentando de novo em %gs enquanto o par continuar na lista.%s",
                         self.nome, simbolo, type(erro).__name__, erro, espera, dica, exc_info=primeira)

    async def _acompanhar_livro(self, simbolo: str) -> None:
        """Repete para sempre: espera o livro mudar e entrega o livro novo, se for valido."""
        mercado = mercado_do_simbolo(simbolo)
        while True:
            ob = await self._exchange.watch_order_book(simbolo)
            agora = self._relogio()
            self._registrar_mensagem(agora)  # vale ate para livro que sera descartado: a conexao esta viva
            livro = livro_de_ccxt(self.nome, mercado, simbolo, ob, agora)
            if livro is None:
                self._descartes["livros"] += 1
                registro.debug("%s %s: livro descartado (vazio, preco invalido ou cruzado)", self.nome, simbolo)
                continue
            if livro.ts_corretora is not None:
                self._registrar_atraso(agora, livro.ts_corretora)
            self._entregar_livro(livro)
            self._livros_que_chegaram.add(simbolo)

    def _entregar_livro(self, livro: Livro) -> None:
        if self._estado is not None:
            par = self._par_do_simbolo(livro.simbolo_original)
            if par is not None:
                self._estado.atualizar_livro(converter_livro(livro, par), ativo=par.ativo)
                self._livros_no_estado[livro.simbolo_original] = (livro.mercado, par.ativo)
        if self._ao_atualizar is not None:
            self._ao_atualizar(livro)

    # --- apoio ---------------------------------------------------------------

    def _remover_ticker_do_estado(self, simbolo: str) -> None:
        """Tira do estado o ticker deste simbolo (a assinatura dele parou), inclusive o de cotacao."""
        if self._estado is None:
            return
        self._estado.remover_cotacao(self.nome, simbolo)
        par = self._pares.get(simbolo)
        if par is not None:
            self._estado.remover_ticker(self.nome, mercado_do_simbolo(simbolo), par.ativo, simbolo_original=simbolo)

    def simbolos_por_chave(self) -> dict[tuple[str, str], str]:
        """Para cada (mercado, ativo), o simbolo do ccxt cujos dados vao para o estado.

        Ex.: {("spot", "BTC"): "BTC/USDT", ("futuro", "PEPE"): "1000PEPE/USDT:USDT"}. Le
        `exchange.markets` (carregue antes com load_markets); a escolha e a mesma de
        `_indexar_markets` (cotacao preferida). O __main__ usa para saber quais tickers pedir.
        """
        self._indexar_se_preciso()
        return dict(self._vencedor_da_chave)

    def cotacoes_dos_simbolos(self, simbolos) -> set[str]:
        """As moedas de cotacao dos simbolos que vao ao estado, ex.: {"USDT", "USDC"}. Os outros sao ignorados."""
        self._indexar_se_preciso()
        return {self._pares[simbolo].cotacao for simbolo in simbolos if simbolo in self._pares}

    def markets(self) -> dict:
        """Os markets carregados da corretora no ccxt ({simbolo: market}); vazio se ainda nao carregou."""
        return getattr(self._exchange, "markets", None) or {}

    def usar_para_cotacao(self, simbolos) -> None:
        """Os simbolos cujo ticker tambem alimenta as cotacoes (ex.: {"USDC/USDT"} na fonte_cotacao).

        O ticker deles vai para o estado pelo simbolo (EstadoMercados.atualizar_cotacao), alem do caminho
        normal. Quem chama (o __main__) poe esses simbolos na lista de tickers da etapa 1.
        """
        self._simbolos_cotacao = set(simbolos)

    def _indexar_se_preciso(self) -> dict:
        """Refaz o indice se `exchange.markets` mudou (primeira vez ou lista recarregada). Devolve os markets."""
        markets = getattr(self._exchange, "markets", None)
        if markets is not self._markets_indexados:
            self._indexar_markets(markets or {})
            self._markets_indexados = markets
        return markets or {}

    def _par_do_simbolo(self, simbolo: str) -> ParNormalizado | None:
        """O par normalizado do simbolo, se os dados dele podem ir para o estado; senao None.

        Le `exchange.markets` (o ccxt carrega ao assinar). O dado NAO vai ao estado, com um aviso
        no log (um por simbolo), quando o simbolo:
          - nao existe nos markets, ou vem sem base/cotacao;
          - nao e spot nem perpetuo linear (opcao, futuro com vencimento, contrato inverso);
          - perde para outro simbolo do mesmo ativo e mercado por ter cotacao menos preferida.
        """
        markets = self._indexar_se_preciso()
        par = self._pares.get(simbolo)
        if par is None and simbolo not in self._avisados:
            self._avisados.add(simbolo)
            registro.warning("%s %s: %s; os dados deste par nao vao para o estado",
                             self.nome, simbolo, self._motivo_fora_do_estado(simbolo, markets.get(simbolo)))
        return par

    def _indexar_markets(self, markets: dict) -> None:
        """Escolhe, para cada (mercado, ativo), o unico simbolo que alimenta o estado.

        A escolha olha todos os markets da corretora, e nao so os simbolos pedidos, para nao mudar
        conforme a lista de pares: vence a moeda preferida (USDT, USDC, USDG, as outras) e, se a
        moeda for a mesma (ex.: "1000PEPE" e "PEPE"), o simbolo que vem primeiro em ordem alfabetica.

        Um market com dados que o scanner nao entende (ex.: tamanho de contrato em texto) e pulado,
        com um aviso no log (uma vez por simbolo), sem atrapalhar os outros. Market inativo
        (`active` False: suspenso ou saindo da corretora) tambem fica de fora, com aviso. Market de um
        mercado fora de `mercados` (mercados_por_corretora) fica de fora sem aviso por simbolo: o log
        mostra quantos, uma vez.
        """
        melhores: dict[tuple[str, str], tuple[tuple, str, ParNormalizado]] = {}
        fora_dos_mercados = Counter()  # mercado fora de `mercados` -> quantos markets ficaram de fora
        for simbolo, market in markets.items():
            try:
                mercado = mercado_do_market(market)
                if mercado is None or not market.get("base") or not market.get("quote"):
                    continue
                if mercado not in self._mercados:
                    fora_dos_mercados[mercado] += 1
                    continue
                if market.get("active") is False:  # suspenso ou saindo da corretora (o ccxt ainda lista)
                    if simbolo not in self._avisados:
                        self._avisados.add(simbolo)
                        registro.warning("%s %s: mercado inativo na corretora (active=False no ccxt: suspenso ou "
                                         "saindo); este par fica de fora", self.nome, simbolo)
                    continue
                par = normalizar(self.nome, market)
                ordem = (*prioridade_cotacao(par.cotacao), simbolo)
            except Exception as erro:  # market malformado: so ele fica de fora
                if simbolo not in self._avisados:
                    self._avisados.add(simbolo)
                    registro.warning("%s %s: market com dados invalidos no ccxt (%s: %s); este par fica de fora",
                                     self.nome, simbolo, type(erro).__name__, erro)
                continue
            chave = (mercado, par.ativo)
            if chave not in melhores or ordem < melhores[chave][0]:
                melhores[chave] = (ordem, simbolo, par)
        self._vencedor_da_chave = {chave: simbolo for chave, (_, simbolo, _) in melhores.items()}
        self._pares = {simbolo: par for _, simbolo, par in melhores.values()}
        if fora_dos_mercados and not self._avisou_mercados_fora:
            self._avisou_mercados_fora = True
            registro.info("%s: usando so %s (mercados_por_corretora); ficam de fora %s", self.nome,
                          " e ".join(self._mercados),
                          ", ".join(f"{n} pares de {m}" for m, n in sorted(fora_dos_mercados.items())))

    def _motivo_fora_do_estado(self, simbolo: str, market: dict | None) -> str:
        """Texto para o log: por que os dados deste simbolo nao vao para o estado."""
        if not market:
            return "mercado nao encontrado nos markets do ccxt"
        mercado = mercado_do_market(market)
        if mercado is None:
            return "mercado que o scanner nao usa (so spot e perpetuo linear; este e inverso, opcao ou com vencimento)"
        if mercado not in self._mercados:
            return (f"o {mercado} desta corretora esta fora de mercados_por_corretora "
                    f"(usando so {', '.join(self._mercados)})")
        if not market.get("base") or not market.get("quote"):
            return "mercado sem base ou cotacao nos markets do ccxt"
        par = normalizar(self.nome, market)
        vencedor = self._vencedor_da_chave.get((mercado, par.ativo), "outro simbolo")
        return (f"mercado descartado: {vencedor} ja ocupa o lugar de {par.ativo} no {mercado} "
                f"(preferencia: cotacao USDT > USDC > USDG; mesma cotacao: ordem do simbolo)")

    @staticmethod
    async def _cancelar(tarefas: list[asyncio.Task]) -> None:
        """Cancela as tarefas e espera cada uma terminar de verdade."""
        for tarefa in tarefas:
            tarefa.cancel()
        await asyncio.gather(*tarefas, return_exceptions=True)
