"""Escritor do banco: grava no SQLite numa thread propria, fora do laco do asyncio.

O motor (e o resto do laco: sessao do painel, metricas das corretoras) so poe o que precisa gravar na fila
do escritor e segue. A thread "escritor do banco" tira o que estiver na fila, grava em lote (uma transacao
para as amostras, uma para os eventos) e, de tempos em tempos, faz o checkpoint do WAL. Assim o disco lento,
o checkpoint ou uma leitura pesada da API (que segura a trava do banco) nunca param o motor, os coletores
nem o painel.

Quando cada coisa e gravada:
- eventos dos sinais, a sessao e as metricas das corretoras: logo (sao poucos);
- amostras (e a ultima vez em que cada sinal ativo foi visto, `marcar_vistos`): esperam juntar ate
  INTERVALO_LOTE_AMOSTRAS_S. Cada lote suja uma pagina de 4 KB por combinacao (as
  amostras de cada par ficam juntas no disco), entao juntar 10 s em vez de 5 s escreve a metade no disco
  (medido com 4045 combinacoes com livro: 8,5 GB por hora no WAL com um lote a cada 5 s, 4,3 GB com um a cada
  10 s). O custo: o grafico do painel pode ficar ate esse tempo atras, e uma queda de energia perde no maximo
  isso de amostras.

Fila:
- amostras: no maximo `max_amostras_na_fila`. Se o disco nao der conta e a fila encher, as amostras que nao
  cabem sao descartadas, com um aviso no log (no maximo um por INTERVALO_AVISO_FILA_S, com quantas foram);
- eventos de sinal, a sessao e as metricas das corretoras NUNCA sao descartados.

Erro do banco:
- banco travado por outro programa (ex.: o scanner.db aberto no DB Browser), disco cheio ou outro erro do SQLite
  (sqlite3.OperationalError): os eventos, a sessao e as leituras das corretoras do lote VOLTAM para a frente da
  fila, na mesma ordem, e o escritor tenta de novo esperando 1 s, 2 s, 4 s... ate ESPERA_MAX_ERRO_S. Nada se
  perde nem se repete (a transacao que falhou volta atras inteira). As amostras do lote sao descartadas, com
  aviso no log (sao muitas e o grafico aguenta um buraco);
- qualquer outro erro (defeito): vai para o log e aquele grupo do lote fica de fora (tentar de novo nao adiantaria).
`parar` grava tudo o que ainda estiver na fila antes de terminar (o __main__ chama ao encerrar). Com o banco
travado ao parar, tenta TENTATIVAS_AO_PARAR vezes e desiste, avisando quantos eventos ficaram sem gravar (ao
religar, os registros que ficaram abertos sao fechados pelo Banco.fechar_abertos).
"""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable

registro = logging.getLogger("scanner.banco")

# Detalhes tecnicos (nao sao ajustes do dono):
# - quantas amostras podem esperar na fila (~200 bytes cada: 200 mil = ~40 MB; com 4 mil combinacoes com livro
#   sao uns 4 minutos de amostras);
# - quanto tempo as amostras esperam para juntar num lote (veja o topo do arquivo);
# - de quanto em quanto tempo o escritor faz o checkpoint do WAL (copia o WAL para o arquivo do banco);
# - de quanto em quanto tempo, no maximo, o log avisa que a fila encheu;
# - quanto o escritor espera depois de um erro do banco antes de tentar o proximo lote (nao enche o log).
MAX_AMOSTRAS_NA_FILA = 200_000
INTERVALO_LOTE_AMOSTRAS_S = 10
INTERVALO_CHECKPOINT_S = 30
INTERVALO_AVISO_FILA_S = 60
# - espera depois de um erro do SQLite: comeca em PAUSA_DEPOIS_DE_ERRO_S e dobra a cada erro seguido, ate
#   ESPERA_MAX_ERRO_S; ao parar, desiste depois de TENTATIVAS_AO_PARAR erros seguidos.
PAUSA_DEPOIS_DE_ERRO_S = 1.0
ESPERA_MAX_ERRO_S = 30.0
TENTATIVAS_AO_PARAR = 3


def espera_depois_de_erros(seguidos: int, base: float = PAUSA_DEPOIS_DE_ERRO_S) -> float:
    """Espera depois de `seguidos` erros seguidos do banco: base, 2 x base, 4 x base... ate ESPERA_MAX_ERRO_S."""
    return min(base * 2 ** max(0, seguidos - 1), ESPERA_MAX_ERRO_S)


def explicar_recusados(recusados: list) -> None:
    """Uma linha de log para cada evento que o banco recusou (Banco.gravar_eventos devolve a lista)."""
    for evento, erro in recusados:
        sinal = evento.sinal if isinstance(evento.sinal, dict) else {}
        registro.error("nao consegui gravar no banco o evento %s do sinal id=%s par=%s tipo=%s (%s: %s); "
                       "os outros eventos do lote foram gravados", evento.tipo, sinal.get("id"),
                       sinal.get("par"), sinal.get("tipo"), type(erro).__name__, erro)


class EscritorBanco:
    """Fila + thread escritora na frente de um `Banco` (veja o topo do arquivo).

    Tem os mesmos metodos de gravacao do Banco (gravar_eventos, gravar_amostras, salvar_sessao,
    registrar_corretoras), mas eles so poem na fila e voltam na hora.
    relogio: relogio do checkpoint e do aviso de fila cheia (os testes passam um falso); padrao: time.monotonic.
    intervalo_lote_amostras_s: quanto as amostras esperam para juntar num lote (padrao INTERVALO_LOTE_AMOSTRAS_S).
    espera_erro_s: a primeira espera depois de um erro do SQLite (padrao PAUSA_DEPOIS_DE_ERRO_S; dobra a cada erro).
    """

    def __init__(self, banco, max_amostras_na_fila: int = MAX_AMOSTRAS_NA_FILA,
                 intervalo_checkpoint_s: float = INTERVALO_CHECKPOINT_S,
                 relogio: Callable[[], float] = time.monotonic,
                 intervalo_lote_amostras_s: float = INTERVALO_LOTE_AMOSTRAS_S,
                 espera_erro_s: float = PAUSA_DEPOIS_DE_ERRO_S):
        self._banco = banco
        self._espera_erro_s = espera_erro_s
        self._erros_seguidos = 0
        self._parada = threading.Event()  # ligado por `parar`: acorda a espera depois de um erro
        self._max_amostras = max_amostras_na_fila
        self._intervalo_checkpoint_s = intervalo_checkpoint_s
        self._intervalo_lote_s = intervalo_lote_amostras_s
        self._relogio = relogio
        self._condicao = threading.Condition()  # protege tudo abaixo e acorda a thread
        # Duas filas: os itens que gravam logo, na ordem em que chegaram ((tipo, dados): "eventos", "sessao",
        # "corretoras"), e as amostras que esperam o lote.
        self._urgentes: list[tuple] = []
        self._amostras: list[tuple] = []
        self._vistos: dict[str, str] = {}  # id do sinal -> ultima vez visto (ISO); vai junto com as amostras
        self._primeira_amostra_em: float | None = None  # time.monotonic da amostra mais velha na fila
        self._pedidos_de_descarga = 0  # quantos descarregar() esperam agora (as amostras gravam ja)
        self._gravando = False  # a thread tirou um lote da fila e ainda esta gravando
        self._parando = False
        self._descartadas = 0  # amostras descartadas desde o ultimo aviso
        self._ultimo_aviso: float | None = None  # relogio do ultimo aviso de fila cheia
        self._ultimo_checkpoint = relogio()
        self._thread = threading.Thread(target=self._trabalhar, name="escritor do banco", daemon=True)

    # --- O que o laco chama (nunca espera o disco) ----------------------------

    def iniciar(self) -> None:
        """Comeca a thread escritora."""
        self._thread.start()

    def gravar_eventos(self, eventos: list) -> list:
        """Poe os eventos do ciclo na fila (nunca descarta). Devolve [] (os recusados vao ao log pela thread)."""
        para_gravar = [e for e in eventos if e.tipo != "atualizado"]  # "atualizado" nao grava nada
        if para_gravar:
            self._por(("eventos", para_gravar))
        return []

    def gravar_amostras(self, amostras: list[tuple]) -> None:
        """Poe as amostras na fila; as que nao cabem (fila cheia) sao descartadas com aviso no log."""
        if not amostras:
            return
        with self._condicao:
            cabem = max(0, self._max_amostras - len(self._amostras))
            if cabem:
                self._amostras.extend(amostras[:cabem])
                if self._primeira_amostra_em is None:
                    self._primeira_amostra_em = time.monotonic()
                    self._condicao.notify()  # a thread passa a contar o tempo do lote
            descartadas = max(0, len(amostras) - cabem)
        if descartadas:
            self._avisar_descarte(descartadas)

    def marcar_vistos(self, vistos: list[tuple[str, str]]) -> None:
        """Poe na fila a ultima vez em que cada sinal ativo foi visto (Banco.marcar_vistos); vai com as amostras.

        Se o mesmo sinal vier de novo antes de gravar, vale a hora mais nova.
        """
        if not vistos:
            return
        with self._condicao:
            for id_, hora in vistos:
                if hora > self._vistos.get(id_, ""):
                    self._vistos[id_] = hora
            if self._primeira_amostra_em is None:
                self._primeira_amostra_em = time.monotonic()
                self._condicao.notify()

    def salvar_sessao(self, sessao: dict) -> None:
        """Poe a sessao na fila (nunca descarta; se houver varias, vale a ultima)."""
        self._por(("sessao", sessao))

    def registrar_corretoras(self, dia: str, status: dict) -> None:
        """Poe uma leitura das corretoras na fila (Banco.registrar_corretoras; nunca descarta)."""
        self._por(("corretoras", (dia, status)))

    def pendentes(self) -> int:
        """Quantos itens esperam na fila agora: amostras + eventos/sessao/leituras (vai para a linha de recursos)."""
        with self._condicao:
            return len(self._amostras) + len(self._urgentes) + len(self._vistos)

    def descarregar(self, timeout: float | None = None) -> bool:
        """Grava ja tudo o que esta na fila (inclusive as amostras do lote) e espera. False se passou do `timeout`.

        Para quem precisa ler logo depois de gravar (o resumo do dia, a recarga das janelas, os testes). Nunca
        chame no laco do asyncio (use asyncio.to_thread): esta funcao espera o disco.
        """
        limite = None if timeout is None else time.monotonic() + timeout
        with self._condicao:
            self._pedidos_de_descarga += 1
            self._condicao.notify_all()
            try:
                while self._amostras or self._vistos or self._urgentes or self._gravando:
                    if not self._thread.is_alive():
                        return False  # sem a thread ninguem esvazia a fila
                    restante = None if limite is None else limite - time.monotonic()
                    if restante is not None and restante <= 0:
                        return False
                    self._condicao.wait(restante if restante is not None else 0.5)
            finally:
                self._pedidos_de_descarga -= 1
        return True

    def parar(self, timeout: float | None = 30) -> None:
        """Grava o que falta na fila e termina a thread (espera ate `timeout` segundos)."""
        with self._condicao:
            self._parando = True
            self._condicao.notify_all()
        self._parada.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
        if self._thread.is_alive():
            registro.warning("o escritor do banco nao terminou em %ss; %d itens podem ter ficado sem gravar",
                             timeout, self.pendentes())

    # --- Internos --------------------------------------------------------------

    def _por(self, item: tuple) -> None:
        """Poe na fila um item que grava logo (nao espera o lote de amostras)."""
        with self._condicao:
            self._urgentes.append(item)
            self._condicao.notify()

    def _avisar_descarte(self, quantas: int) -> None:
        agora = self._relogio()
        with self._condicao:
            self._descartadas += quantas
            if self._ultimo_aviso is not None and agora - self._ultimo_aviso < INTERVALO_AVISO_FILA_S:
                return
            self._ultimo_aviso, total, self._descartadas = agora, self._descartadas, 0
        registro.warning("fila do banco cheia: %d amostras descartadas (o disco nao esta dando conta? confira a "
                         "linha 'recursos:' e o espaco livre); os eventos dos sinais nunca sao descartados", total)

    def _amostras_prontas(self) -> bool:
        """(Com a condicao) True se as amostras vao agora: parando, alguem esperando ou o lote completou o tempo."""
        if not self._amostras and not self._vistos:
            return False
        if self._parando or self._pedidos_de_descarga:
            return True
        return time.monotonic() - self._primeira_amostra_em >= self._intervalo_lote_s

    def _quanto_esperar(self) -> float | None:
        """(Com a condicao) quanto dormir ate o lote de amostras completar o tempo (None = ate alguem acordar)."""
        if self._primeira_amostra_em is None:
            return None
        return max(0.0, self._intervalo_lote_s - (time.monotonic() - self._primeira_amostra_em))

    def _trabalhar(self) -> None:
        """A thread: tira o que estiver pronto na fila, grava, faz o checkpoint quando for a hora e repete."""
        while True:
            with self._condicao:
                while not (self._urgentes or self._amostras_prontas() or self._parando):
                    self._condicao.wait(self._quanto_esperar())
                urgentes, self._urgentes = self._urgentes, []
                amostras, vistos = [], {}
                if self._amostras_prontas():
                    amostras, self._amostras, self._primeira_amostra_em = self._amostras, [], None
                    vistos, self._vistos = self._vistos, {}
                if not urgentes and not amostras and not vistos:  # parando e sem mais nada para gravar
                    return
                self._gravando = True
            devolver: list[tuple] = []
            erro = None
            try:
                devolver, erro = self._gravar(urgentes, amostras, vistos)
                if erro is None:
                    self._checkpoint_se_for_hora()
            finally:
                with self._condicao:
                    self._urgentes[:0] = devolver  # na frente e na mesma ordem: o "novo" antes do "encerrado"
                    self._gravando = False
                    self._condicao.notify_all()
            if erro is None:
                self._erros_seguidos = 0
                continue
            self._erros_seguidos += 1
            if devolver and self._parando and self._erros_seguidos >= TENTATIVAS_AO_PARAR:
                self._desistir(erro)
                return
            espera = espera_depois_de_erros(self._erros_seguidos, self._espera_erro_s)
            if devolver:
                registro.warning("nao consegui gravar no banco (%s: %s); %d itens (eventos dos sinais, sessao, "
                                 "leituras das corretoras) voltaram para a fila e o escritor tenta de novo em %gs "
                                 "(nada se perde); se o scanner.db estiver aberto em outro programa, feche-o",
                                 type(erro).__name__, erro, len(devolver), espera)
            self._parada.wait(espera)  # `parar` acorda na hora

    def _desistir(self, erro: Exception) -> None:
        """Ao parar, com o banco sem aceitar gravacao: desiste do que sobrou na fila, avisando no log."""
        with self._condicao:
            eventos = sum(len(dados) for tipo, dados in self._urgentes if tipo == "eventos")
            self._urgentes.clear()
        registro.error("o banco continua sem aceitar gravacao ao parar (%s: %s): %d eventos de sinal ficaram sem "
                       "gravar; ao religar, os registros que ficaram abertos sao fechados com a ultima hora conhecida",
                       type(erro).__name__, erro, eventos)

    def _gravar(self, urgentes: list[tuple], amostras: list[tuple],
                vistos: dict[str, str] | None = None) -> tuple[list[tuple], Exception | None]:
        """Grava um lote: os eventos numa transacao, as amostras noutra (veja o topo do arquivo sobre os erros).

        Devolve (os itens que voltam para a fila, o ultimo erro ou None). So erro do SQLite (OperationalError)
        devolve itens; as amostras nunca voltam.
        """
        eventos, sessao, leituras = [], None, []
        for tipo, dados in urgentes:
            if tipo == "eventos":
                eventos.extend(dados)
            elif tipo == "sessao":
                sessao = dados  # vale a ultima
            else:
                leituras.append(dados)
        devolver: list[tuple] = []
        erro: Exception | None = None
        if eventos:
            try:
                explicar_recusados(self._banco.gravar_eventos(eventos))
            except sqlite3.OperationalError as falha:  # banco travado, disco cheio...: tenta de novo depois
                devolver.append(("eventos", eventos))
                erro = falha
            except Exception as falha:
                erro = falha
                registro.exception("nao consegui gravar %d eventos no banco (defeito: ficam de fora)", len(eventos))
        if amostras:
            try:
                self._banco.gravar_amostras(amostras)
            except Exception as falha:
                erro = falha
                registro.error("nao consegui gravar %d amostras no banco (%s: %s); essas amostras foram descartadas",
                               len(amostras), type(falha).__name__, falha,
                               exc_info=not isinstance(falha, sqlite3.OperationalError))
        if vistos:
            try:
                self._banco.marcar_vistos(list(vistos.items()))
            except Exception as falha:  # so serve depois de uma queda: fica para a proxima vez
                erro = falha
                registro.error("nao consegui anotar no banco quando %d sinais foram vistos (%s: %s)", len(vistos),
                               type(falha).__name__, falha, exc_info=not isinstance(falha, sqlite3.OperationalError))
        if sessao is not None:
            try:
                self._banco.salvar_sessao(sessao)
            except sqlite3.OperationalError as falha:
                devolver.append(("sessao", sessao))
                erro = falha
            except Exception as falha:
                erro = falha
                registro.exception("nao consegui guardar a sessao do painel no banco")
        for posicao, (dia, status) in enumerate(leituras):
            try:
                self._banco.registrar_corretoras(dia, status)
            except sqlite3.OperationalError as falha:
                # Esta e as seguintes voltam, na ordem: as reconexoes sao contadas pela diferenca entre leituras.
                devolver += [("corretoras", leitura) for leitura in leituras[posicao:]]
                erro = falha
                break
            except Exception as falha:
                erro = falha
                registro.exception("erro ao gravar o atraso e as reconexoes das corretoras no banco")
        return devolver, erro

    def _checkpoint_se_for_hora(self) -> None:
        agora = self._relogio()
        if agora - self._ultimo_checkpoint < self._intervalo_checkpoint_s:
            return
        self._ultimo_checkpoint = agora
        try:
            self._banco.checkpoint()
        except Exception:
            registro.exception("erro no checkpoint do banco; tento de novo no proximo intervalo")
