"""Estado em memoria dos mercados e a regra da etapa 2 (quais pares ganham livro completo).

Os coletores escrevem aqui; o motor so le. Quem le nunca recebe dado velho:
`obter_livro` e `obter_ticker` devolvem None quando a conexao da corretora esta
silenciosa ou quando o dado ficou parado alem do limite.
"""

from scanner.modelos import Livro


class EstadoMercados:
    """Guarda, por corretora, o horario da ultima mensagem e o ultimo ticker e livro de cada par.

    max_silencio_s: maximo de segundos sem NENHUMA mensagem da corretora (o stream de
        tickers serve de heartbeat) antes de ela ser tratada como atrasada.
    max_idade_livro_s: maximo de segundos que um livro (ou ticker) pode ficar parado.
        O WebSocket so manda quando o livro muda, entao parado ainda vale ate aqui.

    Os dois valores vem da Config (max_silencio_conexao_s e max_idade_livro_s).
    Chave de tudo: (corretora, mercado, ativo normalizado).
    """

    def __init__(self, max_silencio_s: float = 2, max_idade_livro_s: float = 30):
        self._max_silencio_s = max_silencio_s
        self._max_idade_livro_s = max_idade_livro_s
        self._ultima_mensagem: dict[str, float] = {}  # corretora -> horario da ultima mensagem
        self._livros: dict[tuple[str, str, str], Livro] = {}
        self._tickers: dict[tuple[str, str, str], dict] = {}

    # --- escrita (coletores) -------------------------------------------------

    def registrar_mensagem(self, corretora: str, agora: float) -> None:
        """Anota que a corretora mandou alguma mensagem agora (ticker, livro ou heartbeat)."""
        self._ultima_mensagem[corretora] = agora

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

    def atualizar_livro(self, livro: Livro, ativo: str | None = None) -> None:
        """Etapa 2: guarda o livro completo. `ativo` e o nome normalizado (padrao: o nome original)."""
        chave = (livro.corretora, livro.mercado, ativo if ativo is not None else livro.simbolo_original)
        self._livros[chave] = livro

    def marcar_desconectada(self, corretora: str) -> None:
        """A conexao caiu: apaga tudo da corretora.

        Apagar tambem os dados (e nao so a conexao) impede que um livro de antes da
        queda volte a valer quando a corretora reconectar, e os livros dela podem ter
        mudado enquanto estava fora.
        """
        self._ultima_mensagem.pop(corretora, None)
        for dados in (self._livros, self._tickers):
            for chave in [c for c in dados if c[0] == corretora]:
                del dados[chave]

    # --- leitura (motor) -----------------------------------------------------

    def conexao_viva(self, corretora: str, agora: float) -> bool:
        """True se a corretora mandou mensagem ha no maximo `max_silencio_s` segundos."""
        ultima = self._ultima_mensagem.get(corretora)
        return ultima is not None and agora - ultima <= self._max_silencio_s

    def obter_livro(self, corretora: str, mercado: str, ativo: str, agora: float) -> Livro | None:
        """O livro do par, ou None se nao existe, a conexao nao esta viva ou ele esta velho demais."""
        livro = self._livros.get((corretora, mercado, ativo))
        if livro is None or not self.conexao_viva(corretora, agora):
            return None
        if agora - livro.recebido_em > self._max_idade_livro_s:
            return None
        return livro

    def obter_ticker(self, corretora: str, mercado: str, ativo: str, agora: float) -> dict | None:
        """O ticker do par (bid, ask, recebido_em, volume_24h_usd, variacao_24h_pct, simbolo_original, cotacao), ou None.

        Vale a mesma regra do livro: conexao viva e dado com no maximo `max_idade_livro_s`.
        """
        ticker = self._tickers.get((corretora, mercado, ativo))
        if ticker is None or not self.conexao_viva(corretora, agora):
            return None
        if agora - ticker["recebido_em"] > self._max_idade_livro_s:
            return None
        return ticker

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
