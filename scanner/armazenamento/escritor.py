"""Escritor do banco: grava no SQLite numa thread propria, fora do laco do asyncio.

O motor (e o resto do laco: sessao do painel, metricas das corretoras) so poe o que precisa gravar na fila
do escritor e segue. A thread "escritor do banco" tira o que estiver na fila, grava em lote (uma transacao
para as amostras, uma para os eventos) e, de tempos em tempos, faz o checkpoint do WAL. Assim o disco lento,
o checkpoint ou uma leitura pesada da API (que segura a trava do banco) nunca param o motor, os coletores
nem o painel.

Quando cada coisa e gravada:
- eventos dos sinais, a sessao e as metricas das corretoras: logo (sao poucos);
- amostras: esperam juntar ate INTERVALO_LOTE_AMOSTRAS_S. Cada lote suja uma pagina de 4 KB por combinacao (as
  amostras de cada par ficam juntas no disco), entao juntar 10 s em vez de 5 s escreve a metade no disco
  (medido com 4045 combinacoes com livro: 8,5 GB por hora no WAL com um lote a cada 5 s, 4,3 GB com um a cada
  10 s). O custo: o grafico do painel pode ficar ate esse tempo atras, e uma queda de energia perde no maximo
  isso de amostras.

Fila:
- amostras: no maximo `max_amostras_na_fila`. Se o disco nao der conta e a fila encher, as amostras que nao
  cabem sao descartadas, com um aviso no log (no maximo um por INTERVALO_AVISO_FILA_S, com quantas foram);
- eventos de sinal, a sessao e as metricas das corretoras NUNCA sao descartados.

Erro do banco (disco cheio, arquivo travado) vai para o log e o escritor segue com o proximo lote.
`parar` grava tudo o que ainda estiver na fila antes de terminar (o __main__ chama ao encerrar).
"""

import logging
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
PAUSA_DEPOIS_DE_ERRO_S = 1.0


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
    """

    def __init__(self, banco, max_amostras_na_fila: int = MAX_AMOSTRAS_NA_FILA,
                 intervalo_checkpoint_s: float = INTERVALO_CHECKPOINT_S,
                 relogio: Callable[[], float] = time.monotonic,
                 intervalo_lote_amostras_s: float = INTERVALO_LOTE_AMOSTRAS_S):
        self._banco = banco
        self._max_amostras = max_amostras_na_fila
        self._intervalo_checkpoint_s = intervalo_checkpoint_s
        self._intervalo_lote_s = intervalo_lote_amostras_s
        self._relogio = relogio
        self._condicao = threading.Condition()  # protege tudo abaixo e acorda a thread
        # Duas filas: os itens que gravam logo, na ordem em que chegaram ((tipo, dados): "eventos", "sessao",
        # "corretoras"), e as amostras que esperam o lote.
        self._urgentes: list[tuple] = []
        self._amostras: list[tuple] = []
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

    def salvar_sessao(self, sessao: dict) -> None:
        """Poe a sessao na fila (nunca descarta; se houver varias, vale a ultima)."""
        self._por(("sessao", sessao))

    def registrar_corretoras(self, dia: str, status: dict) -> None:
        """Poe uma leitura das corretoras na fila (Banco.registrar_corretoras; nunca descarta)."""
        self._por(("corretoras", (dia, status)))

    def pendentes(self) -> int:
        """Quantos itens esperam na fila agora: amostras + eventos/sessao/leituras (vai para a linha de recursos)."""
        with self._condicao:
            return len(self._amostras) + len(self._urgentes)

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
                while self._amostras or self._urgentes or self._gravando:
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
        if not self._amostras:
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
                amostras = []
                if self._amostras_prontas():
                    amostras, self._amostras, self._primeira_amostra_em = self._amostras, [], None
                if not urgentes and not amostras:  # parando e sem mais nada para gravar
                    return
                self._gravando = True
            try:
                deu_erro = self._gravar(urgentes, amostras)
                self._checkpoint_se_for_hora()
            finally:
                with self._condicao:
                    self._gravando = False
                    self._condicao.notify_all()
            if deu_erro and not self._parando:
                time.sleep(PAUSA_DEPOIS_DE_ERRO_S)

    def _gravar(self, urgentes: list[tuple], amostras: list[tuple]) -> bool:
        """Grava um lote: os eventos numa transacao, as amostras noutra. Erro vai ao log. True se algo falhou."""
        eventos, sessao, leituras = [], None, []
        for tipo, dados in urgentes:
            if tipo == "eventos":
                eventos.extend(dados)
            elif tipo == "sessao":
                sessao = dados  # vale a ultima
            else:
                leituras.append(dados)
        deu_erro = False
        if eventos:
            try:
                explicar_recusados(self._banco.gravar_eventos(eventos))
            except Exception:
                deu_erro = True
                registro.exception("nao consegui gravar %d eventos no banco", len(eventos))
        if amostras:
            try:
                self._banco.gravar_amostras(amostras)
            except Exception:
                deu_erro = True
                registro.exception("nao consegui gravar %d amostras no banco", len(amostras))
        if sessao is not None:
            try:
                self._banco.salvar_sessao(sessao)
            except Exception:
                deu_erro = True
                registro.exception("nao consegui guardar a sessao do painel no banco")
        for dia, status in leituras:
            try:
                self._banco.registrar_corretoras(dia, status)
            except Exception:
                deu_erro = True
                registro.exception("erro ao gravar o atraso e as reconexoes das corretoras no banco")
        return deu_erro

    def _checkpoint_se_for_hora(self) -> None:
        agora = self._relogio()
        if agora - self._ultimo_checkpoint < self._intervalo_checkpoint_s:
            return
        self._ultimo_checkpoint = agora
        try:
            self._banco.checkpoint()
        except Exception:
            registro.exception("erro no checkpoint do banco; tento de novo no proximo intervalo")
