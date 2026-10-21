"""Testes das metricas do periodo (Task 6): janela, medias, maiores e invertidas.

Nenhum teste acessa a internet.
"""

import pytest

from scanner.metricas import JanelaMetricas


# ---------------------------------------------------------------------------
# Brief (Task 6, Passo 1) - verbatim
# ---------------------------------------------------------------------------

def janela_exemplo():
    j = JanelaMetricas(periodo_s=4 * 3600)
    for ts, f in zip(range(0, 35, 5), [-1, 0.1, 0.2, 0.1, -1, 0.5, -1]):
        j.adicionar(ts, a=1.0, f=f)
    return j

def test_invertidas_com_duracao_minima():
    assert janela_exemplo().resumo(agora=30, duracao_min_invertida_s=10).invertidas == 1
    assert janela_exemplo().resumo(agora=30, duracao_min_invertida_s=0).invertidas == 2

def test_maior_fechamento_e_o_menos_negativo():
    j = JanelaMetricas(periodo_s=100)
    for ts, f in [(0, -12.0), (5, -11.5), (10, -12.5)]:
        j.adicionar(ts, a=12.0, f=f)
    m = j.resumo(agora=10, duracao_min_invertida_s=0)
    assert m.maior_fechamento == -11.5 and m.fechamento_medio == pytest.approx(-12.0)

def test_descarta_fora_do_periodo():
    j = JanelaMetricas(periodo_s=10); j.adicionar(0, 5.0, -1.0); j.adicionar(20, 1.0, -1.0)
    assert j.resumo(agora=20, duracao_min_invertida_s=0).abertura_media == 1.0


# ---------------------------------------------------------------------------
# Casos extras (Task 6): media dos 10 maiores, run aberta no fim, janela vazia
# ---------------------------------------------------------------------------

def test_media_dos_10_maiores_com_mais_de_10_amostras():
    j = JanelaMetricas(periodo_s=100)
    for ts in range(12):  # A = 1..12 e F = -1..-12
        j.adicionar(ts, a=float(ts + 1), f=-float(ts + 1))
    m = j.resumo(agora=11, duracao_min_invertida_s=0)
    assert m.abertura_media == pytest.approx(6.5)
    assert m.media_10_maiores_abertura == pytest.approx(7.5)  # 3..12 -> (3+...+12)/10
    assert m.fechamento_medio == pytest.approx(-6.5)
    assert m.media_10_maiores_fechamento == pytest.approx(-5.5)  # F -1..-10 (os mais altos)

def test_media_dos_10_maiores_com_menos_de_10_amostras_usa_todas():
    j = JanelaMetricas(periodo_s=100)
    for ts, a, f in [(0, 2.0, -1.0), (5, 4.0, -3.0), (10, 6.0, -2.0)]:
        j.adicionar(ts, a=a, f=f)
    m = j.resumo(agora=10, duracao_min_invertida_s=0)
    assert m.media_10_maiores_abertura == pytest.approx(4.0)  # (2+4+6)/3
    assert m.media_10_maiores_fechamento == pytest.approx(-2.0)  # (-1-3-2)/3

def test_run_aberta_no_fim_tambem_conta():
    j = JanelaMetricas(periodo_s=100)
    for ts, f in zip(range(0, 20, 5), [-1, 0.1, 0.2, 0.3]):  # ts 0, 5, 10, 15
        j.adicionar(ts, a=1.0, f=f)
    # a run aberta vai de ts 5 a ts 15: dura 10 s
    assert j.resumo(agora=15, duracao_min_invertida_s=10).invertidas == 1
    assert j.resumo(agora=15, duracao_min_invertida_s=11).invertidas == 0

def test_janela_vazia_devolve_none():
    assert JanelaMetricas(periodo_s=100).resumo(agora=10, duracao_min_invertida_s=0) is None

def test_amostras_todas_fora_do_periodo_devolvem_none():
    j = JanelaMetricas(periodo_s=10)
    j.adicionar(0, a=1.0, f=-1.0)
    assert j.resumo(agora=100, duracao_min_invertida_s=0) is None
