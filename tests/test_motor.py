"""Testes do motor (Tasks 10 e 13): combinacoes, candidatos spot x futuro e futuro x futuro,
etapa 2, metricas, ciclo e __main__.

Nenhum teste acessa a internet: o estado e preenchido a mao (tests.fabricas) e, nos testes do
__main__, as corretoras do ccxt sao trocadas por objetos falsos.
"""

import asyncio
import logging
import math

import pytest

import scanner.__main__ as principal
from scanner.armazenamento import Banco
from scanner.coletores.ccxt_coletor import ColetorCcxt
from scanner.estado import EstadoMercados
from scanner.filtros import Filtros
from scanner.motor import (
    Motor,
    Sessao,
    chave_de,
    combinacoes,
    pares_futuro_futuro,
    pares_spot_futuro,
    simbolos_para_tickers,
)
from tests.fabricas import DadosLentosFalso, cfg, cfg_com, colocar_ponta, livro, market_ccxt, motor_falso

# Combinacao e chave do caso mais usado: spot da Gate (long) x futuro da Bitget (short).
COMBO = ("BTC", "gate", "spot", "bitget", "futuro")
CHAVE = chave_de(COMBO)


# ---------------------------------------------------------------------------
# Brief (Task 10, Passo 1) - verbatim
# ---------------------------------------------------------------------------

def test_pares_incluem_mesma_corretora():
    m = {"bitget": {("spot", "BTC"), ("futuro", "BTC")}}
    assert ("BTC", "bitget", "spot", "bitget", "futuro") in pares_spot_futuro(m)

def test_spot_sempre_e_o_long():
    for _, _, mercado_long, _, mercado_short in pares_spot_futuro({"gate": {("spot", "BTC")}, "mexc": {("futuro", "BTC")}}):
        assert mercado_long == "spot" and mercado_short == "futuro"

def test_candidato_spot_futuro():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), taxas=0.0)
    c = m.candidatos(agora=10)[0]
    assert c.tipo == "spot_futuro" and round(c.spread_abertura_pct, 2) == 1.0

def test_ponta_velha_nao_gera():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), silencio_futuro_s=3)
    assert m.candidatos(agora=10) == []

def test_suspeito_acima_de_30():
    m = motor_falso(spot=("gate", "X", 1.0), futuro=("bitget", "X", 1.5))
    assert m.candidatos(agora=10)[0].suspeito is True


# ---------------------------------------------------------------------------
# Brief (Task 13, Passo 1) - verbatim
# ---------------------------------------------------------------------------

def test_nunca_mesma_corretora():
    for _, x, _, y, _ in pares_futuro_futuro({"gate": {("futuro", "BTC")}, "mexc": {("futuro", "BTC")}}):
        assert x != y

def test_duas_direcoes():
    p = pares_futuro_futuro({"gate": {("futuro", "BTC")}, "mexc": {("futuro", "BTC")}})
    assert ("BTC", "gate", "futuro", "mexc", "futuro") in p and ("BTC", "mexc", "futuro", "gate", "futuro") in p

def test_akam_ponta_a_ponta():
    m = motor_falso(long_futuro=("bitget", "AKAM", 89.14), short_futuro=("gate", "AKAM", 100.32),
                    f_medio=-11.91, taxas=0.06, funding_short=(0.01, 8), funding_long=(0.0, 8))
    c = m.candidatos(agora=10)[0]
    assert c.tipo == "futuro_futuro"
    assert round(c.rp_bruto_pct, 2) == 0.63 and round(c.rp_liquido_pct, 2) == 0.40
    assert round(c.fr_yield_8h_pct, 2) == 0.01


# ---------------------------------------------------------------------------
# Combinacoes e simbolos da etapa 1
# ---------------------------------------------------------------------------


def test_pares_spot_futuro_todas_as_combinacoes_em_ordem():
    m = {
        "mexc": {("futuro", "BTC"), ("futuro", "ETH")},
        "gate": {("spot", "BTC"), ("futuro", "BTC"), ("spot", "ETH")},
    }
    assert pares_spot_futuro(m) == [
        ("BTC", "gate", "spot", "gate", "futuro"),
        ("BTC", "gate", "spot", "mexc", "futuro"),
        ("ETH", "gate", "spot", "mexc", "futuro"),
    ]


def test_sem_spot_ou_sem_futuro_nao_ha_par():
    assert pares_spot_futuro({"gate": {("spot", "BTC")}, "mexc": {("spot", "BTC")}}) == []
    assert pares_spot_futuro({"gate": {("futuro", "BTC")}, "mexc": {("futuro", "BTC")}}) == []
    assert pares_spot_futuro({}) == []


def test_chave_de_bate_com_a_chave_do_candidato():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    assert m.candidatos(agora=10)[0].chave == CHAVE == ("BTC/USDT", "spot_futuro", "gate", "spot", "bitget", "futuro")


def test_simbolos_para_tickers_so_os_que_formam_par():
    vencedores = {
        "gate": {("spot", "BTC"): "BTC/USDT", ("spot", "DOGE"): "DOGE/USDT"},
        "bitget": {("futuro", "BTC"): "BTC/USDT:USDT", ("futuro", "XRP"): "XRP/USDT:USDT"},
        "mexc": {("spot", "ETH"): "ETH/USDT"},
    }
    assert simbolos_para_tickers(vencedores) == {"gate": ["BTC/USDT"], "bitget": ["BTC/USDT:USDT"]}


def test_simbolos_para_tickers_mesma_corretora_spot_e_futuro():
    vencedores = {"bitget": {("spot", "BTC"): "BTC/USDT", ("futuro", "BTC"): "BTC/USDT:USDT"}}
    assert simbolos_para_tickers(vencedores) == {"bitget": ["BTC/USDT", "BTC/USDT:USDT"]}


# ---------------------------------------------------------------------------
# Candidatos: pontas, liquidez, RP
# ---------------------------------------------------------------------------


def test_candidato_preenche_as_pontas_e_os_campos_do_sinal():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), funding_short=(0.01, 8))
    c = m.candidatos(agora=10)[0]
    assert (c.par, c.modo, c.etiquetas, c.aviso_cotacao, c.suspeito) == ("BTC/USDT", "lucro", [], False, False)
    assert c.tamanho_referencia_usd == 100 and c.custo_por_1000_usd is None

    pl, ps = c.ponta_long, c.ponta_short
    assert (pl.corretora, pl.mercado, pl.simbolo_original) == ("gate", "spot", "BTC/USDT")
    assert (ps.corretora, ps.mercado, ps.simbolo_original) == ("bitget", "futuro", "BTC/USDT:USDT")
    assert pl.preco == pytest.approx(100.0) and ps.preco == pytest.approx(101.0)
    assert pl.lote_usd == pytest.approx(1000.0) and ps.lote_usd == pytest.approx(1000.0)
    assert pl.lote_medio_usd == pytest.approx(1000.0) and ps.lote_medio_usd == pytest.approx(1000.0)
    assert pl.volume_24h_usd == pytest.approx(1_000_000.0) and pl.variacao_24h_pct == 1.0
    assert (pl.funding_pct, pl.funding_intervalo_h) == (None, None)  # spot nao tem funding
    assert (ps.funding_pct, ps.funding_intervalo_h) == (0.01, 8)
    assert pl.limite_ordem_usd is None and ps.limite_ordem_usd is None  # v1: limite desconhecido
    assert pl.taxa_taker_pct == 0.06 and ps.taxa_taker_pct == 0.06


def test_spreads_atuais_usam_os_dois_lados_do_livro():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    c = m.candidatos(agora=10)[0]
    # Fechar: o long vende no bid do spot (100 - 0,1%) e o short recompra no ask do futuro (101 + 0,1%).
    bid_long, ask_short = 100.0 * 0.999, 101.0 * 1.001
    assert c.spread_abertura_pct == pytest.approx(1.0)
    assert c.spread_fechamento_pct == pytest.approx((bid_long - ask_short) / bid_long * 100)


def test_rp_com_f_medio_taxas_e_funding():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0),
                    taxas=0.06, f_medio=-0.5, funding_short=(0.01, 4))
    c = m.candidatos(agora=10)[0]
    assert c.rp_bruto_pct == pytest.approx(0.5)  # A 1,0 + F medio -0,5
    assert c.custo_taxas_pct == pytest.approx(0.24)
    assert c.funding_esperado_pct == pytest.approx(0.02)  # 0,01 x 2 cobrancas (4h) em 8h
    assert c.fr_yield_8h_pct == pytest.approx(0.02)  # 0,01 x 8 / 4
    assert c.rp_liquido_pct == pytest.approx(0.5 - 0.24 + 0.02)


def test_rp_usa_margem_de_slippage_e_tempo_de_operacao_da_config():
    config = cfg_com(margem_slippage_pct=0.05, tempo_operacao_h=16)
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0),
                    f_medio=-0.5, funding_short=(0.01, 8), config=config)
    c = m.candidatos(agora=10)[0]
    assert c.funding_esperado_pct == pytest.approx(0.02)  # 2 cobrancas de 8h em 16h
    assert c.rp_liquido_pct == pytest.approx(0.5 - 0.24 - 0.05 + 0.02)


def test_futuro_sem_funding_conhecido_conta_zero():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5)
    c = m.candidatos(agora=10)[0]
    assert (c.ponta_short.funding_pct, c.ponta_short.funding_intervalo_h) == (None, None)
    assert c.funding_esperado_pct == 0.0 and c.fr_yield_8h_pct == 0.0


def test_suspeito_so_acima_do_limite_da_config():
    m = motor_falso(spot=("gate", "X", 1.0), futuro=("bitget", "X", 1.2))  # A = 20%
    assert m.candidatos(agora=10)[0].suspeito is False
    m = motor_falso(spot=("gate", "X", 1.0), futuro=("bitget", "X", 1.2), config=cfg_com(limite_suspeito_pct=10))
    assert m.candidatos(agora=10)[0].suspeito is True


def test_preco_de_execucao_come_o_livro_e_lote_e_o_melhor_nivel():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    m.estado.atualizar_livro(livro("gate", "spot", "BTC/USDT", 10.0, bids=[(99.9, 10.0)],
                                   asks=[(100.0, 0.5), (102.0, 10.0)]), ativo="BTC")
    c = m.candidatos(agora=10)[0]
    # $50 no primeiro nivel (0,5 unidade) e $50 no segundo (50/102 unidades).
    assert c.ponta_long.preco == pytest.approx(100 / (0.5 + 50 / 102))
    assert c.ponta_long.lote_usd == pytest.approx(50.0)
    assert c.ponta_long.simbolo_original == "BTC/USDT"


@pytest.mark.parametrize("bids, asks", [
    ([(99.9, 10.0)], [(100.0, 0.5)]),  # abrir: so $50 no ask
    ([(99.9, 0.5)], [(100.0, 10.0)]),  # fechar: so ~$50 no bid
])
def test_sem_liquidez_para_o_tamanho_de_referencia_nao_gera(bids, asks):
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    m.estado.atualizar_livro(livro("gate", "spot", "BTC/USDT", 10.0, bids=bids, asks=asks), ativo="BTC")
    assert m.candidatos(agora=10) == []


def test_tamanho_de_referencia_vem_da_config():
    # Cada nivel vale $1000: com referencia de $2000 nao ha liquidez.
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0),
                    config=cfg_com(tamanho_referencia_usd=2000))
    assert m.candidatos(agora=10) == []


def test_corretora_desconectada_nao_gera():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    m.estado.marcar_desconectada("gate")
    assert m.candidatos(agora=10) == []


def test_livro_velho_nao_gera():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    m.estado.registrar_mensagem("gate", 45.0)
    m.estado.registrar_mensagem("bitget", 45.0)
    assert m.candidatos(agora=45) == []  # livros de 10s, 35s parados (limite 30s)


def test_ticker_sem_livro_nao_gera():
    e = EstadoMercados()
    e.atualizar_ticker("gate", "spot", "BTC", 99.9, 100.0, 10.0, simbolo_original="BTC/USDT", cotacao="USDT")
    e.atualizar_ticker("bitget", "futuro", "BTC", 101.0, 101.1, 10.0, simbolo_original="BTC/USDT:USDT", cotacao="USDT")
    e.registrar_mensagem("gate", 10.0); e.registrar_mensagem("bitget", 10.0)
    m = Motor(cfg, e, DadosLentosFalso(taxas={("gate", "spot"): 0.1, ("bitget", "futuro"): 0.06}))
    assert m.candidatos(agora=10) == []


def test_cada_chamada_cria_candidato_novo():
    # O gerenciador de sinais guarda referencias: o motor nunca reaproveita o objeto.
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    c1, c2 = m.candidatos(agora=10)[0], m.candidatos(agora=10.5)[0]
    assert c1 is not c2 and c1.ponta_long is not c2.ponta_long and c1.ponta_short is not c2.ponta_short


def test_modo_farm_preenche_custo_por_1000():
    # A bitget como corretora de farm: no modo Farm o motor so calcula combinacoes com ponta de farm.
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.9,
                    config=cfg_com(corretoras_farm=["bitget"]))
    m.sessao = Sessao(modo="farm")
    c = m.candidatos(agora=10)[0]
    assert c.modo == "farm"
    assert c.rp_liquido_pct == pytest.approx(0.1 - 0.24)
    assert c.custo_por_1000_usd == pytest.approx(-c.rp_liquido_pct * 5)


def test_modo_invalido_na_sessao_usa_lucro_e_avisa_uma_vez(caplog):
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5)
    m.sessao = Sessao(modo="xyz")
    with caplog.at_level(logging.WARNING, logger="scanner.motor"):
        eventos = m.ciclo(agora=10)  # nao levanta erro a cada ciclo
        m.ciclo(agora=10.5)
    assert eventos[0].sinal["modo"] == "lucro"
    assert len([a for a in avisos_do_motor(caplog) if "modo" in a]) == 1


def test_sessao_padrao():
    s = Motor(cfg, EstadoMercados(), DadosLentosFalso()).sessao
    assert (s.modo, s.filtros, s.comparacao) == ("lucro", Filtros(), False)


# ---------------------------------------------------------------------------
# Taxas desconhecidas e numeros invalidos
# ---------------------------------------------------------------------------


def avisos_do_motor(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "scanner.motor" and r.levelno >= logging.WARNING]


def test_sem_taxa_a_combinacao_fica_de_fora_e_avisa_uma_vez(caplog):
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), taxas=None)
    with caplog.at_level(logging.WARNING, logger="scanner.motor"):
        assert m.candidatos(agora=10) == []
        assert m.candidatos(agora=11) == []
    # Um aviso por taxa que falta (gate/spot e bitget/futuro), citando o par; a 2a chamada nao repete.
    avisos = avisos_do_motor(caplog)
    assert len(avisos) == 2 and all("taxa" in a and "BTC" in a for a in avisos)
    assert any("gate/spot" in a for a in avisos) and any("bitget/futuro" in a for a in avisos)


def test_taxa_que_falta_avisa_uma_vez_mesmo_com_varios_pares(caplog):
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    colocar_ponta(m.estado, "gate", "spot", "ETH", 10.0, "long", 10.0)
    colocar_ponta(m.estado, "bitget", "futuro", "ETH", 10.1, "short", 10.0)
    del m.dados_lentos.taxas[("bitget", "futuro")]
    with caplog.at_level(logging.WARNING, logger="scanner.motor"):
        assert m.candidatos(agora=10) == []
    assert len(avisos_do_motor(caplog)) == 1


def test_taxa_que_chega_depois_libera_a_combinacao():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), taxas=None)
    assert m.candidatos(agora=10) == []
    m.dados_lentos.taxas.update({("gate", "spot"): 0.1, ("bitget", "futuro"): 0.06})
    c = m.candidatos(agora=10)[0]
    assert (c.ponta_long.taxa_taker_pct, c.ponta_short.taxa_taker_pct) == (0.1, 0.06)
    assert c.custo_taxas_pct == pytest.approx(2 * (0.1 + 0.06))


def test_taxa_nan_nao_gera_candidato():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    m.dados_lentos.taxas[("bitget", "futuro")] = math.nan
    assert m.candidatos(agora=10) == []


def test_precarregar_recusa_nan():
    m = Motor(cfg, EstadoMercados(), DadosLentosFalso())
    with pytest.raises(ValueError):
        m.precarregar_amostra(CHAVE, 0.0, 1.0, math.nan, 1000.0, 1000.0)


# ---------------------------------------------------------------------------
# Conversao para USDT
# ---------------------------------------------------------------------------


def motor_usdc(cotacoes: dict, cotacao_spot: str = "USDC") -> Motor:
    """Spot da Gate cotado em `cotacao_spot` (ask 100) x futuro da Bitget em USDT (bid 101)."""
    e = EstadoMercados()
    colocar_ponta(e, "gate", "spot", "BTC", 100.0, "long", 10.0, cotacao=cotacao_spot)
    colocar_ponta(e, "bitget", "futuro", "BTC", 101.0, "short", 10.0)
    dados = DadosLentosFalso(taxas={("gate", "spot"): 0.06, ("bitget", "futuro"): 0.06})
    return Motor(cfg, e, dados, cotacoes=lambda: dict(cotacoes))


def test_ponta_em_usdc_convertida_pela_cotacao():
    c = motor_usdc({"USDC": 0.99}).candidatos(agora=10)[0]
    assert c.aviso_cotacao is False
    assert c.ponta_long.simbolo_original == "BTC/USDC"
    assert c.ponta_long.preco == pytest.approx(99.0)
    assert c.spread_abertura_pct == pytest.approx((101 - 99) / 99 * 100)
    assert c.ponta_long.lote_usd == pytest.approx(990.0)  # nivel de 1000 USDC
    assert c.ponta_long.volume_24h_usd == pytest.approx(990_000.0)
    assert c.ponta_short.preco == pytest.approx(101.0)  # USDT nao muda


@pytest.mark.parametrize("cotacoes", [{}, {"USDC": 0.0}, {"USDC": -1.0}, {"USDC": math.nan}, {"USDT": 2.0}])
def test_sem_cotacao_valida_usa_1_para_1_e_avisa(cotacoes):
    c = motor_usdc(cotacoes).candidatos(agora=10)[0]
    assert c.aviso_cotacao is True
    assert c.ponta_long.preco == pytest.approx(100.0)
    assert c.spread_abertura_pct == pytest.approx(1.0)


def test_usdt_nunca_usa_cotacao_nem_avisa():
    c = motor_usdc({"USDT": 2.0}, cotacao_spot="USDT").candidatos(agora=10)[0]
    assert c.aviso_cotacao is False and c.ponta_long.preco == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Metricas do periodo e lote medio
# ---------------------------------------------------------------------------


def test_primeira_amostra_na_hora_em_que_o_par_aparece():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    c = m.candidatos(agora=10)[0]
    assert c.metricas_periodo.fechamento_medio == pytest.approx(c.spread_fechamento_pct)
    assert c.metricas_periodo.abertura_media == pytest.approx(c.spread_abertura_pct)
    assert c.rp_bruto_pct == pytest.approx(c.spread_abertura_pct + c.spread_fechamento_pct)


def test_nova_amostra_so_a_cada_amostra_s():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    f1 = m.candidatos(agora=10)[0].spread_fechamento_pct

    colocar_ponta(m.estado, "bitget", "futuro", "BTC", 102.0, "short", 12.0)
    c = m.candidatos(agora=12)[0]
    assert c.metricas_periodo.fechamento_medio == pytest.approx(f1)  # 2s depois: sem amostra nova

    colocar_ponta(m.estado, "bitget", "futuro", "BTC", 102.0, "short", 15.0)
    m.estado.registrar_mensagem("gate", 15.0)
    c = m.candidatos(agora=15)[0]
    assert c.metricas_periodo.fechamento_medio == pytest.approx((f1 + c.spread_fechamento_pct) / 2)


def test_amostra_s_vem_da_config():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), config=cfg_com(amostra_s=1))
    f1 = m.candidatos(agora=10)[0].spread_fechamento_pct
    colocar_ponta(m.estado, "bitget", "futuro", "BTC", 102.0, "short", 11.0)
    m.estado.registrar_mensagem("gate", 11.0)
    c = m.candidatos(agora=11)[0]
    assert c.metricas_periodo.fechamento_medio == pytest.approx((f1 + c.spread_fechamento_pct) / 2)


# 11 amostras de F: maior -0,5; media dos 10 maiores (-0,5 - 9) / 10; media (-0,5 - 9 - 12) / 11.
F_PRECARREGADOS = [-0.5] + [-1.0] * 9 + [-12.0]
ESPERADO_POR_METRICA = {"medio": -21.5 / 11, "maior": -0.5, "media_10_maiores": -0.95}


def motor_com_historico(**kwargs) -> Motor:
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), **kwargs)
    for ts, f in enumerate(F_PRECARREGADOS):  # ts 0..10: em agora=10 nao entra amostra nova
        m.precarregar_amostra(CHAVE, float(ts), 1.0, f, 1000.0, 1000.0)
    return m


@pytest.mark.parametrize("metrica", ["medio", "maior", "media_10_maiores"])
def test_rp_bruto_usa_a_metrica_da_config(metrica):
    c = motor_com_historico(config=cfg_com(metrica_spread=metrica)).candidatos(agora=10)[0]
    assert c.rp_bruto_pct == pytest.approx(1.0 + ESPERADO_POR_METRICA[metrica])


def test_metrica_dos_filtros_tem_prioridade_sobre_a_config():
    m = motor_com_historico()  # config: medio
    m.sessao.filtros = Filtros(metrica_spread="maior")
    assert m.candidatos(agora=10)[0].rp_bruto_pct == pytest.approx(1.0 - 0.5)


def test_metrica_invalida_nos_filtros_usa_a_da_config_e_avisa_uma_vez(caplog):
    m = motor_com_historico()
    m.sessao.filtros = Filtros(metrica_spread="nao_existe")
    with caplog.at_level(logging.WARNING, logger="scanner.motor"):
        assert m.candidatos(agora=10)[0].rp_bruto_pct == pytest.approx(1.0 + ESPERADO_POR_METRICA["medio"])
        m.candidatos(agora=10)
    assert len([r for r in caplog.records if "metrica_spread" in r.getMessage()]) == 1


def test_duracao_minima_de_invertida_vem_dos_filtros():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    for ts, f in [(0.0, 0.1), (4.0, 0.2), (8.0, -1.0), (10.0, -1.0)]:  # F >= 0 por 4s
        m.precarregar_amostra(CHAVE, ts, 1.0, f, 1000.0, 1000.0)
    assert m.candidatos(agora=10)[0].metricas_periodo.invertidas == 1  # sem filtro: duracao 0
    m.sessao.filtros = Filtros(invertidas_duracao_min_s=5)
    assert m.candidatos(agora=10)[0].metricas_periodo.invertidas == 0


def test_periodo_dos_filtros_recria_as_janelas_e_muda_o_gerenciador():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), agora=7200.0)
    m.precarregar_amostra(CHAVE, 0.0, 1.0, -5.0, 200.0, 200.0)
    m.precarregar_amostra(CHAVE, 7200.0, 1.0, -1.0, 1000.0, 1000.0)
    c = m.candidatos(agora=7200)[0]
    assert c.metricas_periodo.fechamento_medio == pytest.approx(-3.0)  # config: 4h, as duas amostras
    assert c.ponta_long.lote_medio_usd == pytest.approx(600.0)
    assert m.gerenciador.periodo_h == 4

    m.sessao.filtros = Filtros(periodo_metricas_h=1)
    c = m.candidatos(agora=7200)[0]
    # Periodo novo = janelas novas: a primeira amostra e a de agora.
    assert c.metricas_periodo.fechamento_medio == pytest.approx(c.spread_fechamento_pct)
    assert c.ponta_long.lote_medio_usd == pytest.approx(1000.0)
    assert m.gerenciador.periodo_h == 1

    m.sessao.filtros = Filtros()  # volta para o periodo da config
    m.candidatos(agora=7200)
    assert m.gerenciador.periodo_h == 4


@pytest.mark.parametrize("periodo", ["4", 0, -1, math.nan, True])
def test_periodo_invalido_nos_filtros_usa_o_da_config(periodo, caplog):
    m = motor_com_historico()
    m.sessao.filtros = Filtros(periodo_metricas_h=periodo)
    with caplog.at_level(logging.WARNING, logger="scanner.motor"):
        c = m.candidatos(agora=10)[0]
    assert c.rp_bruto_pct == pytest.approx(1.0 + ESPERADO_POR_METRICA["medio"])  # janelas mantidas
    assert m.gerenciador.periodo_h == cfg.periodo_metricas_h
    assert any("periodo_metricas_h" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("duracao", ["5", -1, math.nan])
def test_duracao_de_invertida_invalida_usa_zero(duracao):
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    for ts, f in [(0.0, 0.1), (4.0, 0.2), (8.0, -1.0), (10.0, -1.0)]:
        m.precarregar_amostra(CHAVE, ts, 1.0, f, 1000.0, 1000.0)
    m.sessao.filtros = Filtros(invertidas_duracao_min_s=duracao)
    assert m.candidatos(agora=10)[0].metricas_periodo.invertidas == 1


def test_historico_de_par_sumido_e_esquecido():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    m.candidatos(agora=10)
    assert CHAVE in m._historicos
    m.estado.marcar_desconectada("gate")
    m.candidatos(agora=10 + cfg.periodo_metricas_h * 3600)  # ainda dentro do periodo
    assert CHAVE in m._historicos
    m.candidatos(agora=10 + cfg.periodo_metricas_h * 3600 + 1)
    assert CHAVE not in m._historicos


def test_lote_medio_do_periodo():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    m.candidatos(agora=10)  # amostra com lote $1000
    m.estado.atualizar_livro(livro("gate", "spot", "BTC/USDT", 15.0, bids=[(99.9, 10.0)],
                                   asks=[(100.0, 5.0)]), ativo="BTC")  # melhor ask agora vale $500
    m.estado.registrar_mensagem("gate", 15.0); m.estado.registrar_mensagem("bitget", 15.0)
    c = m.candidatos(agora=15)[0]
    assert c.ponta_long.lote_usd == pytest.approx(500.0)
    assert c.ponta_long.lote_medio_usd == pytest.approx(750.0)
    assert c.ponta_short.lote_medio_usd == pytest.approx(1000.0)


def test_amostra_mais_velha_que_o_periodo_sai_da_media():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    m.precarregar_amostra(CHAVE, 10.0, 1.0, -9.0, 1000.0, 1000.0)
    periodo_s = cfg.periodo_metricas_h * 3600
    agora = 10.0 + periodo_s + 1
    colocar_ponta(m.estado, "gate", "spot", "BTC", 100.0, "long", agora)
    colocar_ponta(m.estado, "bitget", "futuro", "BTC", 101.0, "short", agora)
    c = m.candidatos(agora=agora)[0]
    assert c.metricas_periodo.fechamento_medio == pytest.approx(c.spread_fechamento_pct)  # a amostra -9 saiu


# ---------------------------------------------------------------------------
# Etapa 2: quais livros abrir
# ---------------------------------------------------------------------------


def ticker(e, corretora, mercado, ativo, bid, ask, agora, cotacao="USDT"):
    """So o ticker (etapa 1), sem livro, e a mensagem da corretora."""
    simbolo = f"{ativo}/{cotacao}" if mercado == "spot" else f"{ativo}/{cotacao}:{cotacao}"
    e.atualizar_ticker(corretora, mercado, ativo, bid, ask, agora, simbolo_original=simbolo, cotacao=cotacao)
    e.registrar_mensagem(corretora, agora)


def motor_so_tickers(e, cotacoes=None) -> Motor:
    return Motor(cfg, e, DadosLentosFalso(), cotacoes=lambda: dict(cotacoes or {}))


def test_selecao_pega_as_duas_pontas_acima_do_minimo():
    e = EstadoMercados()
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.5, 100.6, 10.0)  # A = 0,5% (minimo 0,3%)
    assert motor_so_tickers(e).selecao_livros(agora=10) == {"gate": {"BTC/USDT"}, "bitget": {"BTC/USDT:USDT"}}


def test_selecao_abaixo_do_minimo_fica_vazia():
    e = EstadoMercados()
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.2, 100.3, 10.0)  # A = 0,2%
    assert motor_so_tickers(e).selecao_livros(agora=10) == {}


def test_selecao_minimo_vem_da_config():
    e = EstadoMercados()
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.2, 100.3, 10.0)  # A = 0,2%
    m = Motor(cfg_com(spread_min_livro_pct=0.1), e, DadosLentosFalso())
    assert m.selecao_livros(agora=10) == {"gate": {"BTC/USDT"}, "bitget": {"BTC/USDT:USDT"}}


def test_selecao_mantem_o_livro_por_manter_s_depois_de_cair():
    e = EstadoMercados()
    m = motor_so_tickers(e)
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.5, 100.6, 10.0)
    assert m.selecao_livros(agora=10) != {}
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 40.0)
    ticker(e, "bitget", "futuro", "BTC", 100.1, 100.2, 40.0)  # caiu para 0,1%
    assert m.selecao_livros(agora=40) == {"gate": {"BTC/USDT"}, "bitget": {"BTC/USDT:USDT"}}
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 71.0)
    ticker(e, "bitget", "futuro", "BTC", 100.1, 100.2, 71.0)
    assert m.selecao_livros(agora=71) == {}  # 61s abaixo (manter_livro_s = 60)


def test_selecao_mantem_mesmo_se_o_ticker_sumir_e_depois_solta():
    e = EstadoMercados()
    m = motor_so_tickers(e)
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.5, 100.6, 10.0)
    m.selecao_livros(agora=10)
    e.marcar_desconectada("bitget")
    assert m.selecao_livros(agora=20) == {"gate": {"BTC/USDT"}, "bitget": {"BTC/USDT:USDT"}}
    assert m.selecao_livros(agora=71) == {}


def test_selecao_converte_os_tickers_para_usdt():
    e = EstadoMercados()
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0, cotacao="USDC")
    ticker(e, "bitget", "futuro", "BTC", 100.2, 100.3, 10.0)
    assert motor_so_tickers(e, {"USDC": 1.0}).selecao_livros(agora=10) == {}  # A = 0,2%
    assert motor_so_tickers(e, {"USDC": 0.995}).selecao_livros(agora=10) == {  # ask 99,5 USDT: A = 0,7%
        "gate": {"BTC/USDC"}, "bitget": {"BTC/USDT:USDT"}}


def test_selecao_ignora_corretora_silenciosa():
    e = EstadoMercados()
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.5, 100.6, 7.0)  # 3s sem mensagem em agora=10
    assert motor_so_tickers(e).selecao_livros(agora=10) == {}


def test_selecao_junta_os_simbolos_de_varias_combinacoes():
    e = EstadoMercados()
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.5, 100.6, 10.0)
    ticker(e, "mexc", "futuro", "BTC", 100.8, 100.9, 10.0)
    ticker(e, "gate", "futuro", "BTC", 100.1, 100.2, 10.0)
    # Spot x futuro: gate x bitget (0,5%) e gate x mexc (0,8%) entram; gate x gate (0,1%) fica de fora.
    # Futuro x futuro: o futuro da gate entra pelo gate x mexc (ask 100,2 e bid 100,8: ~0,6%);
    # gate x bitget (~0,299%) e bitget x mexc (~0,2%) ficam abaixo de 0,3%.
    assert motor_so_tickers(e).selecao_livros(agora=10) == {
        "gate": {"BTC/USDT", "BTC/USDT:USDT"}, "bitget": {"BTC/USDT:USDT"}, "mexc": {"BTC/USDT:USDT"}}


# ---------------------------------------------------------------------------
# ciclo, banco e status
# ---------------------------------------------------------------------------


def chave_texto(chave) -> str:
    return "|".join(map(str, chave))


def test_ciclo_gera_evento_novo_e_grava_no_banco(tmp_path):
    banco = Banco(str(tmp_path / "s.db"))
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5, banco=banco)
    eventos = m.ciclo(agora=10)  # RP liquido 0,5 - 0,24 = 0,26 >= 0,2
    assert [e.tipo for e in eventos] == ["novo"] and eventos[0].registrar_historico is True
    assert eventos[0].sinal["par"] == "BTC/USDT" and eventos[0].sinal["metricas_periodo"]["periodo_h"] == 4
    registros = banco.sinais_do_dia("1970-01-01")
    assert [r["id"] for r in registros] == [eventos[0].sinal["id"]] and registros[0]["encerrado_em"] is None
    assert m.sinais_ativos()[0]["id"] == eventos[0].sinal["id"]

    assert [e.tipo for e in m.ciclo(agora=10.5)] == ["atualizado"]
    banco.fechar()


def test_ciclo_encerra_quando_o_candidato_some(tmp_path):
    banco = Banco(str(tmp_path / "s.db"))
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5, banco=banco)
    m.ciclo(agora=10)
    m.estado.marcar_desconectada("bitget")
    eventos = m.ciclo(agora=11)
    assert [e.tipo for e in eventos] == ["encerrado"]
    assert banco.sinais_do_dia("1970-01-01")[0]["encerrado_em"] == "1970-01-01T00:00:11.000Z"
    assert m.sinais_ativos() == []
    banco.fechar()


def test_ciclo_grava_amostras_dos_candidatos_a_cada_amostra_s(tmp_path):
    banco = Banco(str(tmp_path / "s.db"))
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), banco=banco)
    assert m.ciclo(agora=10) == []  # RP negativo: nao vira sinal, mas o par e candidato
    m.ciclo(agora=12)
    colocar_ponta(m.estado, "gate", "spot", "BTC", 100.0, "long", 15.0)
    colocar_ponta(m.estado, "bitget", "futuro", "BTC", 101.0, "short", 15.0)
    m.ciclo(agora=15)
    amostras = banco.amostras(chave_texto(CHAVE), desde=0)
    assert [a["ts"] for a in amostras] == [10.0, 15.0]
    a = amostras[0]
    assert a["a"] == pytest.approx(1.0) and a["f"] == pytest.approx(m.candidatos(agora=15)[0].spread_fechamento_pct)
    assert (a["preco_long"], a["preco_short"]) == (pytest.approx(100.0), pytest.approx(101.0))
    assert (a["lote_long"], a["lote_short"]) == (pytest.approx(1000.0), pytest.approx(1000.0))
    banco.fechar()


def test_ciclo_nao_regrava_amostra_precarregada(tmp_path):
    banco = Banco(str(tmp_path / "s.db"))
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5, banco=banco)
    m.ciclo(agora=10)
    assert banco.amostras(chave_texto(CHAVE), desde=0) == []  # a amostra de 10s ja existia
    banco.fechar()


def test_ciclo_usa_modo_e_comparacao_da_sessao():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5,
                    config=cfg_com(rp_min_lucro_pct=0.3))  # RP bruto 0,5; liquido 0,26
    assert m.ciclo(agora=10) == []
    m.sessao.comparacao = True  # compara pelo RP bruto, como o Arbimax
    assert [e.tipo for e in m.ciclo(agora=10.5)] == ["novo"]


def test_ciclo_aplica_os_filtros_da_sessao():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5)
    m.sessao.filtros = Filtros(lista_negra=["BTC"])
    assert m.ciclo(agora=10) == []


def test_ciclo_modo_farm_grava_custo_por_1000():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.9,
                    config=cfg_com(corretoras_farm=["bitget"]))  # RP liquido -0,14 >= -0,1? nao
    m.sessao = Sessao(modo="farm", filtros=Filtros(custo_max_farm_pct=-0.2))
    eventos = m.ciclo(agora=10)
    assert eventos[0].sinal["modo"] == "farm"
    assert eventos[0].sinal["custo_por_1000_usd"] == pytest.approx(0.14 * 5)


def test_status_por_corretora():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), silencio_futuro_s=3)
    sem_dados = {"atraso_medio_ms": None, "reconexoes": 0}
    assert m.status(agora=10) == {
        "mexc": {"estado": "atrasada", **sem_dados},  # nunca mandou mensagem
        "gate": {"estado": "conectada", **sem_dados},
        "bitget": {"estado": "atrasada", **sem_dados},
    }


# ---------------------------------------------------------------------------
# Futuros x futuros (Task 13)
# ---------------------------------------------------------------------------


def por_chave(candidatos) -> dict:
    """Os candidatos indexados pela chave do sinal."""
    return {c.chave: c for c in candidatos}


def test_pares_futuro_futuro_todas_as_combinacoes_em_ordem():
    m = {
        "mexc": {("futuro", "BTC"), ("futuro", "ETH")},
        "gate": {("spot", "BTC"), ("futuro", "BTC"), ("spot", "ETH")},
        "bitget": {("futuro", "BTC"), ("futuro", "ETH"), ("futuro", "XRP")},
    }
    assert pares_futuro_futuro(m) == [
        ("BTC", "bitget", "futuro", "gate", "futuro"),
        ("BTC", "bitget", "futuro", "mexc", "futuro"),
        ("BTC", "gate", "futuro", "bitget", "futuro"),
        ("BTC", "gate", "futuro", "mexc", "futuro"),
        ("BTC", "mexc", "futuro", "bitget", "futuro"),
        ("BTC", "mexc", "futuro", "gate", "futuro"),
        ("ETH", "bitget", "futuro", "mexc", "futuro"),
        ("ETH", "mexc", "futuro", "bitget", "futuro"),
    ]


def test_pares_futuro_futuro_sem_par_possivel():
    assert pares_futuro_futuro({"gate": {("spot", "BTC")}, "mexc": {("futuro", "BTC")}}) == []
    assert pares_futuro_futuro({"bitget": {("spot", "BTC"), ("futuro", "BTC")}}) == []  # uma corretora so
    assert pares_futuro_futuro({"gate": {("futuro", "BTC")}, "mexc": {("futuro", "ETH")}}) == []
    assert pares_futuro_futuro({}) == []


def test_combinacoes_junta_os_dois_tipos():
    m = {"gate": {("spot", "BTC"), ("futuro", "BTC")}, "mexc": {("futuro", "BTC")}}
    assert combinacoes(m) == pares_spot_futuro(m) + pares_futuro_futuro(m) == [
        ("BTC", "gate", "spot", "gate", "futuro"),
        ("BTC", "gate", "spot", "mexc", "futuro"),
        ("BTC", "gate", "futuro", "mexc", "futuro"),
        ("BTC", "mexc", "futuro", "gate", "futuro"),
    ]  # nunca spot vendido nem futuro x futuro na mesma corretora


def test_simbolos_para_tickers_inclui_futuro_x_futuro_e_corretora_so_de_futuros():
    # Nenhuma corretora tem o spot do ETH: o ETH so forma par futuro x futuro.
    vencedores = {
        "hyperliquid": {("futuro", "ETH"): "ETH/USDC:USDC", ("futuro", "SOL"): "SOL/USDC:USDC"},
        "gate": {("futuro", "ETH"): "ETH/USDT:USDT", ("spot", "DOGE"): "DOGE/USDT"},
    }
    assert simbolos_para_tickers(vencedores) == {"hyperliquid": ["ETH/USDC:USDC"], "gate": ["ETH/USDT:USDT"]}


def test_simbolos_para_tickers_junta_os_dois_tipos_sem_repetir():
    # O futuro de BTC da mexc forma par com o spot e com o futuro da gate: aparece uma vez so.
    vencedores = {
        "gate": {("spot", "BTC"): "BTC/USDT", ("futuro", "BTC"): "BTC/USDT:USDT", ("futuro", "ETH"): "ETH/USDT:USDT"},
        "mexc": {("futuro", "BTC"): "BTC/USDT:USDT", ("futuro", "ETH"): "ETH/USDT:USDT"},
    }
    assert simbolos_para_tickers(vencedores) == {
        "gate": ["BTC/USDT", "BTC/USDT:USDT", "ETH/USDT:USDT"],
        "mexc": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
    }


def test_candidato_futuro_futuro_preenche_as_duas_pontas_futuras():
    m = motor_falso(long_futuro=("bitget", "BTC", 100.0), short_futuro=("gate", "BTC", 101.0),
                    funding_long=(0.02, 8), funding_short=(0.01, 4))
    c = por_chave(m.candidatos(agora=10))[("BTC/USDT", "futuro_futuro", "bitget", "futuro", "gate", "futuro")]
    pl, ps = c.ponta_long, c.ponta_short
    assert (pl.corretora, pl.mercado, pl.simbolo_original) == ("bitget", "futuro", "BTC/USDT:USDT")
    assert (ps.corretora, ps.mercado, ps.simbolo_original) == ("gate", "futuro", "BTC/USDT:USDT")
    assert pl.preco == pytest.approx(100.0) and ps.preco == pytest.approx(101.0)
    assert c.spread_abertura_pct == pytest.approx(1.0)
    assert (pl.funding_pct, pl.funding_intervalo_h) == (0.02, 8)  # as duas pontas futuras tem funding
    assert (ps.funding_pct, ps.funding_intervalo_h) == (0.01, 4)
    assert pl.taxa_taker_pct == 0.06 and ps.taxa_taker_pct == 0.06 and c.custo_taxas_pct == pytest.approx(0.24)


def test_funding_das_duas_pontas_com_intervalos_diferentes():
    # Operacao de 6h: o long (bitget) cobra de 1 em 1h (6 cobrancas) e o short (gate) de 4 em 4h (1 cobranca).
    m = motor_falso(long_futuro=("bitget", "BTC", 100.0), short_futuro=("gate", "BTC", 101.0), f_medio=-0.5,
                    funding_long=(0.01, 1), funding_short=(0.03, 4), config=cfg_com(tempo_operacao_h=6))
    c = por_chave(m.candidatos(agora=10))[("BTC/USDT", "futuro_futuro", "bitget", "futuro", "gate", "futuro")]
    assert c.funding_esperado_pct == pytest.approx(0.03 * 1 - 0.01 * 6)  # recebe no short, paga no long
    assert c.fr_yield_8h_pct == pytest.approx(0.03 * 8 / 4 - 0.01 * 8 / 1)  # normalizado para 8h
    assert c.rp_bruto_pct == pytest.approx(1.0 - 0.5)
    assert c.rp_liquido_pct == pytest.approx(0.5 - 0.24 + (0.03 - 0.06))

    # Na direcao contraria as pontas trocam de papel: o funding troca de sinal.
    inverso = por_chave(m.candidatos(agora=10))[("BTC/USDT", "futuro_futuro", "gate", "futuro", "bitget", "futuro")]
    assert inverso.funding_esperado_pct == pytest.approx(0.01 * 6 - 0.03 * 1)
    assert inverso.fr_yield_8h_pct == pytest.approx(0.01 * 8 / 1 - 0.03 * 8 / 4)


def test_duas_direcoes_geram_dois_candidatos_distintos(tmp_path):
    banco = Banco(str(tmp_path / "s.db"))
    m = motor_falso(long_futuro=("bitget", "BTC", 100.0), short_futuro=("gate", "BTC", 101.0), banco=banco)
    candidatos = m.candidatos(agora=10)
    assert [c.chave for c in candidatos] == [
        ("BTC/USDT", "futuro_futuro", "bitget", "futuro", "gate", "futuro"),
        ("BTC/USDT", "futuro_futuro", "gate", "futuro", "bitget", "futuro"),
    ]
    ida, volta = candidatos
    assert ida.spread_abertura_pct == pytest.approx(1.0)  # compra a 100 na bitget, vende a 101 na gate
    # Volta: compra no ask da gate (101 + 0,1%) e vende no bid da bitget (100 - 0,1%).
    assert (volta.ponta_long.preco, volta.ponta_short.preco) == (pytest.approx(101.101), pytest.approx(99.9))
    assert volta.spread_abertura_pct == pytest.approx((99.9 - 101.101) / 101.101 * 100)
    # Cada direcao tem o seu periodo (amostras gravadas com a sua chave).
    m.ciclo(agora=10.5)
    assert len(banco.amostras("BTC/USDT|futuro_futuro|bitget|futuro|gate|futuro", desde=0)) == 1
    assert len(banco.amostras("BTC/USDT|futuro_futuro|gate|futuro|bitget|futuro", desde=0)) == 1
    banco.fechar()


def test_futuro_futuro_na_mesma_corretora_nao_gera():
    e = EstadoMercados()
    colocar_ponta(e, "gate", "futuro", "BTC", 100.0, "long", 10.0)
    m = Motor(cfg, e, DadosLentosFalso(taxas={("gate", "futuro"): 0.06}))
    assert m.candidatos(agora=10) == []


def test_candidato_futuro_futuro_com_corretora_so_de_futuros_em_usdc():
    e = EstadoMercados()
    colocar_ponta(e, "hyperliquid", "futuro", "BTC", 100.0, "long", 10.0, cotacao="USDC")
    colocar_ponta(e, "gate", "futuro", "BTC", 101.0, "short", 10.0)
    dados = DadosLentosFalso(taxas={("hyperliquid", "futuro"): 0.045, ("gate", "futuro"): 0.05})
    m = Motor(cfg, e, dados, cotacoes=lambda: {"USDC": 0.99})
    c = por_chave(m.candidatos(agora=10))[("BTC/USDT", "futuro_futuro", "hyperliquid", "futuro", "gate", "futuro")]
    assert c.ponta_long.simbolo_original == "BTC/USDC:USDC" and c.aviso_cotacao is False
    assert c.ponta_long.preco == pytest.approx(99.0)  # 100 USDC x 0,99
    assert c.spread_abertura_pct == pytest.approx((101 - 99) / 99 * 100)
    assert c.custo_taxas_pct == pytest.approx(2 * (0.045 + 0.05))


def test_sinal_futuro_futuro_tipo_chave_e_id():
    m = motor_falso(long_futuro=("bitget", "AKAM", 89.14), short_futuro=("gate", "AKAM", 100.32),
                    f_medio=-11.91, funding_short=(0.01, 8), funding_long=(0.0, 8))
    eventos = m.ciclo(agora=10)
    assert [e.tipo for e in eventos] == ["novo"]  # a direcao contraria (gate x bitget) tem RP negativo
    s = eventos[0].sinal
    assert s["id"] == "AKAM-futuro_futuro-bitget-gate-19700101T000010Z"
    assert (s["par"], s["tipo"], s["modo"]) == ("AKAM/USDT", "futuro_futuro", "lucro")
    assert (s["ponta_long"]["corretora"], s["ponta_long"]["mercado"]) == ("bitget", "futuro")
    assert (s["ponta_short"]["corretora"], s["ponta_short"]["mercado"]) == ("gate", "futuro")
    assert (s["ponta_long"]["funding_pct"], s["ponta_long"]["funding_intervalo_h"]) == (0.0, 8)
    assert (s["ponta_short"]["funding_pct"], s["ponta_short"]["funding_intervalo_h"]) == (0.01, 8)
    assert s["funding_esperado_pct"] == pytest.approx(0.01)
    c = m.candidatos(agora=10)[0]
    assert c.chave == chave_de(("AKAM", "bitget", "futuro", "gate", "futuro")) == (
        "AKAM/USDT", "futuro_futuro", "bitget", "futuro", "gate", "futuro")


def motor_dois_tipos(**kwargs) -> Motor:
    """Compram: spot da Gate (ask 100) e futuro da MEXC (ask 100). Vende: futuro da Bitget (bid 101).

    Gera 2 spot x futuro (gate/spot x bitget e gate/spot x mexc) e 2 futuro x futuro (mexc x bitget e bitget x mexc).
    """
    return motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0),
                       long_futuro=("mexc", "BTC", 100.0), **kwargs)


def test_motor_gera_os_dois_tipos():
    tipos = [c.tipo for c in motor_dois_tipos().candidatos(agora=10)]
    assert sorted(tipos) == ["futuro_futuro", "futuro_futuro", "spot_futuro", "spot_futuro"]


def test_filtro_tipos_escolhe_o_tipo_no_ciclo_e_o_motor_segue_amostrando_os_dois():
    m = motor_dois_tipos(config=cfg_com(rp_min_lucro_pct=-10.0))  # todos os candidatos passam no modo Lucro
    chave_ff = chave_de(("BTC", "mexc", "futuro", "bitget", "futuro"))

    m.sessao.filtros = Filtros(tipos=["spot_futuro"])
    eventos = m.ciclo(agora=10)
    assert [(e.tipo, e.sinal["tipo"]) for e in eventos] == [("novo", "spot_futuro")] * 2
    assert m._historicos[chave_ff].ultima_amostra == 10.0  # o futuro x futuro segue amostrado (metricas aquecidas)

    m.sessao.filtros = Filtros(tipos=["futuro_futuro"])
    eventos = m.ciclo(agora=10.5)
    assert sorted((e.tipo, e.sinal["tipo"]) for e in eventos) == [
        ("encerrado", "spot_futuro"), ("encerrado", "spot_futuro"),
        ("novo", "futuro_futuro"), ("novo", "futuro_futuro"),
    ]
    assert m._historicos[chave_ff].ultima_amostra == 10.0  # usou as amostras de antes, sem recomecar

    m.sessao.filtros = Filtros()  # sem filtro de tipo: os dois
    m.ciclo(agora=11)
    assert sorted(s["tipo"] for s in m.sinais_ativos()) == ["futuro_futuro"] * 2 + ["spot_futuro"] * 2


def test_selecao_inclui_futuro_x_futuro_e_corretora_so_de_futuros():
    e = EstadoMercados()
    ticker(e, "hyperliquid", "futuro", "BTC", 99.9, 100.0, 10.0, cotacao="USDC")  # so futuros, em USDC
    ticker(e, "gate", "futuro", "BTC", 100.5, 100.6, 10.0)  # A = 0,5% com o long na Hyperliquid
    esperado = {"hyperliquid": {"BTC/USDC:USDC"}, "gate": {"BTC/USDT:USDT"}}
    m = motor_so_tickers(e, {"USDC": 1.0})
    assert m.selecao_livros(agora=10) == esperado
    m.sessao.filtros = Filtros(tipos=["spot_futuro"])  # o filtro de tipo nao muda quais livros abrem
    assert m.selecao_livros(agora=10.5) == esperado


def test_selecao_futuro_x_futuro_abaixo_do_minimo_fica_vazia():
    e = EstadoMercados()
    ticker(e, "gate", "futuro", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "mexc", "futuro", "BTC", 100.2, 100.3, 10.0)  # gate x mexc: 0,2%; mexc x gate: negativo
    assert motor_so_tickers(e).selecao_livros(agora=10) == {}


@pytest.mark.parametrize("minimo, esperado", [
    (0.0, {"gate": {"BTC/USDT"}, "bitget": {"BTC/USDT:USDT"}}),  # spread exatamente 0 chega ao minimo 0
    (-0.5, {"gate": {"BTC/USDT", "BTC/USDT:USDT"}, "bitget": {"BTC/USDT:USDT"}}),  # negativos tambem entram
])
def test_selecao_com_minimo_zero_ou_negativo(minimo, esperado):
    e = EstadoMercados()
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.0, 100.1, 10.0)  # gate/spot x bitget: A = 0%
    ticker(e, "gate", "futuro", "BTC", 99.8, 100.2, 10.0)  # gate/futuro x bitget: A ~ -0,2%; gate x gate: ~ -0,2%
    m = Motor(cfg_com(spread_min_livro_pct=minimo), e, DadosLentosFalso())
    assert m.selecao_livros(agora=10) == esperado


def test_selecao_pula_o_short_que_nao_forma_par_e_segue_procurando():
    # Minimo negativo: o maior bid de BTC e o do proprio futuro da gate, que nao pode ser o short do
    # futuro da gate (futuro x futuro na mesma corretora). O proximo (mexc) forma par e chega ao minimo.
    e = EstadoMercados()
    ticker(e, "gate", "futuro", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "mexc", "futuro", "BTC", 99.8, 100.4, 10.0)  # gate x mexc: -0,2%; mexc x gate: ~ -0,5%
    m = Motor(cfg_com(spread_min_livro_pct=-0.3), e, DadosLentosFalso())
    assert m.selecao_livros(agora=10) == {"gate": {"BTC/USDT:USDT"}, "mexc": {"BTC/USDT:USDT"}}
    assert list(m._ultima_vez_acima) == [("BTC", "gate", "futuro", "mexc", "futuro")]


def test_selecao_acompanha_mercados_que_aparecem_e_somem():
    e = EstadoMercados()
    m = motor_so_tickers(e)
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.1, 100.2, 10.0)  # 0,1%: abaixo do minimo
    assert m.selecao_livros(agora=10) == {}
    ticker(e, "mexc", "futuro", "BTC", 100.35, 100.45, 10.0)  # mercado novo: gate x mexc 0,35%; bitget x mexc ~0,15%
    assert m.selecao_livros(agora=10) == {"gate": {"BTC/USDT"}, "mexc": {"BTC/USDT:USDT"}}
    e.marcar_desconectada("mexc")
    ticker(e, "bitget", "futuro", "BTC", 100.5, 100.6, 100.0)  # mais de 60s depois: so gate x bitget (0,5%)
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 100.0)
    assert m.selecao_livros(agora=100) == {"gate": {"BTC/USDT"}, "bitget": {"BTC/USDT:USDT"}}


def test_selecao_so_guarda_os_simbolos_das_combinacoes_escolhidas():
    e = EstadoMercados()
    for i in range(20):  # 20 ativos com spread abaixo do minimo
        ticker(e, "gate", "spot", f"X{i}", 99.9, 100.0, 10.0)
        ticker(e, "bitget", "futuro", f"X{i}", 100.1, 100.2, 10.0)
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.5, 100.6, 10.0)
    m = motor_so_tickers(e)
    m.selecao_livros(agora=10)
    assert list(m._simbolos_livro) == [("BTC", "gate", "spot", "bitget", "futuro")]


def contar_montagens(monkeypatch) -> list:
    """Troca `combinacoes` do motor por uma versao que anota cada chamada (quantas vezes a lista foi montada)."""
    import scanner.motor as modulo_motor

    chamadas = []
    original = modulo_motor.combinacoes

    def contando(mercados):
        chamadas.append(mercados)
        return original(mercados)

    monkeypatch.setattr(modulo_motor, "combinacoes", contando)
    return chamadas


def test_selecao_nao_remonta_as_combinacoes_quando_um_ticker_fica_velho_ou_volta(monkeypatch):
    e = EstadoMercados()
    m = motor_so_tickers(e)
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.5, 100.6, 10.0)
    ticker(e, "mexc", "futuro", "BTC", 100.8, 100.9, 10.0)
    montagens = contar_montagens(monkeypatch)
    m.selecao_livros(agora=10)
    assert len(montagens) == 1
    # A mexc para de mandar ticker de BTC (mas segue viva): 31s depois o ticker dela fica velho.
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 41.0)
    ticker(e, "bitget", "futuro", "BTC", 100.5, 100.6, 41.0)
    e.registrar_mensagem("mexc", 41.0)
    assert m.selecao_livros(agora=41) == {"gate": {"BTC/USDT"}, "bitget": {"BTC/USDT:USDT"},
                                          "mexc": {"BTC/USDT:USDT"}}  # mexc: mantida por manter_livro_s
    ticker(e, "mexc", "futuro", "BTC", 100.8, 100.9, 42.0)  # o ticker volta
    m.selecao_livros(agora=42)
    assert len(montagens) == 1  # os mercados (com ticker guardado) nao mudaram: a lista nao foi montada de novo


def test_selecao_ticker_que_fica_velho_sai_e_volta():
    e = EstadoMercados()
    m = Motor(cfg_com(manter_livro_s=5), e, DadosLentosFalso())
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "mexc", "futuro", "BTC", 100.8, 100.9, 10.0)  # gate x mexc: 0,8%
    assert m.selecao_livros(agora=10) == {"gate": {"BTC/USDT"}, "mexc": {"BTC/USDT:USDT"}}
    # 31s sem ticker da mexc (a corretora segue viva): a combinacao sai depois de manter_livro_s.
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 41.0)
    e.registrar_mensagem("mexc", 41.0)
    assert m.selecao_livros(agora=41) == {}
    ticker(e, "mexc", "futuro", "BTC", 100.8, 100.9, 42.0)
    e.registrar_mensagem("gate", 42.0)
    assert m.selecao_livros(agora=42) == {"gate": {"BTC/USDT"}, "mexc": {"BTC/USDT:USDT"}}


@pytest.mark.parametrize("bid, ask", [(None, None), ("x", "y"), (None, 10.0), (math.nan, math.inf)])
def test_selecao_ignora_ticker_com_bid_ou_ask_invalido(bid, ask):
    # Estado preenchido direto (sem o coletor, que ja descartaria esse ticker).
    e = EstadoMercados()
    e.atualizar_ticker("gate", "spot", "ETH", bid, ask, 10.0, simbolo_original="ETH/USDT", cotacao="USDT")
    ticker(e, "bitget", "futuro", "ETH", 10.5, 10.6, 10.0)
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    ticker(e, "bitget", "futuro", "BTC", 100.5, 100.6, 10.0)
    selecao = motor_so_tickers(e).selecao_livros(agora=10)  # nao levanta erro
    if ask == 10.0:  # so o bid (que o spot nunca usa) e invalido: o spot ainda serve de long (A = 5%)
        assert selecao == {"gate": {"BTC/USDT", "ETH/USDT"}, "bitget": {"BTC/USDT:USDT", "ETH/USDT:USDT"}}
    else:  # o ETH da gate fica de fora; o resto segue
        assert selecao == {"gate": {"BTC/USDT"}, "bitget": {"BTC/USDT:USDT"}}


def test_selecao_ponta_short_com_ask_invalido_ainda_vende_pelo_bid():
    e = EstadoMercados()
    ticker(e, "gate", "spot", "BTC", 99.9, 100.0, 10.0)
    e.atualizar_ticker("bitget", "futuro", "BTC", 100.5, None, 10.0, simbolo_original="BTC/USDT:USDT", cotacao="USDT")
    e.registrar_mensagem("bitget", 10.0)
    assert motor_so_tickers(e).selecao_livros(agora=10) == {"gate": {"BTC/USDT"}, "bitget": {"BTC/USDT:USDT"}}


def test_candidatos_acompanham_livros_que_aparecem_e_somem():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    assert len(m.candidatos(agora=10)) == 1
    colocar_ponta(m.estado, "mexc", "futuro", "BTC", 101.5, "short", 10.0)
    m.dados_lentos.taxas[("mexc", "futuro")] = 0.06
    chaves = {c.chave for c in m.candidatos(agora=10)}
    assert chaves == {
        ("BTC/USDT", "spot_futuro", "gate", "spot", "bitget", "futuro"),
        ("BTC/USDT", "spot_futuro", "gate", "spot", "mexc", "futuro"),
        ("BTC/USDT", "futuro_futuro", "bitget", "futuro", "mexc", "futuro"),
        ("BTC/USDT", "futuro_futuro", "mexc", "futuro", "bitget", "futuro"),
    }
    m.estado.marcar_desconectada("mexc")
    assert [c.chave for c in m.candidatos(agora=10)] == [CHAVE]


# ---------------------------------------------------------------------------
# __main__: ligacao das pecas, com corretoras falsas
# ---------------------------------------------------------------------------


class ExchangeFalsa:
    """Imita uma corretora do ccxt.pro: entrega um ticker e um livro por simbolo e depois espera para sempre."""

    def __init__(self, markets, tickers=None, livros=None, erro_ao_carregar=None):
        self.markets = {}  # vazio ate load_markets, como no ccxt
        self.has = {"watchTickers": True, "fetchFundingRates": True}
        self._markets = markets
        self._tickers = dict(tickers or {})
        self._livros = dict(livros or {})
        self._erro_ao_carregar = erro_ao_carregar
        self.tickers_pedidos = []
        self.livros_pedidos = []
        self.fechada = False

    async def load_markets(self, reload=False, params=None):
        await asyncio.sleep(0)
        if self._erro_ao_carregar is not None:
            raise self._erro_ao_carregar
        self.markets = self._markets
        return self.markets

    async def watch_tickers(self, symbols=None, params=None):
        self.tickers_pedidos.extend(symbols)
        prontos = {s: self._tickers.pop(s) for s in symbols if s in self._tickers}
        await asyncio.sleep(0)
        if prontos:
            return prontos
        await asyncio.sleep(3600)

    async def watch_order_book(self, symbol, limit=None, params=None):
        self.livros_pedidos.append(symbol)
        await asyncio.sleep(0)
        if symbol in self._livros:
            return self._livros.pop(symbol)
        await asyncio.sleep(3600)

    async def un_watch_order_book(self, symbol, params=None):
        await asyncio.sleep(0)

    async def fetch_funding_rates(self, symbols=None, params=None):
        await asyncio.sleep(0)
        return {}

    async def close(self):
        self.fechada = True


def test_livro_que_deixou_de_chegar_nao_gera_sinal_falso():
    # Repro da revisao: a etapa 2 largou o livro do spot da gate (congelado em t=10), mas o ticker
    # da gate segue chegando e mantem a conexao viva. Em t=25 o mercado subiu ~2%: o futuro da bitget
    # vende a 103,02 e o spot real custa 102 (A real ~1%). O livro congelado (ask 100) daria A = 3,02%
    # e um "novo" falso; o livro tem que sair do estado quando a assinatura acaba.
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=0.0)
    gate = ExchangeFalsa({"BTC/USDT": market_ccxt("BTC/USDT", "BTC")},
                         livros={"BTC/USDT": {"bids": [[99.9, 10.0]], "asks": [[100.0, 10.0]], "timestamp": None}})
    gate.markets = gate._markets
    coletor = ColetorCcxt("gate", gate, estado=m.estado, relogio=lambda: 10.0)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"})
        while len(gate.livros_pedidos) < 2:  # o 1o livro ja foi entregue e o coletor espera o proximo
            await asyncio.sleep(0)
        assert len(m.candidatos(agora=10)) == 1  # com o livro vivo, ha candidato
        await coletor.assinar_livros(set())  # a etapa 2 largou o par
        await coletor.fechar()

    asyncio.run(cenario())
    m.estado.atualizar_ticker("gate", "spot", "BTC", 101.9, 102.0, 25.0, simbolo_original="BTC/USDT", cotacao="USDT")
    m.estado.registrar_mensagem("gate", 25.0)
    colocar_ponta(m.estado, "bitget", "futuro", "BTC", 103.02, "short", 25.0)
    assert m.candidatos(agora=25) == []
    assert [e for e in m.ciclo(agora=25) if e.tipo == "novo"] == []


def exchanges_falsas(gate_erro=None) -> dict:
    """Gate com o spot de BTC (ask 100) e Bitget com o futuro de BTC (bid 101): A = 1%."""
    return {
        "gate": ExchangeFalsa(
            {"BTC/USDT": market_ccxt("BTC/USDT", "BTC")},
            tickers={"BTC/USDT": {"bid": 99.9, "ask": 100.0, "quoteVolume": 5e6, "percentage": 1.0}},
            livros={"BTC/USDT": {"bids": [[99.9, 10.0]], "asks": [[100.0, 10.0]], "timestamp": None}},
            erro_ao_carregar=gate_erro,
        ),
        "bitget": ExchangeFalsa(
            {"BTC/USDT:USDT": market_ccxt("BTC/USDT:USDT", "BTC")},
            tickers={"BTC/USDT:USDT": {"bid": 101.0, "ask": 101.1, "quoteVolume": 9e6, "percentage": 2.0}},
            livros={"BTC/USDT:USDT": {"bids": [[101.0, 10.0]], "asks": [[101.1, 10.0]], "timestamp": None}},
        ),
    }


def config_servico(tmp_path, **campos):
    # RP minimo bem negativo: o primeiro F medio e o F atual, entao o RP comeca negativo.
    base = dict(corretoras=["gate", "bitget"], banco=str(tmp_path / "s.db"), ciclo_ms=50, rp_min_lucro_pct=-5.0)
    base.update(campos)
    return cfg_com(**base)


def test_rodar_dois_ciclos_com_corretoras_falsas(tmp_path, caplog):
    exchanges = exchanges_falsas()
    config = config_servico(tmp_path)
    with caplog.at_level(logging.INFO, logger="scanner"):
        asyncio.run(asyncio.wait_for(
            principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=lambda: 1000.0, ciclos=2),
            timeout=5))

    # Cada watch_* e chamado de novo apos cada mensagem (como no ccxt): conta os simbolos distintos.
    assert set(exchanges["gate"].tickers_pedidos) == {"BTC/USDT"}
    assert set(exchanges["bitget"].tickers_pedidos) == {"BTC/USDT:USDT"}
    assert set(exchanges["gate"].livros_pedidos) == {"BTC/USDT"}  # etapa 2: A = 1% >= 0,3%
    assert set(exchanges["bitget"].livros_pedidos) == {"BTC/USDT:USDT"}
    assert all(ex.fechada for ex in exchanges.values())
    assert any("sinais ativos" in r.getMessage() for r in caplog.records)

    banco = Banco(config.banco)  # o banco foi fechado e gravado de verdade
    registros = banco.sinais_do_dia("1970-01-01")
    assert [r["par"] for r in registros] == ["BTC/USDT"]
    assert registros[0]["sinal"]["ponta_long"]["corretora"] == "gate"
    assert registros[0]["sinal"]["ponta_short"]["taxa_taker_pct"] == 0.06  # taxa da API (dados lentos)
    assert len(banco.amostras(chave_texto(CHAVE), desde=0)) == 1
    banco.fechar()


def test_rodar_futuro_x_futuro_com_corretoras_so_de_futuros(tmp_path):
    # Nenhuma das duas tem spot: sem o futuro x futuro elas ficariam sem tickers e sem livros.
    def so_futuro(bid, ask):
        return ExchangeFalsa(
            {"BTC/USDT:USDT": market_ccxt("BTC/USDT:USDT", "BTC")},
            tickers={"BTC/USDT:USDT": {"bid": bid, "ask": ask, "quoteVolume": 5e6, "percentage": 1.0}},
            livros={"BTC/USDT:USDT": {"bids": [[bid, 10.0]], "asks": [[ask, 10.0]], "timestamp": None}},
        )

    exchanges = {"gate": so_futuro(99.9, 100.0), "bitget": so_futuro(101.0, 101.1)}  # gate x bitget: A = 1%
    config = config_servico(tmp_path)
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=lambda: 1000.0, ciclos=2),
        timeout=5))

    assert set(exchanges["gate"].tickers_pedidos) == {"BTC/USDT:USDT"}
    assert set(exchanges["bitget"].tickers_pedidos) == {"BTC/USDT:USDT"}
    assert set(exchanges["gate"].livros_pedidos) == {"BTC/USDT:USDT"}
    assert set(exchanges["bitget"].livros_pedidos) == {"BTC/USDT:USDT"}
    banco = Banco(config.banco)
    registros = banco.sinais_do_dia("1970-01-01")
    # As duas direcoes viram sinal (RP minimo de -5% no config_servico).
    assert sorted((r["sinal"]["ponta_long"]["corretora"], r["sinal"]["ponta_short"]["corretora"])
                  for r in registros) == [("bitget", "gate"), ("gate", "bitget")]
    assert {r["sinal"]["tipo"] for r in registros} == {"futuro_futuro"}
    banco.fechar()


def test_rodar_cancelado_fecha_tudo(tmp_path):
    exchanges = exchanges_falsas()

    async def cenario():
        tarefa = asyncio.create_task(principal.rodar(
            config_servico(tmp_path), criar_exchange=exchanges.__getitem__, relogio=lambda: 1000.0))
        limite = asyncio.get_running_loop().time() + 5
        while not exchanges["bitget"].livros_pedidos:  # ja passou por pelo menos um ciclo
            assert asyncio.get_running_loop().time() < limite
            await asyncio.sleep(0.01)
        tarefa.cancel()  # o mesmo que o asyncio.run faz no Ctrl+C
        with pytest.raises(asyncio.CancelledError):
            await tarefa

    asyncio.run(cenario())
    assert all(ex.fechada for ex in exchanges.values())


def test_rodar_corretora_que_nao_carrega_fica_de_fora(tmp_path, caplog):
    exchanges = exchanges_falsas(gate_erro=OSError("sem rede"))
    with caplog.at_level(logging.ERROR, logger="scanner"):
        asyncio.run(asyncio.wait_for(principal.rodar(
            config_servico(tmp_path), criar_exchange=exchanges.__getitem__, relogio=lambda: 1000.0, ciclos=1),
            timeout=5))
    assert any("gate" in r.getMessage() and "sem rede" in r.getMessage() for r in caplog.records)
    assert exchanges["gate"].tickers_pedidos == [] and all(ex.fechada for ex in exchanges.values())


def test_rodar_sem_nenhuma_corretora_carregada_da_erro_e_fecha(tmp_path):
    exchanges = {"gate": ExchangeFalsa({}, erro_ao_carregar=OSError("sem rede"))}
    with pytest.raises(RuntimeError, match="nenhuma corretora"):
        asyncio.run(principal.rodar(config_servico(tmp_path, corretoras=["gate"]),
                                    criar_exchange=exchanges.__getitem__, ciclos=1))
    assert exchanges["gate"].fechada


def test_rodar_erro_no_ciclo_vai_para_o_log_e_continua(tmp_path, caplog, monkeypatch):
    exchanges = exchanges_falsas()
    chamadas = []

    def ciclo_com_erro(self, agora):
        chamadas.append(agora)
        raise RuntimeError("defeito")

    monkeypatch.setattr(Motor, "ciclo", ciclo_com_erro)
    with caplog.at_level(logging.ERROR, logger="scanner"):
        asyncio.run(asyncio.wait_for(principal.rodar(
            config_servico(tmp_path), criar_exchange=exchanges.__getitem__, relogio=lambda: 1000.0, ciclos=2),
            timeout=5))
    assert len(chamadas) == 2
    assert any("ciclo" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


def contar_ciclos(monkeypatch) -> list:
    """Troca Motor.ciclo por uma versao que anota cada chamada e depois roda o ciclo de verdade."""
    feitos = []
    original = Motor.ciclo

    def ciclo_contando(self, agora):
        feitos.append(agora)
        return original(self, agora)

    monkeypatch.setattr(Motor, "ciclo", ciclo_contando)
    return feitos


def test_laco_do_motor_segue_enquanto_o_ajuste_de_livros_trava(tmp_path, monkeypatch):
    # Ex.: un_watch sem confirmacao da corretora. O motor nao pode parar de rodar por causa disso.
    exchanges = exchanges_falsas()
    ciclos_feitos = contar_ciclos(monkeypatch)
    chamadas = []

    async def assinar_que_trava(self, simbolos, ao_atualizar=None):
        chamadas.append((self.nome, set(simbolos)))
        await asyncio.sleep(3600)

    monkeypatch.setattr(ColetorCcxt, "assinar_livros", assinar_que_trava)
    asyncio.run(asyncio.wait_for(principal.rodar(
        config_servico(tmp_path), criar_exchange=exchanges.__getitem__, relogio=lambda: 1000.0, ciclos=4),
        timeout=5))
    assert len(ciclos_feitos) == 4
    # So a primeira rodada de ajuste (uma chamada por corretora) comecou: as outras esperam ela acabar.
    assert sorted(nome for nome, _ in chamadas) == ["bitget", "gate"]
    assert all(ex.fechada for ex in exchanges.values())  # a rodada presa foi cancelada no fim


def test_ajuste_de_livros_roda_a_cada_ciclo_quando_termina_e_erro_vai_ao_log(tmp_path, monkeypatch, caplog):
    exchanges = exchanges_falsas()
    chamadas = []

    async def assinar_com_erro(self, simbolos, ao_atualizar=None):
        chamadas.append(self.nome)
        if self.nome == "gate":
            raise RuntimeError("defeito na gate")

    monkeypatch.setattr(ColetorCcxt, "assinar_livros", assinar_com_erro)
    with caplog.at_level(logging.ERROR, logger="scanner"):
        asyncio.run(asyncio.wait_for(principal.rodar(
            config_servico(tmp_path), criar_exchange=exchanges.__getitem__, relogio=lambda: 1000.0, ciclos=3),
            timeout=5))
    # Uma rodada por ciclo, mesmo com erro na anterior. A rodada criada no 3o (ultimo) ciclo e
    # cancelada no encerramento antes de comecar: ficam as dos ciclos 1 e 2.
    assert sorted(chamadas) == ["bitget"] * 2 + ["gate"] * 2
    assert any("defeito na gate" in r.getMessage() or "defeito na gate" in str(r.exc_info) for r in caplog.records)


def test_rodar_passa_o_timeout_de_desassinar_da_config(tmp_path, monkeypatch):
    exchanges = exchanges_falsas()
    recebidos = []
    original = ColetorCcxt.__init__

    def init_anotando(self, *args, **kwargs):
        recebidos.append(kwargs.get("timeout_desassinar_s"))
        original(self, *args, **kwargs)

    monkeypatch.setattr(ColetorCcxt, "__init__", init_anotando)
    asyncio.run(asyncio.wait_for(principal.rodar(
        config_servico(tmp_path, timeout_desassinar_s=1.5), criar_exchange=exchanges.__getitem__,
        relogio=lambda: 1000.0, ciclos=1), timeout=5))
    assert recebidos == [1.5, 1.5]


def escrever_config(tmp_path, texto="corretoras: [bitget]\n"):
    arq = tmp_path / "config.yaml"
    arq.write_text(texto)
    return str(arq)


def test_main_validar_so_confere_o_config(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(principal, "rodar", lambda *a, **k: pytest.fail("nao deveria subir o scanner"))
    assert principal.main(["--config", escrever_config(tmp_path), "--validar"]) == 0
    assert "config ok" in capsys.readouterr().out


def test_main_sobe_o_scanner_com_a_config_lida(tmp_path, monkeypatch):
    recebidas = []

    async def rodar_falso(config):
        recebidas.append(config)

    monkeypatch.setattr(principal, "rodar", rodar_falso)
    monkeypatch.setattr(principal, "configurar_log", lambda cfg: None)
    assert principal.main(["--config", escrever_config(tmp_path)]) == 0
    assert recebidas[0].corretoras == ["bitget"]


def test_main_ctrl_c_encerra_sem_erro(tmp_path, capsys, monkeypatch):
    async def rodar_falso(config):
        raise KeyboardInterrupt

    monkeypatch.setattr(principal, "rodar", rodar_falso)
    monkeypatch.setattr(principal, "configurar_log", lambda cfg: None)
    assert principal.main(["--config", escrever_config(tmp_path)]) == 0
    assert "Encerrado" in capsys.readouterr().out


def test_main_erro_ao_rodar_mostra_e_devolve_1(tmp_path, capsys, monkeypatch):
    async def rodar_falso(config):
        raise RuntimeError("nenhuma corretora carregou")

    monkeypatch.setattr(principal, "rodar", rodar_falso)
    monkeypatch.setattr(principal, "configurar_log", lambda cfg: None)
    assert principal.main(["--config", escrever_config(tmp_path)]) == 1
    assert "nenhuma corretora carregou" in capsys.readouterr().err


@pytest.mark.parametrize("nivel, esperado", [("normal", logging.INFO), ("detalhado", logging.DEBUG)])
def test_configurar_log_nivel_do_scanner(nivel, esperado, tmp_path):
    import scanner.logs

    logger, raiz = logging.getLogger("scanner"), logging.getLogger()
    antes = (logger.level, raiz.level)
    try:
        principal.configurar_log(cfg_com(nivel_log=nivel, pasta_logs=str(tmp_path)))  # o arquivo vai para a pasta do teste
        assert logger.level == esperado
    finally:
        scanner.logs.desligar_logs()
        logger.setLevel(antes[0])
        raiz.setLevel(antes[1])
