"""Testes do armazenamento SQLite (Task 9).

Nenhum teste acessa a internet. Os 3 primeiros sao os do brief (verbatim); os demais cobrem
cada regra de evento (novo, reabrir, encerrar, atualizado, encerrado sem registro), o limite
do dia em `sinais_do_dia`, a persistencia depois de reabrir o arquivo e a limpeza.
"""

import sqlite3

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


# ---------------------------------------------------------------------------
# Eventos em lote e sincronizacao do disco (Task 14)
# ---------------------------------------------------------------------------


def test_gravar_eventos_do_ciclo_numa_transacao_so(banco):
    eventos = [
        Evento("novo", _sinal(id="a"), registrar_historico=True),
        Evento("atualizado", _sinal(id="b"), registrar_historico=False),   # nao grava
        Evento("novo", _sinal(id="c"), registrar_historico=True),
        Evento("encerrado", _sinal(id="a", encerrado_em="2026-10-20T15:10:00.000Z"), registrar_historico=True),
    ]
    comandos = []
    banco._conn.set_trace_callback(comandos.append)
    assert banco.gravar_eventos(eventos) == []
    banco._conn.set_trace_callback(None)
    assert [c for c in comandos if c in ("BEGIN ", "COMMIT")] == ["BEGIN ", "COMMIT"]  # uma transacao so
    registros = {s["id"]: s for s in banco.sinais_do_dia("2026-10-20")}
    assert set(registros) == {"a", "c"}
    assert registros["a"]["encerrado_em"] == "2026-10-20T15:10:00.000Z" and registros["c"]["encerrado_em"] is None


def test_gravar_eventos_recusa_so_o_evento_invalido(banco):
    ruim = Evento("novo", _sinal(id="ruim", rp_liquido_pct=float("nan")), registrar_historico=True)
    bom = Evento("novo", _sinal(id="bom"), registrar_historico=True)
    recusados = banco.gravar_eventos([ruim, bom])
    assert [e for e, _ in recusados] == [ruim] and isinstance(recusados[0][1], ValueError)
    assert _ids(banco, "2026-10-20") == ["bom"]


def test_gravar_eventos_lista_vazia_nao_faz_nada(banco):
    assert banco.gravar_eventos([]) == []


def test_modo_wal_com_sincronizacao_normal(banco):
    assert banco._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert banco._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


# ---------------------------------------------------------------------------
# Correcao pos-revisao da Task 14: um evento ruim nunca leva o lote junto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("campo, valor", [
    ("par", None), ("par", ""), ("id", None), ("id", 7), ("modo", None), ("tipo", ""),
    ("detectado_em", None), ("rp_liquido_pct", "0.5x"), ("rp_liquido_pct", True),
])
def test_evento_com_campo_obrigatorio_ruim_e_recusado_e_o_bom_e_gravado(banco, campo, valor):
    bom = Evento("novo", _sinal(id="bom"), registrar_historico=True)
    ruim = Evento("novo", _sinal(**{"id": "ruim", campo: valor}), registrar_historico=True)
    recusados = banco.gravar_eventos([bom, ruim])
    assert [e for e, _ in recusados] == [ruim]
    assert isinstance(recusados[0][1], ValueError) and campo in str(recusados[0][1])
    assert _ids(banco, "2026-10-20") == ["bom"]


def test_lote_que_falha_no_sqlite_e_refeito_linha_a_linha(banco, monkeypatch):
    # Se algo passar da validacao e o SQLite recusar no meio do lote, os bons continuam gravados.
    original = Banco._linha_do_evento

    def sem_validacao(e):
        if e.sinal["id"] == "ruim":
            return ("ruim", "lucro", None, "spot_futuro", "2026-10-20T15:00:00.000Z", None, None, "{}")  # par NULL
        return original(e)

    monkeypatch.setattr(Banco, "_linha_do_evento", staticmethod(sem_validacao))
    eventos = [Evento("novo", _sinal(id=i), registrar_historico=True) for i in ("a", "ruim", "c")]
    comandos = []
    banco._conn.set_trace_callback(comandos.append)
    recusados = banco.gravar_eventos(eventos)
    banco._conn.set_trace_callback(None)
    assert [e.sinal["id"] for e, _ in recusados] == ["ruim"]
    assert isinstance(recusados[0][1], sqlite3.IntegrityError)
    assert _ids(banco, "2026-10-20") == ["a", "c"]
    assert comandos.count("COMMIT") == 1  # a nova tentativa, linha a linha, ainda e uma transacao so


# ---------------------------------------------------------------------------
# Limpeza em lotes (Task 17): o motor nunca espera a limpeza inteira
# ---------------------------------------------------------------------------


def test_limpar_antigos_em_lotes_pequenos_da_o_mesmo_resultado(banco):
    # Amostras fora de ordem de horario (relogio que voltou, banco reaberto): os lotes andam pelo rowid,
    # entao nenhuma escapa.
    horarios = [5 * DIA, 0, 8 * DIA, 0.5 * DIA, 2 * DIA, DIA - 1, 7.5 * DIA, 0.1, 3 * DIA, 0.9 * DIA]
    for ts in horarios:
        banco.gravar_amostra("k", ts, 1, -1, 1, 1, 1, 1)
    comandos = []
    banco._conn.set_trace_callback(comandos.append)
    assert banco.limpar_antigos(agora=8 * DIA, dias=7, lote=3) == 5  # limite = 1 dia
    banco._conn.set_trace_callback(None)
    assert sorted(x["ts"] for x in banco.amostras("k", desde=0)) == [2 * DIA, 3 * DIA, 5 * DIA, 7.5 * DIA, 8 * DIA]
    apagar_amostras = [c for c in comandos if c.startswith("DELETE FROM amostras")]
    assert len(apagar_amostras) == 4  # 10 linhas em lotes de 3 rowids: um comando (e uma transacao) por lote


def test_limpar_antigos_em_lotes_apaga_os_sinais_encerrados_antigos(banco):
    agora = INICIO_20 + 10 * DIA  # limite de 7 dias = 2026-10-23 00:00 UTC
    for i in range(7):
        banco.gravar_evento(Evento("encerrado", _sinal(id=f"velho{i}", encerrado_em="2026-10-22T10:00:00.000Z"), True))
        banco.gravar_evento(Evento("novo", _sinal(id=f"aberto{i}"), True))
    banco.limpar_antigos(agora=agora, dias=7, lote=2)
    assert _ids(banco, "2026-10-20") == [f"aberto{i}" for i in range(7)]


def test_limpar_antigos_com_banco_vazio(banco):
    assert banco.limpar_antigos(agora=8 * DIA, dias=7) == 0


def test_limpar_antigos_para_se_o_banco_for_fechado_no_meio(tmp_path):
    # O scanner encerrando (Ctrl+C) fecha o banco enquanto a limpeza roda em outra thread: ela para no proximo
    # lote, sem erro.
    b = Banco(str(tmp_path / "s.db"))
    for ts in range(10):
        b.gravar_amostra("k", ts, 1, -1, 1, 1, 1, 1)
    original = b._apagar_lote
    lotes = []

    def fechar_depois_do_primeiro(*argumentos):
        resultado = original(*argumentos)
        lotes.append(resultado)
        if len(lotes) == 1:
            b.fechar()  # fora da trava: o lote ja terminou
        return resultado

    b._apagar_lote = fechar_depois_do_primeiro
    assert b.limpar_antigos(agora=8 * DIA, dias=7, lote=2) == 2  # so o primeiro lote
    assert lotes == [2, None]  # o segundo lote viu o banco fechado e parou ali


def test_limpar_antigos_lote_invalido(banco):
    with pytest.raises(ValueError):
        banco.limpar_antigos(agora=8 * DIA, dias=7, lote=0)


# ---------------------------------------------------------------------------
# Atraso medio e reconexoes de cada corretora, por dia (Task 17)
# ---------------------------------------------------------------------------


def _status(reconexoes, atraso_ms=None, corretora="gate"):
    return {corretora: {"estado": "conectada", "atraso_medio_ms": atraso_ms, "reconexoes": reconexoes}}


def test_reconexoes_do_dia_pela_diferenca_do_contador(banco):
    # O contador do scanner so cresce: 3 e depois 5 = 5 reconexoes no dia (e nao 3 + 5).
    banco.registrar_corretoras("2026-10-20", _status(3))
    banco.registrar_corretoras("2026-10-20", _status(5))
    banco.registrar_corretoras("2026-10-20", _status(5))
    assert banco.corretoras_do_dia("2026-10-20")["gate"]["reconexoes"] == 5


def test_reconexoes_com_o_scanner_reiniciado_no_meio_do_dia(tmp_path):
    caminho = str(tmp_path / "s.db")
    primeiro = Banco(caminho)
    primeiro.registrar_corretoras("2026-10-20", _status(3))
    primeiro.registrar_corretoras("2026-10-20", _status(5))
    primeiro.fechar()
    # Scanner religado: o contador recomeca do zero (1 e depois 2 = mais 2 reconexoes).
    segundo = Banco(caminho)
    segundo.registrar_corretoras("2026-10-20", _status(1))
    segundo.registrar_corretoras("2026-10-20", _status(2))
    assert segundo.corretoras_do_dia("2026-10-20")["gate"]["reconexoes"] == 7
    segundo.fechar()


def test_contador_que_volta_para_tras_conta_como_recomeco(banco):
    banco.registrar_corretoras("2026-10-20", _status(5))
    banco.registrar_corretoras("2026-10-20", _status(2))  # o estado recomecou do zero e ja contou 2
    assert banco.corretoras_do_dia("2026-10-20")["gate"]["reconexoes"] == 7


def test_reconexoes_depois_da_meia_noite_vao_para_o_dia_novo(banco):
    banco.registrar_corretoras("2026-10-20", _status(3))
    banco.registrar_corretoras("2026-10-21", _status(5))  # o mesmo scanner, ja no dia seguinte
    assert banco.corretoras_do_dia("2026-10-20")["gate"]["reconexoes"] == 3
    assert banco.corretoras_do_dia("2026-10-21")["gate"]["reconexoes"] == 2


def test_atraso_medio_do_dia_e_a_media_das_leituras(banco):
    for atraso in (100.0, None, 200.0, 360.0):  # None = a corretora nao informou o horario: fica fora
        banco.registrar_corretoras("2026-10-20", _status(0, atraso))
    banco.registrar_corretoras("2026-10-20", _status(0, corretora="mexc"))
    assert banco.corretoras_do_dia("2026-10-20") == {
        "gate": {"atraso_medio_ms": 220.0, "reconexoes": 0},
        "mexc": {"atraso_medio_ms": None, "reconexoes": 0},
    }


def test_corretoras_do_dia_com_o_status_atual_nao_grava_nada(banco):
    banco.registrar_corretoras("2026-10-20", _status(2, 100.0))
    atual = {**_status(6, 300.0), **_status(1, corretora="bitget")}
    assert banco.corretoras_do_dia("2026-10-20", atual) == {
        "bitget": {"atraso_medio_ms": None, "reconexoes": 1},
        "gate": {"atraso_medio_ms": 200.0, "reconexoes": 6},
    }
    assert banco.corretoras_do_dia("2026-10-20") == {"gate": {"atraso_medio_ms": 100.0, "reconexoes": 2}}
    # E a proxima leitura gravada continua contando a partir do 2 (o status atual nao mexeu no contador).
    banco.registrar_corretoras("2026-10-20", _status(6, 300.0))
    assert banco.corretoras_do_dia("2026-10-20")["gate"]["reconexoes"] == 6


def test_corretoras_do_dia_vazio_e_dia_em_formato_curto(banco):
    assert banco.corretoras_do_dia("2026-10-20") == {}
    banco.registrar_corretoras("20261020", _status(1))  # o mesmo dia escrito sem tracos
    assert banco.corretoras_do_dia("2026-10-20")["gate"]["reconexoes"] == 1


@pytest.mark.parametrize("dia", ["2026-13-01", "ontem", ""])
def test_corretoras_dia_invalido(banco, dia):
    with pytest.raises(ValueError):
        banco.registrar_corretoras(dia, _status(1))
    with pytest.raises(ValueError):
        banco.corretoras_do_dia(dia)


@pytest.mark.parametrize("dados", [
    {"atraso_medio_ms": 10.0, "reconexoes": -1},
    {"atraso_medio_ms": 10.0, "reconexoes": 1.5},
    {"atraso_medio_ms": 10.0, "reconexoes": True},
    {"atraso_medio_ms": float("nan"), "reconexoes": 1},
    {"atraso_medio_ms": "10", "reconexoes": 1},
])
def test_registrar_corretoras_recusa_valor_invalido_e_nao_grava_nada(banco, dados):
    status = {"bitget": {"atraso_medio_ms": 50.0, "reconexoes": 1}, "gate": dados}
    with pytest.raises(ValueError, match="gate"):
        banco.registrar_corretoras("2026-10-20", status)
    assert banco.corretoras_do_dia("2026-10-20") == {}


def test_registrar_corretoras_sem_reconexoes_conta_zero(banco):
    banco.registrar_corretoras("2026-10-20", {"gate": {"estado": "atrasada", "atraso_medio_ms": None}})
    assert banco.corretoras_do_dia("2026-10-20") == {"gate": {"atraso_medio_ms": None, "reconexoes": 0}}


def test_limpar_antigos_apaga_as_metricas_das_corretoras_dos_dias_antigos(banco):
    for dia in ("2026-10-21", "2026-10-22", "2026-10-23", "2026-10-29"):
        banco.registrar_corretoras(dia, _status(0, 10.0))
    banco.limpar_antigos(agora=INICIO_20 + 10 * DIA, dias=7)  # limite = 2026-10-23 00:00 UTC
    assert banco.corretoras_do_dia("2026-10-22") == {}
    assert banco.corretoras_do_dia("2026-10-23") != {} and banco.corretoras_do_dia("2026-10-29") != {}


def test_limpar_antigos_com_o_limite_antes_de_1970(banco):
    # Relogio falso dos testes (t = 1000 s) com 7 dias de retencao: o limite cai antes de 1970. Nada a apagar e
    # nenhum erro (no Windows, datetime.fromtimestamp recusaria esse horario).
    banco.gravar_amostra("k", 500, 1, -1, 1, 1, 1, 1)
    assert banco.limpar_antigos(agora=1000, dias=7) == 0
    assert len(banco.amostras("k", desde=0)) == 1
