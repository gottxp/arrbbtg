"""Testes das correcoes da revisao final da coleta (I1-I3, M1-M7, M10).

  - I1: livro guardado pelo ccxt esquecido antes de assinar de novo depois de uma queda; livros da MEXC e da XT
    assinados de novo de tempos em tempos;
  - I2: intervalo do funding da Bitget pelo endpoint que traz o fundingRateInterval (formato real do ccxt) e
    aviso de intervalo ausente;
  - I3: limite de assinaturas por (corretora, mercado) dividido entre tickers e livros, volume 24h pela REST e
    aviso dos pares que nunca mandaram ticker;
  - M1: conexao viva por (corretora, mercado);
  - M2: tickers de cada corretora ajustados logo depois da recarga dela;
  - M3: so as moedas de cotacao de PREFERENCIA_COTACAO entram no indice;
  - M4/M5: funding gravado assim que chega, so dos simbolos em uso, do contrato ativo;
  - M6: livro da Lighter montado no maximo a cada intervalo_montar_livro_lighter_ms;
  - M7: Kraken spot com watch_bids_asks (bid/ask a cada mudanca, com volume).

Nenhum teste acessa a internet nem dorme de verdade.
"""

import asyncio
import logging

import pytest
from ccxt.base.errors import BadSymbol, ExchangeError, InvalidNonce, NetworkError, NotSupported

import scanner.__main__ as principal
from scanner.coletores.ccxt_coletor import ColetorCcxt
from scanner.coletores.dados_lentos import DadosLentos
from scanner.coletores.lighter import ColetorLighter, criar_coletor_lighter, markets_de_lighter
from scanner.config.carregar import Config, ErroConfig, carregar_config
from scanner.estado import EstadoMercados
from tests.fabricas import (
    ClienteLighterFalso,
    CorretoraFalsa,
    EsperaFalsa,
    WsLighterFalso,
    cfg_com,
    esperar_ate,
    market_ccxt,
    ob,
    tk,
)

AGORA = 1000.0


class Relogio:
    """Relogio falso que o teste adianta quando quer (r.agora += 900)."""

    def __init__(self, agora: float = AGORA):
        self.agora = agora

    def __call__(self) -> float:
        return self.agora


def relogio():
    return AGORA


def avisos(caplog, nivel=logging.WARNING) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith("scanner") and r.levelno >= nivel]


async def parar(tarefa: asyncio.Task) -> None:
    tarefa.cancel()
    await asyncio.gather(tarefa, return_exceptions=True)


def escrever(tmp_path, texto):
    arq = tmp_path / "config.yaml"
    arq.write_text(texto)
    return str(arq)


MEXC_BTC_FUT = market_ccxt("BTC/USDT:USDT", "BTC")


# ---------------------------------------------------------------------------
# I1: o livro guardado pelo ccxt e esquecido antes de assinar de novo
# ---------------------------------------------------------------------------


def coletor_de_livros(corretora, nome="mexc", estado=None, **kwargs):
    estado = EstadoMercados() if estado is None else estado
    kwargs.setdefault("relogio", relogio)
    kwargs.setdefault("esperar", EsperaFalsa())
    kwargs.setdefault("sortear", lambda: 0.5)  # prazo da reassinatura sem variacao (veja o teste da variacao)
    return ColetorCcxt(nome, corretora, estado=estado, **kwargs), estado


def test_queda_da_conexao_esquece_o_livro_do_ccxt_antes_de_assinar_de_novo():
    # MEXC futuros e XT: o ccxt so pede um livro completo novo se nao tiver nenhum guardado (exchange.orderbooks).
    mexc = CorretoraFalsa([MEXC_BTC_FUT])
    coletor, estado = coletor_de_livros(mexc)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob(), NetworkError("conexao caiu"))
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 3)
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob(99.0, 99.1))
        await esperar_ate(lambda: estado.obter_livro("mexc", "futuro", "BTC", AGORA) is not None)
        await coletor.fechar()

    asyncio.run(cenario())
    # 1o pedido: nada guardado; 2o: o livro de antes (espera a proxima mudanca); 3o, depois da queda: esquecido.
    assert mexc.livro_guardado_ao_pedir[:3] == [False, True, False]


@pytest.mark.parametrize("erro", [InvalidNonce("sequencia"), OSError("rede")])
def test_sequencia_quebrada_e_erro_de_rede_tambem_esquecem_o_livro_do_ccxt(erro):
    mexc = CorretoraFalsa([MEXC_BTC_FUT])
    coletor, _ = coletor_de_livros(mexc)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob(), erro)
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 3)
        await coletor.fechar()

    asyncio.run(cenario())
    assert mexc.livro_guardado_ao_pedir[-1] is False


@pytest.mark.parametrize("erro", [BadSymbol("par saiu"), ExchangeError("recusado"), TypeError("defeito")])
def test_erro_com_a_conexao_viva_nao_mexe_no_livro_do_ccxt(erro):
    # Com a conexao ainda viva, apagar o livro do ccxt quebraria a Kraken (a proxima mudanca daria KeyError).
    mexc = CorretoraFalsa([MEXC_BTC_FUT])
    coletor, _ = coletor_de_livros(mexc)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob(), erro)
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 3)
        await coletor.fechar()

    asyncio.run(cenario())
    assert mexc.livro_guardado_ao_pedir == [False, True, True]


def test_par_que_sai_da_lista_na_espera_e_volta_depois_tambem_comeca_sem_o_livro_velho():
    # Internet fora por mais de manter_livro_s: o par sai da lista no meio da espera e o un_watch nao confirma
    # (sem rede). Quando o par volta, a tarefa nova nao pode reaproveitar o livro de antes da queda.
    mexc = CorretoraFalsa([MEXC_BTC_FUT])
    mexc.erro_un_watch = NetworkError("sem rede")
    coletor, _ = coletor_de_livros(mexc, esperar=EsperaFalsa(bloquear=True))

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob(), NetworkError("conexao caiu"))
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 2 and mexc.fila(("livro", "BTC/USDT:USDT"))
                          .empty())
        await asyncio.sleep(0.01)  # a tarefa esta na espera de 1s
        await coletor.assinar_livros(set())  # o par saiu da lista; o un_watch falha (sem rede)
        assert "BTC/USDT:USDT" in mexc.orderbooks
        await coletor.assinar_livros({"BTC/USDT:USDT"})  # voltou
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 3)
        await coletor.fechar()

    asyncio.run(cenario())
    assert mexc.livro_guardado_ao_pedir == [False, True, False]


def test_un_watch_confirmado_esquece_o_livro_do_ccxt():
    mexc = CorretoraFalsa([MEXC_BTC_FUT])
    coletor, _ = coletor_de_livros(mexc)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob())
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 2)
        await coletor.assinar_livros(set())
        await coletor.fechar()

    asyncio.run(cenario())
    assert mexc.desassinados == ["BTC/USDT:USDT"] and "BTC/USDT:USDT" not in mexc.orderbooks


def test_corretora_sem_orderbooks_no_ccxt_nao_quebra_na_queda():
    mexc = CorretoraFalsa([MEXC_BTC_FUT])
    mexc.orderbooks = None  # corretora sem o cache de livros do ccxt
    coletor, estado = coletor_de_livros(mexc)
    no_estado = lambda: estado.obter_livro("mexc", "futuro", "BTC", AGORA)  # noqa: E731

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob(), NetworkError("caiu"), ob(98.0, 98.1))
        await esperar_ate(lambda: no_estado() is not None and no_estado().bids[0][0] == 98.0)
        await coletor.fechar()

    asyncio.run(cenario())


def test_livro_da_mexc_e_assinado_de_novo_depois_do_intervalo():
    mexc = CorretoraFalsa([MEXC_BTC_FUT])
    r = Relogio()
    coletor, estado = coletor_de_livros(mexc, relogio=r, reassinar_livros_s=900)
    no_estado = lambda: estado.obter_livro("mexc", "futuro", "BTC", r.agora)  # noqa: E731

    async def cenario():
        estado.registrar_mensagem("mexc", r.agora)
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob())
        await esperar_ate(lambda: no_estado() is not None)
        r.agora += 899  # antes do intervalo: segue o mesmo livro
        estado.registrar_mensagem("mexc", r.agora)
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob(99.5, 99.6))
        await esperar_ate(lambda: no_estado() is not None and no_estado().bids[0][0] == 99.5)
        assert mexc.desassinados == []
        r.agora += 1  # passou o intervalo: a proxima mudanca entrega e depois a assinatura e refeita
        estado.registrar_mensagem("mexc", r.agora)
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob(99.7, 99.8))
        await esperar_ate(lambda: mexc.desassinados == ["BTC/USDT:USDT"] and len(mexc.livro_guardado_ao_pedir) == 4)
        assert no_estado() is None  # o livro deixou de ser acompanhado ate o novo chegar
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob(99.9, 100.0))
        await esperar_ate(lambda: no_estado() is not None and no_estado().bids[0][0] == 99.9)
        await coletor.fechar()

    asyncio.run(cenario())
    # A assinatura nova comeca sem o livro velho no ccxt (o livro completo vem de novo).
    assert mexc.livro_guardado_ao_pedir[:4] == [False, True, True, False]


def test_sem_intervalo_de_reassinatura_o_livro_nunca_e_reassinado():
    mexc = CorretoraFalsa([MEXC_BTC_FUT])
    r = Relogio()
    coletor, estado = coletor_de_livros(mexc, relogio=r)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob())
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 2)
        r.agora += 100_000
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob(99.0, 99.1))
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 3)
        await coletor.fechar()

    asyncio.run(cenario())
    assert mexc.desassinados == [] and mexc.livro_guardado_ao_pedir == [False, True, True]


def test_reassinatura_sem_un_watch_nao_apaga_o_livro_do_ccxt_e_avisa_uma_vez(caplog):
    mexc = CorretoraFalsa([MEXC_BTC_FUT])
    mexc.erro_un_watch = NotSupported("sem un_watch")
    r = Relogio()
    coletor, _ = coletor_de_livros(mexc, relogio=r, reassinar_livros_s=10)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob())
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 2)
        for _ in range(3):
            r.agora += 10
            mexc.entregar(("livro", "BTC/USDT:USDT"), ob(99.0, 99.1))
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 5)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert mexc.desassinados == ["BTC/USDT:USDT"]  # tentou uma vez; sem un_watch nao tenta mais
    assert all(mexc.livro_guardado_ao_pedir[1:])     # com a assinatura viva, o livro do ccxt nunca e apagado
    assert sum("reassinar_livros_corretoras" in m for m in avisos(caplog)) == 1


def test_rodar_passa_a_reassinatura_so_as_corretoras_da_lista(tmp_path, monkeypatch):
    recebidos = {}
    original = ColetorCcxt.__init__

    def init_anotando(self, nome, *args, **kwargs):
        recebidos[nome] = (kwargs.get("reassinar_livros_s"), kwargs.get("limites_assinatura_mercado"),
                           kwargs.get("livros_reservados_limite_mercado"), kwargs.get("prazo_primeiro_ticker_s"),
                           kwargs.get("prazo_primeiro_livro_s"))
        original(self, nome, *args, **kwargs)

    monkeypatch.setattr(ColetorCcxt, "__init__", init_anotando)
    exchanges = {"mexc": CorretoraFalsa([market_ccxt("BTC/USDT", "BTC"), MEXC_BTC_FUT]),
                 "gate": CorretoraFalsa([MEXC_BTC_FUT])}
    config = cfg_com(corretoras=["mexc", "gate"], banco=str(tmp_path / "s.db"), ciclo_ms=50,
                     reassinar_livros_corretoras=["mexc", "xt"], intervalo_reassinar_livros_s=600,
                     limites_assinatura_mercado={"mexc": {"spot": 30}}, livros_reservados_limite_mercado=8,
                     prazo_primeiro_ticker_s=45, prazo_primeiro_livro_s=50)
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=1), timeout=5))
    assert recebidos == {"mexc": (600, {"spot": 30}, 8, 45, 50), "gate": (None, {}, 8, 45, 50)}


# ---------------------------------------------------------------------------
# I2: intervalo do funding (Bitget pelo formato real do ccxt) e aviso de intervalo ausente
# ---------------------------------------------------------------------------


def _bitget_de_verdade():
    """A Bitget do ccxt instalado, com um contrato e as duas respostas da API trocadas por falsas (sem rede).

    As respostas seguem os exemplos do proprio ccxt: o v2/mix/market/tickers (padrao do fetch_funding_rates)
    nao traz o fundingRateInterval; o v2/mix/market/current-fund-rate (o do fetch_funding_intervals) traz.
    """
    import ccxt.async_support as ccxt_async

    ex = ccxt_async.bitget()
    ex.set_markets([ex.safe_market_structure({
        "id": "XYZUSDT", "symbol": "XYZ/USDT:USDT", "base": "XYZ", "quote": "USDT", "settle": "USDT",
        "baseId": "XYZ", "quoteId": "USDT", "settleId": "USDT", "type": "swap", "spot": False, "swap": True,
        "future": False, "option": False, "contract": True, "linear": True, "inverse": False, "contractSize": 1,
        "active": True, "precision": {"amount": 1, "price": 0.1}, "limits": {}, "taker": 0.0006,
        "info": {"productType": "USDT-FUTURES"},
    })])
    pedidos = []

    async def tickers(params):
        pedidos.append("tickers")
        return {"code": "00000", "data": [{"symbol": "XYZUSDT", "fundingRate": "-0.003", "markPrice": "1",
                                            "ts": "1700000000000"}]}

    async def current(params):
        pedidos.append("current-fund-rate")
        return {"code": "00000", "data": [{"symbol": "XYZUSDT", "fundingRate": "-0.003", "fundingRateInterval": "1",
                                            "nextUpdate": "1700000000000"}]}

    ex.publicMixGetV2MixMarketTickers = tickers
    ex.publicMixGetV2MixMarketCurrentFundRate = current
    return ex, pedidos


def test_funding_da_bitget_com_o_formato_real_do_ccxt_vem_com_o_intervalo_de_1h(caplog):
    async def cenario():
        ex, pedidos = _bitget_de_verdade()
        try:
            dados = DadosLentos()
            await dados.atualizar({"bitget": ex})
            return dados.funding("bitget", "XYZ"), pedidos
        finally:
            await ex.close()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        funding, pedidos = asyncio.run(cenario())
    assert funding == (pytest.approx(-0.3), 1)
    assert "current-fund-rate" in pedidos
    assert not [m for m in avisos(caplog) if "intervalo" in m]


def test_intervalo_do_funding_ausente_vira_8h_com_aviso_uma_vez_por_corretora(caplog):
    gate = CorretoraFalsa([MEXC_BTC_FUT, market_ccxt("ETH/USDT:USDT", "ETH")],
                          funding={"BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001},
                                   "ETH/USDT:USDT": {"symbol": "ETH/USDT:USDT", "fundingRate": 0.0001,
                                                     "interval": "7200000"}})  # ilegivel (ex.: Gate fora da tabela)
    dados = DadosLentos()
    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(dados.atualizar({"gate": gate}))
        asyncio.run(dados.atualizar({"gate": gate}))
    assert dados.funding("gate", "BTC") == (pytest.approx(0.01), 8)
    assert dados.funding("gate", "ETH") == (pytest.approx(0.01), 8)
    linhas = [m for m in avisos(caplog) if "intervalo" in m and "gate" in m]
    assert len(linhas) == 1 and "8h" in linhas[0] and "2 de 2" in linhas[0] and "intervalo_funding_h" in linhas[0]


def test_intervalo_da_config_nao_avisa_nem_pede_os_intervalos():
    gate = CorretoraFalsa([MEXC_BTC_FUT], funding={"BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT",
                                                                     "fundingRate": 0.0001}})
    gate.has["fetchFundingIntervals"] = True
    dados = DadosLentos(intervalos_funding={"gate": 4})
    asyncio.run(dados.atualizar({"gate": gate}))
    assert dados.funding("gate", "BTC") == (pytest.approx(0.01), 4)
    assert gate.chamadas_intervalos == 0


# ---------------------------------------------------------------------------
# I3: limite por (corretora, mercado), volume pela REST e pares sem ticker
# ---------------------------------------------------------------------------


class MexcSpotFalsa(CorretoraFalsa):
    """MEXC spot: so watch_bids_asks (um stream bookTicker por par, sem volume) e o volume pela API REST."""

    def __init__(self, markets, volumes):
        super().__init__(markets, has={"watchBidsAsks": True, "fetchTickers": True})
        self.volumes = dict(volumes)
        self.pedidos_rest = []

    async def fetch_tickers(self, symbols=None, params=None):
        self.pedidos_rest.append(tuple(symbols or ()))
        await asyncio.sleep(0)
        return {s: {"symbol": s, "quoteVolume": v} for s, v in self.volumes.items() if symbols is None or s in symbols}


SEIS_SPOT = {f"T{i}/USDT": market_ccxt(f"T{i}/USDT", f"T{i}") for i in range(6)}
VOLUMES = {f"T{i}/USDT": 1000.0 * (i + 1) for i in range(6)}  # T5 tem o maior volume


def test_limite_por_mercado_corta_os_tickers_e_deixa_a_reserva_para_os_livros(caplog):
    mexc = MexcSpotFalsa(SEIS_SPOT, VOLUMES)
    coletor, estado = coletor_de_livros(mexc, limites_assinatura_mercado={"spot": 5},
                                        livros_reservados_limite_mercado=2)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(sorted(SEIS_SPOT)))
        await esperar_ate(lambda: mexc.pedidos_de("watch_bids_asks"))
        # Os livros: no maximo 5 - 3 tickers = 2, os de maior volume.
        await coletor.assinar_livros({"T5/USDT", "T4/USDT", "T3/USDT", "T0/USDT"})
        assert set(coletor._tarefas_livro) == {"T5/USDT", "T4/USDT"}
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert sorted(mexc.pedidos_de("watch_bids_asks")[0][0]) == ["T3/USDT", "T4/USDT", "T5/USDT"]  # maior volume
    texto = " ".join(avisos(caplog))
    assert "limites_assinatura_mercado" in texto and "3 de 6" in texto and "2 de 4" in texto


def test_sem_limite_por_mercado_nada_muda():
    mexc = MexcSpotFalsa(SEIS_SPOT, VOLUMES)
    coletor, _ = coletor_de_livros(mexc)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(sorted(SEIS_SPOT)))
        await esperar_ate(lambda: mexc.pedidos_de("watch_bids_asks"))
        await coletor.assinar_livros(set(SEIS_SPOT))
        assert set(coletor._tarefas_livro) == set(SEIS_SPOT)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert len(mexc.pedidos_de("watch_bids_asks")[0][0]) == 6 and mexc.pedidos_rest == []


def test_limite_por_mercado_nao_corta_o_outro_mercado():
    markets = {**SEIS_SPOT, "T0/USDT:USDT": market_ccxt("T0/USDT:USDT", "T0"),
               "T1/USDT:USDT": market_ccxt("T1/USDT:USDT", "T1")}
    mexc = MexcSpotFalsa(markets, VOLUMES)
    mexc.has["watchTickers"] = True
    coletor, _ = coletor_de_livros(mexc, limites_assinatura_mercado={"spot": 3}, livros_reservados_limite_mercado=1)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(sorted(markets)))
        await esperar_ate(lambda: mexc.pedidos_de("watch_tickers"))
        await coletor.assinar_livros({"T0/USDT:USDT", "T1/USDT:USDT"})
        assert set(coletor._tarefas_livro) == {"T0/USDT:USDT", "T1/USDT:USDT"}
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    futuros = [simbolos for simbolos, _ in mexc.pedidos_de("watch_tickers") if ":" in simbolos[0]]
    spot = [simbolos for simbolos, _ in mexc.pedidos_de("watch_tickers") if ":" not in simbolos[0]]
    assert futuros == [("T0/USDT:USDT", "T1/USDT:USDT")] and len(spot[0]) == 2


def test_ticker_sem_volume_usa_o_volume_da_api_rest_dos_dados_lentos():
    markets = {"T0/USDT": SEIS_SPOT["T0/USDT"]}
    mexc = MexcSpotFalsa(markets, {"T0/USDT": 7777.0})
    coletor, estado = coletor_de_livros(mexc)
    dados = DadosLentos(estado=estado)
    sem_volume = {"bid": 1.0, "ask": 1.01, "quoteVolume": None, "timestamp": None}

    async def cenario():
        estado.registrar_mensagem("mexc", AGORA)
        tarefa = asyncio.create_task(coletor.manter_tickers(["T0/USDT"]))
        mexc.entregar(("watch_bids_asks", "spot"), {"T0/USDT": sem_volume})
        await esperar_ate(lambda: estado.obter_ticker("mexc", "spot", "T0", AGORA) is not None)
        assert estado.obter_ticker("mexc", "spot", "T0", AGORA)["volume_24h_usd"] is None
        await dados.atualizar({"mexc": mexc})  # dados lentos: o volume 24h vem pela API REST
        mexc.entregar(("watch_bids_asks", "spot"), {"T0/USDT": dict(sem_volume, bid=1.001)})
        await esperar_ate(lambda: estado.obter_ticker("mexc", "spot", "T0", AGORA)["bid"] == 1.001)
        assert estado.obter_ticker("mexc", "spot", "T0", AGORA)["volume_24h_usd"] == 7777.0
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert mexc.pedidos_rest == [("T0/USDT",)]


def test_volume_da_api_rest_so_e_pedido_dos_pares_cujo_ticker_nao_traz_volume():
    mexc = MexcSpotFalsa(SEIS_SPOT, VOLUMES)
    estado = EstadoMercados()
    asyncio.run(DadosLentos(estado=estado).atualizar({"mexc": mexc}))
    assert mexc.pedidos_rest == []


def test_pares_pedidos_que_nunca_mandaram_ticker_avisam_uma_vez(caplog):
    mexc = MexcSpotFalsa({s: SEIS_SPOT[s] for s in ("T0/USDT", "T1/USDT", "T2/USDT")}, VOLUMES)
    r = Relogio()
    coletor, _ = coletor_de_livros(mexc, relogio=r, prazo_primeiro_ticker_s=60)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["T0/USDT", "T1/USDT", "T2/USDT"]))
        mexc.entregar(("watch_bids_asks", "spot"), {"T0/USDT": tk()})
        await esperar_ate(lambda: len(mexc.pedidos_de("watch_bids_asks")) == 2)
        r.agora += 59
        mexc.entregar(("watch_bids_asks", "spot"), {"T0/USDT": tk()})
        await esperar_ate(lambda: len(mexc.pedidos_de("watch_bids_asks")) == 3)
        assert not [m for m in avisos(caplog) if "nunca mandaram ticker" in m]
        r.agora += 1
        mexc.entregar(("watch_bids_asks", "spot"), {"T1/USDT": tk()}, {"T0/USDT": tk()})
        await esperar_ate(lambda: len(mexc.pedidos_de("watch_bids_asks")) == 5)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    linhas = [m for m in avisos(caplog) if "nunca mandaram ticker" in m]
    assert len(linhas) == 1
    assert "mexc" in linhas[0] and "1 de 3" in linhas[0] and "spot" in linhas[0] and "60s" in linhas[0]
    assert "T2/USDT" in linhas[0]


def test_campos_novos_da_coleta_com_os_padroes_e_lidos_do_yaml(tmp_path):
    c = Config()
    assert c.limites_assinatura_mercado == {} and c.livros_reservados_limite_mercado == 10
    assert c.reassinar_livros_corretoras == ["mexc", "xt"] and c.intervalo_reassinar_livros_s == 900
    assert c.prazo_primeiro_ticker_s == 60 and c.intervalo_montar_livro_lighter_ms == 100
    lido = carregar_config(escrever(tmp_path, (
        "corretoras: [mexc, gate]\n"
        "limites_assinatura_mercado:\n  mexc:\n    spot: 30\n"
        "livros_reservados_limite_mercado: 12\n"
        "reassinar_livros_corretoras: []\n"
        "intervalo_reassinar_livros_s: 600\n"
        "prazo_primeiro_ticker_s: 90\n"
        "intervalo_montar_livro_lighter_ms: 0\n"
    )))
    assert lido.limites_assinatura_mercado == {"mexc": {"spot": 30}}
    assert (lido.livros_reservados_limite_mercado, lido.reassinar_livros_corretoras) == (12, [])
    assert (lido.intervalo_reassinar_livros_s, lido.prazo_primeiro_ticker_s) == (600, 90)
    assert lido.intervalo_montar_livro_lighter_ms == 0


@pytest.mark.parametrize("texto, campo", [
    ("limites_assinatura_mercado: 30\n", "limites_assinatura_mercado"),
    ("limites_assinatura_mercado:\n  mexc: 30\n", "limites_assinatura_mercado"),
    ("limites_assinatura_mercado:\n  mexc:\n    margem: 30\n", "limites_assinatura_mercado"),
    ("limites_assinatura_mercado:\n  mexc:\n    spot: 0\n", "limites_assinatura_mercado"),
    ("limites_assinatura_mercado:\n  mexc:\n    spot: 2.5\n", "limites_assinatura_mercado"),
    ("limites_assinatura_mercado:\n  mexcc:\n    spot: 30\n", "limites_assinatura_mercado.*mexcc"),
    ("limites_assinatura_mercado:\n  mexc:\n    spot: 10\n", "limites_assinatura_mercado.*livros_reservados"),
    ("livros_reservados_limite_mercado: 0\n", "livros_reservados_limite_mercado"),
    ("livros_reservados_limite_mercado: 2.5\n", "livros_reservados_limite_mercado"),
    ("reassinar_livros_corretoras: mexc\n", "reassinar_livros_corretoras"),
    ("intervalo_reassinar_livros_s: 0\n", "intervalo_reassinar_livros_s"),
    ("prazo_primeiro_ticker_s: -1\n", "prazo_primeiro_ticker_s"),
    ("intervalo_montar_livro_lighter_ms: -1\n", "intervalo_montar_livro_lighter_ms"),
    ("intervalo_montar_livro_lighter_ms: rapido\n", "intervalo_montar_livro_lighter_ms"),
])
def test_campos_novos_da_coleta_com_formato_errado(tmp_path, texto, campo):
    with pytest.raises(ErroConfig, match=campo):
        carregar_config(escrever(tmp_path, texto))


def test_config_yaml_do_projeto_limita_a_mexc_spot_a_30_e_usa_o_bbo_da_kraken():
    from pathlib import Path

    c = carregar_config(str(Path(__file__).resolve().parent.parent / "config.yaml"))
    assert c.limites_assinatura_mercado == {"mexc": {"spot": 30}}
    assert c.metodo_ticker.get("kraken") == "watch_bids_asks"


# ---------------------------------------------------------------------------
# M1: conexao viva por (corretora, mercado)
# ---------------------------------------------------------------------------


def test_spot_e_futuro_com_conexoes_separadas_nao_se_mantem_vivos():
    e = EstadoMercados()
    e.registrar_mensagem("gate", 100.0, mercado="spot")
    e.registrar_mensagem("gate", 90.0, mercado="futuro")
    assert e.conexao_viva("gate", 101.0, mercado="spot") is True
    assert e.conexao_viva("gate", 101.0, mercado="futuro") is False
    assert e.conexao_viva("gate", 101.0) is True  # sem mercado: a corretora toda (como antes)
    e.atualizar_ticker("gate", "futuro", "BTC", 99.0, 100.0, 100.0)
    e.atualizar_ticker("gate", "spot", "BTC", 99.0, 100.0, 100.0)
    e.atualizar_livro(ob_livro("gate", "futuro", 100.0), ativo="BTC")
    assert e.obter_ticker("gate", "futuro", "BTC", 101.0) is None
    assert e.obter_livro("gate", "futuro", "BTC", 101.0) is None
    assert e.obter_ticker("gate", "spot", "BTC", 101.0) is not None


def ob_livro(corretora, mercado, quando):
    from tests.fabricas import livro

    return livro(corretora, mercado, "BTC", quando)


def test_mensagem_sem_mercado_vale_para_todos_os_mercados_da_corretora():
    e = EstadoMercados()
    e.registrar_mensagem("lighter", 100.0)
    assert e.conexao_viva("lighter", 101.0, mercado="futuro") and e.conexao_viva("lighter", 101.0, mercado="spot")
    e.registrar_mensagem("gate", 100.0, mercado="spot")
    e.marcar_desconectada("gate")  # apaga a corretora inteira, os dois mercados
    assert not e.conexao_viva("gate", 100.0) and not e.conexao_viva("gate", 100.0, mercado="spot")


def test_cotacao_usa_a_conexao_do_spot():
    e = EstadoMercados()
    e.registrar_mensagem("bitget", 100.0, mercado="futuro")
    e.atualizar_cotacao("bitget", "USDC/USDT", 0.999, 1.0, 100.0)
    assert e.obter_cotacao("bitget", "USDC/USDT", 100.5) is None
    e.registrar_mensagem("bitget", 100.0, mercado="spot")
    assert e.obter_cotacao("bitget", "USDC/USDT", 100.5) is not None


def test_coletor_registra_a_mensagem_no_mercado_dela():
    gate = CorretoraFalsa([market_ccxt("BTC/USDT", "BTC"), MEXC_BTC_FUT])
    coletor, estado = coletor_de_livros(gate, nome="gate")

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT", "BTC/USDT:USDT"]))
        gate.entregar(("watch_tickers", "spot"), {"BTC/USDT": tk()})
        await esperar_ate(lambda: estado.conexao_viva("gate", AGORA, mercado="spot"))
        assert not estado.conexao_viva("gate", AGORA, mercado="futuro")
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        gate.entregar(("livro", "BTC/USDT:USDT"), ob())
        await esperar_ate(lambda: estado.conexao_viva("gate", AGORA, mercado="futuro"))
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


# ---------------------------------------------------------------------------
# M2: a recarga ajusta os tickers de cada corretora logo depois da recarga dela
# ---------------------------------------------------------------------------


class CorretoraQueDemoraARecarregar(CorretoraFalsa):
    def __init__(self, markets):
        super().__init__(markets)
        self.liberar = asyncio.Event()

    async def load_markets(self, reload=False, params=None):
        await self.liberar.wait()
        return await super().load_markets(reload, params)


def test_recarga_ajusta_os_tickers_de_quem_ja_recarregou_sem_esperar_as_outras():
    gate = CorretoraFalsa({"BTC/USDT": market_ccxt("BTC/USDT", "BTC"), "ETH/USDT": market_ccxt("ETH/USDT", "ETH"),
                           "ETH/USDT:USDT": market_ccxt("ETH/USDT:USDT", "ETH")})
    estado = EstadoMercados()

    async def cenario():
        bitget = CorretoraQueDemoraARecarregar({"BTC/USDT:USDT": MEXC_BTC_FUT})
        exchanges = {"gate": gate, "bitget": bitget}
        coletores = {n: ColetorCcxt(n, ex, estado=estado, relogio=relogio) for n, ex in exchanges.items()}
        tickers = principal._Tickers(coletores)
        tickers.iniciar("gate", ["BTC/USDT", "ETH/USDT", "ETH/USDT:USDT"])
        tickers.iniciar("bitget", ["BTC/USDT:USDT"])
        gate.proximos_markets = {"BTC/USDT": market_ccxt("BTC/USDT", "BTC")}  # a gate tirou o ETH
        recarga = asyncio.create_task(principal._recarregar_mercados(exchanges, coletores, tickers, estado))
        # A bitget ainda nao recarregou, mas a gate ja: o stream dela nao pode continuar pedindo o ETH.
        await esperar_ate(lambda: tickers.simbolos.get("gate") == ["BTC/USDT"])
        assert not recarga.done()
        bitget.liberar.set()
        await recarga
        for tarefa in tickers.tarefas.values():
            await parar(tarefa)

    asyncio.run(cenario())


def test_recarga_com_erro_no_ajuste_de_uma_corretora_nao_impede_a_outra(monkeypatch, caplog):
    gate, bitget = CorretoraFalsa({"BTC/USDT": market_ccxt("BTC/USDT", "BTC")}), CorretoraFalsa([MEXC_BTC_FUT])
    estado = EstadoMercados()
    chamadas = []
    original = principal._ajustar_tickers

    async def ajustar(coletores, tickers, estado_):
        chamadas.append(1)
        if len(chamadas) == 1:
            raise RuntimeError("defeito no ajuste")
        await original(coletores, tickers, estado_)

    monkeypatch.setattr(principal, "_ajustar_tickers", ajustar)

    async def cenario():
        exchanges = {"gate": gate, "bitget": bitget}
        coletores = {n: ColetorCcxt(n, ex, estado=estado, relogio=relogio) for n, ex in exchanges.items()}
        tickers = principal._Tickers(coletores)
        await principal._recarregar_mercados(exchanges, coletores, tickers, estado)
        for tarefa in tickers.tarefas.values():
            await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert len(chamadas) == 2
    assert any("defeito no ajuste" in r.getMessage() or (r.exc_info and "defeito" in str(r.exc_info[1]))
               for r in caplog.records)


# ---------------------------------------------------------------------------
# M3: so as moedas de cotacao de PREFERENCIA_COTACAO entram no indice
# ---------------------------------------------------------------------------


def test_pares_em_eur_e_gbp_ficam_fora_do_indice_com_aviso_uma_vez(caplog):
    markets = {
        "BTC/USD": market_ccxt("BTC/USD", "BTC", quote="USD"),
        "BTC/EUR": market_ccxt("BTC/EUR", "BTC", quote="EUR"),
        "ADA/EUR": market_ccxt("ADA/EUR", "ADA", quote="EUR"),   # so em EUR: fica de fora
        "DOT/GBP": market_ccxt("DOT/GBP", "DOT", quote="GBP"),
        "ETH/USDT": market_ccxt("ETH/USDT", "ETH"),
    }
    kraken = CorretoraFalsa(markets)
    coletor = ColetorCcxt("kraken", kraken, estado=EstadoMercados(), relogio=relogio)
    with caplog.at_level(logging.INFO, logger="scanner"):
        chaves = coletor.simbolos_por_chave()
        kraken.markets = dict(markets)  # recarga: o indice e refeito, sem repetir o aviso
        coletor.simbolos_por_chave()
    assert chaves == {("spot", "BTC"): "BTC/USD", ("spot", "ETH"): "ETH/USDT"}
    linhas = [r.getMessage() for r in caplog.records if "EUR" in r.getMessage() and "kraken" in r.getMessage()]
    assert len(linhas) == 1 and "3 pares" in linhas[0] and "GBP" in linhas[0]


def test_ticker_de_par_em_eur_nao_vai_ao_estado():
    kraken = CorretoraFalsa({"ADA/EUR": market_ccxt("ADA/EUR", "ADA", quote="EUR")})
    coletor, estado = coletor_de_livros(kraken, nome="kraken")

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["ADA/EUR"]))
        kraken.entregar(("watch_tickers", "spot"), {"ADA/EUR": tk(0.5, 0.51)})
        await esperar_ate(lambda: estado.conexao_viva("kraken", AGORA))
        await parar(tarefa)

    asyncio.run(cenario())
    assert estado.mercados() == {}


def test_funding_de_futuro_cotado_fora_das_moedas_usadas_e_ignorado():
    eur = market_ccxt("BTC/EUR:EUR", "BTC", quote="EUR")
    ex = CorretoraFalsa([eur], funding={"BTC/EUR:EUR": {"symbol": "BTC/EUR:EUR", "fundingRate": 0.0001}})
    dados = DadosLentos()
    asyncio.run(dados.atualizar({"x": ex}))
    assert dados.funding("x", "BTC") is None


# ---------------------------------------------------------------------------
# M4/M5: funding gravado assim que chega, so dos simbolos em uso, do contrato ativo
# ---------------------------------------------------------------------------


class FundingUmAUmFalso(CorretoraFalsa):
    """Sem funding em lote: cada fetch_funding_rate espera o teste liberar aquele simbolo."""

    def __init__(self, markets, funding):
        super().__init__(markets, has={"fetchFundingRate": True}, funding=funding)
        self.perguntados = []
        self.liberados: dict[str, asyncio.Event] = {}

    def liberar(self, simbolo):
        self.liberados.setdefault(simbolo, asyncio.Event()).set()

    async def fetch_funding_rate(self, symbol, params=None):
        self.perguntados.append(symbol)
        await self.liberados.setdefault(symbol, asyncio.Event()).wait()
        return self.funding[symbol]


FUTUROS = {f"{a}/USDT:USDT": market_ccxt(f"{a}/USDT:USDT", a) for a in ("BTC", "ETH", "SOL")}
FUNDINGS = {s: {"symbol": s, "fundingRate": 0.0001, "interval": "4h"} for s in FUTUROS}


def test_funding_um_a_um_e_gravado_assim_que_chega():
    async def cenario():
        mexc = FundingUmAUmFalso(FUTUROS, FUNDINGS)
        dados = DadosLentos()
        tarefa = asyncio.create_task(dados.atualizar({"mexc": mexc}))
        mexc.liberar("BTC/USDT:USDT")
        await esperar_ate(lambda: dados.funding("mexc", "BTC") is not None)
        assert dados.funding("mexc", "ETH") is None and not tarefa.done()  # o ETH ainda nao respondeu
        mexc.liberar("ETH/USDT:USDT")
        mexc.liberar("SOL/USDT:USDT")
        await tarefa
        return dados

    dados = asyncio.run(cenario())
    assert dados.funding("mexc", "SOL") == (pytest.approx(0.01), 4)


def test_funding_um_a_um_so_pergunta_os_simbolos_em_uso_nos_tickers():
    async def cenario():
        mexc = FundingUmAUmFalso({**FUTUROS, "BTC/USDT": market_ccxt("BTC/USDT", "BTC")}, FUNDINGS)
        for simbolo in FUTUROS:
            mexc.liberar(simbolo)
        dados = DadosLentos()
        dados.usar_simbolos("mexc", ["BTC/USDT", "ETH/USDT:USDT"])  # o spot nao tem funding
        await dados.atualizar({"mexc": mexc})
        return dados, mexc

    dados, mexc = asyncio.run(cenario())
    assert mexc.perguntados == ["ETH/USDT:USDT"]
    assert dados.funding("mexc", "ETH") is not None and dados.funding("mexc", "BTC") is None


def test_corretora_sem_simbolos_em_uso_nao_pergunta_nada_depois_que_outra_informou():
    async def cenario():
        mexc = FundingUmAUmFalso(FUTUROS, FUNDINGS)
        dados = DadosLentos()
        dados.usar_simbolos("gate", ["BTC/USDT:USDT"])  # o scanner informa as listas; a mexc nao tem nenhuma
        await dados.atualizar({"mexc": mexc})
        return mexc

    assert asyncio.run(cenario()).perguntados == []


def test_funding_em_lote_fica_com_o_contrato_ativo():
    usdt = dict(market_ccxt("BTC/USDT:USDT", "BTC"), active=False)  # suspenso: o coletor tambem pula
    usdc = market_ccxt("BTC/USDC:USDC", "BTC", quote="USDC")
    ex = CorretoraFalsa([usdt, usdc], funding={
        "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0009, "interval": "8h"},
        "BTC/USDC:USDC": {"symbol": "BTC/USDC:USDC", "fundingRate": 0.0001, "interval": "8h"},
    })
    dados = DadosLentos()
    asyncio.run(dados.atualizar({"x": ex}))
    assert dados.funding("x", "BTC") == (pytest.approx(0.01), 8)


def test_funding_em_lote_com_simbolos_em_uso_fica_com_o_do_indice_do_coletor():
    usdt = market_ccxt("BTC/USDT:USDT", "BTC")
    usdc = market_ccxt("BTC/USDC:USDC", "BTC", quote="USDC")
    ex = CorretoraFalsa([usdt, usdc], funding={
        "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0009, "interval": "8h"},
        "BTC/USDC:USDC": {"symbol": "BTC/USDC:USDC", "fundingRate": 0.0001, "interval": "8h"},
    })
    dados = DadosLentos()
    dados.usar_simbolos("x", ["BTC/USDC:USDC"])  # o coletor usa o USDC (ex.: mercados_por_corretora)
    asyncio.run(dados.atualizar({"x": ex}))
    assert dados.funding("x", "BTC") == (pytest.approx(0.01), 8)


def test_tickers_informam_aos_dados_lentos_os_simbolos_em_uso():
    gate = CorretoraFalsa([MEXC_BTC_FUT])
    dados = DadosLentos()

    async def cenario():
        coletores = {"gate": ColetorCcxt("gate", gate, estado=EstadoMercados(), relogio=relogio)}
        tickers = principal._Tickers(coletores, dados)
        tickers.iniciar("gate", ["BTC/USDT:USDT"])
        assert dados.simbolos_em_uso("gate") == {"BTC/USDT:USDT"}
        await tickers.parar("gate")
        assert dados.simbolos_em_uso("gate") == set()

    asyncio.run(cenario())


# ---------------------------------------------------------------------------
# M6: livro da Lighter montado no maximo a cada intervalo_montar_livro_lighter_ms
# ---------------------------------------------------------------------------

MARKETS_LIGHTER, _ = markets_de_lighter({"order_book_details": [
    {"symbol": "ETH", "market_id": 0, "status": "active", "taker_fee": "0.0000", "size_decimals": 4,
     "price_decimals": 2, "market_type": "perp"}]}, "USDC")
ETH = "ETH/USDC:USDC"


def _nivel(preco, tamanho):
    return {"price": str(preco), "size": str(tamanho)}


def _livro_lighter(bids, asks, nonce, begin_nonce=None, tipo="update/order_book", ts=None):
    return {"channel": "order_book:0", "type": tipo, "timestamp": ts,
            "order_book": {"bids": [_nivel(*n) for n in bids], "asks": [_nivel(*n) for n in asks], "nonce": nonce,
                           "begin_nonce": begin_nonce}}


class EsperaComPortao:
    """Espera que so termina quando o teste abre o portao; anota cada pedido."""

    def __init__(self):
        self.esperas = []
        self.portao = asyncio.Event()

    async def __call__(self, segundos):
        self.esperas.append(segundos)
        await self.portao.wait()


def test_livro_da_lighter_montado_no_maximo_a_cada_intervalo_sem_perder_a_ultima_mudanca():
    estado = EstadoMercados()
    r = Relogio()
    completo = _livro_lighter([(100.0, 1.0)], [(100.5, 1.0)], nonce=1, tipo="subscribed/order_book")
    ws = WsLighterFalso(respostas={"order_book/0": [[completo]]})

    async def cenario():
        espera = EsperaComPortao()
        cliente = ClienteLighterFalso(MARKETS_LIGHTER, [ws])
        coletor = ColetorLighter("principal", cliente, estado=estado, relogio=r, intervalo_montar_livro_ms=100,
                                 esperar_livro=espera)
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        await esperar_ate(lambda: estado.obter_livro("lighter", "futuro", "ETH", r.agora) is not None)
        montagens = []
        original = coletor._entregar_livro
        coletor._entregar_livro = lambda livro: (montagens.append(livro), original(livro))
        # Duas mudancas 30 ms e 60 ms depois: nenhuma montagem agora; uma so, com as duas, depois do intervalo.
        r.agora += 0.03
        ws.entregar(_livro_lighter([(100.1, 2.0)], [], nonce=2, begin_nonce=1))
        await esperar_ate(lambda: ws.vazia() and espera.esperas)
        r.agora += 0.03
        ws.entregar(_livro_lighter([(100.2, 3.0)], [], nonce=3, begin_nonce=2))
        await esperar_ate(lambda: ws.vazia())
        await asyncio.sleep(0.01)
        assert montagens == [] and estado.obter_livro("lighter", "futuro", "ETH", r.agora).bids[0][0] == 100.0
        assert espera.esperas == [pytest.approx(0.07)]  # o que faltava do intervalo depois da 1a mudanca
        espera.portao.set()
        await esperar_ate(lambda: montagens)
        livro = estado.obter_livro("lighter", "futuro", "ETH", r.agora)
        assert livro.bids[:3] == [(100.2, 3.0), (100.1, 2.0), (100.0, 1.0)]
        assert livro.recebido_em == pytest.approx(AGORA + 0.06)  # o horario da ultima mudanca
        assert len(montagens) == 1
        # Passado o intervalo, a proxima mudanca monta na hora.
        r.agora += 0.2
        ws.entregar(_livro_lighter([(100.3, 1.0)], [], nonce=4, begin_nonce=3))
        await esperar_ate(lambda: len(montagens) == 2)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


def test_livro_da_lighter_com_intervalo_zero_monta_a_cada_mensagem():
    estado = EstadoMercados()
    completo = _livro_lighter([(100.0, 1.0)], [(100.5, 1.0)], nonce=1, tipo="subscribed/order_book")
    ws = WsLighterFalso(respostas={"order_book/0": [[completo]]})

    async def cenario():
        coletor = ColetorLighter("principal", ClienteLighterFalso(MARKETS_LIGHTER, [ws]), estado=estado,
                                 relogio=relogio, intervalo_montar_livro_ms=0)
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        await esperar_ate(lambda: estado.obter_livro("lighter", "futuro", "ETH", AGORA) is not None)
        ws.entregar(_livro_lighter([(100.1, 2.0)], [], nonce=2, begin_nonce=1))
        await esperar_ate(lambda: estado.obter_livro("lighter", "futuro", "ETH", AGORA).bids[0][0] == 100.1)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


def test_livro_quebrado_no_meio_do_intervalo_nao_e_montado_depois():
    estado = EstadoMercados()
    r = Relogio()
    completo = _livro_lighter([(100.0, 1.0)], [(100.5, 1.0)], nonce=1, tipo="subscribed/order_book")
    ws = WsLighterFalso(respostas={"order_book/0": [[completo], []]})

    async def cenario():
        espera = EsperaComPortao()
        coletor = ColetorLighter("principal", ClienteLighterFalso(MARKETS_LIGHTER, [ws]), estado=estado, relogio=r,
                                 intervalo_montar_livro_ms=100, esperar_livro=espera)
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        await esperar_ate(lambda: estado.obter_livro("lighter", "futuro", "ETH", r.agora) is not None)
        r.agora += 0.01
        ws.entregar(_livro_lighter([(100.1, 2.0)], [], nonce=2, begin_nonce=1))
        await esperar_ate(lambda: espera.esperas)
        ws.entregar(_livro_lighter([(100.2, 2.0)], [], nonce=9, begin_nonce=7))  # quebrou: sai do estado
        await esperar_ate(lambda: ws.canais().count("order_book/0") == 2)
        espera.portao.set()
        await asyncio.sleep(0.02)
        assert estado.obter_livro("lighter", "futuro", "ETH", r.agora) is None
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


def test_criar_coletor_lighter_le_o_intervalo_de_montagem_da_config():
    coletor = criar_coletor_lighter("lighter", ClienteLighterFalso(MARKETS_LIGHTER),
                                    cfg_com(corretoras=["lighter"], intervalo_montar_livro_lighter_ms=250,
                                            prazo_primeiro_ticker_s=30),
                                    EstadoMercados(), None, relogio)
    assert coletor._intervalo_montar_s == pytest.approx(0.25) and coletor._prazo_primeiro_ticker_s == 30


# ---------------------------------------------------------------------------
# M7: Kraken spot com watch_bids_asks (gatilho bbo), com volume
# ---------------------------------------------------------------------------


def test_kraken_com_watch_bids_asks_assina_o_ticker_com_gatilho_bbo_e_traz_o_volume():
    # O ccxt de verdade monta a assinatura e le a mensagem; so o envio (watch_multiple) e trocado, sem rede.
    import ccxt.pro as ccxtpro

    enviadas = []

    async def cenario():
        ex = ccxtpro.kraken()
        ex.set_markets([ex.safe_market_structure({
            "id": "XBTUSDT", "symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "baseId": "XBT",
            "quoteId": "USDT", "type": "spot", "spot": True, "swap": False, "future": False, "option": False,
            "contract": False, "active": True, "precision": {"amount": 0.0001, "price": 0.1}, "limits": {},
            "altname": "XBTUSDT", "wsId": "BTC/USDT",
        })])
        mensagem = {"channel": "ticker", "type": "update", "data": [{
            "symbol": "BTC/USDT", "bid": 67000.0, "bid_qty": 1.0, "ask": 67000.5, "ask_qty": 2.0, "last": 67000.2,
            "volume": 100.0, "vwap": 66900.0, "low": 66000.0, "high": 68000.0, "change": 500.0, "change_pct": 0.75}]}

        class ClienteFalso:
            def resolve(self, valor, hash_):
                self.valor = valor

        async def watch_multiple(url, hashes, request, subscribe_hashes=None, subscription=None):
            enviadas.append((hashes, request))
            if len(enviadas) > 1:
                await asyncio.sleep(3600)
            cliente = ClienteFalso()
            ex.handle_ticker(cliente, mensagem)
            return cliente.valor

        ex.watch_multiple = watch_multiple
        estado = EstadoMercados()
        coletor = ColetorCcxt("kraken", ex, estado=estado, relogio=relogio, metodo_ticker="watch_bids_asks",
                              mercados=("spot",))
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT"]))
        await esperar_ate(lambda: estado.obter_ticker("kraken", "spot", "BTC", AGORA) is not None)
        guardado = estado.obter_ticker("kraken", "spot", "BTC", AGORA)
        await parar(tarefa)
        await ex.close()
        return guardado

    guardado = asyncio.run(cenario())
    hashes, pedido = enviadas[0]
    assert pedido["params"]["channel"] == "ticker" and pedido["params"]["event_trigger"] == "bbo"
    assert hashes == ["ticker@BTC/USDT"]  # com o gatilho, o ccxt espera a mensagem do canal ticker
    assert (guardado["bid"], guardado["ask"]) == (67000.0, 67000.5)
    assert guardado["volume_24h_usd"] == pytest.approx(100.0 * 66900.0)
    assert guardado["variacao_24h_pct"] == pytest.approx(0.75)


# ===========================================================================
# Correcao pos-re-revisao (R1, R2, m1, m2, m3, m6)
# ===========================================================================


class MexcDeVerdade:
    """A MEXC do ccxt 4.5.89 com uma conexao falsa: o que o ccxt mandaria fica anotado, nada sai para a rede.

    `ativas` conta as assinaturas da conexao spot (SUBSCRIPTION soma, UNSUBSCRIPTION subtrai) e `maximo` guarda
    o maior valor visto. As mensagens da MEXC entram pelo `entregar` (exchange.handle_message, como o cliente WS).
    """

    def __init__(self, n=40, volumes=None):
        import ccxt.pro as ccxtpro

        self.ex = ccxtpro.mexc()
        self.ex.enableRateLimit = False  # sem a fila de envio do ccxt (a conexao e falsa): o teste nao espera
        self.simbolos = [f"T{i:02d}/USDT" for i in range(n)]
        self.ex.set_markets([self.ex.safe_market_structure({
            "id": f"T{i:02d}USDT", "symbol": f"T{i:02d}/USDT", "base": f"T{i:02d}", "quote": "USDT",
            "baseId": f"T{i:02d}", "quoteId": "USDT", "type": "spot", "spot": True, "swap": False, "future": False,
            "option": False, "contract": False, "active": True, "precision": {"amount": 0.0001, "price": 0.01},
            "limits": {}}) for i in range(n)])
        self.volumes = dict(volumes or {s: 1000.0 + i for i, s in enumerate(self.simbolos)})
        self.cliente = self.ex.client(self.ex.urls["api"]["ws"]["spot"])
        pronto = asyncio.get_running_loop().create_future()
        pronto.set_result(True)
        self.cliente.connected = pronto
        self.enviadas = []
        self.ativas = 0
        self.maximo = 0

        async def enviar(msg):
            self.enviadas.append(msg)
            n_params = len(msg.get("params", []))
            self.ativas += n_params if msg.get("method") == "SUBSCRIPTION" else -n_params
            self.maximo = max(self.maximo, self.ativas)

        async def fetch_tickers(symbols=None, params={}):
            return {s: {"symbol": s, "quoteVolume": self.volumes[s]} for s in (symbols or self.simbolos)}

        self.cliente.send = enviar
        self.ex.fetch_tickers = fetch_tickers

    def entregar(self, mensagem):
        self.ex.handle_message(self.cliente, mensagem)

    def assinados(self, tipo):
        """Os ids assinados agora no canal `tipo` ("bookTicker" ou "depth"), pela ordem das mensagens."""
        ids = set()
        for msg in self.enviadas:
            for canal in msg.get("params", []):
                if tipo in canal:
                    (ids.add if msg.get("method") == "SUBSCRIPTION" else ids.discard)(canal.rsplit("@", 1)[-1])
        return ids


def _protobuf_book_ticker(ident, bid, ask):
    from ccxt.protobuf.mexc import PushDataV3ApiWrapper_pb2 as pb

    m = pb.PushDataV3ApiWrapper(channel=f"spot@public.aggre.bookTicker.v3.api.pb@100ms@{ident}", symbol=ident,
                                sendTime=int(AGORA * 1000))
    m.publicAggreBookTicker.bidPrice, m.publicAggreBookTicker.bidQuantity = str(bid), "2"
    m.publicAggreBookTicker.askPrice, m.publicAggreBookTicker.askQuantity = str(ask), "3"
    return m.SerializeToString()


def _protobuf_mudanca(ident, versao, bid, ask):
    from ccxt.protobuf.mexc import PushDataV3ApiWrapper_pb2 as pb

    m = pb.PushDataV3ApiWrapper(channel=f"spot@public.aggre.depth.v3.api.pb@100ms@{ident}", symbol=ident,
                                sendTime=int(AGORA * 1000))
    d = m.publicAggreDepths
    d.eventType, d.fromVersion, d.toVersion = "spot@public.aggre.depth.v3.api.pb@100ms", str(versao), str(versao)
    nb, na = d.bids.add(), d.asks.add()
    nb.price, nb.quantity, na.price, na.quantity = str(bid), "1.5", str(ask), "2.5"
    return m.SerializeToString()


async def _ceder(vezes=20):
    for _ in range(vezes):
        await asyncio.sleep(0)


def coletor_mexc_spot(mexc, **kwargs):
    estado = EstadoMercados()
    kwargs.setdefault("metodo_ticker", "watch_ticker")  # o config.yaml de exemplo (R1)
    coletor = ColetorCcxt("mexc", mexc.ex, estado=estado, relogio=relogio, mercados=("spot",),
                          limites_assinatura_mercado={"spot": 30}, livros_reservados_limite_mercado=10,
                          timeout_desassinar_s=1, **kwargs)
    return coletor, estado


def test_mexc_spot_com_protobuf_de_verdade_ticker_e_livro_chegam_ao_estado():
    # R1: o bookTicker e o livro da MEXC spot chegam em protobuf; com watch_ticker o ccxt resolve o ticker.
    async def cenario():
        mexc = MexcDeVerdade()
        coletor, estado = coletor_mexc_spot(mexc)
        tarefa = asyncio.create_task(coletor.manter_tickers(mexc.simbolos))
        await esperar_ate(lambda: len(mexc.assinados("bookTicker")) == 20)
        mexc.entregar(_protobuf_book_ticker("T39USDT", 1.00, 1.01))
        await esperar_ate(lambda: estado.obter_ticker("mexc", "spot", "T39", AGORA) is not None)
        ticker = estado.obter_ticker("mexc", "spot", "T39", AGORA)

        async def snapshot(symbol, limit=None, params={}):
            livro = mexc.ex.parse_order_book({"bids": [[0.99, 4]], "asks": [[1.02, 6]]}, symbol)
            livro["nonce"] = 100
            return livro

        mexc.ex.fetch_rest_order_book_safe = snapshot
        await coletor.assinar_livros({"T39/USDT"})
        await esperar_ate(lambda: "T39USDT" in mexc.assinados("depth"))
        mexc.entregar({"id": 0, "code": 0, "msg": "spot@public.aggre.depth.v3.api.pb@100ms@T39USDT"})
        for versao in range(101, 127):  # depois da 25a mudanca o ccxt pede o livro completo (snapshot REST)
            mexc.entregar(_protobuf_mudanca("T39USDT", versao, 1.00, 1.01))
            await asyncio.sleep(0)
        await esperar_ate(lambda: estado.obter_livro("mexc", "spot", "T39", AGORA) is not None)
        livro = estado.obter_livro("mexc", "spot", "T39", AGORA)
        await parar(tarefa)
        await coletor.fechar()
        return ticker, livro, mexc

    ticker, livro, mexc = asyncio.run(cenario())
    assert (ticker["bid"], ticker["ask"], ticker["volume_24h_usd"]) == (1.0, 1.01, 1039.0)  # volume pela REST
    assert livro.bids[:2] == [(1.0, 1.5), (0.99, 4.0)] and livro.asks[:2] == [(1.01, 2.5), (1.02, 6.0)]
    assert mexc.maximo <= 30


def test_mexc_spot_com_watch_bids_asks_nao_entrega_ticker_no_ccxt_4_5_89():
    # Registro do porque do metodo_ticker: o bookTicker protobuf resolve "ticker:", nunca "bidask:".
    async def cenario():
        mexc = MexcDeVerdade(n=3)
        coletor, estado = coletor_mexc_spot(mexc, metodo_ticker=None)
        tarefa = asyncio.create_task(coletor.manter_tickers(mexc.simbolos))
        await esperar_ate(lambda: len(mexc.assinados("bookTicker")) == 3)
        mexc.entregar(_protobuf_book_ticker("T00USDT", 1.00, 1.01))
        await _ceder()
        ticker = estado.obter_ticker("mexc", "spot", "T00", AGORA)
        await parar(tarefa)
        await coletor.fechar()
        return ticker

    assert asyncio.run(cenario()) is None


def test_mercado_sem_nenhum_ticker_e_avisado_na_conferencia_periodica(caplog):
    # R1: sem nenhum ticker, o aviso nao pode depender de um ticker chegar.
    mexc = MexcSpotFalsa({s: SEIS_SPOT[s] for s in ("T0/USDT", "T1/USDT")}, VOLUMES)
    r = Relogio()
    coletor, _ = coletor_de_livros(mexc, relogio=r, prazo_primeiro_ticker_s=60)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["T0/USDT", "T1/USDT"]))
        await esperar_ate(lambda: mexc.pedidos_de("watch_bids_asks"))
        r.agora += 30
        coletor.tirar_descartes()
        assert not [m for m in avisos(caplog) if "nunca mandaram ticker" in m]
        r.agora += 30
        coletor.tirar_descartes()  # o __main__ chama a cada intervalo_log_recursos_s
        coletor.tirar_descartes()
        await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    linhas = [m for m in avisos(caplog) if "nunca mandaram ticker" in m]
    assert len(linhas) == 1 and "2 de 2" in linhas[0] and "spot" in linhas[0]


def test_config_yaml_do_projeto_usa_watch_ticker_na_mexc():
    from pathlib import Path

    c = carregar_config(str(Path(__file__).resolve().parent.parent / "config.yaml"))
    assert c.metodo_ticker.get("mexc") == "watch_ticker"


def test_troca_de_livro_na_mexc_spot_nunca_passa_de_30_assinaturas():
    # R2: com 20 tickers e 10 livros, o livro que sai e cancelado antes do novo ser assinado.
    async def cenario():
        mexc = MexcDeVerdade()
        coletor, _ = coletor_mexc_spot(mexc)
        tarefa = asyncio.create_task(coletor.manter_tickers(mexc.simbolos))
        await esperar_ate(lambda: len(mexc.assinados("bookTicker")) == 20)
        tickers = sorted(mexc.assinados("bookTicker"))
        primeiros = {f"{i[:3]}/USDT" for i in tickers[:10]}
        await coletor.assinar_livros(primeiros)
        await esperar_ate(lambda: len(mexc.assinados("depth")) == 10)
        assert mexc.ativas == 30
        trocado = (primeiros - {f"{tickers[0][:3]}/USDT"}) | {f"{tickers[10][:3]}/USDT"}
        await coletor.assinar_livros(trocado)
        await esperar_ate(lambda: tickers[10] in mexc.assinados("depth"))
        await _ceder()
        await parar(tarefa)
        await coletor.fechar()
        return mexc

    mexc = asyncio.run(cenario())
    assert mexc.maximo == 30 and mexc.ativas == 30


def test_tickers_que_saem_do_limite_recebem_un_watch_antes_dos_novos():
    # R2: reiniciar os tickers (recarga, corretora que entra) com outro top 20 cancela os que saem antes.
    async def cenario():
        mexc = MexcDeVerdade()
        coletor, _ = coletor_mexc_spot(mexc)
        tarefa = asyncio.create_task(coletor.manter_tickers(mexc.simbolos))
        await esperar_ate(lambda: len(mexc.assinados("bookTicker")) == 20)
        antes = set(mexc.assinados("bookTicker"))
        await coletor.assinar_livros({"T39/USDT", "T38/USDT"})
        await esperar_ate(lambda: len(mexc.assinados("depth")) == 2)
        await parar(tarefa)  # o __main__ para o stream antes de reiniciar (_Tickers.parar)
        # Volumes novos: T00..T04 passam muito na frente (bem mais que a margem de 10%).
        for i in range(5):
            mexc.volumes[f"T{i:02d}/USDT"] = 1_000_000.0
        coletor._volumes_conhecidos.clear()
        tarefa = asyncio.create_task(coletor.manter_tickers(mexc.simbolos))
        await esperar_ate(lambda: {f"T{i:02d}USDT" for i in range(5)} <= mexc.assinados("bookTicker"))
        await _ceder()
        depois = set(mexc.assinados("bookTicker"))
        await parar(tarefa)
        await coletor.fechar()
        return mexc, antes, depois

    mexc, antes, depois = asyncio.run(cenario())
    assert len(depois) == 20 and len(antes - depois) == 5
    assert mexc.maximo <= 30
    metodos = [m.get("method") for m in mexc.enviadas if any("bookTicker" in p for p in m.get("params", []))]
    primeira_nova = metodos.index("SUBSCRIPTION", 20)  # depois das 20 primeiras assinaturas
    assert metodos[20:primeira_nova] == ["UNSUBSCRIPTION"] * 5  # os 5 que sairam, antes dos novos


def test_top_dos_tickers_nao_troca_por_diferenca_pequena_de_volume():
    async def cenario():
        mexc = MexcDeVerdade()
        coletor, _ = coletor_mexc_spot(mexc)
        tarefa = asyncio.create_task(coletor.manter_tickers(mexc.simbolos))
        await esperar_ate(lambda: len(mexc.assinados("bookTicker")) == 20)
        antes = set(mexc.assinados("bookTicker"))
        await parar(tarefa)
        mexc.volumes["T19/USDT"] = 1021.0  # passa o ultimo do top (T20 = 1020) por menos de 10%
        coletor._volumes_conhecidos.clear()
        tarefa = asyncio.create_task(coletor.manter_tickers(mexc.simbolos))
        await _ceder(50)
        depois = set(mexc.assinados("bookTicker"))
        await parar(tarefa)
        await coletor.fechar()
        return antes, depois

    antes, depois = asyncio.run(cenario())
    assert antes == depois


def test_livro_pedido_que_nao_manda_nada_avisa_uma_vez(caplog):
    mexc = CorretoraFalsa([MEXC_BTC_FUT])
    coletor, estado = coletor_de_livros(mexc, prazo_primeiro_livro_s=0.02)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        await asyncio.sleep(0.1)
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob())
        await esperar_ate(lambda: estado.obter_livro("mexc", "futuro", "BTC", AGORA) is not None)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    linhas = [m for m in avisos(caplog) if "nao mandou nada" in m]
    assert len(linhas) == 1 and "BTC/USDT:USDT" in linhas[0] and "limites_assinatura" in linhas[0]


def test_campo_do_prazo_do_primeiro_livro(tmp_path):
    assert Config().prazo_primeiro_livro_s == 60
    assert carregar_config(escrever(tmp_path, "prazo_primeiro_livro_s: 90\n")).prazo_primeiro_livro_s == 90
    with pytest.raises(ErroConfig, match="prazo_primeiro_livro_s"):
        carregar_config(escrever(tmp_path, "prazo_primeiro_livro_s: 0\n"))


@pytest.mark.parametrize("sorteio, prazo", [(0.0, 720), (0.5, 900), (1.0, 1080)])
def test_prazo_da_reassinatura_varia_ate_20_por_cento(sorteio, prazo):
    # m1: livros abertos juntos nao sao reassinados todos juntos.
    mexc = CorretoraFalsa([MEXC_BTC_FUT])
    r = Relogio()
    coletor, _ = coletor_de_livros(mexc, relogio=r, reassinar_livros_s=900, sortear=lambda: sorteio)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob())
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 2)
        r.agora += prazo - 1
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob())
        await esperar_ate(lambda: len(mexc.livro_guardado_ao_pedir) == 3)
        assert mexc.desassinados == []
        r.agora += 1
        mexc.entregar(("livro", "BTC/USDT:USDT"), ob())
        await esperar_ate(lambda: mexc.desassinados == ["BTC/USDT:USDT"])
        await coletor.fechar()

    asyncio.run(cenario())


def test_saude_atrasada_se_um_mercado_em_uso_esta_mudo():
    # m2: com o spot mudo e o futuro vivo, a corretora nao pode aparecer "conectada".
    e = EstadoMercados()
    e.usar_mercados("mexc", {"spot", "futuro"})
    e.registrar_mensagem("mexc", 100.0, mercado="futuro")
    assert e.saude("mexc", 100.5)["estado"] == "atrasada"
    e.registrar_mensagem("mexc", 100.0, mercado="spot")
    assert e.saude("mexc", 100.5)["estado"] == "conectada"
    e.registrar_mensagem("gate", 100.0, mercado="spot")  # sem mercados em uso informados: como antes
    assert e.saude("gate", 100.5)["estado"] == "conectada"


def test_coletor_informa_os_mercados_em_uso():
    gate = CorretoraFalsa([market_ccxt("BTC/USDT", "BTC"), MEXC_BTC_FUT])
    r = Relogio()
    coletor, estado = coletor_de_livros(gate, nome="gate", relogio=r)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT", "BTC/USDT:USDT"]))
        gate.entregar(("watch_tickers", "spot"), {"BTC/USDT": tk()})
        gate.entregar(("watch_tickers", "futuro"), {"BTC/USDT:USDT": tk()})
        await esperar_ate(lambda: estado.conexao_viva("gate", r.agora, mercado="futuro"))
        assert estado.saude("gate", r.agora)["estado"] == "conectada"
        r.agora += 2.5  # so o spot continua mandando: o futuro esta mudo
        gate.entregar(("watch_tickers", "spot"), {"BTC/USDT": tk()})
        await esperar_ate(lambda: estado.conexao_viva("gate", r.agora, mercado="spot"))
        assert estado.saude("gate", r.agora)["estado"] == "atrasada"
        await parar(tarefa)

    asyncio.run(cenario())


class MexcSpotQueDemoraNoVolume(MexcSpotFalsa):
    def __init__(self, markets, volumes):
        super().__init__(markets, volumes)
        self.liberar = None

    async def fetch_tickers(self, symbols=None, params=None):
        if self.liberar is not None:
            await self.liberar.wait()
        return await super().fetch_tickers(symbols, params)


def test_livros_nao_passam_do_limite_enquanto_os_tickers_buscam_volume():
    # m3: no meio do pedido REST de volumes, o limite dos livros continua contando os tickers assinados.
    mexc = MexcSpotQueDemoraNoVolume(SEIS_SPOT, VOLUMES)
    coletor, _ = coletor_de_livros(mexc, limites_assinatura_mercado={"spot": 5}, livros_reservados_limite_mercado=2)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(sorted(SEIS_SPOT)))
        await esperar_ate(lambda: mexc.pedidos_de("watch_bids_asks"))
        await parar(tarefa)
        mexc.liberar = asyncio.Event()
        coletor._volumes_conhecidos.clear()
        tarefa = asyncio.create_task(coletor.manter_tickers(sorted(SEIS_SPOT)))
        await _ceder()  # parado no pedido REST de volumes
        await coletor.assinar_livros({"T5/USDT", "T4/USDT", "T3/USDT"})
        assert len(coletor._tarefas_livro) == 2  # 5 - 3 tickers, e nao 5
        mexc.liberar.set()
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


def test_bitget_depois_da_queda_o_snapshot_nao_traz_o_nivel_fantasma():
    # m6: o ccxt da Bitget aplica o snapshot por cima do livro guardado; esquecido depois da queda, nao ha fantasma.
    import ccxt.pro as ccxtpro

    class Cliente:
        def __init__(self):
            self.subscriptions, self.resolvidos = {}, []

        def resolve(self, valor, hash_):
            self.resolvidos.append(hash_)

        def reject(self, *a, **k):
            pass

    async def cenario():
        ex = ccxtpro.bitget()
        ex.set_markets([ex.safe_market_structure({
            "id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "base": "BTC", "quote": "USDT", "settle": "USDT",
            "baseId": "BTC", "quoteId": "USDT", "settleId": "USDT", "type": "swap", "spot": False, "swap": True,
            "future": False, "option": False, "contract": True, "linear": True, "inverse": False, "contractSize": 1,
            "active": True, "precision": {"amount": 0.0001, "price": 0.1}, "limits": {},
            "info": {"productType": "USDT-FUTURES"}})])
        snapshot = {"action": "snapshot", "arg": {"instType": "USDT-FUTURES", "channel": "books", "instId": "BTCUSDT"},
                    "data": [{"asks": [["100050", "3"]], "bids": [["99995", "4"]], "checksum": 0, "ts": "1"}],
                    "ts": 1}
        pedidos = 0

        async def watch_order_book(symbol, limit=None, params={}):
            nonlocal pedidos
            pedidos += 1
            if pedidos == 1:  # antes da queda: o livro com um nivel que sumiu durante a queda
                velho = ex.counted_order_book({})
                velho["asks"].storeArray([100000.0, 5.0, ["100000", "5"]])
                velho["bids"].storeArray([99990.0, 1.0, ["99990", "1"]])
                ex.orderbooks[symbol] = velho
                return velho.limit()
            if pedidos == 2:
                raise NetworkError("conexao caiu")
            if pedidos > 3:
                await asyncio.Event().wait()
            ex.handle_order_book(Cliente(), snapshot)  # o handler de verdade, com o snapshot da conexao nova
            return ex.orderbooks[symbol].limit()

        ex.watch_order_book = watch_order_book
        estado = EstadoMercados()
        coletor = ColetorCcxt("bitget", ex, estado=estado, relogio=relogio, esperar=EsperaFalsa())
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        await esperar_ate(lambda: pedidos == 4)
        livro = estado.obter_livro("bitget", "futuro", "BTC", AGORA)
        await coletor.fechar()
        return livro

    livro = asyncio.run(cenario())
    assert livro.asks == [(100050.0, 3.0)] and livro.bids == [(99995.0, 4.0)]


def test_metodo_que_nao_existe_no_mercado_nao_deixa_assinatura_anotada(caplog):
    # O watch_tickers da MEXC spot da NotSupported antes de assinar qualquer coisa: o metodo seguinte nao pode
    # tentar cancelar assinaturas dele.
    mexc = MexcSpotFalsa({s: SEIS_SPOT[s] for s in ("T0/USDT", "T1/USDT")}, VOLUMES)
    mexc.has["watchTickers"] = True
    coletor, _ = coletor_de_livros(mexc, limites_assinatura_mercado={"spot": 30})

    async def cenario():
        mexc.entregar(("watch_tickers", "spot"), NotSupported("mexc watchTickers does not support spot markets"))
        tarefa = asyncio.create_task(coletor.manter_tickers(["T0/USDT", "T1/USDT"]))
        await esperar_ate(lambda: mexc.pedidos_de("watch_bids_asks"))
        await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert not [m for m in avisos(caplog) if "un_watch" in m]
    assert coletor._tickers_assinados == {"spot": ("watch_bids_asks", {"T0/USDT", "T1/USDT"})}
