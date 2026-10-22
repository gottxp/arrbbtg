"""API do painel (especificacao, secao 6): FastAPI com as rotas /api e o WebSocket /ws/sinais.

`criar_app(motor, banco, cfg)` monta o app. Rotas (todas sob /api, menos o WebSocket):

- GET    /api/sinais                 sinais ativos agora (JSON da spec, secao 5)
- GET    /api/historico/{chave}      amostras de spread de um par + resumo (grafico do painel)
- GET    /api/filtros                grupos de filtros salvos: {nome: filtros}
- POST   /api/filtros/{nome}         salva (ou substitui) um grupo
- DELETE /api/filtros/{nome}         apaga um grupo
- GET    /api/sessao, PUT /api/sessao   modo, filtros e comparacao em uso pelo motor
- GET    /api/status                 saude de cada corretora
- GET    /api/validacao?dia=AAAA-MM-DD   metricas de validacao do dia UTC (padrao: hoje), por modo e corretora
- WS     /ws/sinais                  um retrato dos sinais ao conectar, depois cada evento do motor e o
                                     {"tipo": "vivo"} do Transmissor (scanner/api/transmissor.py)
- GET    /                           o painel web (arquivos de scanner/painel: index.html, app.js, estilo.css)

Protecoes (middlewares: rotas e arquivos novos ja nascem protegidos):
- Senha: com `painel.usuario` no config.yaml, TUDO exige HTTP Basic (rotas /api, WebSocket e os
  arquivos do painel). Sem senha, o painel so sobe com host da propria maquina (veja `conferir_painel`).
- Outro site: mudancas (PUT, POST, DELETE) e o WebSocket pedidos por uma pagina de outro site sao
  recusados (veja `RecusarOutroSite`).
- Sem senha, o cabecalho Host precisa ser um nome da propria maquina (localhost, 127.0.0.1, [::1]):
  protege contra DNS rebinding (um site cujo nome passa a apontar para 127.0.0.1).

Threads: as rotas que leem ou mudam o motor sao `async def`, ou seja, rodam no laco do asyncio,
o mesmo do motor, e nunca no meio de um ciclo. As que so falam com o banco (que tem trava propria)
sao `def` comuns e rodam fora do laco, para uma consulta demorada nao parar o motor. A excecao e a
/api/validacao: e `def` (le o dia inteiro do banco), mas para hoje le o motor.status no laco do motor,
com `anyio.from_thread.run_sync`.
"""

import asyncio
import base64
import heapq
import ipaddress
import json
import logging
import mimetypes
import secrets
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import anyio
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from scanner.api.transmissor import Transmissor
from scanner.armazenamento import Banco
from scanner.coletores.lighter_formatos import e_lighter
from scanner.config.carregar import Config, ErroConfig
from scanner.filtros import Filtros
from scanner.metricas.janela import MAIORES_N
from scanner.motor import MODOS, SEGUNDOS_POR_HORA, Motor, Sessao
from scanner.validacao import dia_utc, resumo_do_dia

registro = logging.getLogger("scanner.api")

# Codigos de fechamento do WebSocket.
# CODIGO_RECUSADO (1008, "violacao de politica"): senha errada ou pedido de outro site, fechado ANTES de
#   aceitar. So o TestClient mostra o 1008: no uvicorn de verdade o navegador recebe HTTP 403 e o painel
#   ve uma falha comum de conexao.
# CODIGO_PAINEL_LENTO (1013, "tente de novo mais tarde"): painel lento demais, derrubado pelo Transmissor
#   (ele reconecta e recebe o snapshot de novo).
CODIGO_RECUSADO = 1008
CODIGO_PAINEL_LENTO = 1013

# Sem senha, so estes nomes sao aceitos no cabecalho Host (com qualquer porta): os da propria maquina,
# mais o proprio painel.host (veja nome_no_cabecalho_host).
NOMES_DA_MAQUINA = ["localhost", "127.0.0.1", "[::1]"]

# Corretoras que, no ccxt, so tem um mercado: a kraken so spot e a krakenfutures so futuros. Valem mesmo
# sem a linha delas em mercados_por_corretora (sem isto, o painel mostraria opcoes que nao casam com nada).
MERCADOS_FIXOS = {"kraken": ("spot",), "krakenfutures": ("futuro",)}

# Metodos que so leem: passam mesmo com Origin de outro site (o navegador nao entrega a resposta a ele).
METODOS_DE_LEITURA = ("GET", "HEAD")

# A pasta do painel, achada a partir deste arquivo (scanner/api/app.py -> scanner/painel): funciona
# de qualquer pasta em que o scanner for iniciado, no Windows e no Linux.
PASTA_PAINEL = Path(__file__).resolve().parent.parent / "painel"

# O tipo de cada arquivo vem do modulo `mimetypes`, que no Windows le o registro do sistema; em
# alguns PCs o registro diz que .js e "text/plain", e o navegador pode recusar o script. Aqui o
# tipo certo fica garantido, em qualquer sistema.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/html", ".html")


# ---------------------------------------------------------------------------
# Senha (HTTP Basic)
# ---------------------------------------------------------------------------


def host_da_propria_maquina(host: str) -> bool:
    """True para "localhost" e enderecos de loopback (127.x.x.x, ::1): so a propria maquina alcanca o painel.

    Qualquer outro endereco (0.0.0.0, o IP da rede, um nome como "meu-pc.local") pode ser aberto de fora.
    """
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # nao e um IP (e um nome): pode apontar para fora
        return False


def nome_no_cabecalho_host(host: str) -> str:
    """Como o navegador escreve o `host` no cabecalho Host: IPv6 entre colchetes e na forma curta, ex.: "[::1]"."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:  # um nome (ex.: localhost)
        return host.lower()
    return f"[{ip.compressed}]" if ip.version == 6 else ip.compressed


def conferir_painel(cfg: Config) -> None:
    """Recusa (ErroConfig) um bloco `painel` que deixaria o painel aberto sem o dono querer.

    Param o scanner com uma mensagem clara, em vez de subir sem protecao ou trancar o dono para fora:
    - host que abre o painel para fora da maquina (ex.: 0.0.0.0) sem usuario e senha (especificacao, secao 10);
    - usuario sem senha ou senha sem usuario;
    - usuario vazio ou com ":" (o HTTP Basic nao aceita).
    Vale ao ligar e no --validar (o __main__ chama esta funcao antes de conectar em qualquer coisa).
    """
    usuario, senha = cfg.painel.get("usuario"), cfg.painel.get("senha")
    if usuario is None:
        if senha:
            raise ErroConfig("painel.usuario: a senha esta preenchida mas falta o usuario; preencha painel.usuario "
                             "no config.yaml (ou apague painel.senha para deixar o painel aberto)")
        host = cfg.painel["host"]
        if not host_da_propria_maquina(host):
            raise ErroConfig(f"painel.host {host} abre o painel para fora desta maquina, e ele esta sem senha: "
                             "preencha painel.usuario e painel.senha no config.yaml (especificacao, secao 10), "
                             "ou use host: 127.0.0.1 e abra o painel pelo tunel SSH (README, parte 3.5)")
        return
    if not usuario or ":" in usuario:
        raise ErroConfig("painel.usuario: precisa ser um texto nao vazio e sem o caractere ':'")
    if not senha:
        raise ErroConfig("painel.senha: o painel tem usuario mas nao tem senha; preencha painel.senha no config.yaml "
                         "(ou apague painel.usuario para deixar o painel aberto)")


class ExigirSenha:
    """Middleware ASGI: so deixa passar quem mandou o usuario e a senha certos (HTTP Basic).

    Vale para pedidos HTTP (resposta 401 com o cabecalho que faz o navegador pedir a senha) e para o
    inicio de conexoes WebSocket (fechadas antes de aceitar: no uvicorn de verdade o navegador recebe
    HTTP 403; so o TestClient mostra o codigo 1008, CODIGO_RECUSADO). O navegador reenvia a senha
    digitada sozinho nas conexoes WebSocket do mesmo endereco.
    """

    def __init__(self, app: ASGIApp, usuario: str, senha: str):
        self.app = app
        self._usuario = usuario.encode("utf-8")
        self._senha = senha.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or self._autorizado(scope):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": CODIGO_RECUSADO})
        else:
            # charset="UTF-8": o navegador manda a senha com acento em UTF-8 (sem isso, alguns usam latin-1).
            resposta = JSONResponse({"detail": "usuario ou senha invalidos"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Basic realm="scanner", charset="UTF-8"'})
            await resposta(scope, receive, send)

    def _autorizado(self, scope: Scope) -> bool:
        cabecalho = next((v for nome, v in scope["headers"] if nome == b"authorization"), b"")
        esquema, _, credencial = cabecalho.decode("latin-1").partition(" ")
        if esquema.lower() != "basic":
            return False
        try:
            usuario, separador, senha = base64.b64decode(credencial.strip(), validate=True).partition(b":")
        except ValueError:  # base64 invalido (binascii.Error e um ValueError)
            return False
        if not separador:
            return False
        # As duas conferencias sempre rodam (nada de parar na primeira): o tempo nao revela qual estava certa.
        usuario_certo = secrets.compare_digest(usuario, self._usuario)
        senha_certa = secrets.compare_digest(senha, self._senha)
        return usuario_certo and senha_certa


def _cabecalho(scope: Scope, nome: bytes) -> str | None:
    """O valor do cabecalho `nome` (em minusculas, ex.: b"origin") do pedido, ou None se nao veio."""
    return next((valor.decode("latin-1") for chave, valor in scope["headers"] if chave == nome), None)


def pedido_de_outro_site(scope: Scope) -> bool:
    """True se o pedido traz o cabecalho Origin e o endereco dele (host:porta) nao e o do cabecalho Host.

    O navegador manda o Origin da pagina que fez o pedido. O painel aberto em http://localhost:8000 manda
    "http://localhost:8000", igual ao Host "localhost:8000". Origin "null" (pagina sem endereco) nunca bate.
    Sem Origin (curl, scripts, a barra de endereco) nao e de outro site.
    """
    origem = _cabecalho(scope, b"origin")
    if origem is None:
        return False
    endereco = urlsplit(origem).netloc.lower() if "://" in origem else ""
    host = (_cabecalho(scope, b"host") or "").lower()
    return not endereco or endereco != host


class RecusarOutroSite:
    """Middleware ASGI: recusa mudancas e o WebSocket pedidos por uma pagina de outro site.

    Um site aberto no mesmo navegador consegue mandar pedidos ao painel (o navegador anexa a senha que
    guardou) e abrir o WebSocket, e cada versao do FastAPI barra uma parte diferente disso. Aqui a regra e
    explicita, para qualquer versao:
    - WebSocket de outro site: fechado antes de aceitar (no uvicorn de verdade, HTTP 403);
    - PUT, POST, DELETE (e qualquer metodo que nao seja GET/HEAD) de outro site: HTTP 403, nada muda.
    GET e HEAD passam: nao mudam nada, e o navegador nao entrega a resposta a outro site (o painel nao
    libera CORS). Sem Origin tambem passa (veja `pedido_de_outro_site`).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        confere = scope["type"] == "websocket" or (
            scope["type"] == "http" and scope["method"] not in METODOS_DE_LEITURA)
        if not confere or not pedido_de_outro_site(scope):
            await self.app(scope, receive, send)
            return
        registro.debug("pedido de outro site recusado: %s %s (Origin %s)", scope.get("method", "WebSocket"),
                       scope["path"], _cabecalho(scope, b"origin"))
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": CODIGO_RECUSADO})
            return
        resposta = JSONResponse({"detail": "pedido recusado: veio de uma pagina de outro site (cabecalho Origin "
                                           "diferente do endereco do painel)"}, status_code=403)
        await resposta(scope, receive, send)


class ArquivosDoPainel(StaticFiles):
    """Os arquivos do painel, com `Cache-Control: no-cache`: o navegador pode guardar uma copia, mas
    confere com o servidor antes de usar (resposta 304 se nao mudou). Assim, depois de atualizar o
    scanner, ninguem fica com um app.js velho na tela.
    """

    def file_response(self, *argumentos, **nomeados):
        resposta = super().file_response(*argumentos, **nomeados)
        resposta.headers["Cache-Control"] = "no-cache"
        return resposta


# ---------------------------------------------------------------------------
# Apoio das rotas
# ---------------------------------------------------------------------------


def mercados_ligados(cfg: Config, corretora: str) -> tuple[str, ...]:
    """Os mercados da corretora que entram no scanner ("spot" e/ou "futuro").

    A Lighter (lighter e lighter_rh, conector proprio) so le futuros; a kraken e a krakenfutures tem um
    mercado so (MERCADOS_FIXOS); as outras usam os de mercados_por_corretora (ou os dois, se ela nao estiver la).
    """
    if e_lighter(corretora):
        return ("futuro",)
    return MERCADOS_FIXOS.get(corretora) or cfg.mercados_da_corretora(corretora)


class CorpoSessao(BaseModel):
    """O corpo do PUT /api/sessao: a sessao INTEIRA (o PUT troca tudo), entao os tres campos sao obrigatorios.

    Chave desconhecida ou campo faltando da 422 e a sessao em uso nao muda: um erro de digitacao no
    painel (ex.: "mode") nao pode apagar em silencio os filtros que o motor esta usando.
    """

    model_config = ConfigDict(extra="forbid")

    modo: str
    filtros: Any  # quem confere e o Filtros.de_dict (erro 400 com a mensagem)
    comparacao: StrictBool


def _filtros_validos(dados: Any, periodo_max_h: float) -> Filtros:
    """Filtros.de_dict + recusa de NaN e infinito (nao existem em JSON e estragariam as comparacoes). ValueError se invalido.

    periodo_max_h: o maior periodo das metricas aceito (retencao_dias x 24: o banco so guarda isso de amostras).
    """
    filtros = Filtros.de_dict(dados, periodo_max_h=periodo_max_h)
    try:
        json.dumps(filtros.para_dict(), allow_nan=False)
    except ValueError:
        raise ValueError("filtros: NaN e infinito nao sao numeros validos") from None
    return filtros


def _sessao_json(sessao: Sessao) -> dict:
    return {"modo": sessao.modo, "filtros": sessao.filtros.para_dict(), "comparacao": sessao.comparacao}


def _media(valores: list[float]) -> float | None:
    return sum(valores) / len(valores) if valores else None


def _resumo(spreads: list[float], lotes_long: list[float], lotes_short: list[float]) -> dict:
    """Maior spread, media dos MAIORES_N maiores, media geral e lote medio de cada ponta (None sem amostras)."""
    maiores = heapq.nlargest(MAIORES_N, spreads)
    return {
        "maior_spread": maiores[0] if maiores else None,
        "media_10_maiores": _media(maiores),
        "spread_medio": _media(spreads),
        "lote_medio_long": _media(lotes_long),
        "lote_medio_short": _media(lotes_short),
    }


def _periodo_padrao_h(motor: Motor, cfg: Config) -> float:
    """O periodo das metricas em uso: o da sessao, se for um numero valido, senao o da Config (como o motor faz)."""
    escolhido = motor.sessao.filtros.periodo_metricas_h
    if isinstance(escolhido, (int, float)) and not isinstance(escolhido, bool) and 0 < escolhido < float("inf"):
        return escolhido
    return cfg.periodo_metricas_h


# ---------------------------------------------------------------------------
# O app
# ---------------------------------------------------------------------------


def criar_app(
    motor: Motor,
    banco: Banco,
    cfg: Config,
    transmissor: Transmissor | None = None,
    relogio: Callable[[], float] = time.time,
) -> FastAPI:
    """Monta a API do painel.

    transmissor: quem entrega os eventos aos paineis; sem ele, cria um (fica em `app.state.transmissor`,
        e o `__main__` chama `publicar` nele a cada ciclo do motor).
    relogio: hora atual em segundos desde 1970 (os testes passam um relogio fixo).
    Levanta ErroConfig se o bloco `painel` da Config for inseguro (veja `conferir_painel`).
    """
    conferir_painel(cfg)
    transmissor = transmissor if transmissor is not None else Transmissor()

    # Sem /docs, /redoc e /openapi.json: a documentacao automatica ficaria aberta mesmo com senha.
    app = FastAPI(title="Scanner de arbitragem", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.motor, app.state.banco, app.state.transmissor = motor, banco, transmissor
    # Middlewares: o ultimo acrescentado e o primeiro a ver o pedido. Ordem: Host, outro site, senha.
    if cfg.painel.get("usuario") is not None:
        app.add_middleware(ExigirSenha, usuario=cfg.painel["usuario"], senha=cfg.painel["senha"])
    app.add_middleware(RecusarOutroSite)
    if cfg.painel.get("usuario") is None:
        # Sem senha o host e da propria maquina (conferir_painel garante): so os nomes dela valem no Host,
        # inclusive o proprio painel.host (ex.: 127.0.0.2).
        nomes = list(dict.fromkeys(NOMES_DA_MAQUINA + [nome_no_cabecalho_host(cfg.painel["host"])]))
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=nomes)

    # --- Sinais e historico --------------------------------------------------

    @app.get("/api/sinais")
    async def sinais() -> list[dict]:
        # Le o gerenciador, que so guarda o que o ultimo ciclo decidiu. NAO chama Motor.candidatos:
        # ele tem efeito colateral (acrescenta amostras ao periodo).
        return motor.sinais_ativos()

    @app.get("/api/historico/{chave:path}")
    def historico(
        chave: str,
        intencao: Literal["abertura", "fechamento"] = "abertura",
        periodo_h: float | None = Query(default=None, gt=0, allow_inf_nan=False),
    ) -> dict:
        """Amostras do par `chave` ("BTC/USDT|spot_futuro|gate|spot|bitget|futuro", codificada na URL).

        `ts` em segundos desde 1970; `spread` e o A (abertura) ou o F (fechamento) conforme a `intencao`.
        `periodo_h` padrao: o periodo das metricas em uso.
        """
        periodo = periodo_h if periodo_h is not None else _periodo_padrao_h(motor, cfg)
        campo = "a" if intencao == "abertura" else "f"
        linhas = banco.amostras(chave, relogio() - periodo * SEGUNDOS_POR_HORA)
        amostras = [
            {"ts": linha["ts"], "spread": linha[campo], "preco_long": linha["preco_long"],
             "preco_short": linha["preco_short"], "lote_long": linha["lote_long"], "lote_short": linha["lote_short"]}
            for linha in linhas
        ]
        resumo = _resumo(
            [linha[campo] for linha in linhas if linha[campo] is not None],
            [linha["lote_long"] for linha in linhas if linha["lote_long"] is not None],
            [linha["lote_short"] for linha in linhas if linha["lote_short"] is not None],
        )
        return {"amostras": amostras, "resumo": resumo}

    # --- Grupos de filtros ---------------------------------------------------

    @app.get("/api/filtros")
    def listar_filtros() -> dict[str, dict]:
        return banco.listar_grupos()

    @app.post("/api/filtros/{nome}")
    def salvar_filtros(nome: str, corpo: Any = Body()) -> dict:
        if not nome.strip():
            raise HTTPException(400, "o nome do grupo nao pode ser vazio")
        try:
            filtros = _filtros_validos(corpo, cfg.retencao_dias * 24).para_dict()
        except ValueError as erro:
            raise HTTPException(400, str(erro)) from None
        banco.salvar_grupo(nome, filtros)
        return filtros

    @app.delete("/api/filtros/{nome}")
    def apagar_filtros(nome: str) -> dict:
        if nome not in banco.listar_grupos():
            raise HTTPException(404, f"nao existe o grupo de filtros '{nome}'")
        banco.apagar_grupo(nome)
        return {"apagado": nome}

    # --- Sessao --------------------------------------------------------------

    @app.get("/api/sessao")
    async def ver_sessao() -> dict:
        return _sessao_json(motor.sessao)

    @app.put("/api/sessao")
    async def trocar_sessao(corpo: CorpoSessao) -> dict:
        if corpo.modo not in MODOS:
            raise HTTPException(400, f"modo precisa ser {' ou '.join(repr(m) for m in MODOS)} (recebi {corpo.modo!r})")
        try:
            filtros = _filtros_validos(corpo.filtros, cfg.retencao_dias * 24)
        except ValueError as erro:
            raise HTTPException(400, str(erro)) from None
        # Um objeto novo de uma vez so (nunca campo a campo): o motor le a sessao sem nunca ver metade da troca.
        motor.sessao = Sessao(modo=corpo.modo, filtros=filtros, comparacao=corpo.comparacao)
        motor.salvar_sessao()  # guarda no banco (pela fila do escritor): o scanner religado comeca nesta sessao
        return _sessao_json(motor.sessao)

    # --- Status das corretoras -------------------------------------------------

    @app.get("/api/status")
    async def status() -> dict[str, dict]:
        """Por corretora: estado (conectada | reconectando | atrasada), atraso medio em ms, reconexoes e mercados.

        Os dados vem do estado dos mercados (Motor.status -> EstadoMercados.saude). atraso_medio_ms e
        null quando a corretora nao informa o horario dos dados. `mercados`: os ligados (veja
        mercados_ligados), ex.: ["spot"] na kraken; o painel monta as opcoes de "Corretoras 1/2" com eles.
        """
        return {
            corretora: {
                "estado": dados["estado"],
                "atraso_medio_ms": dados.get("atraso_medio_ms"),
                "reconexoes": dados.get("reconexoes", 0),
                "mercados": list(mercados_ligados(cfg, corretora)),
            }
            for corretora, dados in motor.status(relogio()).items()
        }

    # --- Metricas de validacao (especificacao, secao 9) ---------------------------

    @app.get("/api/validacao")
    def validacao(dia: str | None = Query(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")) -> dict:
        """Metricas de validacao do dia UTC `dia` ("AAAA-MM-DD"; padrao: hoje). Formato em scanner/validacao.

        Data em outro formato, ou que nao existe (ex.: 2026-02-30), da 422. Para hoje, o atraso e as reconexoes
        gravados sao completados com o status atual das corretoras.
        """
        agora = relogio()
        hoje = dia_utc(agora)
        dia = hoje if dia is None else dia
        try:
            date.fromisoformat(dia)
        except ValueError:
            raise HTTPException(422, f"dia invalido: {dia!r} (use AAAA-MM-DD, ex.: 2026-10-20)") from None
        status_atual = None
        if dia == hoje:
            # Esta rota roda numa thread (a consulta ao banco nao para o motor), mas o estado do motor so pode
            # ser lido no laco do asyncio, o mesmo do motor: from_thread.run_sync chama motor.status la.
            status_atual = anyio.from_thread.run_sync(motor.status, agora)
        return resumo_do_dia(banco, dia, status_atual)

    # --- WebSocket -----------------------------------------------------------

    @app.websocket("/ws/sinais")
    async def ws_sinais(ws: WebSocket) -> None:
        await ws.accept()
        # Sem nenhum `await` entre inscrever e tirar o retrato: todo evento publicado depois do retrato
        # chega pela fila, e nenhum evento aparece duas vezes.
        fila = transmissor.inscrever()
        retrato = json.dumps({"tipo": "snapshot", "sinais": motor.sinais_ativos()})
        try:
            # Enviar e escutar ao mesmo tempo: escutar serve so para perceber quando o painel fecha a aba
            # (sem isso um painel parado ficaria na lista ate o proximo evento).
            await _ate_um_terminar(lambda: _enviar(ws, fila, retrato), lambda: _esperar_desconexao(ws))
        finally:
            transmissor.remover(fila)

    # --- Painel web (arquivos estaticos) -------------------------------------
    # Montado POR ULTIMO: o "/" pega qualquer endereco, entao as rotas /api e /ws precisam vir antes
    # (o FastAPI confere as rotas na ordem em que foram criadas). html=True: "/" entrega o index.html.
    # Endereco que nao existe da 404; WebSocket em endereco desconhecido e fechado.
    app.mount("/", ArquivosDoPainel(directory=PASTA_PAINEL, html=True), name="painel")

    return app


async def _ate_um_terminar(*funcoes: Callable[[], Any]) -> None:
    """Roda as funcoes (async) ao mesmo tempo e, quando a primeira termina, cancela as outras."""
    async with anyio.create_task_group() as grupo:

        async def executar(funcao: Callable[[], Any]) -> None:
            try:
                await funcao()
            finally:
                grupo.cancel_scope.cancel()

        for funcao in funcoes:
            grupo.start_soon(executar, funcao)


async def _enviar(ws: WebSocket, fila: asyncio.Queue, retrato: str) -> None:
    """Manda o retrato e depois cada mensagem da fila (eventos e o vivo, que o Transmissor poe la).

    `None` na fila = o Transmissor derrubou este painel. A rota nao manda nada sozinha: se o motor parar,
    o painel para de receber, e o horario da "Ultima atualizacao" para.
    """
    try:
        await ws.send_text(retrato)
        while True:
            mensagem = await fila.get()
            if mensagem is None:
                await ws.close(code=CODIGO_PAINEL_LENTO)
                return
            await ws.send_text(mensagem)
    except (WebSocketDisconnect, OSError, RuntimeError) as erro:  # o painel fechou a aba no meio do envio
        registro.debug("conexao do painel terminou durante o envio: %r", erro)


async def _esperar_desconexao(ws: WebSocket) -> None:
    """Le (e ignora) o que o painel mandar, so para perceber quando ele desconecta."""
    while True:
        mensagem = await ws.receive()
        if mensagem["type"] == "websocket.disconnect":
            return
