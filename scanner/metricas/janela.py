"""Janela de metricas do periodo: A e F medios e maiores, media dos 10 maiores e invertidas.

A = spread de abertura e F = spread de fechamento, ambos em %. Tudo em memoria e sem rede.
O motor (Task 10) guarda uma JanelaMetricas por par e acrescenta uma amostra (A, F) a cada
`amostra_s` segundos. As amostras chegam em ordem crescente de ts.

O motor chama `resumo` a cada 500 ms, mas as amostras mudam so a cada 5 s. Por isso o
ultimo resumo fica guardado (cache) e so e recontado quando a janela muda ou quando a
duracao minima de invertida muda.
"""

import heapq
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

# Quantos valores entram na metrica "media dos 10 maiores". Faz parte do nome da metrica
# (media_10_maiores), por isso nao vem da Config.
MAIORES_N = 10


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


def _media_dos_maiores(valores: Iterable[float]) -> float:
    """Media dos MAIORES_N maiores valores. Se houver menos que isso, usa todos.

    `heapq.nlargest` acha os maiores sem ordenar a lista toda. Quem chama garante que
    `valores` nao esta vazio.
    """
    maiores = heapq.nlargest(MAIORES_N, valores)
    return sum(maiores) / len(maiores)


def _contar_invertidas(amostras: Iterable[tuple[float, float, float]], duracao_min_s: float) -> int:
    """Conta as sequencias seguidas de amostras com F >= 0 que duram pelo menos `duracao_min_s`.

    `amostras` sao (ts, A, F) em ordem. Uma sequencia que ainda esta aberta no fim
    da lista tambem conta.
    """
    contadas = 0
    inicio = None  # ts da primeira amostra da sequencia atual; None = nao estamos numa sequencia
    fim = None  # ts da ultima amostra da sequencia atual
    for ts, _, f in amostras:
        if f >= 0:
            if inicio is None:
                inicio = ts
            fim = ts
        else:
            # F ficou negativo: a sequencia terminou. Conta se durou o bastante.
            if inicio is not None and fim - inicio >= duracao_min_s:
                contadas += 1
            inicio = None
    # Sequencia que ficou aberta ate o fim da janela
    if inicio is not None and fim - inicio >= duracao_min_s:
        contadas += 1
    return contadas


class JanelaMetricas:
    """Guarda as amostras (ts, A, F) de um par e calcula o resumo do periodo."""

    def __init__(self, periodo_s: float):
        self.periodo_s = periodo_s  # tamanho da janela, em segundos
        self._amostras: deque[tuple[float, float, float]] = deque()  # (ts, A, F), do mais antigo ao mais novo
        # Ultimo resumo calculado, guardado junto com a duracao minima usada na conta.
        # None = nada guardado (ou a janela mudou desde a ultima conta).
        self._cache: tuple[float, Metricas] | None = None

    def _descartar_antigas(self, agora: float) -> None:
        """Tira do inicio as amostras com ts < agora - periodo_s, que ficaram fora da janela."""
        limite = agora - self.periodo_s
        while self._amostras and self._amostras[0][0] < limite:
            self._amostras.popleft()
            self._cache = None  # saiu amostra: o resumo guardado ja nao vale

    def adicionar(self, ts: float, a: float, f: float) -> None:
        """Guarda uma amostra. Ja descarta as antigas, para a memoria nao crescer sem limite."""
        self._amostras.append((ts, a, f))
        self._cache = None  # entrou amostra nova: o resumo guardado ja nao vale
        self._descartar_antigas(ts)

    def _calcular(self, duracao_min_invertida_s: float) -> Metricas:
        """Conta o resumo do zero. Quem chama garante que ha amostras na janela."""
        n = len(self._amostras)
        return Metricas(
            abertura_media=sum(a for _, a, _ in self._amostras) / n,
            fechamento_medio=sum(f for _, _, f in self._amostras) / n,
            maior_abertura=max(a for _, a, _ in self._amostras),
            maior_fechamento=max(f for _, _, f in self._amostras),
            media_10_maiores_abertura=_media_dos_maiores(a for _, a, _ in self._amostras),
            media_10_maiores_fechamento=_media_dos_maiores(f for _, _, f in self._amostras),
            invertidas=_contar_invertidas(self._amostras, duracao_min_invertida_s),
        )

    def resumo(self, agora: float, duracao_min_invertida_s: float) -> Metricas | None:
        """Resume as amostras do periodo que termina em `agora`. Devolve None se nao houver nenhuma."""
        self._descartar_antigas(agora)  # se tirar alguma amostra, zera o cache dentro dela
        if not self._amostras:
            return None
        if self._cache is not None and self._cache[0] == duracao_min_invertida_s:
            return self._cache[1]  # caso comum: a janela e a duracao nao mudaram desde a ultima conta
        metricas = self._calcular(duracao_min_invertida_s)
        self._cache = (duracao_min_invertida_s, metricas)
        return metricas
