"""Molde de todo coletor de corretora."""

from abc import ABC, abstractmethod
from collections.abc import Callable

from scanner.modelos import Livro


class Coletor(ABC):
    """Fica ligado a uma corretora e avisa a cada livro novo que chega."""

    def __init__(self, nome: str):
        self.nome = nome  # nome da corretora, ex.: "bitget"

    @abstractmethod
    async def iniciar(self, simbolos: list[str], ao_atualizar: Callable[[Livro], None]) -> None:
        """Acompanha o livro de cada simbolo ate ser cancelado (ou dar erro).

        `ao_atualizar` e chamada com cada Livro novo e valido.
        """
