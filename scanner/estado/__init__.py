"""Estado em memoria: o ultimo ticker e o ultimo livro de cada mercado, com o horario de chegada."""

from scanner.estado.mercados import EstadoMercados, pares_para_livro

__all__ = ["EstadoMercados", "pares_para_livro"]
