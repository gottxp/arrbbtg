"""Testes das duas etapas do ColetorCcxt (tickers de todos os pares + livros sob demanda).

Nenhum teste acessa a internet: a corretora do ccxt e trocada por um objeto falso
que entrega, na ordem, as mensagens combinadas no `roteiro` de cada teste.
"""

import asyncio
import logging

import pytest
from ccxt.base.errors import NotSupported

from scanner.coletores.ccxt_coletor import ColetorCcxt, livro_de_ccxt, mercado_do_simbolo
from scanner.estado.mercados import EstadoMercados
from scanner.modelos import Livro, Ticker
from tests.fabricas import market_ccxt


class ErroDaCorretora(Exception):
    """Erro inventado pelo teste para simular uma falha de rede."""


def mercado(simbolo, base, quote="USDT", tamanho=1, **extra):
    """Um market no formato do ccxt (veja tests.fabricas.market_ccxt)."""
    return market_ccxt(simbolo, base, quote, tamanho=tamanho, **extra)


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
    Uma resposta que e uma excecao e levantada; uma funcao async e chamada e o que ela devolve vale. Acabando a fila, a chamada espera
    para sempre (como o ccxt espera a proxima mensagem); o teste cancela no fim.
    """

    def __init__(self, markets=None, has=None, roteiro=None, erro_un_watch=None):
        self.markets = MARKETS if markets is None else markets
        self.has = dict(TUDO) if has is None else has  # sem "unWatchOrderBook", como a Bitget
        self.roteiro = {chave: list(fila) for chave, fila in (roteiro or {}).items()}
        self.erro_un_watch = erro_un_watch
        self.chamadas = []          # (metodo, simbolos pedidos)
        self.livros_cancelados = []  # simbolos cujo watch_order_book foi cancelado
        self.livros_desassinados = []  # simbolos para os quais un_watch_order_book foi chamado
        self.eventos = []           # ordem dos fatos: ("cancelado", simbolo) e ("un_watch", simbolo)
        self.fechada = False

    async def _proximo(self, chave):
        await asyncio.sleep(0)
        fila = self.roteiro.get(chave)
        if fila:
            resposta = fila.pop(0)
            if isinstance(resposta, BaseException):
                raise resposta
            if callable(resposta):  # resposta que o teste libera quando quiser (ex.: espera um Event)
                return await resposta()
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
            self.eventos.append(("cancelado", symbol))
            raise

    async def un_watch_order_book(self, symbol, params=None):
        await asyncio.sleep(0)  # cede a vez, como uma chamada de rede de verdade
        self.livros_desassinados.append(symbol)
        self.eventos.append(("un_watch", symbol))
        if self.erro_un_watch is not None:
            raise self.erro_un_watch

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


# ---------------------------------------------------------------------------
# Etapa 2: sair da lista tambem desfaz a assinatura no ccxt (un_watch_order_book)
# ---------------------------------------------------------------------------

def avisos_do_coletor(caplog):
    """Mensagens de aviso (WARNING ou mais) do proprio coletor, sem o log interno do asyncio."""
    return [r.getMessage() for r in caplog.records if r.name == "scanner.coletores" and r.levelno >= logging.WARNING]


def test_livro_que_saiu_da_lista_e_desassinado_na_corretora():
    # Cancelar a tarefa local nao basta: o ccxt guarda a assinatura ate o un_watch_order_book.
    # O ExchangeDuasEtapas nao tem "unWatchOrderBook" em `has` (como a Bitget): vale o metodo existir.
    ex = ExchangeDuasEtapas()
    coletor = ColetorCcxt("bitget", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT"}, lambda l: None)
        await asyncio.sleep(0.02)
        await coletor.assinar_livros({"ETH/USDT"})
        assert ex.livros_desassinados == ["BTC/USDT"]          # so quem saiu; ETH continua assinado
        assert ex.eventos.index(("cancelado", "BTC/USDT")) < ex.eventos.index(("un_watch", "BTC/USDT"))
        await coletor.assinar_livros(set())
        assert sorted(ex.livros_desassinados) == ["BTC/USDT", "ETH/USDT"]
        await coletor.fechar()

    asyncio.run(cenario())


def test_assinar_livros_com_a_mesma_lista_nao_desassina_ninguem():
    ex = ExchangeDuasEtapas()
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"}, lambda l: None)
        await coletor.assinar_livros({"BTC/USDT"})
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT"})   # so entrou gente
        assert ex.livros_desassinados == []
        await coletor.fechar()

    asyncio.run(cenario())


def test_varios_livros_saindo_juntos_sao_todos_desassinados():
    ex = ExchangeDuasEtapas()
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT", "SOL/USDT"}, lambda l: None)
        await asyncio.sleep(0.02)
        await coletor.assinar_livros({"SOL/USDT"})
        assert sorted(ex.livros_desassinados) == ["BTC/USDT", "ETH/USDT"]
        await coletor.fechar()

    asyncio.run(cenario())


def test_corretora_sem_un_watch_order_book_continua_funcionando():
    ex = ExchangeDuasEtapas()
    ex.un_watch_order_book = None   # objeto sem o metodo
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT"}, lambda l: None)
        await asyncio.sleep(0.02)
        await coletor.assinar_livros({"ETH/USDT"})
        assert set(coletor._tarefas_livro) == {"ETH/USDT"}
        await coletor.fechar()

    asyncio.run(cenario())


def test_un_watch_nao_suportado_avisa_uma_vez_e_nao_tenta_de_novo(caplog):
    # O ccxt base levanta NotSupported nas corretoras sem unWatchOrderBook.
    ex = ExchangeDuasEtapas(erro_un_watch=NotSupported("x unWatchOrderBook() is not supported yet"))
    coletor = ColetorCcxt("xt", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT", "SOL/USDT"}, lambda l: None)
        await asyncio.sleep(0.02)
        await coletor.assinar_livros({"ETH/USDT", "SOL/USDT"})   # BTC sai: tenta e descobre
        await coletor.assinar_livros({"SOL/USDT"})               # ETH sai: nem tenta mais
        assert set(coletor._tarefas_livro) == {"SOL/USDT"}       # a lista segue correta
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner.coletores"):
        asyncio.run(cenario())
    assert ex.livros_desassinados == ["BTC/USDT"]
    avisos = [m for m in avisos_do_coletor(caplog) if "un_watch" in m]
    assert len(avisos) == 1 and "xt" in avisos[0]


def test_erro_no_un_watch_vai_para_o_log_e_nao_impede_os_outros(caplog):
    ex = ExchangeDuasEtapas(erro_un_watch=ErroDaCorretora("sem resposta"))
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT", "SOL/USDT"}, lambda l: None)
        await asyncio.sleep(0.02)
        await coletor.assinar_livros({"SOL/USDT"})               # BTC e ETH saem e os dois un_watch falham
        assert set(coletor._tarefas_livro) == {"SOL/USDT"}
        await coletor.assinar_livros(set())                      # um erro nao desliga o recurso
        assert sorted(ex.livros_desassinados) == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner.coletores"):
        asyncio.run(cenario())
    avisos = [m for m in avisos_do_coletor(caplog) if "sem resposta" in m]
    assert any("BTC/USDT" in m for m in avisos) and any("ETH/USDT" in m for m in avisos)


def test_fechar_nao_precisa_desassinar_porque_o_close_derruba_as_conexoes():
    ex = ExchangeDuasEtapas()
    coletor = ColetorCcxt("gate", ex)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"}, lambda l: None)
        await asyncio.sleep(0.02)
        await coletor.fechar()

    asyncio.run(cenario())
    assert ex.livros_desassinados == [] and ex.fechada is True


# ---------------------------------------------------------------------------
# Estado so recebe spot e perpetuo linear; uma cotacao por ativo (USDT > USDC > USDG)
# ---------------------------------------------------------------------------

def ex_com_markets(markets, roteiro):
    return ExchangeDuasEtapas(markets=markets, roteiro=roteiro)


async def rodar_tickers_ate_tratar(coletor, ex, simbolos, mensagens=1):
    """Roda iniciar_tickers ate a(s) primeira(s) `mensagens` serem tratadas e cancela."""
    tarefa = asyncio.create_task(coletor.iniciar_tickers(simbolos))
    await esperar_ate(lambda: len(ex.chamadas) >= 1 + mensagens)
    await cancelar(tarefa)


MARKETS_GATE_MISTOS = {
    "BTC/USDT:USDT": mercado("BTC/USDT:USDT", "BTC", tamanho=0.001),
    "BTC/USD:BTC": mercado("BTC/USD:BTC", "BTC", quote="USD", tamanho=1, inverso=True),   # inverso: contrato vale 1 USD
    "BTC/USDT:USDT-251226": mercado("BTC/USDT:USDT-251226", "BTC", tipo="future"),
    "BTC/USDT:USDT-251226-100000-C": mercado("BTC/USDT:USDT-251226-100000-C", "BTC", tipo="option"),
}


def test_ticker_so_vai_ao_estado_se_for_perpetuo_linear_ou_spot(caplog):
    mensagem = {
        "BTC/USDT:USDT": tk(100.0, 100.1),
        "BTC/USD:BTC": tk(900.0, 900.1),
        "BTC/USDT:USDT-251226": tk(800.0, 800.1),
        "BTC/USDT:USDT-251226-100000-C": tk(700.0, 700.1),
    }
    ex = ex_com_markets(MARKETS_GATE_MISTOS, {("watch_tickers", "futuro"): [mensagem, mensagem]})
    estado = EstadoMercados()
    recebidos = []
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))
    coletor._ao_ticker = recebidos.append

    async def cenario():
        await rodar_tickers_ate_tratar(coletor, ex, list(mensagem), mensagens=2)

    with caplog.at_level(logging.WARNING, logger="scanner.coletores"):
        asyncio.run(cenario())
    t = estado.obter_ticker("gate", "futuro", "BTC", agora=500.0)
    assert t["bid"] == 100.0 and t["simbolo_original"] == "BTC/USDT:USDT"   # so o linear perpetuo
    assert len(recebidos) == 8                                              # o callback recebe tudo, cru
    avisos = avisos_do_coletor(caplog)
    for descartado in ("BTC/USD:BTC", "BTC/USDT:USDT-251226", "BTC/USDT:USDT-251226-100000-C"):
        assert len([m for m in avisos if f" {descartado}: " in m]) == 1   # 1 aviso por simbolo, mesmo com 2 mensagens


def test_livro_inverso_ou_de_opcao_nao_vai_ao_estado():
    roteiro = {
        ("watch_order_book", "BTC/USD:BTC"): [ob(900.0, 900.1, qtd=1_000_000.0)],       # 1 milhao de contratos de 1 USD
        ("watch_order_book", "BTC/USDT:USDT-251226-100000-C"): [ob(700.0, 700.1)],
        ("watch_order_book", "BTC/USDT:USDT"): [ob(100.0, 100.1, qtd=10.0)],
    }
    ex = ex_com_markets(MARKETS_GATE_MISTOS, roteiro)
    estado = EstadoMercados()
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        await coletor.assinar_livros({"BTC/USD:BTC", "BTC/USDT:USDT-251226-100000-C", "BTC/USDT:USDT"})
        await esperar_ate(lambda: estado.obter_livro("gate", "futuro", "BTC", agora=500.0))
        await asyncio.sleep(0.02)   # tempo para os outros dois entregarem (e serem descartados)
        await coletor.fechar()

    asyncio.run(cenario())
    livro = estado.obter_livro("gate", "futuro", "BTC", agora=500.0)
    assert livro.simbolo_original == "BTC/USDT:USDT"
    assert livro.bids == [(100.0, pytest.approx(0.01))]     # 10 contratos x 0,001 BTC; nada do inverso


def test_ticker_guardado_traz_simbolo_original_e_cotacao():
    ex = ex_com_markets(MARKETS, {("watch_tickers", "futuro"): [{"1000PEPE/USDT:USDT": tk(0.0100, 0.0101)}]})
    estado = EstadoMercados()
    coletor = ColetorCcxt("mexc", ex, estado=estado, relogio=relogio_fixo(500.0))
    asyncio.run(rodar_tickers_ate_tratar(coletor, ex, ["1000PEPE/USDT:USDT"]))
    t = estado.obter_ticker("mexc", "futuro", "PEPE", agora=500.0)
    assert t["simbolo_original"] == "1000PEPE/USDT:USDT" and t["cotacao"] == "USDT"


def markets_com_cotacoes(*cotacoes, derivativo=True):
    """BTC cotado em cada uma das moedas dadas (perpetuo linear, ou spot)."""
    markets = {}
    for cotacao in cotacoes:
        simbolo = f"BTC/{cotacao}:{cotacao}" if derivativo else f"BTC/{cotacao}"
        markets[simbolo] = mercado(simbolo, "BTC", quote=cotacao)
    return markets


def ticker_no_estado_apos(markets, mensagem, simbolos_pedidos, mercado_do_ticker="futuro"):
    """Entrega `mensagem` (simbolo -> ticker) pelo coletor e devolve o ticker de BTC guardado."""
    ex = ex_com_markets(markets, {("watch_tickers", mercado_do_ticker): [mensagem]})
    estado = EstadoMercados()
    coletor = ColetorCcxt("hyperliquid", ex, estado=estado, relogio=relogio_fixo(500.0))
    asyncio.run(rodar_tickers_ate_tratar(coletor, ex, simbolos_pedidos))
    return estado.obter_ticker("hyperliquid", mercado_do_ticker, "BTC", agora=500.0)


def test_mesma_chave_com_duas_cotacoes_fica_com_usdt_em_qualquer_ordem(caplog):
    markets = markets_com_cotacoes("USDC", "USDT")   # USDC primeiro nos markets
    usdc, usdt = "BTC/USDC:USDC", "BTC/USDT:USDT"
    for mensagem in ({usdc: tk(200.0, 200.1), usdt: tk(100.0, 100.1)},    # USDC chega antes
                     {usdt: tk(100.0, 100.1), usdc: tk(200.0, 200.1)}):   # USDT chega antes
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="scanner.coletores"):
            t = ticker_no_estado_apos(markets, mensagem, [usdc, usdt])
        assert t["simbolo_original"] == usdt and t["cotacao"] == "USDT" and t["bid"] == 100.0
        avisos = [m for m in avisos_do_coletor(caplog) if usdc in m]
        assert len(avisos) == 1 and usdt in avisos[0]     # o descartado e logado, citando quem ficou


def test_so_a_cotacao_perdedora_pedida_tambem_e_descartada():
    # A regra vale para o mercado todo, nao so para o que foi pedido: a escolha nao muda com a lista de simbolos.
    markets = markets_com_cotacoes("USDC", "USDT")
    assert ticker_no_estado_apos(markets, {"BTC/USDC:USDC": tk(200.0, 200.1)}, ["BTC/USDC:USDC"]) is None


def test_ordem_de_preferencia_usdt_usdc_usdg_e_depois_as_outras():
    simbolo = lambda c: f"BTC/{c}:{c}"
    cotacoes = ["EUR", "USDG", "USDC", "USDT"]
    while cotacoes:
        markets = markets_com_cotacoes(*cotacoes)
        mensagem = {simbolo(c): tk(100.0, 100.1) for c in cotacoes}
        t = ticker_no_estado_apos(markets, mensagem, list(mensagem))
        esperado = [c for c in ("USDT", "USDC", "USDG", "EUR") if c in cotacoes][0]
        assert t["cotacao"] == esperado
        cotacoes.remove(esperado)


def test_ativo_so_com_usdc_e_aceito():
    markets = markets_com_cotacoes("USDC")
    t = ticker_no_estado_apos(markets, {"BTC/USDC:USDC": tk(100.0, 100.1)}, ["BTC/USDC:USDC"])
    assert t["cotacao"] == "USDC"


def test_preferencia_vale_no_spot_tambem():
    markets = markets_com_cotacoes("USDC", "USDT", derivativo=False)
    mensagem = {"BTC/USDC": tk(200.0, 200.1), "BTC/USDT": tk(100.0, 100.1)}
    t = ticker_no_estado_apos(markets, mensagem, list(mensagem), mercado_do_ticker="spot")
    assert t["simbolo_original"] == "BTC/USDT"


def test_spot_e_futuro_do_mesmo_ativo_nao_disputam_a_chave():
    markets = {"BTC/USDC": mercado("BTC/USDC", "BTC", quote="USDC"),
               "BTC/USDT:USDT": mercado("BTC/USDT:USDT", "BTC")}
    spot = ticker_no_estado_apos(markets, {"BTC/USDC": tk(200.0, 200.1)}, ["BTC/USDC"], mercado_do_ticker="spot")
    assert spot["cotacao"] == "USDC"   # o USDT e futuro: outra chave


def test_mesmo_ativo_e_cotacao_desempata_pelo_simbolo_em_qualquer_ordem():
    # 1000PEPE e PEPE no mesmo futuro: precos normalizados iguais, mas niveis diferentes; so um ocupa a chave.
    markets = {"PEPE/USDT:USDT": mercado("PEPE/USDT:USDT", "PEPE"),
               "1000PEPE/USDT:USDT": mercado("1000PEPE/USDT:USDT", "1000PEPE")}
    for mensagem in ({"PEPE/USDT:USDT": tk(0.00001, 0.0000101), "1000PEPE/USDT:USDT": tk(0.0100, 0.0101)},
                     {"1000PEPE/USDT:USDT": tk(0.0100, 0.0101), "PEPE/USDT:USDT": tk(0.00001, 0.0000101)}):
        ex = ex_com_markets(markets, {("watch_tickers", "futuro"): [mensagem]})
        estado = EstadoMercados()
        coletor = ColetorCcxt("mexc", ex, estado=estado, relogio=relogio_fixo(500.0))
        asyncio.run(rodar_tickers_ate_tratar(coletor, ex, list(mensagem)))
        assert estado.obter_ticker("mexc", "futuro", "PEPE", agora=500.0)["simbolo_original"] == "1000PEPE/USDT:USDT"


def test_livro_da_cotacao_perdedora_nao_sobrescreve_o_da_vencedora():
    markets = markets_com_cotacoes("USDC", "USDT")
    roteiro = {
        ("watch_order_book", "BTC/USDC:USDC"): [ob(200.0, 200.1), ob(201.0, 201.1)],
        ("watch_order_book", "BTC/USDT:USDT"): [ob(100.0, 100.1)],
    }
    ex = ex_com_markets(markets, roteiro)
    estado = EstadoMercados()
    coletor = ColetorCcxt("hyperliquid", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        await coletor.assinar_livros({"BTC/USDC:USDC", "BTC/USDT:USDT"})
        await esperar_ate(lambda: estado.obter_livro("hyperliquid", "futuro", "BTC", agora=500.0))
        await asyncio.sleep(0.05)   # sobra tempo para o USDC entregar os dois livros dele
        await coletor.fechar()

    asyncio.run(cenario())
    livro = estado.obter_livro("hyperliquid", "futuro", "BTC", agora=500.0)
    assert livro.simbolo_original == "BTC/USDT:USDT" and livro.bids[0][0] == 100.0


def test_markets_recarregados_refazem_a_escolha():
    liberar = asyncio.Event()

    async def segunda_mensagem():
        await liberar.wait()   # so chega depois de o teste trocar os markets
        return {"BTC/USDC:USDC": tk(200.0, 200.1), "BTC/USDT:USDT": tk(100.0, 100.1)}

    ex = ex_com_markets(markets_com_cotacoes("USDC"), {("watch_tickers", "futuro"): [
        {"BTC/USDC:USDC": tk(200.0, 200.1)},
        segunda_mensagem,
    ]})
    estado = EstadoMercados()
    coletor = ColetorCcxt("hyperliquid", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        tarefa = asyncio.create_task(coletor.iniciar_tickers(["BTC/USDC:USDC", "BTC/USDT:USDT"]))
        await esperar_ate(lambda: len(ex.chamadas) >= 2)           # 1a mensagem tratada: so existe USDC
        assert estado.obter_ticker("hyperliquid", "futuro", "BTC", agora=500.0)["cotacao"] == "USDC"
        ex.markets = markets_com_cotacoes("USDC", "USDT")           # a corretora listou o USDT (markets novos)
        liberar.set()
        await esperar_ate(lambda: len(ex.chamadas) >= 3)           # 2a mensagem tratada
        await cancelar(tarefa)

    asyncio.run(cenario())
    assert estado.obter_ticker("hyperliquid", "futuro", "BTC", agora=500.0)["cotacao"] == "USDT"


def test_simbolos_por_chave_um_simbolo_por_mercado_e_ativo():
    # O __main__ (Task 10) pede tickers exatamente destes simbolos: os que alimentam o estado.
    markets = {
        **markets_com_cotacoes("USDC", "USDT"),
        **MARKETS_GATE_MISTOS,
        "BTC/USDC": mercado("BTC/USDC", "BTC", quote="USDC"),
        "1000PEPE/USDT:USDT": mercado("1000PEPE/USDT:USDT", "1000PEPE"),
    }
    coletor = ColetorCcxt("gate", ExchangeDuasEtapas(markets=markets))
    assert coletor.simbolos_por_chave() == {
        ("futuro", "BTC"): "BTC/USDT:USDT",
        ("spot", "BTC"): "BTC/USDC",
        ("futuro", "PEPE"): "1000PEPE/USDT:USDT",
    }


def test_simbolos_por_chave_acompanha_markets_recarregados():
    ex = ExchangeDuasEtapas(markets={})
    coletor = ColetorCcxt("gate", ex)
    assert coletor.simbolos_por_chave() == {}
    ex.markets = markets_com_cotacoes("USDT", derivativo=False)
    assert coletor.simbolos_por_chave() == {("spot", "BTC"): "BTC/USDT"}


# ---------------------------------------------------------------------------
# Livro que deixa de chegar sai do estado; un_watch com tempo limite (revisao da Task 10)
# ---------------------------------------------------------------------------

def test_livro_que_sai_da_lista_e_removido_do_estado():
    # Sem isso o ultimo livro continuaria valendo ate 30s (o ticker mantem a conexao viva) e o
    # motor leria um preco congelado.
    ex = ExchangeDuasEtapas(roteiro={("watch_order_book", "BTC/USDT"): [ob(100.0, 100.1)],
                                     ("watch_order_book", "ETH/USDT"): [ob(10.0, 10.1)]})
    estado = EstadoMercados()
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))
    livro_de = lambda ativo: estado.obter_livro("gate", "spot", ativo, agora=500.0)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT"})
        await esperar_ate(lambda: livro_de("BTC") is not None and livro_de("ETH") is not None)
        await coletor.assinar_livros({"ETH/USDT"})
        assert livro_de("BTC") is None and livro_de("ETH") is not None
        await coletor.fechar()

    asyncio.run(cenario())


def test_livro_que_parou_com_erro_e_removido_do_estado():
    ex = ExchangeDuasEtapas(roteiro={("watch_order_book", "BTC/USDT"): [ob(100.0, 100.1), ErroDaCorretora("caiu")]})
    estado = EstadoMercados()
    recebidos = []
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"}, recebidos.append)
        await esperar_ate(lambda: recebidos and "BTC/USDT" not in coletor._tarefas_livro)
        await coletor.fechar()

    asyncio.run(cenario())
    assert recebidos  # o livro chegou e foi guardado antes de a assinatura cair
    assert estado.obter_livro("gate", "spot", "BTC", agora=500.0) is None


def test_tarefa_velha_que_termina_nao_apaga_o_livro_da_tarefa_nova():
    liberar = asyncio.Event()

    async def cai_quando_liberar():
        await liberar.wait()
        raise ErroDaCorretora("a velha caiu")

    ex = ExchangeDuasEtapas(roteiro={("watch_order_book", "BTC/USDT"): [ob(100.0, 100.1), cai_quando_liberar]})
    estado = EstadoMercados()
    coletor = ColetorCcxt("gate", ex, estado=estado, relogio=relogio_fixo(500.0))

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT"})
        await esperar_ate(lambda: estado.obter_livro("gate", "spot", "BTC", agora=500.0) is not None)
        velha = coletor._tarefas_livro["BTC/USDT"]
        nova = asyncio.create_task(asyncio.sleep(3600))  # outra tarefa ja assumiu o simbolo...
        coletor._tarefas_livro["BTC/USDT"] = nova
        estado.atualizar_livro(livro_de_ccxt("gate", "spot", "BTC/USDT", ob(200.0, 200.1), 501.0), ativo="BTC")
        liberar.set()  # ...e so depois a velha termina
        await asyncio.wait([velha])
        assert estado.obter_livro("gate", "spot", "BTC", agora=501.0).bids[0][0] == 200.0
        await coletor.fechar()

    asyncio.run(cenario())


class ExchangeUnWatchSemResposta(ExchangeDuasEtapas):
    """Corretora cujo un_watch_order_book nunca recebe a confirmacao (ex.: Bitget sem ack)."""

    async def un_watch_order_book(self, symbol, params=None):
        self.livros_desassinados.append(symbol)
        await asyncio.sleep(3600)


def test_un_watch_sem_resposta_tem_tempo_limite_avisa_e_segue(caplog):
    ex = ExchangeUnWatchSemResposta(roteiro={("watch_order_book", "BTC/USDT"): [ob(100.0, 100.1)]})
    estado = EstadoMercados()
    coletor = ColetorCcxt("bitget", ex, estado=estado, relogio=relogio_fixo(500.0), timeout_desassinar_s=0.05)

    async def cenario():
        await coletor.assinar_livros({"BTC/USDT", "ETH/USDT"})
        await esperar_ate(lambda: estado.obter_livro("bitget", "spot", "BTC", agora=500.0) is not None)
        await asyncio.wait_for(coletor.assinar_livros({"ETH/USDT"}), timeout=2)  # nao fica preso esperando
        assert ex.livros_desassinados == ["BTC/USDT"]
        assert set(coletor._tarefas_livro) == {"ETH/USDT"}
        assert estado.obter_livro("bitget", "spot", "BTC", agora=500.0) is None  # o livro sai mesmo sem o ack
        await coletor.fechar()

    with caplog.at_level(logging.WARNING, logger="scanner.coletores"):
        asyncio.run(cenario())
    avisos = [m for m in avisos_do_coletor(caplog) if "BTC/USDT" in m]
    assert len(avisos) == 1 and "0.05" in avisos[0]
