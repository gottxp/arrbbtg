"""Motor: transforma o estado dos mercados em candidatos e sinais (especificacao, secoes 3 a 5).

A cada ciclo (ciclo_ms da config, padrao 500 ms) o __main__ chama:
  1. `ciclo(agora)`: monta os candidatos spot x futuro e futuro x futuro, aplica os filtros e o
     modo da sessao, passa ao gerenciador de sinais e grava eventos e amostras no banco;
  2. `selecao_livros(agora)`: etapa 2 da coleta, diz de quais pares abrir o livro completo.

O motor sempre calcula os dois tipos; quem escolhe qual aparece e o filtro "tipos de oportunidade"
(em `passa`). Assim as metricas do periodo dos dois tipos continuam aquecidas quando o dono liga
ou desliga um tipo no painel.

Precos: o estado guarda os livros com preco por 1 unidade do ativo, mas ainda na moeda de
cotacao do par (USDT, USDC, USDG). A troca para USDT acontece aqui, com as cotacoes do momento;
sem cotacao valida, usa 1:1 e marca o candidato com aviso_cotacao.

Combinacao ("combo"): (ativo, corretora long, mercado long, corretora short, mercado short),
ex.: ("BTC", "gate", "spot", "bitget", "futuro") ou ("BTC", "gate", "futuro", "mexc", "futuro").
No spot x futuro o spot e sempre o long; o futuro x futuro e sempre entre corretoras diferentes
e vale nas duas direcoes.

Desempenho: com 9 corretoras e centenas de ativos sao dezenas de milhares de combinacoes. Por
isso cada mercado e lido uma vez so por ciclo (e nao uma vez por combinacao), e as combinacoes
montadas ficam guardadas enquanto os mercados nao mudam. Os candidatos so olham as combinacoes
com livro valido nas duas pontas; a etapa 2, por ativo, anda nos shorts do maior bid para o menor
e para no primeiro que nao chega ao minimo. As metricas do periodo sao incrementais (JanelaMetricas).

Nada aqui acessa a rede: tudo vem do estado (coletores) e dos dados lentos (funding e taxas).
Regra geral: dado em que nao se pode confiar (velho, sem liquidez, sem taxa, numero invalido)
nao gera candidato. Um erro inesperado numa combinacao (ou num ativo, na etapa 2) so tira aquela
combinacao do ciclo, com um aviso no log (uma vez por combinacao): as outras seguem.
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple

from scanner.calculo import (
    custo_taxas,
    fr_yield_8h,
    funding_esperado,
    preco_execucao,
    rp_bruto,
    rp_liquido,
    spread_abertura,
    spread_fechamento,
)
from scanner.coletores.dados_lentos import TaxaDesconhecida
from scanner.config.carregar import Config
from scanner.estado import EstadoMercados, pares_para_livro
from scanner.filtros import Filtros, custo_por_1000, passa
from scanner.metricas import JanelaMetricas
from scanner.modelos import Candidato, Ponta
from scanner.normalizador import ParNormalizado, preco_unitario_usdt
from scanner.sinais import Evento, GerenciadorSinais

registro = logging.getLogger("scanner.motor")

SEGUNDOS_POR_HORA = 3600

# Modos de uso aceitos na sessao (especificacao, secao 4).
MODOS = ("lucro", "farm")

# Qual campo das Metricas e o "F" do RP bruto, para cada opcao de metrica_spread (config ou filtros).
CAMPO_DO_F = {
    "medio": "fechamento_medio",
    "maior": "maior_fechamento",
    "media_10_maiores": "media_10_maiores_fechamento",
}


# ---------------------------------------------------------------------------
# Combinacoes
# ---------------------------------------------------------------------------


def pares_spot_futuro(mercados: dict[str, set[tuple[str, str]]]) -> list[tuple]:
    """Todas as combinacoes spot x futuro do mesmo ativo, inclusive na mesma corretora.

    mercados: {corretora: {(mercado, ativo), ...}}, ex.: {"gate": {("spot", "BTC")}}.
    Devolve (ativo, corretora do spot, "spot", corretora do futuro, "futuro"), em ordem
    alfabetica. O spot e sempre a ponta comprada (long): vender spot exigiria emprestimo.
    """
    futuros: dict[str, list[str]] = {}  # ativo -> corretoras que tem o futuro dele
    spots: list[tuple[str, str]] = []  # (ativo, corretora) de cada spot
    for corretora, pares in mercados.items():
        for mercado, ativo in pares:
            if mercado == "futuro":
                futuros.setdefault(ativo, []).append(corretora)
            elif mercado == "spot":
                spots.append((ativo, corretora))

    combinacoes = []
    for ativo, corretora_spot in sorted(spots):
        for corretora_futuro in sorted(futuros.get(ativo, [])):
            combinacoes.append((ativo, corretora_spot, "spot", corretora_futuro, "futuro"))
    return combinacoes


def pares_futuro_futuro(mercados: dict[str, set[tuple[str, str]]]) -> list[tuple]:
    """Todas as combinacoes futuro x futuro do mesmo ativo, sempre entre corretoras diferentes.

    mercados: {corretora: {(mercado, ativo), ...}}, como em pares_spot_futuro.
    Devolve (ativo, corretora do long, "futuro", corretora do short, "futuro") nas duas direcoes
    (gate x mexc e mexc x gate), em ordem alfabetica. Futuro pode ser vendido em qualquer
    corretora, entao as duas direcoes valem.
    """
    futuros: dict[str, list[str]] = {}  # ativo -> corretoras que tem o futuro dele
    for corretora, pares in mercados.items():
        for mercado, ativo in pares:
            if mercado == "futuro":
                futuros.setdefault(ativo, []).append(corretora)

    combinacoes = []
    for ativo in sorted(futuros):
        corretoras = sorted(futuros[ativo])
        for corretora_long in corretoras:
            for corretora_short in corretoras:
                if corretora_long != corretora_short:
                    combinacoes.append((ativo, corretora_long, "futuro", corretora_short, "futuro"))
    return combinacoes


def combinacoes(mercados: dict[str, set[tuple[str, str]]]) -> list[tuple]:
    """Todas as combinacoes que o scanner acompanha: primeiro as spot x futuro, depois as futuro x futuro.

    E a unica funcao que monta a lista completa: o motor (candidatos e etapa 2) e a escolha dos
    tickers usam esta, para os dois tipos andarem sempre juntos.
    """
    return pares_spot_futuro(mercados) + pares_futuro_futuro(mercados)


def chave_de(combo: tuple) -> tuple:
    """A chave do sinal de uma combinacao: igual a `Candidato.chave` do candidato que ela gera."""
    ativo, corretora_long, mercado_long, corretora_short, mercado_short = combo
    tipo = "spot_futuro" if mercado_long == "spot" else "futuro_futuro"
    return (f"{ativo}/USDT", tipo, corretora_long, mercado_long, corretora_short, mercado_short)


def texto_da_chave(chave: tuple) -> str:
    """A chave em texto, como fica gravada nas amostras do banco: "BTC/USDT|spot_futuro|gate|spot|bitget|futuro"."""
    return "|".join(map(str, chave))


def simbolos_para_tickers(vencedores: dict[str, dict[tuple[str, str], str]]) -> dict[str, list[str]]:
    """Quais simbolos cada corretora deve acompanhar na etapa 1 (tickers).

    vencedores: {corretora: {(mercado, ativo): simbolo do ccxt}} (ColetorCcxt.simbolos_por_chave).
    So entram os simbolos que formam pelo menos uma combinacao (spot x futuro ou futuro x futuro)
    com alguma corretora; o resto nunca viraria sinal. Uma corretora so de futuros (ex.: Hyperliquid)
    entra pelos pares futuro x futuro. Corretora sem nenhum simbolo util fica fora do resultado.
    """
    mercados = {corretora: set(chaves) for corretora, chaves in vencedores.items()}
    usados: dict[str, set[str]] = {}
    for ativo, corretora_long, mercado_long, corretora_short, mercado_short in combinacoes(mercados):
        usados.setdefault(corretora_long, set()).add(vencedores[corretora_long][(mercado_long, ativo)])
        usados.setdefault(corretora_short, set()).add(vencedores[corretora_short][(mercado_short, ativo)])
    return {corretora: sorted(simbolos) for corretora, simbolos in usados.items()}


# ---------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------


def _numero_valido(valor) -> bool:
    """True para numero real (int/float, nao bool, nao NaN nem infinito)."""
    if type(valor) is float:  # o caso comum (milhares de vezes por ciclo): uma checagem so
        return math.isfinite(valor)
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


def _todos_validos(*valores) -> bool:
    """True se todos os valores sao numero real (veja _numero_valido)."""
    for valor in valores:
        if not _numero_valido(valor):
            return False
    return True


def _preco_valido(preco, fator: float) -> float | None:
    """preco x fator (o preco em USDT), ou None se o preco ou o resultado nao e numero real maior que zero."""
    if not _numero_valido(preco):
        return None  # ex.: None ou texto
    convertido = preco * fator
    return convertido if _numero_valido(convertido) and convertido > 0 else None


def _nome_combo(combo: tuple) -> str:
    """Texto curto da combinacao para o log, ex.: "BTC gate/spot x bitget/futuro"."""
    ativo, corretora_long, mercado_long, corretora_short, mercado_short = combo
    return f"{ativo} {corretora_long}/{mercado_long} x {corretora_short}/{mercado_short}"


def _fator_usdt(ativo: str, cotacao: str | None, cotacoes: dict[str, float]) -> tuple[float, bool]:
    """(quanto vale 1 unidade da moeda de cotacao em USDT, aviso_cotacao).

    Mesma regra do normalizador (preco_unitario_usdt): USDT vale 1; outra moeda usa a cotacao
    do momento; sem cotacao, 1:1 com aviso. O livro do estado ja esta por 1 unidade do ativo,
    entao o multiplicador aqui e 1.
    """
    par = ParNormalizado(ativo=ativo, cotacao=cotacao, multiplicador=1.0, tamanho_contrato=1.0)
    return preco_unitario_usdt(1.0, par, cotacoes)


def _em_usdt(niveis: list[tuple[float, float]], fator: float) -> list[tuple[float, float]]:
    """Os niveis (preco, quantidade) com o preco em USDT. A quantidade (unidades do ativo) nao muda."""
    if fator == 1.0:
        return niveis  # USDT (o caso comum): nada a converter, nem copia
    return [(preco * fator, quantidade) for preco, quantidade in niveis]


def _executar(niveis: list[tuple[float, float]], tamanho_usd: float) -> float | None:
    """Preco medio comendo o livro ate `tamanho_usd`; None se nao ha liquidez ou o resultado nao e confiavel."""
    try:
        preco = preco_execucao(niveis, tamanho_usd)
    except ArithmeticError:  # ex.: preco infinito num nivel (divisao por zero dentro da conta)
        return None
    if preco is None or not _numero_valido(preco) or preco <= 0:
        return None
    return preco


def _agrupar(chaves) -> dict[str, set[tuple[str, str]]]:
    """Chaves (corretora, mercado, ativo) no formato que `combinacoes` recebe: {corretora: {(mercado, ativo)}}."""
    mercados: dict[str, set[tuple[str, str]]] = {}
    for corretora, mercado, ativo in chaves:
        mercados.setdefault(corretora, set()).add((mercado, ativo))
    return mercados


class _CombinacoesGuardadas:
    """`combinacoes(mercados)` com a ultima lista guardada: so monta de novo quando os mercados mudam.

    De um ciclo para o outro os mercados recebidos quase nunca mudam, e montar a lista
    (dezenas de milhares de combinacoes) custa bem mais que comparar os mercados.
    Cada item e (combo, chave do long, chave do short), com chave = (corretora, mercado, ativo),
    para quem percorre nao precisar montar as chaves a cada ciclo.
    Quem recebe a lista so a percorre (nunca altera).
    """

    def __init__(self):
        self._mercados: dict | None = None  # os mercados da ultima lista montada
        self._lista: list[tuple[tuple, tuple, tuple]] = []

    def obter(self, mercados: dict[str, set[tuple[str, str]]]) -> list[tuple[tuple, tuple, tuple]]:
        if mercados != self._mercados:
            self._mercados = mercados
            self._lista = [
                (combo, (combo[1], combo[2], combo[0]), (combo[3], combo[4], combo[0]))
                for combo in combinacoes(mercados)
            ]
        return self._lista


class _CombinacoesPorAtivo:
    """`combinacoes(mercados)` arrumadas para a etapa 2, guardadas enquanto os mercados nao mudam.

    Para cada ativo: {chave do long: {chave do short: combo}} (com quais shorts cada long forma
    combinacao) e a lista de todas as chaves de short. Chave = (corretora, mercado, ativo).
    """

    def __init__(self):
        self._mercados: dict | None = None  # os mercados da ultima arrumacao
        self._por_ativo: dict[str, tuple[dict, list]] = {}

    def obter(self, mercados: dict[str, set[tuple[str, str]]]) -> dict[str, tuple[dict, list]]:
        if mercados != self._mercados:
            self._mercados = mercados
            por_ativo: dict[str, tuple[dict, list]] = {}
            for combo in combinacoes(mercados):
                ativo, corretora_long, mercado_long, corretora_short, mercado_short = combo
                longs, shorts = por_ativo.setdefault(ativo, ({}, []))
                chave_short = (corretora_short, mercado_short, ativo)
                longs.setdefault((corretora_long, mercado_long, ativo), {})[chave_short] = combo
                if chave_short not in shorts:
                    shorts.append(chave_short)
            self._por_ativo = por_ativo
        return self._por_ativo


@dataclass
class Sessao:
    """O que o usuario escolheu no painel: modo, grupo de filtros e modo comparacao."""

    modo: str = "lucro"  # "lucro" ou "farm"
    filtros: Filtros = field(default_factory=Filtros)
    comparacao: bool = False  # True: decide pelo RP bruto, como o Arbimax (validacao lado a lado)


@dataclass
class _Lado:
    """Uma ponta lida do estado: o livro ja em USDT e os dados de apoio do ticker."""

    corretora: str
    mercado: str
    simbolo: str  # simbolo do ccxt, ex.: "BTC/USDT:USDT"
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    aviso_cotacao: bool
    volume_24h_usd: float | None
    variacao_24h_pct: float | None


class _PrecosDosTickers(NamedTuple):
    """Os tickers de agora na etapa 2, por mercado (corretora, mercado, ativo), com os precos em USDT.

    Um dicionario por campo, cada um com uma entrada para cada mercado. Lado que nao serve fica
    None: sem ask o mercado nao pode ser o long (que compra no ask); sem bid nao pode ser o short
    (que vende no bid). Ticker velho (ou conexao silenciosa) ou sem simbolo: os tres ficam None.
    """

    asks: dict[tuple[str, str, str], float | None]
    bids: dict[tuple[str, str, str], float | None]
    simbolos: dict[tuple[str, str, str], str | None]  # simbolo do ccxt, ex.: "BTC/USDT:USDT"


class _Historico:
    """Amostras do periodo de uma combinacao e o horario da ultima amostra.

    A janela (JanelaMetricas) guarda A, F e o lote de cada ponta de cada amostra, da as metricas
    e os lotes medios, e tira sozinha as amostras mais velhas que o periodo. As amostras chegam
    em ordem de horario.
    """

    def __init__(self, periodo_s: float):
        self.janela = JanelaMetricas(periodo_s)
        self.ultima_amostra: float | None = None  # horario da ultima amostra (None = nenhuma)

    def adicionar(self, ts: float, a: float, f: float, lote_long: float, lote_short: float) -> None:
        """Guarda uma amostra. A janela recusa NaN e infinito (ValueError): eles estragariam as medias."""
        self.janela.adicionar(ts, a, f, lote_long, lote_short)
        self.ultima_amostra = ts

    def lotes_medios(self, agora: float) -> tuple[float | None, float | None]:
        """(lote medio do long, lote medio do short) no periodo, em $; (None, None) sem amostras."""
        return self.janela.lotes_medios(agora)

    def vazio(self, agora: float) -> bool:
        """True se nao sobrou nenhuma amostra dentro do periodo."""
        return self.janela.vazia(agora)


# ---------------------------------------------------------------------------
# Motor
# ---------------------------------------------------------------------------


class Motor:
    """Calcula candidatos e sinais a partir do estado dos mercados.

    cfg: a Config (todos os limites vem dela).
    estado: EstadoMercados, preenchido pelos coletores.
    dados_lentos: DadosLentos (funding e taxas taker).
    banco: Banco opcional; com ele, `ciclo` grava eventos e amostras.
    cotacoes: funcao que devolve as cotacoes do momento, {moeda: valor em USDT}, ex.: {"USDC": 0.9998}.
        Padrao: nenhuma cotacao (pares fora de USDT usam 1:1 com aviso_cotacao).
    `sessao` guarda o modo, os filtros e o modo comparacao escolhidos no painel.
    """

    def __init__(
        self,
        cfg: Config,
        estado: EstadoMercados,
        dados_lentos,
        banco=None,
        cotacoes: Callable[[], dict[str, float]] = lambda: {},
    ):
        self.cfg = cfg
        self.estado = estado
        self.dados_lentos = dados_lentos
        self.banco = banco
        self._cotacoes = cotacoes
        self.sessao = Sessao()
        self._periodo_h = cfg.periodo_metricas_h  # periodo em uso (a sessao pode trocar)
        self.gerenciador = GerenciadorSinais(cfg.anti_repeticao_s, self._periodo_h)
        self._historicos: dict[tuple, _Historico] = {}  # chave do sinal -> amostras do periodo
        self._amostras_pendentes: list[tuple] = []  # amostras novas que `ciclo` ainda vai gravar no banco
        self._ultima_vez_acima: dict[tuple, float] = {}  # etapa 2: combo -> ultima vez acima do spread minimo
        # Etapa 2: combo com livro (as de _ultima_vez_acima) -> ((corretora, simbolo) do long, do short).
        self._simbolos_livro: dict[tuple, tuple[tuple[str, str], tuple[str, str]]] = {}
        self._avisados: set = set()  # avisos de log ja dados (cada um aparece uma vez so)
        # Combinacoes guardadas: uma lista para os candidatos (mercados com livro valido agora) e as
        # da etapa 2 arrumadas por ativo (todos os mercados com ticker guardado, validos ou nao: so
        # mudam quando entra ou sai um mercado). Separadas, para uma nao desfazer a outra.
        self._combos_candidatos = _CombinacoesGuardadas()
        self._combos_selecao = _CombinacoesPorAtivo()

    # --- API -----------------------------------------------------------------

    def candidatos(self, agora: float) -> list[Candidato]:
        """Os candidatos de agora (spot x futuro e futuro x futuro), ANTES dos filtros.

        Gera sempre os dois tipos: o filtro "tipos de oportunidade" e aplicado depois, em `passa`.
        Cada chamada cria objetos novos. Combinacao sem livro valido nas duas pontas, sem liquidez
        para o tamanho de referencia ou sem taxa taker conhecida nao gera candidato. Tambem
        acrescenta as amostras do periodo (a primeira quando a combinacao aparece; depois a cada
        `amostra_s`).
        """
        modo = self._modo_da_sessao()
        duracao_invertida_s, metrica = self._ajustar_sessao()
        cotacoes = self._cotacoes_validas()
        # Cada mercado e lido uma vez so; ficam os que tem livro valido agora.
        lados = self._lados_com_livro(agora, cotacoes)
        resultado = []
        vistas = set()
        # So as combinacoes com livro valido nas duas pontas: as outras nao gerariam candidato.
        for combo, chave_long, chave_short in self._combos_candidatos.obter(_agrupar(lados)):
            try:
                candidato = self._candidato(combo, lados[chave_long], lados[chave_short], agora, modo,
                                            duracao_invertida_s, metrica)
            except Exception:  # defeito inesperado nesta combinacao: so ela fica de fora
                self._avisar_uma_vez(("erro_candidato", combo),
                                     "%s: erro ao calcular o candidato; so esta combinacao fica de fora "
                                     "(aviso uma vez so)", _nome_combo(combo), exc_info=True)
                continue
            if candidato is not None:
                resultado.append(candidato)
                vistas.add(candidato.chave)
        self._esquecer_historicos(agora, vistas)
        return resultado

    def ciclo(self, agora: float) -> list[Evento]:
        """Um ciclo completo: candidatos -> filtros e modo -> gerenciador -> banco. Devolve os eventos."""
        candidatos = self.candidatos(agora)
        s = self.sessao
        modo = self._modo_da_sessao()
        aprovados = []
        for c in candidatos:
            try:
                if passa(c, s.filtros, modo, self.cfg, s.comparacao):
                    aprovados.append(c)
            except Exception:  # defeito inesperado nos filtros deste candidato: so ele fica de fora
                self._avisar_uma_vez(("erro_filtros", c.chave),
                                     "%s %s: erro ao aplicar os filtros; so este candidato fica de fora "
                                     "(aviso uma vez so)", c.par, texto_da_chave(c.chave), exc_info=True)
        eventos = self.gerenciador.processar(aprovados, agora)
        amostras, self._amostras_pendentes = self._amostras_pendentes, []
        if self.banco is not None:
            self._gravar(eventos, amostras)
        return eventos

    def sinais_ativos(self) -> list[dict]:
        """Os sinais ativos agora, no formato JSON da spec (secao 5)."""
        return self.gerenciador.ativos()

    def selecao_livros(self, agora: float) -> dict[str, set[str]]:
        """Etapa 2: os simbolos que devem ter livro completo, por corretora.

        Para as combinacoes (spot x futuro e futuro x futuro) calcula o spread de abertura com os
        TICKERS (ask do long e bid do short, em USDT). Quem passa de `spread_min_livro_pct` ganha o
        livro das duas pontas e o mantem por `manter_livro_s` depois de cair abaixo (mesmo se o
        ticker sumir nesse tempo). O filtro "tipos de oportunidade" nao muda quais livros abrem.
        Combinacao que nao chega ao minimo nem precisa ter o spread calculado: para cada long, os
        shorts sao olhados do maior bid para o menor, e o spread so cai quando o bid cai.
        """
        cotacoes = self._cotacoes_validas()
        minimo = self.cfg.spread_min_livro_pct
        # As combinacoes saem de TODOS os mercados com ticker guardado: ticker que fica velho e volta
        # (comum) nao obriga a arruma-las de novo. Quem nao tem preco valido agora e pulado no laco
        # (precos tem uma entrada para cada mercado).
        mercados = self.estado.mercados()
        precos = self._precos_dos_tickers(mercados, agora, cotacoes)  # cada mercado lido uma vez so
        asks, bids = precos.asks, precos.bids
        # So as combinacoes que chegam ao minimo agora: as outras nao mudam nada em pares_para_livro.
        acima: dict[tuple, float] = {}  # combo -> spread
        for ativo, (longs, shorts) in self._combos_selecao.obter(mercados).items():  # um ativo por vez
            try:
                self._acima_do_minimo(longs, shorts, asks, bids, minimo, acima)
            except Exception:  # defeito inesperado neste ativo: so ele fica fora da etapa 2 neste ciclo
                self._avisar_uma_vez(("erro_selecao", ativo),
                                     "%s: erro na etapa 2 (escolha dos livros); este ativo fica de fora "
                                     "(aviso uma vez so)", ativo, exc_info=True)

        escolhidos = pares_para_livro(acima, minimo, self._ultima_vez_acima, agora, self.cfg.manter_livro_s)
        # Simbolos das escolhidas: os do ticker de agora (mesma condicao do spread acima); se o
        # ticker sumiu, ficam os da ultima vez.
        for combo in escolhidos:
            ativo, corretora_long, mercado_long, corretora_short, mercado_short = combo
            chave_long, chave_short = (corretora_long, mercado_long, ativo), (corretora_short, mercado_short, ativo)
            # .get: o mercado pode ter saido do estado (corretora desconectada) e a combinacao seguir mantida.
            if asks.get(chave_long) is not None and bids.get(chave_short) is not None:
                self._simbolos_livro[combo] = ((corretora_long, precos.simbolos[chave_long]),
                                               (corretora_short, precos.simbolos[chave_short]))
        # Esquece os simbolos de quem saiu de vez (pares_para_livro ja tirou do _ultima_vez_acima).
        for combo in [c for c in self._simbolos_livro if c not in self._ultima_vez_acima]:
            del self._simbolos_livro[combo]

        selecao: dict[str, set[str]] = {}
        for combo in escolhidos:
            for corretora, simbolo in self._simbolos_livro.get(combo, ()):
                selecao.setdefault(corretora, set()).add(simbolo)
        return selecao

    @staticmethod
    def _acima_do_minimo(longs: dict, shorts: list, asks: dict, bids: dict, minimo: float,
                         acima: dict[tuple, float]) -> None:
        """Etapa 2 de um ativo: anota em `acima` as combinacoes com spread de abertura (tickers) >= minimo."""
        # Os shorts do ativo com bid valido, do maior bid para o menor.
        vendas = sorted(((bids[chave], chave) for chave in shorts if bids[chave] is not None), reverse=True)
        for chave_long, combos_do_long in longs.items():
            ask_long = asks[chave_long]
            if ask_long is None:
                continue  # sem ask valido agora: nao serve de long
            for bid_short, chave_short in vendas:
                combo = combos_do_long.get(chave_short)
                if combo is None:
                    continue  # nao forma combinacao com este long (ex.: futuro x futuro na mesma corretora)
                spread = spread_abertura(ask_long, bid_short)
                if spread < minimo:
                    break  # os proximos shorts tem bid menor ou igual: spread menor ou igual
                acima[combo] = spread

    def status(self, agora: float) -> dict[str, dict]:
        """Saude de cada corretora da config, para o painel (/api/status).

        Ex.: {"gate": {"estado": "conectada", "atraso_medio_ms": 180.5, "reconexoes": 2}}. estado:
        conectada | reconectando | atrasada; atraso_medio_ms None se a corretora nao informa o horario.
        """
        return {corretora: self.estado.saude(corretora, agora) for corretora in self.cfg.corretoras}

    def quantidade_historicos(self) -> int:
        """Quantas combinacoes tem amostras do periodo na memoria (vai para a linha de recursos do log)."""
        return len(self._historicos)

    def precarregar_amostra(self, chave: tuple, ts: float, a: float, f: float,
                            lote_long: float, lote_short: float) -> None:
        """Acrescenta uma amostra ja conhecida ao periodo da `chave`, sem grava-la no banco (ex.: testes).

        As amostras precisam vir em ordem de horario. NaN ou infinito levantam ValueError.
        """
        historico = self._historicos.get(chave) or _Historico(self._periodo_h * SEGUNDOS_POR_HORA)
        historico.adicionar(ts, a, f, lote_long, lote_short)
        self._historicos[chave] = historico

    # --- Sessao --------------------------------------------------------------

    def _modo_da_sessao(self) -> str:
        """O modo da sessao ("lucro" ou "farm"); um modo invalido usa "lucro" e avisa uma vez."""
        return self._conferir("modo", self.sessao.modo, lambda v: v in MODOS, "lucro")

    def _ajustar_sessao(self) -> tuple[float, str]:
        """Le e confere os ajustes da sessao que o motor aplica. Devolve (duracao de invertida em s, metrica).

        O periodo das metricas, se mudar, recomeca todas as janelas (as amostras antigas foram
        resumidas com outro periodo) e passa o periodo novo ao gerenciador de sinais.
        """
        f = self.sessao.filtros
        periodo_h = self._conferir("periodo_metricas_h", f.periodo_metricas_h,
                                   lambda v: _numero_valido(v) and v > 0, self.cfg.periodo_metricas_h)
        duracao_s = self._conferir("invertidas_duracao_min_s", f.invertidas_duracao_min_s,
                                   lambda v: _numero_valido(v) and v >= 0, 0)
        metrica = self._conferir("metrica_spread", f.metrica_spread,
                                 lambda v: isinstance(v, str) and v in CAMPO_DO_F, self.cfg.metrica_spread)
        if periodo_h != self._periodo_h:
            registro.info("periodo das metricas mudou de %sh para %sh: as janelas recomecam do zero",
                          self._periodo_h, periodo_h)
            self._periodo_h = periodo_h
            self._historicos.clear()
            self.gerenciador.periodo_h = periodo_h
        return duracao_s, metrica

    def _conferir(self, campo: str, valor, valido: Callable[[object], bool], padrao):
        """O valor da sessao se for valido; vazio (None ou "") ou invalido usa `padrao` (invalido avisa uma vez)."""
        if valor is None or valor == "":
            return padrao
        if valido(valor):
            return valor
        self._avisar_uma_vez(("sessao", campo, repr(valor)),
                             "sessao: %s=%r invalido; usando %r", campo, valor, padrao)
        return padrao

    def _avisar_uma_vez(self, chave, mensagem: str, *argumentos, exc_info: bool = False) -> None:
        if chave not in self._avisados:
            self._avisados.add(chave)
            registro.warning(mensagem, *argumentos, exc_info=exc_info)

    def _cotacoes_validas(self) -> dict[str, float]:
        """As cotacoes do momento que sao numero real maior que zero (as outras contam como "sem cotacao")."""
        return {
            moeda: float(valor)
            for moeda, valor in (self._cotacoes() or {}).items()
            if _numero_valido(valor) and valor > 0
        }

    # --- Leitura do estado (cada mercado uma vez por ciclo) ------------------

    def _lados_com_livro(self, agora: float, cotacoes: dict[str, float]) -> dict[tuple[str, str, str], _Lado]:
        """{(corretora, mercado, ativo): lado} dos mercados com livro valido agora, ja em USDT.

        Mercado sem livro valido (velho, conexao silenciosa ou nunca chegou) fica de fora.
        """
        lados = {}
        for corretora, pares in self.estado.mercados().items():
            for mercado, ativo in pares:
                lado = self._ler_lado(corretora, mercado, ativo, agora, cotacoes)
                if lado is not None:
                    lados[(corretora, mercado, ativo)] = lado
        return lados

    def _precos_dos_tickers(self, mercados: dict[str, set[tuple[str, str]]], agora: float,
                            cotacoes: dict[str, float]) -> _PrecosDosTickers:
        """Ask, bid (em USDT) e simbolo do ticker de agora de CADA um dos `mercados` (veja _PrecosDosTickers).

        ask ou bid que nao e numero real maior que zero (inclusive None ou texto, se um coletor
        escrever lixo no estado) vira None so daquele lado, e o resto da etapa 2 segue normal.
        """
        asks, bids, simbolos = {}, {}, {}
        fatores: dict[str | None, float] = {}  # moeda de cotacao -> fator: a conta so depende da moeda
        for corretora, pares in mercados.items():
            for mercado, ativo in pares:
                chave = (corretora, mercado, ativo)
                ticker = self.estado.obter_ticker(corretora, mercado, ativo, agora)
                if ticker is None or ticker.get("simbolo_original") is None:
                    asks[chave] = bids[chave] = simbolos[chave] = None  # ticker que nao serve agora
                    continue
                cotacao = ticker.get("cotacao")
                if cotacao not in fatores:
                    fatores[cotacao], _ = _fator_usdt(ativo, cotacao, cotacoes)
                asks[chave] = _preco_valido(ticker.get("ask"), fatores[cotacao])
                bids[chave] = _preco_valido(ticker.get("bid"), fatores[cotacao])
                simbolos[chave] = ticker["simbolo_original"]
        return _PrecosDosTickers(asks, bids, simbolos)

    # --- Um candidato --------------------------------------------------------

    def _candidato(self, combo: tuple, long: _Lado, short: _Lado, agora: float, modo: str,
                   duracao_invertida_s: float, metrica: str) -> Candidato | None:
        """O candidato da combinacao, com o livro das duas pontas ja lido (long e short), ou None."""
        ativo, corretora_long, mercado_long, corretora_short, mercado_short = combo

        # Abrir: o long compra no ask e o short vende no bid. Fechar: o long vende no bid e o short recompra no ask.
        tamanho = self.cfg.tamanho_referencia_usd
        ask_long, bid_short = _executar(long.asks, tamanho), _executar(short.bids, tamanho)
        bid_long, ask_short = _executar(long.bids, tamanho), _executar(short.asks, tamanho)
        if None in (ask_long, bid_short, bid_long, ask_short):
            return None  # o livro nao aguenta o tamanho de referencia
        a = spread_abertura(ask_long, bid_short)
        f = spread_fechamento(bid_long, ask_short)
        # Lote: quanto vale (em $) o melhor nivel do lado executado ao abrir.
        lote_long = long.asks[0][0] * long.asks[0][1]
        lote_short = short.bids[0][0] * short.bids[0][1]
        if not _todos_validos(a, f, lote_long, lote_short):
            return None

        taxa_long = self._taxa(corretora_long, mercado_long, combo)
        taxa_short = self._taxa(corretora_short, mercado_short, combo)
        if taxa_long is None or taxa_short is None:
            return None
        fr_long, intervalo_long = self._funding(corretora_long, mercado_long, ativo)
        fr_short, intervalo_short = self._funding(corretora_short, mercado_short, ativo)

        historico = self._amostrar(chave_de(combo), agora, a, f, ask_long, bid_short, lote_long, lote_short)
        metricas = historico.janela.resumo(agora, duracao_invertida_s)  # objeto compartilhado: so leitura
        lote_medio_long, lote_medio_short = historico.lotes_medios(agora)
        # Sem amostra no periodo (periodo menor que amostra_s), o F da metrica e o F atual.
        f_metrica = f if metricas is None else getattr(metricas, CAMPO_DO_F[metrica])

        custo = custo_taxas(taxa_long, taxa_short)
        funding = funding_esperado(fr_short, intervalo_short, fr_long, intervalo_long, self.cfg.tempo_operacao_h)
        bruto = rp_bruto(a, f_metrica)
        liquido = rp_liquido(bruto, custo, self.cfg.margem_slippage_pct, funding)
        rendimento_8h = fr_yield_8h(fr_short, intervalo_short, fr_long, intervalo_long)
        if not _todos_validos(bruto, liquido, rendimento_8h):
            return None

        return Candidato(
            par=f"{ativo}/USDT",
            tipo="spot_futuro" if mercado_long == "spot" else "futuro_futuro",
            modo=modo,
            etiquetas=[],  # STOCK (acao tokenizada) ainda nao e identificada na v1
            suspeito=a > self.cfg.limite_suspeito_pct,
            aviso_cotacao=long.aviso_cotacao or short.aviso_cotacao,
            ponta_long=self._ponta(long, ask_long, lote_long, lote_medio_long, taxa_long, fr_long, intervalo_long),
            ponta_short=self._ponta(short, bid_short, lote_short, lote_medio_short, taxa_short, fr_short,
                                    intervalo_short),
            spread_abertura_pct=a,
            spread_fechamento_pct=f,
            metricas_periodo=metricas,
            fr_yield_8h_pct=rendimento_8h,
            rp_bruto_pct=bruto,
            custo_taxas_pct=custo,
            funding_esperado_pct=funding,
            rp_liquido_pct=liquido,
            custo_por_1000_usd=custo_por_1000(liquido) if modo == "farm" else None,
            tamanho_referencia_usd=self.cfg.tamanho_referencia_usd,
        )

    def _ler_lado(self, corretora: str, mercado: str, ativo: str, agora: float,
                  cotacoes: dict[str, float]) -> _Lado | None:
        """O livro da ponta em USDT, ou None se nao ha livro valido.

        O ticker guardado (mesmo velho) so fornece dados de apoio: a moeda de cotacao, o volume
        e a variacao 24h. O preco vem sempre do livro, que o estado so entrega se estiver valido.
        """
        livro = self.estado.obter_livro(corretora, mercado, ativo, agora)
        if livro is None:
            return None  # o caso mais comum (so os pares interessantes tem livro): nem le o ticker
        ticker = self.estado.ticker_guardado(corretora, mercado, ativo)
        if ticker is None:
            return None
        fator, aviso = _fator_usdt(ativo, ticker.get("cotacao"), cotacoes)
        volume = ticker.get("volume_24h_usd")
        variacao = ticker.get("variacao_24h_pct")
        return _Lado(
            corretora=corretora,
            mercado=mercado,
            simbolo=livro.simbolo_original,
            bids=_em_usdt(livro.bids, fator),
            asks=_em_usdt(livro.asks, fator),
            aviso_cotacao=aviso,
            volume_24h_usd=volume * fator if _numero_valido(volume) else None,  # o volume vem na moeda de cotacao
            variacao_24h_pct=variacao if _numero_valido(variacao) else None,
        )

    def _taxa(self, corretora: str, mercado: str, combo: tuple) -> float | None:
        """Taxa taker em %, ou None (a combinacao fica de fora) se desconhecida ou invalida. Avisa uma vez."""
        try:
            taxa = self.dados_lentos.taxa_taker(corretora, mercado)
        except TaxaDesconhecida as erro:
            self._avisar_uma_vez(("taxa", corretora, mercado),
                                 "%s: %s; as combinacoes com %s/%s ficam de fora ate a taxa chegar",
                                 _nome_combo(combo), erro, corretora, mercado)
            return None
        if not (_numero_valido(taxa) and taxa >= 0):
            self._avisar_uma_vez(("taxa_invalida", corretora, mercado),
                                 "%s: taxa taker invalida de %s/%s (%r); as combinacoes com ela ficam de fora",
                                 _nome_combo(combo), corretora, mercado, taxa)
            return None
        return taxa

    def _funding(self, corretora: str, mercado: str, ativo: str) -> tuple[float | None, float | None]:
        """(funding em %, intervalo em horas) do futuro; (None, None) no spot ou se desconhecido/invalido."""
        if mercado != "futuro":
            return None, None
        dado = self.dados_lentos.funding(corretora, ativo)
        if dado is None:
            return None, None
        taxa, intervalo = dado
        if not (_numero_valido(taxa) and _numero_valido(intervalo) and intervalo > 0):
            return None, None
        return taxa, intervalo

    @staticmethod
    def _ponta(lado: _Lado, preco: float, lote: float, lote_medio: float | None, taxa: float,
               funding: float | None, intervalo: float | None) -> Ponta:
        return Ponta(
            corretora=lado.corretora,
            mercado=lado.mercado,
            simbolo_original=lado.simbolo,
            preco=preco,
            lote_usd=lote,
            lote_medio_usd=lote_medio,
            volume_24h_usd=lado.volume_24h_usd,
            funding_pct=funding,
            funding_intervalo_h=intervalo,
            limite_ordem_usd=None,  # v1: o limite de ordem da corretora ainda nao e lido
            variacao_24h_pct=lado.variacao_24h_pct,
            taxa_taker_pct=taxa,
        )

    # --- Amostras e banco ----------------------------------------------------

    def _amostrar(self, chave: tuple, agora: float, a: float, f: float, preco_long: float,
                  preco_short: float, lote_long: float, lote_short: float) -> _Historico:
        """Acrescenta a amostra de agora se a chave acabou de aparecer ou se ja passou `amostra_s`."""
        historico = self._historicos.get(chave)
        if historico is None:
            historico = self._historicos[chave] = _Historico(self._periodo_h * SEGUNDOS_POR_HORA)
        if historico.ultima_amostra is None or agora - historico.ultima_amostra >= self.cfg.amostra_s:
            historico.adicionar(agora, a, f, lote_long, lote_short)
            self._amostras_pendentes.append((chave, agora, a, f, preco_long, preco_short, lote_long, lote_short))
        return historico

    def _esquecer_historicos(self, agora: float, vistas: set) -> None:
        """Apaga o historico de quem nao apareceu agora e ja nao tem amostra no periodo (a memoria nao cresce sem fim)."""
        for chave in [c for c, h in self._historicos.items() if c not in vistas and h.vazio(agora)]:
            del self._historicos[chave]

    def _gravar(self, eventos: list[Evento], amostras: list[tuple]) -> None:
        """Grava eventos e amostras do ciclo, cada grupo numa transacao so (um acesso ao disco, e nao um por item).

        Erro no banco vai para o log e nao para o scanner: o painel ao vivo continua. Um evento que o
        banco recusa (ex.: JSON invalido) vai para o log sem impedir os outros.
        """
        try:
            recusados = self.banco.gravar_eventos(eventos)
        except Exception:
            registro.exception("nao consegui gravar %d eventos no banco", len(eventos))
        else:
            for evento, erro in recusados:
                registro.error("nao consegui gravar o evento %s do sinal %s no banco (%s: %s)",
                               evento.tipo, evento.sinal.get("id"), type(erro).__name__, erro)
        try:
            self.banco.gravar_amostras([(texto_da_chave(chave), *resto) for chave, *resto in amostras])
        except Exception:
            registro.exception("nao consegui gravar %d amostras no banco", len(amostras))
