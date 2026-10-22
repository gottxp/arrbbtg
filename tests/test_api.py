"""Testes da API (Task 11): rotas /api, WebSocket /ws/sinais, senha (HTTP Basic) e a ligacao no __main__.

Nenhum teste acessa a internet. A API roda no TestClient do FastAPI (em memoria); so um teste abre uma
porta de verdade (127.0.0.1), para conferir que o servidor sobe e para sem deixar nada preso.
"""

import asyncio
import base64
import inspect
import json
import logging
import secrets
import signal
import socket
import time
from dataclasses import replace
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import scanner.__main__ as principal
from scanner.api import Transmissor, conferir_painel, criar_app, servir_api
from scanner.api import servidor as modulo_servidor
from scanner.armazenamento import Banco
from scanner.config.carregar import ErroConfig
from scanner.filtros import Filtros
from scanner.motor import Motor, Sessao, chave_de, texto_da_chave
from scanner.sinais import Evento
from tests.fabricas import motor_falso
from tests.test_motor import config_servico, exchanges_falsas

# Horario do estado criado pelo motor_falso (as pontas "chegaram" em t = 10 s).
AGORA = 10.0

COMBO = ("BTC", "gate", "spot", "bitget", "futuro")
CHAVE_TEXTO = texto_da_chave(chave_de(COMBO))  # "BTC/USDT|spot_futuro|gate|spot|bitget|futuro"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# O WebSocket do TestClient espera para sempre por uma mensagem que nunca chega (ex.: evento perdido).
LIMITE_POR_TESTE_S = 30


@pytest.fixture(autouse=True)
def teste_que_trava_falha_em_vez_de_esperar_para_sempre():
    """Um teste que passa de LIMITE_POR_TESTE_S falha com TimeoutError (so onde o sistema tem SIGALRM)."""
    if not hasattr(signal, "SIGALRM"):  # Windows
        yield
        return

    def estourou(numero, quadro):
        raise TimeoutError(f"o teste passou de {LIMITE_POR_TESTE_S} s (travado esperando uma mensagem?)")

    anterior = signal.signal(signal.SIGALRM, estourou)
    signal.setitimer(signal.ITIMER_REAL, LIMITE_POR_TESTE_S)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, anterior)


@pytest.fixture
def banco(tmp_path):
    b = Banco(str(tmp_path / "api.db"))
    yield b
    b.fechar()


@pytest.fixture
def motor(banco):
    """Motor com um sinal ativo: spot da Gate (100) x futuro da Bitget (101), RP liquido 0,26%."""
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5, banco=banco)
    m.ciclo(agora=AGORA)
    return m


def montar_cliente(motor, banco, painel=None, relogio=lambda: AGORA, **extras) -> TestClient:
    """TestClient da API do `motor`; `painel` troca campos do bloco painel da Config (usuario, senha...)."""
    config = replace(motor.cfg, painel={**motor.cfg.painel, **(painel or {})})
    return TestClient(criar_app(motor, banco, config, relogio=relogio, **extras))


@pytest.fixture
def cliente(motor, banco):
    return montar_cliente(motor, banco)


@pytest.fixture
def cliente_com_senha(motor, banco):
    return montar_cliente(motor, banco, painel={"usuario": "jr", "senha": "segredo"})


def basic(usuario: str, senha: str) -> dict:
    """O cabecalho Authorization de uma conexao WebSocket (o TestClient so monta o do HTTP sozinho)."""
    codigo = base64.b64encode(f"{usuario}:{senha}".encode()).decode()
    return {"Authorization": f"Basic {codigo}"}


def publicar(cliente: TestClient, eventos: list) -> None:
    """Chama `Transmissor.publicar` dentro do laco da API (como o __main__ faz). Precisa de `with cliente:`."""
    cliente.portal.call(cliente.app.state.transmissor.publicar, eventos)


# ---------------------------------------------------------------------------
# Brief (Task 11, Passo 1) - verbatim
# ---------------------------------------------------------------------------


def test_sinais_ativos(cliente):
    r = cliente.get("/api/sinais"); assert r.status_code == 200 and isinstance(r.json(), list)


def test_grupo_de_filtros(cliente):
    cliente.post("/api/filtros/Farm", json={"volume_min_usd": 500})
    assert cliente.get("/api/filtros").json()["Farm"]["volume_min_usd"] == 500


def test_exige_senha_quando_configurada(cliente_com_senha):
    assert cliente_com_senha.get("/api/sinais").status_code == 401
    assert cliente_com_senha.get("/api/sinais", auth=("jr", "segredo")).status_code == 200


# ---------------------------------------------------------------------------
# GET /api/sinais
# ---------------------------------------------------------------------------


def test_sinais_devolve_os_sinais_ativos_do_gerenciador(cliente, motor):
    sinais = cliente.get("/api/sinais").json()
    assert sinais == motor.gerenciador.ativos()
    assert [s["par"] for s in sinais] == ["BTC/USDT"] and sinais[0]["ponta_long"]["corretora"] == "gate"


def test_sinais_sem_sinal_ativo_devolve_lista_vazia(banco):
    vazio = motor_falso()
    r = montar_cliente(vazio, banco).get("/api/sinais")
    assert r.status_code == 200 and r.json() == []


def test_sinais_nao_chama_candidatos_do_motor(cliente, monkeypatch):
    # Motor.candidatos tem efeito colateral (acrescenta amostras): a API so le o gerenciador.
    def proibido(self, agora):
        raise AssertionError("a API nao pode chamar Motor.candidatos")

    monkeypatch.setattr(Motor, "candidatos", proibido)
    assert cliente.get("/api/sinais").status_code == 200
    assert cliente.get("/api/status").status_code == 200


# ---------------------------------------------------------------------------
# GET /api/historico/{chave}
# ---------------------------------------------------------------------------


def gravar_amostras_de_teste(banco, n=12, ts0=1000.0, chave=CHAVE_TEXTO) -> None:
    """n amostras, de 5 em 5 s. Na i-esima (1..n): A = 0,1 x i; F = -0,1 x i; lotes 1000+i e 2000+i."""
    banco.gravar_amostras([
        (chave, ts0 + 5 * i, 0.1 * i, -0.1 * i, 100.0 + i, 101.0 + i, 1000.0 + i, 2000.0 + i)
        for i in range(1, n + 1)
    ])


def historico(cliente, chave=CHAVE_TEXTO, **parametros):
    return cliente.get("/api/historico/" + quote(chave, safe=""), params=parametros)


def test_historico_abertura(motor, banco):
    gravar_amostras_de_teste(banco)
    r = historico(montar_cliente(motor, banco), intencao="abertura")
    assert r.status_code == 200
    corpo = r.json()
    assert len(corpo["amostras"]) == 12
    primeira = corpo["amostras"][0]
    assert primeira == {"ts": 1005.0, "spread": pytest.approx(0.1), "preco_long": 101.0, "preco_short": 102.0,
                        "lote_long": 1001.0, "lote_short": 2001.0}
    assert [a["ts"] for a in corpo["amostras"]] == sorted(a["ts"] for a in corpo["amostras"])  # do mais antigo
    resumo = corpo["resumo"]
    assert resumo["maior_spread"] == pytest.approx(1.2)
    assert resumo["media_10_maiores"] == pytest.approx(0.75)  # (0,3 + ... + 1,2) / 10
    assert resumo["spread_medio"] == pytest.approx(0.65)  # (0,1 + ... + 1,2) / 12
    assert resumo["lote_medio_long"] == pytest.approx(1006.5)
    assert resumo["lote_medio_short"] == pytest.approx(2006.5)


def test_historico_fechamento_usa_o_f(motor, banco):
    gravar_amostras_de_teste(banco)
    corpo = historico(montar_cliente(motor, banco), intencao="fechamento").json()
    assert corpo["amostras"][0]["spread"] == pytest.approx(-0.1)
    resumo = corpo["resumo"]
    assert resumo["maior_spread"] == pytest.approx(-0.1)  # o maior F e o menos negativo
    assert resumo["media_10_maiores"] == pytest.approx(-0.55)  # (-0,1 + ... + -1,0) / 10
    assert resumo["spread_medio"] == pytest.approx(-0.65)
    assert resumo["lote_medio_long"] == pytest.approx(1006.5)  # os lotes nao dependem da intencao


def test_historico_com_menos_de_10_amostras_a_media_dos_maiores_usa_todas(motor, banco):
    gravar_amostras_de_teste(banco, n=3)
    resumo = historico(montar_cliente(motor, banco), intencao="abertura").json()["resumo"]
    assert resumo["media_10_maiores"] == pytest.approx(0.2) == pytest.approx(resumo["spread_medio"])


def test_historico_sem_amostras(cliente):
    r = historico(cliente, intencao="abertura")
    assert r.status_code == 200
    assert r.json() == {"amostras": [], "resumo": {"maior_spread": None, "media_10_maiores": None,
                                                    "spread_medio": None, "lote_medio_long": None,
                                                    "lote_medio_short": None}}


def test_historico_intencao_padrao_e_abertura(motor, banco):
    gravar_amostras_de_teste(banco, n=2)
    corpo = historico(montar_cliente(motor, banco)).json()
    assert corpo["amostras"][0]["spread"] == pytest.approx(0.1)


@pytest.mark.parametrize("intencao", ["", "abrir", "ABERTURA", "fechar", "1"])
def test_historico_intencao_invalida_da_422(cliente, intencao):
    assert historico(cliente, intencao=intencao).status_code == 422


@pytest.mark.parametrize("periodo", ["0", "-1", "abc", "nan", "inf"])
def test_historico_periodo_invalido_da_422(cliente, periodo):
    assert historico(cliente, intencao="abertura", periodo_h=periodo).status_code == 422


def test_historico_chave_com_barra_e_pipe_vai_codificada_ou_nao(motor, banco):
    gravar_amostras_de_teste(banco, n=2)
    cliente = montar_cliente(motor, banco)
    codificada = cliente.get("/api/historico/" + quote(CHAVE_TEXTO, safe=""), params={"intencao": "abertura"})
    crua = cliente.get("/api/historico/" + CHAVE_TEXTO, params={"intencao": "abertura"})
    assert len(codificada.json()["amostras"]) == len(crua.json()["amostras"]) == 2


def test_historico_so_traz_a_chave_pedida(motor, banco):
    gravar_amostras_de_teste(banco, n=2)
    gravar_amostras_de_teste(banco, n=5, chave="ETH/USDT|spot_futuro|gate|spot|bitget|futuro")
    corpo = historico(montar_cliente(motor, banco), intencao="abertura").json()
    assert len(corpo["amostras"]) == 2


def test_historico_respeita_o_periodo(motor, banco):
    agora = 100_000.0
    banco.gravar_amostras([
        (CHAVE_TEXTO, agora - 5 * 3600, 0.9, -0.9, 1.0, 2.0, 10.0, 20.0),  # ha 5 h
        (CHAVE_TEXTO, agora - 3 * 3600, 0.5, -0.5, 1.0, 2.0, 10.0, 20.0),  # ha 3 h
        (CHAVE_TEXTO, agora - 600, 0.1, -0.1, 1.0, 2.0, 10.0, 20.0),  # ha 10 min
    ])
    cliente = montar_cliente(motor, banco, relogio=lambda: agora)
    assert len(historico(cliente, intencao="abertura").json()["amostras"]) == 2  # padrao: 4 h da Config
    assert len(historico(cliente, intencao="abertura", periodo_h=6).json()["amostras"]) == 3
    assert len(historico(cliente, intencao="abertura", periodo_h=0.5).json()["amostras"]) == 1
    assert historico(cliente, intencao="abertura", periodo_h=0.01).json()["amostras"] == []


def test_historico_periodo_padrao_vem_da_sessao_e_se_invalido_da_config(motor, banco):
    agora = 100_000.0
    banco.gravar_amostras([
        (CHAVE_TEXTO, agora - 3 * 3600, 0.5, -0.5, 1.0, 2.0, 10.0, 20.0),
        (CHAVE_TEXTO, agora - 600, 0.1, -0.1, 1.0, 2.0, 10.0, 20.0),
    ])
    cliente = montar_cliente(motor, banco, relogio=lambda: agora)
    motor.sessao = Sessao(filtros=Filtros(periodo_metricas_h=2))
    assert len(historico(cliente, intencao="abertura").json()["amostras"]) == 1
    motor.sessao = Sessao(filtros=Filtros(periodo_metricas_h="abc"))  # o motor ignora; a API tambem
    assert len(historico(cliente, intencao="abertura").json()["amostras"]) == 2  # 4 h da Config
    motor.sessao = Sessao(filtros=Filtros(periodo_metricas_h=-3))
    assert len(historico(cliente, intencao="abertura").json()["amostras"]) == 2


# ---------------------------------------------------------------------------
# Grupos de filtros
# ---------------------------------------------------------------------------


def test_filtros_comeca_vazio(cliente):
    assert cliente.get("/api/filtros").json() == {}


def test_salvar_grupo_guarda_o_filtros_completo_validado(cliente, banco):
    r = cliente.post("/api/filtros/Farm", json={"volume_min_usd": 500, "tipos": ["spot_futuro"]})
    assert r.status_code == 200
    esperado = Filtros(volume_min_usd=500, tipos=["spot_futuro"]).para_dict()
    assert r.json() == esperado
    assert banco.listar_grupos() == {"Farm": esperado}
    assert cliente.get("/api/filtros").json() == {"Farm": esperado}


def test_salvar_grupo_com_o_mesmo_nome_substitui(cliente):
    cliente.post("/api/filtros/Farm", json={"volume_min_usd": 500})
    cliente.post("/api/filtros/Farm", json={"spread_min_pct": 1.5})
    grupo = cliente.get("/api/filtros").json()["Farm"]
    assert grupo["spread_min_pct"] == 1.5 and grupo["volume_min_usd"] is None


def test_nome_do_grupo_com_espaco_e_acento(cliente):
    cliente.post("/api/filtros/" + quote("Meu grupo ç"), json={})
    assert "Meu grupo ç" in cliente.get("/api/filtros").json()


def test_salvar_grupo_com_campo_desconhecido_da_400_com_a_mensagem(cliente, banco):
    r = cliente.post("/api/filtros/Ruim", json={"volume_minimo": 500})
    assert r.status_code == 400 and "volume_minimo" in r.json()["detail"]
    assert banco.listar_grupos() == {}


@pytest.mark.parametrize("corpo, campo", [
    ({"volume_min_usd": "muito"}, "volume_min_usd"),
    ({"tipos": "spot_futuro"}, "tipos"),
    ({"mostrar_suspeitos": 1}, "mostrar_suspeitos"),
    ([1, 2], "dicionario"),
    ("texto", "dicionario"),
])
def test_salvar_grupo_com_valor_de_tipo_errado_da_400(cliente, corpo, campo):
    r = cliente.post("/api/filtros/Ruim", json=corpo)
    assert r.status_code == 400 and campo in r.json()["detail"]


def test_salvar_grupo_com_nan_da_400(cliente):
    r = cliente.post("/api/filtros/Ruim", content='{"volume_min_usd": NaN}', headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_salvar_grupo_sem_corpo_json_valido_da_422_e_json_null_da_400(cliente):
    cabecalho = {"Content-Type": "application/json"}
    assert cliente.post("/api/filtros/Ruim", content="isso nao e json", headers=cabecalho).status_code == 422
    assert cliente.post("/api/filtros/Ruim").status_code == 422  # sem corpo
    assert cliente.post("/api/filtros/Ruim", content="null", headers=cabecalho).status_code == 400


def test_apagar_grupo(cliente):
    cliente.post("/api/filtros/Farm", json={})
    cliente.post("/api/filtros/Lucro", json={})
    r = cliente.delete("/api/filtros/Farm")
    assert r.status_code == 200
    assert list(cliente.get("/api/filtros").json()) == ["Lucro"]


def test_apagar_grupo_que_nao_existe_da_404(cliente):
    r = cliente.delete("/api/filtros/Fantasma")
    assert r.status_code == 404 and "Fantasma" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Sessao
# ---------------------------------------------------------------------------


def test_sessao_atual(cliente):
    assert cliente.get("/api/sessao").json() == {"modo": "lucro", "filtros": Filtros().para_dict(), "comparacao": False}


def test_trocar_a_sessao_cria_um_objeto_novo_no_motor(cliente, motor):
    antiga = motor.sessao
    r = cliente.put("/api/sessao", json={"modo": "farm", "filtros": {"volume_min_usd": 500}, "comparacao": True})
    assert r.status_code == 200
    nova = motor.sessao
    assert nova is not antiga and isinstance(nova, Sessao)
    assert (nova.modo, nova.comparacao) == ("farm", True)
    assert nova.filtros == Filtros(volume_min_usd=500)
    assert (antiga.modo, antiga.comparacao, antiga.filtros) == ("lucro", False, Filtros())  # a antiga nao mudou
    esperado = {"modo": "farm", "filtros": Filtros(volume_min_usd=500).para_dict(), "comparacao": True}
    assert r.json() == esperado and cliente.get("/api/sessao").json() == esperado


def test_sessao_trocada_vale_no_proximo_ciclo(cliente, motor):
    assert [s["modo"] for s in cliente.get("/api/sinais").json()] == ["lucro"]
    cliente.put("/api/sessao", json={"modo": "lucro", "filtros": {"apenas_mostrar": ["ETH"]}, "comparacao": False})
    eventos = motor.ciclo(agora=AGORA + 0.5)
    assert [e.tipo for e in eventos] == ["encerrado"]  # BTC saiu por causa do filtro novo
    assert cliente.get("/api/sinais").json() == []


def sessao_em_uso(motor) -> Sessao:
    """Poe no motor uma sessao bem diferente do padrao (farm, filtros, comparacao) e a devolve."""
    motor.sessao = Sessao(modo="farm", filtros=Filtros(volume_min_usd=500, tipos=["spot_futuro"]), comparacao=True)
    return motor.sessao


def conferir_sessao_intacta(cliente, motor, antes: Sessao) -> None:
    assert motor.sessao is antes
    assert (antes.modo, antes.filtros, antes.comparacao) == (
        "farm", Filtros(volume_min_usd=500, tipos=["spot_futuro"]), True)
    assert cliente.get("/api/sessao").json() == {"modo": "farm", "filtros": antes.filtros.para_dict(),
                                                 "comparacao": True}


def test_sessao_com_chave_desconhecida_da_422_e_nao_muda_nada(cliente, motor):
    # Erro de digitacao no painel (ex.: "mode" em vez de "modo") nao pode apagar os filtros em uso.
    antes = sessao_em_uso(motor)
    r = cliente.put("/api/sessao", json={"mode": "farm", "filters": {"volume_min_usd": 500}, "comparison": True})
    assert r.status_code == 422
    conferir_sessao_intacta(cliente, motor, antes)
    r = cliente.put("/api/sessao", json={"modo": "lucro", "filtros": {}, "comparacao": False, "extra": 1})
    assert r.status_code == 422  # uma chave a mais, com os tres campos certos, tambem recusa
    conferir_sessao_intacta(cliente, motor, antes)


def test_sessao_com_corpo_vazio_da_422_e_nao_muda_nada(cliente, motor):
    antes = sessao_em_uso(motor)
    assert cliente.put("/api/sessao", json={}).status_code == 422
    assert cliente.put("/api/sessao").status_code == 422  # sem corpo nenhum
    conferir_sessao_intacta(cliente, motor, antes)


@pytest.mark.parametrize("faltando", ["modo", "filtros", "comparacao"])
def test_sessao_com_campo_faltando_da_422_e_nao_muda_nada(cliente, motor, faltando):
    # PUT troca a sessao inteira: os tres campos sao obrigatorios (nada de "completar com o padrao").
    antes = sessao_em_uso(motor)
    corpo = {"modo": "lucro", "filtros": {}, "comparacao": False}
    del corpo[faltando]
    r = cliente.put("/api/sessao", json=corpo)
    assert r.status_code == 422 and faltando in json.dumps(r.json())
    conferir_sessao_intacta(cliente, motor, antes)


@pytest.mark.parametrize("modo", ["", "Lucro", "arbitragem", "farm "])
def test_sessao_com_modo_invalido_da_400_e_nao_muda_nada(cliente, motor, modo):
    antiga = motor.sessao
    r = cliente.put("/api/sessao", json={"modo": modo, "filtros": {}, "comparacao": False})
    assert r.status_code == 400 and "modo" in r.json()["detail"]
    assert motor.sessao is antiga


@pytest.mark.parametrize("filtros, campo", [
    ({"inventado": 1}, "inventado"),
    ({"volume_min_usd": "x"}, "volume_min_usd"),
    ([1], "dicionario"),
])
def test_sessao_com_filtros_invalidos_da_400_e_nao_muda_nada(cliente, motor, filtros, campo):
    antiga = motor.sessao
    r = cliente.put("/api/sessao", json={"modo": "lucro", "filtros": filtros, "comparacao": False})
    assert r.status_code == 400 and campo in r.json()["detail"]
    assert motor.sessao is antiga


def test_sessao_com_comparacao_que_nao_e_booleano_da_422(cliente, motor):
    antiga = motor.sessao
    r = cliente.put("/api/sessao", json={"modo": "lucro", "filtros": {}, "comparacao": "talvez"})
    assert r.status_code == 422 and motor.sessao is antiga


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_por_corretora_tem_formato_estavel(cliente, motor):
    corpo = cliente.get("/api/status").json()
    assert set(corpo) == set(motor.cfg.corretoras) == {"mexc", "gate", "bitget"}
    # gate e bitget mandaram mensagem em t = 10 s (agora do teste); a mexc nunca mandou.
    assert corpo["gate"] == {"estado": "conectada", "atraso_medio_ms": None, "reconexoes": 0}
    assert corpo["bitget"] == {"estado": "conectada", "atraso_medio_ms": None, "reconexoes": 0}
    assert corpo["mexc"] == {"estado": "atrasada", "atraso_medio_ms": None, "reconexoes": 0}


def test_status_usa_o_relogio_da_api(motor, banco):
    # 60 s depois da ultima mensagem (limite de silencio: 2 s) todas estao atrasadas.
    corpo = montar_cliente(motor, banco, relogio=lambda: AGORA + 60).get("/api/status").json()
    assert {c["estado"] for c in corpo.values()} == {"atrasada"}


def test_status_repassa_o_que_o_motor_informar(cliente, motor, monkeypatch):
    # A Task 14 acrescenta reconectando, atraso e reconexoes ao motor.status; a API so repassa.
    monkeypatch.setattr(motor, "status", lambda agora: {
        "gate": {"estado": "reconectando", "atraso_medio_ms": 123.5, "reconexoes": 3},
        "bitget": {"estado": "conectada"},
    })
    assert cliente.get("/api/status").json() == {
        "gate": {"estado": "reconectando", "atraso_medio_ms": 123.5, "reconexoes": 3},
        "bitget": {"estado": "conectada", "atraso_medio_ms": None, "reconexoes": 0},
    }


# ---------------------------------------------------------------------------
# Senha (HTTP Basic)
# ---------------------------------------------------------------------------

ROTAS = [
    ("GET", "/api/sinais"),
    ("GET", "/api/historico/" + quote(CHAVE_TEXTO, safe="") + "?intencao=abertura"),
    ("GET", "/api/filtros"),
    ("POST", "/api/filtros/Farm"),
    ("DELETE", "/api/filtros/Farm"),
    ("GET", "/api/sessao"),
    ("PUT", "/api/sessao"),
    ("GET", "/api/status"),
    ("GET", "/api/nao-existe"),
]


@pytest.mark.parametrize("metodo, caminho", ROTAS)
def test_senha_vale_para_todas_as_rotas_da_api(cliente_com_senha, metodo, caminho):
    r = cliente_com_senha.request(metodo, caminho, json={})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


@pytest.mark.parametrize("auth", [("jr", "errada"), ("outro", "segredo"), ("jr", ""), ("", ""), ("JR", "segredo"),
                                  ("jr", "segredo "), ("jr", "Segredo")])
def test_senha_errada_da_401(cliente_com_senha, auth):
    assert cliente_com_senha.get("/api/sinais", auth=auth).status_code == 401


def test_cabecalho_authorization_malformado_da_401(cliente_com_senha):
    for valor in ["", "Basic", "Basic !!!", "Bearer abc", "Basic " + base64.b64encode(b"semdoispontos").decode(),
                  b"Basic \xe9\xe9"]:
        r = cliente_com_senha.get("/api/sinais", headers={"Authorization": valor})
        assert r.status_code == 401, valor


def test_senha_certa_libera_todas_as_rotas(cliente_com_senha):
    auth = ("jr", "segredo")
    assert cliente_com_senha.get("/api/sinais", auth=auth).status_code == 200
    assert cliente_com_senha.get("/api/filtros", auth=auth).status_code == 200
    assert cliente_com_senha.post("/api/filtros/Farm", json={}, auth=auth).status_code == 200
    assert cliente_com_senha.delete("/api/filtros/Farm", auth=auth).status_code == 200
    assert cliente_com_senha.get("/api/sessao", auth=auth).status_code == 200
    assert cliente_com_senha.get("/api/status", auth=auth).status_code == 200
    assert cliente_com_senha.get("/api/historico/x", auth=auth).status_code == 200


def test_senha_com_acento_e_dois_pontos(motor, banco):
    cliente = montar_cliente(motor, banco, painel={"usuario": "jr", "senha": "séc:ret:ö"})
    assert cliente.get("/api/sinais", auth=("jr", "séc:ret:ö")).status_code == 200
    assert cliente.get("/api/sinais", auth=("jr", "séc")).status_code == 401


def test_sem_usuario_configurado_nao_pede_senha(cliente):
    assert cliente.get("/api/sinais").status_code == 200
    assert cliente.get("/api/sinais", auth=("qualquer", "coisa")).status_code == 200


def test_a_senha_e_comparada_com_compare_digest(cliente_com_senha, monkeypatch):
    chamadas = []
    original = secrets.compare_digest

    def espiao(a, b):
        chamadas.append((a, b))
        return original(a, b)

    monkeypatch.setattr("scanner.api.app.secrets.compare_digest", espiao)
    cliente_com_senha.get("/api/sinais", auth=("jr", "errada"))
    # Mesmo com o usuario certo e a senha errada, as duas conferencias rodam (nao para na primeira).
    assert (b"jr", b"jr") in chamadas and (b"errada", b"segredo") in chamadas


def test_a_documentacao_automatica_nao_fica_aberta(cliente_com_senha):
    for caminho in ("/docs", "/redoc", "/openapi.json"):
        assert cliente_com_senha.get(caminho).status_code in (401, 404)
        assert cliente_com_senha.get(caminho, auth=("jr", "segredo")).status_code == 404


# ---------------------------------------------------------------------------
# Recusa de configuracao de painel insegura
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("painel", [
    {"usuario": "jr"},
    {"usuario": "jr", "senha": None},
    {"usuario": "jr", "senha": ""},
])
def test_nao_sobe_com_usuario_sem_senha(motor, banco, painel):
    config = replace(motor.cfg, painel={**motor.cfg.painel, **painel})
    with pytest.raises(ErroConfig, match="painel.senha"):
        criar_app(motor, banco, config)
    with pytest.raises(ErroConfig, match="painel.senha"):
        conferir_painel(config)


def test_nao_sobe_com_senha_sem_usuario(motor, banco):
    # Senha preenchida e usuario esquecido: o dono acha que o painel esta protegido, mas ficaria aberto.
    config = replace(motor.cfg, painel={**motor.cfg.painel, "senha": "segredo"})
    with pytest.raises(ErroConfig, match="painel.usuario"):
        criar_app(motor, banco, config)


@pytest.mark.parametrize("usuario", ["", "a:b"])
def test_nao_sobe_com_usuario_invalido(motor, banco, usuario):
    config = replace(motor.cfg, painel={**motor.cfg.painel, "usuario": usuario, "senha": "segredo"})
    with pytest.raises(ErroConfig, match="painel.usuario"):
        criar_app(motor, banco, config)


def test_conferir_painel_aceita_painel_aberto_e_painel_protegido(motor):
    conferir_painel(motor.cfg)
    conferir_painel(replace(motor.cfg, painel={**motor.cfg.painel, "usuario": "jr", "senha": "x"}))


def test_main_recusa_usuario_sem_senha_antes_de_conectar(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(principal, "rodar", lambda *a, **k: pytest.fail("nao deveria subir o scanner"))
    arquivo = tmp_path / "config.yaml"
    arquivo.write_text("painel:\n  usuario: jr\n")
    assert principal.main(["--config", str(arquivo)]) == 1
    assert "painel.senha" in capsys.readouterr().err
    assert principal.main(["--config", str(arquivo), "--validar"]) == 1  # o --validar tambem acusa


# ---------------------------------------------------------------------------
# WebSocket /ws/sinais
# ---------------------------------------------------------------------------


def test_ws_manda_o_snapshot_ao_conectar(cliente, motor):
    with cliente.websocket_connect("/ws/sinais") as ws:
        mensagem = ws.receive_json()
    assert mensagem == {"tipo": "snapshot", "sinais": motor.gerenciador.ativos()}
    assert [s["par"] for s in mensagem["sinais"]] == ["BTC/USDT"]


def test_ws_snapshot_vazio_sem_sinais(banco):
    with montar_cliente(motor_falso(), banco).websocket_connect("/ws/sinais") as ws:
        assert ws.receive_json() == {"tipo": "snapshot", "sinais": []}


def test_ws_entrega_cada_evento_como_tipo_e_sinal(cliente, motor):
    with cliente:
        with cliente.websocket_connect("/ws/sinais") as ws:
            assert ws.receive_json()["tipo"] == "snapshot"
            eventos = [
                Evento("atualizado", {"id": "a", "par": "BTC/USDT"}, False),
                Evento("novo", {"id": "b", "par": "ETH/USDT"}, True),
                Evento("encerrado", {"id": "c", "par": "SOL/USDT"}, True),
            ]
            publicar(cliente, eventos)
            assert [ws.receive_json() for _ in eventos] == [{"tipo": e.tipo, "sinal": e.sinal} for e in eventos]


def test_ws_entrega_os_eventos_de_verdade_do_motor(cliente, motor):
    with cliente:
        with cliente.websocket_connect("/ws/sinais") as ws:
            ws.receive_json()
            cliente.portal.call(lambda: cliente.app.state.transmissor.publicar(motor.ciclo(agora=AGORA + 0.5)))
            mensagem = ws.receive_json()
    assert mensagem["tipo"] == "atualizado" and mensagem["sinal"]["par"] == "BTC/USDT"
    assert mensagem["sinal"]["id"] == motor.gerenciador.ativos()[0]["id"]


def test_ws_todos_os_clientes_recebem_tudo_e_o_que_chegou_antes_nao_se_repete(cliente):
    with cliente:
        with cliente.websocket_connect("/ws/sinais") as primeiro:
            primeiro.receive_json()
            publicar(cliente, [Evento("novo", {"id": "antes"}, True)])
            assert primeiro.receive_json()["sinal"]["id"] == "antes"
            with cliente.websocket_connect("/ws/sinais") as segundo:
                assert segundo.receive_json()["tipo"] == "snapshot"  # o "antes" nao e reenviado ao segundo
                publicar(cliente, [Evento("novo", {"id": "depois"}, True)])
                assert primeiro.receive_json()["sinal"]["id"] == "depois"
                assert segundo.receive_json()["sinal"]["id"] == "depois"
            assert cliente.app.state.transmissor.clientes == 1
        assert cliente.app.state.transmissor.clientes == 0


def test_ws_cliente_lento_e_derrubado_com_1013_e_nao_trava_a_publicacao(motor, banco):
    transmissor = Transmissor(tamanho_fila=3)
    cliente = montar_cliente(motor, banco, transmissor=transmissor)
    with cliente:
        with cliente.websocket_connect("/ws/sinais") as ws:
            ws.receive_json()
            # Cinco eventos de uma vez (o laco do servidor nao teve chance de esvaziar a fila): estoura.
            publicar(cliente, [Evento("atualizado", {"id": str(i)}, False) for i in range(5)])
            assert transmissor.clientes == 0
            with pytest.raises(WebSocketDisconnect) as erro:
                ws.receive_json()
            assert erro.value.code == 1013
        # O servidor segue de pe: um cliente novo entra normalmente.
        with cliente.websocket_connect("/ws/sinais") as outro:
            assert outro.receive_json()["tipo"] == "snapshot"
            publicar(cliente, [Evento("novo", {"id": "x"}, True)])
            assert outro.receive_json()["sinal"]["id"] == "x"


def test_ws_exige_senha(cliente_com_senha):
    for cabecalhos in ({}, basic("jr", "errada"), basic("outro", "segredo"), {"Authorization": "Bearer x"}):
        with pytest.raises(WebSocketDisconnect) as erro:
            with cliente_com_senha.websocket_connect("/ws/sinais", headers=cabecalhos):
                pass
        assert erro.value.code == 1008
    assert cliente_com_senha.app.state.transmissor.clientes == 0


def test_ws_com_senha_certa_conecta(cliente_com_senha):
    with cliente_com_senha.websocket_connect("/ws/sinais", headers=basic("jr", "segredo")) as ws:
        assert ws.receive_json()["tipo"] == "snapshot"


# ---------------------------------------------------------------------------
# Transmissor (sem o servidor)
# ---------------------------------------------------------------------------


def evento(i, tipo="atualizado"):
    return Evento(tipo, {"id": str(i), "par": "BTC/USDT"}, False)


def test_transmissor_entrega_json_de_tipo_e_sinal_a_cada_inscrito():
    async def cenario():
        t = Transmissor()
        a, b = t.inscrever(), t.inscrever()
        t.publicar([evento(1, "novo"), evento(2)])
        for fila in (a, b):
            assert [json.loads(fila.get_nowait()) for _ in range(2)] == [
                {"tipo": "novo", "sinal": {"id": "1", "par": "BTC/USDT"}},
                {"tipo": "atualizado", "sinal": {"id": "2", "par": "BTC/USDT"}},
            ]
            assert fila.empty()

    asyncio.run(cenario())


def test_transmissor_sem_inscritos_ou_sem_eventos_nao_faz_nada():
    async def cenario():
        t = Transmissor()
        t.publicar([evento(1)])  # ninguem ouvindo
        fila = t.inscrever()
        t.publicar([])
        assert fila.empty() and t.clientes == 1

    asyncio.run(cenario())


def test_transmissor_remover_para_de_entregar():
    async def cenario():
        t = Transmissor()
        fila = t.inscrever()
        t.remover(fila)
        t.remover(fila)  # remover duas vezes nao da erro
        t.publicar([evento(1)])
        assert fila.empty() and t.clientes == 0

    asyncio.run(cenario())


def test_transmissor_derruba_o_cliente_lento_e_nao_atrapalha_o_rapido():
    async def cenario():
        t = Transmissor(tamanho_fila=3)
        lento, rapido = t.inscrever(), t.inscrever()
        recebidos = []
        for i in range(0, 8, 2):  # 4 lotes de 2 eventos
            t.publicar([evento(i), evento(i + 1)])
            while not rapido.empty():  # o rapido esvazia a fila a cada ciclo
                recebidos.append(json.loads(rapido.get_nowait())["sinal"]["id"])
        assert recebidos == [str(i) for i in range(8)]
        assert t.clientes == 1  # so o rapido ficou
        # O lento foi esvaziado e recebeu o aviso de fim (None): quem le a fila sabe que deve fechar.
        assert lento.get_nowait() is None and lento.empty()

    asyncio.run(cenario())


def test_publicar_nao_espera_nem_levanta_erro_com_cliente_parado():
    async def cenario():
        t = Transmissor(tamanho_fila=10)
        t.inscrever()  # nunca le
        inicio = time.perf_counter()
        t.publicar([evento(i) for i in range(10_000)])
        assert time.perf_counter() - inicio < 2.0
        assert t.clientes == 0

    asyncio.run(cenario())
    assert not inspect.iscoroutinefunction(Transmissor.publicar)  # funcao comum: o laco do motor nao espera


def test_transmissor_precisa_de_fila_com_pelo_menos_um_lugar():
    with pytest.raises(ValueError, match="tamanho_fila"):
        Transmissor(tamanho_fila=0)


# ---------------------------------------------------------------------------
# __main__: ligacao da API ao laco do motor
# ---------------------------------------------------------------------------


def rodar_com_painel_espiao(tmp_path, monkeypatch, ciclos=2, **campos_config):
    """Roda o scanner com corretoras falsas e um painel falso que se inscreve no transmissor. Devolve o que viu."""
    visto = {}

    async def painel_espiao(app, cfg):
        visto["app"], visto["cfg"] = app, cfg
        visto["fila"] = app.state.transmissor.inscrever()
        visto["clientes_no_inicio"] = app.state.transmissor.clientes
        await asyncio.sleep(3600)

    monkeypatch.setattr(principal, "servir_api", painel_espiao)
    exchanges = exchanges_falsas()
    config = config_servico(tmp_path, **campos_config)
    asyncio.run(asyncio.wait_for(
        principal.rodar(config, criar_exchange=exchanges.__getitem__, relogio=lambda: 1000.0, ciclos=ciclos),
        timeout=5))
    visto["config"] = config
    return visto


def test_rodar_entrega_o_app_ao_servidor_e_publica_os_eventos_de_cada_ciclo(tmp_path, monkeypatch):
    visto = rodar_com_painel_espiao(tmp_path, monkeypatch)
    assert visto["cfg"] is visto["config"] and visto["clientes_no_inicio"] == 1
    mensagens = []
    while not visto["fila"].empty():
        mensagens.append(json.loads(visto["fila"].get_nowait()))
    assert [m["tipo"] for m in mensagens][0] == "novo"  # o motor achou o BTC gate x bitget
    assert mensagens[0]["sinal"]["par"] == "BTC/USDT"
    assert set(mensagens[0]) == {"tipo", "sinal"}
    # O app entregue e o de verdade, com as rotas e o motor da execucao.
    assert isinstance(visto["app"].state.motor, Motor) and visto["app"].state.banco is not None


def test_rodar_recusa_painel_com_usuario_sem_senha_e_fecha_tudo(tmp_path):
    exchanges = exchanges_falsas()
    config = config_servico(tmp_path, painel={"host": "127.0.0.1", "porta": 8000, "usuario": "jr", "senha": None})
    with pytest.raises(ErroConfig, match="painel.senha"):
        asyncio.run(principal.rodar(config, criar_exchange=exchanges.__getitem__, ciclos=1))
    assert all(ex.fechada for ex in exchanges.values())  # as corretoras ja criadas foram fechadas
    assert exchanges["gate"].tickers_pedidos == [] and exchanges["bitget"].tickers_pedidos == []  # sem conectar


def test_erro_ao_transmitir_vai_para_o_log_e_o_motor_segue(tmp_path, monkeypatch, caplog):
    def publicar_com_defeito(self, eventos):
        raise RuntimeError("defeito ao transmitir")

    monkeypatch.setattr(Transmissor, "publicar", publicar_com_defeito)
    chamadas = []
    original = Motor.ciclo

    def ciclo_contando(self, agora):
        chamadas.append(agora)
        return original(self, agora)

    monkeypatch.setattr(Motor, "ciclo", ciclo_contando)
    exchanges = exchanges_falsas()
    with caplog.at_level(logging.ERROR, logger="scanner"):
        asyncio.run(asyncio.wait_for(principal.rodar(
            config_servico(tmp_path), criar_exchange=exchanges.__getitem__, relogio=lambda: 1000.0, ciclos=3),
            timeout=5))
    assert len(chamadas) == 3  # o defeito nao parou o laco
    assert any("defeito ao transmitir" in r.getMessage() or "defeito ao transmitir" in str(r.exc_info)
               for r in caplog.records)
    # E o ajuste dos livros (etapa 2) continuou rodando depois do defeito.
    assert exchanges["gate"].livros_pedidos and exchanges["bitget"].livros_pedidos


def test_painel_que_nao_abre_vai_para_o_log_e_o_scanner_segue(tmp_path, monkeypatch, caplog):
    async def painel_quebrado(app, cfg):
        raise RuntimeError("porta ocupada")

    monkeypatch.setattr(principal, "servir_api", painel_quebrado)
    exchanges = exchanges_falsas()
    with caplog.at_level(logging.ERROR, logger="scanner"):
        asyncio.run(asyncio.wait_for(principal.rodar(
            config_servico(tmp_path), criar_exchange=exchanges.__getitem__, relogio=lambda: 1000.0, ciclos=2),
            timeout=5))
    assert any("painel" in r.getMessage() and "porta ocupada" in r.getMessage() for r in caplog.records)
    assert all(ex.fechada for ex in exchanges.values())


# ---------------------------------------------------------------------------
# servir_api: o uvicorn de verdade, no mesmo laco do asyncio
# ---------------------------------------------------------------------------


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def config_do_painel(motor, porta):
    return replace(motor.cfg, painel={"host": "127.0.0.1", "porta": porta, "usuario": "jr", "senha": "segredo"})


def test_servir_api_sobe_responde_e_para_limpo_ao_ser_cancelado(motor, banco):
    porta = porta_livre()
    config = config_do_painel(motor, porta)
    app = criar_app(motor, banco, config, relogio=lambda: AGORA)

    async def cenario():
        sigint_antes = signal.getsignal(signal.SIGINT)  # o do asyncio.run, que cancela o programa no Ctrl+C
        tarefa = asyncio.create_task(servir_api(app, config))
        async with httpx.AsyncClient() as http:
            for _ in range(200):  # espera ate ~10 s o servidor abrir a porta
                try:
                    resposta = await http.get(f"http://127.0.0.1:{porta}/api/sinais", auth=("jr", "segredo"))
                    break
                except httpx.ConnectError:
                    assert not tarefa.done(), tarefa.exception()
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("o servidor nao abriu a porta")
            assert resposta.status_code == 200 and resposta.json()[0]["par"] == "BTC/USDT"
            assert (await http.get(f"http://127.0.0.1:{porta}/api/sinais")).status_code == 401
            assert signal.getsignal(signal.SIGINT) is sigint_antes  # o uvicorn nao roubou o Ctrl+C do programa
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa
        with pytest.raises(OSError):  # a porta foi solta
            socket.create_connection(("127.0.0.1", porta), timeout=1).close()
        assert signal.getsignal(signal.SIGINT) is sigint_antes

    asyncio.run(asyncio.wait_for(cenario(), timeout=15))


def test_servir_api_avisa_quando_falta_a_biblioteca_de_websocket(monkeypatch, caplog):
    monkeypatch.setattr(modulo_servidor.importlib.util, "find_spec", lambda nome: None)
    with caplog.at_level(logging.WARNING, logger="scanner"):
        modulo_servidor.avisar_se_faltar_websocket()
    assert any("websockets" in r.getMessage() for r in caplog.records)


def test_servir_api_nao_avisa_quando_a_biblioteca_existe(monkeypatch, caplog):
    monkeypatch.setattr(modulo_servidor.importlib.util, "find_spec", lambda nome: object())
    with caplog.at_level(logging.WARNING, logger="scanner"):
        modulo_servidor.avisar_se_faltar_websocket()
    assert caplog.records == []
