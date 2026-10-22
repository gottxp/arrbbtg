"""Janela de metricas do periodo: A e F medios e maiores, media dos 10 maiores, invertidas e lotes medios.

A = spread de abertura e F = spread de fechamento, ambos em %. Tudo em memoria e sem rede.
O motor guarda uma JanelaMetricas por combinacao e acrescenta uma amostra (A, F e o lote de cada
ponta) a cada `amostra_s` segundos. As amostras chegam em ordem crescente de ts.

Com 4 h de periodo e uma amostra a cada 5 s sao 2.880 amostras por combinacao, e o motor acompanha
milhares de combinacoes. Por isso a janela e INCREMENTAL e COMPACTA:
- cada amostra nova so mexe no que entrou e no que saiu, sem olhar a janela inteira:
  somas exatas para as medias, a lista curta dos maiores valores (refeita olhando a janela toda
  so em caso raro, veja _Maiores) e as sequencias de invertidas;
- os numeros ficam em arrays de 8 bytes por numero (array("d")), e nao em tuplas de floats soltos.
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

# Quantos valores entram na metrica "media dos 10 maiores". Faz parte do nome da metrica
# (media_10_maiores), por isso nao vem da Config.
MAIORES_N = 10

# Quantos dos maiores valores de A (e de F) ficam guardados. Basta ter os 10 maiores, mas guardar
# alguns a mais faz a lista quase nunca precisar ser refeita olhando a janela inteira (veja _Maiores).
MAIORES_GUARDADOS = 50

# De quantas em quantas linhas que sairam a tabela libera o espaco delas (veja _Colunas).
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
    """Linhas de numeros reais guardadas em colunas compactas: um array("d") por coluna, 8 bytes por numero.

    As linhas entram no fim e saem do comeco (uma fila). Tirar do comeco de um array obrigaria a
    mover todos os outros numeros, entao so avancamos `inicio` (a posicao da primeira linha que
    ainda esta aqui) e, a cada LIMPAR_A_CADA saidas, apagamos de uma vez o espaco das que sairam.
    `n` e quantas linhas estao aqui. As colunas sao escolhidas pela posicao (ex.: _TS, _A, _F).
    """

    def __init__(self, quantas_colunas: int):
        self.colunas = [array("d") for _ in range(quantas_colunas)]
        self.inicio = 0
        self.n = 0

    def acrescentar(self, *linha: float) -> None:
        for coluna, numero in zip(self.colunas, linha):
            coluna.append(numero)
        self.n += 1

    def primeira(self, coluna: int) -> float:
        """O numero da `coluna` na linha mais antiga."""
        return self.colunas[coluna][self.inicio]

    def ultima(self, coluna: int) -> float:
        """O numero da `coluna` na linha mais nova."""
        return self.colunas[coluna][-1]

    def trocar_primeira(self, coluna: int, numero: float) -> None:
        self.colunas[coluna][self.inicio] = numero

    def trocar_ultima(self, coluna: int, numero: float) -> None:
        self.colunas[coluna][-1] = numero

    def tirar_primeira(self) -> list[float]:
        """Tira a linha mais antiga e devolve os numeros dela, na ordem das colunas."""
        linha = [coluna[self.inicio] for coluna in self.colunas]
        self.inicio += 1
        self.n -= 1
        if self.n == 0:  # esvaziou: libera tudo
            self.colunas = [array("d") for _ in self.colunas]
            self.inicio = 0
        elif self.inicio >= LIMPAR_A_CADA:  # libera o espaco das linhas que ja sairam
            for coluna in self.colunas:
                del coluna[:self.inicio]
            self.inicio = 0
        return linha

    def coluna(self, coluna: int) -> array:
        """Copia dos numeros da `coluna` que estao aqui, da linha mais antiga a mais nova."""
        return self.colunas[coluna][self.inicio:]


# Colunas das amostras e das sequencias de invertida.
_TS, _A, _F, _LOTE_LONG, _LOTE_SHORT = range(5)
_INICIO, _FIM = range(2)


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


class _SomaExata:
    """Soma de floats que nunca acumula erro, mesmo somando e tirando numeros por dias.

    Numa soma comum de float, cada conta arredonda um pouco; somando e tirando sem parar, o erro
    vai crescendo. Aqui cada numero vira um inteiro exato (o float vezes 2**1074) e a soma e
    guardada como inteiro (o Python tem inteiros de qualquer tamanho), entao somar e tirar e
    sempre exato. A soma de uma janela fica igual qualquer que tenha sido o caminho ate ela. So no
    fim (`valor`) ela vira float, arredondada uma vez so (o mesmo que o math.fsum daria).
    """

    def __init__(self):
        self._inteiro = 0

    def somar(self, numero: float) -> None:
        self._inteiro += _inteiro_exato(numero)

    def tirar(self, numero: float) -> None:
        self._inteiro -= _inteiro_exato(numero)

    def valor(self) -> float:
        """A soma como float. Soma alem do maior float (~1,8e308) vira infinito, com o sinal da soma.

        Com numeros comuns isso nunca acontece; com numeros absurdos o infinito faz o motor descartar
        o candidato (numero invalido), em vez de o erro de conta (OverflowError) derrubar o ciclo inteiro.
        """
        try:
            return self._inteiro / _ESCALA  # divisao de inteiros do Python: arredondamento correto
        except OverflowError:
            return math.inf if self._inteiro > 0 else -math.inf


class _Maiores:
    """Os maiores valores da janela (no maximo MAIORES_GUARDADOS), do menor para o maior.

    Regra que vale sempre: `valores` sao os len(valores) maiores valores da janela (todos eles,
    se a janela tiver ate MAIORES_GUARDADOS). O maior e o ultimo; os 10 maiores sao os 10 ultimos.
    - Valor que entra: vai para a lista se for pelo menos o menor dela (ou se a lista tem a janela
      toda); se a lista passar do tamanho, sai o menor.
    - Valor que sai da janela: se estava na lista, sai dela.
    - Se a lista ficar com menos de 10 enquanto a janela tem mais valores, ela e refeita olhando a
      janela inteira. Isso so acontece depois de sairem dezenas dos maiores sem nenhum maior entrar
      (ex.: valores caindo sem parar): no maximo uma vez a cada ~40 amostras, e quase nunca na pratica.
    """

    def __init__(self):
        self.valores: list[float] = []

    def entrou(self, valor: float, tamanho_da_janela_antes: int) -> None:
        tem_a_janela_toda = len(self.valores) == tamanho_da_janela_antes
        if tem_a_janela_toda or valor >= self.valores[0]:
            insort(self.valores, valor)  # poe no lugar certo, mantendo a ordem
            if len(self.valores) > MAIORES_GUARDADOS:
                del self.valores[0]  # fica so com os maiores

    def saiu(self, valor: float, amostras: _Colunas, coluna: int) -> None:
        """`valor` saiu da janela; `amostras` ja esta sem ele (`coluna` e a coluna desses valores)."""
        tinha_a_janela_toda = len(self.valores) == amostras.n + 1
        # Se nao tinha a janela toda, todo valor acima do menor da lista esta na lista. Valor igual
        # ao menor: tira uma copia dele da lista (ela continua sendo os maiores da janela).
        if tinha_a_janela_toda or valor >= self.valores[0]:
            del self.valores[bisect_left(self.valores, valor)]
        if len(self.valores) < MAIORES_N and amostras.n > len(self.valores):
            self.valores = sorted(heapq.nlargest(MAIORES_GUARDADOS, amostras.coluna(coluna)))

    def maior(self) -> float:
        return self.valores[-1]

    def media_dos_10_maiores(self) -> float:
        """Media dos MAIORES_N maiores (ou de todos, se houver menos). Soma do maior para o menor."""
        maiores = self.valores[-MAIORES_N:]
        return sum(reversed(maiores)) / len(maiores)


# ---------------------------------------------------------------------------
# A janela
# ---------------------------------------------------------------------------


class JanelaMetricas:
    """Guarda as amostras (ts, A, F, lote do long, lote do short) de uma combinacao e resume o periodo.

    Invertida: sequencia seguida de amostras com F >= 0. Conta no resumo se durar (ts da ultima
    menos ts da primeira) pelo menos a duracao minima; a sequencia aberta no fim tambem conta.
    """

    def __init__(self, periodo_s: float):
        self.periodo_s = periodo_s  # tamanho da janela, em segundos
        # As amostras, da mais antiga a mais nova: colunas _TS, _A, _F, _LOTE_LONG e _LOTE_SHORT.
        self._amostras = _Colunas(5)
        # Somas exatas para as medias.
        self._soma_a, self._soma_f = _SomaExata(), _SomaExata()
        self._soma_lote_long, self._soma_lote_short = _SomaExata(), _SomaExata()
        # Os maiores valores de A e de F (maior e media dos 10 maiores).
        self._maiores_a, self._maiores_f = _Maiores(), _Maiores()
        # Sequencias de invertida dentro da janela, da mais antiga a mais nova: ts da primeira
        # (_INICIO) e da ultima (_FIM) amostra de cada uma. A ultima esta aberta se a ultima
        # amostra tem F >= 0.
        self._invertidas = _Colunas(2)
        # Ultimo resumo calculado, com a duracao minima usada (None = nada guardado ou a janela mudou).
        self._cache: tuple[float, Metricas] | None = None
        self._cache_lotes: tuple[float, float] | None = None
        # Ate que `agora` as antigas ja foram descartadas (None = conferir de novo). O motor pergunta
        # resumo, lotes e vazia no mesmo `agora`: so a primeira pergunta precisa olhar a fila.
        self._descartado_ate: float | None = None

    def adicionar(self, ts: float, a: float, f: float, lote_long: float = 0.0, lote_short: float = 0.0) -> None:
        """Guarda uma amostra (sempre com ts maior ou igual ao da anterior) e ja descarta as antigas.

        Valor que nao e numero real (NaN, infinito, texto, bool, None): ValueError, e nada entra
        (estragariam as medias e a ordem dos maiores).
        """
        try:
            ts, a, f = _numero_real(ts), _numero_real(a), _numero_real(f)
            lote_long, lote_short = _numero_real(lote_long), _numero_real(lote_short)
        except ValueError as erro:
            raise ValueError(f"amostra com numero invalido ({erro}): ts={ts!r} a={a!r} f={f!r} "
                             f"lotes={lote_long!r}/{lote_short!r}") from None
        tamanho_antes = self._amostras.n

        if f >= 0:  # invertida: continua a sequencia aberta ou comeca uma nova
            if tamanho_antes and self._amostras.ultima(_F) >= 0:
                self._invertidas.trocar_ultima(_FIM, ts)
            else:
                self._invertidas.acrescentar(ts, ts)

        self._amostras.acrescentar(ts, a, f, lote_long, lote_short)
        self._soma_a.somar(a)
        self._soma_f.somar(f)
        self._soma_lote_long.somar(lote_long)
        self._soma_lote_short.somar(lote_short)
        self._maiores_a.entrou(a, tamanho_antes)
        self._maiores_f.entrou(f, tamanho_antes)
        self._mudou()
        self._descartado_ate = None  # amostra nova: a proxima pergunta confere a fila de novo
        self._descartar_antigas(ts)

    def resumo(self, agora: float, duracao_min_invertida_s: float) -> Metricas | None:
        """Resume as amostras do periodo que termina em `agora`. Devolve None se nao houver nenhuma.

        O objeto devolvido fica guardado e e devolvido de novo enquanto nada mudar: so leitura.
        """
        self._descartar_antigas(agora)
        if not self._amostras.n:
            return None
        if self._cache is not None and self._cache[0] == duracao_min_invertida_s:
            return self._cache[1]  # caso comum: a janela e a duracao nao mudaram desde a ultima conta
        n = self._amostras.n
        metricas = Metricas(
            abertura_media=self._soma_a.valor() / n,
            fechamento_medio=self._soma_f.valor() / n,
            maior_abertura=self._maiores_a.maior(),
            maior_fechamento=self._maiores_f.maior(),
            media_10_maiores_abertura=self._maiores_a.media_dos_10_maiores(),
            media_10_maiores_fechamento=self._maiores_f.media_dos_10_maiores(),
            invertidas=self._contar_invertidas(duracao_min_invertida_s),
        )
        self._cache = (duracao_min_invertida_s, metricas)
        return metricas

    def lotes_medios(self, agora: float) -> tuple[float | None, float | None]:
        """(lote medio do long, lote medio do short) no periodo que termina em `agora`; (None, None) sem amostras."""
        self._descartar_antigas(agora)
        if not self._amostras.n:
            return None, None
        if self._cache_lotes is None:
            n = self._amostras.n
            self._cache_lotes = (self._soma_lote_long.valor() / n, self._soma_lote_short.valor() / n)
        return self._cache_lotes

    def vazia(self, agora: float) -> bool:
        """True se nao sobrou nenhuma amostra dentro do periodo que termina em `agora`."""
        self._descartar_antigas(agora)
        return not self._amostras.n

    # --- por dentro -----------------------------------------------------------

    def _mudou(self) -> None:
        self._cache = None
        self._cache_lotes = None

    def _descartar_antigas(self, agora: float) -> None:
        """Tira do comeco as amostras com ts < agora - periodo_s, que ficaram fora da janela."""
        if self._descartado_ate is not None and agora <= self._descartado_ate:
            return  # ja descartou ate um `agora` igual ou maior: nada novo pode sair
        limite = agora - self.periodo_s
        while self._amostras.n and self._amostras.primeira(_TS) < limite:
            self._tirar_a_mais_antiga()
        self._descartado_ate = agora

    def _tirar_a_mais_antiga(self) -> None:
        _, a, f, lote_long, lote_short = self._amostras.tirar_primeira()
        self._soma_a.tirar(a)
        self._soma_f.tirar(f)
        self._soma_lote_long.tirar(lote_long)
        self._soma_lote_short.tirar(lote_short)
        self._maiores_a.saiu(a, self._amostras, _A)
        self._maiores_f.saiu(f, self._amostras, _F)
        if f >= 0:  # era a primeira amostra da sequencia de invertida mais antiga
            if self._amostras.n and self._amostras.primeira(_F) >= 0:
                # A sequencia continua: agora comeca na proxima amostra.
                self._invertidas.trocar_primeira(_INICIO, self._amostras.primeira(_TS))
            else:  # a sequencia saiu inteira
                self._invertidas.tirar_primeira()
        self._mudou()

    def _contar_invertidas(self, duracao_min_s: float) -> int:
        """Quantas sequencias de invertida da janela duram pelo menos `duracao_min_s` (conta sequencias)."""
        if duracao_min_s <= 0:
            return self._invertidas.n  # toda sequencia dura 0 ou mais: contam todas (o caso padrao)
        inicios, fins = self._invertidas.coluna(_INICIO), self._invertidas.coluna(_FIM)
        return sum(1 for inicio, fim in zip(inicios, fins) if fim - inicio >= duracao_min_s)
