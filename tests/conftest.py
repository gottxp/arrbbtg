"""Configuracao compartilhada dos testes."""

import asyncio
import os
from dataclasses import replace

import pytest

import scanner.__main__ as principal
from scanner.motor import Motor


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
def historico_tickers_pela_variavel(monkeypatch):
    """Com SCANNER_HISTORICO_TICKERS=0, todo Motor dos testes nasce com historico_tickers desligado (Task 18).

    Serve para conferir que os testes de antes da Task 18 continuam passando com o comportamento de antes:
        SCANNER_HISTORICO_TICKERS=0 .venv/bin/python -W error -m pytest -q
    Os testes do historico pelos tickers (tests/test_historico_tickers.py) ficam de fora nesse caso. Sem a
    variavel (o normal), nada muda.
    """
    if os.environ.get("SCANNER_HISTORICO_TICKERS") != "0":
        return
    original = Motor.__init__

    def sem_historico_pelos_tickers(self, cfg, *args, **kwargs):
        original(self, replace(cfg, historico_tickers=False), *args, **kwargs)

    monkeypatch.setattr(Motor, "__init__", sem_historico_pelos_tickers)
