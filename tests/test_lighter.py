"""Testes da Task 16: conector proprio da Lighter (principal e Robinhood).

Cobre:
  - livro_de_lighter: as mesmas regras de descarte do livro_de_ccxt (vazio, preco <= 0 ou invalido, cruzado);
  - orderBookDetails -> markets (so perpetuos ativos, taxa taker em %);
  - market_stats/all -> tickers no estado (ativo normalizado, cotacao USDC, volume) e funding no DadosLentos;
  - order_book: livro completo + mudancas, nivel com tamanho 0 sai, sequencia quebrada (begin_nonce) tira o
    livro do estado e assina de novo, livro cruzado descartado;
  - reconexao com espera crescente, ping periodico, silencio da conexao, erros do servidor;
  - o ClienteLighter de verdade (aiohttp) contra um servidor local de teste (127.0.0.1, sem internet).

As fixtures em tests/dados/ seguem o formato da documentacao oficial e precisam ser conferidas com uma
mensagem real no PC do dono (veja o campo "_conferir" de cada uma).
"""

import asyncio
import json
import logging
import socket
import types
from pathlib import Path

import pytest

import scanner.__main__ as principal
from scanner.coletores.ccxt_coletor import ColetorCcxt
from scanner.coletores.dados_lentos import DadosLentos
from scanner.coletores.lighter import (
    CORRETORAS_LIGHTER,
    INTERVALO_FUNDING_LIGHTER_H,
    ClienteLighter,
    ColetorLighter,
    ErroApiLighter,
    criar_cliente_lighter,
    criar_coletor_lighter,
    e_lighter,
    endereco_somente_leitura,
    livro_de_lighter,
    markets_de_lighter,
    ticker_de_lighter,
)
from scanner.config.carregar import Config
from scanner.estado import EstadoMercados
from tests.fabricas import ClienteLighterFalso, EsperaFalsa, WsLighterFalso, cfg_com, esperar_ate, market_ccxt

DADOS = Path(__file__).resolve().parent / "dados"
AGORA = 1000.0


def relogio():
    return AGORA


def carregar(nome: str) -> dict:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


def avisos(caplog, nivel=logging.WARNING) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith("scanner") and r.levelno >= nivel]


async def parar(tarefa: asyncio.Task) -> None:
    tarefa.cancel()
    await asyncio.gather(tarefa, return_exceptions=True)


MARKETS, _ = markets_de_lighter(carregar("lighter_order_book_details.json"), "USDC")
ETH = "ETH/USDC:USDC"   # market_id 0
BTC = "BTC/USDC:USDC"   # market_id 1


def nivel(preco, tamanho) -> dict:
    return {"price": str(preco), "size": str(tamanho)}


def msg_livro(market_id, bids=(), asks=(), nonce=1, begin_nonce=None, tipo="update/order_book", ts=None) -> dict:
    """Mensagem do canal order_book no formato documentado (tipo "subscribed/order_book" = livro completo)."""
    return {
        "channel": f"order_book:{market_id}",
        "offset": 10,
        "order_book": {"code": 0, "bids": [nivel(p, q) for p, q in bids], "asks": [nivel(p, q) for p, q in asks],
                       "offset": 10, "nonce": nonce, "begin_nonce": begin_nonce, "last_updated_at": 1},
        "timestamp": ts,
        "type": tipo,
    }


def completo(market_id=0, bids=((2064.50, 0.81), (2064.42, 2.5)), asks=((2064.54, 0.3285), (2064.60, 1.2)),
             nonce=100) -> dict:
    return msg_livro(market_id, bids, asks, nonce=nonce, tipo="subscribed/order_book")


def stats(market_id, bid="2064.50", ask="2064.54", funding="0.0012", volume=1_000_000.0, variacao=1.5) -> dict:
    return {"market_id": market_id, "best_bid_price": bid, "best_ask_price": ask, "current_funding_rate": funding,
            "daily_quote_token_volume": volume, "daily_price_change": variacao}


def msg_stats(*itens, ts=None) -> dict:
    return {"channel": "market_stats:all", "market_stats": {str(i["market_id"]): i for i in itens},
            "timestamp": ts, "type": "update/market_stats"}


def coletor_com(ws_ou_lista, estado=None, markets=None, **kwargs) -> tuple[ColetorLighter, ClienteLighterFalso]:
    conexoes = ws_ou_lista if isinstance(ws_ou_lista, list) else [ws_ou_lista]
    cliente = ClienteLighterFalso(MARKETS if markets is None else markets, conexoes)
    kwargs.setdefault("relogio", relogio)
    return ColetorLighter("principal", cliente, estado=estado, **kwargs), cliente


# ---------------------------------------------------------------------------
# Os dois testes do brief (literais)
# ---------------------------------------------------------------------------


def inverter_bids_e_asks(msg: dict) -> None:
    """Troca os bids pelos asks no livro da mensagem: o melhor bid fica acima do melhor ask (cruzado)."""
    livro = msg["order_book"]
    livro["bids"], livro["asks"] = livro["asks"], livro["bids"]


def test_livro_lighter_real():
    msg = json.loads(Path("tests/dados/lighter_livro.json").read_text())
    l = livro_de_lighter(msg, "principal", agora=0)
    assert l is not None and l.bids[0][0] < l.asks[0][0]


def test_livro_lighter_cruzado_descartado():
    msg = json.loads(Path("tests/dados/lighter_livro.json").read_text())
    inverter_bids_e_asks(msg)  # helper do teste
    assert livro_de_lighter(msg, "principal", agora=0) is None


# ---------------------------------------------------------------------------
# livro_de_lighter: as mesmas regras de descarte do livro_de_ccxt
# ---------------------------------------------------------------------------


def test_livro_lighter_preenche_os_campos_e_ordena_os_niveis():
    msg = carregar("lighter_livro.json")
    msg["order_book"]["bids"].reverse()  # a ordem que chega nao importa: o melhor preco fica sempre primeiro
    livro = livro_de_lighter(msg, "principal", agora=5.0, simbolo=ETH)
    assert (livro.corretora, livro.mercado, livro.simbolo_original) == ("lighter", "futuro", ETH)
    assert livro.bids == [(2064.50, 0.81), (2064.42, 2.5), (2064.30, 4.1)]
    assert livro.asks == [(2064.54, 0.3285), (2064.60, 1.2), (2064.75, 3.05)]
    assert livro.recebido_em == 5.0
    assert livro.ts_corretora == pytest.approx(1774884082.326)  # a Lighter manda o timestamp em ms


def test_livro_lighter_fica_com_os_melhores_niveis_de_cada_lado():
    # A Lighter manda o livro inteiro; o estado so precisa do comeco (o motor come ate o tamanho de referencia).
    niveis = Config().niveis_livro_lighter
    bids = [(1000.0 - i, 1.0) for i in range(niveis + 50)]
    asks = [(1001.0 + i, 1.0) for i in range(niveis + 50)]
    msg = msg_livro(0, bids[::-1], asks[::-1], tipo="subscribed/order_book")
    livro = livro_de_lighter(msg, "principal", agora=0)
    assert livro.bids == bids[:niveis] and livro.asks == asks[:niveis]
    assert livro_de_lighter(msg, "principal", agora=0, niveis=5).bids == bids[:5]   # niveis_livro_lighter


def test_livro_lighter_robinhood_e_sem_simbolo_usa_o_id_do_canal():
    livro = livro_de_lighter(carregar("lighter_livro.json"), "robinhood", agora=0)
    assert livro.corretora == "lighter_rh" and livro.simbolo_original == "0"


@pytest.mark.parametrize("bids, asks", [
    ([], [(10.0, 1.0)]),                  # so um lado
    ([(9.0, 1.0)], []),
    ([], []),                             # vazio
    ([(0, 1.0)], [(10.0, 1.0)]),          # preco zero
    ([(9.0, 1.0)], [(-10.0, 1.0)]),       # preco negativo
    ([("nan", 1.0)], [(10.0, 1.0)]),      # preco invalido
    ([("abc", 1.0)], [(10.0, 1.0)]),
    ([(9.0, "x")], [(10.0, 1.0)]),        # tamanho invalido
    ([(9.0, -1.0)], [(10.0, 1.0)]),       # tamanho negativo
    ([(10.0, 1.0)], [(10.0, 1.0)]),       # bid igual ao ask
])
def test_livro_lighter_invalido_e_descartado(bids, asks):
    assert livro_de_lighter(msg_livro(0, bids, asks, tipo="subscribed/order_book"), "principal", agora=0) is None


def test_livro_lighter_ignora_nivel_com_tamanho_zero_e_mensagem_sem_livro():
    msg = msg_livro(0, [(9.0, 0), (8.0, 1.0)], [(10.0, 1.0)], tipo="subscribed/order_book")
    assert livro_de_lighter(msg, "principal", agora=0).bids == [(8.0, 1.0)]
    assert livro_de_lighter({"channel": "order_book:0"}, "principal", agora=0) is None
    assert livro_de_lighter({"order_book": {"bids": "x", "asks": []}}, "principal", agora=0) is None


def test_livro_lighter_com_instancia_desconhecida_da_erro():
    with pytest.raises(ValueError, match="instancia"):
        livro_de_lighter(carregar("lighter_livro.json"), "testnet", agora=0)


# ---------------------------------------------------------------------------
# orderBookDetails -> markets
# ---------------------------------------------------------------------------


def test_order_book_details_so_perpetuos_ativos_no_estilo_do_ccxt():
    markets, inativos = markets_de_lighter(carregar("lighter_order_book_details.json"), "USDC")
    assert sorted(markets) == ["1000PEPE/USDC:USDC", "BTC/USDC:USDC", "ETH/USDC:USDC"]  # sem o spot e sem o LUNA
    assert inativos == 1
    eth = markets[ETH]
    assert (eth["id"], eth["base"], eth["quote"], eth["settle"]) == ("0", "ETH", "USDC", "USDC")
    assert eth["swap"] and eth["linear"] and not eth["spot"] and not eth["inverse"] and eth["active"]
    assert eth["contractSize"] == 1
    assert eth["precision"] == {"amount": pytest.approx(0.0001), "price": pytest.approx(0.01)}
    assert eth["taker"] == 0.0   # "0.0000" = 0% (conta Standard)


def test_robinhood_cota_em_usdg():
    markets, _ = markets_de_lighter(carregar("lighter_order_book_details.json"), "USDG")
    assert "ETH/USDG:USDG" in markets and markets["ETH/USDG:USDG"]["quote"] == "USDG"


def test_taxa_taker_da_lighter_e_em_percentual():
    # Na resposta real os campos de taxa vem em % com 4 casas (liquidation_fee "1.0000" = 1%), como os do
    # market_stats (funding_clamp_small "0.0500" = 0,05% da documentacao do funding): "0.0280" = 0,028%.
    resposta = {"order_book_details": [{"symbol": "ETH", "market_id": 0, "status": "active", "taker_fee": "0.0280"}]}
    markets, _ = markets_de_lighter(resposta, "USDC")
    assert markets[ETH]["taker"] == pytest.approx(0.00028)   # fracao, como o market["taker"] do ccxt
    dados = DadosLentos()
    dados.receber_taxas("lighter", markets)
    assert dados.taxa_taker("lighter", "futuro") == pytest.approx(0.028)


def test_order_book_details_pula_itens_malformados():
    resposta = {"order_book_details": [
        "nao e um dict",
        {"symbol": "", "market_id": 1, "status": "active"},
        {"symbol": "SOL", "status": "active"},                                     # sem market_id
        {"symbol": "DOGE", "market_id": True, "status": "active"},
        {"symbol": "ETH/USDC", "market_id": 2048, "status": "active"},              # spot no lugar errado
        {"symbol": "ARB", "market_id": 9, "market_type": "spot", "status": "active"},
        {"symbol": "WIF", "market_id": 5, "status": "active", "taker_fee": "abc"},  # taxa ilegivel: sem taxa
    ]}
    markets, inativos = markets_de_lighter(resposta, "USDC")
    assert list(markets) == ["WIF/USDC:USDC"] and markets["WIF/USDC:USDC"]["taker"] is None
    assert inativos == 0


def test_resposta_sem_lista_de_perpetuos_da_erro():
    with pytest.raises(ErroApiLighter, match="order_book_details"):
        markets_de_lighter({"code": 200}, "USDC")


# ---------------------------------------------------------------------------
# market_stats -> tickers no estado, funding e taxa no DadosLentos
# ---------------------------------------------------------------------------


def test_ticker_de_lighter_le_bid_ask_volume_e_variacao():
    t = ticker_de_lighter("lighter", ETH, stats(0), agora=7.0)
    assert (t.corretora, t.mercado, t.simbolo_original, t.bid, t.ask) == ("lighter", "futuro", ETH, 2064.50, 2064.54)
    assert (t.volume_24h_usd, t.variacao_24h_pct, t.recebido_em) == (1_000_000.0, 1.5, 7.0)


@pytest.mark.parametrize("bid, ask", [(None, "1"), ("1", None), ("0", "1"), ("1", "1"), ("2", "1"), ("x", "1")])
def test_ticker_de_lighter_sem_bid_ask_valido_e_descartado(bid, ask):
    assert ticker_de_lighter("lighter", ETH, stats(0, bid=bid, ask=ask), agora=0) is None


def test_market_stats_vira_ticker_com_ativo_normalizado_cotacao_e_volume_contra_a_pepe_da_mexc(caplog):
    # 1000PEPE da Lighter (cota em USDC) e PEPE da MEXC (USDT) sao o mesmo ativo PEPE.
    instante = 1774884082.7  # 200 ms depois do timestamp da fixture
    estado = EstadoMercados()
    dados = DadosLentos(estado=estado, relogio=lambda: instante)
    ws = WsLighterFalso(respostas={"market_stats/all": [[carregar("lighter_market_stats.json")]]})
    lighter, _ = coletor_com(ws, estado=estado, relogio=lambda: instante, dados_lentos=dados)
    mexc = ColetorCcxt("mexc", types.SimpleNamespace(markets={
        "PEPE/USDT:USDT": market_ccxt("PEPE/USDT:USDT", "PEPE", taker=0.0002)}), estado=estado, relogio=relogio)
    listas, _ = principal._listas_de_tickers({"lighter": lighter, "mexc": mexc}, dados)
    assert listas["lighter"] == ["1000PEPE/USDC:USDC"] and listas["mexc"] == ["PEPE/USDT:USDT"]

    async def cenario():
        tarefa = asyncio.create_task(lighter.manter_tickers(listas["lighter"]))
        await esperar_ate(lambda: estado.obter_ticker("lighter", "futuro", "PEPE", instante) is not None)
        await parar(tarefa)
        await lighter.fechar()

    asyncio.run(cenario())
    ticker = estado.ticker_guardado("lighter", "futuro", "PEPE")
    assert ticker["bid"] == pytest.approx(0.010012 / 1000) and ticker["ask"] == pytest.approx(0.010015 / 1000)
    assert ticker["cotacao"] == "USDC" and ticker["simbolo_original"] == "1000PEPE/USDC:USDC"
    assert ticker["volume_24h_usd"] == 8150000.5 and ticker["variacao_24h_pct"] == 2.15
    assert estado.mercados() == {"lighter": {("futuro", "PEPE")}}   # ETH e BTC nao estao na lista de tickers
    assert estado.atraso_medio_ms("lighter", instante) == pytest.approx(200, abs=1)
    assert ws.canais() == ["market_stats/all"]


def test_funding_e_taxa_da_lighter_vao_para_o_dados_lentos():
    dados = DadosLentos(taxas_config={("lighter_rh", "futuro"): 0.035})
    estado = EstadoMercados()
    ws = WsLighterFalso(respostas={"market_stats/all": [[carregar("lighter_market_stats.json")]]})
    coletor, _ = coletor_com(ws, estado=estado, dados_lentos=dados)
    coletor.simbolos_por_chave()                    # indexa os markets: a taxa ja vai para o DadosLentos
    assert dados.taxa_taker("lighter", "futuro") == 0.0

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: dados.funding("lighter", "PEPE") is not None)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    # current_funding_rate ja vem em % (por hora: a Lighter cobra de hora em hora), para todos os mercados.
    assert INTERVALO_FUNDING_LIGHTER_H == 1
    assert dados.funding("lighter", "ETH") == (pytest.approx(0.0012), 1)
    assert dados.funding("lighter", "BTC") == (pytest.approx(0.0010), 1)
    assert dados.funding("lighter", "PEPE") == (pytest.approx(-0.0025), 1)


def test_intervalo_do_funding_da_config_vale_sobre_o_da_lighter():
    dados = DadosLentos(intervalos_funding={"lighter": 2})
    dados.receber_funding("lighter", "ETH", 0.0012, INTERVALO_FUNDING_LIGHTER_H)
    assert dados.funding("lighter", "ETH") == (0.0012, 2)


@pytest.mark.parametrize("valor", [None, float("nan"), float("inf"), True, "0.1"])
def test_funding_invalido_nao_entra(valor):
    dados = DadosLentos()
    dados.receber_funding("lighter", "ETH", 0.0012, 1)
    dados.receber_funding("lighter", "ETH", valor, 1)
    assert dados.funding("lighter", "ETH") == (0.0012, 1)   # fica o ultimo valido


def test_taxa_da_config_vale_sobre_a_da_lighter():
    dados = DadosLentos(taxas_config={("lighter", "futuro"): 0.028})
    dados.receber_taxas("lighter", MARKETS)
    assert dados.taxa_taker("lighter", "futuro") == 0.028


def test_market_stats_de_um_mercado_so_tambem_serve():
    # O canal market_stats/{id} manda um objeto so (sem o nivel por market_id).
    estado = EstadoMercados()
    msg = {"channel": "market_stats:0", "market_stats": stats(0), "type": "update/market_stats"}
    ws = WsLighterFalso(respostas={"market_stats/all": [[msg]]})
    coletor, _ = coletor_com(ws, estado=estado)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: estado.obter_ticker("lighter", "futuro", "ETH", AGORA) is not None)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


# ---------------------------------------------------------------------------
# Livros (etapa 2): livro completo, mudancas, sequencia, cruzado
# ---------------------------------------------------------------------------


def livro_no_estado(estado, ativo="ETH"):
    return estado.obter_livro("lighter", "futuro", ativo, AGORA)


def test_livro_completo_e_mudancas_juntam_no_livro_guardado():
    estado = EstadoMercados()
    ws = WsLighterFalso(respostas={"order_book/0": [[completo()]]})
    coletor, _ = coletor_com(ws, estado=estado)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        primeiro = livro_no_estado(estado)
        assert primeiro.bids == [(2064.50, 0.81), (2064.42, 2.5)] and primeiro.asks[0] == (2064.54, 0.3285)
        # Mudancas: 2064.42 passa a 5.0 (o tamanho e o total novo), 2064.48 entra e 2064.54 sai (tamanho 0).
        ws.entregar(msg_livro(0, bids=[(2064.42, 5.0), (2064.48, 1.0)], asks=[(2064.54, 0)], nonce=101,
                              begin_nonce=100, ts=1774884082326))
        await esperar_ate(lambda: livro_no_estado(estado).asks[0][0] == 2064.60)
        livro = livro_no_estado(estado)
        assert livro.bids == [(2064.50, 0.81), (2064.48, 1.0), (2064.42, 5.0)]
        assert livro.asks == [(2064.60, 1.2)]
        assert livro.simbolo_original == ETH and livro.ts_corretora == pytest.approx(1774884082.326)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert ws.canais() == ["market_stats/all", "order_book/0"]


def test_sequencia_quebrada_tira_o_livro_do_estado_e_assina_de_novo(caplog):
    estado = EstadoMercados()
    novo = completo(bids=[(2000.0, 1.0)], asks=[(2000.5, 1.0)], nonce=500)
    # A segunda assinatura nao responde sozinha: o teste entrega o livro novo na hora que quiser.
    ws = WsLighterFalso(respostas={"order_book/0": [[completo()], []]})
    coletor, _ = coletor_com(ws, estado=estado)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        # Pulou mudancas: begin_nonce 103 nao e o nonce anterior (100).
        ws.entregar(msg_livro(0, bids=[(2064.0, 1.0)], nonce=105, begin_nonce=103))
        await esperar_ate(lambda: ws.canais().count("order_book/0") == 2)
        assert livro_no_estado(estado) is None
        # Cancela antes de assinar de novo (assinar por cima de uma assinatura viva a Lighter recusa).
        tipos = [(d["type"], d["channel"]) for d in ws.enviadas if d.get("channel") == "order_book/0"]
        assert tipos == [("subscribe", "order_book/0"), ("unsubscribe", "order_book/0"), ("subscribe", "order_book/0")]
        # A confirmacao do cancelamento e uma mudanca velha que ainda chega antes do livro novo: ignoradas.
        ws.entregar({"type": "unsubscribed", "channel": "order_book:0"})
        ws.entregar(msg_livro(0, bids=[(2064.1, 1.0)], nonce=106, begin_nonce=105))
        ws.entregar(novo)
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        assert livro_no_estado(estado).bids == [(2000.0, 1.0)]
        ws.entregar(msg_livro(0, bids=[(2000.1, 2.0)], nonce=501, begin_nonce=500))
        await esperar_ate(lambda: livro_no_estado(estado).bids[0] == (2000.1, 2.0))
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    # A mudanca velha nao foi aplicada (nem descartada como livro invalido): so a quebra conta.
    assert coletor.tirar_descartes() == {"livros": 0, "tickers": 0, "sequencia_quebrada": 1}
    assert any("sequencia do livro quebrada" in m and ETH in m for m in avisos(caplog))


def test_sequencia_que_quebra_de_novo_sem_mudanca_conferida_espera_antes_de_assinar():
    estado = EstadoMercados()
    espera = EsperaFalsa()
    ws = WsLighterFalso(respostas={"order_book/0": [[completo(nonce=100)], [completo(nonce=200)],
                                                    [completo(nonce=300)], [completo(nonce=400)]]})
    coletor, _ = coletor_com(ws, estado=estado, esperar=espera)

    async def quebrar_e_esperar_assinatura(begin, vezes):
        ws.entregar(msg_livro(0, bids=[(1.0, 1.0)], nonce=begin + 5, begin_nonce=begin + 1))
        await esperar_ate(lambda: ws.canais().count("order_book/0") == vezes)
        await esperar_ate(lambda: livro_no_estado(estado) is not None)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        await quebrar_e_esperar_assinatura(100, 2)
        assert espera.esperas == []                 # a primeira quebra assina de novo na hora
        await quebrar_e_esperar_assinatura(200, 3)
        assert espera.esperas == [1]                # quebrou de novo sem nenhuma mudanca conferida: espera 1s
        # Uma mudanca conferida (begin_nonce certo) zera a conta: a proxima quebra volta a ser na hora.
        ws.entregar(msg_livro(0, bids=[(2064.0, 1.0)], nonce=301, begin_nonce=300))
        await esperar_ate(lambda: (2064.0, 1.0) in livro_no_estado(estado).bids)
        await quebrar_e_esperar_assinatura(301, 4)
        assert espera.esperas == [1]
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


def test_so_mudancas_sem_o_livro_completo_avisam_no_log(caplog):
    # Se a primeira mensagem nao vier como "subscribed/order_book" (formato a conferir), o livro nunca fica
    # pronto: o log precisa dizer, em vez de a Lighter ficar sem sinais em silencio.
    estado = EstadoMercados()
    mudancas = [msg_livro(0, [(1.0, 1.0)], [(2.0, 1.0)], nonce=n, begin_nonce=n - 1) for n in range(1, 8)]
    ws = WsLighterFalso(respostas={"order_book/0": [mudancas]})
    coletor, _ = coletor_com(ws, estado=estado, mudancas_sem_livro_completo=5)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        await esperar_ate(lambda: "order_book/0" in ws.canais() and ws.vazia())
        for _ in range(5):
            await asyncio.sleep(0)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert livro_no_estado(estado) is None
    assert len([m for m in avisos(caplog) if "subscribed/order_book" in m and ETH in m]) == 1


def test_livro_cruzado_depois_das_mudancas_e_descartado_e_o_anterior_fica():
    estado = EstadoMercados()
    ws = WsLighterFalso(respostas={"order_book/0": [[completo()]]})
    coletor, _ = coletor_com(ws, estado=estado)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        # Um bid acima do melhor ask: o livro juntado fica cruzado e e descartado (o anterior fica).
        ws.entregar(msg_livro(0, bids=[(2064.70, 1.0)], nonce=101, begin_nonce=100))
        await esperar_ate(lambda: coletor._descartes["livros"] == 1)
        assert livro_no_estado(estado).bids[0] == (2064.50, 0.81)
        # A proxima mudanca tira o nivel cruzado: o livro volta a valer.
        ws.entregar(msg_livro(0, bids=[(2064.70, 0)], asks=[(2064.54, 2.0)], nonce=102, begin_nonce=101))
        await esperar_ate(lambda: livro_no_estado(estado).asks[0] == (2064.54, 2.0))
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert coletor.tirar_descartes() == {"livros": 1, "tickers": 0, "sequencia_quebrada": 0}


def test_par_que_sai_da_lista_cancela_o_livro_e_sai_do_estado_na_hora():
    estado = EstadoMercados()
    ws = WsLighterFalso(respostas={"order_book/0": [[completo()]]})
    coletor, _ = coletor_com(ws, estado=estado)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        await coletor.assinar_livros(set())
        assert livro_no_estado(estado) is None
        assert ws.canais("unsubscribe") == ["order_book/0"]
        ws.entregar(msg_livro(0, bids=[(2064.0, 1.0)], nonce=101, begin_nonce=100))  # atrasada: ignorada
        ws.entregar({"type": "unsubscribed", "channel": "order_book:0"})
        await esperar_ate(ws.vazia)
        await asyncio.sleep(0)
        assert livro_no_estado(estado) is None
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


def test_livro_pedido_antes_de_conectar_e_assinado_quando_a_conexao_abre():
    estado = EstadoMercados()
    ws = WsLighterFalso(respostas={"order_book/1": [[completo(market_id=1, bids=[(64000.0, 1.0)],
                                                              asks=[(64001.0, 1.0)])]]})
    coletor, _ = coletor_com(ws, estado=estado)

    async def cenario():
        await coletor.assinar_livros({BTC})
        tarefa = asyncio.create_task(coletor.manter_tickers([BTC]))
        await esperar_ate(lambda: livro_no_estado(estado, "BTC") is not None)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert ws.canais() == ["market_stats/all", "order_book/1"]


def test_mercado_que_sai_na_recarga_sai_dos_livros_sem_derrubar_a_conexao():
    estado = EstadoMercados()
    btc = completo(market_id=1, bids=[(64000.0, 1.0)], asks=[(64001.0, 1.0)])
    ws = WsLighterFalso(respostas={"order_book/0": [[completo()]], "order_book/1": [[btc]]})
    coletor, cliente = coletor_com(ws, estado=estado, esperar=EsperaFalsa())

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH, BTC]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH, BTC})
        await esperar_ate(lambda: livro_no_estado(estado) is not None and livro_no_estado(estado, "BTC") is not None)
        # A recarga da lista de mercados (load_markets com reload) tirou o ETH.
        cliente.markets = {s: m for s, m in MARKETS.items() if s != ETH}
        assert ("futuro", "ETH") not in coletor.simbolos_por_chave()
        assert livro_no_estado(estado) is None and livro_no_estado(estado, "BTC") is not None
        await coletor.assinar_livros({ETH, BTC})     # o motor ainda pode pedir o ETH por um ciclo
        ws.entregar(msg_livro(0, bids=[(2064.0, 1.0)], nonce=101, begin_nonce=100))  # o ETH ainda chega: ignorado
        ws.entregar(msg_livro(1, bids=[(64000.5, 2.0)], nonce=101, begin_nonce=7))   # BTC quebra: pede de novo
        await esperar_ate(lambda: ws.canais().count("order_book/1") == 2)
        assert livro_no_estado(estado) is None
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert cliente.conectou == 1                     # nada disso derrubou a conexao
    assert ws.canais().count("order_book/0") == 1


def test_simbolo_desconhecido_avisa_uma_vez_e_nao_assina(caplog):
    ws = WsLighterFalso()
    coletor, _ = coletor_com(ws, estado=EstadoMercados())

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({"XYZ/USDC:USDC"})
        await coletor.assinar_livros({"XYZ/USDC:USDC"})
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert ws.canais() == ["market_stats/all"]
    assert len([m for m in avisos(caplog) if "XYZ/USDC:USDC" in m]) == 1


def test_limite_de_assinaturas_fica_com_os_livros_de_maior_volume(caplog):
    estado = EstadoMercados()
    # O ETH tem mais volume (e vem depois do BTC na ordem alfabetica: o volume e que decide).
    estatisticas = msg_stats(stats(0, volume=9e8), stats(1, bid="64000", ask="64001", volume=5e6))
    ws = WsLighterFalso(respostas={"market_stats/all": [[estatisticas]]})
    coletor, _ = coletor_com(ws, estado=estado, limite_assinaturas=1)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH, BTC]))
        await esperar_ate(lambda: estado.obter_ticker("lighter", "futuro", "BTC", AGORA) is not None)
        await coletor.assinar_livros({ETH, BTC})
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert ws.canais() == ["market_stats/all", "order_book/0"]   # o ETH tem mais volume
    assert any("limite de 1 assinaturas" in m for m in avisos(caplog))


def test_assinar_livros_sem_estado_nem_callback_da_erro():
    coletor, _ = coletor_com(WsLighterFalso())
    with pytest.raises(ValueError, match="ao_atualizar"):
        asyncio.run(coletor.assinar_livros({ETH}))


# ---------------------------------------------------------------------------
# Conexao: reconexao, ping, silencio, erros do servidor, fechar
# ---------------------------------------------------------------------------


def test_conexao_que_cai_espera_cada_vez_mais_e_volta(caplog):
    estado = EstadoMercados()
    respostas = {"market_stats/all": [[msg_stats(stats(0))]], "order_book/0": [[completo()]]}
    ws1, ws3, ws4 = WsLighterFalso(respostas), WsLighterFalso(respostas), WsLighterFalso(respostas)
    espera = EsperaFalsa()
    coletor, cliente = coletor_com([ws1, OSError("sem internet"), ws3, ws4], estado=estado, esperar=espera)

    async def cenario():
        await coletor.assinar_livros({ETH})
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: livro_no_estado(estado) is not None
                          and estado.obter_ticker("lighter", "futuro", "ETH", AGORA) is not None)
        ws1.entregar(ConnectionResetError("a corretora derrubou a conexao"))
        await esperar_ate(lambda: cliente.conectou == 3 and livro_no_estado(estado) is not None
                          and estado.saude("lighter", AGORA)["estado"] == "conectada")
        assert espera.esperas == [1, 2]                # caiu: 1s; a conexao nao abriu: 2s; abriu
        assert estado.saude("lighter", AGORA)["reconexoes"] == 1
        # Voltou a funcionar: a proxima queda volta a esperar 1s.
        ws3.entregar(ConnectionResetError("caiu de novo"))
        await esperar_ate(lambda: cliente.conectou == 4 and livro_no_estado(estado) is not None)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert espera.esperas == [1, 2, 1]
    assert estado.saude("lighter", AGORA)["reconexoes"] == 2
    assert ws3.canais() == ["market_stats/all", "order_book/0"]  # o livro desejado e assinado de novo
    quedas = [m for m in avisos(caplog) if "a conexao da lighter caiu" in m]
    assert len(quedas) == 3 and "tentando de novo em 1s" in quedas[0] and "em 2s" in quedas[1]
    # Queda de conexao nao e defeito do scanner: sem traceback.
    assert not any(r.exc_info for r in caplog.records if "a conexao da lighter caiu" in r.getMessage())


def test_queda_apaga_os_dados_da_corretora_do_estado():
    estado = EstadoMercados()
    ws = WsLighterFalso({"market_stats/all": [[msg_stats(stats(0))]], "order_book/0": [[completo()]]})
    coletor, cliente = coletor_com([ws], estado=estado, esperar=EsperaFalsa())

    async def cenario():
        await coletor.assinar_livros({ETH})
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        ws.entregar(ConnectionResetError("caiu"))
        await esperar_ate(lambda: cliente.conectou == 2)
        assert estado.mercados() == {} and livro_no_estado(estado) is None
        assert estado.saude("lighter", AGORA)["estado"] == "reconectando"
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


def test_ping_periodico_com_o_intervalo_da_config_e_pong_conta_como_mensagem():
    agora = [AGORA]
    estado = EstadoMercados()
    ws = WsLighterFalso()
    pausas, liberar = [], asyncio.Queue()

    async def esperar_ping(segundos):
        pausas.append(segundos)
        await liberar.get()

    coletor, _ = coletor_com(ws, estado=estado, relogio=lambda: agora[0], intervalo_ping_s=25,
                             esperar_ping=esperar_ping)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: pausas == [25])
        assert ws.pings() == 0
        liberar.put_nowait(None)
        await esperar_ate(lambda: ws.pings() == 1 and len(pausas) == 2)
        liberar.put_nowait(None)
        await esperar_ate(lambda: ws.pings() == 2 and len(pausas) == 3)
        assert {"type": "ping"} in ws.enviadas
        # O pong e uma mensagem como outra qualquer: a conexao fica viva.
        agora[0] = AGORA + 10
        assert not estado.conexao_viva("lighter", agora[0])
        ws.entregar({"type": "pong"})
        await esperar_ate(lambda: estado.conexao_viva("lighter", agora[0]))
        # Ping do servidor: o coletor responde pong.
        ws.entregar({"type": "ping"})
        await esperar_ate(lambda: {"type": "pong"} in ws.enviadas)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert pausas == [25, 25, 25]


def test_conexao_em_silencio_por_mais_de_dois_pings_e_tratada_como_queda(caplog):
    espera = EsperaFalsa()
    ws1, ws2 = WsLighterFalso(), WsLighterFalso()

    async def nunca(segundos):
        await asyncio.Event().wait()

    coletor, cliente = coletor_com([ws1, ws2], estado=EstadoMercados(), esperar=espera, intervalo_ping_s=0.01,
                                   esperar_ping=nunca)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: cliente.conectou >= 2)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert any("nenhuma mensagem" in m for m in avisos(caplog))
    assert ws1.fechada


def test_erros_do_servidor_vao_para_o_log_e_ja_assinado_nao(caplog):
    ws = WsLighterFalso()
    coletor, _ = coletor_com(ws, estado=EstadoMercados())

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        ws.entregar({"error": {"code": 30003, "message": "Already Subscribed to : market_stats:all"}},
                    {"error": {"code": 30005, "message": "Invalid Channel:  (marketId)"}},
                    {"error": {"code": 30005, "message": "Invalid Channel:  (marketId)"}},
                    {"error": {"code": 30010, "message": "max subscriptions limit exceeded"}})
        await esperar_ate(ws.vazia)
        await asyncio.sleep(0)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    mensagens = avisos(caplog)
    assert not any("Already Subscribed" in m for m in mensagens)
    assert len([m for m in mensagens if "Invalid Channel" in m]) == 1          # um aviso por codigo
    assert any("max subscriptions" in m and "limites_assinatura" in m for m in mensagens)


def test_mensagem_estranha_nao_derruba_a_conexao(caplog):
    estado = EstadoMercados()
    ws = WsLighterFalso()
    coletor, cliente = coletor_com(ws, estado=estado)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        ws.entregar(None, [1, 2], {"channel": "market_stats:all", "market_stats": "lixo"},
                    {"channel": "order_book:abc"}, {"type": "connected", "session_id": "x"},
                    msg_stats(stats(0)))
        await esperar_ate(lambda: estado.obter_ticker("lighter", "futuro", "ETH", AGORA) is not None)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert cliente.conectou == 1


def test_fechar_para_tudo_e_fecha_o_cliente():
    ws = WsLighterFalso()
    coletor, cliente = coletor_com(ws, estado=EstadoMercados())

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.fechar()
        await asyncio.wait_for(asyncio.gather(tarefa, return_exceptions=True), timeout=1)
        await coletor.assinar_livros({ETH})       # depois de fechar nao comeca nada

    asyncio.run(cenario())
    assert ws.fechada and cliente.fechado and cliente.conectou == 1
    assert ws.canais() == ["market_stats/all"]


def test_manter_tickers_sem_simbolos_termina_sem_conectar():
    coletor, cliente = coletor_com(WsLighterFalso(), estado=EstadoMercados())
    asyncio.run(coletor.manter_tickers([]))
    assert cliente.conectou == 0


def test_iniciar_entrega_os_livros_crus_e_fecha_no_fim():
    # Modo simples (imprimir_precos): sem estado, cada livro vai para o callback, com o preco como a Lighter manda.
    ws = WsLighterFalso({"order_book/4": [[completo(market_id=4, bids=[(0.010012, 1000)], asks=[(0.010015, 500)])]]})
    coletor, cliente = coletor_com(ws)
    recebidos = []

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar(["1000PEPE/USDC:USDC"], recebidos.append))
        await esperar_ate(lambda: recebidos)
        await parar(tarefa)

    asyncio.run(cenario())
    assert recebidos[0].simbolo_original == "1000PEPE/USDC:USDC" and recebidos[0].bids == [(0.010012, 1000.0)]
    assert cliente.fechado


def test_iniciar_com_a_conexao_caindo_levanta_o_erro_e_fecha():
    ws = WsLighterFalso()
    ws.entregar(ConnectionResetError("caiu"))
    coletor, cliente = coletor_com(ws)
    with pytest.raises(ConnectionResetError):
        asyncio.run(coletor.iniciar([ETH], lambda livro: None))
    assert cliente.fechado


# ---------------------------------------------------------------------------
# Montagem: nomes, enderecos, config
# ---------------------------------------------------------------------------


def test_nomes_e_instancias():
    assert CORRETORAS_LIGHTER == {"lighter": "principal", "lighter_rh": "robinhood"}
    assert e_lighter("lighter") and e_lighter("lighter_rh") and not e_lighter("hyperliquid")
    cliente = ClienteLighterFalso(MARKETS)
    assert ColetorLighter("principal", cliente).nome == "lighter"
    assert ColetorLighter("robinhood", cliente).nome == "lighter_rh"
    with pytest.raises(ValueError, match="instancia"):
        ColetorLighter("testnet", cliente)


@pytest.mark.parametrize("url, esperado", [
    ("wss://mainnet.zklighter.elliot.ai/stream", "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"),
    ("wss://api.rh.lighter.xyz/stream?x=1", "wss://api.rh.lighter.xyz/stream?x=1&readonly=true"),
    ("wss://api.rh.lighter.xyz/stream?readonly=true", "wss://api.rh.lighter.xyz/stream?readonly=true"),
])
def test_websocket_sempre_somente_leitura(url, esperado):
    assert endereco_somente_leitura(url) == esperado


def test_criar_cliente_usa_os_enderecos_da_config_e_nao_acessa_a_rede():
    cfg = cfg_com(url_rest_lighter_rh="https://rh.exemplo", url_ws_lighter_rh="wss://rh.exemplo/stream",
                  tempo_max_rede_lighter_s=7)
    principal_ = criar_cliente_lighter("lighter", cfg)
    rh = criar_cliente_lighter("lighter_rh", cfg)
    assert (principal_.instancia, principal_.cotacao) == ("principal", "USDC")
    assert principal_.url_rest == "https://mainnet.zklighter.elliot.ai"
    assert principal_.url_ws == "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
    assert (rh.instancia, rh.cotacao, rh.url_rest, rh.url_ws) == (
        "robinhood", "USDG", "https://rh.exemplo", "wss://rh.exemplo/stream?readonly=true")
    assert principal_.markets == {}
    assert principal_.tempo_max_s == rh.tempo_max_s == 7      # tempo_max_rede_lighter_s


def test_criar_coletor_le_a_config():
    cfg = cfg_com(corretoras=["lighter"], limites_assinatura={"lighter": 50}, intervalo_ping_lighter_s=20,
                  espera_max_reconexao_s=30, mensagens_sem_bid_ask=4, niveis_livro_lighter=40,
                  pings_sem_resposta_lighter=3, mudancas_sem_livro_completo_lighter=7,
                  tempo_max_livro_completo_lighter_s=12)
    estado, dados = EstadoMercados(), DadosLentos()
    coletor = criar_coletor_lighter("lighter", ClienteLighterFalso(MARKETS), cfg, estado, dados, relogio)
    assert isinstance(coletor, ColetorLighter) and coletor.nome == "lighter" and coletor.instancia == "principal"
    assert (coletor._limite, coletor._intervalo_ping_s) == (50, 20)
    assert (coletor._espera_max_s, coletor._max_sem_bid_ask) == (30, 4)
    assert (coletor._niveis, coletor._silencio_max_s, coletor._max_sem_completo, coletor._tempo_max_completo_s) == (
        40, 60, 7, 12)
    assert coletor._estado is estado and coletor._dados_lentos is dados


def test_padroes_da_config_da_lighter():
    c = Config()
    assert (c.niveis_livro_lighter, c.tempo_max_rede_lighter_s, c.pings_sem_resposta_lighter) == (100, 10, 2)
    assert (c.mudancas_sem_livro_completo_lighter, c.tempo_max_livro_completo_lighter_s) == (10, 10)
    assert c.url_rest_lighter == "https://mainnet.zklighter.elliot.ai"
    assert c.url_ws_lighter == "wss://mainnet.zklighter.elliot.ai/stream"
    assert c.url_rest_lighter_rh == "https://api.rh.lighter.xyz"
    assert c.url_ws_lighter_rh == "wss://api.rh.lighter.xyz/stream"
    assert c.intervalo_ping_lighter_s == 30


# ---------------------------------------------------------------------------
# O ClienteLighter de verdade (aiohttp) contra um servidor local de teste, sem internet
# ---------------------------------------------------------------------------


async def servidor_local(app):
    """Sobe o app aiohttp num endereco local (127.0.0.1, porta livre). Devolve (executor, porta)."""
    from aiohttp import web

    soquete = socket.socket()
    soquete.bind(("127.0.0.1", 0))
    executor = web.AppRunner(app)
    await executor.setup()
    await web.SockSite(executor, soquete).start()
    return executor, soquete.getsockname()[1]


def test_cliente_de_verdade_le_os_mercados_e_conversa_pelo_websocket():
    from aiohttp import web

    from scanner.coletores.lighter import ConexaoFechada

    recebidas, consultas = [], []

    async def detalhes(pedido):
        return web.json_response(carregar("lighter_order_book_details.json"))

    async def stream(pedido):
        consultas.append(dict(pedido.query))
        ws = web.WebSocketResponse()
        await ws.prepare(pedido)
        async for mensagem in ws:
            dados = json.loads(mensagem.data)
            recebidas.append(dados)
            if dados.get("type") == "ping":
                await ws.send_str(json.dumps({"type": "pong"}))
            elif dados.get("channel") == "market_stats/all":
                await ws.send_str(json.dumps(msg_stats(stats(0))))
                await ws.send_str("isto nao e json")
                await ws.close()
        return ws

    async def cenario():
        app = web.Application()
        app.router.add_get("/api/v1/orderBookDetails", detalhes)
        app.router.add_get("/stream", stream)
        executor, porta = await servidor_local(app)
        cliente = ClienteLighter("principal", f"http://127.0.0.1:{porta}", f"ws://127.0.0.1:{porta}/stream")
        try:
            markets = await cliente.load_markets()
            assert sorted(markets) == ["1000PEPE/USDC:USDC", "BTC/USDC:USDC", "ETH/USDC:USDC"]
            assert await cliente.load_markets() is markets               # sem reload: a lista ja carregada
            assert await cliente.load_markets(reload=True) is not markets
            conexao = await cliente.conectar_ws()
            await conexao.enviar({"type": "ping"})
            assert await conexao.receber() == {"type": "pong"}
            await conexao.enviar({"type": "subscribe", "channel": "market_stats/all"})
            assert (await conexao.receber())["channel"] == "market_stats:all"
            assert await conexao.receber() is None                       # texto que nao e JSON
            with pytest.raises(ConexaoFechada):
                await conexao.receber()                                  # o servidor fechou
            await conexao.fechar()
        finally:
            await cliente.close()
            await executor.cleanup()

    asyncio.run(asyncio.wait_for(cenario(), timeout=10))
    assert consultas == [{"readonly": "true"}]
    assert recebidas == [{"type": "ping"}, {"type": "subscribe", "channel": "market_stats/all"}]


def test_cliente_de_verdade_com_erro_http_levanta_erro_claro():
    from aiohttp import web

    async def detalhes(pedido):
        return web.Response(status=503, text="manutencao")

    async def cenario():
        app = web.Application()
        app.router.add_get("/api/v1/orderBookDetails", detalhes)
        executor, porta = await servidor_local(app)
        cliente = ClienteLighter("robinhood", f"http://127.0.0.1:{porta}/", "ws://127.0.0.1:1/stream")
        try:
            with pytest.raises(ErroApiLighter, match="503"):
                await cliente.load_markets()
        finally:
            await cliente.close()
            await executor.cleanup()

    asyncio.run(asyncio.wait_for(cenario(), timeout=10))


# ---------------------------------------------------------------------------
# Correcao pos-revisao (Task 16)
# ---------------------------------------------------------------------------


def test_os_dois_conectores_usam_o_mesmo_codigo_de_saude_e_limite():
    # Saude, quedas, limite de livros e descartes moram em ColetorComSaude (base.py): um ajuste vale para os dois.
    from scanner.coletores.base import ColetorComSaude, ColetorDoScanner

    for metodo in ("_chegou_ticker", "_queda_da_conexao", "_erro_ao_tratar_ticker", "_avisar_limite",
                   "_livros_dentro_do_limite", "_remover_livro_do_estado", "tirar_descartes",
                   "_registrar_mensagem", "_registrar_atraso"):
        assert getattr(ColetorLighter, metodo) is getattr(ColetorComSaude, metodo), metodo
        assert getattr(ColetorCcxt, metodo) is getattr(ColetorComSaude, metodo), metodo
    lighter = ColetorLighter("principal", ClienteLighterFalso(MARKETS))
    ccxt = ColetorCcxt("mexc", types.SimpleNamespace(markets={}))
    assert isinstance(lighter, ColetorDoScanner) and isinstance(ccxt, ColetorDoScanner)


def test_sem_begin_nonce_avisa_que_a_continuidade_nao_esta_sendo_conferida(caplog):
    # Como a amostra de update/order_book do ccxt (nov/2025): sem begin_nonce. As mudancas sao aplicadas, mas o
    # log diz com todas as letras que a sequencia nao esta sendo conferida.
    estado = EstadoMercados()
    sem_nonce = completo()
    sem_nonce["order_book"].pop("nonce")
    ws = WsLighterFalso(respostas={"order_book/0": [[sem_nonce]]})
    coletor, _ = coletor_com(ws, estado=estado)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        mudanca = msg_livro(0, bids=[(2064.48, 1.0)])
        mudanca["order_book"].pop("begin_nonce")
        mudanca["order_book"].pop("nonce")
        ws.entregar(mudanca, mudanca)
        await esperar_ate(lambda: (2064.48, 1.0) in livro_no_estado(estado).bids)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    sem = [m for m in avisos(caplog) if "begin_nonce" in m]
    assert len(sem) == 1 and "continuidade" in sem[0] and "NAO esta sendo conferida" in sem[0]


def test_ja_assinado_sem_livro_completo_pede_de_novo_cancelando_antes():
    # 30003 (ja assinado) num order_book cujo livro completo nao chegou: cancela e assina de novo.
    estado = EstadoMercados()
    ws = WsLighterFalso(respostas={"order_book/0": [[], [completo()]]})
    coletor, _ = coletor_com(ws, estado=estado, esperar=EsperaFalsa())

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        ws.entregar({"error": {"code": 30003, "message": "Already Subscribed to : order_book:0"}})
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    tipos = [d["type"] for d in ws.enviadas if d.get("channel") == "order_book/0"]
    assert tipos == ["subscribe", "unsubscribe", "subscribe"]


def test_nao_assinado_sem_livro_completo_assina_de_novo_sem_cancelar():
    # 30002 (nao estava assinado) num order_book que espera o livro completo: so assina de novo.
    estado = EstadoMercados()
    ws = WsLighterFalso(respostas={"order_book/0": [[], [completo()]]})
    coletor, _ = coletor_com(ws, estado=estado, esperar=EsperaFalsa())

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        ws.entregar({"error": {"code": 30002, "message": "Not Subscribed to : order_book:0"}})
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        # Com o livro pronto, o mesmo aviso nao pede nada (e nem vai para o log como erro).
        ws.entregar({"error": {"code": 30003, "message": "Already Subscribed to : order_book:0"}},
                    {"type": "pong"})
        await esperar_ate(ws.vazia)
        await asyncio.sleep(0)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    tipos = [d["type"] for d in ws.enviadas if d.get("channel") == "order_book/0"]
    assert tipos == ["subscribe", "subscribe"]


def test_livro_completo_que_nao_chega_no_tempo_maximo_e_pedido_de_novo_com_espera_crescente(caplog):
    agora = [AGORA]
    estado = EstadoMercados()
    espera = EsperaFalsa()
    ws = WsLighterFalso(respostas={"order_book/0": [[], [], [completo()]]})
    coletor, _ = coletor_com(ws, estado=estado, esperar=espera, relogio=lambda: agora[0],
                             tempo_max_livro_completo_s=10)

    async def passar(segundos, assinaturas):
        agora[0] += segundos
        ws.entregar({"type": "pong"})  # qualquer mensagem: o coletor confere os livros atrasados
        await esperar_ate(lambda: ws.canais().count("order_book/0") == assinaturas)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        agora[0] += 9
        ws.entregar({"type": "pong"})
        await esperar_ate(ws.vazia)
        await asyncio.sleep(0)
        assert ws.canais().count("order_book/0") == 1      # 9s: ainda dentro do tempo maximo
        await passar(2, 2)                                  # 11s sem o livro completo: pede de novo, na hora
        assert espera.esperas == []
        await passar(11, 3)                                 # de novo sem livro: agora espera 1s antes
        assert espera.esperas == [1]
        await esperar_ate(lambda: livro_no_estado(estado, ) is not None
                          and estado.obter_livro("lighter", "futuro", "ETH", agora[0]) is not None)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert any("livro completo nao chegou" in m for m in avisos(caplog))


def stats_sem_bid_ask(market_id, volume=5e8, variacao=0.5, funding="0.0012"):
    """market_stats como as amostras reais do ccxt (nov/2025): sem best_bid_price/best_ask_price."""
    return {"market_id": market_id, "index_price": "2064.6", "mark_price": "2064.5", "last_trade_price": "2064.5",
            "current_funding_rate": funding, "daily_quote_token_volume": volume, "daily_price_change": variacao}


def test_market_stats_sem_bid_ask_passa_a_etapa_1_para_os_livros(caplog):
    estado = EstadoMercados()
    estatisticas = [msg_stats(stats_sem_bid_ask(0, volume=7e5), stats_sem_bid_ask(1, volume=9e8)) for _ in range(3)]
    ws = WsLighterFalso(respostas={"market_stats/all": [estatisticas], "order_book/0": [[completo()]],
                                   "order_book/1": [[completo(market_id=1, bids=[(64000.0, 1.0)],
                                                              asks=[(64001.0, 1.0)])]]})
    coletor, _ = coletor_com(ws, estado=estado, mensagens_sem_bid_ask=3)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH, BTC]))
        await esperar_ate(lambda: estado.obter_ticker("lighter", "futuro", "ETH", AGORA) is not None
                          and estado.obter_ticker("lighter", "futuro", "BTC", AGORA) is not None)
        eth = estado.ticker_guardado("lighter", "futuro", "ETH")
        assert (eth["bid"], eth["ask"]) == (2064.50, 2064.54)                  # o melhor bid/ask do livro
        assert (eth["volume_24h_usd"], eth["variacao_24h_pct"]) == (7e5, 0.5)  # do market_stats
        assert eth["cotacao"] == "USDC" and eth["simbolo_original"] == ETH
        # O motor pedir nenhum livro nao tira os livros da etapa 1.
        await coletor.assinar_livros(set())
        assert ws.canais("unsubscribe") == []
        assert livro_no_estado(estado) is not None and livro_no_estado(estado, "BTC") is not None
        await parar(tarefa)
        await coletor.fechar()
        # A conexao acabou: os livros saem do estado e, com eles, os tickers que vinham deles.
        assert estado.ticker_guardado("lighter", "futuro", "ETH") is None

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    assert sorted(ws.canais()[1:]) == ["order_book/0", "order_book/1"]
    trocas = [m for m in avisos(caplog) if "passa a vir dos livros" in m]
    assert len(trocas) == 1 and "best_bid_price" in trocas[0]


def test_troca_para_os_livros_precisa_de_varias_mensagens_e_varios_pares():
    # Um par iliquido sem bid/ask repetindo nao troca a etapa 1 de todos (como a troca da Task 15).
    estado = EstadoMercados()
    so_eth = [msg_stats(stats_sem_bid_ask(0)) for _ in range(5)]
    ws = WsLighterFalso(respostas={"market_stats/all": [so_eth]})
    coletor, _ = coletor_com(ws, estado=estado, mensagens_sem_bid_ask=3)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH, BTC]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais() and ws.vazia())
        for _ in range(5):
            await asyncio.sleep(0)
        assert ws.canais() == ["market_stats/all"]          # 5 mensagens, mas de um par so: nada muda
        ws.entregar(msg_stats(stats_sem_bid_ask(1)))         # o segundo par tambem sem bid/ask: troca
        await esperar_ate(lambda: len(ws.canais()) == 3)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


def test_etapa_1_pelos_livros_respeita_o_limite_de_assinaturas_pelo_volume():
    estado = EstadoMercados()
    estatisticas = [msg_stats(stats_sem_bid_ask(0, volume=9e8), stats_sem_bid_ask(1, volume=7e5)) for _ in range(3)]
    ws = WsLighterFalso(respostas={"market_stats/all": [estatisticas]})
    coletor, _ = coletor_com(ws, estado=estado, mensagens_sem_bid_ask=3, limite_assinaturas=1)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH, BTC]))
        await esperar_ate(lambda: len(ws.canais()) == 2)
        await asyncio.sleep(0)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert ws.canais() == ["market_stats/all", "order_book/0"]   # so o ETH, o de maior volume


def test_bid_ask_que_volta_no_market_stats_devolve_a_etapa_1_ao_modo_normal(caplog):
    estado = EstadoMercados()
    estatisticas = [msg_stats(stats_sem_bid_ask(0), stats_sem_bid_ask(1)) for _ in range(3)]
    ws = WsLighterFalso(respostas={"market_stats/all": [estatisticas], "order_book/0": [[completo()]],
                                   "order_book/1": [[completo(market_id=1, bids=[(64000.0, 1.0)],
                                                              asks=[(64001.0, 1.0)])]]})
    # tempo_min_modo_s=0: aqui so a histerese conta (o tempo minimo em cada modo tem teste proprio).
    coletor, _ = coletor_com(ws, estado=estado, mensagens_sem_bid_ask=3, tempo_min_modo_s=0)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH, BTC]))
        await esperar_ate(lambda: livro_no_estado(estado) is not None and livro_no_estado(estado, "BTC") is not None)
        await coletor.assinar_livros({BTC})                  # a etapa 2 quer o livro do BTC
        com_bid_ask = msg_stats(stats(0, bid="2070.0", ask="2070.5"), stats(1, bid="64100", ask="64101"))
        ws.entregar(com_bid_ask, com_bid_ask)
        await esperar_ate(ws.vazia)
        for _ in range(5):
            await asyncio.sleep(0)
        assert ws.canais("unsubscribe") == []                # 2 mensagens com bid/ask ainda nao bastam (N = 3)
        ws.entregar(com_bid_ask)
        await esperar_ate(lambda: ws.canais("unsubscribe") == ["order_book/0"])
        # O livro do ETH (so etapa 1) saiu; o do BTC (etapa 2) ficou; os tickers voltaram a vir do market_stats.
        assert livro_no_estado(estado) is None and livro_no_estado(estado, "BTC") is not None
        assert estado.ticker_guardado("lighter", "futuro", "ETH")["bid"] == 2070.0
        assert estado.ticker_guardado("lighter", "futuro", "BTC")["bid"] == 64100.0
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.INFO, logger="scanner"):
        asyncio.run(cenario())
    assert any("voltou a trazer bid/ask" in m for m in avisos(caplog, logging.INFO))


def test_etapa_1_pelos_livros_continua_depois_de_reconectar():
    estado = EstadoMercados()
    estatisticas = [msg_stats(stats_sem_bid_ask(0)) for _ in range(3)]
    respostas = {"market_stats/all": [estatisticas], "order_book/0": [[completo()]]}
    ws1, ws2 = WsLighterFalso(respostas), WsLighterFalso({"order_book/0": [[completo()]]})
    coletor, _ = coletor_com([ws1, ws2], estado=estado, mensagens_sem_bid_ask=3, esperar=EsperaFalsa())

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: estado.obter_ticker("lighter", "futuro", "ETH", AGORA) is not None)
        ws1.entregar(ConnectionResetError("caiu"))
        await esperar_ate(lambda: estado.obter_ticker("lighter", "futuro", "ETH", AGORA) is not None
                          and "order_book/0" in ws2.canais())
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert ws2.canais() == ["market_stats/all", "order_book/0"]  # ja assina o livro, sem esperar 3 mensagens


def test_etapa_1_pelos_livros_segue_a_lista_nova_de_tickers():
    # O __main__ reinicia os tickers quando a lista de pares muda: com a etapa 1 pelos livros, a conexao nova
    # ja assina o livro de cada par da lista NOVA.
    estado = EstadoMercados()
    ws1 = WsLighterFalso({"market_stats/all": [[msg_stats(stats_sem_bid_ask(0)) for _ in range(3)]],
                          "order_book/0": [[completo()]]})
    ws2 = WsLighterFalso()
    coletor, _ = coletor_com([ws1, ws2], estado=estado, mensagens_sem_bid_ask=3, esperar=EsperaFalsa())

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "order_book/0" in ws1.canais())
        await parar(tarefa)                                    # como o _Tickers.parar do __main__
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH, BTC]))
        await esperar_ate(lambda: len(ws2.canais()) == 3)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert ws2.canais()[0] == "market_stats/all" and sorted(ws2.canais()[1:]) == ["order_book/0", "order_book/1"]



# ---------------------------------------------------------------------------
# Correcao pos-revisao 2 (Task 16)
# ---------------------------------------------------------------------------


def intermitente(vezes=5, btc_tambem=False):
    """A sequencia do revisor: 3 market_stats sem bid/ask e 1 com (so o ETH, ou os dois pares), repetido."""
    btc = stats(1, bid="64000", ask="64001") if btc_tambem else stats_sem_bid_ask(1)
    seq = []
    for _ in range(vezes):
        seq += [msg_stats(stats_sem_bid_ask(0), stats_sem_bid_ask(1))] * 3
        seq += [msg_stats(stats(0, bid="2064.5", ask="2064.6"), btc)]
    return seq


@pytest.mark.parametrize("btc_tambem", [False, True], ids=["com_so_o_eth", "com_os_dois_pares"])
def test_bid_ask_intermitente_troca_no_maximo_uma_vez_sem_tempestade_de_assinaturas(caplog, btc_tambem):
    # O relogio anda 100 s por mensagem: o tempo minimo em cada modo passa, e so a histerese segura a volta.
    agora = [AGORA]

    def relogio_andando():
        agora[0] += 100
        return agora[0]

    respostas = {"market_stats/all": [intermitente(btc_tambem=btc_tambem)], "order_book/0": [[completo()]],
                 "order_book/1": [[completo(market_id=1, bids=[(64000.0, 1.0)], asks=[(64001.0, 1.0)])]]}
    ws = WsLighterFalso(respostas)
    # (O livro completo chega depois das 20 mensagens da fila; com o relogio correndo, o tempo maximo do livro
    # completo fica fora do caminho deste teste.)
    coletor, _ = coletor_com(ws, estado=EstadoMercados(), relogio=relogio_andando, mensagens_sem_bid_ask=3,
                             tempo_max_livro_completo_s=1e12)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH, BTC]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais() and ws.vazia())
        for _ in range(20):
            await asyncio.sleep(0)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.INFO, logger="scanner"):
        asyncio.run(cenario())
    livros = [c for c in ws.canais() if c.startswith("order_book")]
    assert sorted(livros) == ["order_book/0", "order_book/1"]   # uma troca so: um subscribe por par
    assert ws.canais("unsubscribe") == []
    mensagens = avisos(caplog, logging.INFO)
    assert len([m for m in mensagens if "passa a vir dos livros" in m]) == 1
    assert not any("voltou a trazer bid/ask" in m for m in mensagens)


def test_tempo_minimo_em_cada_modo_antes_de_voltar_ao_market_stats():
    agora = [AGORA]
    estado = EstadoMercados()
    sem = [msg_stats(stats_sem_bid_ask(0), stats_sem_bid_ask(1)) for _ in range(3)]
    ws = WsLighterFalso(respostas={"market_stats/all": [sem], "order_book/0": [[completo()]],
                                   "order_book/1": [[completo(market_id=1, bids=[(64000.0, 1.0)],
                                                              asks=[(64001.0, 1.0)])]]})
    coletor, _ = coletor_com(ws, estado=estado, relogio=lambda: agora[0], mensagens_sem_bid_ask=3,
                             tempo_min_modo_s=300)
    com_bid_ask = msg_stats(stats(0, bid="2070.0", ask="2070.5"), stats(1, bid="64100", ask="64101"))

    async def entregar_e_esperar(*mensagens):
        ws.entregar(*mensagens)
        await esperar_ate(ws.vazia)
        for _ in range(5):
            await asyncio.sleep(0)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH, BTC]))
        await esperar_ate(lambda: len(ws.canais()) == 3)          # trocou para os livros em AGORA
        agora[0] = AGORA + 200
        await entregar_e_esperar(com_bid_ask, com_bid_ask, com_bid_ask, com_bid_ask)
        assert ws.canais("unsubscribe") == []                     # 200 s: ainda dentro do tempo minimo
        agora[0] = AGORA + 301
        await entregar_e_esperar(com_bid_ask, com_bid_ask, com_bid_ask)
        assert sorted(ws.canais("unsubscribe")) == ["order_book/0", "order_book/1"]
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


def test_corrida_entre_cancelar_e_assinar_o_mesmo_livro_termina_assinado():
    # Dois ajustes de livros ao mesmo tempo (o laco de leitura e o assinar_livros do __main__): o cancelamento
    # do primeiro demora; o segundo assina o mesmo livro de novo. Com a trava, o ultimo pedido e o subscribe.
    class WsLento(WsLighterFalso):
        def __init__(self, respostas):
            super().__init__(respostas)
            self.portao = asyncio.Event()

        async def enviar(self, dados):
            if dados.get("type") == "unsubscribe":
                await self.portao.wait()
            await super().enviar(dados)

    estado = EstadoMercados()
    ws = WsLento({"order_book/0": [[completo()], [completo(nonce=200)]]})
    coletor, _ = coletor_com(ws, estado=estado)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        cancelar = asyncio.create_task(coletor.assinar_livros(set()))
        await asyncio.sleep(0)
        assinar = asyncio.create_task(coletor.assinar_livros({ETH}))
        for _ in range(5):
            await asyncio.sleep(0)
        ws.portao.set()
        await asyncio.gather(cancelar, assinar)
        await esperar_ate(lambda: livro_no_estado(estado) is not None)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    tipos = [d["type"] for d in ws.enviadas if d.get("channel") == "order_book/0"]
    assert tipos == ["subscribe", "unsubscribe", "subscribe"]


def test_nao_assinado_em_resposta_ao_nosso_cancelamento_nao_avisa_nem_assina_de_novo(caplog):
    # Pedido de novo pelo tempo maximo: o coletor cancela e assina. Se a Lighter responder 30002 ao NOSSO
    # cancelamento (o livro nao estava assinado), isso e esperado: nem aviso, nem subscribe a mais.
    agora = [AGORA]
    estado = EstadoMercados()
    ws = WsLighterFalso(respostas={"order_book/0": [[], []]})   # o livro completo o teste entrega na hora certa
    coletor, _ = coletor_com(ws, estado=estado, relogio=lambda: agora[0], esperar=EsperaFalsa(),
                             tempo_max_livro_completo_s=10)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await coletor.assinar_livros({ETH})
        agora[0] += 11
        ws.entregar({"type": "pong"})                                    # pede de novo (cancela e assina)
        await esperar_ate(lambda: ws.canais().count("order_book/0") == 2)
        # A Lighter responde ao cancelamento (30002: nao estava assinado) e depois manda o livro completo.
        ws.entregar({"error": {"code": 30002, "message": "Not Subscribed to : order_book:0"}}, completo(),
                    {"type": "pong"})
        await esperar_ate(lambda: livro_no_estado(estado, ) is not None
                          and estado.obter_livro("lighter", "futuro", "ETH", agora[0]) is not None and ws.vazia())
        for _ in range(5):
            await asyncio.sleep(0)
        await parar(tarefa)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(cenario())
    tipos = [d["type"] for d in ws.enviadas if d.get("channel") == "order_book/0"]
    assert tipos == ["subscribe", "unsubscribe", "subscribe"]
    assert not any("nao estava assinado" in m for m in avisos(caplog))


def test_livros_atrasados_conferidos_no_maximo_uma_vez_por_segundo():
    agora = [AGORA]
    ws = WsLighterFalso()
    coletor, _ = coletor_com(ws, estado=EstadoMercados(), relogio=lambda: agora[0])
    conferencias = []
    coletor._conferir_livros_atrasados = conferencias.append

    async def entregar(*mensagens):
        ws.entregar(*mensagens)
        await esperar_ate(ws.vazia)
        for _ in range(5):
            await asyncio.sleep(0)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH]))
        await esperar_ate(lambda: "market_stats/all" in ws.canais())
        await entregar(*[{"type": "pong"}] * 20)
        assert conferencias == [AGORA]                     # 20 mensagens no mesmo segundo: uma conferencia
        agora[0] += 0.5
        await entregar({"type": "pong"})
        assert conferencias == [AGORA]
        agora[0] += 0.5
        await entregar({"type": "pong"})
        assert conferencias == [AGORA, AGORA + 1]
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())


def test_tempo_minimo_do_modo_da_etapa_1_vem_da_config():
    c = Config()
    assert c.tempo_min_modo_etapa1_lighter_s == 300
    cfg = cfg_com(corretoras=["lighter"], tempo_min_modo_etapa1_lighter_s=120)
    coletor = criar_coletor_lighter("lighter", ClienteLighterFalso(MARKETS), cfg, EstadoMercados(), None, relogio)
    assert coletor._tempo_min_modo_s == 120


def test_anotacoes_do_imprimir_precos_usam_o_protocol():
    import typing

    from scanner.coletores.base import ColetorDoScanner
    from scanner.ferramentas import imprimir_precos

    assert typing.get_type_hints(imprimir_precos.simbolos_da_corretora)["coletor"] is ColetorDoScanner
    assert typing.get_type_hints(imprimir_precos.preparar)["return"] == tuple[
        list[ColetorDoScanner], dict[str, list[str]], list[str]]


def test_regra_do_erro_de_conexao_do_ccxt_num_lugar_so():
    from scanner.coletores import ccxt_coletor

    assert ccxt_coletor.ColetorCcxt._ERROS_DE_CONEXAO is ccxt_coletor.ERROS_DE_CONEXAO_CCXT
    assert ccxt_coletor._erro_de_conexao(OSError()) and not ccxt_coletor._erro_de_conexao(ValueError())


def test_market_stats_parcial_nos_livros_nao_volta_com_um_par_so():
    # Tudo ou nada tambem na volta: com so o ETH trazendo bid/ask (o BTC nunca), a etapa 1 fica nos livros
    # (senao o BTC ficaria sem ticker).
    sem = [msg_stats(stats_sem_bid_ask(0), stats_sem_bid_ask(1)) for _ in range(3)]
    so_eth = [msg_stats(stats(0, bid="2070.0", ask="2070.5"), stats_sem_bid_ask(1)) for _ in range(6)]
    ws = WsLighterFalso(respostas={"market_stats/all": [sem + so_eth]})
    coletor, _ = coletor_com(ws, estado=EstadoMercados(), mensagens_sem_bid_ask=3, tempo_min_modo_s=0)

    async def cenario():
        tarefa = asyncio.create_task(coletor.manter_tickers([ETH, BTC]))
        await esperar_ate(lambda: len(ws.canais()) == 3 and ws.vazia())
        for _ in range(10):
            await asyncio.sleep(0)
        await parar(tarefa)
        await coletor.fechar()

    asyncio.run(cenario())
    assert ws.canais("unsubscribe") == []
