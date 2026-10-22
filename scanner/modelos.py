"""Tipos de dados compartilhados entre os modulos do scanner."""

from dataclasses import dataclass, field
from typing import Literal

from scanner.metricas.janela import Metricas


@dataclass
class Livro:
    """Foto do livro de ofertas de um par numa corretora, no momento em que chegou.

    Cada nivel e uma tupla (preco, quantidade). Os bids vem do maior preco
    para o menor, e os asks do menor para o maior: o primeiro de cada lado
    e sempre o melhor preco.
    """

    corretora: str  # ex.: "bitget"
    mercado: Literal["spot", "futuro"]
    simbolo_original: str  # nome do par como o coletor recebeu, ex.: "BTC/USDT:USDT"
    bids: list[tuple[float, float]]  # ofertas de compra
    asks: list[tuple[float, float]]  # ofertas de venda
    recebido_em: float  # quando o scanner recebeu (segundos desde 1970, relogio local)
    ts_corretora: float | None  # horario informado pela corretora (segundos); None se nao informou


@dataclass
class Ticker:
    """Melhor bid e melhor ask de um par, vindos do stream leve de tickers (etapa 1).

    Os valores estao como a corretora entrega (ex.: "1000PEPE" em preco de 1000 unidades).
    """

    corretora: str  # ex.: "mexc"
    mercado: Literal["spot", "futuro"]
    simbolo_original: str  # nome do par como o coletor recebeu, ex.: "BTC/USDT:USDT"
    bid: float  # melhor preco de compra
    ask: float  # melhor preco de venda
    recebido_em: float  # quando o scanner recebeu (segundos desde 1970, relogio local)
    volume_24h_usd: float | None  # volume das ultimas 24h na moeda de cotacao (aprox. USD); None se nao veio
    variacao_24h_pct: float | None  # variacao do preco em 24h, em %; None se nao veio


@dataclass
class Ponta:
    """Uma ponta (long ou short) de um sinal: os mesmos campos de ponta_long/ponta_short da spec (secao 5).

    Na ponta spot os campos de funding ficam None. Campo que a corretora nao informou tambem e None.
    """

    corretora: str  # ex.: "bitget"
    mercado: Literal["spot", "futuro"]
    simbolo_original: str  # nome do par na corretora, ex.: "AKAMUSDT"
    preco: float  # preco de execucao em USDT, por 1 unidade do ativo
    lote_usd: float  # quanto o livro aguenta agora, em $
    lote_medio_usd: float | None  # lote medio no periodo, em $; None se ainda nao ha amostras
    volume_24h_usd: float | None  # volume das ultimas 24h, em $
    funding_pct: float | None  # funding da proxima cobranca, em %; None no spot
    funding_intervalo_h: float | None  # de quantas em quantas horas cobra (1, 4, 8); None no spot
    limite_ordem_usd: float | None  # tamanho maximo de ordem que a corretora aceita, em $; None = nao informou
    variacao_24h_pct: float | None  # variacao do preco em 24h, em %
    taxa_taker_pct: float  # taxa taker da corretora, em %


@dataclass(kw_only=True)
class Candidato:
    """Oportunidade calculada pelo motor, antes dos filtros: os campos do sinal da spec (secao 5).

    Faltam so id, detectado_em, atualizado_em e encerrado_em, que o gerenciador de sinais
    (Task 8) acrescenta ao serializar. Percentuais em % (0.40 = 0,40%).
    """

    par: str  # ex.: "AKAM/USDT"
    tipo: Literal["spot_futuro", "futuro_futuro"]
    modo: Literal["lucro", "farm"]
    etiquetas: list[str] = field(default_factory=list)  # ex.: ["STOCK"]
    suspeito: bool = False
    aviso_cotacao: bool = False  # True quando o preco em USDC, USDG ou USD foi convertido 1:1 por falta de cotacao
    ponta_long: Ponta
    ponta_short: Ponta
    spread_abertura_pct: float  # A atual
    spread_fechamento_pct: float  # F atual
    metricas_periodo: Metricas | None  # None enquanto nao ha amostras no periodo
    fr_yield_8h_pct: float
    rp_bruto_pct: float
    custo_taxas_pct: float
    funding_esperado_pct: float
    rp_liquido_pct: float
    custo_por_1000_usd: float | None = None  # so preenchido no modo Farm
    tamanho_referencia_usd: float

    @property
    def chave(self) -> tuple:
        """Identifica o sinal: par, tipo e (corretora, mercado) de cada ponta. Usada na anti-repeticao."""
        return (
            self.par,
            self.tipo,
            self.ponta_long.corretora,
            self.ponta_long.mercado,
            self.ponta_short.corretora,
            self.ponta_short.mercado,
        )

    @property
    def ativo(self) -> str:
        """A moeda do par, ex.: "AKAM" para "AKAM/USDT"."""
        return self.par.split("/")[0]
