"""Testes dos filtros e dos modos Lucro e Farm (Task 7).

Nenhum teste acessa a internet. Cada filtro da especificacao (secao 4) tem pelo menos um
teste proprio: ligado, desligado e o valor exato do limite.
"""

import json
from dataclasses import fields

import pytest

from scanner.filtros import Filtros, custo_por_1000, passa
from scanner.modelos import Candidato, Ponta
from tests.fabricas import candidato, cfg, cfg_com, metricas


# ---------------------------------------------------------------------------
# Brief (Task 7, Passo 1) - verbatim
# ---------------------------------------------------------------------------

def test_lucro():
    assert passa(candidato(rp_liquido=0.40), Filtros(), "lucro", cfg)
    assert not passa(candidato(rp_liquido=0.15), Filtros(), "lucro", cfg)

def test_farm_exige_ponta_de_farm():
    cfg_farm = cfg_com(corretoras_farm=["lighter"])
    assert passa(candidato(rp_liquido=-0.09, short="lighter"), Filtros(), "farm", cfg_farm)
    assert not passa(candidato(rp_liquido=-0.09, short="gate"), Filtros(), "farm", cfg_farm)
    assert not passa(candidato(rp_liquido=-0.20, short="lighter"), Filtros(), "farm", cfg_farm)

def test_lote_minimo_obrigatorio():
    assert not passa(candidato(rp_liquido=0.40, lote_long=50), Filtros(), "lucro", cfg)

def test_suspeito_oculto_por_padrao():
    assert not passa(candidato(rp_liquido=0.40, suspeito=True), Filtros(), "lucro", cfg)
    assert passa(candidato(rp_liquido=0.40, suspeito=True), Filtros(mostrar_suspeitos=True), "lucro", cfg)

def test_lista_negra():
    assert not passa(candidato(rp_liquido=0.40, par="AKAM/USDT"), Filtros(lista_negra=["AKAM"]), "lucro", cfg)

def test_custo_por_1000():
    assert custo_por_1000(-0.09) == pytest.approx(0.45)


# ---------------------------------------------------------------------------
# Modelos: Ponta e Candidato seguem exatamente a secao 5 da especificacao
# ---------------------------------------------------------------------------

def test_ponta_tem_os_12_campos_da_spec():
    assert [f.name for f in fields(Ponta)] == [
        "corretora", "mercado", "simbolo_original", "preco", "lote_usd", "lote_medio_usd",
        "volume_24h_usd", "funding_pct", "funding_intervalo_h", "limite_ordem_usd",
        "variacao_24h_pct", "taxa_taker_pct",
    ]

def test_candidato_tem_os_campos_da_spec_sem_os_de_ciclo_de_vida():
    esperados = {
        "par", "tipo", "modo", "etiquetas", "suspeito", "aviso_cotacao", "ponta_long", "ponta_short",
        "spread_abertura_pct", "spread_fechamento_pct", "metricas_periodo", "fr_yield_8h_pct",
        "rp_bruto_pct", "custo_taxas_pct", "funding_esperado_pct", "rp_liquido_pct",
        "custo_por_1000_usd", "tamanho_referencia_usd",
    }
    assert {f.name for f in fields(Candidato)} == esperados

def test_chave_do_candidato():
    c = candidato(par="AKAM/USDT", long="bitget", short="mexc", mercado_long="spot")
    assert c.chave == ("AKAM/USDT", "spot_futuro", "bitget", "spot", "mexc", "futuro")

def test_ativo_do_candidato_e_a_parte_antes_da_barra():
    assert candidato(par="AKAM/USDT").ativo == "AKAM"
    assert candidato(par="PEPE/USDC").ativo == "PEPE"

def test_ponta_spot_nao_tem_funding():
    c = candidato(mercado_long="spot")
    assert c.ponta_long.funding_pct is None and c.ponta_long.funding_intervalo_h is None


# ---------------------------------------------------------------------------
# custo_por_1000
# ---------------------------------------------------------------------------

def test_custo_por_1000_zero_e_positivo_conforme_o_rp():
    assert custo_por_1000(0.0) == 0
    assert custo_por_1000(-0.10) == pytest.approx(0.50)
    assert custo_por_1000(0.40) == pytest.approx(-2.0)  # lucro: custo negativo


# ---------------------------------------------------------------------------
# Modo Lucro
# ---------------------------------------------------------------------------

def test_lucro_aceita_exatamente_o_minimo_da_config():
    assert passa(candidato(rp_liquido=0.2), Filtros(), "lucro", cfg)
    assert not passa(candidato(rp_liquido=0.1999), Filtros(), "lucro", cfg)

def test_lucro_usa_o_minimo_da_config_quando_o_filtro_esta_desligado():
    assert passa(candidato(rp_liquido=0.15), Filtros(), "lucro", cfg_com(rp_min_lucro_pct=0.1))
    assert not passa(candidato(rp_liquido=0.40), Filtros(), "lucro", cfg_com(rp_min_lucro_pct=0.5))

def test_rp_min_pct_do_filtro_vale_mais_que_a_config():
    assert not passa(candidato(rp_liquido=0.40), Filtros(rp_min_pct=0.5), "lucro", cfg)
    assert passa(candidato(rp_liquido=0.15), Filtros(rp_min_pct=0.1), "lucro", cfg)

def test_rp_min_pct_zero_e_um_valor_valido_e_nao_volta_para_a_config():
    # 0 e um minimo de verdade (aceita qualquer RP >= 0), nao "desligado".
    assert passa(candidato(rp_liquido=0.05), Filtros(rp_min_pct=0.0), "lucro", cfg)
    assert not passa(candidato(rp_liquido=-0.01), Filtros(rp_min_pct=0.0), "lucro", cfg)

def test_lucro_nao_exige_corretora_de_farm():
    assert passa(candidato(rp_liquido=0.40, short="gate"), Filtros(), "lucro", cfg_com(corretoras_farm=["lighter"]))

def test_modo_desconhecido_levanta_erro():
    with pytest.raises(ValueError, match="modo"):
        passa(candidato(), Filtros(), "Lucro", cfg)


# ---------------------------------------------------------------------------
# Modo Farm
# ---------------------------------------------------------------------------

CFG_FARM = cfg_com(corretoras_farm=["lighter"])

def test_farm_aceita_ponta_de_farm_no_long_ou_no_short():
    assert passa(candidato(rp_liquido=-0.05, long="lighter", short="gate"), Filtros(), "farm", CFG_FARM)
    assert passa(candidato(rp_liquido=-0.05, long="gate", short="lighter"), Filtros(), "farm", CFG_FARM)

def test_farm_aceita_exatamente_o_custo_maximo():
    assert passa(candidato(rp_liquido=-0.1, short="lighter"), Filtros(), "farm", CFG_FARM)
    assert not passa(candidato(rp_liquido=-0.1001, short="lighter"), Filtros(), "farm", CFG_FARM)

def test_farm_tambem_aceita_rp_positivo():
    assert passa(candidato(rp_liquido=0.40, short="lighter"), Filtros(), "farm", CFG_FARM)

def test_custo_max_farm_pct_do_filtro_vale_mais_que_a_config():
    c = candidato(rp_liquido=-0.20, short="lighter")
    assert not passa(c, Filtros(), "farm", CFG_FARM)
    assert passa(c, Filtros(custo_max_farm_pct=-0.3), "farm", CFG_FARM)
    assert not passa(candidato(rp_liquido=-0.05, short="lighter"), Filtros(custo_max_farm_pct=-0.01), "farm", CFG_FARM)

def test_custo_max_farm_pct_zero_e_um_valor_valido():
    assert not passa(candidato(rp_liquido=-0.01, short="lighter"), Filtros(custo_max_farm_pct=0.0), "farm", CFG_FARM)
    assert passa(candidato(rp_liquido=0.0, short="lighter"), Filtros(custo_max_farm_pct=0.0), "farm", CFG_FARM)

def test_farm_sem_corretoras_de_farm_na_config_nao_passa_nada():
    assert not passa(candidato(rp_liquido=0.40, short="lighter"), Filtros(), "farm", cfg)

def test_farm_ignora_rp_min_pct_do_modo_lucro():
    assert passa(candidato(rp_liquido=-0.05, short="lighter"), Filtros(rp_min_pct=5.0), "farm", CFG_FARM)

def test_farm_nome_da_corretora_nao_depende_de_maiusculas_na_config():
    assert passa(candidato(rp_liquido=-0.05, short="lighter"), Filtros(), "farm", cfg_com(corretoras_farm=["Lighter"]))


# ---------------------------------------------------------------------------
# Modo comparacao (RP bruto, como o Arbimax)
# ---------------------------------------------------------------------------

def test_comparacao_usa_o_rp_bruto_no_lucro():
    c = candidato(rp_liquido=-0.09, rp_bruto_pct=0.30)
    assert not passa(c, Filtros(), "lucro", cfg)  # liquido -0,09 nao chega no minimo
    assert passa(c, Filtros(), "lucro", cfg, comparacao=True)  # bruto 0,30 chega
    assert not passa(candidato(rp_liquido=0.40, rp_bruto_pct=0.15), Filtros(), "lucro", cfg, comparacao=True)

def test_comparacao_usa_o_rp_bruto_no_farm():
    c = candidato(rp_liquido=-0.50, rp_bruto_pct=-0.05, short="lighter")
    assert not passa(c, Filtros(), "farm", CFG_FARM)
    assert passa(c, Filtros(), "farm", CFG_FARM, comparacao=True)


# ---------------------------------------------------------------------------
# Lote minimo obrigatorio
# ---------------------------------------------------------------------------

def test_lote_minimo_e_o_tamanho_de_referencia_nas_duas_pontas():
    assert passa(candidato(lote_long=100, lote_short=100), Filtros(), "lucro", cfg)  # exatamente o minimo
    assert not passa(candidato(lote_long=99.99), Filtros(), "lucro", cfg)
    assert not passa(candidato(lote_short=99.99), Filtros(), "lucro", cfg)  # a ponta short tambem conta

def test_lote_minimo_acompanha_o_tamanho_de_referencia_da_config():
    assert passa(candidato(lote_long=300, lote_short=300), Filtros(), "lucro", cfg_com(tamanho_referencia_usd=300))
    assert not passa(candidato(lote_long=299), Filtros(), "lucro", cfg_com(tamanho_referencia_usd=300))

def test_lote_min_usd_sobe_o_minimo():
    assert not passa(candidato(lote_long=500), Filtros(lote_min_usd=600), "lucro", cfg)
    assert not passa(candidato(lote_short=500), Filtros(lote_min_usd=600), "lucro", cfg)
    assert passa(candidato(lote_long=600, lote_short=600), Filtros(lote_min_usd=600), "lucro", cfg)

def test_lote_min_usd_menor_que_a_referencia_nao_baixa_o_minimo():
    assert not passa(candidato(lote_long=50), Filtros(lote_min_usd=10), "lucro", cfg)
    assert passa(candidato(lote_long=150), Filtros(lote_min_usd=10), "lucro", cfg)


# ---------------------------------------------------------------------------
# Suspeitos
# ---------------------------------------------------------------------------

def test_suspeito_visivel_tambem_no_farm_quando_ligado():
    c = candidato(rp_liquido=-0.05, short="lighter", suspeito=True)
    assert not passa(c, Filtros(), "farm", CFG_FARM)
    assert passa(c, Filtros(mostrar_suspeitos=True), "farm", CFG_FARM)


# ---------------------------------------------------------------------------
# Corretoras do lado 1 (ponta long) e do lado 2 (ponta short)
# ---------------------------------------------------------------------------

def test_corretoras_lado1_filtra_a_ponta_long():
    c = candidato(long="bitget", short="gate")
    assert passa(c, Filtros(corretoras_lado1=["bitget"]), "lucro", cfg)
    assert passa(c, Filtros(corretoras_lado1=["mexc", "bitget"]), "lucro", cfg)
    assert not passa(c, Filtros(corretoras_lado1=["gate"]), "lucro", cfg)  # gate e o short, nao o long

def test_corretoras_lado2_filtra_a_ponta_short():
    c = candidato(long="bitget", short="gate")
    assert passa(c, Filtros(corretoras_lado2=["gate"]), "lucro", cfg)
    assert not passa(c, Filtros(corretoras_lado2=["bitget"]), "lucro", cfg)  # bitget e o long

def test_corretoras_com_mercado():
    spot_futuro = candidato(long="bitget", short="gate", mercado_long="spot")
    assert passa(spot_futuro, Filtros(corretoras_lado1=["bitget:spot"]), "lucro", cfg)
    assert not passa(spot_futuro, Filtros(corretoras_lado1=["bitget:futuro"]), "lucro", cfg)
    assert passa(spot_futuro, Filtros(corretoras_lado2=["gate:futuro"]), "lucro", cfg)
    assert not passa(spot_futuro, Filtros(corretoras_lado2=["gate:spot"]), "lucro", cfg)

def test_corretora_sem_mercado_vale_para_qualquer_mercado():
    spot_futuro = candidato(long="bitget", short="gate", mercado_long="spot")
    futuro_futuro = candidato(long="bitget", short="gate")
    assert passa(spot_futuro, Filtros(corretoras_lado1=["bitget"]), "lucro", cfg)
    assert passa(futuro_futuro, Filtros(corretoras_lado1=["bitget"]), "lucro", cfg)

def test_corretoras_sem_diferenciar_maiusculas():
    c = candidato(long="bitget", short="gate", mercado_long="spot")
    assert passa(c, Filtros(corretoras_lado1=["Bitget:SPOT"], corretoras_lado2=["GATE"]), "lucro", cfg)

def test_corretoras_lado1_e_lado2_juntos_exigem_os_dois():
    c = candidato(long="bitget", short="gate")
    assert passa(c, Filtros(corretoras_lado1=["bitget"], corretoras_lado2=["gate"]), "lucro", cfg)
    assert not passa(c, Filtros(corretoras_lado1=["bitget"], corretoras_lado2=["mexc"]), "lucro", cfg)

def test_corretoras_vazia_ou_none_e_filtro_desligado():
    c = candidato()
    assert passa(c, Filtros(corretoras_lado1=[], corretoras_lado2=[]), "lucro", cfg)
    assert passa(c, Filtros(corretoras_lado1=None, corretoras_lado2=None), "lucro", cfg)


# ---------------------------------------------------------------------------
# Tipos de oportunidade
# ---------------------------------------------------------------------------

def test_tipos():
    ff = candidato()
    sf = candidato(mercado_long="spot")
    assert passa(ff, Filtros(tipos=["futuro_futuro"]), "lucro", cfg)
    assert not passa(sf, Filtros(tipos=["futuro_futuro"]), "lucro", cfg)
    assert passa(sf, Filtros(tipos=["spot_futuro"]), "lucro", cfg)
    assert passa(sf, Filtros(tipos=["spot_futuro", "futuro_futuro"]), "lucro", cfg)
    assert passa(sf, Filtros(tipos=[]), "lucro", cfg)  # vazio = desligado


# ---------------------------------------------------------------------------
# Volume minimo 24h (nas duas pontas)
# ---------------------------------------------------------------------------

def test_volume_min_usd_vale_para_as_duas_pontas():
    f = Filtros(volume_min_usd=50_000)
    assert passa(candidato(campos_long={"volume_24h_usd": 50_000}, campos_short={"volume_24h_usd": 90_000}), f, "lucro", cfg)
    assert not passa(candidato(campos_long={"volume_24h_usd": 49_999}), f, "lucro", cfg)
    assert not passa(candidato(campos_short={"volume_24h_usd": 49_999}), f, "lucro", cfg)

def test_volume_desconhecido_reprova_quando_o_filtro_esta_ligado():
    c = candidato(campos_short={"volume_24h_usd": None})
    assert not passa(c, Filtros(volume_min_usd=1), "lucro", cfg)
    assert passa(c, Filtros(), "lucro", cfg)  # desligado: nao importa


# ---------------------------------------------------------------------------
# Spread minimo (A atual)
# ---------------------------------------------------------------------------

def test_spread_min_pct_usa_o_spread_de_abertura_atual():
    f = Filtros(spread_min_pct=1.0)
    assert passa(candidato(spread_abertura_pct=1.0), f, "lucro", cfg)
    assert not passa(candidato(spread_abertura_pct=0.99), f, "lucro", cfg)
    assert passa(candidato(spread_abertura_pct=0.1), Filtros(), "lucro", cfg)


# ---------------------------------------------------------------------------
# Lote medio minimo (no periodo, nas duas pontas)
# ---------------------------------------------------------------------------

def test_lote_medio_min_usd_vale_para_as_duas_pontas():
    f = Filtros(lote_medio_min_usd=800)
    assert passa(candidato(campos_long={"lote_medio_usd": 800}, campos_short={"lote_medio_usd": 900}), f, "lucro", cfg)
    assert not passa(candidato(campos_long={"lote_medio_usd": 799}), f, "lucro", cfg)
    assert not passa(candidato(campos_short={"lote_medio_usd": 799}), f, "lucro", cfg)

def test_lote_medio_desconhecido_reprova_quando_o_filtro_esta_ligado():
    c = candidato(campos_long={"lote_medio_usd": None})
    assert not passa(c, Filtros(lote_medio_min_usd=1), "lucro", cfg)
    assert passa(c, Filtros(), "lucro", cfg)


# ---------------------------------------------------------------------------
# Limite de ordem minimo
# ---------------------------------------------------------------------------

def test_limite_ordem_min_usd_vale_para_as_duas_pontas():
    f = Filtros(limite_ordem_min_usd=10_000)
    assert passa(candidato(campos_long={"limite_ordem_usd": 10_000}, campos_short={"limite_ordem_usd": 20_000}), f, "lucro", cfg)
    assert not passa(candidato(campos_long={"limite_ordem_usd": 9_999}), f, "lucro", cfg)
    assert not passa(candidato(campos_short={"limite_ordem_usd": 9_999}), f, "lucro", cfg)

def test_limite_ordem_desconhecido_conta_como_sem_limite():
    c = candidato(campos_long={"limite_ordem_usd": None}, campos_short={"limite_ordem_usd": None})
    assert passa(c, Filtros(limite_ordem_min_usd=1_000_000_000), "lucro", cfg)


# ---------------------------------------------------------------------------
# Spreads do periodo (metricas_periodo)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filtro, campo_da_metrica, valor",
    [
        ("maior_abertura_min_pct", "maior_abertura", 2.0),
        ("maior_fechamento_min_pct", "maior_fechamento", -0.5),
        ("abertura_media_min_pct", "abertura_media", 1.5),
        ("fechamento_medio_min_pct", "fechamento_medio", -0.3),
    ],
)
def test_filtros_de_spread_do_periodo(filtro, campo_da_metrica, valor):
    exato = candidato(metricas_periodo=metricas(**{campo_da_metrica: valor}))
    abaixo = candidato(metricas_periodo=metricas(**{campo_da_metrica: valor - 0.01}))
    assert passa(exato, Filtros(**{filtro: valor}), "lucro", cfg)  # no limite exato passa
    assert not passa(abaixo, Filtros(**{filtro: valor}), "lucro", cfg)
    assert passa(abaixo, Filtros(), "lucro", cfg)  # desligado

def test_invertidas_min():
    f = Filtros(invertidas_min=3)
    assert passa(candidato(metricas_periodo=metricas(invertidas=3)), f, "lucro", cfg)
    assert not passa(candidato(metricas_periodo=metricas(invertidas=2)), f, "lucro", cfg)
    assert passa(candidato(metricas_periodo=metricas(invertidas=0)), Filtros(invertidas_min=0), "lucro", cfg)

def test_filtro_de_zero_nas_metricas_esta_ligado():
    # 0 e um limite de verdade: fechamento medio minimo 0 reprova F negativo.
    c = candidato(metricas_periodo=metricas(fechamento_medio=-0.1))
    assert not passa(c, Filtros(fechamento_medio_min_pct=0.0), "lucro", cfg)

@pytest.mark.parametrize(
    "filtro, valor",
    [
        ("maior_abertura_min_pct", -100.0),
        ("maior_fechamento_min_pct", -100.0),
        ("abertura_media_min_pct", -100.0),
        ("fechamento_medio_min_pct", -100.0),
        ("invertidas_min", 0),
    ],
)
def test_sem_metricas_reprova_quando_algum_filtro_do_periodo_esta_ligado(filtro, valor):
    sem_metricas = candidato(metricas_periodo=None)
    assert not passa(sem_metricas, Filtros(**{filtro: valor}), "lucro", cfg)
    assert passa(sem_metricas, Filtros(), "lucro", cfg)  # nenhum filtro do periodo ligado: passa


# ---------------------------------------------------------------------------
# Funding minimo e maximo por ponta
# ---------------------------------------------------------------------------

def test_funding_min_pct_vale_para_cada_ponta_futura():
    f = Filtros(funding_min_pct=-0.02)
    ok = candidato(campos_long={"funding_pct": -0.02}, campos_short={"funding_pct": 0.05})
    assert passa(ok, f, "lucro", cfg)
    assert not passa(candidato(campos_long={"funding_pct": -0.0201}), f, "lucro", cfg)
    assert not passa(candidato(campos_short={"funding_pct": -0.0201}), f, "lucro", cfg)

def test_funding_max_pct_vale_para_cada_ponta_futura():
    f = Filtros(funding_max_pct=0.05)
    assert passa(candidato(campos_long={"funding_pct": 0.05}, campos_short={"funding_pct": 0.0}), f, "lucro", cfg)
    assert not passa(candidato(campos_long={"funding_pct": 0.0501}), f, "lucro", cfg)
    assert not passa(candidato(campos_short={"funding_pct": 0.0501}), f, "lucro", cfg)

def test_funding_min_e_max_juntos_formam_uma_faixa():
    f = Filtros(funding_min_pct=-0.01, funding_max_pct=0.01)
    assert passa(candidato(campos_long={"funding_pct": 0.0}, campos_short={"funding_pct": 0.01}), f, "lucro", cfg)
    assert not passa(candidato(campos_short={"funding_pct": 0.02}), f, "lucro", cfg)
    assert not passa(candidato(campos_long={"funding_pct": -0.02}), f, "lucro", cfg)

def test_funding_ignora_a_ponta_spot():
    spot_futuro = candidato(mercado_long="spot", campos_short={"funding_pct": 0.01})
    assert spot_futuro.ponta_long.funding_pct is None
    assert passa(spot_futuro, Filtros(funding_min_pct=0.0, funding_max_pct=0.02), "lucro", cfg)
    assert not passa(spot_futuro, Filtros(funding_max_pct=0.005), "lucro", cfg)  # o futuro ainda conta

def test_funding_desconhecido_na_ponta_futura_reprova_quando_o_filtro_esta_ligado():
    c = candidato(campos_short={"funding_pct": None})
    assert not passa(c, Filtros(funding_min_pct=-1.0), "lucro", cfg)
    assert not passa(c, Filtros(funding_max_pct=1.0), "lucro", cfg)
    assert passa(c, Filtros(), "lucro", cfg)


# ---------------------------------------------------------------------------
# FR Yield minimo
# ---------------------------------------------------------------------------

def test_fr_yield_min_pct():
    f = Filtros(fr_yield_min_pct=0.05)
    assert passa(candidato(fr_yield_8h_pct=0.05), f, "lucro", cfg)
    assert not passa(candidato(fr_yield_8h_pct=0.0499), f, "lucro", cfg)
    assert passa(candidato(fr_yield_8h_pct=-1.0), Filtros(), "lucro", cfg)


# ---------------------------------------------------------------------------
# Intervalo de cobranca do funding
# ---------------------------------------------------------------------------

def test_intervalos_funding_todas_as_pontas_futuras_precisam_estar_na_lista():
    f = Filtros(intervalos_funding=[4, 8])
    assert passa(candidato(campos_long={"funding_intervalo_h": 4}, campos_short={"funding_intervalo_h": 8}), f, "lucro", cfg)
    assert not passa(candidato(campos_long={"funding_intervalo_h": 1}), f, "lucro", cfg)
    assert not passa(candidato(campos_short={"funding_intervalo_h": 1}), f, "lucro", cfg)

def test_intervalos_funding_ignora_a_ponta_spot():
    spot_futuro = candidato(mercado_long="spot", campos_short={"funding_intervalo_h": 8})
    assert passa(spot_futuro, Filtros(intervalos_funding=[8]), "lucro", cfg)
    assert not passa(spot_futuro, Filtros(intervalos_funding=[1]), "lucro", cfg)

def test_intervalos_funding_desconhecido_reprova_quando_ligado():
    c = candidato(campos_short={"funding_intervalo_h": None})
    assert not passa(c, Filtros(intervalos_funding=[1, 4, 8]), "lucro", cfg)
    assert passa(c, Filtros(intervalos_funding=[]), "lucro", cfg)  # vazio = desligado


# ---------------------------------------------------------------------------
# Funding sincronizado (aproximado por intervalos iguais)
# ---------------------------------------------------------------------------

def test_funding_sincronizado_exige_o_mesmo_intervalo_nas_pontas_futuras():
    f = Filtros(funding_sincronizado=True)
    assert passa(candidato(campos_long={"funding_intervalo_h": 4}, campos_short={"funding_intervalo_h": 4}), f, "lucro", cfg)
    assert not passa(candidato(campos_long={"funding_intervalo_h": 4}, campos_short={"funding_intervalo_h": 8}), f, "lucro", cfg)
    assert passa(candidato(campos_long={"funding_intervalo_h": 4}, campos_short={"funding_intervalo_h": 8}), Filtros(), "lucro", cfg)

def test_funding_sincronizado_com_uma_ponta_spot_passa():
    # So ha uma ponta que cobra funding: nao ha com quem dessincronizar.
    assert passa(candidato(mercado_long="spot"), Filtros(funding_sincronizado=True), "lucro", cfg)

def test_funding_sincronizado_com_intervalo_desconhecido_reprova():
    c = candidato(campos_long={"funding_intervalo_h": None}, campos_short={"funding_intervalo_h": None})
    assert not passa(c, Filtros(funding_sincronizado=True), "lucro", cfg)
    assert not passa(candidato(campos_long={"funding_intervalo_h": None}), Filtros(funding_sincronizado=True), "lucro", cfg)


# ---------------------------------------------------------------------------
# Lista negra, nunca mostrar, apenas mostrar, sempre mostrar (por moeda)
# ---------------------------------------------------------------------------

def test_lista_negra_nao_diferencia_maiusculas_e_so_exclui_a_moeda_da_lista():
    assert not passa(candidato(par="AKAM/USDT"), Filtros(lista_negra=["akam"]), "lucro", cfg)
    assert passa(candidato(par="BTC/USDT"), Filtros(lista_negra=["AKAM"]), "lucro", cfg)
    assert passa(candidato(par="AKAM/USDT"), Filtros(lista_negra=[]), "lucro", cfg)

def test_lista_negra_compara_a_moeda_inteira_e_nao_um_pedaco():
    assert passa(candidato(par="AKAMAI/USDT"), Filtros(lista_negra=["AKAM"]), "lucro", cfg)

def test_nunca_mostrar():
    assert not passa(candidato(par="PEPE/USDT"), Filtros(nunca_mostrar=["pepe"]), "lucro", cfg)
    assert passa(candidato(par="BTC/USDT"), Filtros(nunca_mostrar=["PEPE"]), "lucro", cfg)

def test_apenas_mostrar():
    f = Filtros(apenas_mostrar=["btc", "ETH"])
    assert passa(candidato(par="BTC/USDT"), f, "lucro", cfg)
    assert passa(candidato(par="ETH/USDT"), f, "lucro", cfg)
    assert not passa(candidato(par="SOL/USDT"), f, "lucro", cfg)
    assert passa(candidato(par="SOL/USDT"), Filtros(apenas_mostrar=[]), "lucro", cfg)  # vazio = desligado

def test_apenas_mostrar_continua_exigindo_o_resto():
    assert not passa(candidato(rp_liquido=0.01, par="BTC/USDT"), Filtros(apenas_mostrar=["BTC"]), "lucro", cfg)

def test_sempre_mostrar_ignora_filtros_e_regra_do_modo():
    f = Filtros(
        sempre_mostrar=["btc"], rp_min_pct=5.0, volume_min_usd=1e12, spread_min_pct=50,
        corretoras_lado1=["mexc"], tipos=["spot_futuro"], funding_max_pct=-5, invertidas_min=9,
        apenas_mostrar=["ETH"], nunca_mostrar=["BTC"],
    )
    assert passa(candidato(rp_liquido=-3.0, par="BTC/USDT"), f, "lucro", cfg)
    assert passa(candidato(rp_liquido=-3.0, par="BTC/USDT"), f, "farm", cfg)  # nem a regra do Farm
    assert not passa(candidato(rp_liquido=-3.0, par="SOL/USDT"), f, "lucro", cfg)  # outras moedas seguem as regras

def test_sempre_mostrar_vale_tambem_contra_a_lista_negra():
    f = Filtros(sempre_mostrar=["AKAM"], lista_negra=["AKAM"])
    assert passa(candidato(par="AKAM/USDT"), f, "lucro", cfg)

def test_sempre_mostrar_nao_ignora_o_lote_minimo_obrigatorio():
    f = Filtros(sempre_mostrar=["BTC"])
    assert not passa(candidato(par="BTC/USDT", lote_long=50), f, "lucro", cfg)
    assert not passa(candidato(par="BTC/USDT", lote_short=50), f, "lucro", cfg)

def test_sempre_mostrar_respeita_o_lote_min_usd_do_filtro():
    f = Filtros(sempre_mostrar=["BTC"], lote_min_usd=600)
    assert not passa(candidato(par="BTC/USDT", lote_long=500), f, "lucro", cfg)

def test_sempre_mostrar_nao_mostra_suspeito():
    # Suspeito e uma regra a parte (condicao 4 da spec): so a chave mostrar_suspeitos libera.
    c = candidato(par="BTC/USDT", suspeito=True)
    assert not passa(c, Filtros(sempre_mostrar=["BTC"]), "lucro", cfg)
    assert passa(c, Filtros(sempre_mostrar=["BTC"], mostrar_suspeitos=True), "lucro", cfg)


# ---------------------------------------------------------------------------
# Campos guardados no grupo mas usados so pelo motor e pelo painel
# ---------------------------------------------------------------------------

def test_campos_de_motor_e_painel_nao_mudam_o_resultado_de_passa():
    f = Filtros(
        periodo_metricas_h=1, invertidas_duracao_min_s=30, metrica_spread="maior",
        ordenar_por="volume", fixados=["BTC/USDT"],
    )
    assert passa(candidato(rp_liquido=0.40), f, "lucro", cfg)
    assert not passa(candidato(rp_liquido=0.15), f, "lucro", cfg)


# ---------------------------------------------------------------------------
# Todos os filtros ligados ao mesmo tempo
# ---------------------------------------------------------------------------

def test_todos_os_filtros_juntos_e_cada_variacao_quebrando_um_so():
    # BTC, ETH e AKAM em "apenas mostrar": assim ETH (nunca mostrar) e AKAM (lista negra) quebram so a lista deles.
    f = Filtros(
        corretoras_lado1=["bitget"], corretoras_lado2=["gate"], tipos=["futuro_futuro"],
        volume_min_usd=1000, spread_min_pct=0.1, lote_min_usd=500, lote_medio_min_usd=500,
        limite_ordem_min_usd=1000, maior_abertura_min_pct=0.1, maior_fechamento_min_pct=-1,
        abertura_media_min_pct=0.1, fechamento_medio_min_pct=-1, invertidas_min=0,
        funding_min_pct=-1, funding_max_pct=1, fr_yield_min_pct=0, intervalos_funding=[8],
        funding_sincronizado=True, rp_min_pct=0.3, apenas_mostrar=["BTC", "ETH", "AKAM"], nunca_mostrar=["ETH"],
        lista_negra=["AKAM"],
    )
    assert passa(candidato(), f, "lucro", cfg)
    # Cada variacao abaixo quebra exatamente um filtro e precisa reprovar.
    assert not passa(candidato(long="mexc"), f, "lucro", cfg)  # corretoras do lado 1
    assert not passa(candidato(short="mexc"), f, "lucro", cfg)  # corretoras do lado 2
    assert not passa(candidato(mercado_long="spot"), f, "lucro", cfg)  # tipos
    assert not passa(candidato(campos_long={"volume_24h_usd": 10}), f, "lucro", cfg)  # volume
    assert not passa(candidato(spread_abertura_pct=0.05), f, "lucro", cfg)  # spread minimo
    assert not passa(candidato(lote_long=400), f, "lucro", cfg)  # lote minimo
    assert not passa(candidato(campos_long={"lote_medio_usd": 400}), f, "lucro", cfg)  # lote medio
    assert not passa(candidato(campos_long={"limite_ordem_usd": 500}), f, "lucro", cfg)  # limite de ordem
    assert not passa(candidato(metricas_periodo=metricas(maior_abertura=0.05)), f, "lucro", cfg)  # maior A
    assert not passa(candidato(campos_long={"funding_pct": 2.0}), f, "lucro", cfg)  # funding maximo
    assert not passa(candidato(fr_yield_8h_pct=-0.01), f, "lucro", cfg)  # FR Yield
    # As duas pontas em 4h: quebra so o intervalo (continuam sincronizadas).
    assert not passa(candidato(campos_long={"funding_intervalo_h": 4}, campos_short={"funding_intervalo_h": 4}),
                     f, "lucro", cfg)
    assert not passa(candidato(campos_long={"funding_intervalo_h": 4}),
                     Filtros(funding_sincronizado=True), "lucro", cfg)  # so a sincronizacao
    assert not passa(candidato(rp_liquido=0.25), f, "lucro", cfg)  # RP minimo
    assert not passa(candidato(par="ETH/USDT"), f, "lucro", cfg)  # nunca mostrar
    assert not passa(candidato(par="AKAM/USDT"), f, "lucro", cfg)  # lista negra
    assert not passa(candidato(par="SOL/USDT"), f, "lucro", cfg)  # apenas mostrar
    # Sem metricas do periodo (combinacao que acabou de aparecer), os filtros do periodo reprovam juntos.
    assert not passa(candidato(metricas_periodo=None), f, "lucro", cfg)


# ---------------------------------------------------------------------------
# Filtros.para_dict / Filtros.de_dict (grupos salvos)
# ---------------------------------------------------------------------------

def test_filtros_padrao_estao_todos_desligados():
    f = Filtros()
    assert f.mostrar_suspeitos is False and f.funding_sincronizado is False
    for campo in fields(Filtros):
        if campo.name not in ("mostrar_suspeitos", "funding_sincronizado"):
            assert getattr(f, campo.name) in (None, []), campo.name

def test_listas_dos_filtros_nao_sao_compartilhadas_entre_instancias():
    a, b = Filtros(), Filtros()
    a.lista_negra.append("AKAM")
    assert b.lista_negra == []

def test_para_dict_e_de_dict_ida_e_volta():
    f = Filtros(
        corretoras_lado1=["bitget:spot"], tipos=["spot_futuro"], volume_min_usd=5000, lote_min_usd=200,
        invertidas_min=2, funding_max_pct=0.05, intervalos_funding=[1, 8], funding_sincronizado=True,
        rp_min_pct=0.0, mostrar_suspeitos=True, lista_negra=["AKAM"], sempre_mostrar=["BTC"],
        periodo_metricas_h=4, invertidas_duracao_min_s=30, metrica_spread="maior",
        ordenar_por="resultado", fixados=["BTC/USDT"],
    )
    d = f.para_dict()
    assert d["volume_min_usd"] == 5000 and d["lista_negra"] == ["AKAM"]
    assert Filtros.de_dict(d) == f
    assert Filtros.de_dict(Filtros().para_dict()) == Filtros()

def test_para_dict_tem_um_item_por_campo_e_so_tipos_de_json():
    d = Filtros().para_dict()
    assert set(d) == {f.name for f in fields(Filtros)}
    assert json.loads(json.dumps(d)) == d

def test_de_dict_parcial_usa_o_padrao_nos_campos_que_faltam():
    f = Filtros.de_dict({"volume_min_usd": 500})
    assert f.volume_min_usd == 500 and f.spread_min_pct is None and f.lista_negra == []
    assert Filtros.de_dict({}) == Filtros()

def test_de_dict_chave_desconhecida_levanta_erro_com_o_nome_dela():
    with pytest.raises(ValueError, match="volume_minimo"):
        Filtros.de_dict({"volume_minimo": 500})
    with pytest.raises(ValueError) as erro:
        Filtros.de_dict({"volume_min_usd": 5, "foo": 1, "bar": 2})
    assert "foo" in str(erro.value) and "bar" in str(erro.value)

def test_de_dict_aceita_int_nos_campos_numericos_e_none_nas_listas():
    f = Filtros.de_dict({"volume_min_usd": 500, "spread_min_pct": 1, "lista_negra": None, "tipos": None})
    assert f.volume_min_usd == 500 and f.lista_negra == [] and f.tipos == []

def test_de_dict_copia_as_listas():
    d = {"lista_negra": ["AKAM"]}
    f = Filtros.de_dict(d)
    f.lista_negra.append("BTC")
    assert d["lista_negra"] == ["AKAM"]

@pytest.mark.parametrize(
    "dados, campo",
    [
        ({"volume_min_usd": "muito"}, "volume_min_usd"),
        ({"volume_min_usd": True}, "volume_min_usd"),
        ({"lista_negra": "AKAM"}, "lista_negra"),
        ({"lista_negra": [1, 2]}, "lista_negra"),
        ({"corretoras_lado1": "bitget"}, "corretoras_lado1"),
        ({"intervalos_funding": ["8h"]}, "intervalos_funding"),
        ({"mostrar_suspeitos": "sim"}, "mostrar_suspeitos"),
        ({"funding_sincronizado": 1}, "funding_sincronizado"),
    ],
)
def test_de_dict_valor_com_tipo_errado_levanta_erro_com_o_nome_do_campo(dados, campo):
    with pytest.raises(ValueError, match=campo):
        Filtros.de_dict(dados)

def test_de_dict_que_nao_e_dict_levanta_erro():
    with pytest.raises(ValueError):
        Filtros.de_dict([("volume_min_usd", 5)])
