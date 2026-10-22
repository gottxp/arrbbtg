"""Molde de todo coletor de corretora e a parte comum a todos.

- `Coletor`: o molde minimo (o imprimir_precos usa so o `iniciar`).
- `ColetorDoScanner`: a interface que o __main__ usa de um coletor (ColetorCcxt e ColetorLighter).
- `ColetorComSaude`: o codigo comum de saude, reconexao, limite de assinaturas e descartes (especificacao,
  secao 8), para os dois conectores seguirem exatamente a mesma regra: queda apaga a corretora do estado e
  mostra "reconectando", a primeira mensagem boa conta a reconexao, traceback so uma vez, o limite fica com
  os livros de maior volume 24h (com histerese a favor dos ja abertos).
"""

import logging
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from scanner.coletores.reconexao import MARGEM_PARA_TROCAR_LIVRO, parece_limite_de_assinatura, selecionar_pares
from scanner.estado.mercados import EstadoMercados
from scanner.modelos import Livro, Ticker

registro = logging.getLogger("scanner.coletores")

# Tipos de descarte contados por corretora (veja `tirar_descartes`).
TIPOS_DE_DESCARTE = ("livros", "tickers", "sequencia_quebrada")

# Dica que vai no log quando o erro da corretora parece ser o limite de assinaturas por conexao.
DICA_LIMITE = (" A mensagem parece de limite de assinaturas da corretora: confira limites_assinatura "
               "no config.yaml.")


class Coletor(ABC):
    """Fica ligado a uma corretora e avisa a cada livro novo que chega."""

    def __init__(self, nome: str):
        self.nome = nome  # nome da corretora, ex.: "bitget"

    @abstractmethod
    async def iniciar(self, simbolos: list[str], ao_atualizar: Callable[[Livro], None]) -> None:
        """Acompanha o livro de cada simbolo ate ser cancelado (ou dar erro).

        `ao_atualizar` e chamada com cada Livro novo e valido.
        """


@runtime_checkable
class ColetorDoScanner(Protocol):
    """O que o __main__ usa de um coletor (ColetorCcxt e ColetorLighter oferecem tudo isto)."""

    nome: str

    async def manter_tickers(self, simbolos: list[str], ao_ticker: Callable[[Ticker], None] | None = None) -> None:
        """Etapa 1 com reconexao, ate ser cancelado."""

    async def assinar_livros(self, simbolos: set[str], ao_atualizar: Callable[[Livro], None] | None = None) -> None:
        """Etapa 2: deixa o livro aberto exatamente para `simbolos`."""

    def simbolos_por_chave(self) -> dict[tuple[str, str], str]:
        """{(mercado, ativo): simbolo} dos simbolos que vao ao estado."""

    def cotacoes_dos_simbolos(self, simbolos) -> set[str]:
        """As moedas de cotacao dos simbolos (ex.: {"USDT", "USDC"})."""

    def markets(self) -> dict:
        """Os markets carregados ({simbolo: market})."""

    def usar_para_cotacao(self, simbolos) -> None:
        """Os simbolos cujo ticker tambem alimenta as cotacoes."""

    def tirar_descartes(self) -> dict[str, int]:
        """Os descartes desde a ultima vez (e zera a conta)."""

    async def fechar(self) -> None:
        """Para tudo e libera as conexoes."""


class ColetorComSaude(Coletor):
    """Parte comum dos coletores do scanner: saude da corretora, quedas, limite de assinaturas e descartes.

    estado: EstadoMercados opcional (sem ele, nada vai ao estado). relogio: hora atual em segundos.
    limite_assinaturas: maximo de livros abertos (limites_assinatura da Config); None = sem limite.
    espera_max_reconexao_s: maior espera entre duas tentativas de reconexao (da Config).

    Quem herda informa:
      - `_TEXTO_DA_CONEXAO`: o que caiu, no log ("a conexao dos tickers", "a conexao da lighter");
      - `_ERROS_DE_CONEXAO`: os erros que sao queda de rede (sem traceback no log);
      - `_volume_conhecido(simbolo)` e `_livros_abertos()`: para o limite de livros.
    O `_livros_no_estado` guarda {simbolo: (mercado, ativo)} do livro que cada simbolo entregou ao estado.
    """

    _TEXTO_DA_CONEXAO = "a conexao dos tickers"
    _ERROS_DE_CONEXAO: tuple[type[BaseException], ...] = (OSError,)

    def __init__(self, nome: str, estado: EstadoMercados | None, relogio: Callable[[], float],
                 limite_assinaturas: int | None, espera_max_reconexao_s: float):
        super().__init__(nome)
        self._estado = estado
        self._relogio = relogio
        self._limite = limite_assinaturas
        self._espera_max_s = espera_max_reconexao_s
        self._ticker_desde_o_inicio = False  # chegou mensagem de ticker desde o ultimo (re)inicio da conexao
        self._caiu_em: float | None = None  # quando a conexao caiu (None = funcionando)
        self._queda_com_traceback = False  # um defeito (nao de conexao) ja saiu com traceback na queda
        self._traceback_ticker: set[str] = set()  # simbolos cujo erro ao tratar o ticker ja saiu com traceback
        self._descartes: Counter = Counter()  # tipo de descarte -> quantos desde o ultimo tirar_descartes
        self._fora_do_limite = {"livros": 0, "tickers": 0}  # quantos pares ficaram de fora (ultimo aviso)
        self._livros_no_estado: dict[str, tuple[str, str]] = {}  # simbolo -> (mercado, ativo) do livro guardado

    # --- estado e saude ------------------------------------------------------

    def _registrar_mensagem(self, agora: float) -> None:
        if self._estado is not None:
            self._estado.registrar_mensagem(self.nome, agora)

    def _registrar_atraso(self, agora: float, horario_da_corretora: float) -> None:
        """Manda ao estado o atraso do dado: quando chegou menos o horario que a corretora informou (s)."""
        if self._estado is not None:
            self._estado.registrar_atraso(self.nome, agora - horario_da_corretora, agora)

    def _chegou_ticker(self, agora: float) -> None:
        """Mensagem boa de tickers: se a corretora estava fora, conta a reconexao.

        O estado e avisado a cada mensagem (so conta se ele marcava a corretora como "reconectando"):
        assim volta a "conectada" tambem a corretora que nao tinha carregado os mercados ao ligar.
        """
        self._ticker_desde_o_inicio = True
        if self._estado is not None:
            self._estado.marcar_reconectada(self.nome)
        if self._caiu_em is not None:
            fora_s = agora - self._caiu_em
            self._caiu_em = None
            registro.info("%s: tickers de volta depois de %.1fs fora", self.nome, fora_s)

    def _queda_da_conexao(self, erro: Exception, espera: float, tentativa: int) -> None:
        """A conexao caiu: o estado esquece a corretora e o log registra a queda e a espera."""
        if self._caiu_em is None:
            self._caiu_em = self._relogio()
        if self._estado is not None:
            self._estado.marcar_desconectada(self.nome)
        self._livros_no_estado.clear()  # o estado ja apagou os livros da corretora
        dica = DICA_LIMITE if parece_limite_de_assinatura(erro) else ""
        # Erro que nao e de conexao pode ser defeito do proprio scanner: traceback na primeira vez (por corretora).
        com_traceback = not isinstance(erro, self._ERROS_DE_CONEXAO) and not self._queda_com_traceback
        if com_traceback:
            self._queda_com_traceback = True
        registro.warning("%s: %s caiu (%s: %s); tentando de novo em %gs (tentativa %d).%s",
                         self.nome, self._TEXTO_DA_CONEXAO, type(erro).__name__, erro, espera, tentativa, dica,
                         exc_info=erro if com_traceback else False)

    def _erro_ao_tratar_ticker(self, simbolo: str) -> None:
        """Log do erro ao tratar um ticker: com traceback na primeira vez do simbolo; depois so no log detalhado."""
        if simbolo in self._traceback_ticker:
            registro.debug("%s %s: erro ao tratar o ticker de novo; descartado", self.nome, simbolo)
            return
        self._traceback_ticker.add(simbolo)
        registro.warning("%s %s: erro ao tratar o ticker; so ele foi descartado e o resto segue "
                         "(os proximos erros deste par so aparecem no log detalhado)", self.nome, simbolo,
                         exc_info=True)

    def _remover_livro_do_estado(self, simbolo: str) -> None:
        """Tira do estado o livro que este simbolo entregou (se entregou algum)."""
        chave = self._livros_no_estado.pop(simbolo, None)
        if chave is not None and self._estado is not None:
            mercado, ativo = chave
            # Com o simbolo: se outro simbolo do mesmo ativo ja ocupa a chave, o livro dele fica.
            self._estado.remover_livro(self.nome, mercado, ativo, simbolo_original=simbolo)

    def tirar_descartes(self) -> dict[str, int]:
        """Quantos livros e tickers foram descartados (e quantas sequencias de livro quebraram) desde a ultima vez.

        Devolve {"livros": n, "tickers": n, "sequencia_quebrada": n} e zera os contadores (o __main__ chama a
        cada intervalo_log_recursos_s para o resumo do log).
        """
        resumo = {tipo: self._descartes[tipo] for tipo in TIPOS_DE_DESCARTE}
        self._descartes.clear()
        return resumo

    # --- limite de assinaturas -----------------------------------------------

    def _volume_conhecido(self, simbolo: str) -> float | None:
        """Volume 24h do simbolo, para o limite de assinaturas (quem herda informa); None = desconhecido."""
        return None

    def _livros_abertos(self) -> set[str]:
        """Os simbolos com livro aberto agora (tem preferencia no limite, para nao trocar a cada ciclo)."""
        return set()

    def _livros_dentro_do_limite(self, desejados: set[str]) -> set[str]:
        """Os livros que cabem no limite de assinaturas: os de maior volume 24h, com preferencia aos ja abertos."""
        if self._limite is None or len(desejados) <= self._limite:
            self._avisar_limite("livros", 0, len(desejados))
            return desejados
        volumes = {}
        for simbolo in desejados:
            volume = self._volume_conhecido(simbolo)
            if volume is not None:
                volumes[simbolo] = volume
        # Os ja abertos tem preferencia (empate e diferenca pequena): o livro nao fica trocando a cada ciclo.
        escolhidos, fora = selecionar_pares(sorted(desejados), volumes, self._limite,
                                            abertos=self._livros_abertos(), margem=MARGEM_PARA_TROCAR_LIVRO)
        self._avisar_limite("livros", fora, len(desejados))
        return set(escolhidos)

    def _avisar_limite(self, etapa: str, fora: int, total: int) -> None:
        """Registra quantos pares ficaram de fora do limite, so quando esse numero muda (nao a cada ciclo)."""
        if fora == self._fora_do_limite[etapa]:
            return
        self._fora_do_limite[etapa] = fora
        nome_da_etapa = "livros" if etapa == "livros" else "tickers (um por par)"
        if fora:
            registro.warning("%s: limite de %d assinaturas de %s (limites_assinatura): %d de %d pares ficaram de "
                             "fora (os de menor volume 24h)", self.nome, self._limite, nome_da_etapa, fora, total)
        else:
            registro.info("%s: todos os pares de %s cabem de novo no limite de assinaturas", self.nome, nome_da_etapa)
