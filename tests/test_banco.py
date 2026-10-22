"""Testes do armazenamento SQLite (Task 9).

Nenhum teste acessa a internet. Os 3 primeiros sao os do brief (verbatim); os demais cobrem
cada regra de evento (novo, reabrir, encerrar, atualizado, encerrado sem registro), o limite
do dia em `sinais_do_dia`, a persistencia depois de reabrir o arquivo e a limpeza.
"""

import pytest

from scanner.armazenamento import Banco
from scanner.sinais import Evento, GerenciadorSinais
from tests.fabricas import candidato

# ---------------------------------------------------------------------------
# Brief (Task 9, Passo 1) - verbatim
# ---------------------------------------------------------------------------


def test_amostras_ida_e_volta(tmp_path):
    b = Banco(str(tmp_path / "s.db")); b.gravar_amostra("k", 100.0, 12.5, -12.6, 89.1, 100.3, 1800, 1400)
    assert b.amostras("k", desde=0)[0]["a"] == 12.5

def test_limpeza_7_dias(tmp_path):
    b = Banco(str(tmp_path / "s.db")); dia = 86400
    b.gravar_amostra("k", 0, 1, -1, 1, 1, 1, 1); b.gravar_amostra("k", 8 * dia, 1, -1, 1, 1, 1, 1)
    assert b.limpar_antigos(agora=8 * dia, dias=7) == 1

def test_grupos_de_filtros(tmp_path):
    b = Banco(str(tmp_path / "s.db")); b.salvar_grupo("Farm", {"volume_min_usd": 500})
    assert b.listar_grupos()["Farm"]["volume_min_usd"] == 500
    b.apagar_grupo("Farm"); assert "Farm" not in b.listar_grupos()


# ---------------------------------------------------------------------------
# Ajudantes
# ---------------------------------------------------------------------------

DIA = 86400
# 2026-10-20 00:00:00 UTC, em segundos desde 1970.
INICIO_20 = 1792454400


@pytest.fixture
def banco(tmp_path):
    b = Banco(str(tmp_path / "s.db"))
    yield b
    b.fechar()


def _sinal(**campos) -> dict:
    """Um sinal minimo no formato da spec (so o que o banco le, mais um campo extra para conferir o JSON)."""
    sinal = {
        "id": "BTC-futuro_futuro-bitget-gate-20261020T150412Z",
        "par": "BTC/USDT",
        "tipo": "futuro_futuro",
        "modo": "lucro",
        "rp_liquido_pct": 0.40,
        "detectado_em": "2026-10-20T15:04:12.000Z",
        "atualizado_em": "2026-10-20T15:04:12.000Z",
        "encerrado_em": None,
        "ponta_long": {"corretora": "bitget", "preco": 89.1},
    }
    sinal.update(campos)
    return sinal


def _ids(banco, dia) -> list[str]:
    return [s["id"] for s in banco.sinais_do_dia(dia)]


# ---------------------------------------------------------------------------
# Regras dos eventos
# ---------------------------------------------------------------------------


def test_novo_com_historico_insere_registro_aberto(banco):
    banco.gravar_evento(Evento("novo", _sinal(), registrar_historico=True))

    [s] = banco.sinais_do_dia("2026-10-20")
    assert s["id"] == "BTC-futuro_futuro-bitget-gate-20261020T150412Z"
    assert s["modo"] == "lucro"
    assert s["par"] == "BTC/USDT"
    assert s["tipo"] == "futuro_futuro"
    assert s["detectado_em"] == "2026-10-20T15:04:12.000Z"
    assert s["encerrado_em"] is None
    assert s["rp_liquido_pct"] == 0.40
    assert s["sinal"] == _sinal()  # o JSON inteiro volta como dict


def test_novo_sem_historico_reabre_o_registro(banco):
    banco.gravar_evento(Evento("novo", _sinal(), registrar_historico=True))
    banco.gravar_evento(Evento("encerrado", _sinal(encerrado_em="2026-10-20T15:10:00.000Z"), registrar_historico=True))
    assert banco.sinais_do_dia("2026-10-20")[0]["encerrado_em"] == "2026-10-20T15:10:00.000Z"

    # O sinal voltou dentro da janela de anti-repeticao: mesmo id, sem novo registro.
    banco.gravar_evento(Evento("novo", _sinal(rp_liquido_pct=0.55, atualizado_em="2026-10-20T15:10:20.000Z"),
                               registrar_historico=False))

    [s] = banco.sinais_do_dia("2026-10-20")  # continua um registro so
    assert s["encerrado_em"] is None
    assert s["rp_liquido_pct"] == 0.55
    assert s["sinal"]["rp_liquido_pct"] == 0.55
    assert s["sinal"]["atualizado_em"] == "2026-10-20T15:10:20.000Z"
    assert s["detectado_em"] == "2026-10-20T15:04:12.000Z"


def test_encerrado_fecha_o_registro_e_guarda_o_json_final(banco):
    banco.gravar_evento(Evento("novo", _sinal(), registrar_historico=True))
    banco.gravar_evento(Evento("encerrado", _sinal(encerrado_em="2026-10-20T15:10:00.000Z", rp_liquido_pct=0.31),
                               registrar_historico=True))

    [s] = banco.sinais_do_dia("2026-10-20")
    assert s["encerrado_em"] == "2026-10-20T15:10:00.000Z"
    assert s["rp_liquido_pct"] == 0.31
    assert s["sinal"]["encerrado_em"] == "2026-10-20T15:10:00.000Z"


def test_novo_sem_historico_e_sem_registro_insere(banco):
    # Defensivo: o gerenciador diz que o registro existe, mas ele nao esta no banco.
    banco.gravar_evento(Evento("novo", _sinal(), registrar_historico=False))

    [s] = banco.sinais_do_dia("2026-10-20")
    assert s["encerrado_em"] is None


def test_encerrado_sem_registro_insere(banco):
    banco.gravar_evento(Evento("encerrado", _sinal(encerrado_em="2026-10-20T15:10:00.000Z"), registrar_historico=True))

    [s] = banco.sinais_do_dia("2026-10-20")
    assert s["encerrado_em"] == "2026-10-20T15:10:00.000Z"


def test_atualizado_nao_grava_nada(banco):
    banco.gravar_evento(Evento("atualizado", _sinal(), registrar_historico=False))
    assert banco.sinais_do_dia("2026-10-20") == []

    # E nao mexe num registro que ja existe.
    banco.gravar_evento(Evento("novo", _sinal(), registrar_historico=True))
    banco.gravar_evento(Evento("atualizado", _sinal(rp_liquido_pct=9.9), registrar_historico=False))
    assert banco.sinais_do_dia("2026-10-20")[0]["rp_liquido_pct"] == 0.40


def test_atualizado_com_historico_true_tambem_nao_grava(banco):
    # Defensivo: so "novo" e "encerrado" gravam, mesmo que a flag venha True.
    banco.gravar_evento(Evento("atualizado", _sinal(), registrar_historico=True))
    assert banco.sinais_do_dia("2026-10-20") == []


def test_rp_liquido_ausente_vira_none(banco):
    banco.gravar_evento(Evento("novo", _sinal(rp_liquido_pct=None), registrar_historico=True))
    assert banco.sinais_do_dia("2026-10-20")[0]["rp_liquido_pct"] is None


def test_nan_no_sinal_e_recusado_e_nada_e_gravado(banco):
    with pytest.raises(ValueError):
        banco.gravar_evento(Evento("novo", _sinal(rp_liquido_pct=float("nan")), registrar_historico=True))
    assert banco.sinais_do_dia("2026-10-20") == []


def test_ciclo_completo_com_o_gerenciador_real(banco):
    """Do gerenciador ao banco: novo, atualizado, encerrado, volta (anti-repeticao) e encerrado de novo."""
    g = GerenciadorSinais(anti_repeticao_s=60)
    c = candidato(rp_liquido=0.40)
    t0 = INICIO_20 + 15 * 3600

    for agora, lista in [(t0, [c]), (t0 + 1, [c]), (t0 + 2, []), (t0 + 30, [c]), (t0 + 40, [])]:
        for evento in g.processar(lista, agora=agora):
            banco.gravar_evento(evento)

    [s] = banco.sinais_do_dia("2026-10-20")  # um unico registro, apesar de dois "novo"
    assert s["encerrado_em"] == s["sinal"]["encerrado_em"] and s["encerrado_em"] is not None
    assert s["detectado_em"].startswith("2026-10-20T15:00:00")
    assert s["modo"] == "lucro" and s["par"] == "BTC/USDT"


# ---------------------------------------------------------------------------
# sinais_do_dia
# ---------------------------------------------------------------------------


def test_sinais_do_dia_limites_do_dia_utc(banco):
    detectados = {
        "antes": "2026-10-19T23:59:59.999Z",
        "inicio": "2026-10-20T00:00:00.000Z",
        "fim": "2026-10-20T23:59:59.999Z",
        "depois": "2026-10-21T00:00:00.000Z",
    }
    for nome, quando in detectados.items():
        banco.gravar_evento(Evento("novo", _sinal(id=nome, detectado_em=quando), registrar_historico=True))

    assert _ids(banco, "2026-10-20") == ["inicio", "fim"]
    assert _ids(banco, "2026-10-19") == ["antes"]
    assert _ids(banco, "2026-10-21") == ["depois"]
    assert _ids(banco, "2026-10-22") == []


def test_sinais_do_dia_ordenados_por_deteccao(banco):
    for nome, quando in [("c", "2026-10-20T12:00:00.000Z"), ("a", "2026-10-20T01:00:00.000Z"),
                         ("b", "2026-10-20T05:00:00.000Z")]:
        banco.gravar_evento(Evento("novo", _sinal(id=nome, detectado_em=quando), registrar_historico=True))
    assert _ids(banco, "2026-10-20") == ["a", "b", "c"]


def test_sinais_do_dia_atravessa_virada_de_mes_e_ano(banco):
    banco.gravar_evento(Evento("novo", _sinal(id="x", detectado_em="2026-12-31T23:00:00.000Z"), registrar_historico=True))
    banco.gravar_evento(Evento("novo", _sinal(id="y", detectado_em="2027-01-01T00:00:00.000Z"), registrar_historico=True))
    assert _ids(banco, "2026-12-31") == ["x"]
    assert _ids(banco, "2027-01-01") == ["y"]


def test_sinais_do_dia_data_invalida(banco):
    with pytest.raises(ValueError):
        banco.sinais_do_dia("ontem")


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------


def test_dados_persistem_ao_reabrir_o_arquivo(tmp_path):
    caminho = str(tmp_path / "s.db")
    b = Banco(caminho)
    b.gravar_evento(Evento("novo", _sinal(), registrar_historico=True))
    b.gravar_amostra("k", 100.0, 12.5, -12.6, 89.1, 100.3, 1800, 1400)
    b.salvar_grupo("Farm", {"volume_min_usd": 500})
    b.fechar()

    b2 = Banco(caminho)
    try:
        assert _ids(b2, "2026-10-20") == ["BTC-futuro_futuro-bitget-gate-20261020T150412Z"]
        assert b2.amostras("k", desde=0)[0]["preco_short"] == 100.3
        assert b2.listar_grupos() == {"Farm": {"volume_min_usd": 500}}
    finally:
        b2.fechar()


def test_modo_wal_e_banco_novo_nao_apaga_o_antigo(tmp_path):
    caminho = str(tmp_path / "s.db")
    b = Banco(caminho)
    modo = b._conn.execute("PRAGMA journal_mode").fetchone()[0]
    b.fechar()
    assert modo == "wal"
    # Abrir de novo (CREATE TABLE IF NOT EXISTS) nao pode falhar nem limpar nada.
    Banco(caminho).fechar()


# ---------------------------------------------------------------------------
# Amostras
# ---------------------------------------------------------------------------


def test_amostras_chaves_do_dict_e_ordem_por_ts(banco):
    banco.gravar_amostra("k", 30.0, 3, -3, 3, 3, 3, 3)
    banco.gravar_amostra("k", 10.0, 1, -1, 1, 1, 1, 1)
    banco.gravar_amostra("k", 20.0, 2, -2, 2, 2, 2, 2)

    linhas = banco.amostras("k", desde=0)
    assert [x["ts"] for x in linhas] == [10.0, 20.0, 30.0]
    assert set(linhas[0]) == {"ts", "a", "f", "preco_long", "preco_short", "lote_long", "lote_short"}
    assert linhas[0] == {"ts": 10.0, "a": 1, "f": -1, "preco_long": 1, "preco_short": 1, "lote_long": 1, "lote_short": 1}


def test_amostras_filtra_por_chave_e_desde(banco):
    banco.gravar_amostra("k", 10.0, 1, -1, 1, 1, 1, 1)
    banco.gravar_amostra("k", 20.0, 2, -2, 2, 2, 2, 2)
    banco.gravar_amostra("outra", 20.0, 9, -9, 9, 9, 9, 9)

    assert [x["ts"] for x in banco.amostras("k", desde=20.0)] == [20.0]  # "desde" inclui o proprio instante
    assert [x["a"] for x in banco.amostras("outra", desde=0)] == [9]
    assert banco.amostras("nao_existe", desde=0) == []


def test_limpar_antigos_apaga_so_o_que_passou_do_limite(banco):
    # Limite = agora - 7 dias = 1 dia. Uma amostra exatamente no limite fica (so < apaga).
    for ts in (0, DIA - 1, DIA, 8 * DIA):
        banco.gravar_amostra("k", ts, 1, -1, 1, 1, 1, 1)

    assert banco.limpar_antigos(agora=8 * DIA, dias=7) == 2
    assert [x["ts"] for x in banco.amostras("k", desde=0)] == [DIA, 8 * DIA]
    assert banco.limpar_antigos(agora=8 * DIA, dias=7) == 0  # de novo: nada a apagar


def test_limpar_antigos_apaga_sinais_encerrados_antigos_mas_nao_os_abertos_nem_os_recentes(banco):
    agora = INICIO_20 + 10 * DIA  # 2026-10-30 00:00 UTC; limite de 7 dias = 2026-10-23 00:00 UTC
    eventos = [
        # encerrado antes do limite: apaga
        Evento("encerrado", _sinal(id="velho", detectado_em="2026-10-20T10:00:00.000Z",
                                   encerrado_em="2026-10-22T10:00:00.000Z"), True),
        # encerrado depois do limite: fica
        Evento("encerrado", _sinal(id="recente", detectado_em="2026-10-20T10:00:00.000Z",
                                   encerrado_em="2026-10-25T10:00:00.000Z"), True),
        # ainda aberto, mesmo velho: fica
        Evento("novo", _sinal(id="aberto", detectado_em="2026-10-20T10:00:00.000Z"), True),
    ]
    for e in eventos:
        banco.gravar_evento(e)

    assert banco.limpar_antigos(agora=agora, dias=7) == 0  # o retorno conta so amostras
    assert _ids(banco, "2026-10-20") == ["aberto", "recente"]


def test_limpar_antigos_aceita_dias_fracionados(banco):
    banco.gravar_amostra("k", 0, 1, -1, 1, 1, 1, 1)
    banco.gravar_amostra("k", 0.6 * DIA, 1, -1, 1, 1, 1, 1)
    # limite = 1 dia - 0.5 dia = 0.5 dia: so a de ts=0 e mais velha que isso
    assert banco.limpar_antigos(agora=DIA, dias=0.5) == 1


# ---------------------------------------------------------------------------
# Grupos
# ---------------------------------------------------------------------------


def test_salvar_grupo_substitui_o_existente(banco):
    banco.salvar_grupo("Farm", {"volume_min_usd": 500})
    banco.salvar_grupo("Farm", {"volume_min_usd": 900, "modo": "farm"})
    assert banco.listar_grupos() == {"Farm": {"volume_min_usd": 900, "modo": "farm"}}


def test_listar_grupos_varios_e_vazio(banco):
    assert banco.listar_grupos() == {}
    banco.salvar_grupo("Farm", {"a": 1})
    banco.salvar_grupo("Default", {"b": 2})
    assert banco.listar_grupos() == {"Farm": {"a": 1}, "Default": {"b": 2}}


def test_apagar_grupo_inexistente_nao_da_erro(banco):
    banco.apagar_grupo("Nada")
    assert banco.listar_grupos() == {}


def test_grupo_com_nan_e_recusado(banco):
    with pytest.raises(ValueError):
        banco.salvar_grupo("Ruim", {"x": float("nan")})
    assert banco.listar_grupos() == {}


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


def test_usa_o_mesmo_banco_de_outra_thread(banco):
    import threading

    erros = []

    def trabalho():
        try:
            banco.gravar_amostra("k", 1.0, 1, -1, 1, 1, 1, 1)
            banco.listar_grupos()
        except Exception as erro:  # noqa: BLE001 - so para o teste enxergar a falha
            erros.append(erro)

    t = threading.Thread(target=trabalho)
    t.start(); t.join()

    assert erros == []
    assert len(banco.amostras("k", desde=0)) == 1


def test_gravar_amostras_em_lote_numa_transacao(banco):
    # O motor (Task 10) grava as amostras do ciclo de uma vez: um acesso ao disco por ciclo.
    banco.gravar_amostras([
        ("k1", 10.0, 1.0, -1.0, 100.0, 101.0, 1000.0, 900.0),
        ("k2", 10.0, 2.0, -2.0, 50.0, 51.0, 500.0, 400.0),
        ("k1", 15.0, 1.5, -0.5, 100.5, 101.5, 1100.0, 950.0),
    ])
    assert [a["ts"] for a in banco.amostras("k1", desde=0)] == [10.0, 15.0]
    assert banco.amostras("k2", desde=0) == [
        {"ts": 10.0, "a": 2.0, "f": -2.0, "preco_long": 50.0, "preco_short": 51.0, "lote_long": 500.0, "lote_short": 400.0}
    ]


def test_gravar_amostras_lista_vazia_nao_faz_nada(banco):
    banco.gravar_amostras([])
    assert banco.amostras("k", desde=0) == []
