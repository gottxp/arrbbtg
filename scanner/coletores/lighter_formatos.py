"""Formatos da API da Lighter: leitura das mensagens e juncao do livro (funcoes puras, sem rede).

Documentacao oficial (https://apidocs.lighter.xyz, lida em 22/10/2026):
  - orderBookDetails (REST): os perpetuos ficam em `order_book_details`, com market_id, symbol ("ETH",
    "1000PEPE"), status ("active"/"inactive"), taker_fee (texto, em %), size_decimals e price_decimals.
  - order_book (WebSocket): a primeira mensagem depois de assinar ("subscribed/order_book") e o livro
    completo; as seguintes ("update/order_book") so os niveis que mudaram. Cada nivel e {"price", "size"} em
    texto; o tamanho e o total novo do nivel e "0" quer dizer que ele sai. O begin_nonce de cada mudanca
    precisa ser o nonce da mensagem anterior; senao, faltou mudanca e o livro tem de ser pedido de novo.
  - market_stats (WebSocket): best_bid_price, best_ask_price, daily_quote_token_volume, daily_price_change,
    current_funding_rate... (em texto ou numero).
O coletor (scanner/coletores/lighter.py) usa estas funcoes; os testes tambem, direto.
"""

import heapq
import math
import re
from dataclasses import dataclass, field

from scanner.config.carregar import Config
from scanner.modelos import Livro, Ticker

# Nome da corretora no scanner (config.yaml) -> instancia da Lighter.
CORRETORAS_LIGHTER = {"lighter": "principal", "lighter_rh": "robinhood"}
NOME_DA_INSTANCIA = {instancia: nome for nome, instancia in CORRETORAS_LIGHTER.items()}
# Moeda de cotacao (e de garantia) de cada instancia: USDC na principal, USDG na Robinhood (docs/lighter-rh).
COTACAO_DA_INSTANCIA = {"principal": "USDC", "robinhood": "USDG"}
# A Lighter cobra funding a cada hora cheia (docs.lighter.xyz/trading/funding). O intervalo_funding_h do
# config.yaml vale sobre este.
INTERVALO_FUNDING_LIGHTER_H = 1
# Canal da etapa 1: as estatisticas de todos os mercados numa assinatura so.
CANAL_TICKERS = "market_stats/all"
# Erros que a Lighter manda e que nao sao problema por si: 30003 = ja estava assinado, 30002 = nao estava
# assinado (o coletor so age se for o livro de um par que ainda espera o livro completo).
JA_ASSINADO, NAO_ASSINADO = 30003, 30002
ERROS_INOFENSIVOS = frozenset({NAO_ASSINADO, JA_ASSINADO})

# Resultados de LivroJuntado.juntar_mudancas.
CONFERIDA, SEM_NONCE, IGNORADA, QUEBRADA, INVALIDA = "conferida", "sem_nonce", "ignorada", "quebrada", "invalida"


class ErroApiLighter(Exception):
    """A API REST da Lighter respondeu erro ou algo num formato que o scanner nao entende."""


def e_lighter(nome: str) -> bool:
    """True se a corretora usa o conector proprio da Lighter ("lighter" ou "lighter_rh")."""
    return nome in CORRETORAS_LIGHTER


def nome_da_instancia(instancia: str) -> str:
    """"principal" -> "lighter", "robinhood" -> "lighter_rh". Outra coisa: ValueError."""
    if instancia not in NOME_DA_INSTANCIA:
        raise ValueError(f"instancia da Lighter desconhecida: {instancia!r} (use principal ou robinhood)")
    return NOME_DA_INSTANCIA[instancia]


def endereco_somente_leitura(url: str) -> str:
    """O endereco do WebSocket com ?readonly=true (so leitura; vale tambem em regiao restrita)."""
    _, interrogacao, consulta = url.partition("?")
    if not interrogacao:
        return f"{url}?readonly=true"
    if "readonly=true" in consulta.split("&"):
        return url
    return f"{url}&readonly=true"


def ler_numero(valor) -> float | None:
    """Numero real de um campo da Lighter (ela manda precos em texto, ex.: "2064.50"), ou None.

    bool, NaN, infinito e texto que nao e numero contam como None.
    """
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        numero = float(valor)
    elif isinstance(valor, str):
        try:
            numero = float(valor)
        except ValueError:
            return None
    else:
        return None
    return numero if math.isfinite(numero) else None


def ler_inteiro(valor) -> int | None:
    """Numero inteiro (int ou texto com digitos, ex.: market_id e nonce), ou None. bool nao conta."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, str) and valor.strip().lstrip("-").isdigit():
        return int(valor)
    return None


def _passo(decimais) -> float | None:
    """10^-decimais (o menor passo de preco ou tamanho), ou None se nao veio."""
    casas = ler_inteiro(decimais)
    return None if casas is None or casas < 0 else 10.0 ** -casas


# ---------------------------------------------------------------------------
# Mercados (orderBookDetails)
# ---------------------------------------------------------------------------


def markets_de_lighter(resposta: dict, cotacao: str) -> tuple[dict, int]:
    """Os perpetuos ativos do orderBookDetails no estilo do ccxt, e quantos inativos ficaram de fora.

    cotacao: "USDC" (principal) ou "USDG" (Robinhood). Devolve ({simbolo: market}, inativos), com o simbolo
    como o ccxt escreveria um perpetuo ("ETH/USDC:USDC"). O spot (`spot_order_book_details`) nao entra.
    Cada market guarda o market_id em "id", a taxa taker em "taker" (fracao, como o ccxt) e os decimais em
    "precision"; o item original fica em "info". Item malformado (sem simbolo ou sem market_id) e pulado.

    Taxa: o taker_fee vem em % (como os outros campos de taxa da Lighter, ex.: liquidation_fee "1.0000" = 1%);
    "0.0000" e a conta Standard (0%). Taxa ilegivel vira None (o motor descarta por falta de taxa).
    contractSize = 1: o tamanho dos niveis do WebSocket ja vem em unidades do ativo (o ccxt usa o
    quote_multiplier como contractSize, mas isso so mudaria a quantidade, e a quantidade da Lighter ja e a do
    ativo; veja o relatorio da Task 16).
    """
    detalhes = resposta.get("order_book_details") if isinstance(resposta, dict) else None
    if not isinstance(detalhes, list):
        raise ErroApiLighter("a resposta do orderBookDetails veio sem a lista order_book_details (a API mudou?)")
    markets: dict[str, dict] = {}
    inativos = 0
    for detalhe in detalhes:
        if not isinstance(detalhe, dict):
            continue
        base, ident = detalhe.get("symbol"), ler_inteiro(detalhe.get("market_id"))
        if not isinstance(base, str) or not base or "/" in base or ident is None:
            continue
        if detalhe.get("market_type") not in (None, "perp"):
            continue
        if detalhe.get("status") != "active":
            inativos += 1
            continue
        taxa_pct = ler_numero(detalhe.get("taker_fee"))
        simbolo = f"{base}/{cotacao}:{cotacao}"
        markets[simbolo] = {
            "id": str(ident), "symbol": simbolo, "base": base, "quote": cotacao, "settle": cotacao,
            "type": "swap", "spot": False, "swap": True, "future": False, "option": False,
            "linear": True, "inverse": False, "contractSize": 1.0, "active": True,
            "taker": taxa_pct / 100 if taxa_pct is not None and taxa_pct >= 0 else None,
            "precision": {"amount": _passo(detalhe.get("size_decimals")),
                          "price": _passo(detalhe.get("price_decimals"))},
            "info": detalhe,
        }
    return markets, inativos


# ---------------------------------------------------------------------------
# Livro (order_book)
# ---------------------------------------------------------------------------


def niveis_de_lighter(lista) -> dict[float, float] | None:
    """{preco: tamanho} dos niveis [{"price": "...", "size": "..."}] da Lighter, ou None se algum for invalido.

    Preco precisa ser numero real maior que zero; tamanho, numero real maior ou igual a zero (nas mudancas,
    0 = o nivel sai). Lado que nao veio conta como vazio.
    """
    if lista is None:
        return {}
    if not isinstance(lista, list):
        return None
    niveis = {}
    for nivel in lista:
        if not isinstance(nivel, dict):
            return None
        preco, tamanho = ler_numero(nivel.get("price")), ler_numero(nivel.get("size"))
        if preco is None or tamanho is None or preco <= 0 or tamanho < 0:
            return None
        niveis[preco] = tamanho
    return niveis


def sem_zeros(niveis: dict[float, float]) -> dict[float, float]:
    """Os niveis com tamanho maior que zero (no livro completo, tamanho 0 nao e nivel)."""
    return {preco: tamanho for preco, tamanho in niveis.items() if tamanho > 0}


def montar_livro(corretora: str, simbolo: str, bids: dict[float, float], asks: dict[float, float],
                 agora: float, ts_ms: float | None, niveis: int = Config.niveis_livro_lighter) -> Livro | None:
    """O Livro com os `niveis` melhores niveis de cada lado, em ordem (melhor preco primeiro), ou None.

    bids/asks: {preco: tamanho}, so com tamanho maior que zero. As mesmas regras do livro_de_ccxt: lado
    vazio, preco <= 0 (ja barrado em niveis_de_lighter) ou melhor bid maior ou igual ao melhor ask descartam o
    livro. niveis: niveis_livro_lighter da Config (a Lighter manda o livro inteiro).
    """
    if not bids or not asks:
        return None
    precos_bids = heapq.nlargest(niveis, bids)
    precos_asks = heapq.nsmallest(niveis, asks)
    if precos_bids[0] >= precos_asks[0]:  # cruzado: a melhor compra paga mais que a melhor venda
        return None
    return Livro(corretora=corretora, mercado="futuro", simbolo_original=simbolo,
                 bids=[(p, bids[p]) for p in precos_bids], asks=[(p, asks[p]) for p in precos_asks],
                 recebido_em=agora, ts_corretora=None if ts_ms is None else ts_ms / 1000)  # a Lighter manda ms


def livro_de_lighter(msg: dict, instancia: str, agora: float, simbolo: str | None = None,
                     niveis: int = Config.niveis_livro_lighter) -> Livro | None:
    """Transforma uma mensagem do canal order_book com o livro COMPLETO em um Livro (ou None, descartado).

    msg: a primeira mensagem depois de assinar ("subscribed/order_book"), no formato documentado:
        {"channel": "order_book:0", "order_book": {"bids": [{"price", "size"}], "asks": [...], "nonce", ...},
         "timestamp": ms, "type": ...}.
    instancia: "principal" (corretora "lighter") ou "robinhood" ("lighter_rh").
    simbolo: o simbolo do scanner (ex.: "ETH/USDC:USDC"); sem ele, o market_id do canal ("0").
    As mesmas regras de descarte do livro_de_ccxt: vazio (ou um lado so), preco zero, negativo ou invalido,
    cruzado (bid >= ask). Os niveis sao postos em ordem; os de tamanho 0 nao entram, e so os `niveis`
    melhores de cada lado ficam. Precos crus, como a Lighter manda (o coletor converte "1000PEPE" para 1
    unidade do PEPE ao guardar no estado).
    """
    corretora = nome_da_instancia(instancia)
    dados = msg.get("order_book") if isinstance(msg, dict) else None
    if not isinstance(dados, dict):
        return None
    bids, asks = niveis_de_lighter(dados.get("bids")), niveis_de_lighter(dados.get("asks"))
    if bids is None or asks is None:
        return None
    if simbolo is None:
        simbolo = str(msg.get("channel") or "").partition(":")[2]
    return montar_livro(corretora, simbolo, sem_zeros(bids), sem_zeros(asks), agora, ler_numero(msg.get("timestamp")),
                        niveis)


@dataclass
class LivroJuntado:
    """O livro de um par montado com o livro completo e as mudancas que chegam depois (sem rede).

    `pronto` so fica True depois do livro completo: antes dele, mudanca nao vale (nao ha onde junta-la).
    """

    bids: dict[float, float] = field(default_factory=dict)  # preco -> tamanho (so tamanho > 0)
    asks: dict[float, float] = field(default_factory=dict)
    nonce: int | None = None  # nonce da ultima mensagem aplicada (None = a Lighter nao mandou)
    pronto: bool = False

    def receber_completo(self, dados: dict) -> bool:
        """Troca o guardado pelo livro completo. False (nada muda) se algum nivel for invalido."""
        bids, asks = niveis_de_lighter(dados.get("bids")), niveis_de_lighter(dados.get("asks"))
        if bids is None or asks is None:
            return False
        self.bids, self.asks = sem_zeros(bids), sem_zeros(asks)
        self.nonce = ler_inteiro(dados.get("nonce"))
        self.pronto = True
        return True

    def juntar_mudancas(self, dados: dict) -> tuple[str, str]:
        """Junta as mudancas no livro guardado. Devolve (resultado, detalhe para o log).

        resultado: CONFERIDA (o begin_nonce bate com o nonce anterior), SEM_NONCE (aplicada sem conferir: falta
        o begin_nonce ou o nonce anterior), IGNORADA (o livro completo ainda nao chegou), QUEBRADA (o
        begin_nonce nao bate: nada aplicado) ou INVALIDA (nivel invalido: nada aplicado).
        """
        if not self.pronto:
            return IGNORADA, "mudanca antes do livro completo"
        inicio = ler_inteiro(dados.get("begin_nonce"))
        conferivel = inicio is not None and self.nonce is not None
        if conferivel and inicio != self.nonce:
            return QUEBRADA, f"begin_nonce {inicio}, esperado {self.nonce}"
        bids, asks = niveis_de_lighter(dados.get("bids")), niveis_de_lighter(dados.get("asks"))
        if bids is None or asks is None:
            return INVALIDA, "mudanca com nivel invalido"
        for lado, mudancas in ((self.bids, bids), (self.asks, asks)):
            for preco, tamanho in mudancas.items():
                if tamanho == 0:
                    lado.pop(preco, None)  # tamanho 0: o nivel sai
                else:
                    lado[preco] = tamanho  # o tamanho e o total novo do nivel
        self.nonce = ler_inteiro(dados.get("nonce"))
        return (CONFERIDA, "") if conferivel else (SEM_NONCE, "sem begin_nonce/nonce")

    def esvaziar(self) -> None:
        """Esquece o guardado (o livro deixou de ser confiavel): espera um livro completo novo."""
        self.bids, self.asks, self.nonce, self.pronto = {}, {}, None, False

    def montar(self, corretora: str, simbolo: str, agora: float, ts_ms: float | None, niveis: int) -> Livro | None:
        """O Livro do que esta guardado (veja montar_livro), ou None se vazio ou cruzado."""
        return montar_livro(corretora, simbolo, self.bids, self.asks, agora, ts_ms, niveis)


# ---------------------------------------------------------------------------
# market_stats e erros
# ---------------------------------------------------------------------------


def ticker_de_lighter(corretora: str, simbolo: str, stats: dict, agora: float) -> Ticker | None:
    """O Ticker de um mercado do market_stats, ou None se faltar bid/ask valido (zero, cruzado, ilegivel).

    Volume = daily_quote_token_volume (na moeda de cotacao, USDC ou USDG); variacao = daily_price_change (%).
    """
    bid, ask = ler_numero(stats.get("best_bid_price")), ler_numero(stats.get("best_ask_price"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or bid >= ask:
        return None
    return Ticker(corretora=corretora, mercado="futuro", simbolo_original=simbolo, bid=bid, ask=ask, recebido_em=agora,
                  volume_24h_usd=ler_numero(stats.get("daily_quote_token_volume")),
                  variacao_24h_pct=ler_numero(stats.get("daily_price_change")))


def itens_do_market_stats(mensagem: dict) -> list[dict]:
    """As estatisticas de cada mercado numa mensagem market_stats (o canal "all" manda {market_id: ...})."""
    dados = mensagem.get("market_stats")
    if isinstance(dados, dict) and "market_id" in dados:
        return [dados]  # canal de um mercado so (market_stats/{id})
    if isinstance(dados, dict):
        return [item for item in dados.values() if isinstance(item, dict)]
    return []


_CANAL_DO_LIVRO = re.compile(r"order_book[:/](\d+)")


def livro_do_erro(texto: str) -> str | None:
    """O market_id do livro citado numa mensagem de erro (ex.: "Already Subscribed to : order_book:0" -> "0")."""
    achado = _CANAL_DO_LIVRO.search(texto or "")
    return achado.group(1) if achado else None
