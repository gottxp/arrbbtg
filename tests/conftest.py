"""Configuracao compartilhada dos testes."""

import asyncio

import pytest

import scanner.__main__ as principal
from scanner.config.carregar import Config
from scanner.filtros import limitar_periodo_metricas


@pytest.fixture(autouse=True)
def sem_painel_de_verdade(monkeypatch):
    """Nenhum teste do `rodar` abre uma porta de verdade: o servidor do painel (uvicorn) e trocado por um falso.

    O falso so espera ser cancelado, como o de verdade. Os testes que querem ver o que o `rodar` entrega ao
    painel trocam `servir_api` de novo (o monkeypatch do teste vem depois deste).
    """

    async def servidor_falso(app, cfg):
        await asyncio.sleep(3600)

    monkeypatch.setattr(principal, "servir_api", servidor_falso)


@pytest.fixture(autouse=True)
def periodo_maximo_padrao():
    """O motor ajusta, ao ser criado, o maior periodo das metricas que os filtros aceitam (retencao_dias x 24 da
    Config dele). Depois de cada teste volta o padrao, para um teste com outra retencao nao mudar os seguintes."""
    yield
    limitar_periodo_metricas(Config.retencao_dias * 24)
