"""Testes das duas etapas do ColetorCcxt (tickers de todos os pares + livros sob demanda).

Nenhum teste acessa a internet: a corretora do ccxt e trocada por um objeto falso
que entrega, na ordem, as mensagens combinadas no `roteiro` de cada teste.
"""

import asyncio
import logging

import pytest
from ccxt.base.errors import NotSupported

from scanner.coletores.ccxt_coletor import ColetorCcxt, mercado_do_simbolo
from scanner.estado.mercados import EstadoMercados
from scanner.modelos import Livro, Ticker


class ErroDaCorretora(Exception):
    """Erro inventado pelo teste para simular uma falha de rede."""


def mercado(simbolo, base, quote="USDT", tamanho=1):
    """Um market no formato do ccxt (so os campos que o scanner le)."""
    return {"symbol": simbolo, "base": base, "quote": quote, "contractSize": tamanho if ":" in simbolo else None}


MARKETS = {
    "BTC/USDT": mercado("BTC/USDT", "BTC"),
    "ETH/USDT": mercado("ETH/USDT", "ETH"),
    "SOL/USDT": mercado("SOL/USDT", "SOL"),
    "BTC/USDT:USDT": mercado("BTC/USDT:USDT", "BTC", tamanho=0.001),
    "1000PEPE/USDT:USDT": mercado("1000PEPE/USDT:USDT", "1000PEPE"),
}

TUDO = {"watchTickers": True, "watchBidsAsks": True, "watchTicker": True}


class ExchangeDuasEtapas:
    """Imita a corretora do ccxt com tickers e livros.

    `roteiro` mapeia uma chave para a fila de respostas:
      - tickers em lote: (metodo, mercado), ex.: ("watch_tickers", "futuro")
      - ticker de um par: ("watch_ticker", simbolo)
      - livro: ("watch_order_book", simbolo)
    Uma resposta que e uma excecao e levantada. Acabando a fila, a chamada espera
    para sempre (como o ccxt espera a proxima mensagem); o teste cancela no fim.
    """

    def __init__(self, markets=None, has=None, roteiro=None):
        self.markets = MARKETS if markets is None else markets
        self.has = dict(TUDO) if has is None else has
        self.roteiro = {chave: list(fila) for chave, fila in (roteiro or {}).items()}
        self.chamadas = []          # (metodo, simbolos pedidos)
        self.livros_cancelados = []  # simbolos cujo watch_order_book foi cancelado
        self.fechada = False

    async def _proximo(self, chave):
        await asyncio.sleep(0)
        fila = self.roteiro.get(chave)
        if fila:
            resposta = fila.pop(0)
            if isinstance(resposta, BaseException):
                raise resposta
            return resposta
        await asyncio.sleep(3600)

    async def watch_tickers(self, symbols=None, params=None):
        self.chamadas.append(("watch_tickers", tuple(symbols)))
        return await self._proximo(("watch_tickers", mercado_do_simbolo(symbols[0])))

    async def watch_bids_asks(self, symbols=None, params=None):
        self.chamadas.append(("watch_bids_asks", tuple(symbols)))
        return await self._proximo(("watch_bids_asks", mercado_do_simbolo(symbols[0])))

    async def watch_ticker(self, symbol, params=None):
        self.chamadas.append(("watch_ticker", (symbol,)))
        return await self._proximo(("watch_ticker", symbol))

    async def watch_order_book(self, symbol, limit=None):
        try:
            return await self._proximo(("watch_order_book", symbol))
        except asyncio.CancelledError:
            self.livros_cancelados.append(symbol)
            raise

    async def close(self):
        self.fechada = True


def tk(bid, ask, volume=None, variacao=None):
    """Ticker no formato do ccxt."""
    return {"bid": bid, "ask": ask, "quoteVolume": volume, "percentage": variacao}


def ob(bid, ask, qtd=1.0):
    """Livro no formato do ccxt com um nivel de cada lado."""
    return {"bids": [[bid, qtd]], "asks": [[ask, qtd]], "timestamp": None}


async def esperar_ate(condicao, timeout=2.0):
    """Espera (cedendo a vez ao resto do programa) ate a condicao ficar verdadeira."""
    limite = asyncio.get_running_loop().time() + timeout
    while not condicao():
        if asyncio.get_running_loop().time() > limite:
            raise AssertionError("a condicao nao ficou verdadeira a tempo")
        await asyncio.sleep(0.005)


async def cancelar(tarefa):
    tarefa.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarefa


def relogio_fixo(valor=500.0):
    return lambda: valor


# ---------------------------------------------------------------------------
# Etapa 1: tickers
# ---------------------------------------------------------------------------

def test_tickers_sao_pedidos_separados_por_mercado():
    # O ccxt decide o tipo de mercado pelo primeiro simbolo: misturar spot e futuro erraria.
    ex = ExchangeDuasEtapas()
    coletor = ColetorCcxt("bitget", ex)

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(
            ["BTC/USDT", "BTC/USDT:USDT", "ETH/USDT"], lambda t: None))
        await esperar_ate(lambda: len(ex.chamadas) >= 2)
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert sorted(ex.chamadas) == [
        ("watch_tickers", ("BTC/USDT", "ETH/USDT")),
        ("watch_tickers", ("BTC/USDT:USDT",)),
    ]


def test_ticker_vai_cru_ao_callback_e_normalizado_ao_estado():
    ex = ExchangeDuasEtapas(roteiro={("watch_tickers", "futuro"): [
        {"1000PEPE/USDT:USDT": tk(0.0100, 0.0101, volume=5_000_000.0, variacao=-1.5)},
    ]})
    estado = EstadoMercados()
    coletor = ColetorCcxt("mexc", ex, estado=estado, relogio=relogio_fixo(500.0))
    recebidos = []

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(["1000PEPE/USDT:USDT"], recebidos.append))
        await esperar_ate(lambda: recebidos)
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert recebidos == [Ticker("mexc", "futuro", "1000PEPE/USDT:USDT", 0.0100, 0.0101, 500.0, 5_000_000.0, -1.5)]
    t = estado.obter_ticker("mexc", "futuro", "PEPE", agora=500.0)   # guardado pelo ativo normalizado
    assert t["bid"] == pytest.approx(0.00001) and t["ask"] == pytest.approx(0.0000101)
    assert t["volume_24h_usd"] == 5_000_000.0 and t["variacao_24h_pct"] == -1.5
    assert t["recebido_em"] == 500.0
    assert estado.obter_ticker("mexc", "futuro", "1000PEPE", agora=500.0) is None


def test_ticker_sem_volume_nem_variacao_vira_none():
    ex = ExchangeDuasEtapas(roteiro={("watch_tickers", "spot"): [{"BTC/USDT": tk(100.0, 100.1)}]})
    recebidos = []
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(["BTC/USDT"], recebidos.append))
        await esperar_ate(lambda: recebidos)
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert (recebidos[0].volume_24h_usd, recebidos[0].variacao_24h_pct) == (None, None)


def test_toda_mensagem_de_ticker_registra_a_conexao_ate_a_invalida():
    ex = ExchangeDuasEtapas(roteiro={("watch_tickers", "spot"): [{"BTC/USDT": tk(None, None)}]})
    estado = EstadoMercados()
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(["BTC/USDT"], lambda t: None))
        await esperar_ate(lambda: len(ex.chamadas) >= 2)  # a primeira mensagem ja foi tratada
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert estado.conexao_viva("gate", 500.0) is True
    assert estado.conexao_viva("gate", 502.5) is False


def test_ticker_invalido_e_descartado():
    mensagem = {
        "BTC/USDT": tk(None, 100.1),
        "ETH/USDT": tk(100.0, None),
        "SOL/USDT": tk(0.0, 1.0),
        "XRP/USDT": tk(5.0, 5.0),              # bid igual ao ask
        "BTC/USDT:USDT": tk(101.0, 100.0),     # cruzado
        "1000PEPE/USDT:USDT": tk(float("nan"), 1.0),
    }
    ex = ExchangeDuasEtapas(roteiro={
        ("watch_tickers", "spot"): [mensagem, {"BTC/USDT": tk(100.0, 100.1)}],
    })
    recebidos = []
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(["BTC/USDT", "ETH/USDT", "SOL/USDT"], recebidos.append))
        await esperar_ate(lambda: recebidos)
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert [(t.simbolo_original, t.bid) for t in recebidos] == [("BTC/USDT", 100.0)]


def test_ticker_sem_mercado_conhecido_nao_vai_ao_estado(caplog):
    ex = ExchangeDuasEtapas(markets={}, roteiro={("watch_tickers", "spot"): [{"BTC/USDT": tk(100.0, 100.1)}]})
    estado = EstadoMercados()
    recebidos = []
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(["BTC/USDT"], recebidos.append))
        await esperar_ate(lambda: recebidos)
        await cancelar(tarefa)

    with caplog.at_level(logging.WARNING, logger="scanner.coletores"):
        asyncio.run(cenario())
    assert estado.obter_ticker("gate", "spot", "BTC", agora=500.0) is None
    assert any("BTC/USDT" in m and "mercado" in m for m in caplog.messages)


def test_sem_watch_tickers_usa_watch_bids_asks():
    has = {"watchTickers": False, "watchBidsAsks": True, "watchTicker": True}
    ex = ExchangeDuasEtapas(has=has, roteiro={("watch_bids_asks", "spot"): [{"BTC/USDT": tk(100.0, 100.1)}]})
    recebidos = []
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(["BTC/USDT"], recebidos.append))
        await esperar_ate(lambda: recebidos)
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert ex.chamadas[0][0] == "watch_bids_asks"
    assert all(metodo != "watch_tickers" for metodo, _ in ex.chamadas)


def test_sem_lote_usa_watch_ticker_por_simbolo():
    has = {"watchTickers": False, "watchBidsAsks": False, "watchTicker": True}
    ex = ExchangeDuasEtapas(has=has, roteiro={
        ("watch_ticker", "BTC/USDT"): [tk(100.0, 100.1)],
        ("watch_ticker", "ETH/USDT"): [tk(10.0, 10.1)],
    })
    estado = EstadoMercados()
    recebidos = []
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(["BTC/USDT", "ETH/USDT"], recebidos.append))
        await esperar_ate(lambda: len(recebidos) == 2)
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert {t.simbolo_original for t in recebidos} == {"BTC/USDT", "ETH/USDT"}
    assert {metodo for metodo, _ in ex.chamadas} == {"watch_ticker"}
    assert estado.conexao_viva("gate", 500.0)
    assert estado.obter_ticker("gate", "spot", "ETH", agora=500.0)["bid"] == 10.0


def test_metodo_nao_suportado_cai_para_o_proximo_so_no_mercado_afetado():
    # Como a MEXC: watch_tickers so existe no futuro, watch_bids_asks so no spot.
    ex = ExchangeDuasEtapas(roteiro={
        ("watch_tickers", "spot"): [NotSupported("mexc watchTickers does not support spot markets")],
        ("watch_bids_asks", "spot"): [{"BTC/USDT": tk(100.0, 100.1)}],
        ("watch_tickers", "futuro"): [{"BTC/USDT:USDT": tk(101.0, 101.1)}],
    })
    recebidos = []
    coletor = ColetorCcxt("mexc", ex)

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(["BTC/USDT", "BTC/USDT:USDT"], recebidos.append))
        await esperar_ate(lambda: len(recebidos) == 2)
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert {t.simbolo_original for t in recebidos} == {"BTC/USDT", "BTC/USDT:USDT"}
    metodos_do_futuro = {m for m, s in ex.chamadas if s == ("BTC/USDT:USDT",)}
    assert metodos_do_futuro == {"watch_tickers"}
    metodos_do_spot = [m for m, s in ex.chamadas if s == ("BTC/USDT",)]
    assert metodos_do_spot[:2] == ["watch_tickers", "watch_bids_asks"]


def test_nenhum_metodo_de_ticker_suportado_levanta_erro():
    has = {"watchTickers": True, "watchBidsAsks": True, "watchTicker": True}
    ex = ExchangeDuasEtapas(has=has, roteiro={
        ("watch_tickers", "spot"): [NotSupported("a")],
        ("watch_bids_asks", "spot"): [NotSupported("b")],
        ("watch_ticker", "BTC/USDT"): [NotSupported("c")],
    })
    coletor = ColetorCcxt("mexc", ex)
    with pytest.raises(NotSupported):
        asyncio.run(asyncio.wait_for(coletor.iniciar_tickers(["BTC/USDT"], lambda t: None), timeout=2))


def test_corretora_sem_nenhum_metodo_de_ticker_levanta_erro():
    ex = ExchangeDuasEtapas(has={})
    coletor = ColetorCcxt("x", ex)
    with pytest.raises(NotSupported):
        asyncio.run(asyncio.wait_for(coletor.iniciar_tickers(["BTC/USDT"], lambda t: None), timeout=2))


def test_erro_de_rede_no_ticker_sobe_para_quem_chamou():
    ex = ExchangeDuasEtapas(roteiro={("watch_tickers", "spot"): [ErroDaCorretora("caiu")]})
    coletor = ColetorCcxt("gate", ex)
    with pytest.raises(ErroDaCorretora):
        asyncio.run(asyncio.wait_for(coletor.iniciar_tickers(["BTC/USDT", "BTC/USDT:USDT"], lambda t: None), timeout=2))


def test_iniciar_tickers_nao_fecha_a_corretora():
    # Quem fecha e o fechar(): os livros da etapa 2 usam a mesma corretora.
    ex = ExchangeDuasEtapas(roteiro={("watch_tickers", "spot"): [{"BTC/USDT": tk(100.0, 100.1)}]})
    recebidos = []
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(["BTC/USDT"], recebidos.append))
        await esperar_ate(lambda: recebidos)
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert ex.fechada is False


def test_iniciar_tickers_sem_callback_so_alimenta_o_estado():
    ex = ExchangeDuasEtapas(roteiro={("watch_tickers", "spot"): [{"BTC/USDT": tk(100.0, 100.1)}]})
    estado = EstadoMercados()
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(["BTC/USDT"]))
        await esperar_ate(lambda: estado.obter_ticker("gate", "spot", "BTC", agora=500.0))
        await cancelar(tarefa)

    asyncio.run(cenario())


# ---------------------------------------------------------------------------
# Etapa 2: livros sob demanda
# ---------------------------------------------------------------------------

def test_livro_vai_cru_ao_callback_e_convertido_ao_estado():
    ex = ExchangeDuasEtapas(roteiro={("watch_order_book", "1000PEPE/USDT:USDT"): [ob(0.0100, 0.0101, qtd=5000.0)]})
    estado = EstadoMercados()
    recebidos = []
    coletor = ColetorCcxt("mexc", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        await coletor.assinar_livros({"1000PEPE/USDT:USDT"}, recebidos.append)
        await esperar_ate(lambda: recebidos)
        await coletor.fechar()

    asyncio.run(cenario())
    assert recebidos[0].bids == [(0.0100, 5000.0)]          # cru: igual ao site da corretora
    livro = estado.obter_livro("mexc", "futuro", "PEPE", agora=500.0)
    assert isinstance(livro, Livro)
    assert livro.bids == [(pytest.approx(0.00001), pytest.approx(5_000_000.0))]
    assert livro.simbolo_original == "1000PEPE/USDT:USDT"


def test_livro_de_contrato_diferente_de_1_chega_em_unidades_do_ativo():
    ex = ExchangeDuasEtapas(roteiro={("watch_order_book", "BTC/USDT:USDT"): [ob(67000.0, 67001.0, qtd=10.0)]})
    estado = EstadoMercados()
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT:USDT"})
        await esperar_ate(lambda: estado.obter_livro("gate", "futuro", "BTC", agora=500.0))
        await coletor.fechar()

    asyncio.run(cenario())
    livro = estado.obter_livro("gate", "futuro", "BTC", agora=500.0)
    assert livro.bids == [(67000.0, pytest.approx(0.01))]   # 10 contratos x 0,001 BTC


def test_toda_mensagem_de_livro_registra_a_conexao_ate_a_descartada():
    ex = ExchangeDuasEtapas(roteiro={("watch_order_book", "BTC/USDT"): [ob(101.0, 100.0)]})  # cruzado
    estado = EstadoMercados()
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"})
        await esperar_ate(lambda: estado.conexao_viva("gate", 500.0))
        await coletor.fechar()

    asyncio.run(cenario())
    assert estado.obter_livro("gate", "spot", "BTC", agora=500.0) is None  # descartado, mas a conexao esta viva


def test_iniciar_tambem_registra_a_conexao_e_guarda_no_estado():
    ex = ExchangeDuasEtapas(roteiro={("watch_order_book", "BTC/USDT"): [ob(100.0, 100.1)]})
    estado = EstadoMercados()
    recebidos = []
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar(["BTC/USDT"], recebidos.append))
        await esperar_ate(lambda: recebidos)
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert estado.conexao_viva("gate", 500.0)
    assert estado.obter_livro("gate", "spot", "BTC", agora=500.0) is not None


def test_assinar_livros_inicia_os_novos_e_cancela_os_que_sairam():
    ex = ExchangeDuasEtapas()
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT"}, lambda l: None)
        await asyncio.sleep(0.02)
        eth_antes = coletor._tarefas_livro["ETH/USDT"]
        await coletor.assinar_livros({"ETH/USDT", "SOL/USDT"})
        assert set(coletor._tarefas_livro) == {"ETH/USDT", "SOL/USDT"}
        assert coletor._tarefas_livro["ETH/USDT"] is eth_antes      # quem ficou nao reinicia
        assert ex.livros_cancelados == ["BTC/USDT"]                  # quem saiu foi cancelado de verdade
        await asyncio.sleep(0.02)                                    # deixa o SOL comecar a esperar o livro
        await coletor.assinar_livros(set())
        assert coletor._tarefas_livro == {}
        assert sorted(ex.livros_cancelados) == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
        await coletor.fechar()

    asyncio.run(cenario())


def test_assinar_livros_repetido_com_o_mesmo_conjunto_nao_duplica():
    ex = ExchangeDuasEtapas()
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"}, lambda l: None)
        tarefa = coletor._tarefas_livro["BTC/USDT"]
        await coletor.assinar_livros({"BTC/USDT"})
        await coletor.assinar_livros({"BTC/USDT"})
        assert coletor._tarefas_livro["BTC/USDT"] is tarefa
        await coletor.fechar()

    asyncio.run(cenario())


def test_falha_num_simbolo_nao_derruba_os_outros(caplog):
    ex = ExchangeDuasEtapas(roteiro={
        ("watch_order_book", "ETH/USDT"): [ErroDaCorretora("eth caiu")],
        ("watch_order_book", "BTC/USDT"): [ob(100.0, 100.1), ob(100.2, 100.3), ob(100.4, 100.5)],
    })
    recebidos = []
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT"}, recebidos.append)
        await esperar_ate(lambda: len(recebidos) == 3)           # BTC segue entregando
        assert "ETH/USDT" not in coletor._tarefas_livro          # ETH foi retirado
        assert "BTC/USDT" in coletor._tarefas_livro
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner.coletores"):
        asyncio.run(cenario())
    avisos = [r.getMessage() for r in caplog.records if r.name == "scanner.coletores"]  # nao conta o log do asyncio
    assert any("ETH/USDT" in m and "eth caiu" in m for m in avisos)


def test_simbolo_que_caiu_volta_na_proxima_assinatura():
    ex = ExchangeDuasEtapas(roteiro={
        ("watch_order_book", "ETH/USDT"): [ErroDaCorretora("eth caiu"), ob(10.0, 10.1)],
    })
    recebidos = []
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        await coletor.assinar_livros({"ETH/USDT"}, recebidos.append)
        await esperar_ate(lambda: "ETH/USDT" not in coletor._tarefas_livro)
        await coletor.assinar_livros({"ETH/USDT"})
        await esperar_ate(lambda: recebidos)
        await coletor.fechar()

    asyncio.run(cenario())
    assert recebidos[0].simbolo_original == "ETH/USDT"


def test_assinar_livros_guarda_o_callback_para_as_proximas_chamadas():
    ex = ExchangeDuasEtapas(roteiro={("watch_order_book", "ETH/USDT"): [ob(10.0, 10.1)]})
    recebidos = []
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"}, recebidos.append)
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT"})   # sem callback: usa o guardado
        await esperar_ate(lambda: recebidos)
        await coletor.fechar()

    asyncio.run(cenario())
    assert recebidos[0].simbolo_original == "ETH/USDT"


def test_assinar_livros_sem_destino_para_os_livros_levanta_erro():
    coletor = ColetorCcxt("gate", ExchangeDuasEtapas())
    with pytest.raises(ValueError):
        asyncio.run(coletor.assinar_livros({"BTC/USDT"}))


def test_mercado_desconhecido_nao_vai_ao_estado_mas_o_callback_recebe(caplog):
    ex = ExchangeDuasEtapas(markets={}, roteiro={("watch_order_book", "BTC/USDT"): [ob(100.0, 100.1), ob(100.2, 100.3)]})
    estado = EstadoMercados()
    recebidos = []
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"}, recebidos.append)
        await esperar_ate(lambda: len(recebidos) == 2)
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner.coletores"):
        asyncio.run(cenario())
    assert estado.obter_livro("gate", "spot", "BTC", agora=500.0) is None
    avisos = [m for m in caplog.messages if "BTC/USDT" in m and "mercado" in m]
    assert len(avisos) == 1  # avisa uma vez so, nao a cada mensagem


def test_market_sem_base_ou_cotacao_e_tratado_como_desconhecido():
    markets = {"BTC/USDT": {"symbol": "BTC/USDT", "base": None, "quote": "USDT", "contractSize": None}}
    ex = ExchangeDuasEtapas(markets=markets, roteiro={("watch_order_book", "BTC/USDT"): [ob(100.0, 100.1)]})
    estado = EstadoMercados()
    recebidos = []
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"}, recebidos.append)
        await esperar_ate(lambda: recebidos)
        await coletor.fechar()

    asyncio.run(cenario())
    assert estado.obter_livro("gate", "spot", "BTC", agora=500.0) is None


def test_fechar_cancela_os_livros_e_fecha_a_corretora():
    ex = ExchangeDuasEtapas()
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT"}, lambda l: None)
        await asyncio.sleep(0.02)
        await coletor.fechar()
        # ainda dentro do loop: o fechar() e quem cancela, nao o encerramento do asyncio.run
        assert sorted(ex.livros_cancelados) == ["BTC/USDT", "ETH/USDT"]

    asyncio.run(cenario())
    assert coletor._tarefas_livro == {}
    assert ex.fechada is True


def test_fechar_sem_nada_assinado_so_fecha_a_corretora():
    ex = ExchangeDuasEtapas()
    asyncio.run(ColetorCcxt("gate", ex).fechar())
    assert ex.fechada is True


def test_iniciar_continua_fechando_a_corretora_no_fim():
    ex = ExchangeDuasEtapas(roteiro={("watch_order_book", "BTC/USDT"): [ob(100.0, 100.1)]})
    recebidos = []
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar(["BTC/USDT"], recebidos.append))
        await esperar_ate(lambda: recebidos)
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert ex.fechada is True
