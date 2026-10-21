"""Testes do estado em memoria, da etapa 2 (pares_para_livro) e dos dados lentos.

Nenhum teste acessa a internet.
"""

import asyncio
import logging

import pytest

from scanner.coletores.dados_lentos import DadosLentos, TaxaDesconhecida, intervalo_funding_h
from scanner.estado.mercados import EstadoMercados, pares_para_livro
from tests.fabricas import livro, market_ccxt


# ---------------------------------------------------------------------------
# Brief (Task 4, Passo 1) - verbatim
# ---------------------------------------------------------------------------

def test_conexao_silenciosa_bloqueia():  # Foco de revisão 3
    e = EstadoMercados(); e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 100.0)
    assert e.obter_livro("gate", "spot", "BTC", agora=101.9) is not None
    assert e.obter_livro("gate", "spot", "BTC", agora=102.1) is None

def test_livro_parado_vale_30s_com_conexao_viva():
    e = EstadoMercados(); e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 125.0)
    assert e.obter_livro("gate", "spot", "BTC", agora=126.0) is not None
    e.registrar_mensagem("gate", 131.0)
    assert e.obter_livro("gate", "spot", "BTC", agora=131.5) is None

def test_corretora_desconectada_sem_dado():
    e = EstadoMercados(); e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 100.0); e.marcar_desconectada("gate")
    assert e.obter_livro("gate", "spot", "BTC", agora=100.1) is None

def test_pares_para_livro():
    assert pares_para_livro({"A": 0.5, "B": 0.1}, 0.3, {}, agora=0) == {"A"}
    assert pares_para_livro({"A": 0.1}, 0.3, {"A": 0.0}, agora=50) == {"A"}
    assert pares_para_livro({"A": 0.1}, 0.3, {"A": 0.0}, agora=61) == set()

def test_taxa_config_tem_prioridade():
    d = DadosLentos(taxas_config={("mexc", "futuro"): 0.02}); d._taxas_api[("mexc", "futuro")] = 0.05
    assert d.taxa_taker("mexc", "futuro") == 0.02


# ---------------------------------------------------------------------------
# Extras: estado em memoria
# ---------------------------------------------------------------------------

def test_padroes_do_estado_sao_2s_de_silencio_e_30s_de_livro():
    e = EstadoMercados()
    e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 102.0)
    assert e.conexao_viva("gate", 104.0) is True    # exatamente 2s: ainda viva
    assert e.conexao_viva("gate", 104.1) is False
    e.registrar_mensagem("gate", 130.0)
    assert e.obter_livro("gate", "spot", "BTC", agora=130.0) is not None  # livro de 30s: vale
    e.registrar_mensagem("gate", 130.1)
    assert e.obter_livro("gate", "spot", "BTC", agora=130.1) is None      # passou de 30s


def test_limites_do_estado_vem_dos_parametros():
    e = EstadoMercados(max_silencio_s=5, max_idade_livro_s=10)
    e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 104.0)
    assert e.obter_livro("gate", "spot", "BTC", agora=109.0) is not None
    e.registrar_mensagem("gate", 110.5)
    assert e.obter_livro("gate", "spot", "BTC", agora=111.0) is None  # livro com 11s > 10s


def test_corretora_que_nunca_mandou_mensagem_nao_esta_viva():
    e = EstadoMercados()
    e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    assert e.conexao_viva("gate", 100.0) is False
    assert e.obter_livro("gate", "spot", "BTC", agora=100.0) is None


def test_livro_novo_substitui_o_antigo():
    e = EstadoMercados()
    e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0, bids=[(1.0, 1.0)], asks=[(1.1, 1.0)]))
    e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=101.0, bids=[(2.0, 1.0)], asks=[(2.1, 1.0)]))
    e.registrar_mensagem("gate", 101.0)
    assert e.obter_livro("gate", "spot", "BTC", agora=101.0).bids[0][0] == 2.0


def test_livro_e_guardado_pelo_ativo_normalizado():
    e = EstadoMercados()
    e.atualizar_livro(livro("mexc", "futuro", "1000PEPE", recebido_em=100.0), ativo="PEPE")
    e.registrar_mensagem("mexc", 100.0)
    assert e.obter_livro("mexc", "futuro", "PEPE", agora=100.0) is not None
    assert e.obter_livro("mexc", "futuro", "1000PEPE", agora=100.0) is None


def test_livro_sem_ativo_usa_o_simbolo_original():
    e = EstadoMercados()
    e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 100.0)
    assert e.obter_livro("gate", "spot", "BTC", agora=100.0).simbolo_original == "BTC"


def test_chave_separa_corretora_e_mercado():
    e = EstadoMercados()
    e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 100.0); e.registrar_mensagem("mexc", 100.0)
    assert e.obter_livro("gate", "futuro", "BTC", agora=100.0) is None
    assert e.obter_livro("mexc", "spot", "BTC", agora=100.0) is None
    assert e.obter_livro("gate", "spot", "ETH", agora=100.0) is None


def test_silencio_de_uma_corretora_nao_bloqueia_a_outra():
    e = EstadoMercados()
    e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.atualizar_livro(livro("mexc", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 100.0); e.registrar_mensagem("mexc", 110.0)
    assert e.obter_livro("gate", "spot", "BTC", agora=110.0) is None
    assert e.obter_livro("mexc", "spot", "BTC", agora=110.0) is not None


def test_livro_de_antes_da_queda_nao_volta_depois_da_reconexao():  # Foco de revisão 3
    e = EstadoMercados()
    e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 100.0)
    e.marcar_desconectada("gate")
    e.registrar_mensagem("gate", 105.0)  # reconectou; o livro de 100.0 ainda teria menos de 30s
    assert e.conexao_viva("gate", 105.0) is True
    assert e.obter_livro("gate", "spot", "BTC", agora=105.0) is None
    e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=106.0))
    assert e.obter_livro("gate", "spot", "BTC", agora=106.0) is not None


def test_desconectar_uma_corretora_nao_apaga_as_outras():
    e = EstadoMercados()
    e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.atualizar_livro(livro("mexc", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 100.0); e.registrar_mensagem("mexc", 100.0)
    e.marcar_desconectada("gate")
    assert e.obter_livro("mexc", "spot", "BTC", agora=100.0) is not None


def test_marcar_desconectada_de_corretora_desconhecida_nao_da_erro():
    EstadoMercados().marcar_desconectada("nunca_vista")


def test_ticker_guardado_e_devolvido():
    e = EstadoMercados()
    e.atualizar_ticker("gate", "futuro", "BTC", 100.0, 100.1, recebido_em=200.0,
                       volume_24h_usd=5_000_000.0, variacao_24h_pct=-1.5,
                       simbolo_original="BTC/USDC:USDC", cotacao="USDC")
    e.registrar_mensagem("gate", 200.0)
    assert e.obter_ticker("gate", "futuro", "BTC", agora=200.5) == {
        "bid": 100.0, "ask": 100.1, "recebido_em": 200.0,
        "volume_24h_usd": 5_000_000.0, "variacao_24h_pct": -1.5,
        "simbolo_original": "BTC/USDC:USDC", "cotacao": "USDC",   # o motor precisa da cotacao para converter para USDT
    }


def test_ticker_sem_simbolo_e_cotacao_guarda_none():
    e = EstadoMercados()
    e.atualizar_ticker("gate", "spot", "BTC", 100.0, 100.1, recebido_em=200.0)
    e.registrar_mensagem("gate", 200.0)
    t = e.obter_ticker("gate", "spot", "BTC", agora=200.0)
    assert t["simbolo_original"] is None and t["cotacao"] is None


def test_ticker_sem_volume_e_variacao_guarda_none():
    e = EstadoMercados()
    e.atualizar_ticker("gate", "spot", "BTC", 100.0, 100.1, recebido_em=200.0)
    e.registrar_mensagem("gate", 200.0)
    t = e.obter_ticker("gate", "spot", "BTC", agora=200.0)
    assert t["volume_24h_usd"] is None and t["variacao_24h_pct"] is None


def test_ticker_inexistente_ou_de_conexao_morta_ou_velho_e_none():
    e = EstadoMercados()
    e.atualizar_ticker("gate", "spot", "BTC", 100.0, 100.1, recebido_em=200.0)
    assert e.obter_ticker("gate", "spot", "BTC", agora=200.0) is None      # conexao nunca registrada
    e.registrar_mensagem("gate", 200.0)
    assert e.obter_ticker("gate", "spot", "ETH", agora=200.0) is None      # ticker que nao existe
    assert e.obter_ticker("gate", "spot", "BTC", agora=202.5) is None      # silencio > 2s
    e.registrar_mensagem("gate", 231.0)
    assert e.obter_ticker("gate", "spot", "BTC", agora=231.0) is None      # ticker com 31s


def test_desconectar_apaga_os_tickers_da_corretora():
    e = EstadoMercados()
    e.atualizar_ticker("gate", "spot", "BTC", 100.0, 100.1, recebido_em=200.0)
    e.registrar_mensagem("gate", 200.0)
    e.marcar_desconectada("gate")
    e.registrar_mensagem("gate", 201.0)  # reconectou
    assert e.obter_ticker("gate", "spot", "BTC", agora=201.0) is None


# ---------------------------------------------------------------------------
# Extras: etapa 2 (pares_para_livro)
# ---------------------------------------------------------------------------

def test_pares_para_livro_atualiza_a_ultima_vez_acima():
    ultima = {"B": 10.0}
    pares_para_livro({"A": 0.5, "B": 0.4, "C": 0.1}, 0.3, ultima, agora=100.0)
    assert ultima == {"A": 100.0, "B": 100.0}  # C nunca esteve acima; nao entra


def test_pares_para_livro_spread_igual_ao_minimo_conta():
    assert pares_para_livro({"A": 0.3}, 0.3, {}, agora=0) == {"A"}


def test_pares_para_livro_mantem_ate_exatamente_manter_s():
    assert pares_para_livro({"A": 0.1}, 0.3, {"A": 0.0}, agora=60.0) == {"A"}
    assert pares_para_livro({"A": 0.1}, 0.3, {"A": 0.0}, agora=60.1) == set()


def test_pares_para_livro_respeita_manter_s_diferente_do_padrao():
    assert pares_para_livro({"A": 0.1}, 0.3, {"A": 0.0}, agora=20, manter_s=10) == set()
    assert pares_para_livro({"A": 0.1}, 0.3, {"A": 0.0}, agora=20, manter_s=30) == {"A"}


def test_pares_para_livro_esquece_quem_passou_do_prazo():
    ultima = {"A": 0.0, "B": 50.0}
    pares_para_livro({}, 0.3, ultima, agora=100.0)
    assert ultima == {"B": 50.0}  # A passou de 60s e sai do dicionario; B ainda esta no prazo


def test_pares_para_livro_volta_a_contar_quando_sobe_de_novo():
    ultima = {"A": 0.0}
    assert pares_para_livro({"A": 0.1}, 0.3, ultima, agora=100.0) == set()
    assert pares_para_livro({"A": 0.9}, 0.3, ultima, agora=101.0) == {"A"}
    assert pares_para_livro({"A": 0.1}, 0.3, ultima, agora=150.0) == {"A"}  # 49s depois de subir


# ---------------------------------------------------------------------------
# Extras: dados lentos (funding e taxas por REST), com corretoras falsas
# ---------------------------------------------------------------------------

def mercado_rest(simbolo, base, quote="USDT", taker=0.0006, tamanho=1, inverso=False, tipo=None):
    """Um market no formato do ccxt (veja tests.fabricas.market_ccxt)."""
    return market_ccxt(simbolo, base, quote, tipo=tipo, tamanho=tamanho, inverso=inverso, taker=taker)


class ExchangeRestFalsa:
    """Imita a parte REST da corretora do ccxt: markets, funding em lote ou um a um."""

    def __init__(self, markets, funding=None, em_lote=True, erro=None):
        self.markets = markets  # None = ainda nao carregou
        self._markets_para_carregar = markets
        self.funding = funding or {}  # simbolo -> dict no formato do ccxt
        self.has = {"fetchFundingRates": em_lote, "fetchFundingRate": True}
        self.erro = erro
        self.chamadas_lote = 0
        self.simbolos_individuais = []
        self.load_markets_chamado = False

    async def load_markets(self):
        self.load_markets_chamado = True
        if self.erro is not None:
            raise self.erro
        self.markets = self._markets_para_carregar

    async def fetch_funding_rates(self, symbols=None):
        self.chamadas_lote += 1
        if self.erro is not None:
            raise self.erro
        return dict(self.funding)

    async def fetch_funding_rate(self, symbol):
        self.simbolos_individuais.append(symbol)
        if self.erro is not None:
            raise self.erro
        if symbol not in self.funding:
            raise KeyError(symbol)
        return self.funding[symbol]


class ErroRest(Exception):
    """Erro inventado pelo teste para simular uma falha de rede."""


def rodar(coro):
    return asyncio.run(coro)


def test_intervalo_funding_h():
    assert intervalo_funding_h("8h") == 8
    assert intervalo_funding_h("1h") == 1
    assert intervalo_funding_h("4h") == 4
    assert intervalo_funding_h(None) == 8
    assert intervalo_funding_h("") == 8
    assert intervalo_funding_h("30m") == 8   # formato que nao e em horas inteiras: padrao
    assert intervalo_funding_h("lixo") == 8


def test_taxa_da_api_vale_quando_a_config_nao_tem():
    d = DadosLentos(taxas_config={("mexc", "futuro"): 0.02})
    d._taxas_api[("mexc", "spot")] = 0.1
    assert d.taxa_taker("mexc", "spot") == 0.1


def test_taxa_desconhecida_levanta_erro_claro():
    d = DadosLentos()
    with pytest.raises(TaxaDesconhecida, match="mexc.*futuro"):
        d.taxa_taker("mexc", "futuro")
    assert issubclass(TaxaDesconhecida, LookupError)


def test_atualizar_converte_taker_de_fracao_para_percentual():
    ex = ExchangeRestFalsa({
        "BTC/USDT": mercado_rest("BTC/USDT", "BTC", taker=0.001),
        "BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC", taker=0.0006),
    })
    d = DadosLentos()
    rodar(d.atualizar({"gate": ex}))
    assert d.taxa_taker("gate", "spot") == pytest.approx(0.1)
    assert d.taxa_taker("gate", "futuro") == pytest.approx(0.06)


def test_atualizar_taker_usa_o_valor_mais_comum_do_mercado():
    markets = {f"X{i}/USDT": mercado_rest(f"X{i}/USDT", f"X{i}", taker=0.001) for i in range(5)}
    markets["PROMO/USDT"] = mercado_rest("PROMO/USDT", "PROMO", taker=0.0)       # excecao
    markets["CARO/USDT"] = mercado_rest("CARO/USDT", "CARO", taker=0.004)         # excecao
    d = DadosLentos()
    rodar(d.atualizar({"gate": ExchangeRestFalsa(markets)}))
    assert d.taxa_taker("gate", "spot") == pytest.approx(0.1)


def test_atualizar_taker_empatado_fica_com_o_maior():
    markets = {
        "A/USDT": mercado_rest("A/USDT", "A", taker=0.001),
        "B/USDT": mercado_rest("B/USDT", "B", taker=0.002),
    }
    d = DadosLentos()
    rodar(d.atualizar({"gate": ExchangeRestFalsa(markets)}))
    assert d.taxa_taker("gate", "spot") == pytest.approx(0.2)


def test_atualizar_ignora_mercado_sem_taker_e_inverso_na_taxa():
    markets = {
        "BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC", taker=0.0006),
        "ETH/USDT:USDT": mercado_rest("ETH/USDT:USDT", "ETH", taker=None),
        "BTC/USD:BTC": mercado_rest("BTC/USD:BTC", "BTC", quote="USD", taker=0.005, inverso=True),
        "XRP/USD:XRP": mercado_rest("XRP/USD:XRP", "XRP", quote="USD", taker=0.005, inverso=True),
    }
    d = DadosLentos()
    rodar(d.atualizar({"gate": ExchangeRestFalsa(markets)}))
    assert d.taxa_taker("gate", "futuro") == pytest.approx(0.06)


def test_taker_do_futuro_ignora_opcoes_e_futuros_com_vencimento_mesmo_sendo_maioria():
    # Como a Gate: as opcoes (taker 0,03%) sao muito mais numerosas que os perpetuos (taker 0,05%).
    # Se contassem como "futuro", a taxa do futuro cairia para 0,03% e criaria sinais falsos.
    markets = {f"X{i}/USDT:USDT": mercado_rest(f"X{i}/USDT:USDT", f"X{i}", taker=0.0005) for i in range(3)}
    for i in range(10):
        simbolo = f"BTC/USDT:USDT-261226-{100000 + i}-C"
        markets[simbolo] = mercado_rest(simbolo, "BTC", taker=0.0003, tipo="option")
    for i in range(4):
        simbolo = f"ETH/USDT:USDT-26122{i}"
        markets[simbolo] = mercado_rest(simbolo, "ETH", taker=0.0004, tipo="future")
    markets["BTC/USDT"] = mercado_rest("BTC/USDT", "BTC", taker=0.002)
    d = DadosLentos()
    rodar(d.atualizar({"gate": ExchangeRestFalsa(markets)}))
    assert d.taxa_taker("gate", "futuro") == pytest.approx(0.05)
    assert d.taxa_taker("gate", "spot") == pytest.approx(0.2)
    assert set(d._taxas_api) == {("gate", "futuro"), ("gate", "spot")}   # opcoes e vencimentos nao viram um mercado


def test_taker_so_de_opcoes_nao_cria_taxa_de_futuro():
    markets = {"BTC/USDT:USDT-261226-100000-C": mercado_rest("BTC/USDT:USDT-261226-100000-C", "BTC", taker=0.0003, tipo="option")}
    d = DadosLentos()
    rodar(d.atualizar({"gate": ExchangeRestFalsa(markets)}))
    with pytest.raises(TaxaDesconhecida):
        d.taxa_taker("gate", "futuro")


def test_taxa_da_config_continua_valendo_depois_de_atualizar():
    ex = ExchangeRestFalsa({"BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC", taker=0.0006)})
    d = DadosLentos(taxas_config={("gate", "futuro"): 0.02})
    rodar(d.atualizar({"gate": ex}))
    assert d._taxas_api[("gate", "futuro")] == pytest.approx(0.06)
    assert d.taxa_taker("gate", "futuro") == 0.02


def test_atualizar_carrega_os_mercados_se_ainda_nao_foram_carregados():
    ex = ExchangeRestFalsa(None)
    ex._markets_para_carregar = {"BTC/USDT": mercado_rest("BTC/USDT", "BTC", taker=0.001)}
    d = DadosLentos()
    rodar(d.atualizar({"gate": ex}))
    assert ex.load_markets_chamado
    assert d.taxa_taker("gate", "spot") == pytest.approx(0.1)


def test_funding_em_lote_com_intervalos_diferentes():
    markets = {
        "BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC"),
        "ETH/USDT:USDT": mercado_rest("ETH/USDT:USDT", "ETH"),
    }
    funding = {
        "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001, "interval": "8h"},
        "ETH/USDT:USDT": {"symbol": "ETH/USDT:USDT", "fundingRate": -0.00025, "interval": "1h"},
    }
    ex = ExchangeRestFalsa(markets, funding)
    d = DadosLentos()
    rodar(d.atualizar({"gate": ex}))
    taxa, horas = d.funding("gate", "BTC")
    assert taxa == pytest.approx(0.01) and horas == 8
    taxa, horas = d.funding("gate", "ETH")
    assert taxa == pytest.approx(-0.025) and horas == 1
    assert ex.chamadas_lote == 1 and ex.simbolos_individuais == []


def test_funding_sem_intervalo_informado_assume_8h():
    markets = {"BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC")}
    funding = {"BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001}}
    d = DadosLentos()
    rodar(d.atualizar({"gate": ExchangeRestFalsa(markets, funding)}))
    assert d.funding("gate", "BTC") == (pytest.approx(0.01), 8)


def test_funding_um_a_um_quando_a_corretora_nao_tem_lote():
    markets = {
        "BTC/USDT": mercado_rest("BTC/USDT", "BTC"),  # spot: nao tem funding
        "BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC"),
        "ETH/USDT:USDT": mercado_rest("ETH/USDT:USDT", "ETH"),
    }
    funding = {
        "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0002, "interval": "4h"},
        "ETH/USDT:USDT": {"symbol": "ETH/USDT:USDT", "fundingRate": 0.0003},
    }
    ex = ExchangeRestFalsa(markets, funding, em_lote=False)
    d = DadosLentos()
    rodar(d.atualizar({"mexc": ex}))
    assert sorted(ex.simbolos_individuais) == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    assert ex.chamadas_lote == 0
    assert d.funding("mexc", "BTC") == (pytest.approx(0.02), 4)
    assert d.funding("mexc", "ETH") == (pytest.approx(0.03), 8)


def test_funding_um_a_um_so_pergunta_dos_perpetuos_lineares():
    markets = {
        "BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC"),
        "BTC/USD:BTC": mercado_rest("BTC/USD:BTC", "BTC", quote="USD", inverso=True),
        "BTC/USDT:USDT-261226": mercado_rest("BTC/USDT:USDT-261226", "BTC", tipo="future"),
        "BTC/USDT:USDT-261226-100000-C": mercado_rest("BTC/USDT:USDT-261226-100000-C", "BTC", tipo="option"),
        "BTC/USDT": mercado_rest("BTC/USDT", "BTC"),
    }
    funding = {"BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001}}
    ex = ExchangeRestFalsa(markets, funding, em_lote=False)
    rodar(DadosLentos().atualizar({"mexc": ex}))
    assert ex.simbolos_individuais == ["BTC/USDT:USDT"]


def test_funding_em_lote_ignora_opcoes_e_futuros_com_vencimento():
    markets = {
        "BTC/USDT:USDT-261226": mercado_rest("BTC/USDT:USDT-261226", "BTC", tipo="future"),
        "ETH/USDT:USDT-261226-3000-C": mercado_rest("ETH/USDT:USDT-261226-3000-C", "ETH", tipo="option"),
    }
    funding = {
        "BTC/USDT:USDT-261226": {"symbol": "BTC/USDT:USDT-261226", "fundingRate": 0.0001},
        "ETH/USDT:USDT-261226-3000-C": {"symbol": "ETH/USDT:USDT-261226-3000-C", "fundingRate": 0.0001},
    }
    d = DadosLentos()
    rodar(d.atualizar({"gate": ExchangeRestFalsa(markets, funding)}))
    assert d.funding("gate", "BTC") is None and d.funding("gate", "ETH") is None


def test_funding_mesmo_ativo_e_cotacao_desempata_pelo_simbolo_em_qualquer_ordem():
    markets = {
        "PEPE/USDT:USDT": mercado_rest("PEPE/USDT:USDT", "PEPE"),
        "1000PEPE/USDT:USDT": mercado_rest("1000PEPE/USDT:USDT", "1000PEPE"),
    }
    um = {"symbol": "1000PEPE/USDT:USDT", "fundingRate": 0.0001, "interval": "8h"}
    outro = {"symbol": "PEPE/USDT:USDT", "fundingRate": 0.0002, "interval": "4h"}
    for funding in ({"PEPE/USDT:USDT": outro, "1000PEPE/USDT:USDT": um},
                    {"1000PEPE/USDT:USDT": um, "PEPE/USDT:USDT": outro}):
        d = DadosLentos()
        rodar(d.atualizar({"mexc": ExchangeRestFalsa(markets, funding)}))
        assert d.funding("mexc", "PEPE") == (pytest.approx(0.01), 8)   # o mesmo simbolo que ocupa a chave do estado


def test_funding_um_a_um_erro_num_simbolo_nao_perde_os_outros(caplog):
    markets = {
        "BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC"),
        "ETH/USDT:USDT": mercado_rest("ETH/USDT:USDT", "ETH"),
    }
    funding = {"ETH/USDT:USDT": {"symbol": "ETH/USDT:USDT", "fundingRate": 0.0003}}  # BTC da KeyError
    d = DadosLentos()
    with caplog.at_level(logging.WARNING, logger="scanner.coletores"):
        rodar(d.atualizar({"mexc": ExchangeRestFalsa(markets, funding, em_lote=False)}))
    assert d.funding("mexc", "ETH") is not None
    assert d.funding("mexc", "BTC") is None
    assert any("BTC/USDT:USDT" in m for m in caplog.messages)


def test_funding_usa_o_ativo_normalizado():
    markets = {"1000PEPE/USDT:USDT": mercado_rest("1000PEPE/USDT:USDT", "1000PEPE")}
    funding = {"1000PEPE/USDT:USDT": {"symbol": "1000PEPE/USDT:USDT", "fundingRate": 0.0001, "interval": "8h"}}
    d = DadosLentos()
    rodar(d.atualizar({"mexc": ExchangeRestFalsa(markets, funding)}))
    assert d.funding("mexc", "PEPE") is not None
    assert d.funding("mexc", "1000PEPE") is None


def test_funding_ignora_inverso_e_prefere_usdt():
    markets = {
        "BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC"),
        "BTC/USDC:USDC": mercado_rest("BTC/USDC:USDC", "BTC", quote="USDC"),
        "BTC/USD:BTC": mercado_rest("BTC/USD:BTC", "BTC", quote="USD", inverso=True),
    }
    funding = {
        "BTC/USDC:USDC": {"symbol": "BTC/USDC:USDC", "fundingRate": 0.0009},
        "BTC/USD:BTC": {"symbol": "BTC/USD:BTC", "fundingRate": 0.0008},
        "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001},  # ultimo na ordem de chegada
    }
    d = DadosLentos()
    rodar(d.atualizar({"bitget": ExchangeRestFalsa(markets, funding)}))
    assert d.funding("bitget", "BTC") == (pytest.approx(0.01), 8)
    # e a ordem inversa nao muda o resultado
    funding2 = dict(reversed(list(funding.items())))
    d2 = DadosLentos()
    rodar(d2.atualizar({"bitget": ExchangeRestFalsa(markets, funding2)}))
    assert d2.funding("bitget", "BTC") == (pytest.approx(0.01), 8)


def test_funding_de_contrato_inverso_e_ignorado_mesmo_sozinho():
    markets = {"XRP/USD:XRP": mercado_rest("XRP/USD:XRP", "XRP", quote="USD", inverso=True)}
    funding = {"XRP/USD:XRP": {"symbol": "XRP/USD:XRP", "fundingRate": 0.0008}}
    d = DadosLentos()
    rodar(d.atualizar({"bitget": ExchangeRestFalsa(markets, funding)}))
    assert d.funding("bitget", "XRP") is None


def test_funding_sem_taxa_e_simbolo_desconhecido_sao_ignorados():
    markets = {"BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC")}
    funding = {
        "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": None},
        "XYZ/USDT:USDT": {"symbol": "XYZ/USDT:USDT", "fundingRate": 0.0001},  # nao esta nos markets
    }
    d = DadosLentos()
    rodar(d.atualizar({"gate": ExchangeRestFalsa(markets, funding)}))
    assert d.funding("gate", "BTC") is None and d.funding("gate", "XYZ") is None


def test_funding_desconhecido_devolve_none():
    assert DadosLentos().funding("gate", "BTC") is None


def test_erro_numa_corretora_nao_impede_as_outras_e_mantem_o_dado_antigo(caplog):
    markets = {"BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC", taker=0.0006)}
    funding = {"BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001}}
    boa = ExchangeRestFalsa(markets, funding)
    ruim = ExchangeRestFalsa(markets, funding)
    d = DadosLentos()
    rodar(d.atualizar({"mexc": ruim, "gate": boa}))
    assert d.funding("mexc", "BTC") is not None

    ruim.erro = ErroRest("sem rede")
    with caplog.at_level(logging.WARNING, logger="scanner.coletores"):
        rodar(d.atualizar({"mexc": ruim, "gate": boa}))
    assert d.funding("mexc", "BTC") is not None          # dado antigo continua
    assert d.taxa_taker("mexc", "futuro") == pytest.approx(0.06)
    assert d.funding("gate", "BTC") is not None
    assert any("mexc" in m for m in caplog.messages)


def test_rodar_atualiza_de_tempos_em_tempos_e_sobrevive_a_erro():
    markets = {"BTC/USDT:USDT": mercado_rest("BTC/USDT:USDT", "BTC")}
    funding = {"BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001}}
    ex = ExchangeRestFalsa(markets, funding)
    d = DadosLentos()

    async def cenario():
        tarefa = asyncio.create_task(d.rodar({"gate": ex}, intervalo_s=0.01))
        await asyncio.sleep(0.05)
        ex.erro = ErroRest("falha momentanea")
        await asyncio.sleep(0.05)
        ex.erro = None
        antes = ex.chamadas_lote
        await asyncio.sleep(0.05)
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa
        return antes

    antes = asyncio.run(cenario())
    assert ex.chamadas_lote > antes >= 2  # continuou chamando depois do erro
    assert d.funding("gate", "BTC") is not None
