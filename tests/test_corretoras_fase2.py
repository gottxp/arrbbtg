"""Testes da Task 15: fase 2 de corretoras (BingX, XT, Hyperliquid).

Cobre o que a fase 2 trouxe de novo:
  - cotacoes (USDC, USD...) lidas do ticker spot da fonte (`DadosLentos.cotacoes`);
  - mercados por corretora (Hyperliquid so futuros);
  - metodo de ticker por corretora e a troca automatica quando o lote nao traz bid/ask;
  - intervalo de funding por corretora (config > ccxt > 8h);
  - um sinal futuro x futuro Hyperliquid (USDC) x MEXC (USDT) de ponta a ponta.

Nenhum teste acessa a internet nem dorme de verdade: as corretoras sao objetos falsos com filas de
mensagens e as esperas de reconexao sao trocadas por uma EsperaFalsa.
"""

import asyncio
import logging

import pytest
from ccxt.base.errors import NotSupported

import scanner.__main__ as principal
from scanner.armazenamento import Banco
from scanner.coletores.ccxt_coletor import ColetorCcxt
from scanner.coletores.dados_lentos import DadosLentos
from scanner.estado import EstadoMercados
from tests.fabricas import (
    HL_BTC,
    HL_KPEPE,
    HL_SPOT_BTC,
    HL_SPOT_USDT,
    CorretoraFalsa,
    EsperaFalsa,
    cfg_com,
    esperar_ate,
    market_ccxt,
    ob,
    tk,
)


AGORA = 1000.0


def relogio():
    return AGORA


def avisos(caplog, nivel=logging.WARNING) -> list[str]:
    """Mensagens do scanner com nivel >= `nivel`."""
    return [r.getMessage() for r in caplog.records if r.name.startswith("scanner") and r.levelno >= nivel]


def so_preco(preco=100.0):
    """Ticker sem bid/ask, como o allMids da Hyperliquid ou o 'tickers' da XT: so o ultimo preco."""
    return {"bid": None, "ask": None, "last": preco, "close": preco, "quoteVolume": None, "timestamp": None}


async def parar(tarefa: asyncio.Task) -> None:
    tarefa.cancel()
    await asyncio.gather(tarefa, return_exceptions=True)


# Markets no formato do ccxt (veja tests/test_normalizador.py para as outras corretoras).
BITGET_USDC_USDT = market_ccxt("USDC/USDT", "USDC", taker=0.001)                 # spot USDC cotado em USDT
KRAKEN_USDT_USD = market_ccxt("USDT/USD", "USDT", quote="USD", taker=0.002)      # par inverso: 1 USDT em USD
KRAKEN_USDT_EUR = market_ccxt("USDT/EUR", "USDT", quote="EUR", taker=0.002)
MEXC_BTC = market_ccxt("BTC/USDT:USDT", "BTC", taker=0.0002)


# ---------------------------------------------------------------------------
# DadosLentos.cotacoes(): preco em USDT das outras moedas de cotacao
# ---------------------------------------------------------------------------


def dados_com_cotacao(estado, **kwargs) -> DadosLentos:
    kwargs.setdefault("fonte_cotacao", "bitget")
    return DadosLentos(estado=estado, relogio=relogio, **kwargs)


def por_na_fonte(estado, corretora, simbolo, bid, ask, quando=AGORA):
    """O ticker do par de cotacao chegou na corretora (o coletor faz isto para os simbolos de cotacao)."""
    estado.registrar_mensagem(corretora, quando)
    estado.atualizar_cotacao(corretora, simbolo, bid, ask, quando)


def test_cotacao_e_o_meio_do_bid_ask_do_ticker_fresco():
    estado = EstadoMercados()
    d = dados_com_cotacao(estado)
    assert d.escolher_pares_de_cotacao({"USDC"}, {"bitget": {"USDC/USDT": BITGET_USDC_USDT}}) == \
        {"bitget": ["USDC/USDT"]}
    por_na_fonte(estado, "bitget", "USDC/USDT", 0.9997, 0.9999)
    assert d.cotacoes() == {"USDC": pytest.approx(0.9998)}


def test_ticker_velho_ou_conexao_silenciosa_fica_sem_cotacao():
    estado = EstadoMercados(max_silencio_s=2, max_idade_livro_s=30)
    d = dados_com_cotacao(estado)
    d.escolher_pares_de_cotacao({"USDC"}, {"bitget": {"USDC/USDT": BITGET_USDC_USDT}})
    por_na_fonte(estado, "bitget", "USDC/USDT", 0.9997, 0.9999, quando=AGORA - 31)  # parado ha 31s
    estado.registrar_mensagem("bitget", AGORA)                                     # conexao viva
    assert d.cotacoes() == {}
    por_na_fonte(estado, "bitget", "USDC/USDT", 0.9997, 0.9999, quando=AGORA - 5)   # fresco (5s)...
    estado.registrar_mensagem("bitget", AGORA)
    assert d.cotacoes() == {"USDC": pytest.approx(0.9998)}
    estado.registrar_mensagem("bitget", AGORA - 5)                                   # ...mas a conexao calou
    assert d.cotacoes() == {}


def test_sem_par_escolhido_nao_ha_cotacao():
    assert dados_com_cotacao(EstadoMercados()).cotacoes() == {}


@pytest.mark.parametrize("bid, ask, dentro", [
    (0.9999, 1.0001, True),
    (1.0999, 1.1001, True),    # 10% acima: ainda dentro
    (1.1100, 1.1200, False),   # 11,5% acima
    (0.8000, 0.8002, False),   # depeg de 20%
    (0.0300, 0.0301, False),   # moeda que nao e dolar (ex.: lira turca)
])
def test_faixa_de_seguranca_em_volta_de_1(bid, ask, dentro, caplog):
    estado = EstadoMercados()
    d = dados_com_cotacao(estado, faixa_cotacao_pct=10)
    d.escolher_pares_de_cotacao({"USDC"}, {"bitget": {"USDC/USDT": BITGET_USDC_USDT}})
    por_na_fonte(estado, "bitget", "USDC/USDT", bid, ask)
    with caplog.at_level(logging.WARNING, logger="scanner"):
        primeira, segunda = d.cotacoes(), d.cotacoes()
    assert primeira == segunda == ({"USDC": pytest.approx((bid + ask) / 2)} if dentro else {})
    fora = [m for m in avisos(caplog) if "USDC" in m and "faixa" in m]
    assert len(fora) == (0 if dentro else 1)  # um aviso so, nao um por ciclo


def test_cotacao_que_volta_para_a_faixa_vale_de_novo_e_avisa_de_novo_se_sair(caplog):
    estado = EstadoMercados()
    d = dados_com_cotacao(estado)
    d.escolher_pares_de_cotacao({"USDC"}, {"bitget": {"USDC/USDT": BITGET_USDC_USDT}})
    with caplog.at_level(logging.INFO, logger="scanner"):
        por_na_fonte(estado, "bitget", "USDC/USDT", 0.80, 0.80)
        assert d.cotacoes() == {}
        por_na_fonte(estado, "bitget", "USDC/USDT", 0.999, 0.999)
        assert d.cotacoes() == {"USDC": pytest.approx(0.999)}
        por_na_fonte(estado, "bitget", "USDC/USDT", 0.80, 0.80)
        assert d.cotacoes() == {}
    assert len([m for m in avisos(caplog) if "faixa" in m]) == 2


def test_fonte_sem_o_par_fica_sem_cotacao_e_avisa(caplog):
    d = dados_com_cotacao(EstadoMercados())
    with caplog.at_level(logging.WARNING, logger="scanner"):
        extras = d.escolher_pares_de_cotacao({"USDG"}, {"bitget": {"USDC/USDT": BITGET_USDC_USDT}})
    assert extras == {} and d.cotacoes() == {}
    assert any("USDG" in m and "bitget" in m for m in avisos(caplog))


def test_moeda_sem_fonte_ligada_fica_sem_cotacao_e_avisa(caplog):
    # A fonte (bitget) nao esta entre as corretoras que carregaram os mercados (desligada ou fora do ar).
    d = dados_com_cotacao(EstadoMercados())
    with caplog.at_level(logging.WARNING, logger="scanner"):
        extras = d.escolher_pares_de_cotacao({"USDC"}, {"gate": {"USDC/USDT": BITGET_USDC_USDT}})
    assert extras == {} and d.cotacoes() == {}
    assert any("USDC" in m and "bitget" in m for m in avisos(caplog))


def test_escolher_de_novo_com_o_mesmo_resultado_nao_repete_o_aviso(caplog):
    d = dados_com_cotacao(EstadoMercados())
    with caplog.at_level(logging.WARNING, logger="scanner"):
        for _ in range(3):  # a recarga de mercados chama de novo a cada hora
            d.escolher_pares_de_cotacao({"USDG"}, {"bitget": {}})
    assert len([m for m in avisos(caplog) if "USDG" in m]) == 1


def test_par_inverso_vale_1_sobre_o_preco():
    # Kraken (Task 16): USDT/USD = quantos USD vale 1 USDT; 1 USD vale 1/preco USDT.
    estado = EstadoMercados()
    d = dados_com_cotacao(estado, fonte_cotacao="kraken")
    assert d.escolher_pares_de_cotacao({"USD"}, {"kraken": {"USDT/USD": KRAKEN_USDT_USD}}) == \
        {"kraken": ["USDT/USD"]}
    por_na_fonte(estado, "kraken", "USDT/USD", 1.0004, 1.0006)
    assert d.cotacoes() == {"USD": pytest.approx(1 / 1.0005)}


def test_par_direto_tem_preferencia_sobre_o_inverso():
    d = dados_com_cotacao(EstadoMercados())
    usdt_usdc = market_ccxt("USDT/USDC", "USDT", quote="USDC")
    markets = {"USDT/USDC": usdt_usdc, "USDC/USDT": BITGET_USDC_USDT}
    assert d.escolher_pares_de_cotacao({"USDC"}, {"bitget": markets}) == {"bitget": ["USDC/USDT"]}
    assert d.escolher_pares_de_cotacao({"USDC"}, {"bitget": {"USDT/USDC": usdt_usdc}}) == {"bitget": ["USDT/USDC"]}


def test_par_inativo_ou_que_nao_e_spot_nao_serve_de_cotacao():
    d = dados_com_cotacao(EstadoMercados())
    inativo = {**BITGET_USDC_USDT, "active": False}
    futuro = market_ccxt("USDC/USDT:USDT", "USDC")
    assert d.escolher_pares_de_cotacao({"USDC"}, {"bitget": {"USDC/USDT": inativo}}) == {}
    assert d.escolher_pares_de_cotacao({"USDC"}, {"bitget": {"USDC/USDT": futuro}}) == {}


def test_fontes_cotacao_escolhe_a_corretora_de_cada_moeda():
    estado = EstadoMercados()
    d = dados_com_cotacao(estado, fontes_cotacao={"USD": "kraken"})
    markets = {"bitget": {"USDC/USDT": BITGET_USDC_USDT},
               "kraken": {"USDT/USD": KRAKEN_USDT_USD, "USDC/USDT": BITGET_USDC_USDT}}
    assert d.escolher_pares_de_cotacao({"USDC", "USD"}, markets) == {"bitget": ["USDC/USDT"], "kraken": ["USDT/USD"]}
    por_na_fonte(estado, "bitget", "USDC/USDT", 0.9997, 0.9999)
    por_na_fonte(estado, "kraken", "USDT/USD", 0.9999, 1.0001)
    por_na_fonte(estado, "kraken", "USDC/USDT", 0.95, 0.95)  # a Kraken tambem tem, mas o USDC vem da Bitget
    assert d.cotacoes() == {"USDC": pytest.approx(0.9998), "USD": pytest.approx(1.0)}


def test_moedas_que_nao_sao_mais_usadas_saem_da_cotacao():
    estado = EstadoMercados()
    d = dados_com_cotacao(estado)
    d.escolher_pares_de_cotacao({"USDC"}, {"bitget": {"USDC/USDT": BITGET_USDC_USDT}})
    por_na_fonte(estado, "bitget", "USDC/USDT", 0.9997, 0.9999)
    assert d.escolher_pares_de_cotacao(set(), {"bitget": {"USDC/USDT": BITGET_USDC_USDT}}) == {}
    assert d.cotacoes() == {}


# ---------------------------------------------------------------------------
# Coletor: o ticker do par de cotacao vai para o lugar certo do estado
# ---------------------------------------------------------------------------


async def tratar_mensagens(coletor, corretora_falsa, simbolos, *mensagens, chave=("watch_tickers", "spot")):
    """Roda os tickers do coletor ate tratar todas as `mensagens` da fila `chave`."""
    corretora_falsa.entregar(chave, *mensagens)
    tarefa = asyncio.create_task(coletor.manter_tickers(simbolos))
    await esperar_ate(lambda: corretora_falsa.fila(chave).empty())
    await asyncio.sleep(0)
    await parar(tarefa)


def test_par_de_cotacao_le_o_simbolo_certo_mesmo_sem_ocupar_a_chave_do_estado(caplog):
    # Kraken: USDT/USD, USDT/EUR e USDT/USDC sao o mesmo "ativo" USDT no spot; a chave do estado fica com um
    # so (USDC vem antes de USD na preferencia). A cotacao do USD le o USDT/USD pelo simbolo.
    estado = EstadoMercados()
    kraken = CorretoraFalsa([KRAKEN_USDT_USD, KRAKEN_USDT_EUR, market_ccxt("USDT/USDC", "USDT", quote="USDC")])
    coletor = ColetorCcxt("kraken", kraken, estado=estado, relogio=relogio)
    assert coletor.simbolos_por_chave() == {("spot", "USDT"): "USDT/USDC"}
    coletor.usar_para_cotacao({"USDT/USD"})
    d = dados_com_cotacao(estado, fonte_cotacao="kraken")
    d.escolher_pares_de_cotacao({"USD"}, {"kraken": kraken.markets})

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(tratar_mensagens(coletor, kraken, ["USDT/USD"], {"USDT/USD": tk(1.0004, 1.0006)}))
    assert d.cotacoes() == {"USD": pytest.approx(1 / 1.0005)}
    # O par so de cotacao nao entra nos mercados do estado (nao forma combinacao) e nao gera aviso.
    assert estado.mercados() == {}
    assert not any("USDT/USD" in m for m in avisos(caplog))


def test_par_de_cotacao_que_tambem_e_o_da_chave_vai_aos_dois_lugares():
    estado = EstadoMercados()
    bitget = CorretoraFalsa([BITGET_USDC_USDT])
    coletor = ColetorCcxt("bitget", bitget, estado=estado, relogio=relogio)
    coletor.usar_para_cotacao({"USDC/USDT"})
    asyncio.run(tratar_mensagens(coletor, bitget, ["USDC/USDT"], {"USDC/USDT": tk(0.9997, 0.9999)}))
    assert estado.obter_cotacao("bitget", "USDC/USDT", AGORA)["bid"] == 0.9997
    assert estado.obter_ticker("bitget", "spot", "USDC", AGORA)["bid"] == 0.9997


def test_queda_da_fonte_apaga_a_cotacao():
    # Como o livro: o ticker de antes da queda nao pode voltar a valer quando a conexao volta.
    estado = EstadoMercados()
    por_na_fonte(estado, "bitget", "USDC/USDT", 0.9997, 0.9999)
    estado.marcar_desconectada("bitget")
    estado.registrar_mensagem("bitget", AGORA)  # voltou (outra mensagem qualquer chegou)
    assert estado.obter_cotacao("bitget", "USDC/USDT", AGORA) is None


# ---------------------------------------------------------------------------
# __main__: o simbolo da cotacao sempre nos tickers da fonte (inclusive depois da recarga)
# ---------------------------------------------------------------------------


def montar(exchanges, config, estado):
    coletores = {nome: ColetorCcxt(nome, ex, estado=estado, relogio=relogio,
                                   mercados=config.mercados_da_corretora(nome),
                                   metodo_ticker=config.metodo_ticker.get(nome))
                 for nome, ex in exchanges.items()}
    dados = DadosLentos(estado=estado, fonte_cotacao=config.fonte_cotacao, relogio=relogio)
    return coletores, dados


CFG_HL = cfg_com(corretoras=["hyperliquid", "mexc", "bitget"], mercados_por_corretora={"hyperliquid": ("futuro",)},
                 metodo_ticker={"hyperliquid": "watch_ticker"})


def test_simbolo_da_cotacao_entra_nos_tickers_da_fonte_mesmo_sem_combinacao():
    exchanges = {"hyperliquid": CorretoraFalsa([HL_BTC]), "mexc": CorretoraFalsa([MEXC_BTC]),
                 "bitget": CorretoraFalsa([BITGET_USDC_USDT])}
    estado = EstadoMercados()
    coletores, dados = montar(exchanges, CFG_HL, estado)
    listas, _ = principal._listas_de_tickers(coletores, dados)
    # A Bitget nao forma combinacao nenhuma aqui: entra so pelo USDC/USDT (a Hyperliquid cota em USDC).
    assert listas == {"hyperliquid": ["BTC/USDC:USDC"], "mexc": ["BTC/USDT:USDT"], "bitget": ["USDC/USDT"]}


def test_sem_moeda_fora_de_usdt_nao_assina_cotacao():
    exchanges = {"mexc": CorretoraFalsa([MEXC_BTC]), "gate": CorretoraFalsa([market_ccxt("BTC/USDT", "BTC")]),
                 "bitget": CorretoraFalsa([BITGET_USDC_USDT])}
    coletores, dados = montar(exchanges, cfg_com(corretoras=list(exchanges)), EstadoMercados())
    listas, _ = principal._listas_de_tickers(coletores, dados)
    assert "bitget" not in listas


def test_simbolo_da_cotacao_continua_nos_tickers_depois_da_recarga_e_sai_quando_nao_serve_mais():
    hyperliquid, mexc = CorretoraFalsa([HL_BTC]), CorretoraFalsa([MEXC_BTC])
    bitget = CorretoraFalsa([BITGET_USDC_USDT, market_ccxt("ETH/USDT", "ETH")])
    exchanges = {"hyperliquid": hyperliquid, "mexc": mexc, "bitget": bitget}
    estado = EstadoMercados()
    coletores, dados = montar(exchanges, CFG_HL, estado)
    tickers = principal._Tickers(coletores, dados)

    async def cenario():
        await principal._ajustar_tickers(coletores, tickers, estado)
        assert tickers.simbolos["bitget"] == ["USDC/USDT"]
        bitget.entregar(("watch_tickers", "spot"), {"USDC/USDT": tk(0.9997, 0.9999)})
        await esperar_ate(lambda: dados.cotacoes() != {})

        # Recarga sem mudanca: o stream da Bitget nao reinicia e a cotacao segue valendo.
        antes = tickers.tarefas["bitget"]
        await principal._recarregar_mercados(exchanges, coletores, tickers, estado)
        assert tickers.simbolos["bitget"] == ["USDC/USDT"] and tickers.tarefas["bitget"] is antes
        assert dados.cotacoes() == {"USDC": pytest.approx(0.9998)}

        # A Hyperliquid tirou o BTC: ninguem mais cota em USDC e a Bitget sai da etapa 1.
        hyperliquid.proximos_markets = {}
        await principal._recarregar_mercados(exchanges, coletores, tickers, estado)
        assert "bitget" not in tickers.simbolos
        assert dados.cotacoes() == {}
        for tarefa in tickers.tarefas.values():
            await parar(tarefa)

    asyncio.run(cenario())


# ---------------------------------------------------------------------------
# mercados_por_corretora: a Hyperliquid fica so com os futuros
# ---------------------------------------------------------------------------


def test_mercado_fora_da_lista_nao_entra_no_estado_nem_nos_tickers_nem_nos_livros(caplog):
    estado = EstadoMercados()
    hl = CorretoraFalsa([HL_BTC, HL_KPEPE, HL_SPOT_BTC, HL_SPOT_USDT], has={"watchTicker": True})
    coletor = ColetorCcxt("hyperliquid", hl, estado=estado, relogio=relogio, mercados=("futuro",))
    assert set(coletor.simbolos_por_chave().values()) == {"BTC/USDC:USDC", "KPEPE/USDC:USDC"}

    async def cenario():
        # Mesmo que um ticker ou livro do spot chegasse, ele nao vai ao estado.
        hl.entregar(("watch_ticker", "BTC/USDC"), tk(99.0, 99.1))
        hl.entregar(("watch_ticker", "BTC/USDC:USDC"), tk(100.0, 100.1))
        hl.entregar(("livro", "BTC/USDC"), ob(99.0, 99.1))
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDC", "BTC/USDC:USDC"]))
        await coletor.assinar_livros({"BTC/USDC"})
        await esperar_ate(lambda: hl.fila(("livro", "BTC/USDC")).empty()
                          and hl.fila(("watch_ticker", "BTC/USDC")).empty()
                          and estado.obter_ticker("hyperliquid", "futuro", "BTC", AGORA) is not None)
        await asyncio.sleep(0)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert estado.mercados() == {"hyperliquid": {("futuro", "BTC")}}
    assert estado.obter_livro("hyperliquid", "spot", "BTC", AGORA) is None
    assert any("BTC/USDC" in m and "mercados_por_corretora" in m for m in avisos(caplog))


def test_rodar_cria_os_coletores_com_mercados_e_metodo_da_config(tmp_path, monkeypatch):
    recebidos = {}
    init_original = ColetorCcxt.__init__

    def init_anotando(self, nome, *args, **kwargs):
        recebidos[nome] = (kwargs.get("mercados"), kwargs.get("metodo_ticker"), kwargs.get("mensagens_sem_bid_ask"))
        init_original(self, nome, *args, **kwargs)

    monkeypatch.setattr(ColetorCcxt, "__init__", init_anotando)
    exchanges = {"hyperliquid": CorretoraFalsa([HL_BTC]), "mexc": CorretoraFalsa([MEXC_BTC])}
    config = cfg_com(corretoras=["hyperliquid", "mexc"], banco=str(tmp_path / "s.db"), ciclo_ms=50,
                     mercados_por_corretora={"hyperliquid": ("futuro",)}, metodo_ticker={"hyperliquid": "watch_ticker"},
                     mensagens_sem_bid_ask=4)
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=1), timeout=5))
    assert recebidos == {"hyperliquid": (("futuro",), "watch_ticker", 4), "mexc": (("spot", "futuro"), None, 4)}


# ---------------------------------------------------------------------------
# metodo_ticker e a troca automatica quando o lote nao traz bid/ask
# ---------------------------------------------------------------------------


def test_metodo_ticker_da_config_vem_primeiro():
    # A Hyperliquid tem watch_tickers (allMids, sem bid/ask); com watch_ticker na config ela nem tenta o lote.
    estado = EstadoMercados()
    hl = CorretoraFalsa([HL_BTC], has={"watchTickers": True, "watchTicker": True})
    coletor = ColetorCcxt("hyperliquid", hl, estado=estado, relogio=relogio, mercados=("futuro",),
                          metodo_ticker="watch_ticker")
    asyncio.run(tratar_mensagens(coletor, hl, ["BTC/USDC:USDC"], tk(100.0, 100.1),
                                 chave=("watch_ticker", "BTC/USDC:USDC")))
    assert hl.pedidos_de("watch_tickers") == []
    assert estado.obter_ticker("hyperliquid", "futuro", "BTC", AGORA)["bid"] == 100.0


def test_metodo_da_config_que_a_corretora_nao_tem_passa_ao_seguinte(caplog):
    estado = EstadoMercados()
    gate = CorretoraFalsa([market_ccxt("BTC/USDT", "BTC")], has={"watchTickers": True, "watchTicker": True})
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, metodo_ticker="watch_bids_asks")
    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(tratar_mensagens(coletor, gate, ["BTC/USDT"], tk(100.0, 100.1), chave=("watch_ticker", "BTC/USDT")))
    assert gate.pedidos_de("watch_tickers") == []   # o configurado era o do meio: o lote completo nem entra
    assert estado.obter_ticker("gate", "spot", "BTC", AGORA) is not None
    assert any("watch_bids_asks" in m for m in avisos(caplog))


def test_lote_sem_bid_ask_troca_para_o_proximo_metodo_com_aviso(caplog):
    estado = EstadoMercados()
    hl = CorretoraFalsa([HL_BTC], has={"watchTickers": True, "watchTicker": True})
    coletor = ColetorCcxt("hyperliquid", hl, estado=estado, relogio=relogio, mercados=("futuro",),
                          mensagens_sem_bid_ask=3)
    hl.entregar(("watch_tickers", "futuro"), *[{"BTC/USDC:USDC": so_preco()} for _ in range(3)])
    hl.entregar(("watch_ticker", "BTC/USDC:USDC"), tk(100.0, 100.1))

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDC:USDC"]))
        await esperar_ate(lambda: estado.obter_ticker("hyperliquid", "futuro", "BTC", AGORA) is not None)
        await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert len(hl.pedidos_de("watch_tickers")) == 3
    assert any("watch_tickers" in m and "bid/ask" in m and "watch_ticker" in m for m in avisos(caplog))


def test_lote_que_ja_trouxe_bid_ask_nao_e_trocado_por_mensagens_ruins_depois():
    # Ex.: pares iliquidos sem bid num momento. O metodo ja provou que funciona: segue com ele.
    estado = EstadoMercados()
    gate = CorretoraFalsa([market_ccxt("BTC/USDT", "BTC"), market_ccxt("ETH/USDT", "ETH")],
                          has={"watchTickers": True, "watchTicker": True})
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, mensagens_sem_bid_ask=2)
    mensagens = [{"BTC/USDT": tk(100.0, 100.1)}] + [{"ETH/USDT": so_preco()} for _ in range(5)]
    asyncio.run(tratar_mensagens(coletor, gate, ["BTC/USDT", "ETH/USDT"], *mensagens))
    # 6 mensagens tratadas + o pedido que ficou esperando a proxima: seguiu no lote.
    assert len(gate.pedidos_de("watch_tickers")) == 7 and gate.pedidos_de("watch_ticker") == []


def test_mensagem_com_algum_ticker_bom_zera_a_contagem():
    estado = EstadoMercados()
    gate = CorretoraFalsa([market_ccxt("BTC/USDT", "BTC"), market_ccxt("ETH/USDT", "ETH")],
                          has={"watchTickers": True, "watchTicker": True})
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, mensagens_sem_bid_ask=2)
    misturada = {"BTC/USDT": tk(100.0, 100.1), "ETH/USDT": so_preco()}
    asyncio.run(tratar_mensagens(coletor, gate, ["BTC/USDT", "ETH/USDT"], {"ETH/USDT": so_preco()}, misturada))
    assert gate.pedidos_de("watch_ticker") == []


def test_nenhum_metodo_com_bid_ask_avisa_com_erro_e_o_outro_mercado_segue(caplog):
    # Como o spot da XT no ccxt 4.5.89: nem o 'tickers' nem o 'ticker' trazem bid/ask.
    estado = EstadoMercados()
    xt = CorretoraFalsa([market_ccxt("BTC/USDT", "BTC"), market_ccxt("BTC/USDT:USDT", "BTC")],
                        has={"watchTickers": True, "watchTicker": True})
    coletor = ColetorCcxt("xt", xt, estado=estado, relogio=relogio, mensagens_sem_bid_ask=2, esperar=EsperaFalsa())
    xt.entregar(("watch_tickers", "spot"), {"BTC/USDT": so_preco()}, {"BTC/USDT": so_preco()})
    xt.entregar(("watch_ticker", "BTC/USDT"), so_preco(), so_preco())
    xt.entregar(("watch_tickers", "futuro"), {"BTC/USDT:USDT": tk(100.0, 100.1)})

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT", "BTC/USDT:USDT"]))
        await esperar_ate(lambda: any("nenhum metodo" in m for m in avisos(caplog, logging.ERROR)))
        xt.entregar(("watch_tickers", "futuro"), {"BTC/USDT:USDT": tk(101.0, 101.1)})
        await esperar_ate(lambda: (estado.obter_ticker("xt", "futuro", "BTC", AGORA) or {}).get("bid") == 101.0)
        await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    erros = [m for m in avisos(caplog, logging.ERROR) if "spot" in m]
    assert erros and "mercados_por_corretora" in erros[0]


def test_metodo_sem_bid_ask_nao_e_tentado_de_novo_depois_de_reconectar():
    estado = EstadoMercados()
    hl = CorretoraFalsa([HL_BTC], has={"watchTickers": True, "watchTicker": True})
    espera = EsperaFalsa()
    coletor = ColetorCcxt("hyperliquid", hl, estado=estado, relogio=relogio, mercados=("futuro",),
                          mensagens_sem_bid_ask=2, esperar=espera)
    hl.entregar(("watch_tickers", "futuro"), {"BTC/USDC:USDC": so_preco()}, {"BTC/USDC:USDC": so_preco()})
    hl.entregar(("watch_ticker", "BTC/USDC:USDC"), tk(100.0, 100.1), OSError("sem rede"), tk(100.5, 100.6))

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDC:USDC"]))
        await esperar_ate(
            lambda: (estado.obter_ticker("hyperliquid", "futuro", "BTC", AGORA) or {}).get("bid") == 100.5)
        await parar(tarefa)

    asyncio.run(cenario())
    assert espera.esperas == [1]                    # caiu uma vez e voltou
    assert len(hl.pedidos_de("watch_tickers")) == 2  # so antes da queda: depois foi direto ao watch_ticker


def test_futuro_da_xt_assina_o_agg_tickers_e_o_spot_o_canal_padrao_ao_mesmo_tempo():
    # ccxt/pro/xt.py: o canal 'tickers' (padrao) nao traz bid/ask; o 'agg_tickers' (so contratos) traz bp/ap.
    # Spot e futuro rodam juntos: cada pedido sai com o canal do seu mercado, sem params extras.
    estado = EstadoMercados()
    xt = CorretoraFalsa([market_ccxt("BTC/USDT", "BTC"), market_ccxt("BTC/USDT:USDT", "BTC")],
                        has={"watchTickers": True, "watchTicker": True})
    coletor = ColetorCcxt("xt", xt, estado=estado, relogio=relogio)
    xt.entregar(("watch_tickers", "spot"), *[{"BTC/USDT": tk(100.0, 100.1)} for _ in range(3)])
    xt.entregar(("watch_tickers", "futuro"), *[{"BTC/USDT:USDT": tk(100.2, 100.3)} for _ in range(3)])

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT", "BTC/USDT:USDT"]))
        await esperar_ate(lambda: xt.fila(("watch_tickers", "spot")).empty()
                          and xt.fila(("watch_tickers", "futuro")).empty())
        await parar(tarefa)

    asyncio.run(cenario())
    assert {(s, c) for _, s, c in xt.canais} == {(("BTC/USDT",), "tickers"), (("BTC/USDT:USDT",), "agg_tickers")}
    assert {p for _, _, p in xt.pedidos} == {None}


def test_xt_no_um_a_um_tambem_usa_o_agg_ticker_no_futuro():
    xt = CorretoraFalsa([market_ccxt("BTC/USDT:USDT", "BTC")], has={"watchTicker": True})
    coletor = ColetorCcxt("xt", xt, estado=EstadoMercados(), relogio=relogio)
    asyncio.run(tratar_mensagens(coletor, xt, ["BTC/USDT:USDT"], tk(), chave=("watch_ticker", "BTC/USDT:USDT")))
    assert {c for _, _, c in xt.canais} == {"agg_ticker"}


def test_outras_corretoras_nao_tem_o_canal_mexido():
    gate = CorretoraFalsa([market_ccxt("BTC/USDT:USDT", "BTC")])
    coletor = ColetorCcxt("gate", gate, estado=EstadoMercados(), relogio=relogio)
    asyncio.run(tratar_mensagens(coletor, gate, ["BTC/USDT:USDT"], {"BTC/USDT:USDT": tk()},
                                 chave=("watch_tickers", "futuro")))
    assert gate.options == {} and {p for _, _, p in gate.pedidos} == {None}


def test_xt_com_o_ccxt_de_verdade_assina_o_agg_tickers_com_subscribe():
    # O ccxt de verdade monta a mensagem de assinatura; so o envio (watch) e trocado, sem rede. Com o canal
    # pelo params o ccxt 4.5.89 mandaria {"method": "agg_tickers"} no lugar de "SUBSCRIBE".
    import ccxt.pro as ccxtpro
    from ccxt import xt as xt_rest

    rest = xt_rest()
    futuro = rest.parse_market({"id": 52, "symbol": "btc_usdt", "pair": "btc_usdt", "contractType": "PERPETUAL",
                                "productType": "perpetual", "underlyingType": "U_BASED", "contractSize": "0.0001",
                                "isOpenApi": True, "baseCoin": "btc", "quoteCoin": "usdt", "quantityPrecision": 0,
                                "pricePrecision": 1, "takerFee": "0.0006", "makerFee": "0.0004"})
    enviadas = []

    async def cenario():
        ex = ccxtpro.xt()
        ex.set_markets([futuro])

        async def watch_sem_rede(url, message_hash, message=None, subscribe_hash=None, subscription=None):
            enviadas.append(message)
            if len(enviadas) > 1:
                await asyncio.sleep(3600)  # a segunda espera a proxima mensagem, como o ccxt
            return {"BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "bid": 100.0, "ask": 100.1, "timestamp": None}}

        ex.watch = watch_sem_rede
        estado = EstadoMercados()
        coletor = ColetorCcxt("xt", ex, estado=estado, relogio=relogio)
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT:USDT"]))
        await esperar_ate(lambda: estado.obter_ticker("xt", "futuro", "BTC", AGORA) is not None)
        await parar(tarefa)
        await ex.close()

    asyncio.run(cenario())
    assert enviadas[0]["method"] == "SUBSCRIBE" and enviadas[0]["params"] == ["agg_tickers"]


def test_limite_de_assinaturas_vale_para_o_watch_ticker_escolhido_na_config():
    # A corretora tem lote no ccxt, mas com watch_ticker na config assina um par por vez: o limite vale para
    # a corretora toda (spot e futuro juntos), como na BingX.
    hl = CorretoraFalsa([HL_BTC, HL_SPOT_BTC], has={"watchTickers": True, "watchTicker": True})
    estado = EstadoMercados()
    estado.registrar_mensagem("hyperliquid", AGORA)
    estado.atualizar_ticker("hyperliquid", "futuro", "BTC", 1, 2, AGORA, 9e9, simbolo_original="BTC/USDC:USDC")
    estado.atualizar_ticker("hyperliquid", "spot", "BTC", 1, 2, AGORA, 1e3, simbolo_original="BTC/USDC")
    coletor = ColetorCcxt("hyperliquid", hl, estado=estado, relogio=relogio, metodo_ticker="watch_ticker",
                          limite_assinaturas=1)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDC", "BTC/USDC:USDC"]))
        await esperar_ate(lambda: len(hl.pedidos) >= 1)
        await asyncio.sleep(0.01)
        await parar(tarefa)

    asyncio.run(cenario())
    assert [s for s, _ in hl.pedidos_de("watch_ticker")] == [("BTC/USDC:USDC",)]  # 1 no total: o de maior volume


def test_troca_automatica_para_o_um_a_um_tambem_respeita_o_limite(caplog):
    markets = [HL_BTC, {**HL_BTC, "symbol": "ETH/USDC:USDC", "base": "ETH", "baseName": "ETH"}]
    hl = CorretoraFalsa(markets, has={"watchTickers": True, "watchTicker": True})
    coletor = ColetorCcxt("hyperliquid", hl, estado=EstadoMercados(), relogio=relogio, mercados=("futuro",),
                          mensagens_sem_bid_ask=1, limite_assinaturas=1)
    hl.entregar(("watch_tickers", "futuro"), {"BTC/USDC:USDC": so_preco(), "ETH/USDC:USDC": so_preco()})

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDC:USDC", "ETH/USDC:USDC"]))
        await esperar_ate(lambda: len(hl.pedidos_de("watch_ticker")) >= 1)
        await asyncio.sleep(0.01)
        await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert len(hl.pedidos_de("watch_ticker")) == 1
    assert any("limite" in m and "1 de 2" in m for m in avisos(caplog))


# ---------------------------------------------------------------------------
# Funding: intervalo por corretora (config > ccxt > 8h) e lote da Hyperliquid
# ---------------------------------------------------------------------------


def funding_de(corretora, market, item, intervalos=None):
    ex = CorretoraFalsa([market], funding={market["symbol"]: item})
    d = DadosLentos(intervalos_funding=intervalos)
    asyncio.run(d.atualizar({corretora: ex}))
    return d.funding(corretora, "BTC"), ex


def test_intervalo_do_funding_config_vale_sobre_o_do_ccxt():
    item = {"symbol": "BTC/USDC:USDC", "fundingRate": 0.0000125, "interval": "8h"}
    assert funding_de("hyperliquid", HL_BTC, item, {"hyperliquid": 1})[0] == (pytest.approx(0.00125), 1)


def test_intervalo_do_funding_sem_config_usa_o_do_ccxt():
    item = {"symbol": "BTC/USDC:USDC", "fundingRate": 0.0000125, "interval": "1h"}  # o ccxt da Hyperliquid diz 1h
    assert funding_de("hyperliquid", HL_BTC, item, {"mexc": 4})[0] == (pytest.approx(0.00125), 1)


def test_intervalo_do_funding_sem_config_nem_ccxt_e_8h():
    item = {"symbol": "BTC/USDC:USDC", "fundingRate": 0.0000125}
    assert funding_de("hyperliquid", HL_BTC, item)[0] == (pytest.approx(0.00125), 8)


def test_funding_da_hyperliquid_e_em_lote():
    item = {"symbol": "BTC/USDC:USDC", "fundingRate": 0.0000125, "interval": "1h"}
    _, ex = funding_de("hyperliquid", HL_BTC, item)
    assert ex.chamadas_funding == 1


# ---------------------------------------------------------------------------
# Ponta a ponta: futuro x futuro Hyperliquid (USDC) x MEXC (USDT), cotacao da Bitget
# ---------------------------------------------------------------------------


def test_sinal_hyperliquid_usdc_contra_mexc_usdt_com_cotacao(tmp_path):
    hyperliquid = CorretoraFalsa([HL_BTC, HL_SPOT_BTC], has={"watchTickers": True, "watchTicker": True,
                                                            "fetchFundingRates": True},
                                 funding={"BTC/USDC:USDC": {"symbol": "BTC/USDC:USDC", "fundingRate": 0.0000125,
                                                            "interval": "1h"}})
    # Obs.: a MEXC falsa responde o watch_tickers do futuro COM bid/ask para o teste ser curto. A MEXC de
    # verdade nao traz (ccxt/pro/mexc.py: sub.tickers sem bid1/ask1): no PC do dono a troca automatica
    # passa o futuro dela para um par por assinatura (watch_ticker), com aviso no log.
    mexc = CorretoraFalsa([MEXC_BTC])
    bitget = CorretoraFalsa([BITGET_USDC_USDT])
    # A Hyperliquid vende a 100,00 USDC; a MEXC compra a 101,00 USDT; 1 USDC = 0,999 USDT.
    hyperliquid.entregar(("watch_ticker", "BTC/USDC:USDC"), *[tk(99.9, 100.0) for _ in range(50)])
    hyperliquid.entregar(("livro", "BTC/USDC:USDC"), ob(99.9, 100.0))
    mexc.entregar(("watch_tickers", "futuro"), *[{"BTC/USDT:USDT": tk(101.0, 101.1)} for _ in range(50)])
    mexc.entregar(("livro", "BTC/USDT:USDT"), ob(101.0, 101.1))
    bitget.entregar(("watch_tickers", "spot"), *[{"USDC/USDT": tk(0.9989, 0.9991)} for _ in range(50)])
    exchanges = {"hyperliquid": hyperliquid, "mexc": mexc, "bitget": bitget}
    config = cfg_com(corretoras=["mexc", "bitget", "hyperliquid"], banco=str(tmp_path / "s.db"), ciclo_ms=50,
                     rp_min_lucro_pct=-5.0, mercados_por_corretora={"hyperliquid": ("futuro",)},
                     metodo_ticker={"hyperliquid": "watch_ticker"}, intervalo_funding_h={"hyperliquid": 1},
                     pasta_logs=str(tmp_path / "logs"))
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=10), timeout=5))

    banco = Banco(config.banco)
    sinais = [r["sinal"] for r in banco.sinais_do_dia("1970-01-01")]
    banco.fechar()
    hl_x_mexc = [s for s in sinais if s["ponta_long"]["corretora"] == "hyperliquid"]
    assert len(hl_x_mexc) == 1
    s = hl_x_mexc[0]
    assert s["tipo"] == "futuro_futuro" and s["par"] == "BTC/USDT"
    assert s["aviso_cotacao"] is False
    assert s["ponta_long"]["simbolo_original"] == "BTC/USDC:USDC"
    assert s["ponta_long"]["preco"] == pytest.approx(100.0 * 0.999)      # ask em USDC convertido para USDT
    assert s["ponta_short"]["corretora"] == "mexc" and s["ponta_short"]["preco"] == pytest.approx(101.0)
    assert s["ponta_long"]["funding_intervalo_h"] == 1
    assert s["spread_abertura_pct"] == pytest.approx((101.0 - 99.9) / 99.9 * 100)
    assert hyperliquid.pedidos_de("watch_tickers") == []                  # direto ao watch_ticker (config)
    assert all(":" in simbolos[0] for simbolos, _ in hyperliquid.pedidos_de("watch_ticker"))  # nunca o spot


# ---------------------------------------------------------------------------
# Detalhes
# ---------------------------------------------------------------------------


def test_metodos_de_ticker_da_config_e_do_coletor_sao_os_mesmos_e_na_mesma_ordem():
    from scanner.coletores.ccxt_coletor import METODOS_DE_TICKER
    from scanner.config.carregar import METODOS_TICKER
    assert tuple(metodo for metodo, _ in METODOS_DE_TICKER) == METODOS_TICKER


def test_coletor_recusa_metodo_ticker_desconhecido():
    with pytest.raises(ValueError, match="watch_order_book"):
        ColetorCcxt("gate", CorretoraFalsa([]), metodo_ticker="watch_order_book")


def test_aviso_do_limite_na_troca_automatica_nao_repete_a_cada_reconexao(caplog):
    markets = [HL_BTC, {**HL_BTC, "symbol": "ETH/USDC:USDC", "base": "ETH", "baseName": "ETH"}]
    hl = CorretoraFalsa(markets, has={"watchTickers": True, "watchTicker": True})
    coletor = ColetorCcxt("hyperliquid", hl, estado=EstadoMercados(), relogio=relogio, mercados=("futuro",),
                          mensagens_sem_bid_ask=1, limite_assinaturas=1, esperar=EsperaFalsa())
    hl.entregar(("watch_tickers", "futuro"), {"BTC/USDC:USDC": so_preco()})
    hl.entregar(("watch_ticker", "BTC/USDC:USDC"), OSError("sem rede"), OSError("sem rede"))

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDC:USDC", "ETH/USDC:USDC"]))
        await esperar_ate(lambda: len(hl.pedidos_de("watch_ticker")) >= 3)  # caiu duas vezes e voltou
        await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert len([m for m in avisos(caplog) if "1 de 2" in m]) == 1


def test_hyperliquid_com_o_ccxt_de_verdade_assina_kpepe_e_guarda_pepe():
    # O ccxt de verdade monta a assinatura e le a resposta (activeAssetCtx, exemplo da docstring dele);
    # so o envio (watch) e trocado, sem rede. O bid/ask vem dos impactPxs.
    import ccxt.pro as ccxtpro

    class Cliente:
        def __init__(self):
            self.resolvidos = {}

        def resolve(self, valor, chave):
            self.resolvidos[chave] = valor

    enviadas = []
    estado = EstadoMercados()

    async def cenario():
        ex = ccxtpro.hyperliquid()
        ex.set_markets([ex.parse_market({"name": "kPEPE", "szDecimals": "0", "markPx": "0.0101", "maxLeverage": "10",
                                         "baseId": 11})])

        async def watch_sem_rede(url, message_hash, message=None, subscribe_hash=None, subscription=None):
            enviadas.append(message)
            if len(enviadas) > 1:
                await asyncio.sleep(3600)  # a segunda espera a proxima mensagem, como o ccxt
            cliente = Cliente()
            ex.handle_active_asset_ctx(cliente, {"channel": "activeAssetCtx", "data": {"coin": "kPEPE", "ctx": {
                "dayNtlVlm": "1169046.29", "prevDayPx": "0.0102", "markPx": "0.0101", "midPx": "0.01011",
                "funding": "0.0000125", "impactPxs": ["0.010105", "0.010115"]}}})
            return cliente.resolvidos["ticker:KPEPE/USDC:USDC"]

        ex.watch = watch_sem_rede
        coletor = ColetorCcxt("hyperliquid", ex, estado=estado, relogio=relogio, mercados=("futuro",),
                              metodo_ticker="watch_ticker")
        tarefa = asyncio.create_task(coletor.manter_tickers(["KPEPE/USDC:USDC"]))
        await esperar_ate(lambda: estado.obter_ticker("hyperliquid", "futuro", "PEPE", AGORA) is not None)
        await parar(tarefa)
        await ex.close()

    asyncio.run(cenario())
    assert enviadas[0]["subscription"] == {"type": "activeAssetCtx", "coin": "kPEPE"}
    ticker = estado.obter_ticker("hyperliquid", "futuro", "PEPE", AGORA)
    assert (ticker["bid"], ticker["ask"]) == (pytest.approx(0.010105 / 1000), pytest.approx(0.010115 / 1000))
    assert ticker["cotacao"] == "USDC" and ticker["simbolo_original"] == "KPEPE/USDC:USDC"



# ---------------------------------------------------------------------------
# Correcao pos-revisao
# ---------------------------------------------------------------------------

HL_ETH = {**HL_BTC, "id": "1", "symbol": "ETH/USDC:USDC", "base": "ETH", "baseName": "ETH"}


def coletor_hyperliquid_um_a_um(espera, **kwargs):
    hl = CorretoraFalsa([HL_BTC, HL_ETH], has={"watchTickers": True, "watchTicker": True})
    estado = EstadoMercados()
    coletor = ColetorCcxt("hyperliquid", hl, estado=estado, relogio=relogio, mercados=("futuro",),
                          metodo_ticker="watch_ticker", mensagens_sem_bid_ask=2, esperar=espera, **kwargs)
    return hl, estado, coletor


async def esvaziar(corretora_falsa, *chaves):
    """Espera as filas `chaves` esvaziarem e da a vez ao coletor tratar a ultima mensagem."""
    await esperar_ate(lambda: all(corretora_falsa.fila(c).empty() for c in chaves))
    for _ in range(5):
        await asyncio.sleep(0)


def test_ultimo_metodo_sem_bid_ask_nao_desliga_o_mercado_e_tenta_de_novo_com_espera_crescente(caplog):
    # Hyperliquid so com watch_ticker: as primeiras mensagens sem bid/ask (ex.: impactPxs vazios) nao podem
    # deixar a corretora sem tickers ate reiniciar o scanner.
    espera = EsperaFalsa()
    hl, estado, coletor = coletor_hyperliquid_um_a_um(espera)
    btc, eth = ("watch_ticker", "BTC/USDC:USDC"), ("watch_ticker", "ETH/USDC:USDC")

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDC:USDC", "ETH/USDC:USDC"]))
        hl.entregar(btc, so_preco())
        hl.entregar(eth, so_preco())
        await esperar_ate(lambda: espera.esperas == [1])
        hl.entregar(btc, so_preco())
        hl.entregar(eth, so_preco())
        await esperar_ate(lambda: espera.esperas == [1, 2])     # espera crescente
        hl.entregar(btc, tk(100.0, 100.1))                          # agora veio com bid/ask
        await esperar_ate(lambda: estado.obter_ticker("hyperliquid", "futuro", "BTC", AGORA) is not None)
        await parar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    erros = [m for m in avisos(caplog, logging.ERROR) if "bid/ask" in m]
    assert len(erros) == 1 and "tentando de novo em 1s" in erros[0]  # ERRO uma vez; depois so aviso
    assert any("bid/ask" in m and "tentando de novo em 2s" in m for m in avisos(caplog))
    assert hl.pedidos_de("watch_tickers") == []


def test_um_a_um_so_conclui_sem_bid_ask_com_mensagens_de_simbolos_diferentes():
    espera = EsperaFalsa()
    hl, estado, coletor = coletor_hyperliquid_um_a_um(espera)
    btc, eth = ("watch_ticker", "BTC/USDC:USDC"), ("watch_ticker", "ETH/USDC:USDC")

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDC:USDC", "ETH/USDC:USDC"]))
        hl.entregar(btc, *[so_preco() for _ in range(5)])  # 5 mensagens ruins, todas do mesmo par
        await esvaziar(hl, btc)
        assert espera.esperas == []                          # um par so nao condena o metodo
        hl.entregar(eth, so_preco())                         # o segundo par tambem sem bid/ask
        await esperar_ate(lambda: espera.esperas == [1])
        await parar(tarefa)

    asyncio.run(cenario())


def test_par_de_cotacao_nunca_e_cortado_pelo_limite_de_assinaturas():
    # Fonte que assina um par por vez (como a BingX) com limite: o USDC/USDT (volume baixo) sempre fica.
    markets = [BITGET_USDC_USDT, market_ccxt("BTC/USDT", "BTC"), market_ccxt("ETH/USDT", "ETH")]
    bingx = CorretoraFalsa(markets, has={"watchTicker": True})
    estado = EstadoMercados()
    estado.registrar_mensagem("bingx", AGORA)
    for ativo, simbolo, volume in (("USDC", "USDC/USDT", 1.0), ("BTC", "BTC/USDT", 9e9), ("ETH", "ETH/USDT", 1e8)):
        estado.atualizar_ticker("bingx", "spot", ativo, 1, 2, AGORA, volume, simbolo_original=simbolo)
    coletor = ColetorCcxt("bingx", bingx, estado=estado, relogio=relogio, limite_assinaturas=2)
    coletor.usar_para_cotacao({"USDC/USDT"})

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT", "ETH/USDT", "USDC/USDT"]))
        await esperar_ate(lambda: len(bingx.pedidos_de("watch_ticker")) >= 2)
        await asyncio.sleep(0.01)
        await parar(tarefa)

    asyncio.run(cenario())
    assert sorted(s for (s,), _ in bingx.pedidos_de("watch_ticker")) == ["BTC/USDT", "USDC/USDT"]


def test_par_de_cotacao_fica_tambem_na_troca_automatica_para_o_um_a_um():
    markets = [BITGET_USDC_USDT, market_ccxt("BTC/USDT", "BTC"), market_ccxt("ETH/USDT", "ETH")]
    bitget = CorretoraFalsa(markets, has={"watchTickers": True, "watchTicker": True})
    coletor = ColetorCcxt("bitget", bitget, estado=EstadoMercados(), relogio=relogio, limite_assinaturas=2,
                          mensagens_sem_bid_ask=1)
    coletor.usar_para_cotacao({"USDC/USDT"})
    bitget.entregar(("watch_tickers", "spot"), {"BTC/USDT": so_preco(), "ETH/USDT": so_preco()})

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT", "ETH/USDT", "USDC/USDT"]))
        await esperar_ate(lambda: len(bitget.pedidos_de("watch_ticker")) >= 2)
        await asyncio.sleep(0.01)
        await parar(tarefa)

    asyncio.run(cenario())
    pedidos = [s for (s,), _ in bitget.pedidos_de("watch_ticker")]
    assert "USDC/USDT" in pedidos and len(pedidos) == 2


def test_padroes_da_cotacao_no_dados_lentos_sao_os_da_config():
    import inspect

    from scanner.config.carregar import Config
    padroes = inspect.signature(DadosLentos).parameters
    assert padroes["fonte_cotacao"].default == Config.fonte_cotacao
    assert padroes["faixa_cotacao_pct"].default == Config.faixa_cotacao_pct


def test_metodo_anotado_cujo_seguinte_nao_existe_no_mercado_volta_a_ser_tentado():
    # O watch_bids_asks esta no `has`, mas nao existe neste mercado (NotSupported): o watch_tickers, anotado
    # como sem bid/ask, era na verdade o ultimo. Ele nao fica desligado: tenta de novo depois da espera.
    estado = EstadoMercados()
    espera = EsperaFalsa()
    gate = CorretoraFalsa([market_ccxt("BTC/USDT:USDT", "BTC")], has={"watchTickers": True, "watchBidsAsks": True})
    coletor = ColetorCcxt("gate", gate, estado=estado, relogio=relogio, mensagens_sem_bid_ask=2, esperar=espera)
    lote = ("watch_tickers", "futuro")
    gate.entregar(lote, {"BTC/USDT:USDT": so_preco()}, {"BTC/USDT:USDT": so_preco()})
    gate.entregar(("watch_bids_asks", "futuro"), NotSupported("so no spot"))

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers(["BTC/USDT:USDT"]))
        await esperar_ate(lambda: espera.esperas == [1])
        gate.entregar(lote, {"BTC/USDT:USDT": tk(100.0, 100.1)})
        await esperar_ate(lambda: estado.obter_ticker("gate", "futuro", "BTC", AGORA) is not None)
        await parar(tarefa)

    asyncio.run(cenario())
    assert len(gate.pedidos_de("watch_tickers")) >= 3  # 2 sem bid/ask, a espera, e de novo o lote
