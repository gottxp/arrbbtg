"""Testes do coletor ccxt e da ferramenta imprimir_precos.

Nenhum teste acessa a internet: a corretora do ccxt e trocada por um objeto
falso (ExchangeFalsa) que devolve livros prontos.
"""

import asyncio
import logging
import time

import pytest

from scanner.coletores.base import Coletor
from scanner.coletores.ccxt_coletor import (
    ColetorCcxt,
    criar_exchange_ccxt,
    livro_de_ccxt,
    mercado_do_simbolo,
)
from scanner.ferramentas.imprimir_precos import (
    formatar_linha,
    imprimir_precos,
    main,
    simbolos_padrao,
    texto_preco,
)
from scanner.modelos import Livro


# ---------------------------------------------------------------------------
# livro_de_ccxt: transforma o livro do ccxt em Livro (ou descarta)
# ---------------------------------------------------------------------------

def test_livro_valido():
    ob = {"bids": [[89.10, 2.0]], "asks": [[89.14, 1.5]], "timestamp": 1760972652000}
    l = livro_de_ccxt("bitget", "futuro", "AKAM/USDT:USDT", ob, agora=1760972652.5)
    assert l.bids[0] == (89.10, 2.0) and l.asks[0] == (89.14, 1.5)
    assert l.ts_corretora == 1760972652.0


def test_livro_cruzado_descartado():
    ob = {"bids": [[90.0, 1]], "asks": [[89.0, 1]], "timestamp": None}
    assert livro_de_ccxt("bitget", "spot", "X/USDT", ob, agora=0) is None


def test_livro_vazio_descartado():
    assert livro_de_ccxt("bitget", "spot", "X/USDT", {"bids": [], "asks": []}, agora=0) is None


def test_livro_com_so_um_lado_descartado():
    assert livro_de_ccxt("bitget", "spot", "X/USDT", {"bids": [[1.0, 1]], "asks": []}, agora=0) is None
    assert livro_de_ccxt("bitget", "spot", "X/USDT", {"bids": [], "asks": [[1.0, 1]]}, agora=0) is None


def test_livro_com_bid_igual_ao_ask_descartado():
    ob = {"bids": [[89.0, 1]], "asks": [[89.0, 1]]}
    assert livro_de_ccxt("bitget", "spot", "X/USDT", ob, agora=0) is None


@pytest.mark.parametrize(
    "bids, asks",
    [
        ([[0.0, 1]], [[1.0, 1]]),  # melhor bid zerado
        ([[1.0, 1]], [[0.0, 1]]),  # melhor ask zerado
        ([[-1.0, 1]], [[1.0, 1]]),  # preco negativo
        ([[2.0, 1], [0.0, 1]], [[3.0, 1]]),  # zero escondido num nivel mais fundo
        ([[2.0, 1]], [[3.0, 1], [0.0, 1]]),
    ],
)
def test_livro_com_preco_zero_ou_negativo_descartado(bids, asks):
    assert livro_de_ccxt("bitget", "spot", "X/USDT", {"bids": bids, "asks": asks}, agora=0) is None


def test_livro_preenche_os_campos_de_identificacao():
    ob = {"bids": [[10.0, 1]], "asks": [[10.5, 1]], "timestamp": None}
    l = livro_de_ccxt("gate", "spot", "ETH/USDT", ob, agora=123.5)
    assert (l.corretora, l.mercado, l.simbolo_original) == ("gate", "spot", "ETH/USDT")
    assert l.recebido_em == 123.5
    assert l.ts_corretora is None  # a corretora nao informou o horario


def test_livro_guarda_todos_os_niveis_na_ordem_recebida():
    ob = {
        "bids": [[10.0, 1], [9.9, 2], [9.8, 3]],
        "asks": [[10.1, 4], [10.2, 5]],
    }
    l = livro_de_ccxt("bitget", "spot", "X/USDT", ob, agora=0)
    assert l.bids == [(10.0, 1.0), (9.9, 2.0), (9.8, 3.0)]
    assert l.asks == [(10.1, 4.0), (10.2, 5.0)]


def test_livro_aceita_nivel_com_campo_extra():
    # Algumas corretoras mandam [preco, quantidade, algo_a_mais]; so os dois primeiros valem.
    ob = {"bids": [[10.0, 1, "id-1"]], "asks": [[10.1, 2, "id-2"]]}
    l = livro_de_ccxt("bitget", "spot", "X/USDT", ob, agora=0)
    assert l.bids == [(10.0, 1.0)] and l.asks == [(10.1, 2.0)]


def test_livro_e_uma_copia_do_que_o_ccxt_entregou():
    # O ccxt reaproveita e altera o mesmo livro a cada mensagem; o Livro nao pode mudar junto.
    ob = {"bids": [[10.0, 1]], "asks": [[10.1, 1]]}
    l = livro_de_ccxt("bitget", "spot", "X/USDT", ob, agora=0)
    ob["bids"][0][0] = 99.0
    ob["bids"].append([8.0, 1])
    assert l.bids == [(10.0, 1.0)]


# ---------------------------------------------------------------------------
# mercado_do_simbolo: spot ou futuro, pelo nome do simbolo no ccxt
# ---------------------------------------------------------------------------

def test_mercado_do_simbolo():
    assert mercado_do_simbolo("BTC/USDT") == "spot"
    assert mercado_do_simbolo("BTC/USDT:USDT") == "futuro"


# ---------------------------------------------------------------------------
# ColetorCcxt.iniciar, com uma corretora falsa
# ---------------------------------------------------------------------------

class ErroDaCorretora(Exception):
    """Erro inventado pelo teste para simular uma falha de rede."""


class ExchangeFalsa:
    """Imita o objeto da corretora do ccxt.

    `watch_order_book` devolve, na ordem, os livros prontos de cada simbolo.
    Quando os livros acabam, fica esperando para sempre (como o ccxt de
    verdade espera a proxima mensagem) ou, se `erro_no_fim` for dado,
    levanta esse erro. O teste encerra o coletor cancelando a tarefa.
    """

    def __init__(self, livros_por_simbolo, erro_no_fim=None):
        self.livros = {s: list(fila) for s, fila in livros_por_simbolo.items()}
        self.erro_no_fim = erro_no_fim
        self.fechada = False
        self.simbolos_pedidos = []

    async def watch_order_book(self, simbolo, limit=None):
        self.simbolos_pedidos.append(simbolo)
        await asyncio.sleep(0)  # cede a vez, como uma chamada de rede de verdade
        fila = self.livros[simbolo]
        if fila:
            return fila.pop(0)
        if self.erro_no_fim is not None:
            raise self.erro_no_fim
        await asyncio.sleep(3600)

    async def close(self):
        self.fechada = True


def ob(bid, ask, ts=None):
    """Livro no formato do ccxt com um nivel de cada lado."""
    return {"bids": [[bid, 1.0]], "asks": [[ask, 1.0]], "timestamp": ts}


async def coletar(coletor, simbolos, quantidade):
    """Roda o coletor ate chegarem `quantidade` livros; depois cancela e devolve os livros."""
    recebidos = []
    chegou_tudo = asyncio.Event()

    def ao_atualizar(livro):
        recebidos.append(livro)
        if len(recebidos) >= quantidade:
            chegou_tudo.set()

    tarefa = asyncio.create_task(coletor.iniciar(simbolos, ao_atualizar))
    await asyncio.wait_for(chegou_tudo.wait(), timeout=2)
    tarefa.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarefa
    return recebidos


def test_coletor_ccxt_e_um_coletor():
    assert issubclass(ColetorCcxt, Coletor)
    assert ColetorCcxt("bitget", ExchangeFalsa({})).nome == "bitget"


def test_iniciar_entrega_livros_de_spot_e_futuro():
    exchange = ExchangeFalsa(
        {
            "BTC/USDT": [ob(100.0, 100.1, ts=1760972652000)],
            "BTC/USDT:USDT": [ob(100.2, 100.3)],
        }
    )
    coletor = ColetorCcxt("bitget", exchange)
    antes = time.time()
    recebidos = asyncio.run(coletar(coletor, ["BTC/USDT", "BTC/USDT:USDT"], quantidade=2))
    depois = time.time()

    por_simbolo = {l.simbolo_original: l for l in recebidos}
    spot, futuro = por_simbolo["BTC/USDT"], por_simbolo["BTC/USDT:USDT"]
    assert isinstance(spot, Livro)
    assert (spot.corretora, spot.mercado) == ("bitget", "spot")
    assert (futuro.corretora, futuro.mercado) == ("bitget", "futuro")
    assert spot.bids[0] == (100.0, 1.0) and spot.asks[0] == (100.1, 1.0)
    assert spot.ts_corretora == 1760972652.0 and futuro.ts_corretora is None
    assert antes <= spot.recebido_em <= depois  # horario local de chegada


def test_iniciar_entrega_cada_atualizacao_na_ordem():
    exchange = ExchangeFalsa({"BTC/USDT": [ob(100.0, 100.1), ob(100.1, 100.2), ob(100.2, 100.3)]})
    recebidos = asyncio.run(coletar(ColetorCcxt("bitget", exchange), ["BTC/USDT"], quantidade=3))
    assert [l.bids[0][0] for l in recebidos] == [100.0, 100.1, 100.2]


def test_iniciar_so_entrega_livro_valido():
    exchange = ExchangeFalsa(
        {
            "BTC/USDT": [
                ob(100.0, 100.1),
                ob(101.0, 100.0),  # cruzado: descartado
                {"bids": [], "asks": []},  # vazio: descartado
                ob(100.2, 100.3),
            ]
        }
    )
    recebidos = asyncio.run(coletar(ColetorCcxt("bitget", exchange), ["BTC/USDT"], quantidade=2))
    assert [l.bids[0][0] for l in recebidos] == [100.0, 100.2]


def test_iniciar_registra_no_log_o_livro_descartado(caplog):
    exchange = ExchangeFalsa({"BTC/USDT": [ob(101.0, 100.0), ob(100.0, 100.1)]})
    with caplog.at_level(logging.DEBUG, logger="scanner.coletores"):
        asyncio.run(coletar(ColetorCcxt("bitget", exchange), ["BTC/USDT"], quantidade=1))
    assert any("BTC/USDT" in m and "descartado" in m for m in caplog.messages)


def test_iniciar_pede_cada_simbolo_a_corretora():
    exchange = ExchangeFalsa({"BTC/USDT": [ob(1.0, 1.1)], "ETH/USDT": [ob(2.0, 2.1)]})
    asyncio.run(coletar(ColetorCcxt("bitget", exchange), ["BTC/USDT", "ETH/USDT"], quantidade=2))
    assert {"BTC/USDT", "ETH/USDT"} <= set(exchange.simbolos_pedidos)


def test_iniciar_fecha_a_corretora_ao_ser_cancelado():
    exchange = ExchangeFalsa({"BTC/USDT": [ob(1.0, 1.1)]})
    asyncio.run(coletar(ColetorCcxt("bitget", exchange), ["BTC/USDT"], quantidade=1))
    assert exchange.fechada


def test_iniciar_com_erro_propaga_e_fecha_a_corretora():
    exchange = ExchangeFalsa({"BTC/USDT": [ob(1.0, 1.1)]}, erro_no_fim=ErroDaCorretora("caiu"))
    coletor = ColetorCcxt("bitget", exchange)
    with pytest.raises(ErroDaCorretora):
        asyncio.run(coletor.iniciar(["BTC/USDT"], lambda livro: None))
    assert exchange.fechada


def test_erro_num_simbolo_interrompe_os_outros_e_fecha():
    # ETH nunca tem livro e fica esperando; BTC falha. O erro tem que aparecer
    # e a tarefa do ETH nao pode ficar solta.
    exchange = ExchangeFalsa({"BTC/USDT": [], "ETH/USDT": []})

    async def watch_com_erro_no_btc(simbolo, limit=None):
        if simbolo == "BTC/USDT":
            await asyncio.sleep(0)
            raise ErroDaCorretora("btc caiu")
        await asyncio.sleep(3600)

    exchange.watch_order_book = watch_com_erro_no_btc
    coletor = ColetorCcxt("bitget", exchange)

    async def cenario():
        await asyncio.wait_for(coletor.iniciar(["BTC/USDT", "ETH/USDT"], lambda livro: None), timeout=2)

    with pytest.raises(ErroDaCorretora):
        asyncio.run(cenario())
    assert exchange.fechada


def test_callback_com_erro_nao_e_engolido():
    exchange = ExchangeFalsa({"BTC/USDT": [ob(1.0, 1.1)]})
    coletor = ColetorCcxt("bitget", exchange)

    def ao_atualizar(livro):
        raise ValueError("bug no consumidor")

    with pytest.raises(ValueError):
        asyncio.run(asyncio.wait_for(coletor.iniciar(["BTC/USDT"], ao_atualizar), timeout=2))
    assert exchange.fechada


def test_criar_exchange_ccxt_monta_a_corretora_sem_acessar_a_rede():
    exchange = criar_exchange_ccxt("bitget")
    try:
        assert exchange.id == "bitget"
        assert exchange.has["watchOrderBook"]
    finally:
        asyncio.run(exchange.close())


def test_criar_exchange_ccxt_com_nome_inexistente():
    with pytest.raises(ValueError, match="corretora_que_nao_existe"):
        criar_exchange_ccxt("corretora_que_nao_existe")


# ---------------------------------------------------------------------------
# imprimir_precos
# ---------------------------------------------------------------------------

def livro_de_teste(simbolo="BTC/USDT:USDT", mercado="futuro", bid=67123.4, ask=67123.5, corretora="bitget"):
    return Livro(
        corretora=corretora,
        mercado=mercado,
        simbolo_original=simbolo,
        bids=[(bid, 1.0)],
        asks=[(ask, 1.0)],
        recebido_em=0.0,
        ts_corretora=None,
    )


def test_texto_preco_nunca_usa_notacao_cientifica():
    assert texto_preco(67123.4) == "67123.4"
    assert texto_preco(0.00000123) == "0.00000123"
    assert texto_preco(89.0) == "89.0"


def test_formatar_linha_mostra_corretora_mercado_simbolo_bid_e_ask():
    linha = formatar_linha(livro_de_teste())
    for parte in ("bitget", "futuro", "BTC/USDT:USDT", "bid 67123.4", "ask 67123.5"):
        assert parte in linha
    assert "\n" not in linha


def test_simbolos_padrao_tem_spot_e_futuro_de_btc_eth_sol():
    assert simbolos_padrao() == [
        "BTC/USDT", "ETH/USDT", "SOL/USDT",
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    ]


class ColetorFalso(Coletor):
    """Entrega livros prontos e depois espera, como um coletor de verdade."""

    def __init__(self, livros, erro_no_fim=None):
        super().__init__("falsa")
        self.livros = livros
        self.erro_no_fim = erro_no_fim
        self.encerrado = False

    async def iniciar(self, simbolos, ao_atualizar):
        try:
            for livro in self.livros:
                ao_atualizar(livro)
            if self.erro_no_fim is not None:
                await asyncio.sleep(0.05)
                raise self.erro_no_fim
            await asyncio.sleep(3600)
        finally:
            self.encerrado = True


def test_imprimir_precos_escreve_o_ultimo_preco_de_cada_simbolo():
    coletor = ColetorFalso(
        [
            livro_de_teste("BTC/USDT", "spot", bid=100.0, ask=100.1),
            livro_de_teste("BTC/USDT", "spot", bid=101.0, ask=101.1),  # este e o mais novo
        ]
    )
    linhas = []

    async def cenario():
        tarefa = asyncio.create_task(
            imprimir_precos([coletor], ["BTC/USDT", "ETH/USDT"], intervalo_s=0.02, escrever=linhas.append)
        )
        await asyncio.sleep(0.1)
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa

    asyncio.run(cenario())
    texto = "\n".join(linhas)
    assert "bid 101.0" in texto and "bid 100.0" not in texto
    assert "ETH/USDT" in texto and "sem dados" in texto  # ainda nao chegou livro do ETH
    assert coletor.encerrado  # o coletor foi encerrado junto


def test_imprimir_precos_mostra_o_erro_do_coletor():
    coletor = ColetorFalso([], erro_no_fim=ErroDaCorretora("caiu"))
    with pytest.raises(ErroDaCorretora):
        asyncio.run(imprimir_precos([coletor], ["BTC/USDT"], intervalo_s=1, escrever=lambda t: None))
    assert coletor.encerrado


def test_main_com_corretora_inexistente_avisa_e_devolve_1(capsys):
    assert main(["corretora_que_nao_existe"]) == 1
    assert "corretora_que_nao_existe" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# imprimir_precos com varias corretoras ao mesmo tempo (marco da Task 4)
# ---------------------------------------------------------------------------

class ColetorFalsoNomeado(ColetorFalso):
    """ColetorFalso com o nome da corretora escolhido pelo teste."""

    def __init__(self, nome, livros, erro_no_fim=None):
        super().__init__(livros, erro_no_fim)
        self.nome = nome


def rodar_imprimir(coletores, simbolos, espera_s=0.1, intervalo_s=0.02):
    """Roda imprimir_precos por `espera_s`, cancela e devolve as linhas escritas."""
    linhas = []

    async def cenario():
        tarefa = asyncio.create_task(imprimir_precos(coletores, simbolos, intervalo_s, escrever=linhas.append))
        await asyncio.sleep(espera_s)
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa

    asyncio.run(cenario())
    return linhas


def test_imprimir_precos_mostra_cada_corretora_com_o_seu_preco():
    mexc = ColetorFalsoNomeado("mexc", [livro_de_teste("BTC/USDT", "spot", bid=100.0, ask=100.1, corretora="mexc")])
    gate = ColetorFalsoNomeado("gate", [livro_de_teste("BTC/USDT", "spot", bid=200.0, ask=200.1, corretora="gate")])
    linhas = rodar_imprimir([mexc, gate], ["BTC/USDT"])
    assert any(l.startswith("mexc") and "bid 100.0" in l for l in linhas)
    assert any(l.startswith("gate") and "bid 200.0" in l for l in linhas)
    assert not any(l.startswith("mexc") and "bid 200.0" in l for l in linhas)  # precos nao se misturam
    assert mexc.encerrado and gate.encerrado


def test_imprimir_precos_corretora_sem_dado_aparece_como_sem_dados():
    mexc = ColetorFalsoNomeado("mexc", [livro_de_teste("BTC/USDT", "spot", corretora="mexc")])
    gate = ColetorFalsoNomeado("gate", [])
    linhas = rodar_imprimir([mexc, gate], ["BTC/USDT"])
    assert any(l.startswith("gate") and "sem dados" in l for l in linhas)


def test_imprimir_precos_corretora_que_caiu_nao_esconde_as_outras():
    mexc = ColetorFalsoNomeado("mexc", [livro_de_teste("BTC/USDT", "spot", bid=100.0, corretora="mexc")])
    gate = ColetorFalsoNomeado("gate", [], erro_no_fim=ErroDaCorretora("gate caiu"))
    linhas = rodar_imprimir([mexc, gate], ["BTC/USDT"], espera_s=0.2)
    assert any(l.startswith("mexc") and "bid 100.0" in l for l in linhas)  # mexc segue sendo mostrada
    assert any(l.startswith("gate") and "parou" in l and "gate caiu" in l for l in linhas)


def test_imprimir_precos_levanta_o_erro_quando_todas_as_corretoras_caem():
    mexc = ColetorFalsoNomeado("mexc", [], erro_no_fim=ErroDaCorretora("mexc caiu"))
    gate = ColetorFalsoNomeado("gate", [], erro_no_fim=ErroDaCorretora("gate caiu"))
    with pytest.raises(ErroDaCorretora):
        asyncio.run(imprimir_precos([mexc, gate], ["BTC/USDT"], intervalo_s=1, escrever=lambda t: None))
    assert mexc.encerrado and gate.encerrado


def test_imprimir_precos_termina_quando_todos_os_coletores_terminam_sem_erro():
    class ColetorQueAcaba(ColetorFalso):
        async def iniciar(self, simbolos, ao_atualizar):
            self.encerrado = True

    asyncio.run(imprimir_precos([ColetorQueAcaba([])], ["BTC/USDT"], intervalo_s=1, escrever=lambda t: None))


def test_main_com_uma_corretora_inexistente_entre_varias_avisa_e_devolve_1(capsys):
    assert main(["bitget", "corretora_que_nao_existe", "gate"]) == 1
    assert "corretora_que_nao_existe" in capsys.readouterr().err
