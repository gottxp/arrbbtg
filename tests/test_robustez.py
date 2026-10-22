"""Testes da Task 14: reconexao, limites de assinatura, recarga dos mercados, logs, recursos e descartes.

Nenhum teste acessa a internet nem dorme de verdade: a corretora do ccxt e trocada por uma
`CorretoraFalsa` (filas de mensagens que o teste enche quando quer) e as esperas de reconexao
por uma `EsperaFalsa`, que so anota quantos segundos esperaria.
"""

import asyncio
import logging
import math
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

import pytest
from ccxt.base.errors import ChecksumError, ExchangeError, NotSupported
from fastapi.testclient import TestClient

import scanner.__main__ as principal
import scanner.logs as logs
import scanner.motor as modulo_motor
from scanner.api import criar_app
from scanner.armazenamento import Banco
from scanner.coletores.ccxt_coletor import ColetorCcxt, mercado_do_simbolo
from scanner.coletores.dados_lentos import DadosLentos
from scanner.coletores.reconexao import espera_reconexao, parece_limite_de_assinatura, selecionar_pares
from scanner.config.carregar import Config
from scanner.estado import EstadoMercados
from scanner.motor import Motor, chave_de, simbolos_para_tickers
from tests.fabricas import (
    DadosLentosFalso,
    EsperaFalsa,
    cfg,
    cfg_com,
    colocar_ponta,
    esperar_ate,
    market_ccxt,
)

AGORA = 1000.0


def relogio():
    return AGORA


class ErroDaCorretora(Exception):
    """Erro inventado pelo teste para simular uma queda de rede."""


class CorretoraFalsa:
    """Imita a corretora do ccxt.pro com filas: cada watch_* devolve o proximo item da fila dele.

    Filas: ("tickers", "spot" | "futuro") para watch_tickers, ("ticker", simbolo) para watch_ticker e
    ("livro", simbolo) para watch_order_book. Item que e excecao e levantado. Fila vazia: a chamada
    espera (como o ccxt esperando a proxima mensagem). O teste poe itens com `entregar`.
    """

    def __init__(self, markets, has=None, tickers_rest=None):
        self.markets = markets
        self.proximos_markets = None  # se preenchido, o proximo load_markets passa a usar estes
        self.has = {"watchTickers": True, "fetchTickers": True, "fetchFundingRates": True} if has is None else has
        self.tickers_rest = dict(tickers_rest or {})
        self.erro_ao_carregar = None
        self.erro_un_watch = None
        self.cargas = []        # o `reload` de cada load_markets
        self.pedidos = []       # (metodo, simbolos)
        self.desassinados = []
        self.fechada = False
        self._filas = {}

    def fila(self, chave) -> asyncio.Queue:
        if chave not in self._filas:
            self._filas[chave] = asyncio.Queue()
        return self._filas[chave]

    def entregar(self, chave, *itens) -> None:
        for item in itens:
            self.fila(chave).put_nowait(item)

    async def _proximo(self, chave):
        item = await self.fila(chave).get()
        if isinstance(item, BaseException):
            raise item
        return item

    def pedidos_de(self, metodo) -> list:
        return [simbolos for nome, simbolos in self.pedidos if nome == metodo]

    async def load_markets(self, reload=False, params=None):
        self.cargas.append(reload)
        await asyncio.sleep(0)
        if self.erro_ao_carregar is not None:
            raise self.erro_ao_carregar
        if self.proximos_markets is not None:
            self.markets, self.proximos_markets = self.proximos_markets, None
        return self.markets

    async def watch_tickers(self, symbols=None, params=None):
        self.pedidos.append(("watch_tickers", tuple(symbols)))
        return await self._proximo(("tickers", mercado_do_simbolo(symbols[0])))

    async def watch_ticker(self, symbol, params=None):
        self.pedidos.append(("watch_ticker", (symbol,)))
        return await self._proximo(("ticker", symbol))

    async def watch_order_book(self, symbol, limit=None, params=None):
        self.pedidos.append(("watch_order_book", (symbol,)))
        return await self._proximo(("livro", symbol))

    async def un_watch_order_book(self, symbol, params=None):
        await asyncio.sleep(0)
        self.desassinados.append(symbol)
        if self.erro_un_watch is not None:
            raise self.erro_un_watch

    async def fetch_tickers(self, symbols=None, params=None):
        self.pedidos.append(("fetch_tickers", tuple(symbols or ())))
        await asyncio.sleep(0)
        return {s: t for s, t in self.tickers_rest.items() if symbols is None or s in symbols}

    async def fetch_funding_rates(self, symbols=None, params=None):
        await asyncio.sleep(0)
        return {}

    async def close(self):
        self.fechada = True


def tk(bid=99.9, ask=100.0, volume=5e6, timestamp=None):
    """Ticker no formato do ccxt."""
    return {"bid": bid, "ask": ask, "quoteVolume": volume, "percentage": 1.0, "timestamp": timestamp}


def ob(bid=99.9, ask=100.0, qtd=10.0, timestamp=None):
    """Livro no formato do ccxt, um nivel de cada lado ($1000 com os padroes)."""
    return {"bids": [[bid, qtd]], "asks": [[ask, qtd]], "timestamp": timestamp}


MARKETS_GATE = {"BTC/USDT": market_ccxt("BTC/USDT", "BTC")}
MARKETS_BITGET = {"BTC/USDT:USDT": market_ccxt("BTC/USDT:USDT", "BTC")}
CHAVE_BTC = chave_de(("BTC", "gate", "spot", "bitget", "futuro"))


def avisos(caplog, nivel=logging.WARNING, logger="scanner") -> list[str]:
    """Mensagens do scanner com nivel >= `nivel` (sem o log interno do asyncio)."""
    return [r.getMessage() for r in caplog.records if r.name.startswith(logger) and r.levelno >= nivel]


async def parar(tarefa: asyncio.Task) -> None:
    tarefa.cancel()
    await asyncio.gather(tarefa, return_exceptions=True)


# ---------------------------------------------------------------------------
# Brief: espera crescente e prioridade por volume
# ---------------------------------------------------------------------------


def test_espera_crescente():
    assert [espera_reconexao(t) for t in (0, 1, 2, 6)] == [1, 2, 4, 60]


def test_prioriza_volume():
    sel, fora = selecionar_pares(["A", "B", "C"], {"A": 10, "B": 300, "C": 50}, limite=2)
    assert sel == ["B", "C"] and fora == 1


def test_espera_maxima_vem_da_config():
    assert Config().espera_max_reconexao_s == 60
    assert espera_reconexao(10, maximo_s=cfg_com(espera_max_reconexao_s=30).espera_max_reconexao_s) == 30
    assert espera_reconexao(2, maximo_s=3) == 3
    assert espera_reconexao(10_000) == 60       # tentativa enorme nao estoura
    assert espera_reconexao(-1) == 1            # tentativa negativa conta como a primeira


def test_selecionar_pares_dentro_do_limite_devolve_todos_por_volume():
    assert selecionar_pares(["A", "B"], {"A": 1, "B": 2}, limite=5) == (["B", "A"], 0)


def test_selecionar_pares_sem_volume_vai_para_o_fim_e_empate_pelo_nome():
    volumes = {"B": 5.0, "A": 5.0, "D": math.nan, "E": None}
    assert selecionar_pares(["E", "D", "C", "B", "A"], volumes, limite=3) == (["A", "B", "C"], 2)
    assert selecionar_pares(["A", "A", "B"], {}, limite=1) == (["A"], 1)  # repetido conta uma vez


@pytest.mark.parametrize("mensagem, esperado", [
    ('bingx {"code":80015,"msg":"subscription limit exceeded"}', True),
    ("Too many subscriptions on this connection", True),
    ("max subscriptions per connection reached", True),
    ("exceeded the maximum number of topics", True),
    ("too many streams", True),
    ("rate limit exceeded", False),      # limite de requisicoes, nao de assinaturas
    ("network error: connection reset", False),
    ("bad symbol", False),
])
def test_parece_limite_de_assinatura(mensagem, esperado):
    assert parece_limite_de_assinatura(ExchangeError(mensagem)) is esperado


# ---------------------------------------------------------------------------
# Reconexao do stream de tickers
# ---------------------------------------------------------------------------


def motor_com_bitget() -> Motor:
    """Motor com o futuro de BTC da bitget (vende a 101) no estado e F medio 0 na combinacao com o spot da gate.

    A gate (spot, ask 100) chega pelo coletor do teste. Com A = 1% e taxas de 0,06%, o RP liquido e 0,76%.
    """
    estado = EstadoMercados(cfg.max_silencio_conexao_s, cfg.max_idade_livro_s)
    colocar_ponta(estado, "bitget", "futuro", "BTC", 101.0, "short", AGORA)
    dados = DadosLentosFalso(taxas={("gate", "spot"): 0.06, ("bitget", "futuro"): 0.06})
    m = Motor(cfg, estado, dados)
    m.precarregar_amostra(CHAVE_BTC, AGORA, 1.0, 0.0, 1000.0, 1000.0)
    return m


def test_coletor_que_cai_e_volta(caplog):
    m = motor_com_bitget()
    gate = CorretoraFalsa(MARKETS_GATE)
    espera = EsperaFalsa()
    coletor = ColetorCcxt("gate", gate, estado=m.estado, relogio=relogio, esperar=espera)
    saude = lambda: m.status(AGORA)["gate"]  # noqa: E731
    pontas = lambda: {(c.ponta_long.corretora, c.ponta_short.corretora) for c in m.candidatos(AGORA)}  # noqa: E731

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT"]))
        gate.entregar(("tickers", "spot"), {"BTC/USDT": tk()})
        await coletor.assinar_livros({"BTC/USDT"})
        gate.entregar(("livro", "BTC/USDT"), ob())
        await esperar_ate(lambda: m.estado.obter_livro("gate", "spot", "BTC", AGORA) is not None)
        assert [e.tipo for e in m.ciclo(AGORA)] == ["novo"]
        assert saude() == {"estado": "conectada", "atraso_medio_ms": None, "reconexoes": 0}

        # Caiu: o estado esquece a gate, o status mostra "reconectando" e o sinal que dependia dela encerra.
        gate.entregar(("tickers", "spot"), ErroDaCorretora("caiu"))
        await esperar_ate(lambda: espera.esperas == [1])
        assert not m.estado.conexao_viva("gate", AGORA) and "gate" not in m.estado.mercados()
        assert saude()["estado"] == "reconectando"
        assert pontas() == set()
        assert [(e.tipo, e.sinal["par"]) for e in m.ciclo(AGORA)] == [("encerrado", "BTC/USDT")]

        # Continua fora: a espera dobra (2 s, 4 s) e nenhum sinal falso aparece.
        gate.entregar(("tickers", "spot"), ErroDaCorretora("de novo"))
        await esperar_ate(lambda: espera.esperas == [1, 2])
        gate.entregar(("tickers", "spot"), OSError("sem rede"))
        await esperar_ate(lambda: espera.esperas == [1, 2, 4])
        assert saude()["estado"] == "reconectando" and m.ciclo(AGORA) == []

        # Voltou: a primeira mensagem boa conta a reconexao; com o livro de volta, o sinal volta.
        gate.entregar(("tickers", "spot"), {"BTC/USDT": tk()})
        await esperar_ate(lambda: saude()["estado"] == "conectada")
        assert saude()["reconexoes"] == 1
        gate.entregar(("livro", "BTC/USDT"), ob())
        await esperar_ate(lambda: m.estado.obter_livro("gate", "spot", "BTC", AGORA) is not None)
        assert pontas() == {("gate", "bitget")}
        assert [e.tipo for e in m.ciclo(AGORA)] == ["novo"]

        # Caiu de novo depois de ter voltado: a espera recomeca de 1 s.
        gate.entregar(("tickers", "spot"), ErroDaCorretora("caiu outra vez"), {"BTC/USDT": tk()})
        await esperar_ate(lambda: saude()["reconexoes"] == 2)
        assert espera.esperas == [1, 2, 4, 1]
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.INFO, logger="scanner"):
        asyncio.run(cenario())
    quedas = [m for m in avisos(caplog) if "gate" in m and "caiu" in m]
    assert quedas and "1s" in quedas[0]
    assert any("gate" in m and "de volta" in m for m in avisos(caplog, logging.INFO))


def test_cancelar_os_tickers_nao_e_tratado_como_erro(caplog):
    estado = EstadoMercados()
    gate = CorretoraFalsa(MARKETS_GATE)
    espera = EsperaFalsa()
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, esperar=espera)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT"]))
        gate.entregar(("tickers", "spot"), {"BTC/USDT": tk()})
        await esperar_ate(lambda: estado.obter_ticker("gate", "spot", "BTC", AGORA) is not None)
        tarefa.cancel()  # o mesmo que o Ctrl+C faz
        with pytest.raises(asyncio.CancelledError):
            await tarefa

    with caplog.at_level(logging.DEBUG, logger="scanner"):
        asyncio.run(cenario())
    assert espera.esperas == []
    assert estado.saude("gate", AGORA) == {"estado": "conectada", "atraso_medio_ms": None, "reconexoes": 0}
    assert avisos(caplog) == []


def test_cancelar_durante_a_espera_de_reconexao_para_sem_tentar_de_novo(caplog):
    estado = EstadoMercados()
    gate = CorretoraFalsa(MARKETS_GATE)
    espera = EsperaFalsa(bloquear=True)
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, esperar=espera)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT"]))
        gate.entregar(("tickers", "spot"), ErroDaCorretora("caiu"))
        await esperar_ate(lambda: espera.esperas == [1])
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa

    with caplog.at_level(logging.DEBUG, logger="scanner"):
        asyncio.run(cenario())
    assert len(gate.pedidos_de("watch_tickers")) == 1  # nao tentou de novo depois do cancelamento
    assert len(avisos(caplog)) == 1                     # so a queda; o cancelamento nao e erro


def test_corretora_que_cai_nao_derruba_as_outras():
    estado = EstadoMercados()
    gate, bitget = CorretoraFalsa(MARKETS_GATE), CorretoraFalsa(MARKETS_BITGET)
    espera = EsperaFalsa()
    c_gate = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, esperar=espera)
    c_bitget = ColetorCcxt("bitget", bitget, estado=estado, relogio=relogio, esperar=EsperaFalsa())

    async def cenario():
        tarefas = [asyncio.create_task(c_gate.manter_tickers(["BTC/USDT"])),
                   asyncio.create_task(c_bitget.manter_tickers(["BTC/USDT:USDT"]))]
        gate.entregar(("tickers", "spot"), *[ErroDaCorretora(f"queda {i}") for i in range(5)])
        bitget.entregar(("tickers", "futuro"), {"BTC/USDT:USDT": tk(101.0, 101.1)})
        await esperar_ate(lambda: len(espera.esperas) == 5)
        bitget.entregar(("tickers", "futuro"), {"BTC/USDT:USDT": tk(102.0, 102.1)})
        await esperar_ate(lambda: (estado.obter_ticker("bitget", "futuro", "BTC", AGORA) or {}).get("bid") == 102.0)
        for t in tarefas:
            await parar(t)

    asyncio.run(cenario())
    assert espera.esperas == [1, 2, 4, 8, 16]
    assert estado.saude("gate", AGORA)["estado"] == "reconectando"
    assert estado.saude("bitget", AGORA)["estado"] == "conectada"


def test_sem_nenhum_metodo_de_ticker_desiste_com_erro_no_log(caplog):
    espera = EsperaFalsa()
    coletor = ColetorCcxt("gate", CorretoraFalsa(MARKETS_GATE, has={}), estado=EstadoMercados(), esperar=espera)
    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(asyncio.wait_for(coletor.manter_tickers(["BTC/USDT"]), timeout=2))  # termina, nao fica tentando
    assert espera.esperas == []
    assert any("gate" in m and "ticker" in m for m in avisos(caplog, logging.ERROR))


def test_mercado_sem_metodo_de_ticker_nao_derruba_o_outro_mercado(caplog):
    markets = {**MARKETS_GATE, "BTC/USDT:USDT": market_ccxt("BTC/USDT:USDT", "BTC")}
    gate = CorretoraFalsa(markets, has={"watchTickers": True})
    estado = EstadoMercados()
    espera = EsperaFalsa()
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, esperar=espera)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT", "BTC/USDT:USDT"]))
        gate.entregar(("tickers", "spot"), NotSupported("watch_tickers nao existe no spot"))
        gate.entregar(("tickers", "futuro"), {"BTC/USDT:USDT": tk(100.0, 100.1)})
        await esperar_ate(lambda: any("spot" in m for m in avisos(caplog, logging.ERROR)))
        gate.entregar(("tickers", "futuro"), {"BTC/USDT:USDT": tk(200.0, 200.1)})
        await esperar_ate(lambda: (estado.obter_ticker("gate", "futuro", "BTC", AGORA) or {}).get("bid") == 200.0)
        await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert espera.esperas == []  # nao foi tratado como queda


def test_rodar_reconecta_sozinho_sem_reiniciar_o_scanner(tmp_path, caplog):
    gate = CorretoraFalsa(MARKETS_GATE)
    bitget = CorretoraFalsa(MARKETS_BITGET)
    gate.entregar(("tickers", "spot"), OSError("sem rede"), {"BTC/USDT": tk()})
    gate.entregar(("livro", "BTC/USDT"), ob())
    bitget.entregar(("tickers", "futuro"), {"BTC/USDT:USDT": tk(101.0, 101.1)})
    bitget.entregar(("livro", "BTC/USDT:USDT"), ob(101.0, 101.1))
    exchanges = {"gate": gate, "bitget": bitget}
    config = cfg_com(corretoras=["gate", "bitget"], banco=str(tmp_path / "s.db"), ciclo_ms=50,
                     rp_min_lucro_pct=-5.0, espera_max_reconexao_s=0.01, pasta_logs=str(tmp_path / "logs"))
    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(asyncio.wait_for(
            principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=8), timeout=5))
    assert any("gate" in m and "sem rede" in m for m in avisos(caplog))
    banco = Banco(config.banco)
    assert [r["par"] for r in banco.sinais_do_dia("1970-01-01")] == ["BTC/USDT"]  # a gate voltou e o sinal nasceu
    banco.fechar()


# ---------------------------------------------------------------------------
# /api/status com dados de verdade (estado, atraso medio, reconexoes)
# ---------------------------------------------------------------------------


def test_saude_conectada_reconectando_e_atrasada():
    e = EstadoMercados(max_silencio_s=2)
    assert e.saude("gate", AGORA) == {"estado": "atrasada", "atraso_medio_ms": None, "reconexoes": 0}  # nunca mandou
    e.registrar_mensagem("gate", AGORA)
    assert e.saude("gate", AGORA)["estado"] == "conectada"
    assert e.saude("gate", AGORA + 3)["estado"] == "atrasada"  # silencio acima de 2 s
    e.marcar_desconectada("gate")
    e.registrar_mensagem("gate", AGORA)  # um livro de outra conexao chegou: continua "reconectando" ate o ticker voltar
    assert e.saude("gate", AGORA)["estado"] == "reconectando"
    e.marcar_reconectada("gate")
    assert e.saude("gate", AGORA) == {"estado": "conectada", "atraso_medio_ms": None, "reconexoes": 1}


def test_atraso_medio_dos_livros_e_dos_tickers():
    estado = EstadoMercados()
    gate = CorretoraFalsa(MARKETS_GATE)
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, esperar=EsperaFalsa())

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"})
        gate.entregar(("livro", "BTC/USDT"), ob(timestamp=(AGORA - 0.2) * 1000), ob(timestamp=(AGORA - 0.1) * 1000))
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT"]))
        # Mensagem de tickers: vale o horario mais recente da mensagem (0,3 s antes).
        gate.entregar(("tickers", "spot"), {"BTC/USDT": tk(timestamp=(AGORA - 0.3) * 1000)})
        await esperar_ate(lambda: len(gate.pedidos_de("watch_order_book")) >= 3
                          and len(gate.pedidos_de("watch_tickers")) >= 2)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert estado.saude("gate", AGORA)["atraso_medio_ms"] == pytest.approx(200.0)


def test_sem_horario_da_corretora_o_atraso_fica_none():
    estado = EstadoMercados()
    estado.registrar_mensagem("gate", AGORA)
    assert estado.saude("gate", AGORA)["atraso_medio_ms"] is None


def test_atraso_absurdo_e_descartado_e_avisado_uma_vez(caplog):
    estado = EstadoMercados(atraso_max_valido_s=30)
    with caplog.at_level(logging.WARNING, logger="scanner"):
        estado.registrar_atraso("gate", 120.0, AGORA)   # 2 min "atrasado": relogio errado
        estado.registrar_atraso("gate", -45.0, AGORA)   # chegou antes de sair: relogio errado
        estado.registrar_atraso("gate", -50.0, AGORA)
    assert estado.saude("gate", AGORA)["atraso_medio_ms"] is None
    relogio_errado = [m for m in avisos(caplog) if "gate" in m and "relogio" in m]
    assert len(relogio_errado) == 1
    estado.registrar_atraso("gate", -0.05, AGORA)  # diferenca pequena de relogio entra na media
    assert estado.saude("gate", AGORA)["atraso_medio_ms"] == pytest.approx(-50.0)


def test_atraso_e_a_media_do_ultimo_minuto():
    e = EstadoMercados()
    e.registrar_atraso("gate", 1.0, agora=0.0)
    e.registrar_atraso("gate", 0.3, agora=5.0)
    assert e.saude("gate", 5.0)["atraso_medio_ms"] == pytest.approx(650.0)
    e.registrar_atraso("gate", 0.1, agora=70.0)  # as amostras de 0 s e 5 s ja sairam da janela de 60 s
    assert e.saude("gate", 70.0)["atraso_medio_ms"] == pytest.approx(100.0)
    assert e.saude("gate", 200.0)["atraso_medio_ms"] is None  # mais de 1 min sem amostra


def test_api_status_mostra_os_dados_de_verdade(tmp_path):
    m = motor_com_bitget()
    m.estado.registrar_atraso("bitget", 0.25, AGORA)
    m.estado.marcar_desconectada("gate")
    m.estado.marcar_reconectada("gate")
    m.estado.marcar_desconectada("gate")
    banco = Banco(str(tmp_path / "a.db"))
    corpo = TestClient(criar_app(m, banco, m.cfg, relogio=relogio)).get("/api/status").json()
    banco.fechar()
    assert corpo["gate"] == {"estado": "reconectando", "atraso_medio_ms": None, "reconexoes": 1}
    assert corpo["bitget"] == {"estado": "conectada", "atraso_medio_ms": 250.0, "reconexoes": 0}
    assert corpo["mexc"] == {"estado": "atrasada", "atraso_medio_ms": None, "reconexoes": 0}


# ---------------------------------------------------------------------------
# Limite de assinaturas
# ---------------------------------------------------------------------------

MARKETS_ABC = {f"{a}/USDT": market_ccxt(f"{a}/USDT", a) for a in "ABC"}


def estado_com_volumes(volumes: dict[str, float]) -> EstadoMercados:
    estado = EstadoMercados()
    for ativo, volume in volumes.items():
        estado.atualizar_ticker("gate", "spot", ativo, 1.0, 1.01, AGORA, volume, None,
                                simbolo_original=f"{ativo}/USDT", cotacao="USDT")
    return estado


def test_livros_alem_do_limite_ficam_os_de_maior_volume_e_o_log_so_quando_muda(caplog):
    estado = estado_com_volumes({"A": 10.0, "B": 300.0, "C": 50.0})
    gate = CorretoraFalsa(MARKETS_ABC)
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, limite_assinaturas=2)

    async def cenario():
        await coletor.assinar_livros({"A/USDT", "B/USDT", "C/USDT"})
        await coletor.assinar_livros({"A/USDT", "B/USDT", "C/USDT"})  # mesma conta: nao repete o aviso
        await esperar_ate(lambda: len(gate.pedidos_de("watch_order_book")) == 2)
        assert {s for (s,) in gate.pedidos_de("watch_order_book")} == {"B/USDT", "C/USDT"}
        await coletor.assinar_livros({"A/USDT", "B/USDT"})            # cabem os dois: avisa uma vez que voltou
        await coletor.assinar_livros({"A/USDT", "B/USDT"})
        await coletor.fechar()

    with caplog.at_level(logging.INFO, logger="scanner"):
        asyncio.run(cenario())
    fora = [m for m in avisos(caplog) if "limite" in m]
    assert len(fora) == 1 and "1 de 3" in fora[0] and "gate" in fora[0]
    assert len([m for m in avisos(caplog, logging.INFO) if "cabem" in m]) == 1


def test_tickers_um_a_um_com_limite_usam_o_volume_da_api(caplog):
    rest = {"A/USDT": tk(volume=10.0), "B/USDT": tk(volume=300.0), "C/USDT": tk(volume=50.0)}
    gate = CorretoraFalsa(MARKETS_ABC, has={"watchTicker": True, "fetchTickers": True}, tickers_rest=rest)
    coletor = ColetorCcxt("bingx", gate, estado=EstadoMercados(), relogio=relogio, limite_assinaturas=2)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["A/USDT", "B/USDT", "C/USDT"]))
        await esperar_ate(lambda: len(gate.pedidos_de("watch_ticker")) == 2)
        await asyncio.sleep(0.02)
        await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert {s for (s,) in gate.pedidos_de("watch_ticker")} == {"B/USDT", "C/USDT"}
    assert any("bingx" in m and "1 de 3" in m for m in avisos(caplog))


def test_limite_nao_vale_para_tickers_em_lote():
    gate = CorretoraFalsa(MARKETS_ABC)
    coletor = ColetorCcxt("gate", gate, estado=EstadoMercados(), relogio=relogio, limite_assinaturas=1)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["A/USDT", "B/USDT", "C/USDT"]))
        await esperar_ate(lambda: gate.pedidos_de("watch_tickers"))
        await parar(tarefa)

    asyncio.run(cenario())
    assert gate.pedidos_de("watch_tickers") == [("A/USDT", "B/USDT", "C/USDT")]  # uma assinatura so para todos


def test_erro_que_parece_limite_de_assinatura_vai_para_o_log(caplog):
    gate = CorretoraFalsa(MARKETS_GATE)
    coletor = ColetorCcxt("bingx", gate, estado=EstadoMercados(), relogio=relogio, esperar=EsperaFalsa())
    erro = ExchangeError('bingx {"code":80015,"msg":"subscription limit exceeded"}')

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"})
        gate.entregar(("livro", "BTC/USDT"), erro)
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT"]))
        gate.entregar(("tickers", "spot"), erro)
        await esperar_ate(lambda: len([m for m in avisos(caplog) if "limite de assinaturas" in m]) == 2)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert all("limites_assinatura" in m for m in avisos(caplog) if "limite de assinaturas" in m)


def test_rodar_passa_o_limite_e_a_espera_maxima_da_config_ao_coletor(tmp_path, monkeypatch):
    recebidos = {}
    original = ColetorCcxt.__init__

    def init_anotando(self, nome, *args, **kwargs):
        recebidos[nome] = (kwargs.get("limite_assinaturas"), kwargs.get("espera_max_reconexao_s"))
        original(self, nome, *args, **kwargs)

    monkeypatch.setattr(ColetorCcxt, "__init__", init_anotando)
    exchanges = {"gate": CorretoraFalsa(MARKETS_GATE), "bitget": CorretoraFalsa(MARKETS_BITGET)}
    config = cfg_com(corretoras=["gate", "bitget"], banco=str(tmp_path / "s.db"), ciclo_ms=50,
                     limites_assinatura={"gate": 7}, espera_max_reconexao_s=12)
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=1), timeout=5))
    assert recebidos == {"gate": (7, 12), "bitget": (None, 12)}


# ---------------------------------------------------------------------------
# Lista de mercados recarregada
# ---------------------------------------------------------------------------

MARKETS_GATE_ETH = {
    "BTC/USDT": market_ccxt("BTC/USDT", "BTC"),
    "ETH/USDT": market_ccxt("ETH/USDT", "ETH"),
    "ETH/USDT:USDT": market_ccxt("ETH/USDT:USDT", "ETH"),
}


def test_recarregar_mercados_reinicia_so_quem_mudou_e_tira_o_par_que_sumiu(caplog):
    gate, bitget = CorretoraFalsa(dict(MARKETS_GATE_ETH)), CorretoraFalsa(MARKETS_BITGET)
    exchanges = {"gate": gate, "bitget": bitget}
    estado = EstadoMercados()
    coletores = {n: ColetorCcxt(n, ex, estado=estado, relogio=relogio) for n, ex in exchanges.items()}
    dados = DadosLentosFalso(taxas={("gate", "spot"): 0.06, ("gate", "futuro"): 0.06})
    m = Motor(cfg_com(rp_min_lucro_pct=-5.0), estado, dados)
    tickers = principal._Tickers(coletores)

    async def cenario():
        for nome, simbolos in simbolos_para_tickers({n: c.simbolos_por_chave() for n, c in coletores.items()}).items():
            tickers.iniciar(nome, simbolos)
        assert tickers.simbolos["gate"] == ["BTC/USDT", "ETH/USDT", "ETH/USDT:USDT"]
        # ETH spot x ETH futuro na propria gate, com livro e ticker: vira sinal.
        colocar_ponta(estado, "gate", "spot", "ETH", 100.0, "long", AGORA)
        colocar_ponta(estado, "gate", "futuro", "ETH", 101.0, "short", AGORA)
        assert [(e.tipo, e.sinal["par"]) for e in m.ciclo(AGORA)] == [("novo", "ETH/USDT")]
        tarefa_bitget, tarefa_gate = tickers.tarefas["bitget"], tickers.tarefas["gate"]

        gate.proximos_markets = {"BTC/USDT": market_ccxt("BTC/USDT", "BTC")}  # a gate tirou o ETH
        await principal._recarregar_mercados(exchanges, coletores, tickers, estado)

        assert gate.cargas[-1] is True and bitget.cargas[-1] is True        # load_markets(reload=True)
        assert tickers.simbolos["gate"] == ["BTC/USDT"]
        assert tickers.tarefas["gate"] is not tarefa_gate and tarefa_gate.done()   # lista mudou: reiniciou
        assert tickers.tarefas["bitget"] is tarefa_bitget and not tarefa_bitget.done()  # igual: nao mexeu
        da_gate = estado.mercados().get("gate", set())
        assert ("spot", "ETH") not in da_gate and ("futuro", "ETH") not in da_gate
        assert estado.obter_livro("gate", "spot", "ETH", AGORA) is None
        assert [(e.tipo, e.sinal["par"]) for e in m.ciclo(AGORA)] == [("encerrado", "ETH/USDT")]
        for tarefa in tickers.tarefas.values():
            await parar(tarefa)

    with caplog.at_level(logging.INFO, logger="scanner"):
        asyncio.run(cenario())
    assert any("gate" in m and "mudou" in m for m in avisos(caplog, logging.INFO))


def test_recarregar_mercados_com_erro_numa_corretora_segue_com_a_lista_anterior(caplog):
    gate, bitget = CorretoraFalsa(MARKETS_GATE), CorretoraFalsa(MARKETS_BITGET)
    bitget.erro_ao_carregar = OSError("sem rede")
    exchanges = {"gate": gate, "bitget": bitget}
    estado = EstadoMercados()
    coletores = {n: ColetorCcxt(n, ex, estado=estado, relogio=relogio) for n, ex in exchanges.items()}
    tickers = principal._Tickers(coletores)

    async def cenario():
        tickers.iniciar("gate", ["BTC/USDT"])
        tickers.iniciar("bitget", ["BTC/USDT:USDT"])
        antes = dict(tickers.tarefas)
        await principal._recarregar_mercados(exchanges, coletores, tickers, estado)
        assert tickers.tarefas == antes and tickers.simbolos == {"gate": ["BTC/USDT"], "bitget": ["BTC/USDT:USDT"]}
        for tarefa in tickers.tarefas.values():
            await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert any("bitget" in m and "sem rede" in m for m in avisos(caplog))


def test_recarregar_mercados_a_cada_intervalo():
    gate = CorretoraFalsa(MARKETS_GATE)
    coletores = {"gate": ColetorCcxt("gate", gate, estado=EstadoMercados(), relogio=relogio)}
    espera = EsperaFalsa(parar_depois=2)

    async def cenario():
        tickers = principal._Tickers(coletores)
        with pytest.raises(asyncio.CancelledError):
            await principal._recarregar_mercados_sempre({"gate": gate}, coletores, tickers, EstadoMercados(),
                                                        intervalo_s=3600, esperar=espera)
        for tarefa in tickers.tarefas.values():
            await parar(tarefa)

    asyncio.run(cenario())
    assert espera.esperas == [3600, 3600] and gate.cargas == [True, True]


def test_rodar_agenda_a_recarga_e_os_recursos_com_os_intervalos_da_config(tmp_path, monkeypatch):
    agendados = {}

    async def recarga_falsa(exchanges, coletores, tickers, estado, intervalo_s, esperar=asyncio.sleep):
        agendados["recarga"] = intervalo_s
        await asyncio.sleep(3600)

    async def recursos_falsos(motor, coletores, intervalo_s, esperar=asyncio.sleep, medidor=None):
        agendados["recursos"] = intervalo_s
        await asyncio.sleep(3600)

    monkeypatch.setattr(principal, "_recarregar_mercados_sempre", recarga_falsa)
    monkeypatch.setattr(principal, "_registrar_recursos", recursos_falsos)
    exchanges = {"gate": CorretoraFalsa(MARKETS_GATE), "bitget": CorretoraFalsa(MARKETS_BITGET)}
    config = cfg_com(corretoras=["gate", "bitget"], banco=str(tmp_path / "s.db"), ciclo_ms=50,
                     recarregar_mercados_s=1234, intervalo_log_recursos_s=77)
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=2), timeout=5))
    assert agendados == {"recarga": 1234, "recursos": 77}


# ---------------------------------------------------------------------------
# Logs: terminal + um arquivo por dia
# ---------------------------------------------------------------------------


@pytest.fixture
def logs_limpos():
    """Guarda e devolve os niveis dos loggers e tira os handlers do scanner no fim (nada vaza para outros testes)."""
    raiz, do_scanner = logging.getLogger(), logging.getLogger("scanner")
    niveis = (raiz.level, do_scanner.level)
    yield
    logs.desligar_logs()
    raiz.setLevel(niveis[0])
    do_scanner.setLevel(niveis[1])


@pytest.fixture
def fuso_brasilia():
    """O fuso local da maquina passa a ser UTC-3 (como o PC do dono), so durante o teste."""
    if not hasattr(time, "tzset"):
        pytest.skip("trocar o fuso no meio do processo nao funciona no Windows")
    antes = os.environ.get("TZ")
    os.environ["TZ"] = "BRT3"  # fuso no formato POSIX: 3 h atras do UTC, sem horario de verao
    time.tzset()
    yield
    if antes is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = antes
    time.tzset()


def handlers_do_scanner():
    return [h for h in logging.getLogger().handlers if getattr(h, "do_scanner", False)]


def descarregar():
    for h in handlers_do_scanner():
        h.flush()


def momento_local(ano, mes, dia, hora, minuto, segundo) -> float:
    """Segundos desde 1970 do horario LOCAL dado."""
    return time.mktime((ano, mes, dia, hora, minuto, segundo, 0, 0, -1))


def arquivos_de_log(pasta) -> list[str]:
    return sorted(p.name for p in Path(pasta).glob("scanner-*.log"))


@pytest.fixture
def sem_renomear(monkeypatch):
    """Falha o teste se o log tentar renomear algum arquivo (no Windows isso quebra com o arquivo aberto)."""
    def proibido(*args, **kwargs):
        raise AssertionError(f"o log nao pode renomear arquivo: {args}")

    monkeypatch.setattr(os, "rename", proibido)
    monkeypatch.setattr(os, "replace", proibido)


def test_logs_no_terminal_e_no_arquivo_do_dia_com_horario_local(tmp_path, logs_limpos, fuso_brasilia, capsys):
    pasta = tmp_path / "meus_logs"
    logs.configurar_logs("normal", str(pasta), dias=30)
    logging.getLogger("scanner.teste").info("linha de teste")
    logging.getLogger("scanner.teste").debug("detalhe que nao aparece no normal")
    logging.getLogger("outra.biblioteca").warning("aviso de biblioteca")
    descarregar()
    assert arquivos_de_log(pasta) == [f"scanner-{time.strftime('%Y-%m-%d')}.log"]  # data LOCAL
    texto = (pasta / arquivos_de_log(pasta)[0]).read_text(encoding="utf-8")
    # Horario local com o deslocamento do fuso, para nao haver duvida num log vindo de outra maquina.
    assert re.search(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} -0300 INFO scanner.teste: linha de teste$", texto, re.M)
    assert "aviso de biblioteca" in texto and "detalhe" not in texto
    assert "linha de teste" in capsys.readouterr().err  # terminal


def test_virada_a_meia_noite_local_troca_de_arquivo_sem_renomear(tmp_path, logs_limpos, fuso_brasilia, sem_renomear):
    agora = [momento_local(2026, 10, 22, 23, 59, 59)]
    logs.configurar_logs("normal", str(tmp_path), dias=30, relogio=lambda: agora[0])
    log = logging.getLogger("scanner.teste")
    log.info("antes da meia-noite")
    agora[0] = momento_local(2026, 10, 23, 0, 0, 1)
    log.info("depois da meia-noite")
    descarregar()
    assert arquivos_de_log(tmp_path) == ["scanner-2026-10-22.log", "scanner-2026-10-23.log"]
    dia_22 = (tmp_path / "scanner-2026-10-22.log").read_text(encoding="utf-8")
    dia_23 = (tmp_path / "scanner-2026-10-23.log").read_text(encoding="utf-8")
    assert "antes da meia-noite" in dia_22 and "depois" not in dia_22
    assert "depois da meia-noite" in dia_23 and "antes" not in dia_23


def test_partida_num_dia_novo_abre_o_arquivo_do_dia_sem_mexer_no_antigo(tmp_path, logs_limpos, fuso_brasilia,
                                                                       sem_renomear):
    antigo = tmp_path / "scanner-2026-10-21.log"
    antigo.write_text("ontem\n", encoding="utf-8")
    logs.configurar_logs("normal", str(tmp_path), dias=30, relogio=lambda: momento_local(2026, 10, 22, 8, 0, 0))
    logging.getLogger("scanner.teste").info("hoje")
    descarregar()
    assert antigo.read_text(encoding="utf-8") == "ontem\n"
    assert "hoje" in (tmp_path / "scanner-2026-10-22.log").read_text(encoding="utf-8")


def test_guarda_o_arquivo_de_hoje_e_os_dias_logs_anteriores(tmp_path, logs_limpos, fuso_brasilia):
    for dia in range(1, 11):
        (tmp_path / f"scanner-2026-10-{dia:02d}.log").write_text("antigo\n")
    (tmp_path / "outra-coisa.txt").write_text("nao e log\n")
    agora = [momento_local(2026, 10, 11, 12, 0, 0)]
    logs.configurar_logs("normal", str(tmp_path), dias=3, relogio=lambda: agora[0])
    assert arquivos_de_log(tmp_path) == [f"scanner-2026-10-{d}.log" for d in ("08", "09", "10", "11")]
    agora[0] = momento_local(2026, 10, 12, 0, 0, 1)
    logging.getLogger("scanner.teste").info("dia novo")
    assert arquivos_de_log(tmp_path) == [f"scanner-2026-10-{d}.log" for d in ("09", "10", "11", "12")]
    assert (tmp_path / "outra-coisa.txt").exists()


def test_arquivo_antigo_preso_nao_quebra_o_log(tmp_path, logs_limpos, fuso_brasilia, monkeypatch, capsys):
    for dia in range(1, 6):
        (tmp_path / f"scanner-2026-10-{dia:02d}.log").write_text("antigo\n")

    def remover_preso(caminho, *args, **kwargs):
        raise PermissionError(32, "o arquivo esta sendo usado por outro processo", str(caminho))

    monkeypatch.setattr(os, "remove", remover_preso)
    logs.configurar_logs("normal", str(tmp_path), dias=1, relogio=lambda: momento_local(2026, 10, 6, 9, 0, 0))
    logging.getLogger("scanner.teste").info("segue normal")
    descarregar()
    assert "segue normal" in (tmp_path / "scanner-2026-10-06.log").read_text(encoding="utf-8")
    erro = capsys.readouterr().err
    assert "nao consegui apagar" in erro and "Logging error" not in erro


def test_nivel_detalhado_grava_tambem_o_debug(tmp_path, logs_limpos):
    logs.configurar_logs("detalhado", str(tmp_path), dias=3)
    logging.getLogger("scanner.teste").debug("descarte detalhado")
    descarregar()
    assert "descarte detalhado" in (tmp_path / arquivos_de_log(tmp_path)[0]).read_text(encoding="utf-8")


def test_configurar_duas_vezes_nao_duplica_nem_abre_o_arquivo_duas_vezes(tmp_path, logs_limpos):
    logs.configurar_logs("normal", str(tmp_path), dias=3)
    logs.configurar_logs("normal", str(tmp_path), dias=3)
    assert len(handlers_do_scanner()) == 2  # um para o terminal e um para o arquivo
    logging.getLogger("scanner.teste").info("uma vez so")
    descarregar()
    assert (tmp_path / arquivos_de_log(tmp_path)[0]).read_text(encoding="utf-8").count("uma vez so") == 1


def test_pasta_de_logs_que_nao_abre_deixa_so_o_terminal(tmp_path, logs_limpos, capsys):
    arquivo_no_lugar = tmp_path / "logs"
    arquivo_no_lugar.write_text("um arquivo com o nome da pasta")
    logs.configurar_logs("normal", str(arquivo_no_lugar), dias=3)
    assert len(handlers_do_scanner()) == 1
    assert "arquivo de log" in capsys.readouterr().err


def test_main_configura_o_log_com_a_pasta_da_config(tmp_path, monkeypatch, logs_limpos):
    pasta = tmp_path / "logs_do_dono"
    arq = tmp_path / "config.yaml"
    arq.write_text(f"corretoras: [bitget]\npasta_logs: '{pasta.as_posix()}'\ndias_logs: 5\n")

    async def rodar_falso(config):
        logging.getLogger("scanner.principal").info("scanner subiu")

    monkeypatch.setattr(principal, "rodar", rodar_falso)
    assert principal.main(["--config", str(arq)]) == 0
    descarregar()
    assert "scanner subiu" in (pasta / arquivos_de_log(pasta)[0]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Memoria e CPU no log
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.path.exists("/proc/self/statm"), reason="so no Linux")
def test_memoria_do_processo_no_linux():
    bytes_, tipo = logs.memoria_do_processo()
    assert tipo == "atual" and 1_000_000 < bytes_ < 100_000_000_000


def test_memoria_cai_para_o_pico_e_depois_indisponivel(monkeypatch):
    monkeypatch.setattr(logs, "_memoria_linux", lambda: None)
    monkeypatch.setattr(logs, "_memoria_windows", lambda: None)
    monkeypatch.setattr(logs, "_memoria_pico", lambda: 123_000_000)
    assert logs.memoria_do_processo() == (123_000_000, "pico")

    def quebra():
        raise OSError("sem permissao")

    monkeypatch.setattr(logs, "_memoria_pico", quebra)
    assert logs.memoria_do_processo() is None


def test_memoria_windows_so_roda_no_windows(monkeypatch):
    monkeypatch.setattr(logs.sys, "platform", "linux")
    assert logs._memoria_windows() is None


def test_cpu_pela_diferenca_do_tempo_de_processo():
    parede, cpu = [100.0], [5.0]
    medidor = logs.MedidorCpu(relogio=lambda: parede[0], relogio_cpu=lambda: cpu[0])
    parede[0], cpu[0] = 102.0, 6.0  # 1 s de CPU em 2 s de relogio
    assert medidor.medir() == pytest.approx(50.0)
    assert medidor.medir() is None  # nenhum tempo passou desde a ultima medida


def test_linha_de_recursos_tem_memoria_cpu_sinais_e_historicos():
    linha = logs.linha_de_recursos((150 * 1024 * 1024, "atual"), 12.345, sinais_ativos=3, historicos=1520)
    assert "150.0 MB" in linha and "12.3%" in linha and "sinais ativos 3" in linha and "historicos 1520" in linha
    vazia = logs.linha_de_recursos(None, None, sinais_ativos=0, historicos=0)
    assert "indisponivel" in vazia


def test_registrar_recursos_a_cada_intervalo_com_o_resumo_dos_descartes(caplog):
    m = motor_com_bitget()
    gate = CorretoraFalsa(MARKETS_GATE)
    coletor = ColetorCcxt("gate", gate, estado=m.estado, relogio=relogio)
    coletor._descartes["livros"] += 2  # como se 2 livros tivessem sido descartados
    espera = EsperaFalsa(parar_depois=2)
    medidor = logs.MedidorCpu(relogio=iter([0.0, 10.0, 20.0]).__next__, relogio_cpu=iter([0.0, 1.0, 1.5]).__next__)

    async def cenario():
        with pytest.raises(asyncio.CancelledError):
            await principal._registrar_recursos(m, {"gate": coletor}, intervalo_s=300, esperar=espera, medidor=medidor)

    with caplog.at_level(logging.INFO, logger="scanner"):
        asyncio.run(cenario())
    assert espera.esperas == [300, 300]
    linhas = avisos(caplog, logging.INFO)
    recursos = [l for l in linhas if l.startswith("recursos")]
    descartes = [l for l in linhas if l.startswith("descartes")]
    assert len(recursos) == 2 and "CPU 10.0%" in recursos[0] and "CPU 5.0%" in recursos[1]
    assert "historicos 1" in recursos[0]
    assert "gate" in descartes[0] and "livros 2" in descartes[0]
    assert "nenhum" in descartes[1]  # os contadores zeram a cada resumo


# ---------------------------------------------------------------------------
# Descartes e sequencia quebrada
# ---------------------------------------------------------------------------


def test_descartes_contados_por_corretora_e_cada_um_em_debug(caplog):
    estado = EstadoMercados()
    gate = CorretoraFalsa(MARKETS_GATE)
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, esperar=EsperaFalsa())

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"})
        gate.entregar(("livro", "BTC/USDT"), ob(bid=100.0, ask=99.0), ob(bid=0.0), ob())  # cruzado, preco zero, bom
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT"]))
        gate.entregar(("tickers", "spot"), {"BTC/USDT": tk(bid=None)})
        await esperar_ate(lambda: estado.obter_livro("gate", "spot", "BTC", AGORA) is not None
                          and len(gate.pedidos_de("watch_tickers")) >= 2)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.DEBUG, logger="scanner"):
        asyncio.run(cenario())
    assert coletor.tirar_descartes() == {"livros": 2, "tickers": 1, "sequencia_quebrada": 0}
    assert coletor.tirar_descartes() == {"livros": 0, "tickers": 0, "sequencia_quebrada": 0}  # zera ao tirar
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG and "descartado" in r.getMessage()]
    assert len(debug) == 3


def test_sequencia_quebrada_vai_para_o_log_e_o_livro_e_pedido_de_novo_na_hora(caplog):
    estado = EstadoMercados()
    gate = CorretoraFalsa(MARKETS_GATE)
    espera = EsperaFalsa()
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, esperar=espera)
    quebra = ChecksumError("gate BTC/USDT : orderbook data checksum validation failed")
    pedidos = lambda: len(gate.pedidos_de("watch_order_book"))  # noqa: E731

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"})
        gate.entregar(("livro", "BTC/USDT"), ob(), quebra)
        await esperar_ate(lambda: pedidos() == 3)  # reassinou na hora (sem espera)
        assert espera.esperas == []
        assert estado.obter_livro("gate", "spot", "BTC", AGORA) is None  # o livro com buraco nao vale mais
        gate.entregar(("livro", "BTC/USDT"), quebra)  # quebrou de novo sem nenhum livro bom: agora espera
        await esperar_ate(lambda: espera.esperas == [1] and pedidos() == 4)
        gate.entregar(("livro", "BTC/USDT"), ob(bid=99.5), quebra)  # livro bom no meio: a proxima e na hora de novo
        await esperar_ate(lambda: pedidos() == 6)
        assert espera.esperas == [1]
        gate.entregar(("livro", "BTC/USDT"), ob(bid=99.6))
        await esperar_ate(lambda: estado.obter_livro("gate", "spot", "BTC", AGORA) is not None)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert coletor.tirar_descartes()["sequencia_quebrada"] == 3
    quebradas = [r for r in caplog.records if "sequencia" in r.getMessage()]
    assert len(quebradas) == 3 and not any(r.exc_info for r in quebradas)  # esperado: sem traceback


# ---------------------------------------------------------------------------
# Itens adiados para a T14 (Tasks 4, 9, 10 e 13)
# ---------------------------------------------------------------------------


def test_livro_que_falha_espera_cada_vez_mais_e_traceback_so_na_primeira(caplog):
    estado = EstadoMercados()
    gate = CorretoraFalsa(MARKETS_GATE)
    espera = EsperaFalsa()
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, esperar=espera)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"})
        gate.entregar(("livro", "BTC/USDT"), ErroDaCorretora("a"), ErroDaCorretora("b"), ErroDaCorretora("c"), ob())
        await esperar_ate(lambda: estado.obter_livro("gate", "spot", "BTC", AGORA) is not None)
        gate.entregar(("livro", "BTC/USDT"), ErroDaCorretora("d"), ob())  # voltou a funcionar: espera recomeca
        await esperar_ate(lambda: len(espera.esperas) == 4 and len(gate.pedidos_de("watch_order_book")) >= 7)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert espera.esperas == [1, 2, 4, 1]
    falhas = [r for r in caplog.records if r.name.startswith("scanner") and "BTC/USDT" in r.getMessage()]
    assert len(falhas) == 4
    assert falhas[0].exc_info and not any(r.exc_info for r in falhas[1:])


def test_erro_ao_tratar_um_ticker_nao_derruba_o_stream(caplog):
    markets = {**MARKETS_GATE, "ETH/USDT": market_ccxt("ETH/USDT", "ETH")}
    gate = CorretoraFalsa(markets)
    estado = EstadoMercados()
    espera = EsperaFalsa()
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, esperar=espera)
    recebidos = []

    def ao_ticker(ticker):
        if ticker.simbolo_original == "ETH/USDT":
            raise RuntimeError("defeito no callback")
        recebidos.append(ticker.bid)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT", "ETH/USDT"], ao_ticker))
        gate.entregar(("tickers", "spot"), {"ETH/USDT": tk(), "BTC/USDT": tk(10.0, 10.1)},
                      {"ETH/USDT": tk(), "BTC/USDT": tk(11.0, 11.1)})
        await esperar_ate(lambda: recebidos == [10.0, 11.0])
        await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert espera.esperas == []  # o stream nao caiu
    erros = [r for r in caplog.records if "ETH/USDT" in r.getMessage() and r.levelno >= logging.WARNING]
    assert len(erros) == 1 and erros[0].exc_info is not None  # traceback so na primeira vez


def test_fechar_para_os_tickers_em_andamento():
    gate = CorretoraFalsa(MARKETS_GATE)
    coletor = ColetorCcxt("gate", gate, estado=EstadoMercados(), relogio=relogio)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT"]))
        await esperar_ate(lambda: gate.pedidos_de("watch_tickers"))
        await coletor.fechar()
        assert tarefa.done()  # o fechar ja parou os tickers (e nao o fim do asyncio.run)

    asyncio.run(cenario())
    assert gate.fechada


def test_dados_lentos_cada_corretora_no_seu_ritmo():
    liberar_lenta = asyncio.Event()

    class Lenta(CorretoraFalsa):
        async def fetch_funding_rates(self, symbols=None, params=None):
            await liberar_lenta.wait()  # nunca responde
            return {}

    rapida, lenta = CorretoraFalsa(MARKETS_BITGET), Lenta(MARKETS_BITGET)
    chamadas = []
    original = CorretoraFalsa.fetch_funding_rates

    async def contar(self, symbols=None, params=None):
        chamadas.append(self)
        return await original(self, symbols, params)

    rapida.fetch_funding_rates = contar.__get__(rapida)
    espera = EsperaFalsa()

    async def cenario():
        tarefa = asyncio.create_task(DadosLentos().rodar({"rapida": rapida, "lenta": lenta}, 60, esperar=espera))
        await esperar_ate(lambda: len(chamadas) >= 3)  # a rapida segue atualizando enquanto a lenta trava
        await parar(tarefa)

    asyncio.run(cenario())
    assert set(espera.esperas) == {60}


def test_assinar_livros_abre_os_novos_antes_de_desassinar_os_que_sairam():
    liberar = asyncio.Event()

    class UnWatchDemorado(CorretoraFalsa):
        async def un_watch_order_book(self, symbol, params=None):
            self.desassinados.append(symbol)
            await liberar.wait()

    gate = UnWatchDemorado({**MARKETS_ABC})
    coletor = ColetorCcxt("gate", gate, estado=EstadoMercados(), relogio=relogio, timeout_desassinar_s=5)

    async def cenario():
        await coletor.assinar_livros({"A/USDT", "B/USDT"})
        ajuste = asyncio.create_task(coletor.assinar_livros({"C/USDT"}))  # A e B saem, C entra
        await esperar_ate(lambda: ("C/USDT",) in gate.pedidos_de("watch_order_book"))
        assert not ajuste.done() and sorted(gate.desassinados) == ["A/USDT", "B/USDT"]  # em paralelo, ainda esperando
        liberar.set()
        await ajuste
        await coletor.fechar()

    asyncio.run(cenario())


def test_market_malformado_e_pulado_com_aviso(caplog):
    markets = {
        "BTC/USDT": market_ccxt("BTC/USDT", "BTC"),
        "XXX/USDT": None,                                                        # nem e um dict
        "YYY/USDT:USDT": {**market_ccxt("YYY/USDT:USDT", "YYY"), "contractSize": "abc"},
    }
    coletor = ColetorCcxt("gate", CorretoraFalsa(markets), estado=EstadoMercados(), relogio=relogio)
    with caplog.at_level(logging.WARNING, logger="scanner"):
        assert coletor.simbolos_por_chave() == {("spot", "BTC"): "BTC/USDT"}
        coletor.simbolos_por_chave()
    malformados = [m for m in avisos(caplog) if "XXX/USDT" in m or "YYY/USDT:USDT" in m]
    assert len(malformados) == 2  # um aviso por market, nao a cada consulta


def test_falha_no_ciclo_nao_impede_a_etapa_2(tmp_path, monkeypatch, caplog):
    assinaturas = []

    def ciclo_quebrado(self, agora):
        raise RuntimeError("defeito no ciclo")

    async def assinar_anotando(self, simbolos, ao_atualizar=None):
        assinaturas.append(self.nome)

    monkeypatch.setattr(Motor, "ciclo", ciclo_quebrado)
    monkeypatch.setattr(ColetorCcxt, "assinar_livros", assinar_anotando)
    exchanges = {"gate": CorretoraFalsa(MARKETS_GATE), "bitget": CorretoraFalsa(MARKETS_BITGET)}
    config = cfg_com(corretoras=["gate", "bitget"], banco=str(tmp_path / "s.db"), ciclo_ms=50)
    with caplog.at_level(logging.ERROR, logger="scanner"):
        asyncio.run(asyncio.wait_for(
            principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=3), timeout=5))
    assert sorted(set(assinaturas)) == ["bitget", "gate"]
    assert any("ciclo" in m for m in avisos(caplog, logging.ERROR))


def test_falha_na_etapa_2_nao_impede_o_ciclo(tmp_path, monkeypatch, caplog):
    transmitidos = []

    def selecao_quebrada(self, agora):
        raise RuntimeError("defeito na etapa 2")

    monkeypatch.setattr(Motor, "selecao_livros", selecao_quebrada)
    monkeypatch.setattr(principal, "_transmitir", lambda publicar, eventos: transmitidos.append(eventos))
    exchanges = {"gate": CorretoraFalsa(MARKETS_GATE), "bitget": CorretoraFalsa(MARKETS_BITGET)}
    config = cfg_com(corretoras=["gate", "bitget"], banco=str(tmp_path / "s.db"), ciclo_ms=50)
    with caplog.at_level(logging.ERROR, logger="scanner"):
        asyncio.run(asyncio.wait_for(
            principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=3), timeout=5))
    assert len(transmitidos) == 3
    assert any("etapa 2" in m for m in avisos(caplog, logging.ERROR))


def motor_btc_e_eth() -> Motor:
    estado = EstadoMercados()
    for ativo in ("BTC", "ETH"):
        colocar_ponta(estado, "gate", "spot", ativo, 100.0, "long", AGORA)
        colocar_ponta(estado, "bitget", "futuro", ativo, 101.0, "short", AGORA)
    dados = DadosLentosFalso(taxas={("gate", "spot"): 0.06, ("bitget", "futuro"): 0.06})
    return Motor(cfg_com(rp_min_lucro_pct=-5.0), estado, dados)


def test_erro_numa_combinacao_so_tira_ela_e_avisa_uma_vez(monkeypatch, caplog):
    m = motor_btc_e_eth()
    original = Motor._candidato

    def candidato_que_quebra_no_eth(self, combo, *args):
        if combo[0] == "ETH":
            raise ZeroDivisionError("defeito so no ETH")
        return original(self, combo, *args)

    monkeypatch.setattr(Motor, "_candidato", candidato_que_quebra_no_eth)
    with caplog.at_level(logging.WARNING, logger="scanner"):
        assert [c.par for c in m.candidatos(AGORA)] == ["BTC/USDT"]
        assert [c.par for c in m.candidatos(AGORA)] == ["BTC/USDT"]
    erros = [r for r in caplog.records if "ETH" in r.getMessage() and r.levelno >= logging.WARNING]
    assert len(erros) == 1 and erros[0].exc_info is not None


def test_erro_nos_filtros_de_um_candidato_so_tira_ele(monkeypatch, caplog):
    m = motor_btc_e_eth()
    original = modulo_motor.passa

    def passa_que_quebra_no_eth(candidato, *args):
        if candidato.par == "ETH/USDT":
            raise TypeError("defeito no filtro")
        return original(candidato, *args)

    monkeypatch.setattr(modulo_motor, "passa", passa_que_quebra_no_eth)
    with caplog.at_level(logging.WARNING, logger="scanner"):
        assert [e.sinal["par"] for e in m.ciclo(AGORA)] == ["BTC/USDT"]
        m.ciclo(AGORA)
    assert len([r for r in caplog.records if "ETH" in r.getMessage() and r.levelno >= logging.WARNING]) == 1


def test_erro_na_etapa_2_de_um_ativo_so_tira_ele(monkeypatch, caplog):
    m = motor_btc_e_eth()
    m.estado.atualizar_ticker("gate", "spot", "ETH", 99.9, 100.5, AGORA, simbolo_original="ETH/USDT", cotacao="USDT")
    original = modulo_motor.spread_abertura

    def spread_que_quebra_no_eth(ask_long, bid_short):
        if ask_long == 100.5:
            raise ArithmeticError("defeito so no ETH")
        return original(ask_long, bid_short)

    monkeypatch.setattr(modulo_motor, "spread_abertura", spread_que_quebra_no_eth)
    with caplog.at_level(logging.WARNING, logger="scanner"):
        assert m.selecao_livros(AGORA) == {"gate": {"BTC/USDT"}, "bitget": {"BTC/USDT:USDT"}}
        m.selecao_livros(AGORA)
    assert len([r for r in caplog.records if "ETH" in r.getMessage() and r.levelno >= logging.WARNING]) == 1


def test_eventos_do_ciclo_gravados_numa_transacao_so(tmp_path, monkeypatch):
    m = motor_btc_e_eth()
    m.banco = Banco(str(tmp_path / "e.db"))
    lotes = []
    original = Banco.gravar_eventos

    def anotar(self, eventos):
        lotes.append(len(eventos))
        return original(self, eventos)

    monkeypatch.setattr(Banco, "gravar_eventos", anotar)
    monkeypatch.setattr(Banco, "gravar_evento", lambda self, e: pytest.fail("um commit por evento"))
    assert len(m.ciclo(AGORA)) == 2
    assert lotes == [2]
    assert len(m.banco.sinais_do_dia("1970-01-01")) == 2
    m.banco.fechar()


def test_evento_recusado_pelo_banco_vai_ao_log_e_os_outros_sao_gravados(tmp_path, monkeypatch, caplog):
    m = motor_btc_e_eth()
    m.banco = Banco(str(tmp_path / "e.db"))
    original = m.gerenciador.processar
    estragados = []

    def processar_com_um_nan(aprovados, agora):
        eventos = original(aprovados, agora)
        eventos[0].sinal["rp_liquido_pct"] = math.nan  # o JSON da spec nao aceita NaN
        estragados.append(eventos[0].sinal)
        return eventos

    monkeypatch.setattr(m.gerenciador, "processar", processar_com_um_nan)
    with caplog.at_level(logging.ERROR, logger="scanner"):
        m.ciclo(AGORA)
    assert len(m.banco.sinais_do_dia("1970-01-01")) == 1
    (erro,) = avisos(caplog, logging.ERROR)
    sinal = estragados[0]
    assert sinal["id"] in erro and sinal["par"] in erro and sinal["tipo"] in erro  # diz QUAL evento era
    m.banco.fechar()


def test_timeout_interno_do_ccxt_nao_e_confundido_com_o_prazo_de_desassinar(caplog):
    gate = CorretoraFalsa(MARKETS_ABC)
    gate.erro_un_watch = TimeoutError("o ccxt desistiu por dentro")
    coletor = ColetorCcxt("gate", gate, estado=EstadoMercados(), relogio=relogio, timeout_desassinar_s=5)

    async def cenario():
        await coletor.assinar_livros({"A/USDT"})
        await coletor.assinar_livros(set())
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    (aviso,) = [m for m in avisos(caplog) if "A/USDT" in m]
    assert "o ccxt desistiu por dentro" in aviso and "5s" not in aviso


def test_erro_do_ajuste_de_livros_com_traceback_so_na_primeira_vez(tmp_path, monkeypatch, caplog):
    async def assinar_com_erro(self, simbolos, ao_atualizar=None):
        if self.nome == "gate":
            raise RuntimeError("defeito na gate")

    monkeypatch.setattr(ColetorCcxt, "assinar_livros", assinar_com_erro)
    exchanges = {"gate": CorretoraFalsa(MARKETS_GATE), "bitget": CorretoraFalsa(MARKETS_BITGET)}
    config = cfg_com(corretoras=["gate", "bitget"], banco=str(tmp_path / "s.db"), ciclo_ms=50)
    with caplog.at_level(logging.ERROR, logger="scanner"):
        asyncio.run(asyncio.wait_for(
            principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=4), timeout=5))
    erros = [r for r in caplog.records if "defeito na gate" in r.getMessage()]
    assert len(erros) >= 2
    assert erros[0].exc_info and not any(r.exc_info for r in erros[1:])


def test_ajuste_de_livros_e_por_corretora(tmp_path, monkeypatch):
    chamadas = []

    async def assinar(self, simbolos, ao_atualizar=None):
        chamadas.append(self.nome)
        if self.nome == "gate":
            await asyncio.sleep(3600)  # a gate trava; a bitget nao pode esperar por ela

    monkeypatch.setattr(ColetorCcxt, "assinar_livros", assinar)
    exchanges = {"gate": CorretoraFalsa(MARKETS_GATE), "bitget": CorretoraFalsa(MARKETS_BITGET)}
    config = cfg_com(corretoras=["gate", "bitget"], banco=str(tmp_path / "s.db"), ciclo_ms=50)
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=4), timeout=5))
    assert chamadas.count("gate") == 1
    assert chamadas.count("bitget") >= 3


def test_banco_fecha_a_conexao_se_a_preparacao_falhar(tmp_path, monkeypatch):
    fechadas = []
    conectar = sqlite3.connect

    class ConexaoQueFalha:
        def __init__(self, *args, **kwargs):
            self._real = conectar(*args, **kwargs)

        def execute(self, sql, *args):
            if sql.startswith("PRAGMA"):
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *args)

        def close(self):
            fechadas.append(True)
            self._real.close()

        def __getattr__(self, nome):
            return getattr(self._real, nome)

    monkeypatch.setattr(sqlite3, "connect", ConexaoQueFalha)
    with pytest.raises(sqlite3.OperationalError):
        Banco(str(tmp_path / "x.db"))
    assert fechadas == [True]


def test_soma_estourada_nao_derruba_o_ciclo(caplog):
    m = motor_btc_e_eth()
    chave = chave_de(("BTC", "gate", "spot", "bitget", "futuro"))
    m.precarregar_amostra(chave, AGORA, 1.0, 1.7e308, 1000.0, 1000.0)
    m.precarregar_amostra(chave, AGORA, 1.0, 1.7e308, 1000.0, 1000.0)  # soma passa de 1,8e308
    with caplog.at_level(logging.WARNING, logger="scanner"):
        assert [c.par for c in m.candidatos(AGORA)] == ["ETH/USDT"]      # o BTC (F medio infinito) fica de fora
    assert avisos(caplog) == []  # descartado como numero invalido, e nao por erro


def test_motor_conta_os_historicos():
    m = motor_btc_e_eth()
    assert m.quantidade_historicos() == 0
    m.candidatos(AGORA)
    assert m.quantidade_historicos() == 2


def test_dados_lentos_pulam_o_market_malformado():
    markets = {**MARKETS_BITGET, "XXX/USDT:USDT": None}
    dados = DadosLentos()
    asyncio.run(dados.atualizar({"bitget": CorretoraFalsa(markets)}))
    assert dados.taxa_taker("bitget", "futuro") == 0.06


def test_ticker_um_a_um_que_falha_so_ele_tenta_de_novo():
    # Cada ticker e uma assinatura separada: um par com problema (ex.: removido) nao derruba os outros.
    markets = {**MARKETS_GATE, "ETH/USDT": market_ccxt("ETH/USDT", "ETH")}
    gate = CorretoraFalsa(markets, has={"watchTicker": True})
    estado = EstadoMercados()
    espera = EsperaFalsa()
    coletor = ColetorCcxt("bingx", gate, estado=estado, relogio=relogio, esperar=espera)
    pares = lambda: estado.mercados().get("bingx", set())  # noqa: E731

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT", "ETH/USDT"]))
        gate.entregar(("ticker", "ETH/USDT"), tk(20.0, 20.1))
        gate.entregar(("ticker", "BTC/USDT"), tk(10.0, 10.1))
        await esperar_ate(lambda: pares() == {("spot", "BTC"), ("spot", "ETH")})
        gate.entregar(("ticker", "ETH/USDT"), ExchangeError("bingx bad symbol ETH/USDT"))
        await esperar_ate(lambda: espera.esperas == [1])
        assert pares() == {("spot", "BTC")}  # o ticker do par que parou sai do estado na hora, como o livro
        gate.entregar(("ticker", "ETH/USDT"), tk(21.0, 21.1))  # voltou sozinho, depois da espera
        await esperar_ate(lambda: ("spot", "ETH") in pares())
        await parar(tarefa)

    asyncio.run(cenario())
    assert estado.saude("bingx", AGORA) == {"estado": "conectada", "atraso_medio_ms": None, "reconexoes": 0}


def test_ticker_um_a_um_com_erro_de_rede_reconecta_a_corretora():
    from ccxt.base.errors import NetworkError

    gate = CorretoraFalsa(MARKETS_GATE, has={"watchTicker": True})
    estado = EstadoMercados()
    espera = EsperaFalsa()
    coletor = ColetorCcxt("bingx", gate, estado=estado, relogio=relogio, esperar=espera)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT"]))
        gate.entregar(("ticker", "BTC/USDT"), tk(), NetworkError("conexao caiu"), tk())
        await esperar_ate(lambda: estado.saude("bingx", AGORA)["reconexoes"] == 1)
        await parar(tarefa)

    asyncio.run(cenario())
    assert espera.esperas == [1]


def test_queda_de_rede_nos_livros_sem_traceback(caplog):
    from ccxt.base.errors import NetworkError

    gate = CorretoraFalsa(MARKETS_GATE)
    espera = EsperaFalsa()
    coletor = ColetorCcxt("gate", gate, estado=EstadoMercados(), relogio=relogio, esperar=espera)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"})
        gate.entregar(("livro", "BTC/USDT"), NetworkError("conexao caiu"), OSError("sem rede"))
        await esperar_ate(lambda: espera.esperas == [1, 2])
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    quedas = [r for r in caplog.records if "BTC/USDT" in r.getMessage()]
    assert len(quedas) == 2 and not any(r.exc_info for r in quedas)  # queda de rede: o traceback nao ajuda


# ---------------------------------------------------------------------------
# Correcao pos-revisao: corretora que nao carregou ao ligar volta sozinha
# ---------------------------------------------------------------------------


class CorretoraQueDemoraACarregar(CorretoraFalsa):
    """Como a CorretoraFalsa, mas sem markets ate o load_markets dar certo, e as `falhas` primeiras cargas falham."""

    def __init__(self, markets, falhas, **kwargs):
        super().__init__({}, **kwargs)
        self._markets_de_verdade = markets
        self._falhas = falhas

    async def load_markets(self, reload=False, params=None):
        self.cargas.append(reload)
        await asyncio.sleep(0)
        if self._falhas > 0:
            self._falhas -= 1
            raise OSError("sem rede")
        self.markets = self._markets_de_verdade
        return self.markets


def test_corretora_que_nao_carregou_ao_ligar_volta_sozinha(caplog):
    gate = CorretoraQueDemoraACarregar(MARKETS_GATE, falhas=2)
    bitget = CorretoraFalsa(MARKETS_BITGET)
    estado = EstadoMercados()
    coletores = {"bitget": ColetorCcxt("bitget", bitget, estado=estado, relogio=relogio)}
    tickers = principal._Tickers(coletores)
    espera = EsperaFalsa()
    saude_em_cada_espera = []
    entraram = []

    async def esperar(segundos):
        saude_em_cada_espera.append(estado.saude("gate", AGORA)["estado"])
        await espera(segundos)

    async def cenario():
        await principal._trazer_de_volta(
            "gate", gate, coletores, tickers, estado,
            criar_coletor=lambda nome: ColetorCcxt(nome, gate, estado=estado, relogio=relogio),
            ao_entrar=entraram.append, espera_max_s=60, esperar=esperar,
        )
        # A bitget nao tinha par em comum com ninguem; com a gate de volta, ganhou e comecou os tickers.
        assert tickers.simbolos == {"gate": ["BTC/USDT"], "bitget": ["BTC/USDT:USDT"]}
        gate.entregar(("tickers", "spot"), {"BTC/USDT": tk()})
        await esperar_ate(lambda: estado.saude("gate", AGORA)["estado"] == "conectada")
        for tarefa in tickers.tarefas.values():
            await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert espera.esperas == [1, 2, 4]                          # 1 s, 2 s, 4 s: falhou 2 vezes e carregou na 3a
    assert saude_em_cada_espera == ["reconectando"] * 3         # o painel mostra "reconectando" enquanto isso
    assert gate.cargas == [False, False, False] and "gate" in coletores and entraram == ["gate"]
    tentativas = [m for m in avisos(caplog) if "gate" in m and "tentativa" in m]
    assert len(tentativas) == 2                                 # cada tentativa que falhou vai para o log
    assert estado.saude("gate", AGORA)["reconexoes"] == 0       # nunca tinha conectado: a entrada nao e reconexao


def test_rodar_corretora_que_falhou_ao_ligar_entra_sozinha_depois(tmp_path, caplog):
    gate = CorretoraQueDemoraACarregar(MARKETS_GATE, falhas=2)
    bitget = CorretoraFalsa(MARKETS_BITGET)
    gate.entregar(("tickers", "spot"), {"BTC/USDT": tk()})
    gate.entregar(("livro", "BTC/USDT"), ob())
    bitget.entregar(("tickers", "futuro"), {"BTC/USDT:USDT": tk(101.0, 101.1)})
    bitget.entregar(("livro", "BTC/USDT:USDT"), ob(101.0, 101.1))
    config = cfg_com(corretoras=["gate", "bitget"], banco=str(tmp_path / "s.db"), ciclo_ms=50,
                     rp_min_lucro_pct=-5.0, espera_max_reconexao_s=0.01)
    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(asyncio.wait_for(principal.rodar(
            config, criar_exchange={"gate": gate, "bitget": bitget}.__getitem__, relogio=relogio, ciclos=10),
            timeout=5))
    assert gate.cargas[:3] == [False, False, False]
    banco = Banco(config.banco)
    # A gate entrou sozinha: tickers, livros e dados lentos (taxa) dela funcionando, e o sinal nasceu.
    assert [r["par"] for r in banco.sinais_do_dia("1970-01-01")] == ["BTC/USDT"]
    banco.fechar()
    assert any("gate" in m and "tento de novo" in m for m in avisos(caplog))


# ---------------------------------------------------------------------------
# Correcao pos-revisao: menores
# ---------------------------------------------------------------------------


def test_defeito_no_stream_de_tickers_sai_com_traceback_na_primeira_vez(caplog):
    from ccxt.base.errors import NetworkError

    gate = CorretoraFalsa(MARKETS_GATE)
    espera = EsperaFalsa()
    coletor = ColetorCcxt("gate", gate, estado=EstadoMercados(), relogio=relogio, esperar=espera)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT"]))
        gate.entregar(("tickers", "spot"), NetworkError("rede"), RuntimeError("defeito"), RuntimeError("defeito"))
        await esperar_ate(lambda: len(espera.esperas) == 3)
        await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    quedas = [r for r in caplog.records if "caiu" in r.getMessage()]
    assert [bool(r.exc_info) for r in quedas] == [False, True, False]  # rede: nunca; defeito: so na primeira


def test_mercado_inativo_fica_de_fora_e_sai_do_estado_na_recarga(caplog):
    inativo = lambda simbolo, base: {**market_ccxt(simbolo, base), "active": False}  # noqa: E731
    gate = CorretoraFalsa(dict(MARKETS_GATE_ETH))
    estado = EstadoMercados()
    coletores = {"gate": ColetorCcxt("gate", gate, estado=estado, relogio=relogio)}
    tickers = principal._Tickers(coletores)

    async def cenario():
        tickers.iniciar("gate", ["BTC/USDT", "ETH/USDT", "ETH/USDT:USDT"])
        colocar_ponta(estado, "gate", "spot", "ETH", 100.0, "long", AGORA)
        colocar_ponta(estado, "gate", "futuro", "ETH", 101.0, "short", AGORA)
        gate.proximos_markets = {"BTC/USDT": market_ccxt("BTC/USDT", "BTC"),
                                 "ETH/USDT": inativo("ETH/USDT", "ETH"),
                                 "ETH/USDT:USDT": inativo("ETH/USDT:USDT", "ETH")}  # suspenso, ainda listado
        await principal._recarregar_mercados({"gate": gate}, coletores, tickers, estado)
        assert coletores["gate"].simbolos_por_chave() == {("spot", "BTC"): "BTC/USDT"}
        assert estado.mercados().get("gate", set()) == set()  # o ETH saiu do estado, como um par que sumiu
        for tarefa in tickers.tarefas.values():
            await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    inativos = [m for m in avisos(caplog) if "inativo" in m]
    assert len(inativos) == 2  # um aviso por simbolo


def test_selecionar_pares_prefere_os_ja_abertos_no_empate_e_na_diferenca_pequena():
    assert selecionar_pares(["A", "B", "C"], {"A": 100, "B": 100, "C": 100}, 2, abertos={"B", "C"}) == (["B", "C"], 1)
    assert selecionar_pares(["A", "B"], {"A": 105, "B": 100}, 1, abertos={"B"}, margem=0.10) == (["B"], 1)
    assert selecionar_pares(["A", "B"], {"A": 120, "B": 100}, 1, abertos={"B"}, margem=0.10) == (["A"], 1)


def test_limite_de_livros_nao_fica_trocando_por_diferenca_pequena_de_volume():
    estado = estado_com_volumes({"A": 100.0, "B": 100.0, "C": 100.0})
    gate = CorretoraFalsa(MARKETS_ABC)
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, limite_assinaturas=2)
    volume = lambda ativo, v: estado.atualizar_ticker("gate", "spot", ativo, 1.0, 1.01, AGORA, v, None,  # noqa: E731
                                                      simbolo_original=f"{ativo}/USDT", cotacao="USDT")

    async def cenario():
        await coletor.assinar_livros({"A/USDT", "B/USDT", "C/USDT"})
        assert set(coletor._tarefas_livro) == {"A/USDT", "B/USDT"}
        volume("C", 105.0)  # um pouco mais que os abertos: nao vale trocar
        await coletor.assinar_livros({"A/USDT", "B/USDT", "C/USDT"})
        assert set(coletor._tarefas_livro) == {"A/USDT", "B/USDT"} and gate.desassinados == []
        volume("C", 200.0)  # bem mais: troca
        await coletor.assinar_livros({"A/USDT", "B/USDT", "C/USDT"})
        assert set(coletor._tarefas_livro) == {"A/USDT", "C/USDT"} and gate.desassinados == ["B/USDT"]
        await coletor.fechar()

    asyncio.run(cenario())


def test_volumes_guardados_evitam_rest_a_cada_reconexao():
    from ccxt.base.errors import NetworkError

    rest = {"A/USDT": tk(volume=10.0), "B/USDT": tk(volume=300.0), "C/USDT": tk(volume=50.0)}
    gate = CorretoraFalsa(MARKETS_ABC, has={"watchTicker": True, "fetchTickers": True}, tickers_rest=rest)
    espera = EsperaFalsa()
    coletor = ColetorCcxt("bingx", gate, estado=EstadoMercados(), relogio=relogio, limite_assinaturas=2,
                          esperar=espera)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["A/USDT", "B/USDT", "C/USDT"]))
        gate.entregar(("ticker", "B/USDT"), tk(volume=300.0), NetworkError("caiu"))
        await esperar_ate(lambda: espera.esperas == [1] and len(gate.pedidos_de("watch_ticker")) >= 5)
        await parar(tarefa)

    asyncio.run(cenario())
    assert len(gate.pedidos_de("fetch_tickers")) == 1  # so na primeira vez; a reconexao usa os volumes guardados
    assert {s for (s,) in gate.pedidos_de("watch_ticker")} == {"B/USDT", "C/USDT"}


def test_iniciar_os_tickers_de_novo_para_a_tarefa_anterior():
    gate = CorretoraFalsa(MARKETS_GATE)
    tickers = principal._Tickers({"gate": ColetorCcxt("gate", gate, estado=EstadoMercados(), relogio=relogio)})

    anterior_terminou = []
    original = principal._acompanhar_tickers

    async def comeco_anotado(coletor, simbolos):
        anterior_terminou.append(primeira[0].done())
        await original(coletor, simbolos)

    primeira = []

    async def cenario():
        tickers.iniciar("gate", ["BTC/USDT"])
        primeira.append(tickers.tarefas["gate"])
        await esperar_ate(lambda: gate.pedidos_de("watch_tickers"))
        principal._acompanhar_tickers = comeco_anotado
        try:
            tickers.iniciar("gate", ["BTC/USDT"])
            await esperar_ate(lambda: len(gate.pedidos_de("watch_tickers")) == 2)
        finally:
            principal._acompanhar_tickers = original
        assert primeira[0].done() and tickers.tarefas["gate"] is not primeira[0]
        await parar(tickers.tarefas["gate"])

    asyncio.run(cenario())
    assert anterior_terminou == [True]  # o stream novo so comeca depois que o anterior terminou de verdade



# ---------------------------------------------------------------------------
# Correcao pos-revisao 2: o log nunca derruba o scanner; a corretora que volta registra o erro
# ---------------------------------------------------------------------------


def test_virada_com_a_pasta_removida_nao_levanta_e_volta_a_gravar(tmp_path, logs_limpos, fuso_brasilia, capsys):
    pasta = tmp_path / "logs"
    agora = [momento_local(2026, 10, 22, 23, 59, 0)]
    logs.configurar_logs("normal", str(pasta), dias=30, relogio=lambda: agora[0])
    log = logging.getLogger("scanner.teste")
    log.info("antes")
    descarregar()
    shutil.rmtree(pasta)  # a pasta sumiu (ou a unidade caiu) e chega a meia-noite: o arquivo novo nao abre
    agora[0] += 120
    log.info("depois da meia-noite")  # nao pode levantar: o motor chama o log no meio do laco
    log.info("de novo")
    erro = capsys.readouterr().err
    assert erro.count("Logging error") == 1  # avisa uma vez no terminal, nao a cada linha
    pasta.mkdir()  # a pasta voltou: o log volta a gravar sozinho
    log.info("voltou")
    descarregar()
    assert "voltou" in (pasta / "scanner-2026-10-23.log").read_text(encoding="utf-8")


def test_meia_noite_que_o_sistema_poe_antes_de_agora_nao_troca_de_arquivo_a_cada_linha(
        tmp_path, logs_limpos, fuso_brasilia, monkeypatch):
    # Fuso cujo horario de verao comeca a meia-noite: a meia-noite de amanha nao existe, e alguns sistemas
    # (CRT do Windows) a colocam antes de agora. O handler nao pode ficar trocando de arquivo a cada linha.
    agora = momento_local(2026, 10, 22, 23, 30, 0)
    real = logs._meia_noite
    monkeypatch.setattr(logs, "_meia_noite",
                        lambda momento, dias_depois=0: agora - 1800 if dias_depois else real(momento))
    logs.configurar_logs("normal", str(tmp_path), dias=30, relogio=lambda: agora)
    (arquivo,) = [h for h in handlers_do_scanner() if isinstance(h, logs.ArquivoDoDia)]
    assert arquivo._proxima_virada > agora
    stream = arquivo.stream
    for _ in range(3):
        logging.getLogger("scanner.teste").info("linha")
    assert arquivo.stream is stream  # o mesmo arquivo aberto, sem fechar e reabrir


def test_saude_da_corretora_que_ainda_nao_conectou_nao_conta_reconexao():
    e = EstadoMercados()
    e.marcar_esperando("gate")
    assert e.saude("gate", AGORA)["estado"] == "reconectando"
    e.marcar_desconectada("gate")  # caiu antes de conectar a primeira vez: continua sem contar
    e.registrar_mensagem("gate", AGORA)
    e.marcar_reconectada("gate")
    assert e.saude("gate", AGORA) == {"estado": "conectada", "atraso_medio_ms": None, "reconexoes": 0}
    e.marcar_desconectada("gate")  # depois de ter conectado, uma queda de verdade conta
    e.marcar_reconectada("gate")
    assert e.saude("gate", AGORA)["reconexoes"] == 1


def test_corretora_que_volta_com_erro_ao_entrar_registra_e_tenta_de_novo(caplog):
    gate = CorretoraQueDemoraACarregar(MARKETS_GATE, falhas=0)
    estado = EstadoMercados()
    coletores = {"bitget": ColetorCcxt("bitget", CorretoraFalsa(MARKETS_BITGET), estado=estado, relogio=relogio)}
    tickers = principal._Tickers(coletores)
    espera = EsperaFalsa()
    criacoes = []
    entraram = []

    def criar_coletor(nome):
        criacoes.append(nome)
        if len(criacoes) == 1:
            raise RuntimeError("defeito ao criar o coletor")
        return ColetorCcxt(nome, gate, estado=estado, relogio=relogio)

    async def cenario():
        await principal._trazer_de_volta("gate", gate, coletores, tickers, estado, criar_coletor=criar_coletor,
                                         ao_entrar=entraram.append, espera_max_s=60, esperar=espera)
        assert set(tickers.tarefas) == {"gate", "bitget"}
        for tarefa in tickers.tarefas.values():
            await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert espera.esperas == [1, 2] and criacoes == ["gate", "gate"] and entraram == ["gate"]
    erros = [r for r in caplog.records if r.levelno >= logging.ERROR and "gate" in r.getMessage()]
    assert len(erros) == 1 and erros[0].exc_info  # nao some em silencio: log com traceback
    assert estado.saude("gate", AGORA)["estado"] == "reconectando"  # ate o primeiro ticker


def test_corretora_que_volta_com_erro_no_ajuste_tenta_de_novo_sem_repetir_o_que_ja_fez(monkeypatch, caplog):
    gate = CorretoraQueDemoraACarregar(MARKETS_GATE, falhas=0)
    estado = EstadoMercados()
    coletores = {"bitget": ColetorCcxt("bitget", CorretoraFalsa(MARKETS_BITGET), estado=estado, relogio=relogio)}
    tickers = principal._Tickers(coletores)
    espera = EsperaFalsa()
    entraram, ajustes = [], []
    original = principal._ajustar_tickers

    async def ajuste_que_falha_uma_vez(*args):
        ajustes.append(True)
        if len(ajustes) == 1:
            raise RuntimeError("defeito no ajuste")
        await original(*args)

    monkeypatch.setattr(principal, "_ajustar_tickers", ajuste_que_falha_uma_vez)

    async def cenario():
        await principal._trazer_de_volta(
            "gate", gate, coletores, tickers, estado,
            criar_coletor=lambda nome: ColetorCcxt(nome, gate, estado=estado, relogio=relogio),
            ao_entrar=entraram.append, espera_max_s=60, esperar=espera)
        assert set(tickers.tarefas) == {"gate", "bitget"}
        for tarefa in tickers.tarefas.values():
            await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert espera.esperas == [1, 2] and len(ajustes) == 2
    assert entraram == ["gate"]  # os dados lentos da corretora so foram ligados uma vez
    assert any(r.exc_info and "defeito no ajuste" in str(r.exc_info[1]) for r in caplog.records)
