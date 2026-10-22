"""Normalizacao de pares: nome do ativo, multiplicador, cotacao e preco em USDT.

Corretoras chamam o mesmo ativo de nomes diferentes. "1000PEPE" (MEXC) e
"kPEPE" (Hyperliquid) sao o PEPE, mas cada unidade vale 1000 vezes menos que
o preco que a corretora mostra. Este modulo tira esses prefixos, guarda a
moeda de cotacao e converte o preco para USDT por 1 unidade do ativo.

Funcoes puras: nao acessam a rede nem o disco.
"""

from dataclasses import dataclass, replace
from typing import Literal

from scanner.modelos import Livro

# Prefixo -> multiplicador. Os mais longos vem primeiro, por clareza na leitura.
MULTIPLICADORES = (
    ("1000000", 1_000_000),
    ("10000", 10_000),
    ("1000", 1_000),
    ("1M", 1_000_000),
    ("k", 1_000),  # so vale minusculo: KAS e KAVA sao ativos reais, nao prefixos
)


# Moedas de cotacao em ordem de preferencia, quando um ativo tem mais de um par na mesma
# corretora e mercado (ex.: BTC/USDT:USDT e BTC/USDC:USDC): so uma entra no estado.
# Moeda fora da lista vem depois de todas, em ordem alfabetica.
# USD (Task 16): na Kraken muitos ativos so tem par em USD e EUR; o USD tem cotacao (USDT/USD da Kraken) e o
# EUR nao (viraria 1:1 com aviso_cotacao, uns 15% fora). Sem o USD aqui, o EUR venceria pela ordem alfabetica.
PREFERENCIA_COTACAO = ("USDT", "USDC", "USDG", "USD")


def prioridade_cotacao(cotacao: str) -> tuple[int, str]:
    """Chave de ordenacao da moeda de cotacao: menor = preferida (USDT, depois USDC, USDG, as outras)."""
    if cotacao in PREFERENCIA_COTACAO:
        return PREFERENCIA_COTACAO.index(cotacao), cotacao
    return len(PREFERENCIA_COTACAO), cotacao


def mercado_do_market(market: dict) -> Literal["spot", "futuro"] | None:
    """Diz se um market do ccxt entra no scanner: "spot", "futuro" (perpetuo linear) ou None.

    Usa os flags do ccxt e nao o nome do simbolo, porque opcoes (BTC/USDT:USDT-261226-100000-C)
    e futuros com vencimento tambem tem ":" no nome. Ficam de fora: contratos inversos (pagos em
    moeda cripto; o contrato vale 1 USD, entao a quantidade nao e em unidades do ativo), futuros
    com vencimento, opcoes e qualquer market sem os flags.
    """
    if market.get("spot"):
        return "spot"
    if market.get("swap") and market.get("linear"):
        return "futuro"
    return None


@dataclass(frozen=True)
class ParNormalizado:
    """Um par de uma corretora, com o nome do ativo padronizado.

    ativo: nome base sem prefixo, ex.: "PEPE" (vale para "1000PEPE" e "kPEPE").
    cotacao: moeda em que o preco e cotado, ex.: "USDT", "USDC", "USDG".
    multiplicador: quantas vezes o preco da corretora e maior que o de 1 unidade
        do ativo (1000 para "1000PEPE"). O preco por unidade divide por ele.
    tamanho_contrato: contractSize do ccxt (1 no spot): quantas unidades do
        "ativo da corretora" (ex.: "1000PEPE") valem 1 contrato. Nao muda o
        preco, so a quantidade. As quantidades do livro (em contratos) viram
        unidades do ativo assim (veja converter_livro):
            unidades_do_ativo = quantidade_ccxt x tamanho_contrato x multiplicador
    """

    ativo: str
    cotacao: str
    multiplicador: float
    tamanho_contrato: float


def separar_multiplicador(base: str) -> tuple[str, float]:
    """Tira o prefixo de multiplicador do nome: "1000PEPE" -> ("PEPE", 1000.0).

    O prefixo so conta se a letra logo depois dele for maiuscula. Assim "kaspa"
    continua "kaspa" e "KAS" continua "KAS".
    """
    for prefixo, multiplicador in MULTIPLICADORES:
        if base.startswith(prefixo):
            resto = base[len(prefixo):]
            if resto[:1].isupper():
                return resto, float(multiplicador)
    return base, 1.0


def _base_original(market: dict) -> str:
    """O nome da base como a corretora escreve, quando o ccxt so mudou a caixa dele; senao a base do ccxt.

    O ccxt poe o codigo da moeda em maiuscula: o "kPEPE" da Hyperliquid chega como base "KPEPE", com o
    nome original em `baseName`. Sem o original, o "k" minusculo (multiplicador 1000) se perderia. So vale
    no perpetuo (swap) e quando o `baseName` e a mesma palavra em outra caixa: "UBTC" -> "BTC" (token
    embrulhado) e "xyz:TSLA" -> "XYZ-TSLA" (outro nome) continuam com a base do ccxt. No spot o nome do token
    e livre (ex.: "kSTAKE" pode ser um token de staking, e nao 1000 STAKE): fica a base do ccxt.
    """
    base = market["base"]
    original = market.get("baseName")
    if market.get("swap") and isinstance(original, str) and isinstance(base, str) and len(original) == len(base) \
            and original != base and original.upper() == base:
        return original
    return base


def normalizar(corretora: str, market: dict) -> ParNormalizado:
    """Monta o ParNormalizado a partir de um market do ccxt.

    corretora: nome da corretora (ex.: "mexc"). Hoje as regras sao as mesmas
    para todas; o argumento fica para regras especificas por corretora.
    O prefixo de multiplicador e procurado no nome original da base (veja _base_original), e o
    ativo fica com o nome do ccxt sem o prefixo: "kPEPE"/"KPEPE" -> "PEPE" x1000.
    """
    base = market["base"]
    original = _base_original(market)
    resto, multiplicador = separar_multiplicador(original)
    ativo = base[len(original) - len(resto):]  # mesmo tamanho: corta o prefixo na base do ccxt
    # No spot o ccxt manda contractSize None (ou nem manda): conta como 1.
    tamanho = market.get("contractSize")
    if tamanho is None:
        tamanho = 1
    return ParNormalizado(
        ativo=ativo,
        cotacao=market["quote"],
        multiplicador=multiplicador,
        tamanho_contrato=float(tamanho),
    )


def preco_unitario_usdt(
    preco: float, par: ParNormalizado, cotacoes: dict[str, float]
) -> tuple[float, bool]:
    """Devolve (preco de 1 unidade do ativo em USDT, aviso_cotacao).

    O multiplicador sai do preco: 0,01 em "1000PEPE" vira 0,00001 do PEPE.
    O tamanho do contrato nao entra aqui, porque ele muda a quantidade e nao o
    preco. Se a moeda nao for USDT e nao houver cotacao, usa 1:1 e marca o aviso.
    """
    preco_unitario = preco / par.multiplicador
    if par.cotacao == "USDT":
        return preco_unitario, False
    if par.cotacao in cotacoes:
        return preco_unitario * cotacoes[par.cotacao], False
    return preco_unitario, True


def converter_livro(livro: Livro, par: ParNormalizado) -> Livro:
    """Devolve uma copia do livro com os niveis em "preco de 1 unidade do ativo" e "unidades do ativo".

    Para cada nivel (preco, quantidade) que a corretora entrega:
        preco por unidade   = preco / multiplicador
        quantidade (unid.)  = quantidade x tamanho_contrato x multiplicador
    O valor do nivel (preco x quantidade) fica igual ao da corretora multiplicado
    pelo tamanho do contrato, que e o dinheiro de verdade que o nivel negocia.

    O preco continua na moeda de cotacao do par (USDT, USDC...). Trocar para USDT
    com a cotacao do momento e trabalho do motor (preco_unitario_usdt).
    O livro original nao e alterado.
    """
    fator_quantidade = par.tamanho_contrato * par.multiplicador

    def converter(niveis: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(preco / par.multiplicador, quantidade * fator_quantidade) for preco, quantidade in niveis]

    # Dividir todos os precos pelo mesmo numero positivo mantem a ordem melhor -> pior.
    return replace(livro, bids=converter(livro.bids), asks=converter(livro.asks))
