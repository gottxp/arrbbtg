"""Testes das metricas do periodo (Task 6; versao incremental na Task 13): janela, medias, maiores e invertidas.

Nenhum teste acessa a internet.
"""

import dataclasses
import heapq
import math
import random
import tracemalloc

import pytest

from scanner.metricas import JanelaMetricas, Metricas


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


# ---------------------------------------------------------------------------
# Versao incremental (Task 13, correcao pos-revisao): o resultado tem de ser o mesmo de
# recalcular do zero. O calculo "ingenuo" abaixo e a regra original, escrita de novo aqui:
# olha todas as amostras da janela a cada resumo.
# ---------------------------------------------------------------------------

def invertidas_ingenuas(dentro, duracao_min_s) -> int:
    """Conta as sequencias seguidas de F >= 0 que duram pelo menos duracao_min_s (a aberta no fim tambem)."""
    invertidas, inicio, fim = 0, None, None
    for ts, _, valor_f, *_ in dentro:
        if valor_f >= 0:
            inicio = ts if inicio is None else inicio
            fim = ts
        else:
            if inicio is not None and fim - inicio >= duracao_min_s:
                invertidas += 1
            inicio = None
    if inicio is not None and fim - inicio >= duracao_min_s:
        invertidas += 1
    return invertidas


def resumo_ingenuo(amostras, agora, periodo_s, duracao_min_s):
    """Metricas das amostras (ts, A, F, lote_long, lote_short) com ts >= agora - periodo_s, contando do zero."""
    dentro = [x for x in amostras if x[0] >= agora - periodo_s]
    if not dentro:
        return None
    a = [x[1] for x in dentro]
    f = [x[2] for x in dentro]
    maiores_a, maiores_f = heapq.nlargest(10, a), heapq.nlargest(10, f)
    return Metricas(
        abertura_media=sum(a) / len(a),
        fechamento_medio=sum(f) / len(f),
        maior_abertura=max(a),
        maior_fechamento=max(f),
        media_10_maiores_abertura=sum(maiores_a) / len(maiores_a),
        media_10_maiores_fechamento=sum(maiores_f) / len(maiores_f),
        invertidas=invertidas_ingenuas(dentro, duracao_min_s),
    )


def lotes_ingenuos(amostras, agora, periodo_s):
    dentro = [x for x in amostras if x[0] >= agora - periodo_s]
    if not dentro:
        return None, None
    return sum(x[3] for x in dentro) / len(dentro), sum(x[4] for x in dentro) / len(dentro)


def conferir(janela, amostras, agora, periodo_s, duracoes):
    """A janela incremental bate com o calculo ingenuo: exato em tudo, menos as medias (tolerancia de float)."""
    esperado = resumo_ingenuo(amostras, agora, periodo_s, duracoes[0])
    novo = janela.resumo(agora=agora, duracao_min_invertida_s=duracoes[0])
    medio_long, medio_short = janela.lotes_medios(agora)
    if esperado is None:
        assert novo is None and (medio_long, medio_short) == (None, None)
        assert all(janela.resumo(agora=agora, duracao_min_invertida_s=d) is None for d in duracoes)
        return
    medias_novas = (novo.abertura_media, novo.fechamento_medio, medio_long, medio_short)
    medias_esperadas = (esperado.abertura_media, esperado.fechamento_medio, *lotes_ingenuos(amostras, agora, periodo_s))
    assert medias_novas == pytest.approx(medias_esperadas, rel=1e-12, abs=1e-12)
    # O resto e exato: os maiores, a media dos 10 maiores (mesma soma, na mesma ordem) e as invertidas.
    assert (novo.maior_abertura, novo.maior_fechamento, novo.media_10_maiores_abertura,
            novo.media_10_maiores_fechamento) == (esperado.maior_abertura, esperado.maior_fechamento,
                                                  esperado.media_10_maiores_abertura,
                                                  esperado.media_10_maiores_fechamento)
    dentro = [x for x in amostras if x[0] >= agora - periodo_s]
    for duracao in duracoes:
        outro = janela.resumo(agora=agora, duracao_min_invertida_s=duracao)
        assert outro.invertidas == invertidas_ingenuas(dentro, duracao)
        assert dataclasses.replace(outro, invertidas=0) == dataclasses.replace(novo, invertidas=0)


def serie(padrao: str, n: int, semente: int):
    """n amostras (ts, A, F, lote_long, lote_short) em ordem de horario, no `padrao` pedido."""
    rnd = random.Random(semente)
    ts, a, f = 1000.0, 0.5, 0.0
    for i in range(n):
        passos = [5.0, 5.0, 40.0, 400.0] if padrao == "lacunas" else [5.0, 5.0, 5.0, 4.5, 5.5, 10.0]
        ts += rnd.choice(passos)
        if padrao == "descendo":  # o maior sai sempre primeiro: obriga a refazer a lista dos maiores
            a, f = 100.0 - i * 0.01, 1.0 - i * 0.001
        elif padrao == "subindo":
            a, f = i * 0.01, -1.0 + i * 0.001
        elif padrao == "repetidos":  # muitos valores iguais e F == 0 exato
            a, f = float(rnd.randint(-3, 3)), rnd.choice([-0.5, 0.0, 0.0, 0.5])
        else:  # passeio aleatorio em volta de 0 (F cruza o zero o tempo todo)
            a += rnd.gauss(0, 0.05)
            f = rnd.gauss(0, 0.2) if rnd.random() < 0.3 else f + rnd.gauss(0, 0.05)
        yield ts, a, f, rnd.uniform(100, 5000), rnd.uniform(100, 5000)


@pytest.mark.parametrize("padrao", ["passeio", "descendo", "subindo", "repetidos", "lacunas"])
def test_incremental_igual_ao_calculo_ingenuo(padrao):
    periodo_s = 300  # ~60 amostras: muitas saem da janela durante o teste
    duracoes = [0, 4.9, 5, 10, 60, 1000]
    janela, vistas = JanelaMetricas(periodo_s=periodo_s), []
    for i, amostra in enumerate(serie(padrao, 3000, semente=11)):
        janela.adicionar(*amostra)
        vistas.append(amostra)
        agora = amostra[0] + (i % 3)  # as vezes o resumo e pedido um pouco depois da amostra
        conferir(janela, vistas, agora, periodo_s, duracoes)
        vistas = [x for x in vistas if x[0] >= agora - periodo_s]  # o relogio so anda: as velhas nunca voltam


def test_incremental_igual_ao_calculo_ingenuo_com_4h_cheias():
    periodo_s, janela, vistas = 4 * 3600, JanelaMetricas(periodo_s=4 * 3600), []
    for i, amostra in enumerate(serie("passeio", 3500, semente=5)):
        janela.adicionar(*amostra)
        vistas.append(amostra)
        if i % 50 == 0 or i > 3450:
            conferir(janela, vistas, amostra[0], periodo_s, [0, 30])


def test_janela_que_esvazia_e_volta_a_encher():
    periodo_s, janela, vistas = 60, JanelaMetricas(periodo_s=60), []
    for amostra in serie("repetidos", 200, semente=3):
        janela.adicionar(*amostra)
        vistas.append(amostra)
    agora = vistas[-1][0] + 1000  # tudo sai da janela
    conferir(janela, vistas, agora, periodo_s, [0, 5])
    assert janela.resumo(agora=agora, duracao_min_invertida_s=0) is None and janela.vazia(agora)
    for amostra in serie("passeio", 200, semente=4):  # amostras novas depois de agora
        amostra = (amostra[0] + agora, *amostra[1:])
        janela.adicionar(*amostra)
        vistas.append(amostra)
        conferir(janela, vistas, amostra[0], periodo_s, [0, 5])
        vistas = [x for x in vistas if x[0] >= amostra[0] - periodo_s]


def test_lotes_medios_do_periodo():
    j = JanelaMetricas(periodo_s=10)
    assert j.lotes_medios(agora=0) == (None, None)
    j.adicionar(0, 1.0, -1.0, lote_long=100.0, lote_short=300.0)
    j.adicionar(5, 1.0, -1.0, lote_long=200.0, lote_short=500.0)
    assert j.lotes_medios(agora=5) == (pytest.approx(150.0), pytest.approx(400.0))
    assert j.lotes_medios(agora=12) == (pytest.approx(200.0), pytest.approx(500.0))  # a de ts 0 saiu
    assert j.lotes_medios(agora=100) == (None, None)


def test_somas_sem_erro_acumulado_depois_de_muitas_saidas():
    # Valores que se perderiam numa soma comum de float (1e16 + 1 - 1e16 = 0): a soma exata nao perde nada.
    j = JanelaMetricas(periodo_s=10)
    for ts in range(0, 100_000, 5):
        j.adicionar(ts, 1e16 if ts % 10 else 1.0, -1.0)
    j.adicionar(100_000, 1.0, -1.0)
    # Ficaram ts 99.990 (1.0), 99.995 (1e16) e 100.000 (1.0).
    assert j.resumo(agora=100_000, duracao_min_invertida_s=0).abertura_media == (1e16 + 2.0) / 3


@pytest.mark.parametrize("valores", [(math.nan, 0.0), (0.0, math.inf), (-math.inf, 1.0)])
def test_numero_invalido_e_recusado(valores):
    j = JanelaMetricas(periodo_s=10)
    with pytest.raises(ValueError):
        j.adicionar(0, *valores)
    assert j.resumo(agora=0, duracao_min_invertida_s=0) is None  # nada entrou


def test_janela_cheia_de_4h_ocupa_pouca_memoria():
    # 2.880 amostras de 5 s (4 h), com os lotes. Os numeros sao criados dentro da medida, como no motor.
    rnd = random.Random(1)
    tracemalloc.start()
    try:
        j = JanelaMetricas(periodo_s=4 * 3600)
        for i in range(2880):
            j.adicionar(i * 5.0, rnd.gauss(0, 1), rnd.gauss(0, 1), rnd.uniform(100, 5000), rnd.uniform(100, 5000))
        j.resumo(agora=2879 * 5.0, duracao_min_invertida_s=0)
        memoria, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert memoria <= 150 * 1024


# ---------------------------------------------------------------------------
# Numeros absurdos (adiados da Task 13 para a 14)
# ---------------------------------------------------------------------------


def test_soma_que_passa_do_maior_float_vira_infinito_sem_erro():
    from scanner.metricas.janela import _SomaExata

    s = _SomaExata()
    s.somar(1.7e308)
    s.somar(1.7e308)
    assert s.valor() == math.inf
    s.tirar(1.7e308)
    assert s.valor() == 1.7e308  # a soma exata continua certa depois
    negativa = _SomaExata()
    negativa.somar(-1.7e308)
    negativa.somar(-1.7e308)
    assert negativa.valor() == -math.inf


def test_janela_com_soma_estourada_devolve_infinito():
    j = JanelaMetricas(periodo_s=100)
    j.adicionar(0, 1.7e308, -1.0)
    j.adicionar(1, 1.7e308, -1.0)
    assert j.resumo(agora=1, duracao_min_invertida_s=0).abertura_media == math.inf


@pytest.mark.parametrize("amostra", [
    ("0", 1.0, -1.0), (0, "1.5", -1.0), (0, 1.0, "x"), (0, True, -1.0), (0, 1.0, -1.0, False, 1.0),
    (0, 1.0, -1.0, 1.0, None), (0, 10 ** 400, -1.0),
])
def test_numero_que_nao_e_real_e_recusado(amostra):
    j = JanelaMetricas(periodo_s=10)
    with pytest.raises(ValueError):
        j.adicionar(*amostra)
    assert j.vazia(agora=0)


def test_inteiros_continuam_aceitos():
    j = JanelaMetricas(periodo_s=10)
    j.adicionar(0, 1, -1, 100, 200)
    assert j.lotes_medios(agora=0) == (100.0, 200.0)


# ---------------------------------------------------------------------------
# Task 18: amostras pelos tickers na mesma janela (especificacao, secao 3, "De onde vem as amostras das metricas")
# - a amostra pelo ticker (melhor bid/ask) nao tem lote: o lote medio vem so das amostras do livro;
# - as medias valem sobre todas as amostras, cada uma pesando o tempo que ela cobre (a do ticker, feita a cada
#   amostra_ticker_s, vale `peso_ticker` amostras do livro, feitas a cada amostra_s);
# - maiores, media dos 10 maiores e invertidas valem sobre todas as amostras, em ordem de horario.
# ---------------------------------------------------------------------------

def test_amostra_pelo_ticker_entra_nas_medias_com_o_peso_do_tempo_que_cobre():
    # Ticker a cada 60 s e livro a cada 5 s: cada amostra pelo ticker vale 12 do livro.
    j = JanelaMetricas(periodo_s=4 * 3600, peso_ticker=12)
    j.adicionar_pelo_ticker(0, a=0.1, f=-0.2)
    j.adicionar_pelo_ticker(60, a=0.1, f=-0.2)
    j.adicionar(65, a=1.0, f=-1.0, lote_long=500.0, lote_short=700.0)
    j.adicionar(70, a=1.0, f=-1.0, lote_long=500.0, lote_short=700.0)
    m = j.resumo(agora=70, duracao_min_invertida_s=0)
    assert m.fechamento_medio == pytest.approx((24 * -0.2 + 2 * -1.0) / 26)
    assert m.abertura_media == pytest.approx((24 * 0.1 + 2 * 1.0) / 26)
    assert j.lotes_medios(agora=70) == (pytest.approx(500.0), pytest.approx(700.0))  # so as do livro


def test_janela_so_com_amostras_pelo_ticker_tem_metricas_e_nao_tem_lote_medio():
    j = JanelaMetricas(periodo_s=1000, peso_ticker=12)
    for ts, f in [(0, -0.1), (60, -0.3), (120, 0.2)]:
        j.adicionar_pelo_ticker(ts, a=0.5, f=f)
    m = j.resumo(agora=120, duracao_min_invertida_s=0)
    assert m.fechamento_medio == pytest.approx(-0.2 / 3)
    assert (m.maior_fechamento, m.invertidas) == (0.2, 1)
    assert j.lotes_medios(agora=120) == (None, None)
    assert not j.vazia(agora=120)


def test_maiores_e_invertidas_valem_sobre_as_duas_fontes_em_ordem_de_horario():
    j = JanelaMetricas(periodo_s=1000, peso_ticker=12)
    j.adicionar_pelo_ticker(0, a=3.0, f=0.1)   # F >= 0 pelo ticker...
    j.adicionar(5, a=1.0, f=0.2)               # ...e continua pelo livro: uma invertida so, de 0 a 10 s
    j.adicionar(10, a=2.0, f=0.0)
    j.adicionar(15, a=1.5, f=-1.0)
    j.adicionar_pelo_ticker(75, a=0.5, f=0.3)  # outra invertida, aberta no fim
    m = j.resumo(agora=75, duracao_min_invertida_s=0)
    assert (m.maior_abertura, m.maior_fechamento) == (3.0, 0.3)
    assert m.media_10_maiores_abertura == pytest.approx((3.0 + 2.0 + 1.5 + 1.0 + 0.5) / 5)
    assert m.invertidas == 2
    assert j.resumo(agora=75, duracao_min_invertida_s=10).invertidas == 1


def test_as_duas_fontes_saem_da_janela_em_ordem_de_horario():
    j = JanelaMetricas(periodo_s=100, peso_ticker=12)
    j.adicionar_pelo_ticker(0, a=9.0, f=0.5)
    j.adicionar(50, a=1.0, f=-1.0, lote_long=100.0, lote_short=100.0)
    j.adicionar_pelo_ticker(60, a=2.0, f=0.4)
    j.adicionar(65, a=3.0, f=0.6, lote_long=300.0, lote_short=300.0)
    assert j.amostras() == [(0.0, 9.0, 0.5, None, None), (50.0, 1.0, -1.0, 100.0, 100.0),
                            (60.0, 2.0, 0.4, None, None), (65.0, 3.0, 0.6, 300.0, 300.0)]
    m = j.resumo(agora=155, duracao_min_invertida_s=0)  # sairam as de 0 e 50
    assert j.amostras() == [(60.0, 2.0, 0.4, None, None), (65.0, 3.0, 0.6, 300.0, 300.0)]
    assert (m.maior_abertura, m.invertidas) == (3.0, 1)  # a invertida de 60 a 65 continua inteira
    assert m.abertura_media == pytest.approx((12 * 2.0 + 3.0) / 13)
    assert j.lotes_medios(agora=155) == (pytest.approx(300.0), pytest.approx(300.0))
    assert j.lotes_medios(agora=161) == (pytest.approx(300.0), pytest.approx(300.0))  # so a do ticker saiu
    assert j.resumo(agora=161, duracao_min_invertida_s=0).abertura_media == pytest.approx(3.0)
    assert j.vazia(agora=166) and j.lotes_medios(agora=166) == (None, None)


def test_amostra_fora_de_ordem_entre_as_duas_fontes_e_recusada():
    j = JanelaMetricas(periodo_s=100, peso_ticker=12)
    j.adicionar(10, a=1.0, f=-1.0)
    j.adicionar(10, a=1.0, f=-1.0)  # duas do livro no mesmo horario: aceitas, como antes
    with pytest.raises(ValueError):
        j.adicionar_pelo_ticker(10, a=1.0, f=-1.0)  # nunca no mesmo horario (nem antes) da mais nova
    j.adicionar_pelo_ticker(11, a=1.0, f=-1.0)
    with pytest.raises(ValueError):
        j.adicionar(11, a=1.0, f=-1.0)  # o livro tambem nao volta para o horario do ticker
    with pytest.raises(ValueError):
        j.adicionar_pelo_ticker(11, a=1.0, f=-1.0)
    assert len(j.amostras()) == 3  # nada das recusadas entrou


@pytest.mark.parametrize("valores", [(math.nan, 0.0), (0.0, math.inf), ("1", 0.0), (True, 0.0), (0.0, None)])
def test_amostra_pelo_ticker_com_numero_invalido_e_recusada(valores):
    j = JanelaMetricas(periodo_s=10, peso_ticker=12)
    with pytest.raises(ValueError):
        j.adicionar_pelo_ticker(0, *valores)
    assert j.vazia(agora=0)


@pytest.mark.parametrize("peso", [0, -1, math.nan, math.inf, "12", True])
def test_peso_do_ticker_invalido_e_recusado(peso):
    with pytest.raises(ValueError):
        JanelaMetricas(periodo_s=10, peso_ticker=peso)


def resumo_ingenuo_duas_fontes(amostras, agora, periodo_s, duracao_min_s, peso):
    """Como resumo_ingenuo, com amostras (ts, A, F, lote_long, lote_short) das duas fontes (lotes None = ticker).

    Medias ponderadas (ticker = `peso`, livro = 1); maiores, 10 maiores e invertidas sobre todas.
    """
    dentro = [x for x in amostras if x[0] >= agora - periodo_s]
    if not dentro:
        return None
    pesos = [peso if x[3] is None else 1.0 for x in dentro]
    a, f = [x[1] for x in dentro], [x[2] for x in dentro]
    maiores_a, maiores_f = heapq.nlargest(10, a), heapq.nlargest(10, f)
    return Metricas(
        abertura_media=sum(p * v for p, v in zip(pesos, a)) / sum(pesos),
        fechamento_medio=sum(p * v for p, v in zip(pesos, f)) / sum(pesos),
        maior_abertura=max(a),
        maior_fechamento=max(f),
        media_10_maiores_abertura=sum(maiores_a) / len(maiores_a),
        media_10_maiores_fechamento=sum(maiores_f) / len(maiores_f),
        invertidas=invertidas_ingenuas(dentro, duracao_min_s),
    )


def serie_duas_fontes(n: int, semente: int):
    """n amostras em ordem de horario: trechos sem livro (pelo ticker, a cada ~60 s) e com livro (a cada ~5 s)."""
    rnd = random.Random(semente)
    ts, a, f, com_livro = 1000.0, 0.5, -0.2, False
    for _ in range(n):
        if rnd.random() < 0.05:
            com_livro = not com_livro  # o livro abre ou fecha
        ts += rnd.choice([5.0, 5.0, 4.5, 5.5]) if com_livro else rnd.choice([60.0, 60.0, 59.5, 61.0, 120.0])
        a += rnd.gauss(0, 0.05)
        f = rnd.gauss(0, 0.2) if rnd.random() < 0.3 else f + rnd.gauss(0, 0.05)
        if com_livro:
            yield ts, a, f, rnd.uniform(100, 5000), rnd.uniform(100, 5000)
        else:
            yield ts, a, f, None, None


def conferir_duas_fontes(janela, amostras, agora, periodo_s, duracoes, peso):
    esperado = resumo_ingenuo_duas_fontes(amostras, agora, periodo_s, duracoes[0], peso)
    novo = janela.resumo(agora=agora, duracao_min_invertida_s=duracoes[0])
    do_livro = [x for x in amostras if x[3] is not None]
    if esperado is None:
        assert novo is None and janela.lotes_medios(agora) == (None, None)
        return
    assert (novo.abertura_media, novo.fechamento_medio) == pytest.approx(
        (esperado.abertura_media, esperado.fechamento_medio), rel=1e-12, abs=1e-12)
    lotes = janela.lotes_medios(agora)
    esperados = lotes_ingenuos(do_livro, agora, periodo_s)
    if esperados == (None, None):
        assert lotes == (None, None)
    else:
        assert lotes == pytest.approx(esperados, rel=1e-12)
    assert dataclasses.replace(novo, abertura_media=0, fechamento_medio=0) == dataclasses.replace(
        esperado, abertura_media=0, fechamento_medio=0)
    dentro = [x for x in amostras if x[0] >= agora - periodo_s]
    for duracao in duracoes[1:]:
        assert janela.resumo(agora=agora, duracao_min_invertida_s=duracao).invertidas == invertidas_ingenuas(
            dentro, duracao)
    assert janela.amostras() == [tuple(float(v) if v is not None else None for v in x) for x in dentro]


@pytest.mark.parametrize("semente", [1, 2, 3])
def test_incremental_com_as_duas_fontes_igual_ao_calculo_ingenuo(semente):
    periodo_s, peso = 1800, 12.0  # ~30 amostras pelo ticker ou ~360 do livro: muitas saem durante o teste
    janela, vistas = JanelaMetricas(periodo_s=periodo_s, peso_ticker=peso), []
    for i, amostra in enumerate(serie_duas_fontes(2500, semente)):
        ts, a, f, lote_long, lote_short = amostra
        if lote_long is None:
            janela.adicionar_pelo_ticker(ts, a, f)
        else:
            janela.adicionar(ts, a, f, lote_long, lote_short)
        vistas.append(amostra)
        agora = ts + (i % 3) * 20  # as vezes o resumo e pedido depois da amostra (saem mais)
        conferir_duas_fontes(janela, vistas, agora, periodo_s, [0, 5, 60, 300], peso)
        vistas = [x for x in vistas if x[0] >= agora - periodo_s]


def test_janela_de_4h_so_pelos_tickers_ocupa_pouca_memoria():
    # 4 h de amostras pelo ticker a cada 120 s (o padrao de amostra_ticker_s): 120 amostras, sem lote, com A e F
    # como no mercado (F quase sempre negativo). Com dezenas de milhares de pares acompanhados pelos tickers, cada
    # janela precisa ser pequena (especificacao, secao 3: ~100 a 400 MB a mais). Medido: ~5,6 KB (a do livro, com
    # 2.880 amostras de 5 s, ~120 KB).
    rnd = random.Random(1)
    tracemalloc.start()
    try:
        j = JanelaMetricas(periodo_s=4 * 3600, peso_ticker=24)
        for i in range(120 * 3):  # 12 h: as primeiras 8 h ja sairam da janela
            j.adicionar_pelo_ticker(i * 120.0, rnd.gauss(0.1, 0.3), -0.2 + rnd.gauss(0, 0.05))
        memoria, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert memoria <= 6.5 * 1024
