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


# ---------------------------------------------------------------------------
# Cache do resumo (fix da review): o motor chama resumo a cada 500 ms, mas a janela
# so muda a cada 5 s. O resumo fica guardado e so e recontado quando algo muda.
# Em todos os casos o resultado tem de ser igual ao de recalcular do zero.
# ---------------------------------------------------------------------------

AMOSTRAS_CACHE = [(0, 1.0, -1.0), (5, 2.0, 0.5), (8, 3.0, 0.2), (12, 4.0, -0.3), (15, 5.0, 0.1)]

def janela_com(amostras, periodo_s=100):
    j = JanelaMetricas(periodo_s=periodo_s)
    for ts, a, f in amostras:
        j.adicionar(ts, a=a, f=f)
    return j

def test_resumo_repetido_devolve_o_mesmo_objeto_sem_recontar():
    j = janela_com(AMOSTRAS_CACHE)
    m1 = j.resumo(agora=15, duracao_min_invertida_s=0)
    m2 = j.resumo(agora=16, duracao_min_invertida_s=0)  # nada saiu da janela
    assert m2 is m1

def test_adicionar_invalida_o_cache():
    j = janela_com(AMOSTRAS_CACHE)
    antes = j.resumo(agora=15, duracao_min_invertida_s=0)
    j.adicionar(20, a=10.0, f=-2.0)
    depois = j.resumo(agora=20, duracao_min_invertida_s=0)
    assert depois is not antes
    assert depois.maior_abertura == 10.0
    assert depois == janela_com(AMOSTRAS_CACHE + [(20, 10.0, -2.0)]).resumo(agora=20, duracao_min_invertida_s=0)

def test_descarte_dentro_do_resumo_invalida_o_cache():
    # periodo de 10 s: a amostra de ts 5 e a de ts 8 saem so quando resumo(agora=20) roda
    j = janela_com(AMOSTRAS_CACHE, periodo_s=10)
    antes = j.resumo(agora=15, duracao_min_invertida_s=0)
    depois = j.resumo(agora=20, duracao_min_invertida_s=0)
    assert depois is not antes
    assert depois.abertura_media == pytest.approx(4.5)  # so ts 12 e 15 ficaram
    assert depois == janela_com(AMOSTRAS_CACHE[3:], periodo_s=10).resumo(agora=20, duracao_min_invertida_s=0)

def test_duracao_diferente_recalcula_invertidas():
    j = janela_exemplo()
    com_minimo = j.resumo(agora=30, duracao_min_invertida_s=10)
    sem_minimo = j.resumo(agora=30, duracao_min_invertida_s=0)
    assert com_minimo.invertidas == 1 and sem_minimo.invertidas == 2
    assert sem_minimo is not com_minimo
    assert com_minimo == janela_exemplo().resumo(agora=30, duracao_min_invertida_s=10)
    assert sem_minimo == janela_exemplo().resumo(agora=30, duracao_min_invertida_s=0)
    assert j.resumo(agora=30, duracao_min_invertida_s=10) == com_minimo  # voltar ao 10 recalcula igual

def test_cache_bate_com_recalcular_do_zero_em_janela_cheia():
    # 2880 amostras (4 h a cada 5 s), com valores variados e sem repeticao simples
    amostras = [(i * 5.0, ((i * 37) % 101) / 7.0, ((i * 13) % 17 - 8) / 3.0) for i in range(2880)]
    agora = amostras[-1][0]
    j = janela_com(amostras, periodo_s=4 * 3600)
    primeiro = j.resumo(agora=agora, duracao_min_invertida_s=10)
    assert j.resumo(agora=agora, duracao_min_invertida_s=10) is primeiro
    assert primeiro == janela_com(amostras, periodo_s=4 * 3600).resumo(agora=agora, duracao_min_invertida_s=10)
