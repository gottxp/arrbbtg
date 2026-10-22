"""Estado em memoria dos mercados e a regra da etapa 2 (quais pares ganham livro completo).

Os coletores escrevem aqui; o motor so le. Quem le nunca recebe dado velho:
`obter_livro` e `obter_ticker` devolvem None quando a conexao da corretora esta
silenciosa ou quando o dado ficou parado alem do limite.

Aqui fica tambem a saude de cada corretora, mostrada no painel (`saude`): conectada, reconectando
ou atrasada, o atraso medio dos dados e quantas vezes ela reconectou.

Conexao viva por mercado (revisao final): o spot e o futuro de uma corretora costumam ter conexoes
WebSocket separadas. A mensagem registrada com o mercado so mantem vivo aquele mercado; a registrada sem
mercado (conexao unica, ex.: Lighter) vale para a corretora toda.

Volume 24h pela API REST (revisao final): o ticker de algumas corretoras nao traz volume (ex.: bookTicker da
MEXC spot). O coletor anota esses simbolos (`pedir_volume_rest`), os dados lentos buscam o volume pela REST
(`registrar_volume_rest`) e o coletor usa esse volume no ticker (`volume_rest`). E dado lento: nao some na queda.
"""

import logging
import math
from collections import deque

from scanner.modelos import Livro

registro = logging.getLogger("scanner.estado")

# Atraso medio de cada corretora = media das amostras do ultimo minuto. As amostras sao somadas em
# blocos de 10 s (soma e quantidade de cada bloco), entao a memoria nao cresce com o numero de
# mensagens: guarda no maximo 6 blocos por corretora. Detalhe de exibicao (nao e um limite de negocio).
JANELA_ATRASO_S = 60
BLOCO_ATRASO_S = 10
_BLOCOS_NA_JANELA = JANELA_ATRASO_S // BLOCO_ATRASO_S


class EstadoMercados:
    """Guarda, por corretora, o horario da ultima mensagem e o ultimo ticker e livro de cada par.

    max_silencio_s: maximo de segundos sem NENHUMA mensagem da corretora (o stream de
        tickers serve de heartbeat) antes de ela ser tratada como atrasada. Para o livro e o ticker de um
        mercado conta so a mensagem daquele mercado (ou a sem mercado; veja conexao_viva).
    max_idade_livro_s: maximo de segundos que um livro (ou ticker) pode ficar parado.
        O WebSocket so manda quando o livro muda, entao parado ainda vale ate aqui.
    atraso_max_valido_s: atraso (chegada - horario informado pela corretora) acima disto, para
        mais ou para menos, e relogio errado e fica fora do atraso medio.

    Os valores vem da Config (max_silencio_conexao_s, max_idade_livro_s e atraso_max_valido_s).
    Chave de tudo: (corretora, mercado, ativo normalizado). A excecao sao os tickers dos pares de
    cotacao (ex.: USDC/USDT da fonte_cotacao), guardados por (corretora, simbolo do ccxt): o mesmo
    ativo pode ter varios pares spot e a cotacao precisa ler exatamente o seu (ex.: USDT/USD da Kraken,
    e nao o USDT/EUR que ocupa a chave do ativo USDT). Eles nao entram em `mercados()`: nao formam
    combinacao nenhuma por estarem aqui.
    """

    def __init__(self, max_silencio_s: float = 2, max_idade_livro_s: float = 30, atraso_max_valido_s: float = 30):
        self._max_silencio_s = max_silencio_s
        self._max_idade_livro_s = max_idade_livro_s
        self._atraso_max_valido_s = atraso_max_valido_s
        self._ultima_mensagem: dict[str, float] = {}  # corretora -> ultima mensagem sem mercado (vale para todos)
        self._ultima_do_mercado: dict[tuple[str, str], float] = {}  # (corretora, mercado) -> ultima mensagem
        self._livros: dict[tuple[str, str, str], Livro] = {}
        self._tickers: dict[tuple[str, str, str], dict] = {}
        self._cotacoes: dict[tuple[str, str], dict] = {}  # (corretora, simbolo) -> ticker do par de cotacao
        # Saude das corretoras (painel).
        self._reconectando: set[str] = set()  # caiu e ainda nao voltou
        self._reconexoes: dict[str, int] = {}  # corretora -> quantas vezes voltou depois de cair
        self._nunca_conectou: set[str] = set()  # esperando a primeira conexao: ela nao conta como reconexao
        self._atrasos: dict[str, deque] = {}  # corretora -> blocos [numero do bloco, soma dos atrasos, quantidade]
        self._avisados_relogio: set[str] = set()  # corretoras ja avisadas de relogio errado (uma vez cada)
        # Volume 24h pela API REST (dado lento, nao e apagado na queda).
        self._volumes_rest: dict[tuple[str, str], float] = {}  # (corretora, simbolo) -> volume 24h
        self._volumes_pedidos: dict[str, set[str]] = {}  # corretora -> simbolos cujo ticker nao traz volume

    # --- escrita (coletores) -------------------------------------------------

    def registrar_mensagem(self, corretora: str, agora: float, mercado: str | None = None) -> None:
        """Anota que a corretora mandou alguma mensagem agora (ticker, livro ou heartbeat).

        mercado: "spot" ou "futuro" quando a mensagem veio da conexao daquele mercado; so mantem ele vivo.
        Sem mercado (conexao unica da corretora): mantem vivos todos os mercados dela.
        """
        if mercado is None:
            self._ultima_mensagem[corretora] = agora
        else:
            self._ultima_do_mercado[(corretora, mercado)] = agora

    def atualizar_ticker(
        self,
        corretora: str,
        mercado: str,
        ativo: str,
        bid: float,
        ask: float,
        recebido_em: float,
        volume_24h_usd: float | None = None,
        variacao_24h_pct: float | None = None,
        simbolo_original: str | None = None,
        cotacao: str | None = None,
    ) -> None:
        """Etapa 1: guarda o melhor bid/ask do par (preco por 1 unidade do ativo, na moeda `cotacao`).

        `simbolo_original` (ex.: "BTC/USDC:USDC") e `cotacao` (ex.: "USDC") dizem de qual par veio
        o preco; o motor precisa da cotacao para converter o preco para USDT.
        """
        self._tickers[(corretora, mercado, ativo)] = {
            "bid": bid,
            "ask": ask,
            "recebido_em": recebido_em,
            "volume_24h_usd": volume_24h_usd,
            "variacao_24h_pct": variacao_24h_pct,
            "simbolo_original": simbolo_original,
            "cotacao": cotacao,
        }

    def atualizar_cotacao(self, corretora: str, simbolo: str, bid: float, ask: float, recebido_em: float) -> None:
        """Guarda o melhor bid/ask de um par de cotacao pelo simbolo do ccxt (ex.: "USDC/USDT"), com o preco cru."""
        self._cotacoes[(corretora, simbolo)] = {"bid": bid, "ask": ask, "recebido_em": recebido_em}

    def remover_cotacao(self, corretora: str, simbolo: str) -> None:
        """Apaga o ticker do par de cotacao (a assinatura dele parou). Se nao existe, nao faz nada."""
        self._cotacoes.pop((corretora, simbolo), None)

    def atualizar_livro(self, livro: Livro, ativo: str | None = None) -> None:
        """Etapa 2: guarda o livro completo. `ativo` e o nome normalizado (padrao: o nome original)."""
        chave = (livro.corretora, livro.mercado, ativo if ativo is not None else livro.simbolo_original)
        self._livros[chave] = livro

    def remover_livro(self, corretora: str, mercado: str, ativo: str, simbolo_original: str | None = None) -> None:
        """Apaga o livro do par: o coletor parou de recebe-lo (assinatura cancelada ou caiu).

        Sem isso, o ultimo livro continuaria valendo por ate `max_idade_livro_s` (o stream de
        tickers mantem a conexao viva) e o motor leria precos congelados.
        simbolo_original: se informado, so apaga se o livro guardado veio desse simbolo, para
        nao apagar o livro que outro simbolo do mesmo ativo esteja entregando agora.
        Livro que nao existe: nao faz nada. O ticker do par nao e tocado.
        """
        chave = (corretora, mercado, ativo)
        livro = self._livros.get(chave)
        if livro is not None and (simbolo_original is None or livro.simbolo_original == simbolo_original):
            del self._livros[chave]

    def remover_ticker(self, corretora: str, mercado: str, ativo: str, simbolo_original: str | None = None) -> None:
        """Apaga o ticker do par: a assinatura dele parou (ticker um a um). O livro do par nao e tocado.

        simbolo_original: se informado, so apaga se o ticker guardado veio desse simbolo.
        """
        chave = (corretora, mercado, ativo)
        ticker = self._tickers.get(chave)
        if ticker is not None and (simbolo_original is None or ticker.get("simbolo_original") == simbolo_original):
            del self._tickers[chave]

    def marcar_desconectada(self, corretora: str) -> None:
        """A conexao caiu: apaga tudo da corretora e o painel passa a mostrar "reconectando".

        Apagar tambem os dados (e nao so a conexao) impede que um livro de antes da
        queda volte a valer quando a corretora reconectar, e os livros dela podem ter
        mudado enquanto estava fora. Sem tickers, a corretora sai de `mercados()` e nenhum
        candidato usa os precos dela; os sinais que dependiam dela sao encerrados no proximo ciclo.
        """
        self._ultima_mensagem.pop(corretora, None)
        for chave in [c for c in self._ultima_do_mercado if c[0] == corretora]:
            del self._ultima_do_mercado[chave]
        self._reconectando.add(corretora)
        for dados in (self._livros, self._tickers, self._cotacoes):
            for chave in [c for c in dados if c[0] == corretora]:
                del dados[chave]

    def marcar_esperando(self, corretora: str) -> None:
        """A corretora ainda nao conectou nenhuma vez (ex.: nao carregou a lista de mercados ao ligar).

        O painel mostra "reconectando" enquanto ela tenta; a primeira conexao dela nao conta como
        reconexao (nao houve queda). Quedas depois de ter conectado contam normalmente.
        """
        self._reconectando.add(corretora)
        self._nunca_conectou.add(corretora)

    def marcar_reconectada(self, corretora: str) -> None:
        """A conexao voltou (chegou a primeira mensagem boa depois da queda): conta uma reconexao.

        So conta se a corretora estava reconectando, e nao conta a primeira conexao de quem nunca tinha
        conectado (marcar_esperando). Chamar de novo nao conta duas vezes (o coletor chama a cada
        mensagem de ticker, e so a primeira depois da queda conta).
        """
        if corretora in self._reconectando:
            self._reconectando.discard(corretora)
            if corretora in self._nunca_conectou:
                self._nunca_conectou.discard(corretora)
            else:
                self._reconexoes[corretora] = self._reconexoes.get(corretora, 0) + 1

    def registrar_atraso(self, corretora: str, atraso_s: float, agora: float) -> None:
        """Anota um atraso medido (horario de chegada - horario que a corretora informou), em segundos.

        Atraso maior que `atraso_max_valido_s`, para mais ou para menos, so acontece com relogio errado
        (o do PC ou o da corretora): fica fora da media, com um aviso no log (uma vez por corretora).
        Diferencas pequenas e negativas (o relogio do PC um pouco atras) entram: tira-las puxaria a media
        para cima.
        """
        if not isinstance(atraso_s, (int, float)) or isinstance(atraso_s, bool) or not math.isfinite(atraso_s):
            return
        if abs(atraso_s) > self._atraso_max_valido_s:
            if corretora not in self._avisados_relogio:
                self._avisados_relogio.add(corretora)
                registro.warning("%s: o horario informado pela corretora difere do relogio do PC em %.1fs (mais que "
                                 "%ss); esse atraso fica fora da media. Confira o relogio do PC (acertar a hora "
                                 "automaticamente)", corretora, atraso_s, self._atraso_max_valido_s)
            return
        bloco = math.floor(agora / BLOCO_ATRASO_S)
        blocos = self._atrasos.setdefault(corretora, deque())
        if blocos and bloco <= blocos[-1][0]:  # mesmo bloco (ou relogio que voltou): soma no ultimo
            blocos[-1][1] += atraso_s
            blocos[-1][2] += 1
        else:
            blocos.append([bloco, atraso_s, 1])
        self._esquecer_blocos_velhos(blocos, bloco)

    @staticmethod
    def _esquecer_blocos_velhos(blocos: deque, bloco_atual: int) -> None:
        """Tira os blocos que ja sairam da janela do atraso medio (mais velhos que JANELA_ATRASO_S)."""
        while blocos and blocos[0][0] <= bloco_atual - _BLOCOS_NA_JANELA:
            blocos.popleft()

    def remover_pares_fora(self, corretora: str, chaves: set[tuple[str, str]]) -> int:
        """Apaga o ticker e o livro dos pares da corretora que NAO estao em `chaves` ({(mercado, ativo)}).

        Usado quando a lista de mercados e recarregada: par que a corretora tirou (ou que deixou de
        formar combinacao) sai de `mercados()` e o sinal dele e encerrado pelo fluxo normal.
        Devolve quantos pares sairam.
        """
        removidos = set()
        for dados in (self._tickers, self._livros):
            for chave in [c for c in dados if c[0] == corretora and (c[1], c[2]) not in chaves]:
                del dados[chave]
                removidos.add(chave)
        return len(removidos)

    # --- leitura (motor) -----------------------------------------------------

    def conexao_viva(self, corretora: str, agora: float, mercado: str | None = None) -> bool:
        """True se a corretora mandou mensagem ha no maximo `max_silencio_s` segundos.

        mercado: so conta a mensagem daquele mercado (ou a sem mercado, que vale para todos). Sem mercado:
        qualquer mensagem da corretora (como no painel).
        """
        ultima = self._ultima_mensagem.get(corretora)
        if mercado is None:
            do_mercado = [h for (c, _), h in self._ultima_do_mercado.items() if c == corretora]
        else:
            do_mercado = [self._ultima_do_mercado.get((corretora, mercado))]
        horarios = [h for h in (ultima, *do_mercado) if h is not None]
        return bool(horarios) and agora - max(horarios) <= self._max_silencio_s

    def obter_livro(self, corretora: str, mercado: str, ativo: str, agora: float) -> Livro | None:
        """O livro do par, ou None se nao existe, a conexao nao esta viva ou ele esta velho demais."""
        livro = self._livros.get((corretora, mercado, ativo))
        if livro is None or not self.conexao_viva(corretora, agora, mercado):
            return None
        if agora - livro.recebido_em > self._max_idade_livro_s:
            return None
        return livro

    def obter_ticker(self, corretora: str, mercado: str, ativo: str, agora: float) -> dict | None:
        """O ticker do par (bid, ask, recebido_em, volume_24h_usd, variacao_24h_pct, simbolo_original, cotacao), ou None.

        Vale a mesma regra do livro: conexao viva (do mercado) e dado com no maximo `max_idade_livro_s`.
        """
        ticker = self._tickers.get((corretora, mercado, ativo))
        if ticker is None or not self.conexao_viva(corretora, agora, mercado):
            return None
        if agora - ticker["recebido_em"] > self._max_idade_livro_s:
            return None
        return ticker

    def obter_cotacao(self, corretora: str, simbolo: str, agora: float) -> dict | None:
        """O ticker do par de cotacao ({"bid", "ask", "recebido_em"}), ou None.

        Mesma regra de obter_ticker: conexao viva (do spot: o par de cotacao e sempre spot) e dado com no maximo
        `max_idade_livro_s`.
        """
        ticker = self._cotacoes.get((corretora, simbolo))
        if ticker is None or not self.conexao_viva(corretora, agora, "spot"):
            return None
        if agora - ticker["recebido_em"] > self._max_idade_livro_s:
            return None
        return ticker

    def atraso_medio_ms(self, corretora: str, agora: float) -> float | None:
        """Atraso medio dos dados da corretora no ultimo minuto, em ms; None se ela nao informa o horario."""
        blocos = self._atrasos.get(corretora)
        if not blocos:
            return None
        self._esquecer_blocos_velhos(blocos, math.floor(agora / BLOCO_ATRASO_S))
        quantidade = sum(b[2] for b in blocos)
        if not quantidade:
            return None
        return round(sum(b[1] for b in blocos) / quantidade * 1000, 1)

    def saude(self, corretora: str, agora: float) -> dict:
        """Saude da corretora para o painel: {"estado", "atraso_medio_ms", "reconexoes"}.

        estado: "reconectando" (caiu e o coletor esta tentando de novo), "conectada" (mensagem ha no
        maximo `max_silencio_s`) ou "atrasada" (silenciosa, ou nunca mandou nada).
        """
        if corretora in self._reconectando:
            estado = "reconectando"
        elif self.conexao_viva(corretora, agora):
            estado = "conectada"
        else:
            estado = "atrasada"
        return {
            "estado": estado,
            "atraso_medio_ms": self.atraso_medio_ms(corretora, agora),
            "reconexoes": self._reconexoes.get(corretora, 0),
        }

    # --- volume 24h pela API REST (coletor e dados lentos) ------------------

    def pedir_volume_rest(self, corretora: str, simbolo: str) -> None:
        """O ticker deste simbolo (do ccxt) chega sem volume 24h: os dados lentos buscam pela API REST."""
        self._volumes_pedidos.setdefault(corretora, set()).add(simbolo)

    def volumes_rest_pedidos(self, corretora: str) -> set[str]:
        """Os simbolos da corretora cujo volume 24h precisa vir pela API REST (veja pedir_volume_rest)."""
        return set(self._volumes_pedidos.get(corretora, ()))

    def registrar_volume_rest(self, corretora: str, simbolo: str, volume: float) -> None:
        """Guarda o volume 24h do simbolo que veio pela API REST (na moeda de cotacao, como o ticker)."""
        self._volumes_rest[(corretora, simbolo)] = volume

    def volume_rest(self, corretora: str, simbolo: str) -> float | None:
        """O ultimo volume 24h do simbolo vindo pela API REST, ou None."""
        return self._volumes_rest.get((corretora, simbolo))

    def ticker_guardado(self, corretora: str, mercado: str, ativo: str) -> dict | None:
        """O ultimo ticker guardado do par, SEM checar idade nem conexao (None se nao existe).

        Serve so para dados de apoio que mudam devagar (moeda de cotacao, volume 24h, variacao).
        Para PRECO, use obter_ticker ou obter_livro, que nunca devolvem dado velho.
        """
        return self._tickers.get((corretora, mercado, ativo))

    def mercados(self) -> dict[str, set[tuple[str, str]]]:
        """Por corretora, os (mercado, ativo) que ja receberam ticker. Ex.: {"gate": {("spot", "BTC")}}.

        Inclui pares com dado velho: quem decide se o dado vale e obter_ticker/obter_livro.
        Uma corretora desconectada (marcar_desconectada) some daqui.
        """
        resultado: dict[str, set[tuple[str, str]]] = {}
        for corretora, mercado, ativo in self._tickers:
            resultado.setdefault(corretora, set()).add((mercado, ativo))
        return resultado


def pares_para_livro(
    spreads_ticker: dict[str, float],
    minimo_pct: float,
    ultima_vez_acima: dict[str, float],
    agora: float,
    manter_s: float = 60,
) -> set[str]:
    """Etapa 2: quais pares devem ter o livro completo aberto agora.

    spreads_ticker: par -> spread de abertura em %, calculado com os tickers (etapa 1).
    minimo_pct: a partir deste spread o par ganha livro (spread_min_livro_pct da config).
    ultima_vez_acima: par -> ultima vez em que ficou acima do minimo. Quem guarda e quem
        chama; esta funcao atualiza o dicionario (anota quem esta acima agora e esquece
        quem passou do prazo).
    manter_s: quanto tempo o par continua com livro depois de cair abaixo do minimo.
    """
    for par, spread in spreads_ticker.items():
        if spread >= minimo_pct:
            ultima_vez_acima[par] = agora

    # Fica com livro quem esta acima agora (acabou de ser anotado) ou caiu ha no maximo manter_s.
    pares = set()
    for par, ultima in list(ultima_vez_acima.items()):
        if agora - ultima <= manter_s:
            pares.add(par)
        else:
            del ultima_vez_acima[par]  # passou do prazo: esquece, para o dicionario nao crescer sem fim
    return pares
