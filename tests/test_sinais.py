"""Testes do gerenciador de sinais (Task 8).

Nenhum teste acessa a internet. Os 4 primeiros testes sao os do brief (verbatim); os demais
cobrem o id, o JSON campo a campo, -0.0/NaN, o formato das datas e a ordem dos eventos.
"""

import json
import math
import re
from datetime import datetime, timezone

import pytest

from scanner.sinais import Evento, GerenciadorSinais
from tests.fabricas import candidato, metricas

# ---------------------------------------------------------------------------
# Brief (Task 8, Passo 1) - verbatim
# ---------------------------------------------------------------------------

CAMPOS = {"id", "par", "tipo", "modo", "etiquetas", "suspeito", "aviso_cotacao", "ponta_long", "ponta_short",
          "spread_abertura_pct", "spread_fechamento_pct", "metricas_periodo", "fr_yield_8h_pct", "rp_bruto_pct",
          "custo_taxas_pct", "funding_esperado_pct", "rp_liquido_pct", "custo_por_1000_usd",
          "tamanho_referencia_usd", "detectado_em", "atualizado_em", "encerrado_em"}

def test_ciclo_de_vida():
    g = GerenciadorSinais(anti_repeticao_s=60); c = candidato(rp_liquido=0.40)
    assert [e.tipo for e in g.processar([c], agora=0)] == ["novo"]
    assert [e.tipo for e in g.processar([c], agora=1)] == ["atualizado"]
    ev = g.processar([], agora=2)
    assert ev[0].tipo == "encerrado" and ev[0].sinal["encerrado_em"] is not None

def test_par_removido_encerra():  # Foco de revisão 5
    g = GerenciadorSinais(60); g.processar([candidato(par="X/USDT")], agora=0)
    assert g.processar([], agora=1)[0].tipo == "encerrado"

def test_anti_repeticao_no_historico():
    g = GerenciadorSinais(60); c = candidato()
    g.processar([c], 0); g.processar([], 1)
    assert g.processar([c], 30)[0].registrar_historico is False
    g.processar([], 31)
    assert g.processar([c], 92)[0].registrar_historico is True

def test_formato_json():
    s = GerenciadorSinais(60).processar([candidato()], agora=0)[0].sinal
    assert set(s) == CAMPOS
    assert s["detectado_em"].endswith("Z")


# ---------------------------------------------------------------------------
# Ciclo de vida: o que cada evento carrega
# ---------------------------------------------------------------------------

def test_novo_traz_registrar_historico_e_datas_iguais():
    g = GerenciadorSinais(60)
    (ev,) = g.processar([candidato()], agora=10)
    assert isinstance(ev, Evento)
    assert ev.tipo == "novo" and ev.registrar_historico is True
    assert ev.sinal["detectado_em"] == ev.sinal["atualizado_em"] == "1970-01-01T00:00:10.000Z"
    assert ev.sinal["encerrado_em"] is None


def test_atualizado_atualiza_valores_e_mantem_id_e_deteccao():
    g = GerenciadorSinais(60)
    (novo,) = g.processar([candidato(rp_liquido=0.40)], agora=10)
    (atu,) = g.processar([candidato(rp_liquido=0.55)], agora=12.5)  # mesma chave, valores novos
    assert atu.tipo == "atualizado" and atu.registrar_historico is False
    assert atu.sinal["rp_liquido_pct"] == 0.55  # valor atualizado
    assert atu.sinal["id"] == novo.sinal["id"]
    assert atu.sinal["detectado_em"] == novo.sinal["detectado_em"]
    assert atu.sinal["atualizado_em"] == "1970-01-01T00:00:12.500Z"
    assert atu.sinal["encerrado_em"] is None


def test_encerrado_preenche_encerrado_em_e_pede_historico():
    g = GerenciadorSinais(60)
    g.processar([candidato(rp_liquido=0.40)], agora=10)
    g.processar([candidato(rp_liquido=0.55)], agora=11)
    (enc,) = g.processar([], agora=15)
    assert enc.tipo == "encerrado" and enc.registrar_historico is True
    assert enc.sinal["encerrado_em"] == "1970-01-01T00:00:15.000Z"
    assert enc.sinal["detectado_em"] == "1970-01-01T00:00:10.000Z"
    assert enc.sinal["atualizado_em"] == "1970-01-01T00:00:11.000Z"  # a ultima vez que foi visto
    assert enc.sinal["rp_liquido_pct"] == 0.55  # ultimos valores conhecidos


def test_sinal_encerrado_so_gera_um_evento():
    g = GerenciadorSinais(60)
    g.processar([candidato()], agora=0)
    assert [e.tipo for e in g.processar([], agora=1)] == ["encerrado"]
    assert g.processar([], agora=2) == []


def test_sem_candidatos_e_sem_ativos_nao_gera_eventos():
    assert GerenciadorSinais(60).processar([], agora=0) == []


# ---------------------------------------------------------------------------
# ativos()
# ---------------------------------------------------------------------------

def test_ativos_acompanha_o_ciclo_de_vida():
    g = GerenciadorSinais(60)
    assert g.ativos() == []
    g.processar([candidato()], agora=0)
    g.processar([candidato(rp_liquido=0.70)], agora=1)
    ativos = g.ativos()
    assert len(ativos) == 1
    assert set(ativos[0]) == CAMPOS
    assert ativos[0]["rp_liquido_pct"] == 0.70
    assert ativos[0]["encerrado_em"] is None
    g.processar([], agora=2)
    assert g.ativos() == []


def test_ativos_devolve_copias():
    """Mexer no que `ativos()` ou um evento devolveu nao pode estragar o estado do gerenciador."""
    g = GerenciadorSinais(60)
    (novo,) = g.processar([candidato(rp_liquido=0.40)], agora=0)
    novo.sinal["rp_liquido_pct"] = 999
    novo.sinal["ponta_long"]["preco"] = 999
    g.ativos()[0]["etiquetas"].append("LIXO")
    atual = g.ativos()[0]
    assert atual["rp_liquido_pct"] == 0.40
    assert atual["ponta_long"]["preco"] == 100.0
    assert atual["etiquetas"] == []


def test_ativos_guarda_a_ordem_de_deteccao():
    g = GerenciadorSinais(60)
    g.processar([candidato(short="okx"), candidato(short="gate")], agora=0)
    assert [s["ponta_short"]["corretora"] for s in g.ativos()] == ["okx", "gate"]


# ---------------------------------------------------------------------------
# Chave: sinais diferentes sao independentes
# ---------------------------------------------------------------------------

def test_chaves_diferentes_sao_sinais_independentes():
    g = GerenciadorSinais(60)
    a = candidato(short="gate")
    b = candidato(short="okx")  # outra corretora no short = outra chave
    assert [e.tipo for e in g.processar([a, b], agora=0)] == ["novo", "novo"]
    ev = g.processar([a], agora=1)  # so o b some
    assert [(e.tipo, e.sinal["ponta_short"]["corretora"]) for e in ev] == [
        ("encerrado", "okx"),
        ("atualizado", "gate"),
    ]
    assert [s["ponta_short"]["corretora"] for s in g.ativos()] == ["gate"]


def test_ordem_dos_eventos_encerrados_primeiro_depois_entrada():
    g = GerenciadorSinais(60)
    a, b, c = candidato(short="gate"), candidato(short="okx"), candidato(short="mexc")
    g.processar([a, b], agora=0)
    # a continua, b some, c e novo; a entrada esta na ordem [c, a]
    ev = g.processar([c, a], agora=1)
    assert [(e.tipo, e.sinal["ponta_short"]["corretora"]) for e in ev] == [
        ("encerrado", "okx"),
        ("novo", "mexc"),
        ("atualizado", "gate"),
    ]


def test_varios_encerrados_seguem_a_ordem_de_deteccao():
    g = GerenciadorSinais(60)
    g.processar([candidato(short="okx"), candidato(short="gate"), candidato(short="mexc")], agora=0)
    ev = g.processar([], agora=1)
    assert [e.sinal["ponta_short"]["corretora"] for e in ev] == ["okx", "gate", "mexc"]


def test_chave_repetida_na_entrada_vale_o_ultimo():
    g = GerenciadorSinais(60)
    eventos = g.processar([candidato(rp_liquido=0.30), candidato(rp_liquido=0.50)], agora=0)
    assert len(eventos) == 1
    assert eventos[0].sinal["rp_liquido_pct"] == 0.50
    assert len(g.ativos()) == 1


# ---------------------------------------------------------------------------
# Anti-repeticao: id e detectado_em reaproveitados
# ---------------------------------------------------------------------------

def test_anti_repeticao_reaproveita_id_e_detectado_em():
    g = GerenciadorSinais(60)
    c = candidato()
    (primeiro,) = g.processar([c], agora=1000)
    g.processar([], agora=1001)
    (volta,) = g.processar([c], agora=1030)  # 30s depois de visto: dentro dos 60s
    assert volta.tipo == "novo" and volta.registrar_historico is False
    assert volta.sinal["id"] == primeiro.sinal["id"]
    assert volta.sinal["detectado_em"] == primeiro.sinal["detectado_em"]
    assert volta.sinal["atualizado_em"] != volta.sinal["detectado_em"]  # agora = 1030
    assert volta.sinal["encerrado_em"] is None  # voltou: nao esta mais encerrado
    # o encerramento seguinte fecha o MESMO registro do historico
    g.processar([], agora=1031)
    assert g.processar([c], agora=1032)[0].sinal["id"] == primeiro.sinal["id"]


def test_encerrado_de_sinal_reaproveitado_leva_o_id_original():
    g = GerenciadorSinais(60)
    c = candidato()
    (primeiro,) = g.processar([c], agora=1000)
    g.processar([], agora=1001)
    g.processar([c], agora=1010)
    (enc,) = g.processar([], agora=1020)
    assert enc.tipo == "encerrado" and enc.registrar_historico is True
    assert enc.sinal["id"] == primeiro.sinal["id"]
    assert enc.sinal["detectado_em"] == primeiro.sinal["detectado_em"]
    assert enc.sinal["encerrado_em"] == "1970-01-01T00:17:00.000Z"


def test_depois_da_janela_gera_id_novo_e_novo_registro():
    g = GerenciadorSinais(60)
    c = candidato()
    (primeiro,) = g.processar([c], agora=1000)
    g.processar([], agora=1001)
    (volta,) = g.processar([c], agora=1100)  # 100s depois de visto pela ultima vez
    assert volta.tipo == "novo" and volta.registrar_historico is True
    assert volta.sinal["id"] != primeiro.sinal["id"]
    assert volta.sinal["detectado_em"] != primeiro.sinal["detectado_em"]
    assert volta.sinal["id"].endswith("19700101T001820Z")  # 1100s = 00:18:20


def test_anti_repeticao_conta_desde_a_ultima_vez_visto():
    """Visto ate 1050 e some em 1051: voltar em 1109 (59s depois do visto) reaproveita; em 1110 (60s) nao."""
    for volta_em, reaproveita in [(1109, True), (1110, False)]:
        g = GerenciadorSinais(60)
        c = candidato()
        g.processar([c], agora=1000)
        g.processar([c], agora=1050)
        g.processar([], agora=1051)
        (ev,) = g.processar([c], agora=volta_em)
        assert ev.registrar_historico is (not reaproveita), volta_em


def test_anti_repeticao_zero_nunca_reaproveita():
    g = GerenciadorSinais(0)
    c = candidato()
    g.processar([c], agora=1000)
    g.processar([], agora=1001)
    (ev,) = g.processar([c], agora=1001.5)
    assert ev.registrar_historico is True


def test_anti_repeticao_e_por_chave():
    g = GerenciadorSinais(60)
    a, b = candidato(short="gate"), candidato(short="okx")
    g.processar([a], agora=0)
    g.processar([], agora=1)
    (ev,) = g.processar([b], agora=5)  # b nunca apareceu: registro novo normal
    assert ev.registrar_historico is True


def test_memoria_da_anti_repeticao_nao_cresce_para_sempre():
    """Quem sumiu ha mais que a janela nao precisa mais ser lembrado."""
    g = GerenciadorSinais(60)
    g.processar([candidato(short="gate"), candidato(short="okx")], agora=0)
    g.processar([], agora=1)
    assert len(g._encerrados) == 2
    g.processar([], agora=100)  # 100s depois do visto: a janela de 60s ja passou
    assert g._encerrados == {}


# ---------------------------------------------------------------------------
# Id
# ---------------------------------------------------------------------------

def test_formato_do_id():
    agora = datetime(2026, 10, 20, 15, 4, 12, 345000, tzinfo=timezone.utc).timestamp()
    (ev,) = GerenciadorSinais(60).processar([candidato(par="AKAM/USDT")], agora=agora)
    assert ev.sinal["id"] == "AKAM-futuro_futuro-bitget-gate-20261020T150412Z"


def test_id_de_spot_futuro():
    c = candidato(par="ETH/USDT", mercado_long="spot", long="mexc", short="bitget")
    (ev,) = GerenciadorSinais(60).processar([c], agora=0)
    assert ev.sinal["id"] == "ETH-spot_futuro-mexc-bitget-19700101T000000Z"


def test_id_nao_muda_quando_atualiza():
    g = GerenciadorSinais(60)
    ids = {g.processar([candidato()], agora=t)[0].sinal["id"] for t in (0, 1, 2, 3)}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Datas
# ---------------------------------------------------------------------------

def test_formato_das_datas_iso_utc_com_milissegundos():
    agora = datetime(2026, 10, 20, 15, 4, 12, 345000, tzinfo=timezone.utc).timestamp()
    g = GerenciadorSinais(60)
    (novo,) = g.processar([candidato()], agora=agora)
    assert novo.sinal["detectado_em"] == "2026-10-20T15:04:12.345Z"
    assert novo.sinal["atualizado_em"] == "2026-10-20T15:04:12.345Z"
    (atu,) = g.processar([candidato()], agora=agora + 1.775)
    assert atu.sinal["atualizado_em"] == "2026-10-20T15:04:14.120Z"
    (enc,) = g.processar([], agora=agora + 3600)
    assert enc.sinal["encerrado_em"] == "2026-10-20T16:04:12.345Z"


def test_datas_arredondam_para_o_milissegundo_mais_proximo():
    """Float como 1.0005 * 1000 pode cair em .9999...: o arredondamento evita perder 1 ms."""
    (ev,) = GerenciadorSinais(60).processar([candidato()], agora=1_792_508_652.345)
    assert ev.sinal["detectado_em"] == "2026-10-20T15:04:12.345Z"


def test_id_e_detectado_em_concordam_no_segundo():
    """Perto da virada do segundo, o id e o detectado_em nao podem discordar."""
    (ev,) = GerenciadorSinais(60).processar([candidato()], agora=59.9996)  # arredonda para 60.000
    assert ev.sinal["detectado_em"] == "1970-01-01T00:01:00.000Z"
    assert ev.sinal["id"].endswith("19700101T000100Z")


# ---------------------------------------------------------------------------
# JSON campo a campo
# ---------------------------------------------------------------------------

def test_json_de_candidato_conhecido_campo_a_campo():
    c = candidato(
        rp_liquido=0.40,
        par="AKAM/USDT",
        etiquetas=["STOCK"],
        suspeito=True,
        aviso_cotacao=True,
        modo="farm",
        custo_por_1000_usd=-2.0,
        spread_abertura_pct=12.54,
        spread_fechamento_pct=-12.65,
        fr_yield_8h_pct=0.02,
        funding_esperado_pct=0.01,
        metricas_periodo=metricas(
            abertura_media=11.72, fechamento_medio=-11.91, maior_abertura=12.80, maior_fechamento=-11.50, invertidas=3
        ),
        campos_long={"preco": 89.14, "lote_usd": 1847.87, "funding_pct": 0.0, "simbolo_original": "AKAMUSDT"},
    )
    s = GerenciadorSinais(60, periodo_h=6).processar([c], agora=0)[0].sinal
    assert s == {
        "id": "AKAM-futuro_futuro-bitget-gate-19700101T000000Z",
        "par": "AKAM/USDT",
        "tipo": "futuro_futuro",
        "modo": "farm",
        "etiquetas": ["STOCK"],
        "suspeito": True,
        "aviso_cotacao": True,
        "ponta_long": {
            "corretora": "bitget",
            "mercado": "futuro",
            "simbolo_original": "AKAMUSDT",
            "preco": 89.14,
            "lote_usd": 1847.87,
            "lote_medio_usd": 1000.0,
            "volume_24h_usd": 1_000_000.0,
            "funding_pct": 0.0,
            "funding_intervalo_h": 8,
            "limite_ordem_usd": 1_000_000.0,
            "variacao_24h_pct": 1.0,
            "taxa_taker_pct": 0.06,
        },
        "ponta_short": {
            "corretora": "gate",
            "mercado": "futuro",
            "simbolo_original": "AKAMUSDT",
            "preco": 100.0,
            "lote_usd": 1000,
            "lote_medio_usd": 1000.0,
            "volume_24h_usd": 1_000_000.0,
            "funding_pct": 0.01,
            "funding_intervalo_h": 8,
            "limite_ordem_usd": 1_000_000.0,
            "variacao_24h_pct": 1.0,
            "taxa_taker_pct": 0.06,
        },
        "spread_abertura_pct": 12.54,
        "spread_fechamento_pct": -12.65,
        "metricas_periodo": {
            "periodo_h": 6,
            "abertura_media_pct": 11.72,
            "fechamento_medio_pct": -11.91,
            "maior_abertura_pct": 12.80,
            "maior_fechamento_pct": -11.50,
            "invertidas": 3,
        },
        "fr_yield_8h_pct": 0.02,
        "rp_bruto_pct": c.rp_bruto_pct,
        "custo_taxas_pct": 0.24,
        "funding_esperado_pct": 0.01,
        "rp_liquido_pct": 0.40,
        "custo_por_1000_usd": -2.0,
        "tamanho_referencia_usd": 100,
        "detectado_em": "1970-01-01T00:00:00.000Z",
        "atualizado_em": "1970-01-01T00:00:00.000Z",
        "encerrado_em": None,
    }


def test_periodo_h_padrao_e_4():
    s = GerenciadorSinais(60).processar([candidato()], agora=0)[0].sinal
    assert s["metricas_periodo"]["periodo_h"] == 4


def test_metricas_ausentes_viram_null():
    s = GerenciadorSinais(60).processar([candidato(metricas_periodo=None)], agora=0)[0].sinal
    assert s["metricas_periodo"] is None
    assert set(s) == CAMPOS


def test_metricas_nao_vazam_campos_extras():
    """So as chaves da spec: as medias dos 10 maiores existem na Metricas mas nao vao para o JSON."""
    s = GerenciadorSinais(60).processar([candidato()], agora=0)[0].sinal
    assert set(s["metricas_periodo"]) == {
        "periodo_h", "abertura_media_pct", "fechamento_medio_pct",
        "maior_abertura_pct", "maior_fechamento_pct", "invertidas",
    }
    assert set(s["ponta_long"]) == {
        "corretora", "mercado", "simbolo_original", "preco", "lote_usd", "lote_medio_usd", "volume_24h_usd",
        "funding_pct", "funding_intervalo_h", "limite_ordem_usd", "variacao_24h_pct", "taxa_taker_pct",
    }
    assert set(s["ponta_short"]) == set(s["ponta_long"])


def test_ponta_spot_tem_funding_null():
    c = candidato(par="ETH/USDT", mercado_long="spot")
    s = GerenciadorSinais(60).processar([c], agora=0)[0].sinal
    assert s["ponta_long"]["mercado"] == "spot"
    assert s["ponta_long"]["funding_pct"] is None
    assert s["ponta_long"]["funding_intervalo_h"] is None
    assert s["tipo"] == "spot_futuro"


def test_campos_desconhecidos_ficam_null():
    c = candidato(campos_long={"lote_medio_usd": None, "volume_24h_usd": None, "limite_ordem_usd": None})
    s = GerenciadorSinais(60).processar([c], agora=0)[0].sinal
    assert s["ponta_long"]["lote_medio_usd"] is None
    assert s["ponta_long"]["volume_24h_usd"] is None
    assert s["ponta_long"]["limite_ordem_usd"] is None


def test_etiquetas_sao_copiadas():
    etiquetas = ["STOCK"]
    c = candidato(etiquetas=etiquetas)
    g = GerenciadorSinais(60)
    (ev,) = g.processar([c], agora=0)
    etiquetas.append("OUTRA")  # mexer na lista do candidato depois nao muda o sinal ja emitido
    assert ev.sinal["etiquetas"] == ["STOCK"]


def test_sinal_e_serializavel_em_json_estrito():
    """`allow_nan=False` faz o json.dumps falhar se sobrar NaN ou infinito."""
    g = GerenciadorSinais(60)
    (ev,) = g.processar([candidato()], agora=0)
    texto = json.dumps(ev.sinal, allow_nan=False)
    assert json.loads(texto) == ev.sinal
    json.dumps(g.ativos(), allow_nan=False)


# ---------------------------------------------------------------------------
# -0.0 e NaN / infinito
# ---------------------------------------------------------------------------

def _sinal(**outros):
    return GerenciadorSinais(60).processar([candidato(**outros)], agora=0)[0].sinal


def _tem_menos_zero(sinal: dict) -> bool:
    """True se o JSON do sinal tem um "-0.0" de verdade (e nao, por exemplo, o "-0.05" de outro campo)."""
    return re.search(r"-0\.0(?![0-9])", json.dumps(sinal)) is not None


def test_menos_zero_vira_zero():
    s = _sinal(custo_por_1000_usd=-0.0)
    assert s["custo_por_1000_usd"] == 0.0
    assert math.copysign(1, s["custo_por_1000_usd"]) == 1  # zero positivo
    assert not _tem_menos_zero(s)


def test_menos_zero_em_todos_os_campos_numericos():
    c = candidato(
        spread_abertura_pct=-0.0,
        spread_fechamento_pct=-0.0,
        rp_liquido=-0.0,
        funding_esperado_pct=-0.0,
        fr_yield_8h_pct=-0.0,
        metricas_periodo=metricas(abertura_media=-0.0, maior_fechamento=-0.0),
        campos_long={"funding_pct": -0.0, "variacao_24h_pct": -0.0},
        campos_short={"preco": -0.0},
    )
    s = GerenciadorSinais(60).processar([c], agora=0)[0].sinal
    assert not _tem_menos_zero(s)
    assert s["spread_abertura_pct"] == 0.0 and s["rp_liquido_pct"] == 0.0
    assert s["ponta_long"]["funding_pct"] == 0.0 and s["ponta_short"]["preco"] == 0.0
    assert s["metricas_periodo"]["abertura_media_pct"] == 0.0


@pytest.mark.parametrize("ruim", [float("nan"), float("inf"), float("-inf")])
def test_nao_finitos_viram_null_nos_campos_do_candidato(ruim):
    s = _sinal(spread_abertura_pct=ruim, custo_por_1000_usd=ruim, rp_bruto_pct=ruim)
    assert s["spread_abertura_pct"] is None
    assert s["custo_por_1000_usd"] is None
    assert s["rp_bruto_pct"] is None
    json.dumps(s, allow_nan=False)


@pytest.mark.parametrize("ruim", [float("nan"), float("inf"), float("-inf")])
def test_nao_finitos_viram_null_nas_pontas_e_nas_metricas(ruim):
    s = _sinal(
        metricas_periodo=metricas(abertura_media=ruim, maior_abertura=ruim),
        campos_long={"preco": ruim, "funding_pct": ruim},
        campos_short={"lote_medio_usd": ruim, "taxa_taker_pct": ruim},
    )
    assert s["metricas_periodo"]["abertura_media_pct"] is None
    assert s["metricas_periodo"]["maior_abertura_pct"] is None
    assert s["metricas_periodo"]["fechamento_medio_pct"] == -0.10  # os outros ficam
    assert s["ponta_long"]["preco"] is None and s["ponta_long"]["funding_pct"] is None
    assert s["ponta_short"]["lote_medio_usd"] is None and s["ponta_short"]["taxa_taker_pct"] is None
    json.dumps(s, allow_nan=False)


def test_valores_normais_passam_sem_mudar():
    s = _sinal(spread_abertura_pct=0.3, rp_liquido=0.1)
    assert s["spread_abertura_pct"] == 0.3
    assert s["rp_liquido_pct"] == 0.1
    assert s["tamanho_referencia_usd"] == 100
    assert s["metricas_periodo"]["invertidas"] == 0
    assert s["ponta_long"]["funding_intervalo_h"] == 8
    assert s["suspeito"] is False and s["aviso_cotacao"] is False  # bool nao vira numero
