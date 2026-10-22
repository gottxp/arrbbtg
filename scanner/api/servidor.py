"""Servidor da API: o uvicorn rodando no mesmo laco do asyncio do scanner.

O `__main__` cria uma tarefa com `servir_api(app, cfg)`. Cancelar a tarefa (Ctrl+C) faz o uvicorn
fechar as conexoes abertas e soltar a porta antes de o programa terminar.
"""

import asyncio
import contextlib
import importlib.util
import logging

import uvicorn

from scanner.config.carregar import Config

registro = logging.getLogger("scanner.api")

# Quanto tempo esperar o uvicorn fechar as conexoes (paineis abertos) ao encerrar, em segundos.
TEMPO_MAX_ENCERRAR_S = 5


class _ServidorSemSinais(uvicorn.Server):
    """Uvicorn que NAO mexe no Ctrl+C: quem trata o encerramento e o asyncio.run do __main__.

    Sem isto o uvicorn trocaria o tratamento do Ctrl+C por um proprio, e o scanner inteiro nao
    pararia junto com o painel.
    """

    def install_signal_handlers(self) -> None:  # uvicorn antigo (< 0.30)
        pass

    @contextlib.contextmanager
    def capture_signals(self):  # uvicorn novo
        yield


def avisar_se_faltar_websocket() -> None:
    """Sem a biblioteca `websockets` (ou `wsproto`) o uvicorn recusa o WebSocket e o painel ao vivo nao funciona."""
    if not any(importlib.util.find_spec(nome) for nome in ("websockets", "wsproto")):
        registro.warning("o pacote 'websockets' nao esta instalado: o painel ao vivo (/ws/sinais) nao vai funcionar. "
                         "Instale com: pip install websockets")


async def servir_api(app, cfg: Config) -> None:
    """Serve o `app` em painel.host:painel.porta ate ser cancelada. Se nao conseguir abrir a porta, levanta RuntimeError."""
    avisar_se_faltar_websocket()
    host, porta = cfg.painel["host"], cfg.painel["porta"]
    config = uvicorn.Config(
        app,
        host=host,
        port=porta,
        log_config=None,  # o formato do log e o do scanner (configurar_log), nao o do uvicorn
        log_level="info" if cfg.nivel_log == "detalhado" else "warning",
        access_log=False,
        timeout_graceful_shutdown=TEMPO_MAX_ENCERRAR_S,
    )
    servidor = _ServidorSemSinais(config)
    registro.info("painel em http://%s:%d", host, porta)
    tarefa = asyncio.create_task(_servir(servidor, host, porta), name="uvicorn")
    try:
        await asyncio.shield(tarefa)  # shield: cancelar esta funcao nao cancela o uvicorn pela metade
    except asyncio.CancelledError:
        servidor.should_exit = True  # pede ao uvicorn para encerrar com calma (fecha as conexoes e solta a porta)
        try:
            await asyncio.wait_for(tarefa, TEMPO_MAX_ENCERRAR_S + 1)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            registro.warning("o painel demorou para fechar; encerrando a forca")
        raise


async def _servir(servidor: uvicorn.Server, host: str, porta: int) -> None:
    try:
        await servidor.serve()
    except SystemExit:  # e assim que o uvicorn desiste quando nao consegue abrir a porta
        raise RuntimeError(f"nao consegui abrir o painel em {host}:{porta} (a porta ja esta em uso?)") from None
