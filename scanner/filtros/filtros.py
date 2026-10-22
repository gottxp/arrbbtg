"""Filtros, modos Lucro e Farm e condicao de sinal (especificacao, secao 4).

`passa` diz se um candidato pode virar sinal. `Filtros` guarda os filtros que o dono
configura no painel (um grupo salvo = um `Filtros`). Tudo em memoria e sem rede.

Regra geral dos filtros: campo vazio (None ou lista vazia) = filtro desligado. Quando um
filtro esta LIGADO e o dado que ele precisa e desconhecido (None), o candidato NAO passa:
dado em que nao se pode confiar nunca gera sinal. A unica excecao e o limite de ordem
(None = a corretora nao informa limite = sem limite).
"""

from dataclasses import asdict, dataclass, field, fields
from typing import Literal

from scanner.config.carregar import Config
from scanner.modelos import Candidato, Ponta

# Custo a cada $1.000 de volume = -RP liquido x 5 (spec secao 4). De onde vem o 5: cada
# operacao gera volume ao abrir e ao fechar (2 x o tamanho dela), entao $1.000 de volume sao
# operacoes de $500. Como o RP e em %, o custo em $ e RP/100 x 500 = RP x 5.
FATOR_CUSTO_POR_1000 = 5

# Maior "Periodo das metricas" aceito num grupo de filtros ou na sessao, em horas. O banco so guarda
# `retencao_dias` de amostras, e uma janela maior nunca descartaria amostra (a memoria cresceria sem fim). O motor
# acerta este valor ao ser criado (retencao_dias x 24 da Config dele, veja limitar_periodo_metricas); antes disso
# vale o padrao da Config (7 dias = 168 h). Um scanner tem um motor so.
_periodo_max_h: float = Config.retencao_dias * 24


def limitar_periodo_metricas(horas: float) -> None:
    """Define o maior periodo das metricas que `Filtros.de_dict` aceita sem `periodo_max_h` (o motor chama)."""
    global _periodo_max_h
    _periodo_max_h = horas


def custo_por_1000(rp_liquido_pct: float) -> float:
    """Custo em $ para gerar $1.000 de volume na corretora de farm. Negativo = a operacao da lucro."""
    return -rp_liquido_pct * FATOR_CUSTO_POR_1000


@dataclass
class Filtros:
    """Um grupo de filtros (spec secao 4). None ou lista vazia = filtro desligado."""

    # --- Corretoras ---
    # Cada item e "corretora" (qualquer mercado) ou "corretora:mercado", ex.: "bitget:spot".
    corretoras_lado1: list[str] = field(default_factory=list)  # ponta LONG (a que compra)
    corretoras_lado2: list[str] = field(default_factory=list)  # ponta SHORT (a que vende)
    tipos: list[str] = field(default_factory=list)  # "spot_futuro" e/ou "futuro_futuro"

    # --- Geral ---
    periodo_metricas_h: float | None = None  # guardado no grupo; quem aplica e o motor
    volume_min_usd: float | None = None  # volume 24h minimo, nas duas pontas
    spread_min_pct: float | None = None  # spread de abertura (A atual) minimo

    # --- Liquidez ---
    lote_min_usd: float | None = None  # sobe o lote minimo obrigatorio; nunca baixa dele
    lote_medio_min_usd: float | None = None  # lote medio do periodo, nas duas pontas
    limite_ordem_min_usd: float | None = None  # tamanho maximo de ordem aceito, nas duas pontas

    # --- Spreads do periodo (lidos de metricas_periodo) ---
    maior_abertura_min_pct: float | None = None
    maior_fechamento_min_pct: float | None = None
    abertura_media_min_pct: float | None = None
    fechamento_medio_min_pct: float | None = None

    # --- Funding ---
    funding_min_pct: float | None = None  # por ponta futura
    funding_max_pct: float | None = None  # por ponta futura
    fr_yield_min_pct: float | None = None
    intervalos_funding: list[float] = field(default_factory=list)  # horas aceitas: 1, 4, 8
    # Aproximacao: a spec nao tem o horario da proxima cobranca, entao "sincronizado" aqui
    # significa que as pontas futuras cobram no MESMO INTERVALO.
    funding_sincronizado: bool = False

    # --- Invertidas ---
    invertidas_min: int | None = None  # quantidade minima no periodo
    invertidas_duracao_min_s: float | None = None  # guardado no grupo; quem aplica e o motor

    # --- Modos: sobrescrevem a Config so neste grupo ---
    rp_min_pct: float | None = None  # modo Lucro (a Config tem rp_min_lucro_pct)
    custo_max_farm_pct: float | None = None  # modo Farm (negativo, como na Config)

    # --- Organizacao (listas de moedas, ex.: ["BTC", "ETH"]; maiusculas ou minusculas) ---
    mostrar_suspeitos: bool = False
    lista_negra: list[str] = field(default_factory=list)  # moedas excluidas
    nunca_mostrar: list[str] = field(default_factory=list)
    apenas_mostrar: list[str] = field(default_factory=list)  # vazia = todas as moedas
    sempre_mostrar: list[str] = field(default_factory=list)  # ignora filtros e regra do modo

    # --- Guardados no grupo, usados so pelo motor e pelo painel (passa nao le) ---
    metrica_spread: str | None = None  # "medio", "maior" ou "media_10_maiores"
    ordenar_por: str | None = None  # resultado, horario, spread ou volume
    fixados: list = field(default_factory=list)  # sinais fixados no topo; o painel define o formato

    def para_dict(self) -> dict:
        """Todos os campos num dicionario so com tipos de JSON, para salvar o grupo."""
        return asdict(self)

    @classmethod
    def de_dict(cls, dados: dict, periodo_max_h: float | None = None) -> "Filtros":
        """Cria os filtros a partir de um dicionario salvo. Campo que falta fica desligado.

        Levanta ValueError com o nome do campo se houver chave desconhecida, valor com tipo errado ou
        periodo_metricas_h acima de `periodo_max_h` (padrao: o do motor, retencao_dias x 24; veja
        limitar_periodo_metricas). A API devolve essa mensagem com o erro 400.
        """
        if not isinstance(dados, dict):
            raise ValueError("filtros: precisa ser um dicionario campo -> valor")
        conhecidos = {campo.name for campo in fields(cls)}
        desconhecidos = [str(chave) for chave in dados if chave not in conhecidos]
        if desconhecidos:
            raise ValueError("filtros: campo(s) desconhecido(s): " + ", ".join(desconhecidos))
        _conferir_periodo(dados.get("periodo_metricas_h"), _periodo_max_h if periodo_max_h is None else periodo_max_h)
        return cls(**{campo: _validar_valor(campo, valor) for campo, valor in dados.items()})


# Campos que `passa` le. De_dict confere o tipo deles, porque um grupo salvo errado nao pode
# derrubar o motor no meio do ciclo. Os campos "so guardados" (periodo, metrica, ordem) nao
# sao conferidos aqui: quem os usa confere.
_LISTAS_DE_TEXTO = (
    "corretoras_lado1", "corretoras_lado2", "tipos",
    "lista_negra", "nunca_mostrar", "apenas_mostrar", "sempre_mostrar",
)
_NUMEROS = (
    "volume_min_usd", "spread_min_pct", "lote_min_usd", "lote_medio_min_usd", "limite_ordem_min_usd",
    "maior_abertura_min_pct", "maior_fechamento_min_pct", "abertura_media_min_pct",
    "fechamento_medio_min_pct", "funding_min_pct", "funding_max_pct", "fr_yield_min_pct",
    "invertidas_min", "rp_min_pct", "custo_max_farm_pct",
)
_BOOLEANOS = ("mostrar_suspeitos", "funding_sincronizado")


def _e_numero(valor) -> bool:
    """True para int ou float. bool nao conta (True seria 1 sem querer)."""
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def _conferir_periodo(periodo, maximo: float) -> None:
    """Periodo das metricas (numero) acima do maximo e recusado: ValueError com a mensagem para o painel."""
    if _e_numero(periodo) and periodo > maximo:
        raise ValueError(f"periodo_metricas_h: no maximo {maximo:g} horas (retencao_dias x 24: o banco so guarda "
                         f"essas horas de amostras); recebi {periodo:g}")


def _validar_valor(campo: str, valor):
    """Confere o tipo de um campo do dicionario e devolve o valor pronto (listas sao copiadas)."""
    if campo in _LISTAS_DE_TEXTO:
        if valor is None:
            return []
        if not isinstance(valor, list) or not all(isinstance(item, str) for item in valor):
            raise ValueError(f"{campo}: precisa ser uma lista de textos")
        return list(valor)
    if campo == "intervalos_funding":
        if valor is None:
            return []
        if not isinstance(valor, list) or not all(_e_numero(item) for item in valor):
            raise ValueError(f"{campo}: precisa ser uma lista de numeros (horas)")
        return list(valor)
    if campo == "fixados":
        if valor is None:
            return []
        if not isinstance(valor, list):
            raise ValueError(f"{campo}: precisa ser uma lista")
        return list(valor)
    if campo in _NUMEROS and valor is not None and not _e_numero(valor):
        raise ValueError(f"{campo}: precisa ser um numero (ou vazio para desligar)")
    if campo in _BOOLEANOS and not isinstance(valor, bool):
        raise ValueError(f"{campo}: precisa ser verdadeiro ou falso")
    return valor


# ---------------------------------------------------------------------------
# passa: a decisao
# ---------------------------------------------------------------------------

def _normal(texto: str) -> str:
    """Texto sem espacos nas pontas e sem diferenca de maiusculas, para comparar nomes."""
    return texto.strip().casefold()


def _moeda_na_lista(ativo: str, moedas: list[str]) -> bool:
    return _normal(ativo) in {_normal(moeda) for moeda in moedas}


def _ponta_na_lista(ponta: Ponta, entradas: list[str]) -> bool:
    """True se a ponta bate com algum item "corretora" ou "corretora:mercado"."""
    for entrada in entradas:
        corretora, _, mercado = _normal(entrada).partition(":")
        if corretora == _normal(ponta.corretora) and (not mercado or mercado == ponta.mercado):
            return True
    return False


def _passa_no_modo(c: Candidato, f: Filtros, modo: str, cfg: Config, comparacao: bool) -> bool:
    """Regra do modo. Em modo comparacao o RP usado e o bruto (como o Arbimax), nao o liquido."""
    rp = c.rp_bruto_pct if comparacao else c.rp_liquido_pct
    if modo == "lucro":
        minimo = cfg.rp_min_lucro_pct if f.rp_min_pct is None else f.rp_min_pct
        return rp >= minimo
    # Farm: pelo menos uma ponta numa corretora de farm e custo dentro do maximo.
    # custo_max_farm_pct e negativo (-0.1 = aceita perder ate 0,1%), entao a conta e rp >= valor.
    farm = {_normal(corretora) for corretora in cfg.corretoras_farm}
    if not any(_normal(p.corretora) in farm for p in (c.ponta_long, c.ponta_short)):
        return False
    maximo = cfg.custo_max_farm_pct if f.custo_max_farm_pct is None else f.custo_max_farm_pct
    return rp >= maximo


def _passa_corretoras_e_tipos(c: Candidato, f: Filtros) -> bool:
    if f.corretoras_lado1 and not _ponta_na_lista(c.ponta_long, f.corretoras_lado1):
        return False
    if f.corretoras_lado2 and not _ponta_na_lista(c.ponta_short, f.corretoras_lado2):
        return False
    return not f.tipos or c.tipo in f.tipos


def _passa_liquidez(c: Candidato, f: Filtros) -> bool:
    pontas = (c.ponta_long, c.ponta_short)
    if f.volume_min_usd is not None:
        if not all(p.volume_24h_usd is not None and p.volume_24h_usd >= f.volume_min_usd for p in pontas):
            return False
    if f.spread_min_pct is not None and c.spread_abertura_pct < f.spread_min_pct:
        return False
    if f.lote_medio_min_usd is not None:
        if not all(p.lote_medio_usd is not None and p.lote_medio_usd >= f.lote_medio_min_usd for p in pontas):
            return False
    if f.limite_ordem_min_usd is not None:
        # Limite desconhecido (None) = a corretora nao informa limite = sem limite: passa.
        if not all(p.limite_ordem_usd is None or p.limite_ordem_usd >= f.limite_ordem_min_usd for p in pontas):
            return False
    return True


def _passa_metricas(c: Candidato, f: Filtros) -> bool:
    """Filtros dos spreads do periodo e das invertidas. Sem metricas, qualquer um ligado reprova."""
    exigencias = [
        (f.maior_abertura_min_pct, "maior_abertura"),
        (f.maior_fechamento_min_pct, "maior_fechamento"),
        (f.abertura_media_min_pct, "abertura_media"),
        (f.fechamento_medio_min_pct, "fechamento_medio"),
        (f.invertidas_min, "invertidas"),
    ]
    ligadas = [(minimo, campo) for minimo, campo in exigencias if minimo is not None]
    if not ligadas:
        return True
    if c.metricas_periodo is None:
        return False
    return all(getattr(c.metricas_periodo, campo) >= minimo for minimo, campo in ligadas)


def _passa_funding(c: Candidato, f: Filtros) -> bool:
    # So as pontas futuras pagam/recebem funding; a ponta spot e ignorada.
    futuras = [p for p in (c.ponta_long, c.ponta_short) if p.mercado == "futuro"]
    if f.funding_min_pct is not None or f.funding_max_pct is not None:
        for p in futuras:
            if p.funding_pct is None:
                return False
            if f.funding_min_pct is not None and p.funding_pct < f.funding_min_pct:
                return False
            if f.funding_max_pct is not None and p.funding_pct > f.funding_max_pct:
                return False
    if f.fr_yield_min_pct is not None and c.fr_yield_8h_pct < f.fr_yield_min_pct:
        return False
    if f.intervalos_funding and not all(p.funding_intervalo_h in f.intervalos_funding for p in futuras):
        return False  # intervalo desconhecido (None) nao esta na lista: reprova
    if f.funding_sincronizado:
        intervalos = {p.funding_intervalo_h for p in futuras}
        if None in intervalos or len(intervalos) > 1:
            return False
    return True


def passa(
    c: Candidato,
    f: Filtros,
    modo: Literal["lucro", "farm"],
    cfg: Config,
    comparacao: bool = False,
) -> bool:
    """True se o candidato pode virar sinal neste modo, com estes filtros.

    Ordem das regras:
    1. Valem sempre, ate para moedas em "sempre mostrar": lote minimo nas duas pontas
       (o maior entre o filtro e o tamanho de referencia da Config) e suspeito oculto
       (a menos que `mostrar_suspeitos`).
    2. Moeda em `sempre_mostrar` passa direto, ignorando todos os filtros e a regra do modo
       (inclusive lista negra e "apenas mostrar").
    3. Lista negra, "nunca mostrar" e "apenas mostrar".
    4. Regra do modo (Lucro ou Farm).
    5. Os demais filtros ligados.

    `comparacao=True` usa o RP bruto no lugar do liquido (modo comparacao com o Arbimax).
    """
    if modo not in ("lucro", "farm"):
        raise ValueError(f"modo precisa ser 'lucro' ou 'farm', veio {modo!r}")

    lote_minimo = max(f.lote_min_usd or 0, cfg.tamanho_referencia_usd)
    if c.ponta_long.lote_usd < lote_minimo or c.ponta_short.lote_usd < lote_minimo:
        return False
    if c.suspeito and not f.mostrar_suspeitos:
        return False

    if _moeda_na_lista(c.ativo, f.sempre_mostrar):
        return True

    if _moeda_na_lista(c.ativo, f.lista_negra) or _moeda_na_lista(c.ativo, f.nunca_mostrar):
        return False
    if f.apenas_mostrar and not _moeda_na_lista(c.ativo, f.apenas_mostrar):
        return False

    return (
        _passa_no_modo(c, f, modo, cfg, comparacao)
        and _passa_corretoras_e_tipos(c, f)
        and _passa_liquidez(c, f)
        and _passa_metricas(c, f)
        and _passa_funding(c, f)
    )
