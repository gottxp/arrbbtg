"""Monta a corretora pelo nome do config.yaml: a Lighter pelo conector proprio, as outras pelo ccxt (Task 16).

Usado pelo __main__ e pelo imprimir_precos. Nao acessa a rede: so cria o objeto.
"""

from scanner.coletores.ccxt_coletor import criar_exchange_ccxt
from scanner.coletores.lighter import criar_cliente_lighter, e_lighter
from scanner.config.carregar import Config


def criar_corretora(nome: str, cfg: Config):
    """O "exchange" da corretora: ClienteLighter para lighter/lighter_rh, o objeto do ccxt pro para as outras.

    Nome que nao existe no ccxt (nem e da Lighter): ValueError com a mensagem do criar_exchange_ccxt.
    """
    if e_lighter(nome):
        return criar_cliente_lighter(nome, cfg)
    return criar_exchange_ccxt(nome)
