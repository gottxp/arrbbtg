"""Dados lentos: funding e taxas taker (REST, de tempos em tempos) e as cotacoes das moedas fora de USDT.

Preco e livro chegam por WebSocket (rapido). Funding e taxas mudam devagar, entao
basta perguntar a cada `intervalo_dados_lentos_s` segundos (config.yaml).

Cotacoes (Task 15): o preco em USDT de cada moeda de cotacao diferente de USDT (ex.: USDC da
Hyperliquid) vem do ticker spot ja coletado da corretora `fonte_cotacao` (padrao Bitget): o meio do
bid/ask do par USDC/USDT (ou 1/meio do par inverso USDT/USD). Nao ha REST nisso: o par entra nos
tickers da etapa 1 da fonte (o __main__ cuida) e o motor pede `cotacoes()` a cada ciclo.

Tudo em % (0.06 = 0,06%). Sem rede nos testes: as corretoras entram por parametro.
"""

import asyncio
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable

from scanner.config.carregar import Config
from scanner.normalizador import mercado_do_market, normalizar, prioridade_cotacao

registro = logging.getLogger("scanner.coletores")

# Quando a corretora nao informa de quantas em quantas horas cobra funding, a maioria cobra a cada 8h.
INTERVALO_FUNDING_PADRAO_H = 8


class TaxaDesconhecida(LookupError):
    """Nao ha taxa taker nem no config.yaml nem vinda da API para esta corretora e mercado."""


def intervalo_funding_h(texto) -> int:
    """Converte o "interval" do ccxt ("8h", "1h", "4h") em horas inteiras; sem formato valido, 8."""
    achado = re.fullmatch(r"\s*(\d+)\s*h\s*", texto) if isinstance(texto, str) else None
    if achado and int(achado.group(1)) > 0:
        return int(achado.group(1))
    return INTERVALO_FUNDING_PADRAO_H


def _numero_valido(valor) -> bool:
    """True para numero real (int/float, nao bool, nao NaN nem infinito)."""
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


def _para_percentual(fracao: float) -> float:
    """O ccxt usa fracao (0.0006); o scanner usa % (0.06). O arredondamento tira ruido de ponto flutuante."""
    return round(fracao * 100, 10)


def _taxas_taker_dos_mercados(markets: dict) -> dict[str, float]:
    """Taxa taker em % de cada mercado (spot, futuro) da corretora, lida de `market["taker"]`.

    So contam os markets que o scanner usa (veja mercado_do_market): spot e perpetuo linear.
    Opcoes, futuros com vencimento e contratos inversos tem taxa propria e puxariam a do futuro
    para baixo, criando sinais falsos.
    Cada par pode ter uma taxa diferente; fica a mais comum do mercado (as excecoes,
    como pares promocionais, nao mandam). Em empate, a maior: errar para cima so
    deixa o sinal mais cauteloso.
    Market malformado (ex.: nem e um dict) e ignorado sem atrapalhar os outros.
    """
    contagens: dict[str, Counter] = {}
    for market in markets.values():
        if not isinstance(market, dict):
            continue
        mercado = mercado_do_market(market)
        taker = market.get("taker")
        if mercado is None or not _numero_valido(taker) or taker < 0:
            continue
        contagens.setdefault(mercado, Counter())[_para_percentual(taker)] += 1
    return {
        mercado: max(contagem.items(), key=lambda item: (item[1], item[0]))[0]
        for mercado, contagem in contagens.items()
    }


def _ler_funding(item: dict | None, horas_da_config: int | None = None) -> tuple[float, int] | None:
    """(taxa em %, intervalo em horas) de um funding rate do ccxt, ou None se nao trouxer a taxa.

    O intervalo segue a ordem: o da config (intervalo_funding_h), o que o ccxt informa e, sem nenhum, 8h.
    """
    if not item or not _numero_valido(item.get("fundingRate")):
        return None
    horas = horas_da_config if horas_da_config is not None else intervalo_funding_h(item.get("interval"))
    return _para_percentual(item["fundingRate"]), horas


def _meio_do_bid_ask(ticker: dict | None) -> float | None:
    """(bid + ask) / 2 do ticker, ou None se nao ha ticker ou o bid/ask nao e numero maior que zero."""
    if not ticker:
        return None
    bid, ask = ticker.get("bid"), ticker.get("ask")
    if not (_numero_valido(bid) and _numero_valido(ask) and bid > 0 and ask > 0):
        return None
    return (bid + ask) / 2


def _par_de_cotacao(moeda: str, markets: dict) -> tuple[str, bool] | None:
    """(simbolo, inverso) do par spot ativo que da a cotacao da moeda em USDT, ou None se a fonte nao tem.

    Direto ("USDC/USDT": 1 USDC em USDT) tem preferencia sobre o inverso ("USDT/USD": 1 USDT em USD,
    entao 1 USD vale 1/preco USDT).
    """
    for simbolo, inverso in ((f"{moeda}/USDT", False), (f"USDT/{moeda}", True)):
        market = markets.get(simbolo)
        if isinstance(market, dict) and mercado_do_market(market) == "spot" and market.get("active") is not False:
            return simbolo, inverso
    return None


class DadosLentos:
    """Funding e taxas taker de cada corretora, e as cotacoes das moedas fora de USDT.

    taxas_config: taxas do config.yaml, {(corretora, mercado): taxa em %}. Tem prioridade
        sobre a taxa da API, porque e a taxa real da conta do dono (nivel VIP, descontos).
    intervalos_funding: intervalo_funding_h do config.yaml, {corretora: horas}. Vale sobre o
        intervalo que o ccxt informa (ex.: a Hyperliquid cobra de hora em hora).
    estado: o EstadoMercados de onde saem os tickers dos pares de cotacao (sem ele, nenhuma cotacao).
    fonte_cotacao / fontes_cotacao: corretora padrao das cotacoes e a excecao por moeda ({"USD": "kraken"}).
    faixa_cotacao_pct: cotacao mais longe de 1 do que isto (em %) e tratada como sem cotacao.
    relogio: hora atual em segundos (os testes passam um relogio fixo).
    """

    def __init__(
        self,
        taxas_config: dict | None = None,
        intervalos_funding: dict | None = None,
        estado=None,
        fonte_cotacao: str = Config.fonte_cotacao,
        fontes_cotacao: dict | None = None,
        faixa_cotacao_pct: float = Config.faixa_cotacao_pct,
        relogio: Callable[[], float] = time.time,
    ):
        self._taxas_config = dict(taxas_config) if taxas_config else {}
        self._intervalos_config = dict(intervalos_funding) if intervalos_funding else {}
        self._taxas_api: dict[tuple[str, str], float] = {}  # (corretora, mercado) -> taxa taker em %
        self._funding: dict[tuple[str, str], tuple[float, int]] = {}  # (corretora, ativo) -> (taxa %, horas)
        # Cotacoes.
        self._estado = estado
        self._fonte_cotacao = fonte_cotacao
        self._fontes_cotacao = dict(fontes_cotacao) if fontes_cotacao else {}
        self._faixa_cotacao_pct = faixa_cotacao_pct
        self._relogio = relogio
        self._pares_cotacao: dict[str, tuple[str, str, bool]] = {}  # moeda -> (corretora, simbolo, inverso)
        self._sem_par_avisado: dict[str, str] = {}  # moeda -> motivo ja avisado de nao ter par (nao repete)
        self._fora_da_faixa: set[str] = set()  # moedas cuja ultima cotacao lida estava fora da faixa (ja avisado)

    # --- consulta (motor) ----------------------------------------------------

    def funding(self, corretora: str, ativo: str) -> tuple[float, int] | None:
        """(taxa de funding em %, intervalo de cobranca em horas) do ativo, ou None se nao se sabe."""
        return self._funding.get((corretora, ativo))

    def taxa_taker(self, corretora: str, mercado: str) -> float:
        """Taxa taker em %: a do config.yaml se existir, senao a da API.

        Levanta TaxaDesconhecida se nenhuma das duas existe (ainda). Nao ha taxa
        "de chute": errar para menos criaria sinais falsos.
        """
        chave = (corretora, mercado)
        if chave in self._taxas_config:
            return self._taxas_config[chave]
        if chave in self._taxas_api:
            return self._taxas_api[chave]
        raise TaxaDesconhecida(
            f"sem taxa taker para {corretora}/{mercado}: preencha 'taxas' no config.yaml "
            "ou espere a primeira atualizacao dos dados lentos"
        )

    # --- cotacoes (motor) ----------------------------------------------------

    def escolher_pares_de_cotacao(self, moedas: set[str], markets: dict[str, dict]) -> dict[str, list[str]]:
        """Escolhe, para cada moeda de cotacao fora de USDT, o par spot da fonte que da o preco dela em USDT.

        moedas: as moedas de cotacao usadas agora pelas corretoras (ex.: {"USDC"}); USDT e ignorado.
        markets: {corretora: markets do ccxt} das corretoras que carregaram a lista de mercados.
        A fonte de cada moeda e a de fontes_cotacao (se houver) ou fonte_cotacao. Fonte desligada (ou que nao
        carregou) e fonte sem o par: a moeda fica sem cotacao (1:1 com aviso_cotacao), com aviso no log uma
        vez por motivo. Devolve {corretora: [simbolos]}: os pares que o __main__ poe nos tickers da etapa 1 da
        fonte (e no coletor, com usar_para_cotacao). Chamado de novo a cada recarga da lista de mercados.
        """
        pares: dict[str, tuple[str, str, bool]] = {}
        for moeda in sorted(set(moedas) - {"USDT"}):
            fonte = self._fontes_cotacao.get(moeda, self._fonte_cotacao)
            if fonte not in markets:
                self._avisar_sem_par(moeda, f"a fonte {fonte} nao esta ligada (ou ainda nao carregou a lista "
                                            "de mercados)")
                continue
            achado = _par_de_cotacao(moeda, markets[fonte])
            if achado is None:
                self._avisar_sem_par(moeda, f"a fonte {fonte} nao tem o par spot {moeda}/USDT nem USDT/{moeda}")
                continue
            simbolo, inverso = achado
            pares[moeda] = (fonte, simbolo, inverso)
            self._sem_par_avisado.pop(moeda, None)
            if self._pares_cotacao.get(moeda) != pares[moeda]:
                registro.info("cotacao de %s em USDT: ticker spot %s da %s", moeda, simbolo, fonte)
        self._pares_cotacao = pares
        extras: dict[str, list[str]] = {}
        for fonte, simbolo, _ in pares.values():
            extras.setdefault(fonte, []).append(simbolo)
        return {fonte: sorted(simbolos) for fonte, simbolos in extras.items()}

    def _avisar_sem_par(self, moeda: str, motivo: str) -> None:
        if self._sem_par_avisado.get(moeda) != motivo:
            self._sem_par_avisado[moeda] = motivo
            registro.warning("sem cotacao de %s em USDT: %s; os pares cotados em %s usam 1:1 com aviso_cotacao "
                             "(confira fonte_cotacao e fontes_cotacao no config.yaml)", moeda, motivo, moeda)

    def cotacoes(self) -> dict[str, float]:
        """As cotacoes de agora: {moeda: quanto vale 1 unidade em USDT}, ex.: {"USDC": 0.9998}.

        Cada uma e o meio do bid/ask do ticker spot do par escolhido (veja escolher_pares_de_cotacao), so se
        o ticker estiver valido agora (fresco, conexao viva: EstadoMercados.obter_cotacao); no par inverso
        (USDT/USD), 1/meio. Cotacao mais longe de 1 que faixa_cotacao_pct conta como sem cotacao, com aviso
        no log (uma vez ate ela voltar). Moeda que nao aparece aqui usa 1:1 com aviso_cotacao no motor.
        """
        if self._estado is None:
            return {}
        agora = self._relogio()
        resultado = {}
        for moeda, (fonte, simbolo, inverso) in self._pares_cotacao.items():
            valor = _meio_do_bid_ask(self._estado.obter_cotacao(fonte, simbolo, agora))
            if valor is None:
                continue  # sem ticker valido agora
            if inverso:
                valor = 1 / valor
            # Arredondado para o limite valer exatamente (1,1 com faixa de 10% esta dentro).
            if round(abs(valor - 1) * 100, 9) > self._faixa_cotacao_pct:
                if moeda not in self._fora_da_faixa:
                    self._fora_da_faixa.add(moeda)
                    registro.warning("cotacao de %s fora da faixa de seguranca: %.6g USDT (%s da %s), mais de %g%% "
                                     "longe de 1; os pares cotados em %s usam 1:1 com aviso_cotacao ate ela voltar",
                                     moeda, valor, simbolo, fonte, self._faixa_cotacao_pct, moeda)
                continue
            if moeda in self._fora_da_faixa:
                self._fora_da_faixa.discard(moeda)
                registro.info("cotacao de %s voltou para a faixa de seguranca: %.6g USDT", moeda, valor)
            resultado[moeda] = valor
        return resultado

    # --- entregues pelo proprio coletor (Lighter, Task 16) --------------------

    def receber_taxas(self, corretora: str, markets: dict) -> None:
        """Taxas taker lidas dos markets que o proprio coletor carregou (a Lighter nao passa pelo ccxt).

        markets: no formato do ccxt (`market["taker"]` em fracao); vale a mesma regra do REST: a taxa mais
        comum de cada mercado. A taxa do config.yaml continua valendo sobre esta (veja taxa_taker).
        """
        for mercado, taxa in _taxas_taker_dos_mercados(markets).items():
            self._taxas_api[(corretora, mercado)] = taxa

    def receber_funding(self, corretora: str, ativo: str, taxa_pct, intervalo_h: int) -> None:
        """Funding de um futuro que chegou pelo WebSocket (Lighter: market_stats), ja em %.

        intervalo_h: de quantas em quantas horas a corretora cobra; o intervalo_funding_h do config.yaml
        vale sobre ele. Taxa que nao e numero real e ignorada (fica a anterior).
        """
        if not _numero_valido(taxa_pct):
            return
        horas = self._intervalos_config.get(corretora, intervalo_h)
        self._funding[(corretora, ativo)] = (float(taxa_pct), horas)

    # --- atualizacao (REST) --------------------------------------------------

    async def atualizar(self, exchanges: dict) -> None:
        """Busca funding e taxas de todas as corretoras ao mesmo tempo.

        exchanges: {nome da corretora: objeto da corretora no ccxt}. Se uma corretora
        falhar, o erro vai para o log e os dados anteriores dela continuam valendo.
        """
        await asyncio.gather(*(self._atualizar_sem_quebrar(c, e) for c, e in exchanges.items()))

    async def rodar(self, exchanges: dict, intervalo_s: float,
                    esperar: Callable[[float], Awaitable] = asyncio.sleep) -> None:
        """Atualiza para sempre (ate ser cancelado), cada corretora no seu proprio ritmo.

        Cada corretora tem o seu laco (`acompanhar`): uma corretora lenta (ou travada) nao atrasa a
        atualizacao das outras. Erro de uma corretora vai para o log e o laco dela tenta de novo na
        proxima rodada.
        esperar: funcao que espera N segundos (os testes trocam para nao dormir de verdade).
        """
        tarefas = [asyncio.create_task(self.acompanhar(c, e, intervalo_s, esperar), name=f"dados lentos {c}")
                   for c, e in exchanges.items()]
        try:
            await asyncio.gather(*tarefas)
        finally:
            for tarefa in tarefas:
                tarefa.cancel()
            await asyncio.gather(*tarefas, return_exceptions=True)

    async def acompanhar(self, corretora: str, exchange, intervalo_s: float,
                         esperar: Callable[[float], Awaitable] = asyncio.sleep) -> None:
        """O laco de UMA corretora: atualiza, espera `intervalo_s` e repete, ate ser cancelado.

        O __main__ usa direto para a corretora que entra depois (nao tinha carregado os mercados ao ligar).
        """
        while True:
            await self._atualizar_sem_quebrar(corretora, exchange)
            await esperar(intervalo_s)

    async def _atualizar_sem_quebrar(self, corretora: str, exchange) -> None:
        try:
            await self._atualizar_corretora(corretora, exchange)
        except Exception as erro:  # rede, limite de requisicoes, mudanca na API...
            registro.warning(
                "%s: nao consegui atualizar funding e taxas (%s: %s); mantenho os dados anteriores",
                corretora, type(erro).__name__, erro,
            )

    async def _atualizar_corretora(self, corretora: str, exchange) -> None:
        if not exchange.markets:
            await exchange.load_markets()
        markets = exchange.markets

        for mercado, taxa in _taxas_taker_dos_mercados(markets).items():
            self._taxas_api[(corretora, mercado)] = taxa

        escolhidos = await self._buscar_funding(corretora, exchange, markets)
        for ativo, (_, taxa_e_horas) in escolhidos.items():
            self._funding[(corretora, ativo)] = taxa_e_horas

    async def _buscar_funding(self, corretora: str, exchange, markets: dict) -> dict:
        """Funding dos futuros: {ativo normalizado: (chave de preferencia, (taxa %, horas))}."""
        if exchange.has.get("fetchFundingRates"):
            brutos = await exchange.fetch_funding_rates()  # todos de uma vez
        else:
            brutos = await self._funding_um_a_um(corretora, exchange, markets)

        escolhidos: dict[str, tuple] = {}
        horas_da_config = self._intervalos_config.get(corretora)
        for simbolo, item in brutos.items():
            market = markets.get(simbolo)
            funding = _ler_funding(item, horas_da_config) if isinstance(item, dict) else None
            if not isinstance(market, dict) or funding is None or mercado_do_market(market) != "futuro":
                continue
            if not market.get("base") or not market.get("quote"):
                continue
            try:
                par = normalizar(corretora, market)
            except (TypeError, ValueError):  # market com dados que o scanner nao entende: so ele fica de fora
                continue
            # Mais de um futuro pode ter o mesmo ativo (BTC/USDT:USDT e BTC/USDC:USDC): vale a mesma
            # regra do estado (moeda preferida e, em empate, ordem do simbolo), para o funding ser do
            # mesmo contrato cujo preco o motor le.
            preferencia = (*prioridade_cotacao(par.cotacao), simbolo)
            if par.ativo not in escolhidos or preferencia < escolhidos[par.ativo][0]:
                escolhidos[par.ativo] = (preferencia, funding)
        return escolhidos

    async def _funding_um_a_um(self, corretora: str, exchange, markets: dict) -> dict:
        """Para corretoras sem busca em lote: pergunta o funding de cada futuro, um por vez."""
        brutos = {}
        falhas = []
        simbolos = [s for s, m in markets.items() if isinstance(m, dict) and mercado_do_market(m) == "futuro"]
        for simbolo in simbolos:
            try:
                brutos[simbolo] = await exchange.fetch_funding_rate(simbolo)
            except Exception as erro:  # um par com problema nao pode atrapalhar os outros
                falhas.append((simbolo, erro))
        if falhas:
            simbolo, erro = falhas[0]
            registro.warning(
                "%s: funding indisponivel em %d de %d pares (primeiro: %s: %s: %s)",
                corretora, len(falhas), len(simbolos), simbolo, type(erro).__name__, erro,
            )
        return brutos
