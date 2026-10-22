"""Testes da Task 18: historico das metricas pelos tickers (especificacao, secao 3, "De onde vem as amostras das
metricas").

Todo par com ticker valido nas duas pontas ganha uma amostra pelos tickers (melhor bid/ask em USDT) a cada
`amostra_ticker_s`, em rodizio; enquanto o par tem livro aberto (e candidato), a amostra vem do livro. As duas
fontes vao para a mesma janela; o lote medio vem so do livro; as amostras pelos tickers ficam so na memoria.
Nenhum teste acessa a internet.
"""

import logging
import math
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import scanner.motor as modulo_motor
from scanner.armazenamento import Banco
from scanner.calculo import spread_abertura, spread_fechamento
from scanner.config.carregar import Config, ErroConfig, carregar_config
from scanner.estado import EstadoMercados
from scanner.filtros import Filtros
from scanner.modelos import Livro
from scanner.motor import Motor, Sessao, chave_de, texto_da_chave
from tests.fabricas import cfg, cfg_com

RAIZ = Path(__file__).resolve().parent.parent

# Com SCANNER_HISTORICO_TICKERS=0 a suite roda com o historico pelos tickers desligado (tests/conftest.py), para
# conferir que os testes de antes continuam passando; os testes daqui precisam dele ligado.
pytestmark = pytest.mark.skipif(os.environ.get("SCANNER_HISTORICO_TICKERS") == "0",
                                reason="historico pelos tickers desligado pela variavel SCANNER_HISTORICO_TICKERS")

MEIO = 0.0005  # meio spread de bid/ask dos tickers e livros de teste (0,05%)


class DadosFixos:
    """Dados lentos falsos: taxa taker de 0,06% em toda ponta e funding desconhecido."""

    def funding(self, corretora, ativo):
        return None

    def taxa_taker(self, corretora, mercado):
        return 0.06


def simbolo_de(mercado: str, ativo: str, cotacao: str = "USDT") -> str:
    return f"{ativo}/{cotacao}" if mercado == "spot" else f"{ativo}/{cotacao}:{cotacao}"


def por_ticker(estado, corretora, mercado, ativo, bid, ask, agora, cotacao="USDT"):
    """So o ticker (etapa 1) da ponta, e a mensagem da corretora em `agora`."""
    estado.atualizar_ticker(corretora, mercado, ativo, bid, ask, agora, 1e6, 0.0,
                            simbolo_original=simbolo_de(mercado, ativo, cotacao), cotacao=cotacao)
    estado.registrar_mensagem(corretora, agora)


def abrir_livro(estado, corretora, mercado, ativo, bid, ask, agora, lote_usd=1000.0):
    """O livro da ponta (um nivel por lado, `lote_usd` em cada um), como o coletor poria na etapa 2."""
    estado.atualizar_livro(Livro(corretora, mercado, simbolo_de(mercado, ativo), [(bid, lote_usd / bid)],
                                 [(ask, lote_usd / ask)], agora, None), ativo=ativo)


def novo_motor(config=None, cotacoes=None, banco=None):
    config = cfg if config is None else config
    estado = EstadoMercados(config.max_silencio_conexao_s, config.max_idade_livro_s)
    return Motor(config, estado, DadosFixos(), banco=banco, cotacoes=lambda: dict(cotacoes or {})), estado


def amostras_de(motor, combo) -> list[tuple]:
    """As amostras da janela da combinacao, (ts, A, F, lote_long, lote_short), lotes None = pelo ticker."""
    historico = motor._historicos.get(chave_de(combo))
    return [] if historico is None else historico.janela.amostras()


def pelo_ticker(amostras) -> list[float]:
    return [ts for ts, _, _, lote, _ in amostras if lote is None]


def do_livro(amostras) -> list[float]:
    return [ts for ts, _, _, lote, _ in amostras if lote is not None]


def total_pelo_ticker(motor) -> int:
    return sum(len(pelo_ticker(h.janela.amostras())) for h in motor._historicos.values())


GATE_MEXC = ("BTC", "gate", "futuro", "mexc", "futuro")
MEXC_GATE = ("BTC", "mexc", "futuro", "gate", "futuro")


def tickers_gate_mexc(estado, agora, meio_gate=100.0, meio_mexc=100.05):
    for corretora, meio in (("gate", meio_gate), ("mexc", meio_mexc)):
        por_ticker(estado, corretora, "futuro", "BTC", meio * (1 - MEIO), meio * (1 + MEIO), agora)


# ---------------------------------------------------------------------------
# O cenario do revisor (revisao-final-nucleo.md, I2; f_medio_so_com_livro.py)
# ---------------------------------------------------------------------------


def test_cenario_do_revisor_rp_bruto_do_spread_recem_aberto_usa_o_f_medio_do_periodo():
    # 4 h calmas sem livro (F ~ -0,15%, A ~ -0,05% < spread_min_livro_pct), depois o spread abre (A ~ +1,15%). O
    # "Arbimax" e A atual + media de F nas ultimas 4 h a cada 5 s, com ou sem livro. Antes: F medio -1,35 e RP bruto
    # -0,20 (so o tempo com livro); agora o F medio e o do periodo inteiro e o RP bruto bate com o do Arbimax dentro
    # de 0,05 ponto percentual (especificacao, secao 9, criterio 1).
    m, estado = novo_motor()
    historico_f = []  # (ts, F) do "Arbimax": todo passo de 5 s, com ou sem livro

    def passo(agora, meio_gate, meio_mexc):
        tickers_gate_mexc(estado, agora, meio_gate, meio_mexc)
        selecao = m.selecao_livros(agora)
        for corretora, meio in (("gate", meio_gate), ("mexc", meio_mexc)):
            if "BTC/USDT:USDT" in selecao.get(corretora, set()):
                abrir_livro(estado, corretora, "futuro", "BTC", meio * (1 - MEIO), meio * (1 + MEIO), agora, 5000.0)
            else:
                estado.remover_livro(corretora, "futuro", "BTC")
        bid_gate, ask_mexc = meio_gate * (1 - MEIO), meio_mexc * (1 + MEIO)
        historico_f.append((agora, (bid_gate - ask_mexc) / bid_gate * 100))
        m.ciclo(agora)

    inicio = 1_000_000.0
    passos_calmos = 4 * 3600 // 5
    for i in range(passos_calmos):
        passo(inicio + i * 5, 100.0, 100.05)
    agora = inicio + (passos_calmos - 1) * 5
    assert m.candidatos(agora) == []  # 4 h sem livro: nenhum candidato, mas as janelas aquecidas pelos tickers
    assert len(amostras_de(m, GATE_MEXC)) >= 4 * 3600 / cfg.amostra_ticker_s - 1

    primeiro = True
    for minutos in (0.5, 3, 10, 60):
        fim = inicio + passos_calmos * 5 + minutos * 60
        while agora + 5 <= fim:
            agora += 5
            passo(agora, 100.0, 101.25)
        c = next(c for c in m.candidatos(agora) if c.ponta_long.corretora == "gate")
        janela = [f for ts, f in historico_f if ts >= agora - 4 * 3600]
        rp_arbimax = c.spread_abertura_pct + sum(janela) / len(janela)
        assert c.spread_abertura_pct == pytest.approx(1.15, abs=0.01)
        if primeiro:  # o livro acabou de abrir: o F medio e o das 4 h (~ -0,15), e nao o F atual (~ -1,35)
            assert c.metricas_periodo.fechamento_medio == pytest.approx(-0.15, abs=0.02)
            assert c.spread_fechamento_pct == pytest.approx(-1.35, abs=0.01)
            primeiro = False
        assert c.rp_bruto_pct == pytest.approx(rp_arbimax, abs=0.05), f"{minutos} min depois de abrir"
        assert c.rp_bruto_pct > c.spread_abertura_pct + c.spread_fechamento_pct + 0.5  # nao e o A + F atual


# ---------------------------------------------------------------------------
# Rodizio: cada par uma vez por amostra_ticker_s, um pedaco por ciclo (sem pico)
# ---------------------------------------------------------------------------

SEIS = [f"c{i}" for i in range(6)]
DOIS_ATIVOS = ("AAA", "BBB")


def tickers_de_todos(estado, agora, corretoras=SEIS, ativos=DOIS_ATIVOS, mercado="futuro"):
    for n, ativo in enumerate(ativos):
        for k, corretora in enumerate(corretoras):
            meio = (10.0 + n) * (1 + 0.0001 * k)
            por_ticker(estado, corretora, mercado, ativo, meio * (1 - MEIO), meio * (1 + MEIO), agora)


def combos_de_todos(corretoras=SEIS, ativos=DOIS_ATIVOS):
    return [(ativo, longa, "futuro", curta, "futuro") for ativo in ativos for longa in corretoras
            for curta in corretoras if longa != curta]


def test_rodizio_amostra_cada_par_uma_vez_por_amostra_ticker_s_sem_pico():
    m, estado = novo_motor(cfg_com(amostra_ticker_s=60))
    combos = combos_de_todos()  # 2 ativos x 6 corretoras x 5 = 60 pares futuro x futuro, sem livro
    assert len(combos) == 60
    antes, por_ciclo = 0, []
    for i in range(361):  # 180 s, um ciclo a cada 0,5 s
        agora = 1000.0 + i * 0.5
        tickers_de_todos(estado, agora)
        m.ciclo(agora)
        total = total_pelo_ticker(m)
        por_ciclo.append(total - antes)
        antes = total
    assert max(por_ciclo) == 1  # 60 pares x 0,5 s / 60 s = 0,5 por ciclo: nunca um monte de uma vez
    for combo in combos:
        horarios = pelo_ticker(amostras_de(m, combo))
        assert len(horarios) == 3, combo  # 180 s / 60 s
        assert all(59.5 <= depois - antes <= 60.5 for antes, depois in zip(horarios, horarios[1:])), horarios


def test_rodizio_depois_de_um_laco_parado_nao_faz_tudo_de_uma_vez():
    # O laco parou 1000 s (PC suspenso): o ciclo seguinte so anda amostra_s de rodizio (60 x 5 / 60 = 5 pares),
    # em vez de amostrar todos os 60 de uma vez. Os que ficaram para tras sao amostrados na volta seguinte.
    m, estado = novo_motor(cfg_com(amostra_ticker_s=60))
    tickers_de_todos(estado, 1000.0)
    m.ciclo(1000.0)
    antes = total_pelo_ticker(m)
    tickers_de_todos(estado, 2000.0)
    m.ciclo(2000.0)
    assert total_pelo_ticker(m) - antes <= 5


# ---------------------------------------------------------------------------
# Uma fonte por vez, o livro com prioridade; ordem de horario; lote medio so do livro
# ---------------------------------------------------------------------------


def rodar_fases(m, estado, fases, ciclo_s=0.5, inicio=1000.0):
    """fases: [(duracao_s, com_livro)]. Tickers sempre; livro (com os precos dos tickers) so nas fases com livro."""
    agora = inicio
    for duracao, com_livro in fases:
        fim = agora + duracao
        while agora < fim:
            tickers_gate_mexc(estado, agora)
            for corretora, meio in (("gate", 100.0), ("mexc", 100.05)):
                if com_livro:
                    abrir_livro(estado, corretora, "futuro", "BTC", meio * (1 - MEIO), meio * (1 + MEIO), agora)
                else:
                    estado.remover_livro(corretora, "futuro", "BTC")
            m.ciclo(agora)
            agora += ciclo_s
    return agora


def test_uma_fonte_por_vez_com_o_livro_tendo_prioridade():
    m, estado = novo_motor(cfg_com(amostra_ticker_s=60))
    rodar_fases(m, estado, [(300, False), (300, True), (300, False)])  # sem livro, com livro, sem livro
    for combo in (GATE_MEXC, MEXC_GATE):
        amostras = amostras_de(m, combo)
        ticker, livro = pelo_ticker(amostras), do_livro(amostras)
        assert ticker and livro
        assert not [ts for ts in ticker if 1300 <= ts < 1600]  # com livro aberto, nenhuma pelo ticker
        assert all(1300 <= ts < 1600 for ts in livro)  # e as do livro so enquanto ele esta aberto
        assert livro[0] == 1300.0  # o livro abriu: a primeira amostra do livro e na hora
        assert all(depois - antes == cfg.amostra_s for antes, depois in zip(livro, livro[1:]))  # a cada amostra_s
        assert [ts for ts in ticker if ts >= 1600]  # o livro fechou: voltam as amostras pelo ticker


def test_troca_entre_livro_e_ticker_mantem_a_ordem_de_horario(caplog):
    # O livro abre e fecha a cada 7 s por 10 min, com amostra pelo ticker a cada 10 s: as duas fontes se alternam
    # o tempo todo e a janela nunca recebe amostra fora de ordem (a janela recusaria com ValueError e o log avisaria).
    m, estado = novo_motor(cfg_com(amostra_ticker_s=10))
    with caplog.at_level(logging.WARNING, logger="scanner"):
        rodar_fases(m, estado, [(7, i % 2 == 1) for i in range(86)])
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    for combo in (GATE_MEXC, MEXC_GATE):
        horarios = [ts for ts, *_ in amostras_de(m, combo)]
        assert pelo_ticker(amostras_de(m, combo)) and do_livro(amostras_de(m, combo))
        assert all(depois > antes for antes, depois in zip(horarios, horarios[1:]))


def test_primeira_amostra_do_livro_e_na_hora_em_que_o_livro_abre():
    # Um par so (spot x futuro) com amostra pelo ticker a cada 5 s. O livro abre meio segundo depois de uma amostra
    # pelo ticker: a primeira do livro e na hora (como quando o par aparece), sem esperar amostra_s, e o lote medio
    # do sinal ja existe.
    m, estado = novo_motor(cfg_com(amostra_ticker_s=5))
    combo = ("BTC", "gate", "spot", "bitget", "futuro")
    for i in range(40):  # no maximo 20 s: com 5 s de rodizio, uma amostra pelo ticker entra antes
        agora = 1000.0 + i * 0.5
        por_ticker(estado, "gate", "spot", "BTC", 99.95, 100.05, agora)
        por_ticker(estado, "bitget", "futuro", "BTC", 100.15, 100.25, agora)
        m.ciclo(agora)
        if pelo_ticker(amostras_de(m, combo)) and pelo_ticker(amostras_de(m, combo))[-1] == agora:
            break  # acabou de entrar uma amostra pelo ticker
    else:
        pytest.fail("nenhuma amostra pelo ticker em 20 s")
    agora += 0.5
    for corretora, mercado, bid, ask in (("gate", "spot", 99.95, 100.05), ("bitget", "futuro", 100.15, 100.25)):
        por_ticker(estado, corretora, mercado, "BTC", bid, ask, agora)
        abrir_livro(estado, corretora, mercado, "BTC", bid, ask, agora)
    c, = m.candidatos(agora)
    assert do_livro(amostras_de(m, combo)) == [agora]
    assert c.ponta_long.lote_medio_usd == pytest.approx(1000.0)


def test_lote_medio_vem_so_das_amostras_do_livro():
    m, estado = novo_motor(cfg_com(amostra_ticker_s=60))
    agora = rodar_fases(m, estado, [(600, False), (20, True)])
    c = next(c for c in m.candidatos(agora - 0.5) if c.ponta_long.corretora == "gate")
    assert len(pelo_ticker(amostras_de(m, GATE_MEXC))) >= 9  # 10 min de amostras pelo ticker, sem lote
    assert c.ponta_long.lote_medio_usd == pytest.approx(1000.0)  # so as do livro ($1000 no melhor nivel)
    assert c.ponta_short.lote_medio_usd == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Quem e amostrado: ticker valido nas duas pontas, mesmas conversoes e regras de dado invalido do motor
# ---------------------------------------------------------------------------


def test_par_com_ticker_so_de_um_lado_nao_e_amostrado():
    m, estado = novo_motor(cfg_com(amostra_ticker_s=10))
    for i in range(61):
        agora = 1000.0 + i * 0.5
        por_ticker(estado, "gate", "futuro", "BTC", 99.95, 100.05, agora)  # BTC so na gate
        for corretora in ("gate", "mexc"):
            por_ticker(estado, corretora, "futuro", "ETH", 9.995, 10.005, agora)
        m.ciclo(agora)
    assert {chave[0] for chave in m._historicos} == {"ETH/USDT"}


@pytest.mark.parametrize("bid", [None, 0.0, -1.0, math.nan, math.inf, "99.9"])
def test_ticker_com_preco_invalido_de_um_lado_nao_e_amostrado(bid, caplog):
    m, estado = novo_motor(cfg_com(amostra_ticker_s=10))
    with caplog.at_level(logging.WARNING, logger="scanner"):
        for i in range(61):
            agora = 1000.0 + i * 0.5
            por_ticker(estado, "gate", "futuro", "BTC", bid, 100.05, agora)
            por_ticker(estado, "mexc", "futuro", "BTC", 99.95, 100.05, agora)
            m.ciclo(agora)
    assert m.quantidade_historicos() == 0  # nas duas direcoes falta um preco (bid do long ou bid do short)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_ticker_velho_nao_e_amostrado():
    # O ticker da gate para de chegar em t=1010 (a conexao segue viva): passou de max_idade_livro_s (30 s), o par
    # nao e amostrado; quando ele volta, o par volta a ser amostrado.
    m, estado = novo_motor(cfg_com(amostra_ticker_s=60))
    for i in range(401):
        agora = 1000.0 + i * 0.5
        if agora <= 1010 or agora >= 1150:
            por_ticker(estado, "gate", "futuro", "BTC", 99.95, 100.05, agora)
        estado.registrar_mensagem("gate", agora)
        por_ticker(estado, "mexc", "futuro", "BTC", 99.95, 100.05, agora)
        m.ciclo(agora)
    horarios = sorted(pelo_ticker(amostras_de(m, GATE_MEXC)) + pelo_ticker(amostras_de(m, MEXC_GATE)))
    assert horarios  # amostrado antes e depois
    assert not [ts for ts in horarios if 1010 + cfg.max_idade_livro_s < ts < 1150]
    assert [ts for ts in horarios if ts >= 1150]


def test_amostra_pelo_ticker_usa_a_cotacao_e_o_preco_por_unidade_do_motor():
    # Ponta em USDC com cotacao 0,99: o A e o F da amostra saem dos precos em USDT, como no candidato.
    m, estado = novo_motor(cfg_com(amostra_ticker_s=10), cotacoes={"USDC": 0.99})
    for i in range(41):
        agora = 1000.0 + i * 0.5
        por_ticker(estado, "hyperliquid", "futuro", "BTC", 101.0, 101.2, agora, cotacao="USDC")
        por_ticker(estado, "gate", "futuro", "BTC", 99.9, 100.0, agora)
        m.ciclo(agora)
    (_, a, f, _, _), *_ = amostras_de(m, ("BTC", "gate", "futuro", "hyperliquid", "futuro"))
    assert a == pytest.approx(spread_abertura(100.0, 101.0 * 0.99))
    assert f == pytest.approx(spread_fechamento(99.9, 101.2 * 0.99))


def test_amostra_pelo_ticker_sem_cotacao_usa_1_para_1_como_o_motor():
    m, estado = novo_motor(cfg_com(amostra_ticker_s=10))
    for i in range(41):
        agora = 1000.0 + i * 0.5
        por_ticker(estado, "hyperliquid", "futuro", "BTC", 101.0, 101.2, agora, cotacao="USDC")
        por_ticker(estado, "gate", "futuro", "BTC", 99.9, 100.0, agora)
        m.ciclo(agora)
    (_, a, f, _, _), *_ = amostras_de(m, ("BTC", "gate", "futuro", "hyperliquid", "futuro"))
    assert (a, f) == (pytest.approx(spread_abertura(100.0, 101.0)), pytest.approx(spread_fechamento(99.9, 101.2)))


def test_spot_x_futuro_tambem_e_amostrado_pelos_tickers():
    m, estado = novo_motor(cfg_com(amostra_ticker_s=10))
    for i in range(41):
        agora = 1000.0 + i * 0.5
        por_ticker(estado, "gate", "spot", "BTC", 99.9, 100.0, agora)
        por_ticker(estado, "bitget", "futuro", "BTC", 100.1, 100.2, agora)
        m.ciclo(agora)
    assert pelo_ticker(amostras_de(m, ("BTC", "gate", "spot", "bitget", "futuro")))
    assert m.quantidade_historicos() == 1  # o spot so e o long: a outra direcao nao existe


# ---------------------------------------------------------------------------
# So na memoria: nada no banco; a recarga traz as do livro e as dos tickers recomecam
# ---------------------------------------------------------------------------


def test_amostras_pelos_tickers_nao_vao_ao_banco(tmp_path):
    banco = Banco(str(tmp_path / "t.db"))
    try:
        m, estado = novo_motor(cfg_com(amostra_ticker_s=60), banco=banco)
        rodar_fases(m, estado, [(300, False), (30, True)])
        gravadas = {texto: [ts for ts, *_ in linhas] for texto, linhas in banco.amostras_por_combinacao(0)}
        for combo in (GATE_MEXC, MEXC_GATE):
            na_memoria = amostras_de(m, combo)
            assert pelo_ticker(na_memoria)  # a janela tem as pelo ticker...
            assert gravadas[texto_da_chave(chave_de(combo))] == do_livro(na_memoria)  # ...o banco so as do livro
    finally:
        banco.fechar()


def gravar_do_livro(banco, combo, horarios, f=-1.0):
    banco.gravar_amostras([(texto_da_chave(chave_de(combo)), ts, 1.0, f, 100.0, 101.0, 500.0, 500.0)
                           for ts in horarios])


def test_recarga_ao_trocar_o_periodo_traz_as_do_livro_e_as_dos_tickers_recomecam(tmp_path):
    banco = Banco(str(tmp_path / "t.db"))
    try:
        agora = 50_000.0
        gravar_do_livro(banco, GATE_MEXC, [agora - 3 * 3600, agora - 1800])
        m, estado = novo_motor(cfg_com(amostra_ticker_s=60), banco=banco)
        rodar_fases(m, estado, [(600, False)], inicio=agora - 600)  # 10 min pelos tickers, na memoria
        assert len(pelo_ticker(amostras_de(m, GATE_MEXC))) >= 9
        m.sessao = Sessao(filtros=Filtros(periodo_metricas_h=1))
        tickers_gate_mexc(estado, agora)
        m.ciclo(agora)  # o periodo mudou: as janelas recomecam e a recarga (1 h) comeca
        assert m.esperar_recarga(timeout=5)
        rodar_fases(m, estado, [(150, False)], inicio=agora + 0.5)
        amostras = amostras_de(m, GATE_MEXC)
        assert do_livro(amostras) == [agora - 1800]  # do banco, dentro de 1 h (a de 3 h atras ficou fora)
        assert pelo_ticker(amostras) and min(pelo_ticker(amostras)) >= agora  # as pelo ticker recomecaram
    finally:
        banco.fechar()


def test_recarga_demorada_junta_as_amostras_pelos_tickers_feitas_enquanto_ela_lia_o_banco(tmp_path):
    # O banco fica preso por outra leitura: a recarga (numa thread) espera, e o motor segue amostrando pelos
    # tickers. Quando ela termina, a janela tem as do livro (do banco) e as pelos tickers feitas no meio.
    banco = Banco(str(tmp_path / "t.db"))
    try:
        agora = 50_000.0
        gravar_do_livro(banco, GATE_MEXC, [agora - 1800])
        m, estado = novo_motor(cfg_com(amostra_ticker_s=60), banco=banco)
        soltar = threading.Event()
        segurando = threading.Event()

        def segurar():
            with banco._trava:
                segurando.set()
                soltar.wait(5)

        t = threading.Thread(target=segurar)
        t.start()
        try:
            segurando.wait(5)
            m.recarregar_do_banco(agora)
            rodar_fases(m, estado, [(90, False)], inicio=agora)  # 90 s pelos tickers enquanto o banco esta preso
            durante = pelo_ticker(amostras_de(m, GATE_MEXC))
            assert durante and not do_livro(amostras_de(m, GATE_MEXC))
        finally:
            soltar.set()
            t.join()
        assert m.esperar_recarga(timeout=5)
        tickers_gate_mexc(estado, agora + 90)
        m.ciclo(agora + 90)  # junta a recarga
        amostras = amostras_de(m, GATE_MEXC)
        assert do_livro(amostras) == [agora - 1800]
        assert pelo_ticker(amostras)[:len(durante)] == durante  # as feitas durante a recarga continuam
    finally:
        banco.fechar()


def test_recarga_ao_ligar_junta_as_do_banco_com_as_novas_pelos_tickers(tmp_path):
    banco = Banco(str(tmp_path / "t.db"))
    try:
        agora = 50_000.0
        gravar_do_livro(banco, GATE_MEXC, [agora - 3600, agora - 1800], f=-2.0)
        m, estado = novo_motor(cfg_com(amostra_ticker_s=60), banco=banco)
        m.recarregar_do_banco(agora)
        assert m.esperar_recarga(timeout=5)
        rodar_fases(m, estado, [(150, False)], inicio=agora)
        amostras = amostras_de(m, GATE_MEXC)
        assert do_livro(amostras) == [agora - 3600, agora - 1800]
        assert len(pelo_ticker(amostras)) >= 2 and min(pelo_ticker(amostras)) >= agora
    finally:
        banco.fechar()


# ---------------------------------------------------------------------------
# Liga/desliga, teto de memoria, par que some e o modo Farm
# ---------------------------------------------------------------------------


def test_historico_tickers_desligado_nao_amostra_pelos_tickers():
    m, estado = novo_motor(cfg_com(amostra_ticker_s=60, historico_tickers=False))
    rodar_fases(m, estado, [(300, False)])
    assert m.quantidade_historicos() == 0  # como antes: sem livro, sem historico
    rodar_fases(m, estado, [(60, True), (60, False)], inicio=1300.0)
    for combo in (GATE_MEXC, MEXC_GATE):
        amostras = amostras_de(m, combo)
        assert amostras and not pelo_ticker(amostras)


def test_testes_antigos_do_motor_passam_com_o_historico_pelos_tickers_desligado():
    # SCANNER_HISTORICO_TICKERS=0 (tests/conftest.py) roda os testes de antes da Task 18 com historico_tickers
    # desligado: o comportamento de antes, sem mudar nenhum teste.
    ambiente = {**os.environ, "SCANNER_HISTORICO_TICKERS": "0"}
    resultado = subprocess.run([sys.executable, "-W", "error", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                "tests/test_motor.py"], cwd=RAIZ, env=ambiente, capture_output=True, text=True,
                               timeout=300)
    assert resultado.returncode == 0, resultado.stdout[-3000:] + resultado.stderr[-3000:]
    assert " passed" in resultado.stdout and "failed" not in resultado.stdout


def test_teto_de_historicos_avisa_uma_vez_e_os_mais_antigos_sem_livro_saem(caplog):
    combos = combos_de_todos(corretoras=["c0", "c1", "c2"])  # 12 pares
    m, estado = novo_motor(cfg_com(amostra_ticker_s=60, max_historicos=4))
    corretoras = ["c0", "c1", "c2"]
    with caplog.at_level(logging.WARNING, logger="scanner.motor"):
        primeira_volta = None
        for i in range(361):  # 180 s: tres voltas do rodizio
            agora = 1000.0 + i * 0.5
            tickers_de_todos(estado, agora, corretoras)
            m.ciclo(agora)
            if i == 160:  # 80 s: depois da primeira volta inteira
                primeira_volta = {c for c in combos if chave_de(c) in m._historicos}
        assert m.quantidade_historicos() == 4  # o teto: os outros 8 pares ficam sem historico pelos tickers
        com_historico = {c for c in combos if chave_de(c) in m._historicos}
        # Os 4 do teto ficam (e acumulam uma amostra por volta): no teto o rodizio nao cria historico novo para
        # depois apagar outro, o que deixaria todas as janelas com uma amostra so.
        assert com_historico == primeira_volta
        assert all(len(pelo_ticker(amostras_de(m, c))) >= 2 for c in com_historico)
        mais_antigo = min(com_historico, key=lambda c: m._historicos[chave_de(c)].ultima_amostra)
        # O livro abre num par sem historico: ele vira candidato e ganha o seu; o mais antigo sem livro sai.
        sem = next(c for c in combos if c not in com_historico)
        ativo, longa, _, curta, _ = sem
        for corretora in (longa, curta):
            ticker = estado.obter_ticker(corretora, "futuro", ativo, agora)
            abrir_livro(estado, corretora, "futuro", ativo, ticker["bid"], ticker["ask"], agora)
        candidatos = {c.chave for c in m.candidatos(agora)}
        assert chave_de(sem) in candidatos and chave_de(sem) in m._historicos
        assert m.quantidade_historicos() == 4
        assert chave_de(mais_antigo) not in m._historicos
        for i in range(1, 121):  # mais 60 s: o teto continua valendo, sem aviso novo
            tickers_de_todos(estado, agora + i * 0.5, corretoras)
            m.ciclo(agora + i * 0.5)
    avisos = [r.getMessage() for r in caplog.records if "max_historicos" in r.getMessage()]
    assert len(avisos) == 1
    assert m.quantidade_historicos() <= 4 + len(candidatos)


def test_historico_do_par_que_some_e_apagado_depois_do_periodo(monkeypatch):
    # Com muitos historicos a limpeza anda aos poucos (um pedaco por ciclo); aqui o pedaco minimo fica em 1 para
    # o teste passar por esse caminho com poucos pares.
    monkeypatch.setattr(modulo_motor, "VARREDURA_MINIMA", 1)
    m, estado = novo_motor(cfg_com(amostra_ticker_s=60, periodo_metricas_h=0.1))  # periodo de 6 min
    corretoras = ["c0", "c1", "c2"]
    agora = 1000.0
    for i in range(240):  # 2 min com os tickers chegando
        agora = 1000.0 + i * 0.5
        tickers_de_todos(estado, agora, corretoras)
        m.ciclo(agora)
    assert m.quantidade_historicos() == 12
    estado.marcar_desconectada("c2")  # a c2 sai: os 8 pares com ela somem do rodizio
    for i in range(1, 1201):  # mais 10 min so com c0 e c1
        tickers_de_todos(estado, agora + i * 0.5, ["c0", "c1"])
        m.ciclo(agora + i * 0.5)
    # Chave do sinal: (par, tipo, corretora do long, mercado do long, corretora do short, mercado do short).
    assert {(chave[2], chave[4]) for chave in m._historicos} == {("c0", "c1"), ("c1", "c0")}


def test_modo_farm_continua_aquecendo_as_combinacoes_sem_ponta_de_farm_pelos_tickers():
    # No Farm o calculo so passa pelas combinacoes com ponta de farm; as outras, mesmo com livro aberto, nao sao
    # candidatas e seguem amostradas pelos tickers (o F medio delas fica pronto para quando o modo voltar).
    m, estado = novo_motor(cfg_com(amostra_ticker_s=10, corretoras_farm=["lighter"]))
    m.sessao = Sessao(modo="farm")
    for i in range(61):
        agora = 1000.0 + i * 0.5
        tickers_gate_mexc(estado, agora)
        por_ticker(estado, "lighter", "futuro", "BTC", 99.95, 100.05, agora)
        for corretora, meio in (("gate", 100.0), ("mexc", 100.05)):
            abrir_livro(estado, corretora, "futuro", "BTC", meio * (1 - MEIO), meio * (1 + MEIO), agora)
        m.ciclo(agora)
    assert {c.chave for c in m.candidatos(agora)} == set()  # nenhuma com ponta de farm tem livro
    for combo in (GATE_MEXC, MEXC_GATE):
        amostras = amostras_de(m, combo)
        assert pelo_ticker(amostras) and not do_livro(amostras)


# ---------------------------------------------------------------------------
# Config, README e config.yaml
# ---------------------------------------------------------------------------


def test_campos_novos_com_os_padroes():
    padrao = Config()
    assert padrao.historico_tickers is True
    assert padrao.amostra_ticker_s == 120
    assert padrao.max_historicos == 80_000


def test_campos_novos_lidos_do_yaml(tmp_path):
    caminho = tmp_path / "config.yaml"
    caminho.write_text("historico_tickers: false\namostra_ticker_s: 60\nmax_historicos: 5000\n", encoding="utf-8")
    lida = carregar_config(str(caminho))
    assert (lida.historico_tickers, lida.amostra_ticker_s, lida.max_historicos) == (False, 60, 5000)


@pytest.mark.parametrize("texto, campo", [
    ("historico_tickers: 1", "historico_tickers"),
    ("historico_tickers: sim", "historico_tickers"),
    ("amostra_ticker_s: 0", "amostra_ticker_s"),
    ("amostra_ticker_s: -60", "amostra_ticker_s"),
    ("max_historicos: 0", "max_historicos"),
    ("max_historicos: 10.5", "max_historicos"),
    ("amostra_ticker_s: 2", "amostra_ticker_s"),  # menor que amostra_s (5): o ticker amostraria mais que o livro
])
def test_campos_novos_invalidos_dao_erro_claro(tmp_path, texto, campo):
    caminho = tmp_path / "config.yaml"
    caminho.write_text(texto + "\n", encoding="utf-8")
    with pytest.raises(ErroConfig, match=campo) as erro:
        carregar_config(str(caminho))
    assert "desconhecido" not in str(erro.value)  # o campo existe: o erro e do valor


def test_readme_explica_as_metricas_pelos_tickers_e_os_historicos_da_linha_de_recursos():
    texto = (RAIZ / "README.md").read_text(encoding="utf-8")
    painel = texto[texto.index("\n## 5. "):texto.index("\n## 6. ")]
    assert "pelos tickers" in painel and "melhor bid/ask" in painel
    assert "o gráfico só mostra as amostras do livro" in painel
    assert "historicos N" in texto and "max_historicos" in texto
