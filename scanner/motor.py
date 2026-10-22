"""Motor: transforma o estado dos mercados em candidatos e sinais (especificacao, secoes 3 a 5).

A cada ciclo (ciclo_ms da config, padrao 500 ms) o __main__ chama:
  1. `ciclo(agora)`: monta os candidatos spot x futuro, aplica os filtros e o modo da sessao,
     passa ao gerenciador de sinais e grava eventos e amostras no banco;
  2. `selecao_livros(agora)`: etapa 2 da coleta, diz de quais pares abrir o livro completo.

Precos: o estado guarda os livros com preco por 1 unidade do ativo, mas ainda na moeda de
cotacao do par (USDT, USDC, USDG). A troca para USDT acontece aqui, com as cotacoes do momento;
sem cotacao valida, usa 1:1 e marca o candidato com aviso_cotacao.

Combinacao ("combo"): (ativo, corretora long, mercado long, corretora short, mercado short),
ex.: ("BTC", "gate", "spot", "bitget", "futuro"). No spot x futuro o spot e sempre o long.

Nada aqui acessa a rede: tudo vem do estado (coletores) e dos dados lentos (funding e taxas).
Regra geral: dado em que nao se pode confiar (velho, sem liquidez, sem taxa, numero invalido)
nao gera candidato.
"""

import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

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
    So entram os simbolos que formam pelo menos uma combinacao spot x futuro com alguma corretora;
    o resto nunca viraria sinal. Corretora sem nenhum simbolo util fica fora do resultado.
    """
    mercados = {corretora: set(chaves) for corretora, chaves in vencedores.items()}
    usados: dict[str, set[str]] = {}
    for ativo, corretora_long, mercado_long, corretora_short, mercado_short in pares_spot_futuro(mercados):
        usados.setdefault(corretora_long, set()).add(vencedores[corretora_long][(mercado_long, ativo)])
        usados.setdefault(corretora_short, set()).add(vencedores[corretora_short][(mercado_short, ativo)])
    return {corretora: sorted(simbolos) for corretora, simbolos in usados.items()}


# ---------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------


def _numero_valido(valor) -> bool:
    """True para numero real (int/float, nao bool, nao NaN nem infinito)."""
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


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


class _Historico:
    """Amostras do periodo de uma combinacao: A e F (para as metricas) e o lote de cada ponta (lote medio).

    As amostras chegam em ordem de horario. As que ficam mais velhas que o periodo saem.
    """

    def __init__(self, periodo_s: float):
        self.periodo_s = periodo_s
        self.janela = JanelaMetricas(periodo_s)
        self.ultima_amostra: float | None = None  # horario da ultima amostra (None = nenhuma)
        self._lotes: deque[tuple[float, float, float]] = deque()  # (ts, lote_long, lote_short)
        # Somas dos lotes no periodo, atualizadas a cada entrada/saida: a media sai sem percorrer tudo.
        self._soma_long = 0.0
        self._soma_short = 0.0

    def adicionar(self, ts: float, a: float, f: float, lote_long: float, lote_short: float) -> None:
        """Guarda uma amostra. Recusa NaN e infinito (ValueError): eles estragariam as medias."""
        if not all(_numero_valido(v) for v in (ts, a, f, lote_long, lote_short)):
            raise ValueError(f"amostra com numero invalido: ts={ts} a={a} f={f} lotes={lote_long}/{lote_short}")
        self.janela.adicionar(ts, a, f)
        self._lotes.append((ts, lote_long, lote_short))
        self._soma_long += lote_long
        self._soma_short += lote_short
        self.ultima_amostra = ts
        self._descartar_antigas(ts)

    def _descartar_antigas(self, agora: float) -> None:
        limite = agora - self.periodo_s
        while self._lotes and self._lotes[0][0] < limite:
            _, lote_long, lote_short = self._lotes.popleft()
            self._soma_long -= lote_long
            self._soma_short -= lote_short
        if not self._lotes:  # zera de verdade, sem sobra de arredondamento
            self._soma_long = self._soma_short = 0.0

    def lotes_medios(self, agora: float) -> tuple[float | None, float | None]:
        """(lote medio do long, lote medio do short) no periodo, em $; (None, None) sem amostras."""
        self._descartar_antigas(agora)
        if not self._lotes:
            return None, None
        n = len(self._lotes)
        return self._soma_long / n, self._soma_short / n

    def vazio(self, agora: float) -> bool:
        """True se nao sobrou nenhuma amostra dentro do periodo."""
        self._descartar_antigas(agora)
        return not self._lotes


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
        self._simbolos_livro: dict[tuple, tuple[tuple[str, str], tuple[str, str]]] = {}  # etapa 2: combo -> simbolos
        self._avisados: set = set()  # avisos de log ja dados (cada um aparece uma vez so)

    # --- API -----------------------------------------------------------------

    def candidatos(self, agora: float) -> list[Candidato]:
        """Os candidatos spot x futuro de agora, ANTES dos filtros. Cada chamada cria objetos novos.

        Combinacao sem livro valido nas duas pontas, sem liquidez para o tamanho de referencia
        ou sem taxa taker conhecida nao gera candidato. Tambem acrescenta as amostras do periodo
        (a primeira quando a combinacao aparece; depois a cada `amostra_s`).
        """
        modo = self._modo_da_sessao()
        duracao_invertida_s, metrica = self._ajustar_sessao()
        cotacoes = self._cotacoes_validas()
        resultado = []
        vistas = set()
        for combo in pares_spot_futuro(self.estado.mercados()):
            candidato = self._candidato(combo, agora, cotacoes, modo, duracao_invertida_s, metrica)
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
        aprovados = [c for c in candidatos if passa(c, s.filtros, modo, self.cfg, s.comparacao)]
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

        Para cada combinacao calcula o spread de abertura com os TICKERS (ask do long e bid do
        short, em USDT). Quem passa de `spread_min_livro_pct` ganha o livro das duas pontas e o
        mantem por `manter_livro_s` depois de cair abaixo (mesmo se o ticker sumir nesse tempo).
        """
        cotacoes = self._cotacoes_validas()
        spreads: dict[tuple, float] = {}
        for combo in pares_spot_futuro(self.estado.mercados()):
            ativo, corretora_long, mercado_long, corretora_short, mercado_short = combo
            ticker_long = self.estado.obter_ticker(corretora_long, mercado_long, ativo, agora)
            ticker_short = self.estado.obter_ticker(corretora_short, mercado_short, ativo, agora)
            if ticker_long is None or ticker_short is None:
                continue
            simbolo_long, simbolo_short = ticker_long.get("simbolo_original"), ticker_short.get("simbolo_original")
            if simbolo_long is None or simbolo_short is None:
                continue
            fator_long, _ = _fator_usdt(ativo, ticker_long.get("cotacao"), cotacoes)
            fator_short, _ = _fator_usdt(ativo, ticker_short.get("cotacao"), cotacoes)
            ask_long, bid_short = ticker_long["ask"] * fator_long, ticker_short["bid"] * fator_short
            if not (_numero_valido(ask_long) and _numero_valido(bid_short) and ask_long > 0 and bid_short > 0):
                continue
            spreads[combo] = spread_abertura(ask_long, bid_short)
            self._simbolos_livro[combo] = ((corretora_long, simbolo_long), (corretora_short, simbolo_short))

        escolhidos = pares_para_livro(spreads, self.cfg.spread_min_livro_pct, self._ultima_vez_acima,
                                      agora, self.cfg.manter_livro_s)
        # Esquece os simbolos de quem saiu de vez (pares_para_livro ja tirou do _ultima_vez_acima).
        for combo in [c for c in self._simbolos_livro if c not in self._ultima_vez_acima]:
            del self._simbolos_livro[combo]

        selecao: dict[str, set[str]] = {}
        for combo in escolhidos:
            for corretora, simbolo in self._simbolos_livro.get(combo, ()):
                selecao.setdefault(corretora, set()).add(simbolo)
        return selecao

    def status(self, agora: float) -> dict[str, dict]:
        """Saude de cada corretora da config: {"gate": {"estado": "conectada" | "atrasada"}}."""
        return {
            corretora: {"estado": "conectada" if self.estado.conexao_viva(corretora, agora) else "atrasada"}
            for corretora in self.cfg.corretoras
        }

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

    def _avisar_uma_vez(self, chave, mensagem: str, *argumentos) -> None:
        if chave not in self._avisados:
            self._avisados.add(chave)
            registro.warning(mensagem, *argumentos)

    def _cotacoes_validas(self) -> dict[str, float]:
        """As cotacoes do momento que sao numero real maior que zero (as outras contam como "sem cotacao")."""
        return {
            moeda: float(valor)
            for moeda, valor in (self._cotacoes() or {}).items()
            if _numero_valido(valor) and valor > 0
        }

    # --- Um candidato --------------------------------------------------------

    def _candidato(self, combo: tuple, agora: float, cotacoes: dict[str, float], modo: str,
                   duracao_invertida_s: float, metrica: str) -> Candidato | None:
        ativo, corretora_long, mercado_long, corretora_short, mercado_short = combo
        long = self._ler_lado(corretora_long, mercado_long, ativo, agora, cotacoes)
        short = self._ler_lado(corretora_short, mercado_short, ativo, agora, cotacoes)
        if long is None or short is None:
            return None  # sem livro valido (velho, conexao silenciosa ou nunca chegou)

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
        if not all(_numero_valido(v) for v in (a, f, lote_long, lote_short)):
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
        if not all(_numero_valido(v) for v in (bruto, liquido, rendimento_8h)):
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
        ticker = self.estado.ticker_guardado(corretora, mercado, ativo)
        if livro is None or ticker is None:
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
        """Grava eventos e amostras. Erro no banco vai para o log e nao para o scanner: o painel ao vivo continua."""
        for evento in eventos:
            try:
                self.banco.gravar_evento(evento)
            except Exception:
                registro.exception("nao consegui gravar o evento %s do sinal %s no banco",
                                   evento.tipo, evento.sinal.get("id"))
        try:
            self.banco.gravar_amostras([(texto_da_chave(chave), *resto) for chave, *resto in amostras])
        except Exception:
            registro.exception("nao consegui gravar %d amostras no banco", len(amostras))
