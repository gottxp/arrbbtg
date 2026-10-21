"""Testes das formulas de spread, custos, funding e RP (funcoes puras, tudo em %).

Nenhum teste acessa a internet.
"""

import pytest

from scanner.calculo import (
    custo_taxas,
    fr_yield_8h,
    funding_esperado,
    preco_execucao,
    rp_bruto,
    rp_liquido,
    spread_abertura,
    spread_fechamento,
)


def test_preco_execucao_comendo_livro():
    assert preco_execucao([(100.0, 0.5), (101.0, 1.0)], 100) == pytest.approx(100.4975, abs=1e-4)

def test_livro_raso_descarta():  # Foco de revisão 2
    assert preco_execucao([(100.0, 0.5)], 100) is None

def test_spreads_do_arbimax():
    assert round(spread_abertura(89.14, 100.32), 2) == 12.54
    assert round(spread_fechamento(2.678, 3.0949), 2) == -15.57

def test_custos():
    assert custo_taxas(0.06, 0.06) == pytest.approx(0.24)

def test_funding_mesmo_intervalo():
    assert funding_esperado(0.01, 8, 0.0, 8, horas=8) == pytest.approx(0.01)

def test_funding_intervalos_diferentes():  # Foco de revisão 4
    assert funding_esperado(0.01, 1, 0.01, 8, horas=8) == pytest.approx(0.07)

def test_funding_ponta_spot():
    assert funding_esperado(0.01, 8, None, None, horas=8) == pytest.approx(0.01)

def test_rp_lucrativo():
    b = rp_bruto(12.54, -11.91)
    assert b == pytest.approx(0.63)
    assert rp_liquido(b, 0.24, 0.0, 0.01) == pytest.approx(0.40)

def test_rp_parece_bom_mas_nao_e():
    assert rp_liquido(0.15, 0.24, 0.0, 0.0) == pytest.approx(-0.09)


# --- Casos de borda das decisoes da tarefa ---

def test_preco_execucao_ultimo_nivel_pega_so_a_fracao():
    # 100 USD no 1o nivel (1 unidade), 50 USD no 2o nivel (0,25 unidade): 1,25 unidade por 150 USD.
    assert preco_execucao([(100.0, 1.0), (200.0, 1.0)], 150) == pytest.approx(120.0)

def test_preco_execucao_cabe_no_primeiro_nivel():
    assert preco_execucao([(100.0, 10.0), (200.0, 10.0)], 100) == pytest.approx(100.0)

def test_preco_execucao_exatamente_o_livro_inteiro():
    assert preco_execucao([(100.0, 1.0)], 100) == pytest.approx(100.0)

def test_preco_execucao_venda_com_bids_descendentes():
    # Venda come os bids na ordem dada: 102 USD no 1o bid (1 unidade) e 48 USD no 2o (48/101 unidade).
    assert preco_execucao([(102.0, 1.0), (101.0, 5.0)], 150) == pytest.approx(150 / (1 + 48 / 101))

def test_preco_execucao_tamanho_nao_positivo_e_none():
    assert preco_execucao([(100.0, 10.0)], 0) is None
    assert preco_execucao([(100.0, 10.0)], -5) is None

def test_preco_execucao_livro_vazio_e_none():
    assert preco_execucao([], 100) is None

def test_preco_execucao_nivel_usado_com_quantidade_zero_e_none():
    assert preco_execucao([(100.0, 0.0), (101.0, 10.0)], 100) is None

def test_preco_execucao_nivel_usado_com_preco_nao_positivo_e_none():
    assert preco_execucao([(0.0, 10.0)], 100) is None
    assert preco_execucao([(-1.0, 10.0)], 100) is None

def test_preco_execucao_nivel_nao_usado_nao_e_validado():
    # O 1o nivel ja cobre os 100 USD: o 2o nivel invalido nao e consumido, entao nao vira None.
    assert preco_execucao([(100.0, 10.0), (0.0, -1.0)], 100) == pytest.approx(100.0)

def test_spread_abertura_denominador_nao_positivo_levanta():
    with pytest.raises(ValueError):
        spread_abertura(0.0, 100.0)
    with pytest.raises(ValueError):
        spread_abertura(-1.0, 100.0)

def test_spread_fechamento_denominador_nao_positivo_levanta():
    with pytest.raises(ValueError):
        spread_fechamento(0.0, 100.0)
    with pytest.raises(ValueError):
        spread_fechamento(-1.0, 100.0)

def test_funding_intervalo_nao_positivo_levanta():
    with pytest.raises(ValueError):
        funding_esperado(0.01, 0, 0.0, 8, horas=8)
    with pytest.raises(ValueError):
        funding_esperado(0.01, 8, 0.0, -1, horas=8)

def test_funding_intervalo_none_conta_zero():
    # Sem intervalo a ponta curta nao contribui; a ponta longa continua contando.
    assert funding_esperado(0.01, None, 0.005, 8, horas=8) == pytest.approx(-0.005)

def test_funding_floor_do_intervalo():
    # 7,5 h com intervalo de 8 h: zero periodos completos; com intervalo de 1 h: 7 periodos.
    assert funding_esperado(0.01, 8, None, None, horas=7.5) == pytest.approx(0.0)
    assert funding_esperado(0.01, 1, None, None, horas=7.5) == pytest.approx(0.07)

def test_fr_yield_8h_mesmo_intervalo():
    assert fr_yield_8h(0.01, 8, 0.0, 8) == pytest.approx(0.01)

def test_fr_yield_8h_intervalos_diferentes():
    # 0,01 x 8 / 1 = 0,08 da ponta curta; 0,01 x 8 / 8 = 0,01 da ponta longa.
    assert fr_yield_8h(0.01, 1, 0.01, 8) == pytest.approx(0.07)

def test_fr_yield_8h_ponta_spot_conta_zero():
    assert fr_yield_8h(0.01, 8, None, None) == pytest.approx(0.01)
    assert fr_yield_8h(None, None, 0.01, 4) == pytest.approx(-0.02)

def test_fr_yield_8h_intervalo_nao_positivo_levanta():
    with pytest.raises(ValueError):
        fr_yield_8h(0.01, 0, 0.0, 8)
