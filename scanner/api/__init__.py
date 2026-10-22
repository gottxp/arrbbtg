"""API do painel: rotas /api, WebSocket /ws/sinais, senha (HTTP Basic) e o servidor uvicorn."""

from scanner.api.app import conferir_painel, criar_app
from scanner.api.servidor import servir_api
from scanner.api.transmissor import Transmissor

__all__ = ["Transmissor", "conferir_painel", "criar_app", "servir_api"]
