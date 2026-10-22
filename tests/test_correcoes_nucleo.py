"""Testes das correcoes da revisao final do nucleo (motor, banco, log, filtros, sinais e config).

Cada bloco cita o item da revisao (revisao-final-nucleo.md: I1-I5, M1-M12; e os Minor 7 e 8 da interface).
Nenhum teste acessa a internet.
"""

import asyncio
import io
import json
import logging
import logging.handlers
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

import scanner.__main__ as principal
from scanner import logs
from scanner.api import criar_app
from scanner.armazenamento import Banco, EscritorBanco
from scanner.armazenamento import banco as modulo_banco
from scanner.coletores.dados_lentos import DadosLentos
from scanner.config.carregar import Config, ErroConfig, carregar_config
from scanner.estado import EstadoMercados
from scanner.filtros import Filtros
from scanner.motor import Motor, Sessao, chave_de, texto_da_chave
from scanner.sinais import Evento, GerenciadorSinais
from scanner.validacao import linhas_do_resumo, resumo_do_dia
from tests.fabricas import (
    INICIO_20_OUT,
    ClienteDoPainel,
    CorretoraFalsa,
    DadosLentosFalso,
    candidato,
    cfg,
    cfg_com,
    iso_utc,
    market_ccxt,
    motor_falso,
)
from tests.test_motor import config_servico, exchanges_falsas, ticker

RAIZ = Path(__file__).resolve().parent.parent

COMBO = ("BTC", "gate", "spot", "bitget", "futuro")
CHAVE = chave_de(COMBO)
CHAVE_TEXTO = texto_da_chave(CHAVE)  # "BTC/USDT|spot_futuro|gate|spot|bitget|futuro"


def amostra(ts, chave=CHAVE_TEXTO, a=1.0, f=-1.0, lote_long=1000.0, lote_short=1000.0):
    """Uma amostra no formato de Banco.gravar_amostras."""
    return (chave, ts, a, f, 100.0, 101.0, lote_long, lote_short)


@pytest.fixture
def banco(tmp_path):
    b = Banco(str(tmp_path / "n.db"))
    yield b
    b.fechar()


# ---------------------------------------------------------------------------
# I1. Suspeito nos dois sentidos, pela razao dos precos
# ---------------------------------------------------------------------------


def _por_direcao(motor, agora=10.0) -> dict:
    return {(c.ponta_long.corretora, c.ponta_short.corretora): c for c in motor.candidatos(agora=agora)}


def test_suspeito_tambem_com_a_negativo_token_caro_no_long():
    # "X" da mexc vale ~2x o "X" da gate: long mexc / short gate da A ~ -50% e precisa ser suspeito.
    m = motor_falso(long_futuro=("mexc", "X", 2.0), short_futuro=("gate", "X", 1.0))
    c = _por_direcao(m)[("mexc", "gate")]
    assert c.spread_abertura_pct == pytest.approx(-50.0)
    assert c.suspeito


def test_as_duas_direcoes_do_mesmo_par_tem_a_mesma_marca_de_suspeito():
    m = motor_falso(long_futuro=("mexc", "X", 2.0), short_futuro=("gate", "X", 1.0))
    assert {direcao: c.suspeito for direcao, c in _por_direcao(m).items()} == {("mexc", "gate"): True,
                                                                               ("gate", "mexc"): True}


def test_a_negativo_com_razao_acima_do_limite_e_suspeito():
    # long a 1,31 e short a 1,00: A = -23,7% (antes nao era suspeito), mas a razao dos precos e 31% (> 30%).
    m = motor_falso(long_futuro=("mexc", "X", 1.31), short_futuro=("gate", "X", 1.0))
    c = _por_direcao(m)[("mexc", "gate")]
    assert c.spread_abertura_pct == pytest.approx(-23.66, abs=0.01)
    assert c.suspeito


def test_razao_abaixo_do_limite_nao_e_suspeita_nos_dois_sentidos():
    # Razao 1,25 (25%, abaixo dos 30%): A = -20% numa direcao e ~+24,7% na outra, nenhuma suspeita.
    m = motor_falso(long_futuro=("mexc", "X", 1.25), short_futuro=("gate", "X", 1.0))
    assert {direcao: c.suspeito for direcao, c in _por_direcao(m).items()} == {("mexc", "gate"): False,
                                                                               ("gate", "mexc"): False}


def test_limite_do_suspeito_vem_da_config():
    m = motor_falso(long_futuro=("mexc", "X", 1.25), short_futuro=("gate", "X", 1.0),
                    config=cfg_com(limite_suspeito_pct=20))
    assert all(c.suspeito for c in m.candidatos(agora=10))


def test_suspeito_com_a_negativo_nao_vira_sinal():
    # O cenario do revisor (suspeito_negativo.py): RP liquido positivo com A negativo, que agora fica oculto.
    m = motor_falso(long_futuro=("mexc", "X", 2.0), short_futuro=("gate", "X", 1.0))
    m.precarregar_amostra(chave_de(("X", "mexc", "futuro", "gate", "futuro")), 10.0, -50.0, 52.3, 1000.0, 1000.0)
    assert [e for e in m.ciclo(agora=10.0) if e.tipo == "novo"] == []


# ---------------------------------------------------------------------------
# I2 (a). Janelas recarregadas do banco ao ligar e ao trocar o periodo
# ---------------------------------------------------------------------------


def _motor_com_banco(banco, agora):
    return motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), agora=agora, banco=banco)


def test_ao_ligar_o_motor_recarrega_as_janelas_das_amostras_do_banco(banco):
    agora = 20_000.0
    banco.gravar_amostras([
        amostra(agora - 5 * 3600, f=-100.0, lote_long=1.0),  # fora do periodo de 4h: nao entra
        amostra(agora - 3600, f=-3.0, lote_long=400.0),
        amostra(agora - 1800, f=-3.0, lote_long=400.0),
    ])
    m = _motor_com_banco(banco, agora)
    m.recarregar_do_banco(agora)
    assert m.esperar_recarga(timeout=5)
    c = m.candidatos(agora=agora)[0]
    f_atual = c.spread_fechamento_pct
    assert c.metricas_periodo.fechamento_medio == pytest.approx((-3.0 - 3.0 + f_atual) / 3)
    assert c.ponta_long.lote_medio_usd == pytest.approx((400.0 + 400.0 + 1000.0) / 3)


def test_trocar_o_periodo_recarrega_as_janelas_com_o_periodo_novo(banco):
    agora = 50_000.0
    banco.gravar_amostras([amostra(agora - 3 * 3600, f=-9.0), amostra(agora - 1800, f=-1.0)])
    m = _motor_com_banco(banco, agora)
    c = m.candidatos(agora=agora)[0]  # sem recarga ao ligar: so a amostra de agora
    assert c.metricas_periodo.fechamento_medio == pytest.approx(c.spread_fechamento_pct)
    m.sessao = Sessao(filtros=Filtros(periodo_metricas_h=1))
    m.candidatos(agora=agora)  # o periodo mudou: as janelas recomecam e a recarga com 1h comeca
    assert m.esperar_recarga(timeout=5)
    c = m.candidatos(agora=agora)[0]
    # A amostra de 3h atras ficou fora do periodo novo; a de 30 min atras voltou do banco.
    assert c.metricas_periodo.fechamento_medio == pytest.approx((-1.0 + c.spread_fechamento_pct) / 2)
    assert m.gerenciador.periodo_h == 1


def test_recarga_junta_o_banco_com_as_amostras_novas_da_memoria(banco):
    agora = 30_000.0
    banco.gravar_amostras([amostra(agora - 600, f=-2.0)])
    m = _motor_com_banco(banco, agora)
    m.candidatos(agora=agora)  # amostra de agora so na memoria (candidatos nao grava)
    m.recarregar_do_banco(agora)
    assert m.esperar_recarga(timeout=5)
    c = m.candidatos(agora=agora + 1)[0]
    assert c.metricas_periodo.fechamento_medio == pytest.approx((-2.0 + c.spread_fechamento_pct) / 2)


def test_recarga_nao_segura_o_motor(banco):
    # Com o banco travado por outra leitura, o motor segue: a recarga roda numa thread propria.
    agora = 40_000.0
    banco.gravar_amostras([amostra(agora - 60, f=-2.0)])
    m = _motor_com_banco(banco, agora)
    soltar = threading.Event()

    def segurar():
        with banco._trava:
            soltar.wait(5)

    t = threading.Thread(target=segurar)
    t.start()
    try:
        time.sleep(0.05)
        inicio = time.perf_counter()
        m.recarregar_do_banco(agora)
        m.candidatos(agora=agora)  # o calculo (sem gravar: sem escritor, o ciclo gravaria direto no banco)
        assert time.perf_counter() - inicio < 0.5
        assert not m.esperar_recarga(timeout=0.1)  # ainda esperando o banco
    finally:
        soltar.set()
        t.join()
    assert m.esperar_recarga(timeout=5)


def test_rodar_pede_a_recarga_das_janelas_ao_ligar(tmp_path, monkeypatch):
    pedidos = []
    original = Motor.recarregar_do_banco

    def espiao(self, agora):
        pedidos.append(agora)
        return original(self, agora)

    monkeypatch.setattr(Motor, "recarregar_do_banco", espiao)
    exchanges = exchanges_falsas()
    asyncio.run(asyncio.wait_for(principal.rodar(config_servico(tmp_path), criar_exchange=exchanges.__getitem__,
                                                 relogio=lambda: 1000.0, ciclos=1), timeout=5))
    assert pedidos == [1000.0]


# ---------------------------------------------------------------------------
# I3. Banco fora do laco: esquema compacto, escritor numa thread, checkpoint, limites
# ---------------------------------------------------------------------------


def test_amostras_no_esquema_compacto_com_tabela_de_combinacoes(banco):
    sql = banco._conn.execute("SELECT sql FROM sqlite_master WHERE name = 'amostras'").fetchone()[0]
    assert "WITHOUT ROWID" in sql.upper() and "combo_id" in sql and "PRIMARY KEY (combo_id, ts)" in sql
    banco.gravar_amostras([amostra(1.0, chave="k1"), amostra(1.0, chave="k2"), amostra(6.0, chave="k1")])
    combos = dict(banco._conn.execute("SELECT chave, id FROM combos").fetchall())
    assert set(combos) == {"k1", "k2"}
    linhas = banco._conn.execute("SELECT combo_id, ts FROM amostras ORDER BY combo_id, ts").fetchall()
    assert [tuple(linha) for linha in linhas] == [(combos["k1"], 1.0), (combos["k1"], 6.0), (combos["k2"], 1.0)]
    assert [x["ts"] for x in banco.amostras("k1", desde=0)] == [1.0, 6.0]


def test_combinacoes_continuam_com_o_mesmo_numero_ao_reabrir(tmp_path):
    caminho = str(tmp_path / "c.db")
    b = Banco(caminho)
    b.gravar_amostras([amostra(1.0, chave="k1")])
    b.fechar()
    b = Banco(caminho)
    b.gravar_amostras([amostra(2.0, chave="k2"), amostra(3.0, chave="k1")])
    assert [x["ts"] for x in b.amostras("k1", desde=0)] == [1.0, 3.0]
    assert [x["ts"] for x in b.amostras("k2", desde=0)] == [2.0]
    b.fechar()


def test_lote_de_amostras_que_falha_nao_deixa_numero_de_combinacao_errado(banco, monkeypatch):
    # Se o lote volta atras (erro no meio), o numero novo da combinacao nao pode ficar guardado na memoria.
    with pytest.raises((sqlite3.InterfaceError, sqlite3.ProgrammingError)):
        banco.gravar_amostras([amostra(1.0, chave="k1"), ("k2", 1.0, object(), 0, 0, 0, 0, 0)])
    banco.gravar_amostras([amostra(2.0, chave="k3"), amostra(2.0, chave="k1")])
    assert [x["ts"] for x in banco.amostras("k1", desde=0)] == [2.0]
    assert [x["ts"] for x in banco.amostras("k3", desde=0)] == [2.0]
    assert banco.amostras("k2", desde=0) == []


ESQUEMA_ANTIGO = """
CREATE TABLE sinais (id TEXT PRIMARY KEY, modo TEXT NOT NULL, par TEXT NOT NULL, tipo TEXT NOT NULL,
    detectado_em TEXT NOT NULL, encerrado_em TEXT, rp_liquido_pct REAL, sinal_json TEXT NOT NULL);
CREATE INDEX idx_sinais_detectado_em ON sinais (detectado_em);
CREATE TABLE amostras (chave TEXT NOT NULL, ts REAL NOT NULL, a REAL, f REAL, preco_long REAL, preco_short REAL,
    lote_long REAL, lote_short REAL);
CREATE INDEX idx_amostras_chave_ts ON amostras (chave, ts);
CREATE TABLE grupos (nome TEXT PRIMARY KEY, filtros_json TEXT NOT NULL);
"""


def test_banco_antigo_e_convertido_uma_vez_ao_abrir_com_log(tmp_path, caplog):
    caminho = str(tmp_path / "antigo.db")
    con = sqlite3.connect(caminho)
    con.executescript(ESQUEMA_ANTIGO)
    con.executemany("INSERT INTO amostras VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [("k", 20.0, 2, -2, 100, 101, 1000, 900), ("outra", 10.0, 9, -9, 9, 9, 9, 9),
                     ("k", 10.0, 1, -1, 100, 101, 1000, 900)])
    sinal = {"id": "s1", "par": "BTC/USDT", "tipo": "spot_futuro", "modo": "lucro", "rp_liquido_pct": 0.3,
             "detectado_em": "2026-10-20T10:00:00.000Z", "encerrado_em": "2026-10-20T10:01:00.000Z"}
    con.execute("INSERT INTO sinais VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("s1", "lucro", "BTC/USDT", "spot_futuro", sinal["detectado_em"], sinal["encerrado_em"], 0.3,
                 json.dumps(sinal)))
    con.execute("INSERT INTO grupos VALUES ('Farm', '{\"volume_min_usd\": 500}')")
    con.commit()
    con.close()

    with caplog.at_level(logging.INFO, logger="scanner"):
        b = Banco(caminho)
    try:
        lidas = [(x["ts"], x["a"], x["lote_short"]) for x in b.amostras("k", desde=0)]
        assert lidas == [(10.0, 1, 900), (20.0, 2, 900)]
        assert [x["a"] for x in b.amostras("outra", desde=0)] == [9]
        [s] = b.sinais_do_dia("2026-10-20")
        assert s["id"] == "s1" and s["comparacao"] is False and s["sinal"] == sinal
        assert b.listar_grupos() == {"Farm": {"volume_min_usd": 500}}
        sql = b._conn.execute("SELECT sql FROM sqlite_master WHERE name = 'amostras'").fetchone()[0]
        assert "WITHOUT ROWID" in sql.upper()
    finally:
        b.fechar()
    mensagens = [r.getMessage() for r in caplog.records]
    assert any("formato antigo" in m and "3 amostras" in m for m in mensagens)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="scanner"):
        Banco(caminho).fechar()
    assert not any("formato antigo" in r.getMessage() for r in caplog.records)  # so uma vez


def test_cache_maior_e_checkpoint_automatico_com_folga(banco):
    assert banco._conn.execute("PRAGMA cache_size").fetchone()[0] == modulo_banco.CACHE_KIB * -1
    assert banco._conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == modulo_banco.PAGINAS_AUTOCHECKPOINT


def test_amostras_nunca_alem_do_periodo_maximo(tmp_path):
    b = Banco(str(tmp_path / "p.db"), periodo_max_s=3600, relogio=lambda: 10_000.0)
    b.gravar_amostras([amostra(ts) for ts in (5000.0, 7000.0, 9000.0)])
    assert [x["ts"] for x in b.amostras(CHAVE_TEXTO, desde=0)] == [7000.0, 9000.0]
    assert [x["ts"] for x in b.amostras(CHAVE_TEXTO, desde=8000.0)] == [9000.0]
    b.fechar()


def test_historico_da_api_nao_le_alem_da_retencao(tmp_path):
    config = cfg_com(retencao_dias=1)
    agora = 1_000_000.0
    b = Banco(str(tmp_path / "h.db"), periodo_max_s=config.retencao_dias * 86400, relogio=lambda: agora)
    b.gravar_amostras([amostra(agora - 2 * 86400), amostra(agora - 3600)])
    cliente = ClienteDoPainel(criar_app(motor_falso(config=config), b, config, relogio=lambda: agora))
    r = cliente.get(f"/api/historico/{CHAVE_TEXTO}", params={"periodo_h": 1000})
    assert r.status_code == 200 and [x["ts"] for x in r.json()["amostras"]] == [agora - 3600]
    b.fechar()


def test_tamanho_do_banco_em_bytes(banco):
    banco.gravar_amostras([amostra(float(ts)) for ts in range(100)])
    assert banco.tamanho_bytes() > 0


def test_checkpoint_passivo_nao_da_erro(banco):
    banco.gravar_amostras([amostra(1.0)])
    banco.checkpoint()


# --- Escritor (thread propria) ----------------------------------------------


def _sinal(id_="s1", **campos) -> dict:
    sinal = {"id": id_, "par": "BTC/USDT", "tipo": "spot_futuro", "modo": "lucro", "rp_liquido_pct": 0.4,
             "detectado_em": "2026-10-20T15:00:00.000Z", "atualizado_em": "2026-10-20T15:00:00.000Z",
             "encerrado_em": None}
    sinal.update(campos)
    return sinal


def test_escritor_grava_eventos_amostras_sessao_e_corretoras_na_thread_dele(banco):
    threads = []
    original = banco.gravar_amostras

    def anotar(amostras):
        threads.append(threading.current_thread().name)
        return original(amostras)

    banco.gravar_amostras = anotar
    escritor = EscritorBanco(banco)
    escritor.iniciar()
    escritor.gravar_eventos([Evento("novo", _sinal(), registrar_historico=True)])
    escritor.gravar_amostras([amostra(1.0), amostra(6.0)])
    escritor.salvar_sessao({"modo": "farm", "filtros": {}, "comparacao": False})
    escritor.registrar_corretoras("2026-10-20", {"gate": {"atraso_medio_ms": 10.0, "reconexoes": 1}})
    assert escritor.descarregar(timeout=5)
    assert [s["id"] for s in banco.sinais_do_dia("2026-10-20")] == ["s1"]
    assert len(banco.amostras(CHAVE_TEXTO, desde=0)) == 2
    assert banco.ler_sessao() == {"modo": "farm", "filtros": {}, "comparacao": False}
    assert banco.corretoras_do_dia("2026-10-20")["gate"]["reconexoes"] == 1
    assert threads and all(nome == "escritor do banco" for nome in threads)
    escritor.parar()


def test_motor_com_escritor_nao_espera_o_banco_no_ciclo(banco):
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5, banco=banco)
    escritor = EscritorBanco(banco)
    escritor.iniciar()
    m.escritor = escritor
    soltar = threading.Event()

    def segurar():  # uma leitura pesada (ex.: grafico de 7 dias) segurando o banco
        with banco._trava:
            soltar.wait(5)

    t = threading.Thread(target=segurar)
    t.start()
    try:
        time.sleep(0.05)
        inicio = time.perf_counter()
        eventos = m.ciclo(agora=10.0)
        assert time.perf_counter() - inicio < 0.5
        assert [e.tipo for e in eventos] == ["novo"]
    finally:
        soltar.set()
        t.join()
    assert escritor.descarregar(timeout=5)
    assert [r["par"] for r in banco.sinais_do_dia("1970-01-01")] == ["BTC/USDT"]
    escritor.parar()


def test_fila_cheia_descarta_amostras_com_aviso_mas_nunca_eventos(banco, caplog):
    escritor = EscritorBanco(banco, max_amostras_na_fila=3)  # sem iniciar: nada sai da fila ainda
    escritor.gravar_amostras([amostra(1.0), amostra(2.0)])
    with caplog.at_level(logging.WARNING, logger="scanner"):
        escritor.gravar_amostras([amostra(3.0), amostra(4.0)])  # so cabe uma
        escritor.gravar_eventos([Evento("novo", _sinal(f"s{i}"), registrar_historico=True) for i in range(10)])
    avisos = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("fila do banco cheia" in a and "1 amostras descartadas" in a for a in avisos)
    escritor.iniciar()
    escritor.parar()  # grava tudo o que estava na fila antes de parar
    assert [x["ts"] for x in banco.amostras(CHAVE_TEXTO, desde=0)] == [1.0, 2.0, 3.0]
    assert len(banco.sinais_do_dia("2026-10-20")) == 10


def test_escritor_segue_depois_de_um_erro_do_banco(banco, caplog, monkeypatch):
    original = banco.gravar_amostras
    falhas = [True]

    def falhar_uma_vez(amostras):
        if falhas:
            falhas.pop()
            raise sqlite3.OperationalError("disco cheio")
        return original(amostras)

    monkeypatch.setattr(banco, "gravar_amostras", falhar_uma_vez)
    escritor = EscritorBanco(banco)
    escritor.iniciar()
    with caplog.at_level(logging.ERROR, logger="scanner"):
        escritor.gravar_amostras([amostra(1.0)])
        assert escritor.descarregar(timeout=5)
    assert any("nao consegui gravar 1 amostras" in r.getMessage() for r in caplog.records)
    escritor.gravar_amostras([amostra(2.0)])
    escritor.parar()
    assert [x["ts"] for x in banco.amostras(CHAVE_TEXTO, desde=0)] == [2.0]


def test_evento_recusado_pelo_banco_vai_ao_log_pelo_escritor(banco, caplog):
    escritor = EscritorBanco(banco)
    escritor.iniciar()
    with caplog.at_level(logging.ERROR, logger="scanner"):
        escritor.gravar_eventos([Evento("novo", _sinal("ruim", rp_liquido_pct=float("nan")), True),
                                 Evento("novo", _sinal("bom"), True)])
        assert escritor.descarregar(timeout=5)
    escritor.parar()
    assert [s["id"] for s in banco.sinais_do_dia("2026-10-20")] == ["bom"]
    assert any("id=ruim" in r.getMessage() for r in caplog.records)


def _esperar(condicao, limite_s=3.0) -> bool:
    fim = time.monotonic() + limite_s
    while not condicao():
        if time.monotonic() > fim:
            return False
        time.sleep(0.01)
    return True


def test_amostras_esperam_juntar_o_lote_mas_eventos_gravam_logo(banco):
    escritor = EscritorBanco(banco, intervalo_lote_amostras_s=60)
    escritor.iniciar()
    escritor.gravar_amostras([amostra(1.0)])
    escritor.gravar_eventos([Evento("novo", _sinal(), registrar_historico=True)])
    assert _esperar(lambda: len(banco.sinais_do_dia("2026-10-20")) == 1)  # o evento nao espera
    assert banco.amostras(CHAVE_TEXTO, desde=0) == [] and escritor.pendentes() == 1  # a amostra espera o lote
    assert escritor.descarregar(timeout=5)  # quem precisa ler logo pede: grava ja
    assert len(banco.amostras(CHAVE_TEXTO, desde=0)) == 1
    escritor.gravar_amostras([amostra(2.0)])
    escritor.parar()  # ao parar, grava o que estava juntando
    assert len(banco.amostras(CHAVE_TEXTO, desde=0)) == 2


def test_lote_de_amostras_vai_sozinho_quando_completa_o_tempo(banco):
    escritor = EscritorBanco(banco, intervalo_lote_amostras_s=0.2)
    escritor.iniciar()
    escritor.gravar_amostras([amostra(1.0)])
    assert _esperar(lambda: len(banco.amostras(CHAVE_TEXTO, desde=0)) == 1)
    escritor.parar()


def test_recarga_grava_antes_as_amostras_que_estavam_na_fila(banco):
    agora = 60_000.0
    escritor = EscritorBanco(banco, intervalo_lote_amostras_s=60)
    escritor.iniciar()
    m = _motor_com_banco(banco, agora)
    m.escritor = escritor
    escritor.gravar_amostras([amostra(agora - 30, f=-4.0)])  # ainda na fila do escritor
    m.recarregar_do_banco(agora)
    assert m.esperar_recarga(timeout=5)
    c = m.candidatos(agora=agora)[0]
    assert c.metricas_periodo.fechamento_medio == pytest.approx((-4.0 + c.spread_fechamento_pct) / 2)
    escritor.parar()


def test_escritor_faz_checkpoint_de_tempos_em_tempos(banco):
    relogio = [0.0]
    pedidos = []
    banco.checkpoint = lambda: pedidos.append(relogio[0])
    escritor = EscritorBanco(banco, intervalo_checkpoint_s=30, relogio=lambda: relogio[0])
    escritor.iniciar()
    escritor.gravar_amostras([amostra(1.0)])
    assert escritor.descarregar(timeout=5)
    assert pedidos == []  # ainda nao passou o intervalo
    relogio[0] = 31.0
    escritor.gravar_amostras([amostra(2.0)])
    assert escritor.descarregar(timeout=5)
    escritor.parar()
    assert pedidos == [31.0]


def test_rodar_grava_pelo_escritor_e_fecha_tudo(tmp_path):
    exchanges = exchanges_falsas()
    config = config_servico(tmp_path)
    asyncio.run(asyncio.wait_for(principal.rodar(config, criar_exchange=exchanges.__getitem__,
                                                 relogio=lambda: 1000.0, ciclos=2), timeout=5))
    b = Banco(config.banco)
    try:
        assert [r["par"] for r in b.sinais_do_dia("1970-01-01")] == ["BTC/USDT"]
        assert len(b.amostras(texto_da_chave(CHAVE), desde=0)) == 1
    finally:
        b.fechar()
    assert not [t for t in threading.enumerate() if t.name == "escritor do banco"]  # a thread terminou


# --- Linha de recursos -------------------------------------------------------


def test_linha_de_recursos_com_tempo_do_ciclo_tamanho_e_fila_do_banco():
    linha = logs.linha_de_recursos(None, None, sinais_ativos=0, historicos=0, ciclo=(120.04, 310.26),
                                   banco_bytes=512 * 1024 * 1024, fila_banco=7)
    assert "ciclo medio 120.0 ms (maior 310.3 ms)" in linha
    assert "banco 512.0 MB" in linha and "fila do banco 7" in linha
    sem = logs.linha_de_recursos(None, None, sinais_ativos=0, historicos=0, ciclo=None, banco_bytes=None)
    assert "ciclo -" in sem and "banco indisponivel" in sem


def test_tempos_do_ciclo_dao_media_e_maior_e_zeram():
    tempos = logs.TemposDoCiclo()
    assert tempos.tirar() is None
    for ms in (100.0, 300.0, 200.0):
        tempos.registrar(ms)
    assert tempos.tirar() == pytest.approx((200.0, 300.0))
    assert tempos.tirar() is None


def test_registrar_recursos_mostra_o_ciclo_e_o_banco(banco, caplog):
    from tests.fabricas import EsperaFalsa

    m = motor_falso()
    tempos = logs.TemposDoCiclo()
    tempos.registrar(150.0)
    espera = EsperaFalsa(parar_depois=1)

    async def cenario():
        with pytest.raises(asyncio.CancelledError):
            await principal._registrar_recursos(m, {}, intervalo_s=300, esperar=espera, tempos=tempos, banco=banco)

    with caplog.at_level(logging.INFO, logger="scanner"):
        asyncio.run(cenario())
    [linha] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("recursos")]
    assert "ciclo medio 150.0 ms" in linha and "banco " in linha and "MB" in linha


def test_laco_do_motor_mede_o_tempo_de_cada_ciclo():
    m = motor_falso()
    tempos = logs.TemposDoCiclo()
    asyncio.run(principal._laco_do_motor(m, {}, cfg_com(ciclo_ms=10), lambda: 10.0, ciclos=3, tempos=tempos))
    media, maior = tempos.tirar()
    assert 0 <= media <= maior


# ---------------------------------------------------------------------------
# I4. Log pela fila: o laco nunca espera o terminal nem o arquivo
# ---------------------------------------------------------------------------


@pytest.fixture
def logs_limpos():
    raiz, do_scanner = logging.getLogger(), logging.getLogger("scanner")
    niveis = (raiz.level, do_scanner.level)
    yield
    logs.desligar_logs()
    raiz.setLevel(niveis[0])
    do_scanner.setLevel(niveis[1])


class TerminalTravado(io.StringIO):
    """Um terminal cuja escrita espera (como o console do Windows com texto selecionado)."""

    def __init__(self):
        super().__init__()
        self.solta = threading.Event()

    def write(self, texto):
        self.solta.wait(5)
        return super().write(texto)


def test_log_nao_espera_o_terminal_travado(tmp_path, logs_limpos, monkeypatch):
    terminal = TerminalTravado()
    monkeypatch.setattr(sys, "stderr", terminal)
    logs.configurar_logs("normal", str(tmp_path), dias=3)
    inicio = time.perf_counter()
    for i in range(20):
        logging.getLogger("scanner.teste").info("linha %d", i)
    assert time.perf_counter() - inicio < 0.5  # ninguem esperou o terminal
    terminal.solta.set()
    logs.descarregar_logs()
    assert "linha 19" in terminal.getvalue()


def test_raiz_tem_so_a_fila_e_o_ouvinte_tem_terminal_e_arquivo(tmp_path, logs_limpos):
    logs.configurar_logs("normal", str(tmp_path), dias=3)
    do_scanner = [h for h in logging.getLogger().handlers if getattr(h, "do_scanner", False)]
    assert len(do_scanner) == 1 and isinstance(do_scanner[0], logging.handlers.QueueHandler)
    assert len(logs.handlers_do_scanner()) == 2


def test_traceback_chega_ao_arquivo_pela_fila(tmp_path, logs_limpos):
    logs.configurar_logs("normal", str(tmp_path), dias=3)
    try:
        raise ValueError("defeito de teste")
    except ValueError:
        logging.getLogger("scanner.teste").exception("deu erro")
    logs.descarregar_logs()
    texto = next(tmp_path.glob("scanner-*.log")).read_text(encoding="utf-8")
    assert "deu erro" in texto and "ValueError: defeito de teste" in texto


def test_desligar_logs_grava_o_que_estava_na_fila(tmp_path, logs_limpos):
    logs.configurar_logs("normal", str(tmp_path), dias=3)
    for i in range(200):
        logging.getLogger("scanner.teste").info("linha %d", i)
    logs.desligar_logs()
    assert "linha 199" in next(tmp_path.glob("scanner-*.log")).read_text(encoding="utf-8")


def test_linha_so_no_arquivo_nao_vai_ao_terminal(tmp_path, logs_limpos, capsys):
    logs.configurar_logs("normal", str(tmp_path), dias=3)
    logging.getLogger("scanner.teste").error("traceback comprido", extra={"so_no_arquivo": True})
    logging.getLogger("scanner.teste").error("linha normal")
    logs.descarregar_logs()
    terminal = capsys.readouterr().err
    assert "linha normal" in terminal and "traceback comprido" not in terminal
    assert "traceback comprido" in next(tmp_path.glob("scanner-*.log")).read_text(encoding="utf-8")


def test_readme_avisa_da_edicao_rapida_do_console_do_windows():
    texto = (RAIZ / "README.md").read_text(encoding="utf-8")
    assert "Edição Rápida" in texto


# ---------------------------------------------------------------------------
# I5. Etapa 2 nao esconde o Farm nem o "sempre mostrar"
# ---------------------------------------------------------------------------


def _motor_farm(modo="farm", filtros=None, **campos):
    e = EstadoMercados()
    config = cfg_com(corretoras=["gate", "lighter", "mexc"], corretoras_farm=["lighter"], **campos)
    m = Motor(config, e, DadosLentosFalso())
    m.sessao = Sessao(modo=modo, filtros=filtros or Filtros())
    return m, e


def test_modo_farm_abre_livro_da_combinacao_de_farm_com_o_minimo_de_farm():
    m, e = _motor_farm()
    ticker(e, "lighter", "futuro", "BTC", 99.99, 100.0, 10.0)
    ticker(e, "gate", "futuro", "BTC", 100.1, 100.11, 10.0)  # long lighter / short gate: A = 0,1% (< 0,3%)
    assert m.selecao_livros(agora=10) == {"lighter": {"BTC/USDT:USDT"}, "gate": {"BTC/USDT:USDT"}}


def test_minimo_de_farm_vale_tambem_com_a_ponta_de_farm_no_short():
    m, e = _motor_farm()
    ticker(e, "gate", "futuro", "BTC", 99.99, 100.0, 10.0)
    ticker(e, "lighter", "futuro", "BTC", 99.9, 99.91, 10.0)  # long gate / short lighter: A = -0,1% (>= -0,3%)
    assert m.selecao_livros(agora=10) == {"lighter": {"BTC/USDT:USDT"}, "gate": {"BTC/USDT:USDT"}}


def test_minimo_de_farm_vem_da_config():
    m, e = _motor_farm(spread_min_livro_farm_pct=0.2)
    ticker(e, "lighter", "futuro", "BTC", 99.99, 100.0, 10.0)
    ticker(e, "gate", "futuro", "BTC", 100.1, 100.11, 10.0)  # A = 0,1% (< 0,2%)
    assert m.selecao_livros(agora=10) == {}


def test_combinacao_sem_ponta_de_farm_segue_o_minimo_normal_no_modo_farm():
    m, e = _motor_farm()
    ticker(e, "mexc", "futuro", "BTC", 99.99, 100.0, 10.0)
    ticker(e, "gate", "futuro", "BTC", 100.1, 100.11, 10.0)  # mexc x gate: A = 0,1%, nenhuma de farm
    assert m.selecao_livros(agora=10) == {}


def test_modo_lucro_nao_usa_o_minimo_de_farm():
    m, e = _motor_farm(modo="lucro")
    ticker(e, "lighter", "futuro", "BTC", 99.99, 100.0, 10.0)
    ticker(e, "gate", "futuro", "BTC", 100.1, 100.11, 10.0)
    assert m.selecao_livros(agora=10) == {}


def test_sempre_mostrar_sempre_abre_o_livro_da_moeda():
    m, e = _motor_farm(modo="lucro", filtros=Filtros(sempre_mostrar=["btc"]))
    ticker(e, "mexc", "futuro", "BTC", 99.99, 100.0, 10.0)
    ticker(e, "gate", "futuro", "BTC", 99.98, 99.99, 10.0)  # A negativo nas duas direcoes
    ticker(e, "mexc", "futuro", "ETH", 99.99, 100.0, 10.0)
    ticker(e, "gate", "futuro", "ETH", 99.98, 99.99, 10.0)  # ETH nao esta no "sempre mostrar"
    assert m.selecao_livros(agora=10) == {"mexc": {"BTC/USDT:USDT"}, "gate": {"BTC/USDT:USDT"}}


def _cenario_etapa2_esconde(sessao: Sessao) -> list[dict]:
    """O cenario do revisor (etapa2_esconde.py): lighter x gate com A ~0,1%, livros abertos so pela etapa 2."""
    from scanner.modelos import Livro

    class Dados:
        def funding(self, corretora, ativo):
            return (0.0, 1)

        def taxa_taker(self, corretora, mercado):
            return 0.0 if corretora == "lighter" else 0.02

    config = Config(corretoras=["gate", "lighter"], corretoras_farm=["lighter"])
    estado = EstadoMercados()
    motor = Motor(config, estado, Dados())
    motor.sessao = sessao
    meio = 0.0001
    precos = {"lighter": 100.0, "gate": 100.14}
    for agora in range(0, 30, 5):
        for corretora, preco in precos.items():
            simbolo = "BTC/USDT:USDT"
            estado.atualizar_ticker(corretora, "futuro", "BTC", preco * (1 - meio), preco * (1 + meio), agora, 5e8, 0.0,
                                    simbolo_original=simbolo, cotacao="USDT")
            estado.registrar_mensagem(corretora, agora)
            if simbolo in motor.selecao_livros(agora).get(corretora, set()):
                q = 5000 / preco
                estado.atualizar_livro(Livro(corretora, "futuro", simbolo, [(preco * (1 - meio), q)],
                                             [(preco * (1 + meio), q)], agora, None), ativo="BTC")
        motor.ciclo(agora)
    return motor.sinais_ativos()


def test_cenario_do_revisor_farm_aparece_pela_etapa_2():
    sinais = _cenario_etapa2_esconde(Sessao(modo="farm"))
    assert len(sinais) == 2 and all(s["rp_liquido_pct"] >= -0.1 for s in sinais)


def test_cenario_do_revisor_sempre_mostrar_aparece_pela_etapa_2():
    assert len(_cenario_etapa2_esconde(Sessao(modo="lucro", filtros=Filtros(sempre_mostrar=["BTC"])))) == 2


# ---------------------------------------------------------------------------
# M1. Sinais abertos ao parar e ao religar depois de uma queda
# ---------------------------------------------------------------------------


def test_rodar_encerra_os_sinais_ativos_ao_parar(tmp_path):
    exchanges = exchanges_falsas()
    config = config_servico(tmp_path)
    asyncio.run(asyncio.wait_for(principal.rodar(config, criar_exchange=exchanges.__getitem__,
                                                 relogio=lambda: 1000.0, ciclos=2), timeout=5))
    b = Banco(config.banco)
    try:
        [r] = b.sinais_do_dia("1970-01-01")
        assert r["encerrado_em"] == "1970-01-01T00:16:40.000Z" == r["sinal"]["encerrado_em"]
    finally:
        b.fechar()


def test_motor_encerra_os_sinais_ativos(banco):
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5, banco=banco)
    m.ciclo(agora=10.0)
    eventos = m.encerrar_sinais(agora=12.0)
    assert [e.tipo for e in eventos] == ["encerrado"] and m.sinais_ativos() == []
    assert banco.sinais_do_dia("1970-01-01")[0]["encerrado_em"] == "1970-01-01T00:00:12.000Z"


def _sinal_aberto(detectado: float, atualizado: float | None = None) -> dict:
    return {
        "id": f"BTC-spot_futuro-gate-bitget-{int(detectado)}", "par": "BTC/USDT", "tipo": "spot_futuro",
        "modo": "lucro", "rp_liquido_pct": 0.3, "detectado_em": iso_utc(detectado),
        "atualizado_em": iso_utc(atualizado if atualizado is not None else detectado), "encerrado_em": None,
        "ponta_long": {"corretora": "gate", "mercado": "spot"},
        "ponta_short": {"corretora": "bitget", "mercado": "futuro"},
    }


def test_fechar_abertos_usa_a_ultima_amostra_conhecida_do_par(banco):
    t0 = INICIO_20_OUT + 10 * 3600
    banco.gravar_evento(Evento("novo", _sinal_aberto(t0), registrar_historico=True))
    banco.gravar_amostras([amostra(t0 + ts) for ts in (0.0, 5.0, 30.0)])
    assert banco.fechar_abertos() == 1
    [s] = banco.sinais_do_dia("2026-10-20")
    assert s["encerrado_em"] == iso_utc(t0 + 30) == s["sinal"]["encerrado_em"]
    assert banco.fechar_abertos() == 0  # nada mais aberto


def test_fechar_abertos_sem_amostra_usa_o_atualizado_em(banco):
    t0 = INICIO_20_OUT + 10 * 3600
    banco.gravar_evento(Evento("novo", _sinal_aberto(t0, atualizado=t0 + 7), registrar_historico=True))
    assert banco.fechar_abertos() == 1
    assert banco.sinais_do_dia("2026-10-20")[0]["encerrado_em"] == iso_utc(t0 + 7)


def test_rodar_fecha_ao_ligar_o_que_ficou_aberto_com_log(tmp_path, caplog):
    config = config_servico(tmp_path)
    b = Banco(config.banco)
    b.gravar_evento(Evento("novo", {**_sinal_aberto(500.0), "id": "caiu", "par": "ETH/USDT"}, registrar_historico=True))
    b.fechar()
    exchanges = exchanges_falsas()
    with caplog.at_level(logging.INFO, logger="scanner"):
        asyncio.run(asyncio.wait_for(principal.rodar(config, criar_exchange=exchanges.__getitem__,
                                                     relogio=lambda: 1000.0, ciclos=1), timeout=5))
    b = Banco(config.banco)
    try:
        caiu = [r for r in b.sinais_do_dia("1970-01-01") if r["id"] == "caiu"][0]
        assert caiu["encerrado_em"] == iso_utc(500.0)
    finally:
        b.fechar()
    assert any("ficaram abertos" in r.getMessage() for r in caplog.records)


def test_limpeza_apaga_o_registro_fechado_depois_da_queda(banco):
    # Antes da correcao, o registro aberto nunca era apagado. Agora ele e fechado e a limpeza (so amostras,
    # M10) deixa o historico; o que importa e que ele nao conta mais como aberto no resumo.
    t0 = INICIO_20_OUT + 10 * 3600
    banco.gravar_evento(Evento("novo", _sinal_aberto(t0), registrar_historico=True))
    banco.fechar_abertos()
    assert resumo_do_dia(banco, "2026-10-20")["lucro"]["abertos"] == 0


# ---------------------------------------------------------------------------
# M2. Modo comparacao marcado no banco e separado no resumo do dia
# ---------------------------------------------------------------------------


def test_sinal_nascido_em_comparacao_fica_marcado_no_banco_sem_mudar_o_json(banco):
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5, banco=banco)
    m.sessao = Sessao(comparacao=True)
    [evento] = m.ciclo(agora=10.0)
    assert evento.comparacao is True
    [s] = banco.sinais_do_dia("1970-01-01")
    assert s["comparacao"] is True and "comparacao" not in s["sinal"]


def test_marca_de_comparacao_e_a_do_nascimento(banco):
    banco.gravar_evento(Evento("novo", _sinal(), registrar_historico=True, comparacao=True))
    banco.gravar_evento(Evento("encerrado", _sinal(encerrado_em="2026-10-20T15:01:00.000Z"),
                               registrar_historico=True, comparacao=False))
    assert banco.sinais_do_dia("2026-10-20")[0]["comparacao"] is True


def test_gerenciador_marca_os_eventos_com_a_comparacao():
    g = GerenciadorSinais(anti_repeticao_s=60)
    [e] = g.processar([candidato()], agora=10.0, comparacao=True)
    assert e.comparacao is True
    [e] = g.processar([], agora=11.0)
    assert e.comparacao is False


def test_resumo_do_dia_separa_a_comparacao_dos_modos(banco):
    t0 = INICIO_20_OUT + 10 * 3600
    for i, (rp, comp) in enumerate([(0.5, False), (-0.2, True), (-0.3, True)]):
        sinal = {**_sinal(f"s{i}", rp_liquido_pct=rp), "detectado_em": iso_utc(t0 + i)}
        banco.gravar_evento(Evento("novo", sinal, registrar_historico=True, comparacao=comp))
        banco.gravar_evento(Evento("encerrado", {**sinal, "encerrado_em": iso_utc(t0 + i + 10)},
                                   registrar_historico=True, comparacao=comp))
    resumo = resumo_do_dia(banco, "2026-10-20")
    assert resumo["lucro"]["quantidade_sinais"] == 1 and resumo["lucro"]["rp_liquido_medio_pct"] == pytest.approx(0.5)
    assert resumo["comparacao"]["quantidade_sinais"] == 2
    assert resumo["comparacao"]["rp_liquido_medio_pct"] == pytest.approx(-0.25)
    assert any("comparacao" in linha for linha in linhas_do_resumo(resumo))


# ---------------------------------------------------------------------------
# M3. Livro convertido so com o ticker do MESMO simbolo
# ---------------------------------------------------------------------------


def test_livro_e_ticker_de_simbolos_diferentes_nao_geram_candidato():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0))
    assert len(m.candidatos(agora=10)) == 1
    # A recarga trocou o vencedor da chave (BTC/USDT -> BTC/EUR): o ticker ja e do simbolo novo, o livro nao.
    m.estado.atualizar_ticker("gate", "spot", "BTC", 92.0, 92.1, 10.0, simbolo_original="BTC/EUR", cotacao="EUR")
    assert m.candidatos(agora=10) == []


# ---------------------------------------------------------------------------
# M5, M6, M9. Config
# ---------------------------------------------------------------------------


def _escrever(tmp_path, texto, encoding="utf-8"):
    arq = tmp_path / "config.yaml"
    arq.write_bytes(texto.encode(encoding))
    return str(arq)


def test_taxas_de_corretora_fora_de_corretoras_da_erro(tmp_path):
    with pytest.raises(ErroConfig, match="taxas: bitgte nao esta em corretoras"):
        carregar_config(_escrever(tmp_path, "corretoras: [bitget]\ntaxas:\n  bitgte:\n    futuro: 0.02\n"))


def test_config_fora_de_utf8_da_erro_claro(tmp_path):
    caminho = _escrever(tmp_path, "# comentário com acento\ncorretoras: [bitget]\n", encoding="cp1252")
    with pytest.raises(ErroConfig, match="UTF-8"):
        carregar_config(caminho)


def test_main_validar_com_config_fora_de_utf8_mostra_erro_e_nao_traceback(tmp_path, capsys):
    caminho = _escrever(tmp_path, "# comentário\ncorretoras: [bitget]\n", encoding="cp1252")
    assert principal.main(["--config", caminho, "--validar"]) == 1
    erro = capsys.readouterr().err
    assert "Erro na configuracao" in erro and "Traceback" not in erro


def test_campos_novos_da_config_com_os_padroes(tmp_path):
    c = carregar_config(_escrever(tmp_path, "corretoras: [bitget]\n"))
    assert c.spread_min_livro_farm_pct == -0.3
    assert c.validade_funding_s == 900


def test_validade_do_funding_precisa_passar_do_intervalo_dos_dados_lentos(tmp_path):
    with pytest.raises(ErroConfig, match="validade_funding_s"):
        carregar_config(_escrever(tmp_path, "validade_funding_s: 60\nintervalo_dados_lentos_s: 60\n"))
    with pytest.raises(ErroConfig, match="validade_funding_s"):
        carregar_config(_escrever(tmp_path, "validade_funding_s: 0\n"))
    assert carregar_config(_escrever(tmp_path, "validade_funding_s: 61\n")).validade_funding_s == 61


class DadosComHorario(DadosLentosFalso):
    """DadosLentosFalso que tambem diz quando o funding chegou (como o DadosLentos de verdade)."""

    def __init__(self, *argumentos, recebido_em=0.0, **nomeados):
        super().__init__(*argumentos, **nomeados)
        self.recebido_em = recebido_em

    def funding_recebido_em(self, corretora, ativo):
        return self.recebido_em if (corretora, ativo) in self.fundings else None


def test_funding_vencido_fica_desconhecido_e_avisa(caplog):
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), funding_short=(0.05, 8),
                    agora=10_000.0)
    m.dados_lentos = DadosComHorario(taxas=m.dados_lentos.taxas, fundings=m.dados_lentos.fundings,
                                     recebido_em=10_000.0 - cfg.validade_funding_s + 1)
    assert m.candidatos(agora=10_000.0)[0].ponta_short.funding_pct == 0.05  # ainda valido
    m.dados_lentos.recebido_em = 10_000.0 - cfg.validade_funding_s - 1
    with caplog.at_level(logging.WARNING, logger="scanner.motor"):
        c = m.candidatos(agora=10_000.0)[0]
        m.candidatos(agora=10_000.0)
    assert c.ponta_short.funding_pct is None and c.ponta_short.funding_intervalo_h is None
    assert len([r for r in caplog.records if "funding" in r.getMessage() and "bitget" in r.getMessage()]) == 1


def test_dados_lentos_guarda_quando_o_funding_chegou():
    agora = [123.0]
    d = DadosLentos(relogio=lambda: agora[0])
    d.receber_funding("lighter", "BTC", 0.001, 1)
    assert d.funding_recebido_em("lighter", "BTC") == 123.0
    assert d.funding_recebido_em("lighter", "ETH") is None
    gate = CorretoraFalsa([market_ccxt("BTC/USDT:USDT", "BTC")], funding={"BTC/USDT:USDT": {"fundingRate": 0.0001}})
    agora[0] = 200.0
    asyncio.run(d.atualizar({"gate": gate}))
    assert d.funding("gate", "BTC") == (0.01, 8) and d.funding_recebido_em("gate", "BTC") == 200.0


# ---------------------------------------------------------------------------
# M7. Erro inesperado no rodar vai para o arquivo de log
# ---------------------------------------------------------------------------


def test_erro_inesperado_no_rodar_vai_para_o_log_com_traceback(tmp_path, monkeypatch, caplog):
    arq = tmp_path / "config.yaml"
    arq.write_text("corretoras: [bitget]\n")

    async def rodar_com_defeito(config):
        raise RuntimeError("defeito inesperado")

    monkeypatch.setattr(principal, "rodar", rodar_com_defeito)
    monkeypatch.setattr(principal, "configurar_log", lambda cfg: None)
    with caplog.at_level(logging.ERROR, logger="scanner"):
        assert principal.main(["--config", str(arq)]) == 1
    [r] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert r.exc_info is not None and "defeito inesperado" in str(r.exc_info[1])


# ---------------------------------------------------------------------------
# M11 e versoes fixas (item 11)
# ---------------------------------------------------------------------------


def test_gitignore_tem_os_arquivos_do_wal():
    linhas = (RAIZ / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.db-wal" in linhas and "*.db-shm" in linhas


def test_versoes_fixas_nos_requirements():
    principal_ = (RAIZ / "requirements.txt").read_text(encoding="utf-8").split()
    dev = (RAIZ / "requirements-dev.txt").read_text(encoding="utf-8").split()
    assert principal_ == ["ccxt==4.5.89", "fastapi==0.159.1", "uvicorn==0.54.0", "PyYAML==6.0.3", "websockets==17.3"]
    assert dev == ["-r", "requirements.txt", "pytest==9.1.1", "httpx==0.28.1"]


# ---------------------------------------------------------------------------
# Minor 7 da interface: sessao persistente
# ---------------------------------------------------------------------------


def test_banco_guarda_e_le_a_ultima_sessao(tmp_path):
    caminho = str(tmp_path / "s.db")
    b = Banco(caminho)
    assert b.ler_sessao() is None
    b.salvar_sessao({"modo": "lucro", "filtros": {}, "comparacao": False})
    b.salvar_sessao({"modo": "farm", "filtros": {"volume_min_usd": 500}, "comparacao": True})
    b.fechar()
    b = Banco(caminho)
    assert b.ler_sessao() == {"modo": "farm", "filtros": {"volume_min_usd": 500}, "comparacao": True}
    b.fechar()


def test_put_sessao_salva_e_o_scanner_religado_comeca_nela(tmp_path, monkeypatch):
    config = config_servico(tmp_path)
    b = Banco(config.banco)
    m = motor_falso(config=config, banco=b)
    cliente = ClienteDoPainel(criar_app(m, b, config))
    corpo = {"modo": "farm", "filtros": {"volume_min_usd": 500, "periodo_metricas_h": 2}, "comparacao": True}
    assert cliente.put("/api/sessao", json=corpo).status_code == 200
    b.fechar()

    criados = []

    class MotorEspiao(Motor):
        def __init__(self, *argumentos, **nomeados):
            super().__init__(*argumentos, **nomeados)
            criados.append(self)

    monkeypatch.setattr(principal, "Motor", MotorEspiao)
    exchanges = exchanges_falsas()
    asyncio.run(asyncio.wait_for(principal.rodar(config, criar_exchange=exchanges.__getitem__,
                                                 relogio=lambda: 1000.0, ciclos=1), timeout=5))
    s = criados[0].sessao
    assert (s.modo, s.comparacao, s.filtros.volume_min_usd, s.filtros.periodo_metricas_h) == ("farm", True, 500, 2)


def test_sessao_salva_invalida_e_ignorada_com_aviso(banco, caplog):
    m = motor_falso(banco=banco)
    with caplog.at_level(logging.WARNING, logger="scanner"):
        assert not m.restaurar_sessao({"modo": "lucro", "filtros": {"campo_que_nao_existe": 1}, "comparacao": False})
        assert not m.restaurar_sessao({"modo": "outro", "filtros": {}, "comparacao": False})
        assert not m.restaurar_sessao("texto")
    assert m.sessao == Sessao()
    assert len([r for r in caplog.records if "sessao salva" in r.getMessage()]) == 3


# ---------------------------------------------------------------------------
# Minor 8 da interface: periodo das metricas limitado a retencao_dias x 24
# ---------------------------------------------------------------------------


def test_periodo_acima_da_retencao_e_recusado_com_400(tmp_path):
    config = cfg_com(retencao_dias=2)  # no maximo 48 h
    b = Banco(str(tmp_path / "p.db"))
    cliente = ClienteDoPainel(criar_app(motor_falso(config=config, banco=b), b, config))
    sessao = {"modo": "lucro", "filtros": {"periodo_metricas_h": 49}, "comparacao": False}
    r = cliente.put("/api/sessao", json=sessao)
    assert r.status_code == 400 and "48" in r.json()["detail"] and "retencao_dias" in r.json()["detail"]
    assert cliente.post("/api/filtros/G", json={"periodo_metricas_h": 4000}).status_code == 400
    sessao["filtros"]["periodo_metricas_h"] = 48
    assert cliente.put("/api/sessao", json=sessao).status_code == 200
    b.fechar()


def test_filtros_de_dict_recusa_periodo_acima_do_maximo():
    with pytest.raises(ValueError, match="periodo_metricas_h"):
        Filtros.de_dict({"periodo_metricas_h": 25}, periodo_max_h=24)
    assert Filtros.de_dict({"periodo_metricas_h": 24}, periodo_max_h=24).periodo_metricas_h == 24


def test_motor_ignora_periodo_da_sessao_acima_da_retencao(caplog):
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), config=cfg_com(retencao_dias=1))
    m.sessao = Sessao(filtros=Filtros(periodo_metricas_h=30))  # direto, sem passar pela API
    with caplog.at_level(logging.WARNING, logger="scanner.motor"):
        m.candidatos(agora=10)
    assert m.gerenciador.periodo_h == cfg.periodo_metricas_h
    assert any("periodo_metricas_h" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# I3: metricas das corretoras pelo escritor (o laco nao espera o disco) e o resumo da virada
# ---------------------------------------------------------------------------


def test_resumo_da_virada_espera_o_escritor_gravar_a_leitura_que_fecha_o_dia(tmp_path, caplog):
    from tests.test_relatorio import INICIO_21_OUT, MotorDeStatus, Tempo, gate

    b = Banco(str(tmp_path / "v.db"))
    escritor = EscritorBanco(b, intervalo_lote_amostras_s=60)
    escritor.iniciar()
    gravacoes = []
    original = b.registrar_corretoras

    def anotar(dia, status):  # um disco lento: sem esperar o escritor, o resumo leria o dia sem esta leitura
        gravacoes.append(threading.current_thread().name)
        time.sleep(0.2)
        return original(dia, status)

    b.registrar_corretoras = anotar
    tempo = Tempo(INICIO_21_OUT - 100, parar_depois=2)

    async def cenario():
        with pytest.raises(asyncio.CancelledError):
            await principal._validacao_diaria(MotorDeStatus(gate(1, 150.0), gate(4, 250.0)), b,
                                              cfg_com(intervalo_log_recursos_s=300), tempo, esperar=tempo.esperar,
                                              escritor=escritor)

    try:
        with caplog.at_level(logging.INFO, logger="scanner"):
            asyncio.run(cenario())
        corretoras = [r.getMessage() for r in caplog.records if "resumo do dia" in r.getMessage()][-1]
        assert "gate atraso medio 150.0 ms, 1 reconexoes" in corretoras  # a leitura da virada ja estava no banco
        escritor.parar()
        assert gravacoes and all(nome == "escritor do banco" for nome in gravacoes)  # nunca no laco
        assert b.corretoras_do_dia("2026-10-21") == {"gate": {"atraso_medio_ms": 250.0, "reconexoes": 3}}
    finally:
        escritor.parar()
        b.fechar()
