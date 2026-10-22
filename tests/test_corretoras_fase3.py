"""Testes da Task 16: fase 3 de corretoras (Kraken Pro e Lighter) montadas no scanner.

Cobre:
  - Kraken Pro como duas corretoras do ccxt: `kraken` (so spot) e `krakenfutures` (so futuros, em USD), com a
    cotacao do USD pelo par spot USDT/USD da propria Kraken;
  - o config.yaml de exemplo da fase 3 e os campos novos da Config (enderecos e ping da Lighter);
  - o __main__ montando a Lighter pelo conector proprio (fora do laco de REST do DadosLentos);
  - ponta a ponta no modo Farm: um sinal com ponta na Lighter, com custo a cada $1.000;
  - ponta a ponta spot x futuro entre `kraken` e `krakenfutures`;
  - imprimir_precos com a Lighter.

Nenhum teste acessa a internet: corretoras falsas (as do ccxt imitadas como no teste da fase 2, a Lighter
pelas falsas de tests/fabricas.py).
"""

import asyncio
import json
import logging
from pathlib import Path

import pytest
import yaml

import scanner.__main__ as principal
from scanner.armazenamento import Banco
from scanner.coletores import fabrica
from scanner.coletores.ccxt_coletor import ColetorCcxt
from scanner.coletores.dados_lentos import DadosLentos
from scanner.coletores.lighter import ClienteLighter, ColetorLighter, markets_de_lighter
from scanner.config.carregar import Config, ErroConfig, carregar_config
from scanner.estado import EstadoMercados
from scanner.ferramentas import imprimir_precos as ferramenta
from scanner.motor import Sessao
from tests.fabricas import ClienteLighterFalso, WsLighterFalso, cfg_com, esperar_ate, market_ccxt
from tests.test_corretoras_fase2 import CorretoraFalsa, ob, tk

RAIZ = Path(__file__).resolve().parent.parent
DADOS = Path(__file__).resolve().parent / "dados"
AGORA = 1000.0


def relogio():
    return AGORA


def carregar(nome: str) -> dict:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


def escrever(tmp_path, texto: str) -> str:
    arquivo = tmp_path / "config.yaml"
    arquivo.write_text(texto, encoding="utf-8")
    return str(arquivo)


# Markets no formato que o ccxt 4.5.89 entrega: saida do fetch_markets de verdade do ccxt (kraken e
# krakenfutures) rodado com respostas de exemplo, sem rede (scratchpad/t16/kraken_markets.py). So os campos
# que o scanner le. Kraken spot: XBT vira BTC, a taxa e a 1a faixa da tabela do par (0,40% -> 0.004).
def _kraken_spot(simbolo, base, quote, ident):
    return {"id": ident, "symbol": simbolo, "base": base, "quote": quote, "settle": None, "type": "spot",
            "spot": True, "swap": False, "future": False, "option": False, "linear": None, "inverse": None,
            "contractSize": None, "taker": 0.004, "active": True}


KRAKEN_MARKETS = [
    _kraken_spot("BTC/USD", "BTC", "USD", "XXBTZUSD"), _kraken_spot("BTC/USDT", "BTC", "USDT", "XBTUSDT"),
    _kraken_spot("BTC/EUR", "BTC", "EUR", "XXBTZEUR"), _kraken_spot("ADA/USD", "ADA", "USD", "ADAUSD"),
    _kraken_spot("ADA/EUR", "ADA", "EUR", "ADAEUR"), _kraken_spot("USDT/USD", "USDT", "USD", "USDTZUSD"),
    _kraken_spot("USDT/EUR", "USDT", "EUR", "USDTEUR"),
]


def _kraken_futuro(simbolo, base, ident, settle="USD", tipo="swap", inverso=False):
    return {"id": ident, "symbol": simbolo, "base": base, "quote": "USD", "settle": settle, "type": tipo,
            "spot": False, "swap": tipo == "swap", "future": tipo == "future", "option": False,
            "linear": not inverso, "inverse": inverso, "contractSize": 1.0, "taker": 0.0005, "active": True}


# krakenfutures: PF_ = perpetuo linear em USD; PI_ = perpetuo inverso (liquida em BTC); FF_ = com vencimento;
# in_ = indice (nao negocia).
KRAKENFUTURES_MARKETS = [
    _kraken_futuro("BTC/USD:USD", "BTC", "PF_XBTUSD"), _kraken_futuro("ETH/USD:USD", "ETH", "PF_ETHUSD"),
    _kraken_futuro("BTC/USD:BTC", "BTC", "PI_XBTUSD", settle="BTC", inverso=True),
    _kraken_futuro("BTC/USD:USD-261225", "BTC", "FF_XBTUSD_261225", tipo="future"),
    {"id": "in_xbtusd", "symbol": "in_xbtusd", "base": "BTC", "quote": "USD", "settle": None, "type": "index",
     "spot": False, "swap": False, "future": False, "option": False, "linear": None, "inverse": None,
     "contractSize": None, "taker": 0.0005, "active": False},
]

MEXC_BTC = market_ccxt("BTC/USDT:USDT", "BTC", taker=0.0002)
BITGET_USDC_USDT = market_ccxt("USDC/USDT", "USDC", taker=0.001)
LIGHTER_MARKETS, _ = markets_de_lighter(carregar("lighter_order_book_details.json"), "USDC")


# ---------------------------------------------------------------------------
# Kraken Pro: kraken (spot) e krakenfutures (futuros em USD)
# ---------------------------------------------------------------------------


def test_kraken_so_spot_e_krakenfutures_so_futuro_linear():
    estado = EstadoMercados()
    kraken = ColetorCcxt("kraken", CorretoraFalsa(KRAKEN_MARKETS), estado=estado, relogio=relogio, mercados=("spot",))
    futuros = ColetorCcxt("krakenfutures", CorretoraFalsa(KRAKENFUTURES_MARKETS), estado=estado, relogio=relogio,
                          mercados=("futuro",))
    # Spot: o USDT vence; sem USDT, o USD vence o EUR (o USD tem cotacao pelo USDT/USD; o EUR nao).
    assert kraken.simbolos_por_chave() == {("spot", "BTC"): "BTC/USDT", ("spot", "ADA"): "ADA/USD",
                                           ("spot", "USDT"): "USDT/USD"}
    # Futuros: so os perpetuos lineares (inverso, com vencimento e indice ficam de fora).
    assert futuros.simbolos_por_chave() == {("futuro", "BTC"): "BTC/USD:USD", ("futuro", "ETH"): "ETH/USD:USD"}


def test_cotacao_do_usd_vem_do_usdt_usd_da_kraken():
    estado = EstadoMercados()
    kraken_falsa = CorretoraFalsa(KRAKEN_MARKETS)
    coletores = {
        "kraken": ColetorCcxt("kraken", kraken_falsa, estado=estado, relogio=relogio, mercados=("spot",)),
        "krakenfutures": ColetorCcxt("krakenfutures", CorretoraFalsa(KRAKENFUTURES_MARKETS), estado=estado,
                                     relogio=relogio, mercados=("futuro",)),
    }
    dados = DadosLentos(estado=estado, fontes_cotacao={"USD": "kraken"}, relogio=relogio)
    listas, _ = principal._listas_de_tickers(coletores, dados)
    # Spot x futuro entre as duas (BTC) e o par de cotacao do USD na lista do spot da Kraken.
    assert listas == {"kraken": ["BTC/USDT", "USDT/USD"], "krakenfutures": ["BTC/USD:USD"]}

    async def cenario():
        kraken_falsa.entregar(("watch_tickers", "spot"), {"BTC/USDT": tk(99.9, 100.0), "USDT/USD": tk(1.0004, 1.0006)})
        tarefa = asyncio.create_task(coletores["kraken"].manter_tickers(listas["kraken"]))
        await esperar_ate(lambda: dados.cotacoes() != {})
        tarefa.cancel()
        await asyncio.gather(tarefa, return_exceptions=True)

    asyncio.run(cenario())
    assert dados.cotacoes() == {"USD": pytest.approx(1 / 1.0005)}   # 1 USD = 1/1,0005 USDT


def test_taxa_e_funding_da_krakenfutures_pelo_ccxt():
    # ccxt/krakenfutures.py: taxa da tabela fixa (0,05% na 1a faixa) e funding com interval "1h".
    item = {"symbol": "BTC/USD:USD", "fundingRate": 0.0000125, "interval": "1h"}
    futuros = CorretoraFalsa(KRAKENFUTURES_MARKETS, funding={"BTC/USD:USD": item})
    dados = DadosLentos()
    asyncio.run(dados.atualizar({"krakenfutures": futuros}))
    assert dados.taxa_taker("krakenfutures", "futuro") == pytest.approx(0.05)
    assert dados.funding("krakenfutures", "BTC") == (pytest.approx(0.00125), 1)
    spot = CorretoraFalsa(KRAKEN_MARKETS, has={"watchTickers": True})
    asyncio.run(dados.atualizar({"kraken": spot}))
    assert dados.taxa_taker("kraken", "spot") == pytest.approx(0.4)


def test_sinal_spot_kraken_contra_futuro_krakenfutures_com_cotacao_usd(tmp_path):
    kraken = CorretoraFalsa(KRAKEN_MARKETS, has={"watchTickers": True})
    futuros = CorretoraFalsa(KRAKENFUTURES_MARKETS, funding={
        "BTC/USD:USD": {"symbol": "BTC/USD:USD", "fundingRate": 0.0000125, "interval": "1h"}})
    # Spot: compra a 100,00 USDT. Futuro: vende a 100,60 USD; 1 USD = 1/1,0005 USDT.
    kraken.entregar(("watch_tickers", "spot"),
                    *[{"BTC/USDT": tk(99.9, 100.0), "USDT/USD": tk(1.0004, 1.0006)} for _ in range(50)])
    kraken.entregar(("livro", "BTC/USDT"), ob(99.9, 100.0))
    futuros.entregar(("watch_tickers", "futuro"), *[{"BTC/USD:USD": tk(100.6, 100.7)} for _ in range(50)])
    futuros.entregar(("livro", "BTC/USD:USD"), ob(100.6, 100.7))
    exchanges = {"kraken": kraken, "krakenfutures": futuros}
    config = cfg_com(corretoras=["kraken", "krakenfutures"], banco=str(tmp_path / "s.db"), ciclo_ms=50,
                     rp_min_lucro_pct=-5.0, mercados_por_corretora={"kraken": ("spot",), "krakenfutures": ("futuro",)},
                     fontes_cotacao={"USD": "kraken"}, pasta_logs=str(tmp_path / "logs"))
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=10), timeout=5))

    banco = Banco(config.banco)
    sinais = [r["sinal"] for r in banco.sinais_do_dia("1970-01-01")]
    banco.fechar()
    assert len(sinais) == 1
    s = sinais[0]
    assert (s["tipo"], s["par"], s["aviso_cotacao"]) == ("spot_futuro", "BTC/USDT", False)
    assert (s["ponta_long"]["corretora"], s["ponta_long"]["simbolo_original"]) == ("kraken", "BTC/USDT")
    assert (s["ponta_short"]["corretora"], s["ponta_short"]["simbolo_original"]) == ("krakenfutures", "BTC/USD:USD")
    assert s["ponta_short"]["preco"] == pytest.approx(100.6 / 1.0005)
    assert s["ponta_short"]["funding_intervalo_h"] == 1 and s["ponta_short"]["taxa_taker_pct"] == pytest.approx(0.05)
    assert s["ponta_long"]["taxa_taker_pct"] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Ponta a ponta no modo Farm: um sinal com ponta na Lighter
# ---------------------------------------------------------------------------


def lighter_falsa_btc(bid: float, ask: float, funding: str = "0.0010", markets=None) -> ClienteLighterFalso:
    """Lighter falsa com o BTC (market_id 1): market_stats ao assinar e o livro completo ao assinar o livro."""
    estatisticas = {"channel": "market_stats:all", "type": "update/market_stats", "market_stats": {
        "1": {"market_id": 1, "symbol": "BTC", "best_bid_price": str(bid), "best_ask_price": str(ask),
              "current_funding_rate": funding, "daily_quote_token_volume": 5e8, "daily_price_change": 0.5}}}
    livro = {"channel": "order_book:1", "type": "subscribed/order_book", "timestamp": None, "order_book": {
        "code": 0, "nonce": 7, "bids": [{"price": str(bid), "size": "10"}],
        "asks": [{"price": str(ask), "size": "10"}]}}
    ws = WsLighterFalso(respostas={"market_stats/all": [[estatisticas]], "order_book/1": [[livro]]})
    return ClienteLighterFalso(LIGHTER_MARKETS if markets is None else markets, [ws])


def test_modo_farm_mostra_sinal_com_ponta_na_lighter_e_custo_a_cada_1000(tmp_path, monkeypatch):
    criar_app_original = principal.criar_app

    def criar_app_no_farm(motor, *args, **kwargs):  # o painel escolheria o modo Farm (PUT /api/sessao)
        motor.sessao = Sessao(modo="farm")
        return criar_app_original(motor, *args, **kwargs)

    monkeypatch.setattr(principal, "criar_app", criar_app_no_farm)
    # Lighter vende a 100,50 USDC (1 USDC = 1,0 USDT); a MEXC compra a 100,00 USDT: A = 0,5%, F ~ -0,52%.
    lighter = lighter_falsa_btc(100.5, 100.51)
    mexc = CorretoraFalsa([MEXC_BTC])
    mexc.entregar(("watch_tickers", "futuro"), *[{"BTC/USDT:USDT": tk(99.99, 100.0)} for _ in range(50)])
    mexc.entregar(("livro", "BTC/USDT:USDT"), ob(99.99, 100.0))
    bitget = CorretoraFalsa([BITGET_USDC_USDT])
    bitget.entregar(("watch_tickers", "spot"), *[{"USDC/USDT": tk(0.9999, 1.0001)} for _ in range(50)])
    exchanges = {"lighter": lighter, "mexc": mexc, "bitget": bitget}
    config = cfg_com(corretoras=["mexc", "bitget", "lighter"], corretoras_farm=["lighter"],
                     banco=str(tmp_path / "s.db"), ciclo_ms=50, pasta_logs=str(tmp_path / "logs"))
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=10), timeout=5))

    banco = Banco(config.banco)
    sinais = [r["sinal"] for r in banco.sinais_do_dia("1970-01-01")]
    banco.fechar()
    com_lighter = [s for s in sinais if "lighter" in (s["ponta_long"]["corretora"], s["ponta_short"]["corretora"])]
    assert com_lighter, sinais
    s = next(x for x in com_lighter if x["ponta_short"]["corretora"] == "lighter")
    assert (s["modo"], s["tipo"], s["par"], s["aviso_cotacao"]) == ("farm", "futuro_futuro", "BTC/USDT", False)
    assert s["ponta_short"]["simbolo_original"] == "BTC/USDC:USDC"
    assert s["ponta_short"]["preco"] == pytest.approx(100.5)
    assert s["ponta_short"]["funding_pct"] == pytest.approx(0.0010) and s["ponta_short"]["funding_intervalo_h"] == 1
    assert s["ponta_short"]["taxa_taker_pct"] == 0.0 and s["ponta_long"]["taxa_taker_pct"] == pytest.approx(0.02)
    assert s["custo_por_1000_usd"] is not None
    assert s["custo_por_1000_usd"] == pytest.approx(-s["rp_liquido_pct"] * 5)
    assert s["rp_liquido_pct"] >= -0.1
    assert lighter.conectou == 1


def test_lighter_robinhood_cota_em_usdg_e_sem_cotacao_usa_1_para_1_com_aviso(tmp_path, caplog):
    # Nenhuma corretora ligada tem o par USDG/USDT (a fonte padrao, bitget, nem esta ligada): 1:1 com aviso.
    markets_rh, _ = markets_de_lighter(carregar("lighter_order_book_details.json"), "USDG")
    rh = lighter_falsa_btc(100.5, 100.51, markets=markets_rh)
    mexc = CorretoraFalsa([MEXC_BTC])
    mexc.entregar(("watch_tickers", "futuro"), *[{"BTC/USDT:USDT": tk(99.99, 100.0)} for _ in range(50)])
    mexc.entregar(("livro", "BTC/USDT:USDT"), ob(99.99, 100.0))
    exchanges = {"lighter_rh": rh, "mexc": mexc}
    config = cfg_com(corretoras=["mexc", "lighter_rh"], banco=str(tmp_path / "s.db"), ciclo_ms=50,
                     rp_min_lucro_pct=-5.0, pasta_logs=str(tmp_path / "logs"))
    with caplog.at_level(logging.WARNING, logger="scanner"):
        asyncio.run(asyncio.wait_for(
            principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=10), timeout=5))
    banco = Banco(config.banco)
    sinais = [r["sinal"] for r in banco.sinais_do_dia("1970-01-01")]
    banco.fechar()
    s = next(x for x in sinais if x["ponta_short"]["corretora"] == "lighter_rh")
    assert s["ponta_short"]["simbolo_original"] == "BTC/USDG:USDG"
    assert s["aviso_cotacao"] is True and s["ponta_short"]["preco"] == pytest.approx(100.5)
    assert any("sem cotacao de USDG" in r.getMessage() for r in caplog.records)


def test_rodar_monta_a_lighter_pelo_conector_proprio_e_fora_do_rest_do_dados_lentos(tmp_path, monkeypatch):
    clientes = {}

    def cliente_falso(nome, cfg):
        clientes[nome] = lighter_falsa_btc(100.0, 100.1)
        return clientes[nome]

    monkeypatch.setattr(fabrica, "criar_cliente_lighter", cliente_falso)
    monkeypatch.setattr(fabrica, "criar_exchange_ccxt", lambda nome: CorretoraFalsa([MEXC_BTC]))
    rest = []
    rodar_original = DadosLentos.rodar

    async def rodar_anotando(self, exchanges, *args, **kwargs):
        rest.append(sorted(exchanges))
        return await rodar_original(self, exchanges, *args, **kwargs)

    monkeypatch.setattr(DadosLentos, "rodar", rodar_anotando)
    criados = []
    init_original = ColetorLighter.__init__

    def init_anotando(self, *args, **kwargs):
        init_original(self, *args, **kwargs)
        criados.append(self)

    monkeypatch.setattr(ColetorLighter, "__init__", init_anotando)
    config = cfg_com(corretoras=["mexc", "lighter"], banco=str(tmp_path / "s.db"), ciclo_ms=50,
                     pasta_logs=str(tmp_path / "logs"), intervalo_ping_lighter_s=45)
    asyncio.run(asyncio.wait_for(principal.rodar(config, relogio=relogio, ciclos=2), timeout=5))
    assert list(clientes) == ["lighter"]
    assert [c.nome for c in criados] == ["lighter"] and criados[0]._intervalo_ping_s == 45
    assert rest == [["mexc"]]           # a Lighter entrega funding e taxa pelo proprio coletor
    assert clientes["lighter"].fechado  # fechada no fim


def test_criar_corretora_escolhe_o_conector():
    assert isinstance(fabrica.criar_corretora("lighter", Config()), ClienteLighter)
    assert fabrica.criar_corretora("lighter_rh", Config()).cotacao == "USDG"
    kraken = fabrica.criar_corretora("kraken", Config())
    assert type(kraken).__name__ == "kraken"
    with pytest.raises(ValueError, match="nao existe no ccxt"):
        fabrica.criar_corretora("krakn", Config())


def test_lighter_que_entra_depois_nao_liga_o_rest_do_dados_lentos(tmp_path, monkeypatch):
    # A Lighter que nao carregou ao ligar volta pelo _trazer_de_volta; o ao_entrar dela nao liga o laco REST
    # do DadosLentos (o funding e a taxa dela chegam pelo proprio coletor).
    acompanhadas = []

    async def acompanhar(self, corretora, exchange, intervalo_s, esperar=asyncio.sleep):
        acompanhadas.append(corretora)

    monkeypatch.setattr(DadosLentos, "acompanhar", acompanhar)
    voltaram = []

    async def trazer_de_volta(nome, exchange, coletores, tickers, estado, criar_coletor, ao_entrar, *args, **kwargs):
        ao_entrar(nome)
        voltaram.append(nome)

    monkeypatch.setattr(principal, "_trazer_de_volta", trazer_de_volta)
    lighter = ClienteLighterFalso(LIGHTER_MARKETS)

    async def nao_carrega(reload=False, params=None):
        raise OSError("sem internet")

    lighter.load_markets = nao_carrega
    exchanges = {"lighter": lighter, "mexc": CorretoraFalsa([MEXC_BTC])}
    config = cfg_com(corretoras=["mexc", "lighter"], banco=str(tmp_path / "s.db"), ciclo_ms=50,
                     pasta_logs=str(tmp_path / "logs"))
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=relogio, ciclos=2), timeout=5))
    assert voltaram == ["lighter"]
    assert "lighter" not in acompanhadas and "mexc" in acompanhadas


# ---------------------------------------------------------------------------
# config.yaml de exemplo e campos novos da Config
# ---------------------------------------------------------------------------


def test_config_yaml_do_projeto_traz_a_fase_3():
    caminho = RAIZ / "config.yaml"
    c = carregar_config(str(caminho))
    assert "kraken" in c.corretoras and "lighter" in c.corretoras
    assert "krakenfutures" not in c.corretoras and "lighter_rh" not in c.corretoras  # confirmar antes (spec 11)
    assert c.corretoras_farm == ["lighter", "lighter_rh", "kraken", "krakenfutures"]
    assert c.mercados_da_corretora("kraken") == ("spot",)
    assert c.fontes_cotacao == {"USD": "kraken"}
    assert c.intervalo_funding_h.get("lighter") == 1
    texto = caminho.read_text(encoding="utf-8")
    for linha in ("  # - krakenfutures", "  # - lighter_rh", "  # krakenfutures: [futuro]"):
        assert linha in texto, linha
    assert "confirme antes de ativar" in texto and "secao 11" in texto
    # As taxas publicas padrao de cada uma ficam nos comentarios, para o dono conferir com o nivel dele.
    for nome in ("kraken:", "krakenfutures:", "lighter:", "lighter_rh:"):
        assert f"#   {nome}" in texto, nome
    chaves = yaml.safe_load(texto) or {}
    for campo in ("url_rest_lighter", "url_ws_lighter", "url_rest_lighter_rh", "url_ws_lighter_rh",
                  "intervalo_ping_lighter_s"):
        assert campo in chaves and chaves[campo] == getattr(Config(), campo), campo


def test_config_yaml_com_as_linhas_comentadas_ativadas_continua_valido(tmp_path):
    # O dono so precisa tirar o "#" das linhas da krakenfutures e da lighter_rh.
    texto = (RAIZ / "config.yaml").read_text(encoding="utf-8")
    for linha in ("  # - krakenfutures", "  # - lighter_rh", "  # krakenfutures: [futuro]",
                  "  # krakenfutures: 1", "  # lighter_rh: 1"):
        assert texto.count(linha) == 1, linha
        texto = texto.replace(linha, linha.replace("# ", "", 1))
    c = carregar_config(escrever(tmp_path, texto))
    assert {"krakenfutures", "lighter_rh"} <= set(c.corretoras)
    assert c.mercados_da_corretora("krakenfutures") == ("futuro",)
    assert c.intervalo_funding_h == {"hyperliquid": 1, "lighter": 1, "krakenfutures": 1, "lighter_rh": 1}


def test_campos_da_lighter_lidos_do_yaml(tmp_path):
    c = carregar_config(escrever(tmp_path, "url_rest_lighter: https://outro.exemplo\n"
                                           "url_ws_lighter_rh: wss://rh.exemplo/stream\n"
                                           "intervalo_ping_lighter_s: 15\n"))
    assert (c.url_rest_lighter, c.url_ws_lighter_rh, c.intervalo_ping_lighter_s) == (
        "https://outro.exemplo", "wss://rh.exemplo/stream", 15)


@pytest.mark.parametrize("texto, campo", [
    ("url_rest_lighter: ''\n", "url_rest_lighter"),
    ("url_rest_lighter: wss://errado\n", "url_rest_lighter"),
    ("url_ws_lighter: https://errado\n", "url_ws_lighter"),
    ("url_ws_lighter_rh: 5\n", "url_ws_lighter_rh"),
    ("url_rest_lighter_rh: [a]\n", "url_rest_lighter_rh"),
    ("intervalo_ping_lighter_s: 0\n", "intervalo_ping_lighter_s"),
    ("intervalo_ping_lighter_s: -1\n", "intervalo_ping_lighter_s"),
    ("intervalo_ping_lighter_s: 120\n", "intervalo_ping_lighter_s"),
    ("intervalo_ping_lighter_s: trinta\n", "intervalo_ping_lighter_s"),
])
def test_campos_da_lighter_com_formato_errado(tmp_path, texto, campo):
    with pytest.raises(ErroConfig, match=campo):
        carregar_config(escrever(tmp_path, texto))


# ---------------------------------------------------------------------------
# imprimir_precos com a Lighter (marco do dono)
# ---------------------------------------------------------------------------


def test_preparar_monta_a_lighter_com_o_conector_proprio():
    exchanges = {"lighter": ClienteLighterFalso(LIGHTER_MARKETS), "mexc": CorretoraFalsa([MEXC_BTC])}
    config = cfg_com(corretoras=["lighter", "mexc"], intervalo_ping_lighter_s=20)
    coletores, simbolos, erros = asyncio.run(ferramenta.preparar(exchanges, config))
    assert erros == []
    assert [type(c).__name__ for c in coletores] == ["ColetorLighter", "ColetorCcxt"]
    assert simbolos == {"lighter": ["BTC/USDC:USDC", "ETH/USDC:USDC"], "mexc": ["BTC/USDT:USDT"]}
    assert coletores[0]._intervalo_ping_s == 20


def test_imprimir_precos_mostra_o_livro_da_lighter():
    livro = {"channel": "order_book:1", "type": "subscribed/order_book", "order_book": {
        "code": 0, "nonce": 1, "bids": [{"price": "64008.0", "size": "1"}],
        "asks": [{"price": "64009.0", "size": "1"}]}}
    cliente = ClienteLighterFalso(LIGHTER_MARKETS, [WsLighterFalso(respostas={"order_book/1": [[livro]]})])
    coletor = ColetorLighter("principal", cliente)
    linhas = []

    async def cenario():
        tarefa = asyncio.create_task(ferramenta.imprimir_precos([coletor], {"lighter": ["BTC/USDC:USDC"]}, 0.01,
                                                                 escrever=linhas.append))
        await esperar_ate(lambda: any("bid 64008" in linha for linha in linhas))
        tarefa.cancel()
        await asyncio.gather(tarefa, return_exceptions=True)

    asyncio.run(cenario())
    assert any(linha.startswith("lighter") and "BTC/USDC:USDC" in linha and "ask 64009" in linha for linha in linhas)
    assert cliente.fechado


def test_main_do_imprimir_precos_cria_a_lighter_pelo_conector(monkeypatch, capsys):
    criadas = []

    def criar(nome, cfg):
        criadas.append(nome)
        raise RuntimeError("parou aqui")

    monkeypatch.setattr(ferramenta, "criar_corretora", criar)
    assert ferramenta.main(["lighter", "--config", str(RAIZ / "config.yaml")]) == 1
    assert criadas == ["lighter"]
    assert "parou aqui" in capsys.readouterr().err
