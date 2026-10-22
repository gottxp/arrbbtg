"""Testes da normalizacao de pares (prefixos, cotacao e preco por unidade).

Funcoes puras: nenhum teste acessa a internet.
"""

import pytest

from scanner.normalizador import (
    converter_livro,
    mercado_do_market,
    normalizar,
    preco_unitario_usdt,
    prioridade_cotacao,
)
from scanner.coletores.ccxt_coletor import ColetorCcxt
from scanner.motor import simbolos_para_tickers
from tests.fabricas import (
    BINGX_1000PEPE,
    BINGX_BTC,
    BINGX_BTC_USDC,
    BINGX_PEPE_SPOT,
    HL_BTC,
    HL_KPEPE,
    HL_SPOT_BTC,
    HL_SPOT_USDT,
    XT_1000PEPE,
    XT_PEPE_SPOT,
    livro,
    market_ccxt,
)


def test_prefixo_1000():
    p = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 1})
    assert p.ativo == "PEPE" and p.multiplicador == 1000

def test_prefixo_k_hyperliquid():
    p = normalizar("hyperliquid", {"base": "kPEPE", "quote": "USDC", "contractSize": 1})
    assert p.ativo == "PEPE" and p.multiplicador == 1000 and p.cotacao == "USDC"

def test_prefixo_1m():
    p = normalizar("mexc", {"base": "1MBABYDOGE", "quote": "USDT", "contractSize": 1})
    assert p.ativo == "BABYDOGE" and p.multiplicador == 1_000_000

def test_k_maiusculo_nao_e_prefixo():
    assert normalizar("gate", {"base": "KAS", "quote": "USDT", "contractSize": 1}).ativo == "KAS"
    assert normalizar("gate", {"base": "KAVA", "quote": "USDT", "contractSize": 1}).multiplicador == 1

def test_mesmo_ativo_mesmo_preco():  # Foco de revisao 1
    a = normalizar("gate", {"base": "PEPE", "quote": "USDT", "contractSize": 1})
    b = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 1})
    pa, _ = preco_unitario_usdt(0.00001, a, {})
    pb, _ = preco_unitario_usdt(0.01, b, {})
    assert pa == pytest.approx(pb)

def test_conversao_usdc():
    p = normalizar("hyperliquid", {"base": "BTC", "quote": "USDC", "contractSize": 1})
    assert preco_unitario_usdt(100000.0, p, {"USDC": 0.9998}) == (pytest.approx(99980.0), False)

def test_sem_cotacao_usa_1_e_avisa():
    p = normalizar("lighter_rh", {"base": "BTC", "quote": "USDG", "contractSize": 1})
    assert preco_unitario_usdt(100000.0, p, {}) == (100000.0, True)


# Casos de borda alem do brief: prefixos longos, contractSize ausente e USDT.

def test_prefixos_10000_e_1000000():
    p = normalizar("mexc", {"base": "10000SHIB", "quote": "USDT", "contractSize": 1})
    assert p.ativo == "SHIB" and p.multiplicador == 10_000
    p = normalizar("mexc", {"base": "1000000BONK", "quote": "USDT", "contractSize": 1})
    assert p.ativo == "BONK" and p.multiplicador == 1_000_000

def test_k_minusculo_seguido_de_minuscula_nao_e_prefixo():
    assert normalizar("hyperliquid", {"base": "kaspa", "quote": "USDC", "contractSize": 1}).ativo == "kaspa"

def test_contract_size_none_ou_ausente_vira_1():
    assert normalizar("gate", {"base": "BTC", "quote": "USDT", "contractSize": None}).tamanho_contrato == 1
    assert normalizar("gate", {"base": "BTC", "quote": "USDT"}).tamanho_contrato == 1

def test_contract_size_guardado_no_par():
    p = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 0.1})
    assert p.tamanho_contrato == 0.1 and p.multiplicador == 1000

def test_contract_size_nao_muda_preco_unitario():
    # Contrato afeta a quantidade (Task 4), nao o preco de 1 unidade.
    p = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 0.1})
    assert preco_unitario_usdt(0.01, p, {}) == (pytest.approx(0.00001), False)

def test_usdt_ignora_cotacoes_informadas():
    p = normalizar("gate", {"base": "BTC", "quote": "USDT", "contractSize": 1})
    assert preco_unitario_usdt(100000.0, p, {"USDT": 0.5}) == (100000.0, False)


# converter_livro: niveis do livro em preco por 1 unidade do ativo e quantidade em unidades do ativo.
# unidades = quantidade_ccxt x tamanho_contrato x multiplicador; preco = preco_ccxt / multiplicador.

def notional(niveis):
    """Soma preco x quantidade dos niveis (o valor negociavel, na moeda de cotacao)."""
    return sum(preco * quantidade for preco, quantidade in niveis)


def test_converter_livro_1000pepe_divide_o_preco_e_multiplica_a_quantidade():
    par = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 1})
    original = livro("mexc", "futuro", "1000PEPE/USDT:USDT", 10.0,
                     bids=[(0.0100, 5000.0), (0.0099, 2000.0)], asks=[(0.0101, 3000.0)])
    convertido = converter_livro(original, par)
    assert convertido.bids == [(pytest.approx(0.00001), pytest.approx(5_000_000.0)),
                               (pytest.approx(0.0000099), pytest.approx(2_000_000.0))]
    assert convertido.asks == [(pytest.approx(0.0000101), pytest.approx(3_000_000.0))]


def test_converter_livro_contrato_diferente_de_1_so_muda_a_quantidade():
    par = normalizar("gate", {"base": "BTC", "quote": "USDT", "contractSize": 0.001})
    original = livro("gate", "futuro", "BTC/USDT:USDT", 10.0, bids=[(67000.0, 10.0)], asks=[(67001.0, 20.0)])
    convertido = converter_livro(original, par)
    assert convertido.bids == [(67000.0, pytest.approx(0.01))]   # 10 contratos x 0,001 BTC
    assert convertido.asks == [(67001.0, pytest.approx(0.02))]


def test_converter_livro_multiplicador_e_contrato_juntos():
    par = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 0.1})
    original = livro("mexc", "futuro", "1000PEPE/USDT:USDT", 10.0, bids=[(0.0100, 500.0)], asks=[(0.0101, 400.0)])
    convertido = converter_livro(original, par)
    assert convertido.bids == [(pytest.approx(0.00001), pytest.approx(500 * 0.1 * 1000))]


@pytest.mark.parametrize("base, tamanho", [("BTC", 1), ("1000PEPE", 1), ("BTC", 0.001), ("1000PEPE", 0.1), ("1MBABYDOGE", 10)])
def test_converter_livro_preserva_o_valor_de_cada_nivel(base, tamanho):
    # preco x quantidade (o dinheiro do nivel) tem que ser o mesmo antes e depois: preco_ccxt x qtd_ccxt x contrato.
    par = normalizar("mexc", {"base": base, "quote": "USDT", "contractSize": tamanho})
    original = livro("mexc", "futuro", "X", 10.0, bids=[(2.0, 7.0), (1.9, 3.0)], asks=[(2.1, 5.0), (2.2, 9.0)])
    convertido = converter_livro(original, par)
    for lado_original, lado_convertido in ((original.bids, convertido.bids), (original.asks, convertido.asks)):
        for (preco, qtd), (preco_u, qtd_u) in zip(lado_original, lado_convertido):
            assert preco_u * qtd_u == pytest.approx(preco * qtd * tamanho)
    assert notional(convertido.bids) == pytest.approx(sum(p * q * tamanho for p, q in original.bids))


def test_converter_livro_mantem_ordem_e_identificacao():
    par = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 1})
    original = livro("mexc", "futuro", "1000PEPE/USDT:USDT", 10.5, ts_corretora=9.5,
                     bids=[(0.0100, 1.0), (0.0099, 1.0)], asks=[(0.0101, 1.0), (0.0102, 1.0)])
    c = converter_livro(original, par)
    assert [p for p, _ in c.bids] == sorted((p for p, _ in c.bids), reverse=True)
    assert [p for p, _ in c.asks] == sorted(p for p, _ in c.asks)
    assert (c.corretora, c.mercado, c.simbolo_original) == ("mexc", "futuro", "1000PEPE/USDT:USDT")
    assert (c.recebido_em, c.ts_corretora) == (10.5, 9.5)


def test_converter_livro_nao_altera_o_original():
    par = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 1})
    original = livro("mexc", "futuro", "X", 10.0, bids=[(0.0100, 5.0)], asks=[(0.0101, 5.0)])
    convertido = converter_livro(original, par)
    assert original.bids == [(0.0100, 5.0)] and original.asks == [(0.0101, 5.0)]
    assert convertido is not original


def test_converter_livro_de_ativo_simples_nao_muda_nada():
    par = normalizar("gate", {"base": "BTC", "quote": "USDT"})  # spot: sem contractSize
    original = livro("gate", "spot", "BTC/USDT", 10.0)
    convertido = converter_livro(original, par)
    assert convertido.bids == original.bids and convertido.asks == original.asks


def test_converter_livro_nao_converte_a_moeda_de_cotacao():
    # USDC -> USDT e trabalho do motor (Task 10): aqui o preco continua na moeda de cotacao.
    par = normalizar("hyperliquid", {"base": "BTC", "quote": "USDC", "contractSize": 1})
    original = livro("hyperliquid", "futuro", "BTC/USDC:USDC", 10.0, bids=[(100000.0, 1.0)], asks=[(100001.0, 1.0)])
    assert converter_livro(original, par).bids == [(100000.0, 1.0)]


# mercado_do_market: so spot e perpetuo linear entram no scanner (flags do ccxt, nao o nome do simbolo).

def test_mercado_do_market_spot_e_perpetuo_linear():
    assert mercado_do_market(market_ccxt("BTC/USDT", "BTC")) == "spot"
    assert mercado_do_market(market_ccxt("BTC/USDT:USDT", "BTC")) == "futuro"
    assert mercado_do_market(market_ccxt("BTC/USDC:USDC", "BTC", quote="USDC")) == "futuro"


@pytest.mark.parametrize("tipo, inverso", [
    ("swap", True),      # perpetuo inverso (BTC/USD:BTC, contrato vale 1 USD)
    ("future", False),   # futuro com vencimento
    ("option", False),   # opcao: o simbolo tambem tem ":"
])
def test_mercado_do_market_rejeita_o_que_nao_e_spot_nem_perpetuo_linear(tipo, inverso):
    assert mercado_do_market(market_ccxt("BTC/USDT:USDT-261226", "BTC", tipo=tipo, inverso=inverso)) is None


def test_mercado_do_market_sem_flags_ou_com_linear_desconhecido_e_rejeitado():
    assert mercado_do_market({}) is None
    assert mercado_do_market({"swap": True}) is None                    # linear desconhecido: na duvida, fora
    assert mercado_do_market({"swap": True, "linear": None}) is None


def test_prioridade_cotacao_usdt_usdc_usdg_depois_as_outras_em_ordem_alfabetica():
    ordenadas = sorted(["USDG", "EUR", "USDC", "USDT", "BRL"], key=prioridade_cotacao)
    assert ordenadas == ["USDT", "USDC", "USDG", "BRL", "EUR"]


# ---------------------------------------------------------------------------
# Task 15: fase 2 de corretoras (BingX, XT, Hyperliquid)
# ---------------------------------------------------------------------------

def test_hyperliquid_kpepe_contra_mexc():
    h = normalizar("hyperliquid", {"base": "kPEPE", "quote": "USDC", "contractSize": 1})
    m = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 1})
    ph, aviso = preco_unitario_usdt(0.010, h, {"USDC": 1.0}); pm, _ = preco_unitario_usdt(0.010, m, {})
    assert ph == pytest.approx(pm) and aviso is False


class _SoMarkets:
    """Corretora falsa so com os markets (a indexacao do coletor nao acessa mais nada)."""

    def __init__(self, markets):
        self.markets = {m["symbol"]: m for m in markets}
        self.has = {"watchTickers": True}


def indexar(corretora, markets, mercados=("spot", "futuro")):
    """{(mercado, ativo): simbolo} que o coletor da corretora escolhe para o estado."""
    return ColetorCcxt(corretora, _SoMarkets(markets), mercados=mercados).simbolos_por_chave()


def test_kpepe_da_hyperliquid_como_o_ccxt_entrega_vira_pepe_vezes_1000():
    # O ccxt entrega "KPEPE" (maiuscula); o nome original "kPEPE" fica no baseName.
    p = normalizar("hyperliquid", HL_KPEPE)
    assert (p.ativo, p.multiplicador, p.cotacao, p.tamanho_contrato) == ("PEPE", 1000, "USDC", 1)


def test_base_name_so_vale_quando_e_a_mesma_base_em_outra_caixa():
    # "UBTC" -> "BTC" e um token embrulhado (outro nome, nao outra caixa): fica a base do ccxt.
    assert normalizar("hyperliquid", HL_SPOT_BTC).ativo == "BTC"
    # Builder (hip3): "xyz:TSLA" -> "XYZ-TSLA"; continua o nome do ccxt.
    hip3 = {**HL_BTC, "base": "XYZ-TSLA", "baseName": "xyz:TSLA"}
    assert normalizar("hyperliquid", hip3).ativo == "XYZ-TSLA"
    # Nome original com minuscula que nao e prefixo: o ativo fica como o ccxt escreve (maiuscula).
    assert normalizar("hyperliquid", {**HL_BTC, "base": "MOODENG", "baseName": "Moodeng"}).ativo == "MOODENG"
    # Sem baseName (as outras corretoras) nada muda.
    assert normalizar("mexc", {"base": "KAS", "quote": "USDT"}).ativo == "KAS"


def test_indexacao_hyperliquid_kpepe_e_so_futuros():
    chaves = indexar("hyperliquid", [HL_KPEPE, HL_BTC, HL_SPOT_BTC, HL_SPOT_USDT], mercados=("futuro",))
    assert chaves == {("futuro", "PEPE"): "KPEPE/USDC:USDC", ("futuro", "BTC"): "BTC/USDC:USDC"}


def test_indexacao_prefixo_1000_da_bingx_e_da_xt():
    for corretora, futuro, spot in (("bingx", BINGX_1000PEPE, BINGX_PEPE_SPOT), ("xt", XT_1000PEPE, XT_PEPE_SPOT)):
        assert indexar(corretora, [futuro, spot]) == {("futuro", "PEPE"): "1000PEPE/USDT:USDT",
                                                      ("spot", "PEPE"): "PEPE/USDT"}, corretora


def test_indexacao_usdt_e_usdc_do_mesmo_ativo_fica_com_usdt_em_qualquer_ordem():
    for ordem in ([BINGX_BTC, BINGX_BTC_USDC], [BINGX_BTC_USDC, BINGX_BTC]):
        assert indexar("bingx", ordem) == {("futuro", "BTC"): "BTC/USDT:USDT"}
    # So USDC (Hyperliquid): o USDC entra.
    assert indexar("hyperliquid", [HL_BTC], mercados=("futuro",)) == {("futuro", "BTC"): "BTC/USDC:USDC"}


def test_kpepe_da_hyperliquid_e_1000pepe_da_mexc_formam_combinacao():
    mexc = {**BINGX_1000PEPE, "id": "1000PEPE_USDT", "taker": 0.0002}
    vencedores = {"hyperliquid": indexar("hyperliquid", [HL_KPEPE], mercados=("futuro",)),
                  "mexc": indexar("mexc", [mexc])}
    assert simbolos_para_tickers(vencedores) == {"hyperliquid": ["KPEPE/USDC:USDC"], "mexc": ["1000PEPE/USDT:USDT"]}
