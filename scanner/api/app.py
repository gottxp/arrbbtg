"""API do painel (especificacao, secao 6): FastAPI com as rotas /api e o WebSocket /ws/sinais.

`criar_app(motor, banco, cfg)` monta o app. Rotas (todas sob /api, menos o WebSocket):

- GET    /api/sinais                 sinais ativos agora (JSON da spec, secao 5)
- GET    /api/historico/{chave}      amostras de spread de um par + resumo (grafico do painel)
- GET    /api/filtros                grupos de filtros salvos: {nome: filtros}
- POST   /api/filtros/{nome}         salva (ou substitui) um grupo
- DELETE /api/filtros/{nome}         apaga um grupo
- GET    /api/sessao, PUT /api/sessao   modo, filtros e comparacao em uso pelo motor
- GET    /api/status                 saude de cada corretora
- WS     /ws/sinais                  um retrato dos sinais ao conectar e depois cada evento do motor

Senha: com `painel.usuario` no config.yaml, TUDO exige HTTP Basic (rotas /api, WebSocket e, na
Task 12, os arquivos do painel). A conferencia e um middleware, entao rotas e arquivos novos ja
nascem protegidos.

Threads: as rotas que leem ou mudam o motor sao `async def`, ou seja, rodam no laco do asyncio,
o mesmo do motor, e nunca no meio de um ciclo. As que so falam com o banco (que tem trava propria)
sao `def` comuns e rodam fora do laco, para uma consulta demorada nao parar o motor.
"""

import asyncio
import base64
import heapq
import json
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any, Literal

import anyio
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool
from starlette.types import ASGIApp, Receive, Scope, Send

from scanner.api.transmissor import Transmissor
from scanner.armazenamento import Banco
from scanner.config.carregar import Config, ErroConfig
from scanner.filtros import Filtros
from scanner.metricas.janela import MAIORES_N
from scanner.motor import MODOS, SEGUNDOS_POR_HORA, Motor, Sessao

registro = logging.getLogger("scanner.api")

# Fechamento do WebSocket: 1008 = violacao de politica (senha errada); 1013 = "tente de novo mais tarde"
# (painel lento demais, derrubado pelo Transmissor: ele reconecta e recebe o snapshot de novo).
CODIGO_SENHA_ERRADA = 1008
CODIGO_PAINEL_LENTO = 1013


# ---------------------------------------------------------------------------
# Senha (HTTP Basic)
# ---------------------------------------------------------------------------


def conferir_painel(cfg: Config) -> None:
    """Recusa (ErroConfig) um bloco `painel` que deixaria o painel aberto sem o dono querer.

    Usuario sem senha, senha sem usuario e usuario vazio ou com ":" (o HTTP Basic nao aceita) param o scanner
    com uma mensagem clara, em vez de subir sem protecao ou trancar o dono para fora.
    """
    usuario, senha = cfg.painel.get("usuario"), cfg.painel.get("senha")
    if usuario is None:
        if senha:
            raise ErroConfig("painel.usuario: a senha esta preenchida mas falta o usuario; preencha painel.usuario "
                             "no config.yaml (ou apague painel.senha para deixar o painel aberto)")
        return
    if not usuario or ":" in usuario:
        raise ErroConfig("painel.usuario: precisa ser um texto nao vazio e sem o caractere ':'")
    if not senha:
        raise ErroConfig("painel.senha: o painel tem usuario mas nao tem senha; preencha painel.senha no config.yaml "
                         "(ou apague painel.usuario para deixar o painel aberto)")


class ExigirSenha:
    """Middleware ASGI: so deixa passar quem mandou o usuario e a senha certos (HTTP Basic).

    Vale para pedidos HTTP (resposta 401 com o cabecalho que faz o navegador pedir a senha) e para o
    inicio de conexoes WebSocket (fechadas com o codigo 1008 antes de aceitar). O navegador reenvia a
    senha digitada sozinho nas conexoes WebSocket do mesmo endereco.
    """

    def __init__(self, app: ASGIApp, usuario: str, senha: str):
        self.app = app
        self._usuario = usuario.encode("utf-8")
        self._senha = senha.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or self._autorizado(scope):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": CODIGO_SENHA_ERRADA})
        else:
            resposta = JSONResponse({"detail": "usuario ou senha invalidos"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Basic realm="scanner"'})
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


# ---------------------------------------------------------------------------
# Apoio das rotas
# ---------------------------------------------------------------------------


class CorpoSessao(BaseModel):
    """O corpo do PUT /api/sessao. Campos que faltam ficam com o padrao da Sessao (lucro, sem filtros, sem comparacao)."""

    modo: str = "lucro"
    filtros: Any = Field(default_factory=dict)  # quem confere e o Filtros.de_dict (erro 400 com a mensagem)
    comparacao: StrictBool = False


def _filtros_validos(dados: Any) -> Filtros:
    """Filtros.de_dict + recusa de NaN e infinito (nao existem em JSON e estragariam as comparacoes). ValueError se invalido."""
    filtros = Filtros.de_dict(dados)
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
    if cfg.painel.get("usuario") is not None:
        app.add_middleware(ExigirSenha, usuario=cfg.painel["usuario"], senha=cfg.painel["senha"])

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
            filtros = _filtros_validos(corpo).para_dict()
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
            filtros = _filtros_validos(corpo.filtros)
        except ValueError as erro:
            raise HTTPException(400, str(erro)) from None
        # Um objeto novo de uma vez so (nunca campo a campo): o motor le a sessao sem nunca ver metade da troca.
        motor.sessao = Sessao(modo=corpo.modo, filtros=filtros, comparacao=corpo.comparacao)
        return _sessao_json(motor.sessao)

    # --- Status das corretoras -------------------------------------------------

    @app.get("/api/status")
    async def status() -> dict[str, dict]:
        """Por corretora: estado (conectada | reconectando | atrasada), atraso medio em ms e reconexoes.

        O motor ainda so informa o estado; atraso e reconexoes (Task 14) ficam None e 0 ate la.
        """
        return {
            corretora: {
                "estado": dados["estado"],
                "atraso_medio_ms": dados.get("atraso_medio_ms"),
                "reconexoes": dados.get("reconexoes", 0),
            }
            for corretora, dados in motor.status(relogio()).items()
        }

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
    """Manda o retrato e depois cada mensagem da fila. `None` na fila = o Transmissor derrubou este painel."""
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
