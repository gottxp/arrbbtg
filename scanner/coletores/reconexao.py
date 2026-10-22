"""Regras de reconexao e de limite de assinaturas, usadas pelos coletores (especificacao, secao 8).

- Queda de conexao: tenta de novo esperando cada vez mais (1s, 2s, 4s... ate o maximo da config).
- Corretora que limita as assinaturas: ficam os pares de maior volume 24h e o resto fica de fora.

Funcoes puras, sem rede: o coletor decide quando chama-las.
"""

import math
import re

from scanner.config.carregar import Config


def espera_reconexao(tentativa: int, maximo_s: float = Config.espera_max_reconexao_s) -> float:
    """Segundos de espera antes da tentativa numero `tentativa` (0 = primeira): 2^tentativa, no maximo `maximo_s`.

    Ex.: 0 -> 1s, 1 -> 2s, 2 -> 4s, ... 6 -> 60s (com o maximo padrao de 60s).
    maximo_s: espera_max_reconexao_s da Config.
    """
    tentativa = max(0, tentativa)
    if tentativa >= 64:  # 2^64 segundos e muito mais que qualquer maximo: evita conta com numero gigante
        return float(maximo_s)
    return float(min(2 ** tentativa, maximo_s))


def _volume(volumes: dict, par: str) -> float:
    """Volume 24h do par para ordenar; sem volume (ou volume invalido) conta como o menor de todos."""
    valor = volumes.get(par)
    if isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor):
        return float(valor)
    return -math.inf


def selecionar_pares(pares: list[str], volumes: dict[str, float], limite: int) -> tuple[list[str], int]:
    """Os `limite` pares de maior volume 24h, do maior para o menor, e quantos ficaram de fora.

    Par sem volume conhecido vai para o fim. Empate de volume: ordem alfabetica do nome (assim a
    escolha nao muda de um ciclo para o outro). Par repetido na lista conta uma vez so.
    Ex.: selecionar_pares(["A", "B", "C"], {"A": 10, "B": 300, "C": 50}, limite=2) -> (["B", "C"], 1)
    """
    unicos = list(dict.fromkeys(pares))
    ordenados = sorted(unicos, key=lambda par: (-_volume(volumes, par), par))
    escolhidos = ordenados[:max(0, limite)]
    return escolhidos, len(unicos) - len(escolhidos)


# Mensagens de erro das corretoras quando passam do limite de assinaturas por conexao. Cada uma escreve
# de um jeito ("subscription limit exceeded", "too many streams", "max topics"...), entao a regra procura
# uma palavra de assinatura (subscription, topic, stream, channel) perto de uma palavra de limite. "rate
# limit" sozinho (limite de REQUISICOES, outro problema) nao entra.
_PALAVRA_DE_ASSINATURA = r"\b(subscri\w*|topics?|streams?|channels?)\b"
_PALAVRA_DE_LIMITE = r"\b(limit\w*|exceed\w*|too many|max\w*)\b"
_PARECE_LIMITE = re.compile(
    rf"{_PALAVRA_DE_ASSINATURA}[^.;]{{0,40}}?{_PALAVRA_DE_LIMITE}|{_PALAVRA_DE_LIMITE}[^.;]{{0,40}}?{_PALAVRA_DE_ASSINATURA}",
    re.IGNORECASE,
)


def parece_limite_de_assinatura(erro: BaseException) -> bool:
    """True se a mensagem do erro parece a corretora recusando por excesso de assinaturas.

    E so um palpite pelo texto (cada corretora usa uma mensagem): serve para o log avisar o dono
    de que talvez precise preencher `limites_assinatura` no config.yaml.
    """
    return _PARECE_LIMITE.search(str(erro)) is not None
