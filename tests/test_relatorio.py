"""Testes da Task 17: metricas de validacao (especificacao, secao 9).

`resumo_do_dia` (por modo e por corretora), as linhas do log e a parte do __main__ que grava o atraso e as
reconexoes de cada dia, escreve o resumo do dia anterior na virada e chama a limpeza do banco. Nenhum teste
acessa a internet nem dorme de verdade: o relogio e as esperas sao falsos.
"""

import asyncio
import logging
import threading

import pytest

import scanner.__main__ as principal
from scanner.armazenamento import Banco
from scanner.validacao import dia_utc, linhas_do_resumo, resumo_do_dia
from tests.fabricas import INICIO_20_OUT, EsperaFalsa, banco_com_sinais, cfg_com, iso_utc
from tests.test_motor import config_servico, exchanges_falsas

DIA = 86400
INICIO_21_OUT = INICIO_20_OUT + DIA

# ---------------------------------------------------------------------------
# Brief (Task 17, Passo 1) - verbatim
# ---------------------------------------------------------------------------


def test_duracoes(tmp_path):
    b = banco_com_sinais(tmp_path, duracoes_s=[2, 10, 40], rp_liquido=[0.3, 0.4, 0.5], modo="lucro")
    r = resumo_do_dia(b, "2026-10-20")["lucro"]
    assert r["quantidade_sinais"] == 3
    assert r["duracao_media_s"] == pytest.approx(17.33, abs=0.01)
    assert r["pct_sumiu_5s"] == pytest.approx(33.33, abs=0.01)
    assert r["pct_sumiu_30s"] == pytest.approx(66.67, abs=0.01)
    assert r["rp_liquido_medio_pct"] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# resumo_do_dia: sinais por modo
# ---------------------------------------------------------------------------


@pytest.fixture
def fechar():
    """Fecha no fim do teste os bancos que o teste abrir (fechar(b) devolve o proprio b)."""
    abertos = []

    def guardar(banco):
        abertos.append(banco)
        return banco

    yield guardar
    for banco in abertos:
        banco.fechar()


def test_aberto_conta_na_quantidade_mas_fica_fora_das_duracoes_e_dos_percentuais(tmp_path, fechar):
    b = fechar(banco_com_sinais(tmp_path, duracoes_s=[2, None, 40, None], rp_liquido=[0.3, 0.5, 0.4, 0.6]))
    r = resumo_do_dia(b, "2026-10-20")["lucro"]
    assert r["quantidade_sinais"] == 4 and r["abertos"] == 2
    assert r["duracao_media_s"] == pytest.approx(21.0)  # so os encerrados: (2 + 40) / 2
    assert r["pct_sumiu_5s"] == pytest.approx(50.0) and r["pct_sumiu_30s"] == pytest.approx(50.0)
    assert r["rp_liquido_medio_pct"] == pytest.approx(0.45)  # o RP medio inclui os abertos


def test_so_abertos_nao_tem_duracao_nem_percentual(tmp_path, fechar):
    b = fechar(banco_com_sinais(tmp_path, duracoes_s=[None, None], rp_liquido=[0.3, 0.5]))
    r = resumo_do_dia(b, "2026-10-20")["lucro"]
    assert r["quantidade_sinais"] == 2 and r["abertos"] == 2
    assert r["duracao_media_s"] is None and r["pct_sumiu_5s"] is None and r["pct_sumiu_30s"] is None
    assert r["rp_liquido_medio_pct"] == pytest.approx(0.4)


def test_os_dois_modos_ficam_separados(tmp_path, fechar):
    b = fechar(banco_com_sinais(tmp_path, duracoes_s=[2, 10, 40], rp_liquido=[0.3, 0.4, 0.5], modo="lucro"))
    banco_com_sinais(tmp_path, duracoes_s=[100, 3], rp_liquido=[-0.05, -0.07], modo="farm", banco=b)
    resumo = resumo_do_dia(b, "2026-10-20")
    lucro, farm = resumo["lucro"], resumo["farm"]
    assert lucro["quantidade_sinais"] == 3 and lucro["rp_liquido_medio_pct"] == pytest.approx(0.4)
    assert farm["quantidade_sinais"] == 2 and farm["abertos"] == 0
    assert farm["rp_liquido_medio_pct"] == pytest.approx(-0.06)
    assert farm["duracao_media_s"] == pytest.approx(51.5)
    assert farm["pct_sumiu_5s"] == pytest.approx(50.0) and farm["pct_sumiu_30s"] == pytest.approx(50.0)


def test_dia_sem_sinais_da_zero_e_none_sem_dividir_por_zero(tmp_path, fechar):
    b = fechar(banco_com_sinais(tmp_path, duracoes_s=[2], rp_liquido=[0.3]))  # o sinal e de outro dia
    resumo = resumo_do_dia(b, "2026-10-19")
    vazio = {"quantidade_sinais": 0, "abertos": 0, "rp_liquido_medio_pct": None, "duracao_media_s": None,
             "pct_sumiu_5s": None, "pct_sumiu_30s": None}
    assert resumo == {"dia": "2026-10-19", "lucro": vazio, "farm": vazio, "comparacao": vazio, "corretoras": {}}


def test_modo_sem_sinais_ao_lado_de_um_com_sinais(tmp_path, fechar):
    b = fechar(banco_com_sinais(tmp_path, duracoes_s=[2], rp_liquido=[0.3], modo="farm"))
    resumo = resumo_do_dia(b, "2026-10-20")
    assert resumo["lucro"]["quantidade_sinais"] == 0 and resumo["lucro"]["duracao_media_s"] is None
    assert resumo["farm"]["quantidade_sinais"] == 1


def test_sinal_na_virada_do_dia_conta_no_dia_em_que_foi_detectado(tmp_path, fechar):
    # Nasce as 23:59:50 UTC do dia 20 e some 40 s depois, ja no dia 21.
    b = fechar(banco_com_sinais(tmp_path, duracoes_s=[40], rp_liquido=[0.3], nascimento=INICIO_21_OUT - 10))
    dia_20, dia_21 = resumo_do_dia(b, "2026-10-20")["lucro"], resumo_do_dia(b, "2026-10-21")["lucro"]
    assert dia_20["quantidade_sinais"] == 1 and dia_20["duracao_media_s"] == pytest.approx(40.0)
    assert dia_20["pct_sumiu_30s"] == pytest.approx(0.0)
    assert dia_21["quantidade_sinais"] == 0


def test_o_dia_comeca_e_termina_a_meia_noite_utc(tmp_path, fechar):
    b = fechar(banco_com_sinais(tmp_path, duracoes_s=[1], rp_liquido=[0.3], nascimento=INICIO_20_OUT))  # 00:00:00
    banco_com_sinais(tmp_path, duracoes_s=[1], rp_liquido=[0.3], nascimento=INICIO_21_OUT - 0.001, banco=b)  # 23:59:59.999
    banco_com_sinais(tmp_path, duracoes_s=[1], rp_liquido=[0.3], nascimento=INICIO_21_OUT, banco=b)  # ja e dia 21
    assert resumo_do_dia(b, "2026-10-20")["lucro"]["quantidade_sinais"] == 2
    assert resumo_do_dia(b, "2026-10-21")["lucro"]["quantidade_sinais"] == 1


def test_sumir_em_menos_de_5s_e_de_30s_e_estritamente_menos(tmp_path, fechar):
    b = fechar(banco_com_sinais(tmp_path, duracoes_s=[4.999, 5, 29.999, 30], rp_liquido=[0.3] * 4))
    r = resumo_do_dia(b, "2026-10-20")["lucro"]
    assert r["pct_sumiu_5s"] == pytest.approx(25.0)  # so o de 4,999 s
    assert r["pct_sumiu_30s"] == pytest.approx(75.0)  # 5 s e 29,999 s tambem; 30 s nao


def test_rp_liquido_vazio_fica_fora_da_media(tmp_path, fechar):
    b = fechar(banco_com_sinais(tmp_path, duracoes_s=[2, 3], rp_liquido=[None, 0.5]))
    r = resumo_do_dia(b, "2026-10-20")["lucro"]
    assert r["quantidade_sinais"] == 2 and r["rp_liquido_medio_pct"] == pytest.approx(0.5)


def test_sinal_reaberto_pela_anti_repeticao_volta_a_ser_aberto(tmp_path, fechar):
    from scanner.sinais import Evento

    b = fechar(banco_com_sinais(tmp_path, duracoes_s=[10], rp_liquido=[0.3]))
    registro = b.sinais_do_dia("2026-10-20")[0]["sinal"]
    b.gravar_evento(Evento("novo", {**registro, "encerrado_em": None}, registrar_historico=False))  # voltou logo
    r = resumo_do_dia(b, "2026-10-20")["lucro"]
    assert r["quantidade_sinais"] == 1 and r["abertos"] == 1 and r["duracao_media_s"] is None


@pytest.mark.parametrize("dia", ["2026-13-01", "2026-02-30", "ontem", ""])
def test_dia_invalido_levanta_value_error(tmp_path, fechar, dia):
    b = fechar(Banco(str(tmp_path / "s.db")))
    with pytest.raises(ValueError):
        resumo_do_dia(b, dia)


def test_dia_utc():
    assert dia_utc(INICIO_20_OUT) == "2026-10-20"
    assert dia_utc(INICIO_21_OUT - 0.001) == "2026-10-20"
    assert dia_utc(INICIO_21_OUT) == "2026-10-21"
    assert dia_utc(0) == "1970-01-01"


# ---------------------------------------------------------------------------
# resumo_do_dia: corretoras (atraso medio e reconexoes do dia, gravados no banco)
# ---------------------------------------------------------------------------


def test_corretoras_vem_do_banco(tmp_path, fechar):
    b = fechar(Banco(str(tmp_path / "s.db")))
    b.registrar_corretoras("2026-10-20", {"gate": {"estado": "conectada", "atraso_medio_ms": 100.0, "reconexoes": 1},
                                          "mexc": {"estado": "atrasada", "atraso_medio_ms": None, "reconexoes": 0}})
    b.registrar_corretoras("2026-10-20", {"gate": {"estado": "conectada", "atraso_medio_ms": 200.0, "reconexoes": 3},
                                          "mexc": {"estado": "conectada", "atraso_medio_ms": None, "reconexoes": 0}})
    assert resumo_do_dia(b, "2026-10-20")["corretoras"] == {
        "gate": {"atraso_medio_ms": 150.0, "reconexoes": 3},
        "mexc": {"atraso_medio_ms": None, "reconexoes": 0},
    }


def test_status_atual_completa_o_dia_de_hoje_sem_gravar(tmp_path, fechar):
    b = fechar(Banco(str(tmp_path / "s.db")))
    b.registrar_corretoras("2026-10-20", {"gate": {"atraso_medio_ms": 100.0, "reconexoes": 2}})
    agora = {"gate": {"estado": "conectada", "atraso_medio_ms": 400.0, "reconexoes": 5},
             "bitget": {"estado": "reconectando", "atraso_medio_ms": None, "reconexoes": 1}}
    completo = resumo_do_dia(b, "2026-10-20", status_corretoras=agora)["corretoras"]
    assert completo == {"bitget": {"atraso_medio_ms": None, "reconexoes": 1},
                        "gate": {"atraso_medio_ms": 250.0, "reconexoes": 5}}
    # Nada foi gravado: sem o status atual, o resumo continua como estava.
    assert resumo_do_dia(b, "2026-10-20")["corretoras"] == {"gate": {"atraso_medio_ms": 100.0, "reconexoes": 2}}


# ---------------------------------------------------------------------------
# Linhas do log
# ---------------------------------------------------------------------------


def test_linhas_do_resumo_uma_por_modo_e_uma_das_corretoras(tmp_path, fechar):
    b = fechar(banco_com_sinais(tmp_path, duracoes_s=[2, 10, 40, None], rp_liquido=[0.3, 0.4, 0.5, 0.4]))
    b.registrar_corretoras("2026-10-20", {"gate": {"atraso_medio_ms": 180.5, "reconexoes": 2},
                                          "mexc": {"atraso_medio_ms": None, "reconexoes": 0}})
    linhas = linhas_do_resumo(resumo_do_dia(b, "2026-10-20"))
    assert len(linhas) == 3
    lucro, farm, corretoras = linhas
    assert "2026-10-20" in lucro and "lucro" in lucro and "4 sinais" in lucro and "1 ainda aberto" in lucro
    assert "RP liquido medio 0.4000%" in lucro and "duracao media 17.3 s" in lucro
    assert "menos de 5 s: 33.3%" in lucro and "menos de 30 s: 66.7%" in lucro
    assert "farm" in farm and "0 sinais" in farm and "RP liquido medio -" in farm and "duracao media -" in farm
    assert "gate atraso medio 180.5 ms, 2 reconexoes" in corretoras
    assert "mexc atraso medio -, 0 reconexoes" in corretoras


def test_linhas_do_resumo_sem_corretoras(tmp_path, fechar):
    b = fechar(Banco(str(tmp_path / "s.db")))
    linhas = linhas_do_resumo(resumo_do_dia(b, "2026-10-20"))
    assert len(linhas) == 3 and "nenhuma leitura" in linhas[2]


# ---------------------------------------------------------------------------
# __main__: gravacao periodica, resumo na virada do dia e limpeza do banco
# ---------------------------------------------------------------------------


class Tempo:
    """Relogio falso que anda quando o scanner espera: `esperar(s)` anota a espera e soma s ao relogio.

    parar_depois: depois de tantas esperas, a proxima levanta CancelledError (como o Ctrl+C no meio da espera).
    """

    def __init__(self, agora: float, parar_depois: int):
        self.agora = agora
        self.espera = EsperaFalsa(parar_depois=parar_depois)

    def __call__(self) -> float:
        return self.agora

    async def esperar(self, segundos: float) -> None:
        await self.espera(segundos)
        self.agora += segundos


class MotorDeStatus:
    """Imita o Motor.status: devolve as `leituras` em ordem (a ultima se repete) e anota o `agora` de cada pedido."""

    def __init__(self, *leituras: dict):
        self.leituras = list(leituras) or [{}]
        self.pedidos: list[float] = []

    def status(self, agora: float) -> dict:
        self.pedidos.append(agora)
        return self.leituras.pop(0) if len(self.leituras) > 1 else self.leituras[0]


def gate(reconexoes: int, atraso_ms: float | None = None) -> dict:
    return {"gate": {"estado": "conectada", "atraso_medio_ms": atraso_ms, "reconexoes": reconexoes}}


def rodar_validacao(motor, banco, config, tempo: Tempo) -> None:
    """Roda o _validacao_diaria ate a espera falsa cancelar (fim do teste)."""

    async def cenario():
        with pytest.raises(asyncio.CancelledError):
            await principal._validacao_diaria(motor, banco, config, tempo, esperar=tempo.esperar)

    asyncio.run(cenario())


@pytest.fixture
def banco(tmp_path):
    b = Banco(str(tmp_path / "s.db"))
    yield b
    b.fechar()


@pytest.fixture
def limpezas(banco, monkeypatch):
    """Espiao do banco.limpar_antigos: anota (agora, dias, se rodou fora da thread principal) e chama o de verdade."""
    chamadas = []
    original = banco.limpar_antigos

    def espiao(agora, dias, *argumentos, **nomeados):
        chamadas.append((agora, dias, threading.current_thread() is not threading.main_thread()))
        return original(agora, dias, *argumentos, **nomeados)

    monkeypatch.setattr(banco, "limpar_antigos", espiao)
    return chamadas


def test_limpa_ao_ligar_e_na_virada_do_dia_utc_fora_do_laco_do_motor(banco, limpezas):
    config = cfg_com(intervalo_log_recursos_s=300, retencao_dias=7)
    tempo = Tempo(INICIO_21_OUT - 500, parar_depois=3)  # liga as 23:51:40 UTC do dia 20
    rodar_validacao(MotorDeStatus(gate(0)), banco, config, tempo)
    # Ao ligar, e de novo quando o dia virou (a espera foi encurtada para acordar na meia-noite exata).
    assert limpezas == [(INICIO_21_OUT - 500, 7, True), (INICIO_21_OUT, 7, True)]
    assert tempo.espera.esperas == [300, 200, 300]


def test_espera_acorda_na_meia_noite_utc(banco, limpezas):
    config = cfg_com(intervalo_log_recursos_s=300)
    tempo = Tempo(INICIO_21_OUT - 10, parar_depois=2)
    rodar_validacao(MotorDeStatus(gate(0)), banco, config, tempo)
    assert tempo.espera.esperas == [10, 300]


def test_grava_as_corretoras_a_cada_intervalo_e_na_saida(banco, limpezas):
    motor = MotorDeStatus(gate(0, 100.0), gate(2, 200.0), gate(3, 300.0))
    config = cfg_com(intervalo_log_recursos_s=300)
    tempo = Tempo(INICIO_20_OUT + 10 * 3600, parar_depois=2)
    rodar_validacao(motor, banco, config, tempo)
    inicio = INICIO_20_OUT + 10 * 3600
    assert motor.pedidos == [inicio + 300, inicio + 600, inicio + 600]  # duas leituras + a da saida
    assert banco.corretoras_do_dia("2026-10-20") == {"gate": {"atraso_medio_ms": 200.0, "reconexoes": 3}}


def test_resumo_do_dia_anterior_vai_para_o_log_na_virada(tmp_path, monkeypatch, caplog):
    b = banco_com_sinais(tmp_path, duracoes_s=[2, 10, 40], rp_liquido=[0.3, 0.4, 0.5], modo="lucro")
    banco_com_sinais(tmp_path, duracoes_s=[100], rp_liquido=[-0.05], modo="farm", banco=b)
    motor = MotorDeStatus(gate(1, 150.0), gate(4, 250.0))
    config = cfg_com(intervalo_log_recursos_s=300)
    tempo = Tempo(INICIO_21_OUT - 100, parar_depois=2)
    try:
        with caplog.at_level(logging.INFO, logger="scanner"):
            rodar_validacao(motor, b, config, tempo)
        resumos = [r.getMessage() for r in caplog.records if r.getMessage().startswith("resumo do dia")]
        assert len(resumos) == 3  # lucro, farm e corretoras do dia 20; o dia 21 ainda nao acabou
        assert all("2026-10-20" in linha for linha in resumos)
        assert "lucro" in resumos[0] and "3 sinais" in resumos[0] and "farm" in resumos[1] and "1 sinais" in resumos[1]
        # A leitura feita na virada (00:00:00 do dia 21) fecha o dia 20: as reconexoes ate a meia-noite ficam nele.
        assert "gate atraso medio 150.0 ms, 1 reconexoes" in resumos[2]
        assert b.corretoras_do_dia("2026-10-21") == {"gate": {"atraso_medio_ms": 250.0, "reconexoes": 3}}  # a da saida
    finally:
        b.fechar()


def test_erro_na_limpeza_vai_para_o_log_e_o_resto_segue(banco, monkeypatch, caplog):
    def limpeza_com_defeito(agora, dias):
        raise RuntimeError("disco cheio")

    monkeypatch.setattr(banco, "limpar_antigos", limpeza_com_defeito)
    motor = MotorDeStatus(gate(1))
    tempo = Tempo(INICIO_21_OUT - 100, parar_depois=2)
    with caplog.at_level(logging.INFO, logger="scanner"):
        rodar_validacao(motor, banco, cfg_com(intervalo_log_recursos_s=300), tempo)
    erros = [r for r in caplog.records if r.levelno == logging.ERROR and "limpar" in r.getMessage()]
    assert len(erros) == 2 and all("disco cheio" in str(r.exc_info[1]) for r in erros)  # ao ligar e na virada
    assert tempo.espera.esperas == [100, 300]  # o laco seguiu
    assert any(r.getMessage().startswith("resumo do dia") for r in caplog.records)
    assert banco.corretoras_do_dia("2026-10-20") == {"gate": {"atraso_medio_ms": None, "reconexoes": 1}}


def test_erro_ao_gravar_as_corretoras_vai_para_o_log_e_o_laco_segue(banco, limpezas, caplog):
    class MotorQuebrado:
        def status(self, agora):
            raise RuntimeError("status com defeito")

    tempo = Tempo(INICIO_20_OUT + 3600, parar_depois=3)
    with caplog.at_level(logging.ERROR, logger="scanner"):
        rodar_validacao(MotorQuebrado(), banco, cfg_com(intervalo_log_recursos_s=300), tempo)
    assert tempo.espera.esperas == [300, 300, 300]
    erros = [r for r in caplog.records if "status com defeito" in str(r.exc_info and r.exc_info[1])]
    assert len(erros) == 4  # tres leituras e a da saida, cada uma no log


def test_erro_no_resumo_vai_para_o_log_e_a_limpeza_da_virada_acontece(banco, limpezas, monkeypatch, caplog):
    def resumo_com_defeito(banco, dia, status_corretoras=None):
        raise RuntimeError("resumo com defeito")

    monkeypatch.setattr(principal, "resumo_do_dia", resumo_com_defeito)
    tempo = Tempo(INICIO_21_OUT - 100, parar_depois=2)
    with caplog.at_level(logging.ERROR, logger="scanner"):
        rodar_validacao(MotorDeStatus(gate(0)), banco, cfg_com(intervalo_log_recursos_s=300), tempo)
    assert any("resumo com defeito" in str(r.exc_info and r.exc_info[1]) for r in caplog.records)
    assert [agora for agora, _, _ in limpezas] == [INICIO_21_OUT - 100, INICIO_21_OUT]


def test_erro_ao_escrever_as_linhas_do_resumo_vai_para_o_log_e_o_laco_segue(banco, limpezas, monkeypatch, caplog):
    def linhas_com_defeito(resumo):
        raise RuntimeError("linhas com defeito")

    monkeypatch.setattr(principal, "linhas_do_resumo", linhas_com_defeito)
    tempo = Tempo(INICIO_21_OUT - 100, parar_depois=3)
    with caplog.at_level(logging.ERROR, logger="scanner"):
        rodar_validacao(MotorDeStatus(gate(0)), banco, cfg_com(intervalo_log_recursos_s=300), tempo)
    assert any("linhas com defeito" in str(r.exc_info and r.exc_info[1]) for r in caplog.records)
    assert tempo.espera.esperas == [100, 300, 300]  # a tarefa nao morreu
    assert [agora for agora, _, _ in limpezas] == [INICIO_21_OUT - 100, INICIO_21_OUT]


class TempoQuePula(Tempo):
    """Tempo que, na espera numero `quando` (contando de 1), pula para `para` em vez de andar o que esperou."""

    def __init__(self, agora: float, parar_depois: int, quando: int, para: float):
        super().__init__(agora, parar_depois)
        self._quando, self._para = quando, para

    async def esperar(self, segundos: float) -> None:
        await super().esperar(segundos)
        if len(self.espera.esperas) == self._quando:
            self.agora = self._para


def test_relogio_que_volta_para_o_dia_anterior_nao_conta_como_virada(banco, limpezas, caplog):
    # Ligado as 00:00:10 do dia 21; o relogio do PC e acertado e volta para 23:59:00 do dia 20.
    tempo = TempoQuePula(INICIO_21_OUT + 10, parar_depois=2, quando=1, para=INICIO_21_OUT - 60)
    with caplog.at_level(logging.INFO, logger="scanner"):
        rodar_validacao(MotorDeStatus(gate(1)), banco, cfg_com(intervalo_log_recursos_s=300), tempo)
    assert not any(r.getMessage().startswith("resumo do dia") for r in caplog.records)
    assert [agora for agora, _, _ in limpezas] == [INICIO_21_OUT + 10]  # so a de ligar
    # As leituras continuam no dia que estava correndo (21), nada vai para o dia 20.
    assert banco.corretoras_do_dia("2026-10-20") == {}
    assert banco.corretoras_do_dia("2026-10-21") == {"gate": {"atraso_medio_ms": None, "reconexoes": 1}}


def test_pc_que_dormiu_atravessando_dias_fecha_o_dia_em_que_dormiu(banco, limpezas, caplog):
    # Ligado as 10:00 do dia 20; o PC dorme e acorda as 08:00 do dia 23 (a espera "termina" la).
    acorda = INICIO_20_OUT + 3 * DIA + 8 * 3600
    tempo = TempoQuePula(INICIO_20_OUT + 10 * 3600, parar_depois=2, quando=1, para=acorda)
    with caplog.at_level(logging.INFO, logger="scanner"):
        rodar_validacao(MotorDeStatus(gate(2)), banco, cfg_com(intervalo_log_recursos_s=300), tempo)
    resumos = [r.getMessage() for r in caplog.records if r.getMessage().startswith("resumo do dia")]
    assert len(resumos) == 3 and all("2026-10-20" in linha for linha in resumos)  # so o dia em que dormiu
    assert [agora for agora, _, _ in limpezas] == [INICIO_20_OUT + 10 * 3600, acorda]
    assert banco.corretoras_do_dia("2026-10-20")["gate"]["reconexoes"] == 2  # a leitura ao acordar fecha o dia 20
    assert banco.corretoras_do_dia("2026-10-21") == {} and banco.corretoras_do_dia("2026-10-22") == {}
    assert "gate" in banco.corretoras_do_dia("2026-10-23")


def test_rodar_limpa_o_banco_ao_ligar_e_grava_as_corretoras_na_saida(tmp_path, monkeypatch):
    chamadas = []
    original = Banco.limpar_antigos

    def espiao(self, agora, dias, *argumentos, **nomeados):
        chamadas.append((agora, dias))
        return original(self, agora, dias, *argumentos, **nomeados)

    monkeypatch.setattr(Banco, "limpar_antigos", espiao)
    exchanges = exchanges_falsas()
    config = config_servico(tmp_path, retencao_dias=3)
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=lambda: 1000.0, ciclos=2), timeout=5))
    assert chamadas == [(1000.0, 3)]
    b = Banco(config.banco)
    try:
        assert b.corretoras_do_dia("1970-01-01") == {"bitget": {"atraso_medio_ms": None, "reconexoes": 0},
                                                     "gate": {"atraso_medio_ms": None, "reconexoes": 0}}
    finally:
        b.fechar()


def test_iso_utc_da_fabrica_bate_com_o_dia_utc():
    assert iso_utc(INICIO_20_OUT).startswith(dia_utc(INICIO_20_OUT))


def test_dia_utc_antes_de_1970():
    assert dia_utc(-1) == "1969-12-31"
