"""Transmissor: entrega cada evento do motor a todos os paineis conectados pelo WebSocket.

O laco do motor chama `publicar(eventos)` depois de cada ciclo. Cada painel conectado tem a sua
propria fila de mensagens (`asyncio.Queue` com tamanho maximo); a rota do WebSocket le a fila e
manda as mensagens ao navegador.

Regra de ouro: o motor nunca espera por um painel. Por isso:
- `publicar` e uma funcao comum (nao `async`) que so poe mensagens nas filas, sem esperar nada;
- se a fila de um painel enche (ele esta lento ou a conexao travou), o painel e derrubado: a fila
  dele e esvaziada e recebe `None` (o aviso de "pode fechar"). O painel reconecta sozinho e recebe
  um retrato novo dos sinais (snapshot), entao nada se perde.

`publicar` precisa rodar na mesma thread do laco do asyncio (o __main__ e a API ja rodam juntos).
"""

import asyncio
import json
from collections.abc import Iterable

from scanner.sinais import Evento

# Quantas mensagens cabem na fila de cada painel. Um ciclo do motor pode gerar uma mensagem por
# sinal ativo (centenas); 2000 da alguns segundos de folga a um navegador que engasgou.
TAMANHO_FILA_PADRAO = 2000


class Transmissor:
    """Guarda a fila de cada painel conectado e distribui os eventos para elas."""

    def __init__(self, tamanho_fila: int = TAMANHO_FILA_PADRAO):
        if tamanho_fila < 1:
            # asyncio.Queue(maxsize=0) nao tem limite: um painel parado encheria a memoria sem ser derrubado.
            raise ValueError(f"tamanho_fila precisa ser pelo menos 1 (recebi {tamanho_fila})")
        self.tamanho_fila = tamanho_fila
        self._filas: set[asyncio.Queue] = set()

    @property
    def clientes(self) -> int:
        """Quantos paineis estao conectados agora."""
        return len(self._filas)

    def inscrever(self) -> asyncio.Queue:
        """Registra um painel novo e devolve a fila dele. As mensagens sao textos JSON; `None` = fechar."""
        fila: asyncio.Queue = asyncio.Queue(maxsize=self.tamanho_fila)
        self._filas.add(fila)
        return fila

    def remover(self, fila: asyncio.Queue) -> None:
        """Tira o painel da lista (chamar de novo ou com fila desconhecida nao da erro)."""
        self._filas.discard(fila)

    def publicar(self, eventos: Iterable[Evento]) -> None:
        """Poe cada evento, como JSON {"tipo", "sinal"}, na fila de todos os paineis conectados."""
        if not self._filas:
            return
        mensagens = [json.dumps({"tipo": e.tipo, "sinal": e.sinal}) for e in eventos]  # uma vez so, para todos
        if not mensagens:
            return
        for fila in list(self._filas):  # copia: derrubar um painel mexe no conjunto
            try:
                for mensagem in mensagens:
                    fila.put_nowait(mensagem)
            except asyncio.QueueFull:
                self._derrubar(fila)

    def _derrubar(self, fila: asyncio.Queue) -> None:
        """Tira o painel lento da lista e deixa na fila dele so o aviso de fechar (None)."""
        self._filas.discard(fila)
        while not fila.empty():  # o que estava na fila nao importa mais: o painel recomeca do snapshot
            fila.get_nowait()
        fila.put_nowait(None)
