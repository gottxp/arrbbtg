"""Testes do painel web (Task 12): os arquivos estaticos servidos em / pela API.

O comportamento da pagina (tabela, filtros, grafico) e conferido no navegador; aqui ficam as garantias
que da para checar sem navegador: os arquivos sao servidos com o tipo certo, a senha vale para eles, as
rotas /api e o WebSocket continuam funcionando com o painel montado em /, todo campo dos `Filtros` tem
um controle no painel e o JavaScript nao usa o que as regras do projeto proibem.
"""

import base64
import dataclasses
import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from scanner.api import criar_app
from scanner.armazenamento import Banco
from scanner.filtros import Filtros
from scanner.motor import chave_de, texto_da_chave
from tests.fabricas import motor_falso

# Horario do estado criado pelo motor_falso (as pontas "chegaram" em t = 10 s).
AGORA = 10.0
CHAVE_TEXTO = texto_da_chave(chave_de(("BTC", "gate", "spot", "bitget", "futuro")))

PASTA_PAINEL = Path(__file__).resolve().parent.parent / "scanner" / "painel"
ARQUIVOS_DO_PAINEL = ("index.html", "app.js", "estilo.css")

# A unica biblioteca externa do painel, com a versao fixa (decisao do projeto).
URL_GRAFICOS = "https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"


# ---------------------------------------------------------------------------
# Fixtures e apoio
# ---------------------------------------------------------------------------


@pytest.fixture
def banco(tmp_path):
    b = Banco(str(tmp_path / "painel.db"))
    yield b
    b.fechar()


@pytest.fixture
def motor(banco):
    """Motor com um sinal ativo: spot da Gate (100) x futuro da Bitget (101)."""
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), f_medio=-0.5, banco=banco)
    m.ciclo(agora=AGORA)
    return m


def montar_cliente(motor, banco, painel=None) -> TestClient:
    """TestClient da API com o painel; `painel` troca campos do bloco painel da Config (usuario, senha)."""
    config = replace(motor.cfg, painel={**motor.cfg.painel, **(painel or {})})
    return TestClient(criar_app(motor, banco, config, relogio=lambda: AGORA))


@pytest.fixture
def cliente(motor, banco):
    return montar_cliente(motor, banco)


@pytest.fixture
def cliente_com_senha(motor, banco):
    return montar_cliente(motor, banco, painel={"usuario": "jr", "senha": "segredo"})


def basic(usuario: str, senha: str) -> dict:
    """O cabecalho Authorization de uma conexao WebSocket (o TestClient so monta o do HTTP sozinho)."""
    return {"Authorization": "Basic " + base64.b64encode(f"{usuario}:{senha}".encode()).decode()}


def ler(nome: str) -> str:
    return (PASTA_PAINEL / nome).read_text(encoding="utf-8")


def todo_o_painel() -> str:
    return "\n".join(ler(nome) for nome in ARQUIVOS_DO_PAINEL)


# ---------------------------------------------------------------------------
# Arquivos servidos em /
# ---------------------------------------------------------------------------


def test_raiz_devolve_o_html_do_painel(cliente):
    r = cliente.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert 'src="app.js"' in html
    assert 'href="estilo.css"' in html
    assert URL_GRAFICOS in html


@pytest.mark.parametrize("caminho, tipo", [
    ("/app.js", "text/javascript"),
    ("/estilo.css", "text/css"),
    ("/index.html", "text/html"),
])
def test_arquivos_do_painel_com_o_tipo_certo(cliente, caminho, tipo):
    r = cliente.get(caminho)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(tipo)
    assert r.content == (PASTA_PAINEL / caminho.lstrip("/")).read_bytes()


def test_navegador_sempre_confere_se_o_arquivo_mudou(cliente):
    # Depois de atualizar o scanner, o navegador nao pode ficar com o app.js velho guardado.
    for caminho in ("/", "/app.js", "/estilo.css"):
        assert cliente.get(caminho).headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("caminho", ["/nao-existe", "/nao-existe.js", "/painel/app.js"])
def test_arquivo_que_nao_existe_da_404(cliente, caminho):
    assert cliente.get(caminho).status_code == 404


@pytest.mark.parametrize("caminho, arquivo_que_existe", [
    ("/%2e%2e/api/app.py", "scanner/api/app.py"),          # um nivel acima da pasta do painel
    ("/%2e%2e/%2e%2e/config.yaml", "config.yaml"),         # a raiz do projeto
    ("/%2E%2E/%2E%2E/config.yaml", "config.yaml"),
])
def test_travessia_de_pasta_nao_sai_do_painel(cliente, caminho, arquivo_que_existe):
    # Os ".." codificados chegam ao servidor como estao (o httpx so normaliza ".." sem codificar), e o
    # arquivo de destino existe de verdade: o 404 e a protecao do StaticFiles, nao um arquivo que falta.
    assert httpx.URL("http://testserver" + caminho).raw_path == caminho.encode()
    assert (PASTA_PAINEL.parent.parent / arquivo_que_existe).is_file()
    assert cliente.get(caminho).status_code == 404


def test_pasta_do_painel_e_achada_a_partir_do_modulo(monkeypatch, tmp_path, motor, banco):
    # O scanner pode ser iniciado de qualquer pasta (Windows ou Linux): o caminho nao depende do diretorio atual.
    monkeypatch.chdir(tmp_path)
    r = montar_cliente(motor, banco).get("/app.js")
    assert r.status_code == 200 and r.content == (PASTA_PAINEL / "app.js").read_bytes()


# ---------------------------------------------------------------------------
# Senha
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caminho", ["/", "/app.js", "/estilo.css", "/index.html", "/nao-existe"])
def test_arquivos_do_painel_exigem_senha(cliente_com_senha, caminho):
    r = cliente_com_senha.get(caminho)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")
    assert cliente_com_senha.get(caminho, auth=("jr", "errada")).status_code == 401


@pytest.mark.parametrize("caminho", ["/", "/app.js", "/estilo.css"])
def test_arquivos_do_painel_com_a_senha_certa(cliente_com_senha, caminho):
    assert cliente_com_senha.get(caminho, auth=("jr", "segredo")).status_code == 200


# ---------------------------------------------------------------------------
# O painel em / nao esconde a API nem o WebSocket
# ---------------------------------------------------------------------------


def test_rotas_da_api_continuam_funcionando_com_o_painel_montado(cliente, motor, banco):
    sinais = cliente.get("/api/sinais")
    assert sinais.status_code == 200 and sinais.json() == motor.sinais_ativos()
    assert cliente.get("/api/status").json()["gate"]["estado"] == "conectada"
    assert cliente.get("/api/sessao").json()["modo"] == "lucro"
    assert cliente.put("/api/sessao", json={"modo": "farm", "filtros": {}, "comparacao": True}).status_code == 200
    assert cliente.post("/api/filtros/Farm", json={"volume_min_usd": 1}).status_code == 200
    assert cliente.get("/api/filtros").json()["Farm"]["volume_min_usd"] == 1
    assert cliente.delete("/api/filtros/Farm").status_code == 200
    banco.gravar_amostras([(CHAVE_TEXTO, AGORA - i, 0.1, -0.1, 100.0, 101.0, 1000.0, 2000.0) for i in range(3)])
    historico = cliente.get("/api/historico/" + quote(CHAVE_TEXTO, safe=""), params={"intencao": "fechamento"})
    assert historico.status_code == 200 and len(historico.json()["amostras"]) == 3


def test_erros_da_api_continuam_em_json(cliente):
    r = cliente.put("/api/sessao", json={"modo": "outro", "filtros": {}, "comparacao": False})
    assert r.status_code == 400 and "modo" in r.json()["detail"]
    assert cliente.delete("/api/filtros/Fantasma").status_code == 404


def test_websocket_continua_funcionando_com_o_painel_montado(cliente, motor):
    with cliente.websocket_connect("/ws/sinais") as ws:
        assert ws.receive_json() == {"tipo": "snapshot", "sinais": motor.sinais_ativos()}


def test_websocket_com_senha_continua_igual(cliente_com_senha):
    with pytest.raises(WebSocketDisconnect) as erro:
        with cliente_com_senha.websocket_connect("/ws/sinais"):
            pass
    assert erro.value.code == 1008
    with cliente_com_senha.websocket_connect("/ws/sinais", headers=basic("jr", "segredo")) as ws:
        assert ws.receive_json()["tipo"] == "snapshot"


def test_websocket_em_endereco_desconhecido_e_recusado_sem_erro(cliente):
    with pytest.raises(WebSocketDisconnect):
        with cliente.websocket_connect("/ws/outro") as ws:
            ws.receive_json()


# ---------------------------------------------------------------------------
# Guardas do conteudo dos arquivos
# ---------------------------------------------------------------------------


def test_todo_campo_dos_filtros_tem_um_controle_no_painel():
    # Um campo novo nos Filtros sem controle no painel faz este teste falhar.
    painel = todo_o_painel()
    faltando = [c.name for c in dataclasses.fields(Filtros) if f'data-campo="{c.name}"' not in painel]
    assert faltando == []


def test_o_painel_so_usa_nomes_de_campos_que_existem_nos_filtros():
    # Um erro de digitacao no nome (ex.: "volume_minimo") seria recusado pelo servidor.
    usados = set(re.findall(r'data-campo="([^"]+)"', todo_o_painel()))
    assert usados - {c.name for c in dataclasses.fields(Filtros)} == set()


def test_nenhum_recurso_externo_alem_do_lightweight_charts():
    externos = set(re.findall(r"""(?:src|href)\s*=\s*["']((?:https?:)?//[^"']+)""", ler("index.html")))
    assert externos == {URL_GRAFICOS}
    assert "@import" not in ler("estilo.css") and "url(" not in ler("estilo.css")  # sem fontes ou imagens de fora
    # No app.js, o unico endereco e o nome do padrao SVG (exigido pelo createElementNS; nada e baixado dele).
    assert set(re.findall(r"https?://[^\s\"'`]+", ler("app.js"))) <= {"http://www.w3.org/2000/svg"}


def test_javascript_nao_usa_innerhtml_nem_caixas_do_navegador():
    # Dados do servidor (nomes de simbolos vem das corretoras) entram sempre por textContent/createElement.
    codigo = ler("app.js")
    for proibido in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert proibido not in codigo, proibido
    # Confirmacoes e nomes de grupo sao pedidos dentro da propria pagina.
    assert re.findall(r"\b(?:prompt|confirm|alert)\s*\(", codigo) == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node nao esta instalado")
def test_app_js_sem_erro_de_sintaxe():
    resultado = subprocess.run(["node", "--check", str(PASTA_PAINEL / "app.js")], capture_output=True, text=True)
    assert resultado.returncode == 0, resultado.stderr


# ---------------------------------------------------------------------------
# Funcoes puras do app.js rodando no node (leitura de numeros e ordenacao)
# ---------------------------------------------------------------------------
#
# O app.js funciona no navegador e, sem a pagina (no node), so exporta as funcoes puras. Os testes
# abaixo mandam os casos como JSON e conferem o que as funcoes devolvem.

sem_node = pytest.mark.skipif(shutil.which("node") is None, reason="node nao esta instalado")


def rodar_no_node(codigo: str, dados):
    """Carrega o app.js no node (`painel` = funcoes exportadas), roda `codigo` e devolve o JSON que ele imprimir."""
    programa = (
        f"const painel = require({json.dumps(str(PASTA_PAINEL / 'app.js'))});\n"
        "const dados = JSON.parse(process.argv[1]);\n" + codigo
    )
    resultado = subprocess.run(["node", "-e", programa, json.dumps(dados)], capture_output=True, text=True, timeout=60)
    assert resultado.returncode == 0, resultado.stderr
    return json.loads(resultado.stdout)


def ler_numeros(textos: list[str], dolar: bool) -> list:
    return rodar_no_node("console.log(JSON.stringify(dados.textos.map((t) => painel.lerNumeroDigitado(t, dados.dolar))));",
                         {"textos": textos, "dolar": dolar})


CAMPOS_EM_DOLAR = [
    ("50.000", 50000), ("1.000.000", 1000000), ("1.000,50", 1000.5), ("12.345.678,9", 12345678.9),
    ("1.5", 1.5), ("1.0000", 1), ("0.500", 0.5), ("1234.5", 1234.5), (".5", 0.5), (",5", 0.5),
    ("500", 500), ("0,5", 0.5), ("1,5", 1.5), ("-1.000", -1000), ("2 500", 2500), ("$1.000", 1000),
]
INVALIDOS_EM_DOLAR = ["abc", "1,000.50", "1.00.0", "1.000.5", "1,2,3", "1.", "1,", ",", "1.000,", "1e5", "--1", "0.000.000"]


@sem_node
def test_numeros_em_dolar_ponto_com_tres_digitos_e_milhar():
    textos = [texto for texto, _ in CAMPOS_EM_DOLAR]
    assert dict(zip(textos, ler_numeros(textos, dolar=True))) == dict(CAMPOS_EM_DOLAR)


@sem_node
def test_numeros_em_dolar_ambiguos_ou_invalidos_sao_recusados():
    assert ler_numeros(INVALIDOS_EM_DOLAR, dolar=True) == [None] * len(INVALIDOS_EM_DOLAR)


@sem_node
def test_numeros_em_porcentagem_horas_e_contagens_o_ponto_e_decimal():
    casos = [("0,5", 0.5), ("0.5", 0.5), ("1.000", 1), ("1.5", 1.5), ("-0,1", -0.1), (".5", 0.5), ("10", 10)]
    invalidos = ["1.000,5", "1,2.3", "1..2", "abc", "1.", "1e5"]
    textos = [texto for texto, _ in casos]
    assert dict(zip(textos, ler_numeros(textos, dolar=False))) == dict(casos)
    assert ler_numeros(invalidos, dolar=False) == [None] * len(invalidos)


@sem_node
def test_numero_mostrado_no_campo_em_formato_brasileiro_e_lido_de_volta_igual():
    valores_dolar = [50000, 1000.5, 1.5, 0.125, 1500, 999, 1234567.89, -1000, 0.1]
    valores_decimais = [0.5, 1000, -0.1, 0.0000001, 12.25]
    resultado = rodar_no_node(
        "const ida = (v, d) => painel.numeroParaCampo(v, d);"
        "console.log(JSON.stringify({"
        " dolar: dados.dolar.map((v) => [ida(v, true), painel.lerNumeroDigitado(ida(v, true), true)]),"
        " decimal: dados.decimal.map((v) => [ida(v, false), painel.lerNumeroDigitado(ida(v, false), false)])"
        "}));",
        {"dolar": valores_dolar, "decimal": valores_decimais},
    )
    assert [texto for texto, _ in resultado["dolar"]][:3] == ["50.000", "1.000,5", "1,5"]
    assert [texto for texto, _ in resultado["decimal"]][:2] == ["0,5", "1000"]
    assert [valor for _, valor in resultado["dolar"]] == valores_dolar
    assert [valor for _, valor in resultado["decimal"]] == valores_decimais


def sinal_para_ordenar(par, rp_liquido, rp_bruto=0.0, custo=None, a=0.0, detectado="2026-10-22T10:00:00.000Z",
                       volume_long=1000.0, volume_short=1000.0):
    ponta = lambda corretora, mercado, volume: {"corretora": corretora, "mercado": mercado, "volume_24h_usd": volume}  # noqa: E731
    return {"par": par, "tipo": "spot_futuro", "ponta_long": ponta("gate", "spot", volume_long),
            "ponta_short": ponta("bitget", "futuro", volume_short), "rp_liquido_pct": rp_liquido,
            "rp_bruto_pct": rp_bruto, "custo_por_1000_usd": custo, "spread_abertura_pct": a, "detectado_em": detectado}


SINAIS_PARA_ORDENAR = [
    sinal_para_ordenar("A/USDT", 0.3, rp_bruto=0.9, custo=-1.5, a=0.5, detectado="2026-10-22T10:00:03.000Z", volume_long=50),
    sinal_para_ordenar("B/USDT", 0.5, rp_bruto=0.6, custo=None, a=0.9, detectado="2026-10-22T10:00:01.000Z"),
    sinal_para_ordenar("C/USDT", 0.1, rp_bruto=0.7, custo=0.4, a=0.2, detectado="2026-10-22T10:00:02.000Z",
                       volume_long=None),
    sinal_para_ordenar("D/USDT", 0.5, rp_bruto=0.1, custo=0.2, a=0.3, detectado="2026-10-22T10:00:00.000Z",
                       volume_long=3000, volume_short=5000),
]


def ordem(criterio, modo="lucro", comparacao=False, fixados=()):
    resultado = rodar_no_node(
        "painel.estado.modo = dados.modo; painel.estado.comparacao = dados.comparacao;"
        "const lista = dados.sinais.map((s) => { const chave = painel.chaveDoSinal(s);"
        " return { chave, sinal: s, fixado: dados.fixados.includes(s.par) }; });"
        "painel.ordenar(lista, dados.criterio);"
        "console.log(JSON.stringify(lista.map((item) => item.sinal.par[0])));",
        {"sinais": SINAIS_PARA_ORDENAR, "criterio": criterio, "modo": modo, "comparacao": comparacao,
         "fixados": list(fixados)},
    )
    return "".join(resultado)


@sem_node
def test_ordenacao_do_painel():
    assert ordem(None) == "BDAC"  # RP liquido decrescente; empate (B e D) pela chave
    assert ordem("resultado", comparacao=True) == "ACBD"  # comparacao: RP bruto
    assert ordem(None, modo="farm") == "ADCB"  # Farm: menor custo primeiro; sem custo por ultimo
    assert ordem("horario") == "ACBD"  # mais recente primeiro
    assert ordem("spread") == "BADC"  # A atual decrescente
    assert ordem("volume") == "DBAC"  # o menor volume das duas pontas; volume desconhecido por ultimo
    assert ordem(None, fixados=["C/USDT", "A/USDT"]) == "ACBD"  # fixados no topo, na mesma ordem do criterio


@sem_node
def test_chave_do_sinal_igual_a_do_motor():
    sinal = sinal_para_ordenar("BTC/USDT", 0.1)
    assert rodar_no_node("console.log(JSON.stringify(painel.chaveDoSinal(dados)));", sinal) == CHAVE_TEXTO
