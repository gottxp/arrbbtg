"""Dados lentos, buscados por REST de tempos em tempos: funding e taxas taker.

Preco e livro chegam por WebSocket (rapido). Funding e taxas mudam devagar, entao
basta perguntar a cada `intervalo_dados_lentos_s` segundos (config.yaml).

Tudo em % (0.06 = 0,06%). Sem rede nos testes: as corretoras entram por parametro.
"""

import asyncio
import logging
import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable

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


def _ler_funding(item: dict | None) -> tuple[float, int] | None:
    """(taxa em %, intervalo em horas) de um funding rate do ccxt, ou None se nao trouxer a taxa."""
    if not item or not _numero_valido(item.get("fundingRate")):
        return None
    return _para_percentual(item["fundingRate"]), intervalo_funding_h(item.get("interval"))


class DadosLentos:
    """Funding e taxas taker de cada corretora.

    taxas_config: taxas do config.yaml, {(corretora, mercado): taxa em %}. Tem prioridade
        sobre a taxa da API, porque e a taxa real da conta do dono (nivel VIP, descontos).
    """

    def __init__(self, taxas_config: dict | None = None):
        self._taxas_config = dict(taxas_config) if taxas_config else {}
        self._taxas_api: dict[tuple[str, str], float] = {}  # (corretora, mercado) -> taxa taker em %
        self._funding: dict[tuple[str, str], tuple[float, int]] = {}  # (corretora, ativo) -> (taxa %, horas)

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
        for simbolo, item in brutos.items():
            market = markets.get(simbolo)
            funding = _ler_funding(item) if isinstance(item, dict) else None
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
