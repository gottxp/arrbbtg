"""Janela de metricas do periodo: A e F medios e maiores, media dos 10 maiores, invertidas e lotes medios.

A = spread de abertura e F = spread de fechamento, ambos em %. Tudo em memoria e sem rede.
O motor guarda uma JanelaMetricas por combinacao. As amostras chegam em ordem crescente de ts e vem de
duas fontes (especificacao, secao 3, "De onde vem as amostras das metricas"):
- do LIVRO (`adicionar`), a cada `amostra_s` segundos enquanto o par tem livro aberto: A, F e o lote de
  cada ponta;
- pelos TICKERS (`adicionar_pelo_ticker`), a cada `amostra_ticker_s` segundos enquanto o par nao tem livro
  aberto: so A e F (o ticker, melhor bid/ask, nao tem lote).
Uma fonte por vez: o motor nunca manda as duas no mesmo horario. Na janela:
- as medias de A e F valem sobre todas as amostras, cada uma pesando o tempo que ela cobre: uma amostra pelo
  ticker vale `peso_ticker` amostras do livro (amostra_ticker_s / amostra_s; ex.: 120 s / 5 s = 24). Sem esse
  peso, a hora com livro aberto (720 amostras de 5 s) pesaria 24 vezes mais que a hora sem livro (30 amostras de
  120 s) e o F medio voltaria a ser "o F desde que o livro abriu";
- o maior A e F, a media dos 10 maiores e as invertidas valem sobre todas as amostras, em ordem de horario;
- o lote medio vem so das amostras do livro.
Sem amostras pelo ticker, tudo da exatamente o mesmo de antes (a media e a soma dividida pela quantidade).

Com 4 h de periodo e uma amostra do livro a cada 5 s sao 2.880 amostras por combinacao; pelos tickers, o motor
acompanha dezenas de milhares de combinacoes. Por isso a janela e INCREMENTAL e COMPACTA:
- cada amostra nova so mexe no que entrou e no que saiu, sem olhar a janela inteira:
  somas exatas para as medias, a lista curta dos maiores valores (refeita olhando a janela toda
  so em caso raro, veja _maiores_entrou) e as sequencias de invertidas;
- os numeros ficam em arrays de 8 bytes por numero (array("d")), e nao em tuplas de floats soltos; a amostra
  pelo ticker ocupa 3 numeros (ts, A, F) e a do livro 5 (mais os dois lotes);
- uma tabela vazia nao guarda nenhum array (a janela de um par so com tickers nao paga as colunas do livro).
O resultado e o mesmo de recalcular do zero (os testes comparam com o calculo direto).

O motor chama `resumo` a cada 500 ms, mas as amostras mudam so a cada 5 s. Por isso o ultimo
resumo fica guardado (cache) e so e montado de novo quando a janela muda ou quando a duracao
minima de invertida muda.
"""

import heapq
import math
from array import array
from bisect import bisect_left, insort
from dataclasses import dataclass
from itertools import chain

# Quantos valores entram na metrica "media dos 10 maiores". Faz parte do nome da metrica
# (media_10_maiores), por isso nao vem da Config.
MAIORES_N = 10

# Quantos dos maiores valores de A (e de F) ficam guardados. Basta ter os 10 maiores, mas guardar
# alguns a mais faz a lista quase nunca precisar ser refeita olhando a janela inteira (veja _maiores_entrou).
MAIORES_GUARDADOS = 50

# De quantas em quantas linhas que sairam a tabela libera o espaco delas (veja _Colunas). Numa tabela pequena
# (ex.: as 120 amostras pelo ticker de 4 h), libera antes: quando as que sairam passam de 1/8 das que ficaram.
LIMPAR_A_CADA = 256

# Todo float e uma fracao exata cujo denominador e uma potencia de 2 de no maximo 2**1074.
# Multiplicado por 2**1074, vira um numero inteiro exato (veja _SomaExata).
_BITS_ESCALA = 1074
_ESCALA = 1 << _BITS_ESCALA


@dataclass
class Metricas:
    """Resumo das amostras que estao dentro do periodo."""

    abertura_media: float  # media de A
    fechamento_medio: float  # media de F
    maior_abertura: float  # maior A do periodo
    maior_fechamento: float  # maior F do periodo (o menos negativo)
    media_10_maiores_abertura: float  # media dos 10 maiores A (ou de todos, se houver menos de 10)
    media_10_maiores_fechamento: float  # media dos 10 maiores F (ou de todos, se houver menos de 10)
    invertidas: int  # quantas invertidas contaram no periodo


# ---------------------------------------------------------------------------
# Pecas da janela
# ---------------------------------------------------------------------------


class _Colunas:
    """Linhas de numeros reais guardadas num array("d") so, 8 bytes por numero, uma linha depois da outra.

    Cada linha tem `largura` numeros (ex.: ts, A, F). As linhas entram no fim e saem do comeco (uma fila).
    Tirar do comeco de um array obrigaria a mover todos os outros numeros, entao so avancamos `inicio` (a
    posicao da primeira linha que ainda esta aqui) e, a cada LIMPAR_A_CADA saidas (ou antes, numa tabela
    pequena), apagamos de uma vez o espaco das que sairam. `n` e quantas linhas estao aqui. As colunas sao
    escolhidas pela posicao (ex.: _TS, _A, _F). O array so existe enquanto ha alguma linha: tabela vazia =
    `numeros` None (a janela de um par so com tickers nao paga nada pela tabela do livro).
    """

    __slots__ = ("largura", "numeros", "inicio", "n")

    def __init__(self, largura: int):
        self.largura = largura
        self.numeros: array | None = None  # criado na primeira linha
        self.inicio = 0
        self.n = 0

    def acrescentar(self, *linha: float) -> None:
        if self.numeros is None:
            self.numeros = array("d", linha)
        else:
            self.numeros.extend(linha)
        self.n += 1

    def primeira(self, coluna: int) -> float:
        """O numero da `coluna` na linha mais antiga (so com a tabela nao vazia)."""
        return self.numeros[self.inicio * self.largura + coluna]

    def ultima(self, coluna: int) -> float:
        """O numero da `coluna` na linha mais nova (so com a tabela nao vazia)."""
        return self.numeros[coluna - self.largura]

    def trocar_primeira(self, coluna: int, numero: float) -> None:
        self.numeros[self.inicio * self.largura + coluna] = numero

    def trocar_ultima(self, coluna: int, numero: float) -> None:
        self.numeros[coluna - self.largura] = numero

    def tirar_primeira(self) -> array:
        """Tira a linha mais antiga e devolve os numeros dela, na ordem das colunas."""
        comeco = self.inicio * self.largura
        linha = self.numeros[comeco:comeco + self.largura]
        self.inicio += 1
        self.n -= 1
        if self.n == 0:  # esvaziou: libera tudo
            self.numeros = None
            self.inicio = 0
        elif self.inicio >= LIMPAR_A_CADA or 8 * self.inicio >= self.n:  # libera o espaco das que ja sairam
            del self.numeros[:self.inicio * self.largura]
            self.inicio = 0
        return linha

    def coluna(self, coluna: int) -> array:
        """Copia dos numeros da `coluna` que estao aqui, da linha mais antiga a mais nova."""
        if self.numeros is None:
            return array("d")
        return self.numeros[self.inicio * self.largura + coluna::self.largura]


# Colunas das amostras (as do ticker sao so as 3 primeiras) e das sequencias de invertida.
_TS, _A, _F, _LOTE_LONG, _LOTE_SHORT = range(5)
_INICIO, _FIM = range(2)

# A tabela vazia de todas as janelas (n = 0, sem array). A janela troca por uma tabela dela na primeira linha e
# volta para esta quando a tabela esvazia: a janela de um par so com tickers nao guarda as tabelas do livro e das
# invertidas. Com dezenas de milhares de janelas, cada objeto a menos conta na memoria e no tempo do coletor de lixo
# do Python (que percorre todos os objetos). Ninguem acrescenta linhas nela (veja JanelaMetricas._acrescentar).
_TABELA_VAZIA = _Colunas(0)


def _numero_real(valor) -> float:
    """O valor como float, se for um numero real (int ou float, nao bool, nao NaN nem infinito); senao ValueError.

    Texto ("1.5") e bool (True) sao recusados de proposito: float("1.5") e float(True) funcionariam e
    esconderiam um defeito de quem chamou.
    """
    if type(valor) is float:  # o caso comum: uma checagem so
        numero = valor
    elif isinstance(valor, (int, float)) and not isinstance(valor, bool):
        try:
            numero = float(valor)
        except OverflowError:  # inteiro grande demais para virar float
            raise ValueError(f"numero grande demais: {valor}") from None
    else:
        raise ValueError(f"nao e um numero: {valor!r}")
    if not math.isfinite(numero):
        raise ValueError(f"numero invalido: {numero}")
    return numero


def _inteiro_exato(numero: float) -> int:
    """O float multiplicado por 2**1074: sempre um inteiro exato (veja _SomaExata)."""
    numerador, denominador = numero.as_integer_ratio()  # numero = numerador / denominador, denominador = 2**k
    return numerador << (_BITS_ESCALA - (denominador.bit_length() - 1))  # vezes 2**(1074 - k)


def _valor_da_soma(inteiro: int) -> float:
    """A soma exata (veja _SomaExata) como float. Alem do maior float (~1,8e308) vira infinito, com o sinal da soma.

    Com numeros comuns isso nunca acontece; com numeros absurdos o infinito faz o motor descartar
    o candidato (numero invalido), em vez de o erro de conta (OverflowError) derrubar o ciclo inteiro.
    """
    try:
        return inteiro / _ESCALA  # divisao de inteiros do Python: arredondamento correto
    except OverflowError:
        return math.inf if inteiro > 0 else -math.inf


class _SomaExata:
    """Soma de floats que nunca acumula erro, mesmo somando e tirando numeros por dias.

    Numa soma comum de float, cada conta arredonda um pouco; somando e tirando sem parar, o erro
    vai crescendo. Aqui cada numero vira um inteiro exato (o float vezes 2**1074) e a soma e
    guardada como inteiro (o Python tem inteiros de qualquer tamanho), entao somar e tirar e
    sempre exato. A soma de uma janela fica igual qualquer que tenha sido o caminho ate ela. So no
    fim (`valor`) ela vira float, arredondada uma vez so (o mesmo que o math.fsum daria).
    A JanelaMetricas faz exatamente estas contas, mas guarda os inteiros direto, sem um objeto por soma: com
    dezenas de milhares de janelas (historico pelos tickers), cada objeto a menos conta na memoria e no tempo
    do coletor de lixo do Python.
    """

    __slots__ = ("_inteiro",)

    def __init__(self):
        self._inteiro = 0

    def somar(self, numero: float) -> None:
        self._inteiro += _inteiro_exato(numero)

    def tirar(self, numero: float) -> None:
        self._inteiro -= _inteiro_exato(numero)

    def valor(self) -> float:
        """A soma como float (veja _valor_da_soma)."""
        return _valor_da_soma(self._inteiro)


# Os maiores valores da janela, um array("d") para A e outro para F, do menor para o maior (no maximo
# MAIORES_GUARDADOS). Funcoes, e nao uma classe, para a janela nao ter dois objetos a mais (veja _TABELA_VAZIA).
# Regra que vale sempre: `valores` sao os len(valores) maiores valores da janela (todos eles,
# se a janela tiver ate MAIORES_GUARDADOS). O maior e o ultimo; os 10 maiores sao os 10 ultimos.
# - Valor que entra: vai para a lista se for pelo menos o menor dela (ou se a lista tem a janela
#   toda); se a lista passar do tamanho, sai o menor.
# - Valor que sai da janela: se estava na lista, sai dela.
# - Se a lista ficar com menos de 10 enquanto a janela tem mais valores, ela e refeita olhando a
#   janela inteira (_maiores_refeitos). Isso so acontece depois de sairem dezenas dos maiores sem nenhum maior
#   entrar (ex.: valores caindo sem parar): no maximo uma vez a cada ~40 amostras, e quase nunca na pratica.


def _maiores_entrou(valores: array, valor: float, tamanho_da_janela_antes: int) -> None:
    """`valor` entrou na janela (que tinha `tamanho_da_janela_antes` valores): atualiza `valores` no lugar."""
    tem_a_janela_toda = len(valores) == tamanho_da_janela_antes
    if tem_a_janela_toda or valor >= valores[0]:
        insort(valores, valor)  # poe no lugar certo, mantendo a ordem
        if len(valores) > MAIORES_GUARDADOS:
            del valores[0]  # fica so com os maiores


def _maiores_saiu(valores: array, valor: float, tamanho_da_janela_depois: int) -> bool:
    """`valor` saiu da janela (que ficou com `tamanho_da_janela_depois` valores): atualiza `valores` no lugar.

    Devolve True se a lista ficou curta demais e precisa ser refeita olhando a janela inteira (_maiores_refeitos).
    """
    tinha_a_janela_toda = len(valores) == tamanho_da_janela_depois + 1
    # Se nao tinha a janela toda, todo valor acima do menor da lista esta na lista. Valor igual
    # ao menor: tira uma copia dele da lista (ela continua sendo os maiores da janela).
    if tinha_a_janela_toda or valor >= valores[0]:
        del valores[bisect_left(valores, valor)]
    return len(valores) < MAIORES_N and tamanho_da_janela_depois > len(valores)


def _maiores_refeitos(valores_da_janela) -> array:
    """A lista nova dos maiores, olhando todos os valores que estao na janela."""
    return array("d", sorted(heapq.nlargest(MAIORES_GUARDADOS, valores_da_janela)))


def _media_dos_10_maiores(valores: array) -> float:
    """Media dos MAIORES_N maiores (ou de todos, se houver menos). Soma do maior para o menor."""
    maiores = valores[-MAIORES_N:]
    return sum(reversed(maiores)) / len(maiores)


# ---------------------------------------------------------------------------
# A janela
# ---------------------------------------------------------------------------


class JanelaMetricas:
    """Guarda as amostras de uma combinacao, das duas fontes, e resume o periodo.

    Do livro: (ts, A, F, lote do long, lote do short). Pelo ticker: (ts, A, F).
    peso_ticker: quantas amostras do livro uma amostra pelo ticker vale nas medias (o tempo que ela cobre:
        amostra_ticker_s / amostra_s). Numero real maior que zero; senao ValueError.
    Invertida: sequencia seguida de amostras com F >= 0 (de qualquer fonte, em ordem de horario). Conta no
    resumo se durar (ts da ultima menos ts da primeira) pelo menos a duracao minima; a sequencia aberta no fim
    tambem conta.
    """

    __slots__ = ("periodo_s", "peso_ticker", "_amostras", "_do_ticker", "_soma_a", "_soma_f", "_soma_lote_long",
                 "_soma_lote_short", "_soma_a_ticker", "_soma_f_ticker", "_maiores_a", "_maiores_f", "_invertidas",
                 "_ultimo_f", "_cache", "_cache_lotes", "_descartado_ate")

    def __init__(self, periodo_s: float, peso_ticker: float = 1.0):
        peso = _numero_real(peso_ticker)  # texto, bool, NaN e infinito: ValueError
        if peso <= 0:
            raise ValueError(f"peso_ticker precisa ser maior que zero: {peso_ticker!r}")
        self.periodo_s = periodo_s  # tamanho da janela, em segundos
        self.peso_ticker = peso
        # As amostras do livro, da mais antiga a mais nova: colunas _TS, _A, _F, _LOTE_LONG e _LOTE_SHORT.
        self._amostras = _TABELA_VAZIA
        # As amostras pelos tickers, da mais antiga a mais nova: colunas _TS, _A e _F.
        self._do_ticker = _TABELA_VAZIA
        # Somas exatas para as medias (os inteiros de _SomaExata): A e F de cada fonte e os lotes (so do livro).
        self._soma_a = self._soma_f = self._soma_lote_long = self._soma_lote_short = 0
        self._soma_a_ticker = self._soma_f_ticker = 0
        # Os maiores valores de A e de F das duas fontes (maior e media dos 10 maiores).
        self._maiores_a, self._maiores_f = array("d"), array("d")
        # Sequencias de invertida dentro da janela, da mais antiga a mais nova: ts da primeira
        # (_INICIO) e da ultima (_FIM) amostra de cada uma. A ultima esta aberta se a amostra mais nova
        # tem F >= 0.
        self._invertidas = _TABELA_VAZIA
        self._ultimo_f = 0.0  # F da amostra mais nova (de qualquer fonte); so vale com a janela nao vazia
        # Ultimo resumo calculado, com a duracao minima usada (None = nada guardado ou a janela mudou).
        self._cache: tuple[float, Metricas] | None = None
        self._cache_lotes: tuple[float, float] | None = None
        # Ate que `agora` as antigas ja foram descartadas (None = conferir de novo). O motor pergunta
        # resumo, lotes e vazia no mesmo `agora`: so a primeira pergunta precisa olhar a fila.
        self._descartado_ate: float | None = None

    def adicionar(self, ts: float, a: float, f: float, lote_long: float = 0.0, lote_short: float = 0.0) -> None:
        """Guarda uma amostra do LIVRO e ja descarta as antigas.

        ts: maior ou igual ao da amostra do livro anterior e maior que o da amostra pelo ticker mais nova.
        Valor que nao e numero real (NaN, infinito, texto, bool, None) ou ts fora de ordem: ValueError, e nada
        entra (estragariam as medias e a ordem dos maiores).
        """
        try:
            ts, a, f = _numero_real(ts), _numero_real(a), _numero_real(f)
            lote_long, lote_short = _numero_real(lote_long), _numero_real(lote_short)
        except ValueError as erro:
            raise ValueError(f"amostra com numero invalido ({erro}): ts={ts!r} a={a!r} f={f!r} "
                             f"lotes={lote_long!r}/{lote_short!r}") from None
        if self._do_ticker.n and ts <= self._do_ticker.ultima(_TS):
            raise ValueError(f"amostra do livro em ts={ts} nao e mais nova que a amostra pelo ticker de "
                             f"ts={self._do_ticker.ultima(_TS)}")
        self._entrou(ts, a, f)
        if self._amostras is _TABELA_VAZIA:
            self._amostras = _Colunas(5)
        self._amostras.acrescentar(ts, a, f, lote_long, lote_short)
        self._soma_a += _inteiro_exato(a)
        self._soma_f += _inteiro_exato(f)
        self._soma_lote_long += _inteiro_exato(lote_long)
        self._soma_lote_short += _inteiro_exato(lote_short)
        self._mudou()
        self._descartado_ate = None  # amostra nova: a proxima pergunta confere a fila de novo
        self._descartar_antigas(ts)

    def adicionar_pelo_ticker(self, ts: float, a: float, f: float) -> None:
        """Guarda uma amostra PELO TICKER (sem lote) e ja descarta as antigas.

        ts: maior que o da amostra mais nova (de qualquer fonte). Valor que nao e numero real ou ts fora de
        ordem: ValueError, e nada entra.
        """
        try:
            ts, a, f = _numero_real(ts), _numero_real(a), _numero_real(f)
        except ValueError as erro:
            raise ValueError(f"amostra pelo ticker com numero invalido ({erro}): ts={ts!r} a={a!r} "
                             f"f={f!r}") from None
        mais_nova = self._ts_mais_novo()
        if mais_nova is not None and ts <= mais_nova:
            raise ValueError(f"amostra pelo ticker em ts={ts} nao e mais nova que a de ts={mais_nova}")
        self._entrou(ts, a, f)
        if self._do_ticker is _TABELA_VAZIA:
            self._do_ticker = _Colunas(3)
        self._do_ticker.acrescentar(ts, a, f)
        self._soma_a_ticker += _inteiro_exato(a)
        self._soma_f_ticker += _inteiro_exato(f)
        self._mudou()
        self._descartado_ate = None
        self._descartar_antigas(ts)

    def resumo(self, agora: float, duracao_min_invertida_s: float) -> Metricas | None:
        """Resume as amostras do periodo que termina em `agora`. Devolve None se nao houver nenhuma.

        O objeto devolvido fica guardado e e devolvido de novo enquanto nada mudar: so leitura.
        """
        self._descartar_antigas(agora)
        if not (self._amostras.n or self._do_ticker.n):
            return None
        if self._cache is not None and self._cache[0] == duracao_min_invertida_s:
            return self._cache[1]  # caso comum: a janela e a duracao nao mudaram desde a ultima conta
        metricas = Metricas(
            abertura_media=self._media(self._soma_a, self._soma_a_ticker),
            fechamento_medio=self._media(self._soma_f, self._soma_f_ticker),
            maior_abertura=self._maiores_a[-1],
            maior_fechamento=self._maiores_f[-1],
            media_10_maiores_abertura=_media_dos_10_maiores(self._maiores_a),
            media_10_maiores_fechamento=_media_dos_10_maiores(self._maiores_f),
            invertidas=self._contar_invertidas(duracao_min_invertida_s),
        )
        self._cache = (duracao_min_invertida_s, metricas)
        return metricas

    def lotes_medios(self, agora: float) -> tuple[float | None, float | None]:
        """(lote medio do long, lote medio do short) das amostras do LIVRO no periodo que termina em `agora`.

        (None, None) sem amostras do livro (as amostras pelo ticker nao tem lote).
        """
        self._descartar_antigas(agora)
        if not self._amostras.n:
            return None, None
        if self._cache_lotes is None:
            n = self._amostras.n
            self._cache_lotes = (_valor_da_soma(self._soma_lote_long) / n, _valor_da_soma(self._soma_lote_short) / n)
        return self._cache_lotes

    def vazia(self, agora: float) -> bool:
        """True se nao sobrou nenhuma amostra (de nenhuma fonte) dentro do periodo que termina em `agora`."""
        self._descartar_antigas(agora)
        return not (self._amostras.n or self._do_ticker.n)

    def amostras(self) -> list[tuple[float, float, float, float | None, float | None]]:
        """Copia das amostras guardadas agora, da mais antiga a mais nova: (ts, A, F, lote do long, lote do short).

        As pelo ticker vem com os lotes None. O motor usa para juntar as amostras novas da memoria com as janelas
        recarregadas do banco.
        """
        do_livro = list(zip(*[self._amostras.coluna(c) for c in (_TS, _A, _F, _LOTE_LONG, _LOTE_SHORT)]))
        do_ticker = [(ts, a, f, None, None) for ts, a, f in zip(*[self._do_ticker.coluna(c) for c in (_TS, _A, _F)])]
        if not do_ticker:
            return do_livro
        return list(heapq.merge(do_livro, do_ticker, key=lambda amostra: amostra[0]))

    # --- por dentro -----------------------------------------------------------

    def _mudou(self) -> None:
        self._cache = None
        self._cache_lotes = None

    def _ts_mais_novo(self) -> float | None:
        """O ts da amostra mais nova (de qualquer fonte), ou None com a janela vazia."""
        livro = self._amostras.ultima(_TS) if self._amostras.n else None
        ticker = self._do_ticker.ultima(_TS) if self._do_ticker.n else None
        if livro is None or ticker is None:
            return ticker if livro is None else livro
        return max(livro, ticker)

    def _entrou(self, ts: float, a: float, f: float) -> None:
        """O que toda amostra nova muda, qualquer que seja a fonte: invertidas e maiores (antes de entrar na tabela)."""
        tamanho_antes = self._amostras.n + self._do_ticker.n
        if f >= 0:  # invertida: continua a sequencia aberta ou comeca uma nova
            if tamanho_antes and self._ultimo_f >= 0:
                self._invertidas.trocar_ultima(_FIM, ts)
            else:
                if self._invertidas is _TABELA_VAZIA:
                    self._invertidas = _Colunas(2)
                self._invertidas.acrescentar(ts, ts)
        self._ultimo_f = f
        _maiores_entrou(self._maiores_a, a, tamanho_antes)
        _maiores_entrou(self._maiores_f, f, tamanho_antes)

    def _media(self, soma_livro: int, soma_ticker: int) -> float:
        """Media das duas fontes, a do ticker pesando `peso_ticker`. Com uma fonte so: soma dividida pela quantidade."""
        n_livro, n_ticker = self._amostras.n, self._do_ticker.n
        if not n_ticker:
            return _valor_da_soma(soma_livro) / n_livro  # so amostras do livro: a conta de sempre
        if not n_livro:
            return _valor_da_soma(soma_ticker) / n_ticker
        peso = self.peso_ticker
        return (_valor_da_soma(soma_livro) + peso * _valor_da_soma(soma_ticker)) / (n_livro + peso * n_ticker)

    def _descartar_antigas(self, agora: float) -> None:
        """Tira do comeco as amostras com ts < agora - periodo_s, que ficaram fora da janela, em ordem de horario."""
        if self._descartado_ate is not None and agora <= self._descartado_ate:
            return  # ja descartou ate um `agora` igual ou maior: nada novo pode sair
        limite = agora - self.periodo_s
        livro, ticker = self._amostras, self._do_ticker
        if not ticker.n:  # o caso comum (par com livro o periodo todo, ou sem tickers): uma fila so
            while livro.n and livro.primeira(_TS) < limite:
                self._tirar_do_livro()
        else:  # as duas filas: sai sempre a mais antiga das duas (as invertidas dependem da ordem)
            while True:
                ts_livro = livro.primeira(_TS) if livro.n else math.inf
                ts_ticker = ticker.primeira(_TS) if ticker.n else math.inf
                if min(ts_livro, ts_ticker) >= limite:
                    break
                if ts_livro < ts_ticker:
                    self._tirar_do_livro()
                else:
                    self._tirar_do_ticker()
        self._descartado_ate = agora

    def _tirar_do_livro(self) -> None:
        _, a, f, lote_long, lote_short = self._amostras.tirar_primeira()
        if not self._amostras.n:
            self._amostras = _TABELA_VAZIA
        self._soma_a -= _inteiro_exato(a)
        self._soma_f -= _inteiro_exato(f)
        self._soma_lote_long -= _inteiro_exato(lote_long)
        self._soma_lote_short -= _inteiro_exato(lote_short)
        self._saiu(a, f)

    def _tirar_do_ticker(self) -> None:
        _, a, f = self._do_ticker.tirar_primeira()
        if not self._do_ticker.n:
            self._do_ticker = _TABELA_VAZIA
        self._soma_a_ticker -= _inteiro_exato(a)
        self._soma_f_ticker -= _inteiro_exato(f)
        self._saiu(a, f)

    def _saiu(self, a: float, f: float) -> None:
        """O que toda amostra que sai muda (a amostra ja saiu da tabela dela): maiores e invertidas."""
        tamanho = self._amostras.n + self._do_ticker.n
        if _maiores_saiu(self._maiores_a, a, tamanho):
            self._maiores_a = _maiores_refeitos(chain(self._amostras.coluna(_A), self._do_ticker.coluna(_A)))
        if _maiores_saiu(self._maiores_f, f, tamanho):
            self._maiores_f = _maiores_refeitos(chain(self._amostras.coluna(_F), self._do_ticker.coluna(_F)))
        if f >= 0:  # era a primeira amostra da sequencia de invertida mais antiga
            proxima = self._mais_antiga()
            if proxima is not None and proxima[1] >= 0:
                # A sequencia continua: agora comeca na proxima amostra.
                self._invertidas.trocar_primeira(_INICIO, proxima[0])
            else:  # a sequencia saiu inteira
                self._invertidas.tirar_primeira()
                if not self._invertidas.n:
                    self._invertidas = _TABELA_VAZIA
        self._mudou()

    def _mais_antiga(self) -> tuple[float, float] | None:
        """(ts, F) da amostra mais antiga que ficou na janela (de qualquer fonte), ou None se ficou vazia."""
        livro = (self._amostras.primeira(_TS), self._amostras.primeira(_F)) if self._amostras.n else None
        ticker = (self._do_ticker.primeira(_TS), self._do_ticker.primeira(_F)) if self._do_ticker.n else None
        if livro is None or ticker is None:
            return ticker if livro is None else livro
        return livro if livro[0] < ticker[0] else ticker

    def _contar_invertidas(self, duracao_min_s: float) -> int:
        """Quantas sequencias de invertida da janela duram pelo menos `duracao_min_s` (conta sequencias)."""
        if duracao_min_s <= 0:
            return self._invertidas.n  # toda sequencia dura 0 ou mais: contam todas (o caso padrao)
        inicios, fins = self._invertidas.coluna(_INICIO), self._invertidas.coluna(_FIM)
        return sum(1 for inicio, fim in zip(inicios, fins) if fim - inicio >= duracao_min_s)
