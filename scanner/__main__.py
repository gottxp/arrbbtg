"""Ponto de entrada: python -m scanner [--config caminho] [--validar]

Sobe o scanner: le o config.yaml, conecta nas corretoras (so dados publicos), acompanha os
tickers de todos os pares, abre o livro dos pares interessantes e, a cada `ciclo_ms`, roda o
motor (candidatos -> filtros -> sinais -> banco). Junto sobe a API do painel (rotas /api e o
WebSocket /ws/sinais, que recebe cada evento do motor). A cada minuto o log mostra quantos
sinais estao ativos. Ctrl+C encerra fechando as conexoes, o painel e o banco.

Cotacoes (Task 15): os precos em USDC (ex.: Hyperliquid) sao convertidos para USDT pela cotacao do
ticker spot USDC/USDT da fonte_cotacao (DadosLentos.cotacoes); esse par entra sempre nos tickers da
fonte, mesmo sem formar combinacao (veja _listas_de_tickers).

Robustez (especificacao, secao 8): conexao que cai volta sozinha (coletor.manter_tickers), corretora
que nao carregou a lista de mercados ao ligar tenta de novo sozinha (_trazer_de_volta), a lista de
mercados e recarregada a cada `recarregar_mercados_s`, e a cada `intervalo_log_recursos_s` o log
registra memoria, CPU e o resumo dos descartes. Log no terminal e num arquivo por dia (scanner/logs.py).

Lighter (Task 16): lighter e lighter_rh usam o conector proprio (scanner/coletores/lighter.py) no lugar do
ccxt; o funding e a taxa delas chegam ao DadosLentos pelo proprio coletor (fora do laco REST dele).

Validacao (especificacao, secao 9): no mesmo intervalo o banco guarda o atraso e as reconexoes de cada
corretora no dia; quando o dia UTC vira, o resumo do dia anterior vai para o log. O banco apaga o que passou
de `retencao_dias` ao ligar e a cada virada do dia (_validacao_diaria).

Com --validar, so confere o config.yaml e sai (nao conecta em nada).
"""

import argparse
import asyncio
import functools
import logging
import sys
import time
from collections.abc import Awaitable, Callable

from scanner.api import conferir_painel, criar_app, servir_api
from scanner.armazenamento import Banco
from scanner.coletores.base import ColetorDoScanner
from scanner.coletores.ccxt_coletor import ColetorCcxt
from scanner.coletores.dados_lentos import DadosLentos
from scanner.coletores.fabrica import criar_corretora
from scanner.coletores.lighter import criar_coletor_lighter, e_lighter
from scanner.coletores.reconexao import espera_reconexao
from scanner.config.carregar import Config, ErroConfig, carregar_config
from scanner.estado import EstadoMercados
from scanner.logs import MedidorCpu, configurar_logs, linha_de_recursos, memoria_do_processo
from scanner.motor import Motor, simbolos_para_tickers
from scanner.sinais import Evento
from scanner.validacao import dia_utc, linhas_do_resumo, resumo_do_dia

# Nome fixo (e nao __name__, que vira "__main__" com python -m): fica debaixo do logger "scanner".
registro = logging.getLogger("scanner.principal")

# De quanto em quanto tempo o log mostra a quantidade de sinais ativos (especificacao: a cada minuto).
INTERVALO_LOG_SINAIS_S = 60


def configurar_log(cfg: Config) -> None:
    """Log no terminal e num arquivo por dia em `pasta_logs` (guarda `dias_logs` dias); nivel de `nivel_log`.

    "detalhado" mostra tambem as mensagens de depuracao do scanner (ex.: cada livro descartado).
    """
    configurar_logs(cfg.nivel_log, cfg.pasta_logs, cfg.dias_logs)


async def _carregar_mercados(exchanges: dict) -> list[str]:
    """Carrega a lista de mercados de todas as corretoras ao mesmo tempo. Devolve as que conseguiram.

    Uma corretora que falha vai para o log e fica de fora por enquanto; as outras seguem. O `rodar`
    tenta de novo sozinho as que faltaram (veja _trazer_de_volta).
    """
    nomes = list(exchanges)
    resultados = await asyncio.gather(*(exchanges[n].load_markets() for n in nomes), return_exceptions=True)
    carregadas = []
    for nome, resultado in zip(nomes, resultados):
        if isinstance(resultado, BaseException):
            registro.error("%s: nao consegui carregar a lista de mercados (%s: %s); a corretora fica de fora "
                           "por enquanto e eu tento de novo sozinho em instantes", nome, type(resultado).__name__,
                           resultado)
        else:
            carregadas.append(nome)
    return carregadas


async def _acompanhar_tickers(coletor: ColetorDoScanner, simbolos: list[str]) -> None:
    """Etapa 1 de uma corretora, com reconexao (veja manter_tickers do ColetorCcxt e do ColetorLighter).

    Qualquer erro dentro do stream (queda de conexao, erro da corretora ou defeito do proprio scanner)
    e tratado pelo coletor: espera crescente e tenta de novo; o que nao e queda de conexao sai no log
    com traceback na primeira vez. Aqui so chega um erro de fora desse laco (ex.: no proprio laco de
    reconexao): vai para o log, com traceback, e as outras corretoras seguem.
    """
    try:
        await coletor.manter_tickers(simbolos)
    except Exception:  # cancelamento (Ctrl+C) nao entra aqui
        registro.exception("%s: os tickers pararam por um erro inesperado; a corretora fica sem sinais ate "
                           "reiniciar o scanner", coletor.nome)


def _listas_de_tickers(coletores: dict[str, ColetorDoScanner],
                       dados_lentos: DadosLentos | None = None) -> tuple[dict[str, list[str]], dict]:
    """Os simbolos da etapa 1 de cada corretora e, de cada uma, o simbolo que ocupa cada chave do estado.

    Entram os pares que formam alguma combinacao (simbolos_para_tickers) e, na fonte das cotacoes, o par spot
    de cada moeda de cotacao fora de USDT usada por esses pares (ex.: USDC/USDT da Bitget, porque a Hyperliquid
    cota em USDC), mesmo que ele nao forme combinacao nenhuma. Cada coletor fica sabendo quais dos seus
    simbolos sao de cotacao (usar_para_cotacao). Sem `dados_lentos`, so as combinacoes.
    Devolve (listas, vencedores): {corretora: [simbolos]} e {corretora: {(mercado, ativo): simbolo}}.
    """
    vencedores = {nome: coletor.simbolos_por_chave() for nome, coletor in coletores.items()}
    listas = simbolos_para_tickers(vencedores)
    if dados_lentos is not None:
        moedas: set[str] = set()
        for nome, simbolos in listas.items():
            moedas |= coletores[nome].cotacoes_dos_simbolos(simbolos)
        extras = dados_lentos.escolher_pares_de_cotacao(moedas, {nome: c.markets() for nome, c in coletores.items()})
        for nome, coletor in coletores.items():
            coletor.usar_para_cotacao(extras.get(nome, []))
        for nome, simbolos in extras.items():
            listas[nome] = sorted(set(listas.get(nome, [])) | set(simbolos))
    return listas, vencedores


class _Tickers:
    """As tarefas da etapa 1 (uma por corretora) e a lista de simbolos que cada uma acompanha.

    A recarga da lista de mercados e a corretora que entra depois usam isto para reiniciar so as
    corretoras cuja lista mudou. `trava`: so um ajuste das listas por vez (veja _ajustar_tickers).
    `cotacao`: o DadosLentos que escolhe os pares de cotacao que entram nas listas (None = nenhum).
    """

    def __init__(self, coletores: dict[str, ColetorDoScanner], cotacao: DadosLentos | None = None):
        self._coletores = coletores
        self.cotacao = cotacao
        self.tarefas: dict[str, asyncio.Task] = {}
        self.simbolos: dict[str, list[str]] = {}
        self.trava = asyncio.Lock()

    def iniciar(self, nome: str, simbolos: list[str]) -> None:
        """Comeca a acompanhar os tickers de `simbolos` na corretora `nome` (numa tarefa propria).

        Se ja havia uma tarefa dessa corretora, ela e cancelada antes: nunca dois streams da mesma corretora.
        """
        anterior = self.tarefas.get(nome)
        if anterior is not None and not anterior.done():
            anterior.cancel()
        else:
            anterior = None
        self.simbolos[nome] = list(simbolos)
        self.tarefas[nome] = asyncio.create_task(
            self._comecar(self._coletores[nome], list(simbolos), anterior), name=f"tickers {nome}"
        )

    @staticmethod
    async def _comecar(coletor: ColetorDoScanner, simbolos: list[str], anterior: asyncio.Task | None) -> None:
        """Espera a tarefa anterior (ja cancelada) terminar de verdade e so entao abre o stream novo.

        Os dois streams dividem o estado de reconexao do coletor; sem esperar, por um instante os dois
        estariam de pe. (Quem chama normalmente ja usa `parar` antes; isto e a protecao extra.)
        """
        if anterior is not None:
            await asyncio.gather(anterior, return_exceptions=True)
        await _acompanhar_tickers(coletor, simbolos)

    async def parar(self, nome: str) -> None:
        """Para os tickers da corretora `nome` e espera a tarefa terminar de verdade."""
        self.simbolos.pop(nome, None)
        tarefa = self.tarefas.pop(nome, None)
        if tarefa is not None:
            tarefa.cancel()
            await asyncio.gather(tarefa, return_exceptions=True)


async def _recarregar_mercados(exchanges: dict, coletores: dict[str, ColetorDoScanner], tickers: _Tickers,
                               estado: EstadoMercados) -> None:
    """Recarrega a lista de mercados de todas as corretoras (especificacao, secao 8: mudancas nas corretoras).

    load_markets(reload=True) em todas ao mesmo tempo; a que falhar vai para o log e segue com a lista
    anterior. Depois ajusta os tickers de todas (veja _ajustar_tickers).
    """
    nomes = list(coletores)
    resultados = await asyncio.gather(*(exchanges[n].load_markets(reload=True) for n in nomes),
                                      return_exceptions=True)
    for nome, resultado in zip(nomes, resultados):
        if isinstance(resultado, Exception):
            registro.warning("%s: nao consegui recarregar a lista de mercados (%s: %s); sigo com a lista anterior",
                             nome, type(resultado).__name__, resultado)
    await _ajustar_tickers(coletores, tickers, estado)


async def _ajustar_tickers(coletores: dict[str, ColetorDoScanner], tickers: _Tickers, estado: EstadoMercados) -> None:
    """Recalcula os simbolos da etapa 1 de todas as corretoras e ajusta os streams.

    - o stream de tickers so reinicia nas corretoras cuja lista mudou (ex.: uma corretora nova entrou e
      as outras ganharam pares em comum com ela);
    - par que saiu da lista (a corretora tirou, ficou inativo ou deixou de formar combinacao) sai do
      estado, ticker e livro: some do scanner sem erro e o sinal dele e encerrado pelo fluxo normal;
    - o par de cotacao da fonte (ex.: USDC/USDT) continua na lista enquanto alguma corretora cotar na moeda
      dele (veja _listas_de_tickers).
    Um ajuste por vez (`tickers.trava`): a recarga e a corretora que entra nao se atropelam.
    """
    async with tickers.trava:
        novos, vencedores = _listas_de_tickers(coletores, tickers.cotacao)
        for nome in list(coletores):
            lista = novos.get(nome, [])
            anterior = tickers.simbolos.get(nome, [])
            mudou = lista != anterior
            if mudou:
                registro.info("%s: a lista de pares mudou (%d -> %d pares); reiniciando os tickers",
                              nome, len(anterior), len(lista))
                await tickers.parar(nome)  # antes de limpar o estado: o stream velho nao devolve o par que saiu
            usados = set(lista)
            chaves = {chave for chave, simbolo in vencedores[nome].items() if simbolo in usados}
            removidos = estado.remover_pares_fora(nome, chaves)
            if removidos:
                registro.info("%s: %d pares sairam da lista e foram tirados do scanner", nome, removidos)
            if mudou and lista:
                tickers.iniciar(nome, lista)


async def _trazer_de_volta(nome: str, exchange, coletores: dict[str, ColetorDoScanner], tickers: _Tickers,
                           estado: EstadoMercados, criar_coletor: Callable[[str], ColetorDoScanner],
                           ao_entrar: Callable[[str], None], espera_max_s: float,
                           esperar: Callable[[float], Awaitable] = asyncio.sleep) -> None:
    """Corretora que nao carregou a lista de mercados ao ligar: tenta de novo sozinha ate conseguir.

    Espera 1s, 2s, 4s... (ate espera_max_s) entre as tentativas; cada tentativa que falha vai para o log.
    Enquanto isso o painel mostra "reconectando" para ela (a primeira conexao dela nao conta como
    reconexao). Quando carrega: cria o coletor dela, chama `ao_entrar` (o __main__ liga os dados lentos
    dela; a recarga dos mercados ja passa a incluir quem esta em `coletores`) e ajusta os tickers de todas
    (as outras podem ganhar pares em comum com ela).
    Se der erro nessa entrada (defeito do scanner), vai para o log com traceback e tenta de novo na
    proxima espera, sem repetir o que ja deu certo: a corretora nunca fica "meio dentro" em silencio.
    """
    estado.marcar_esperando(nome)  # painel: "reconectando"
    tentativa = 0
    entrou = False  # `ao_entrar` ja foi chamado (os dados lentos dela ja estao ligados)
    while True:
        await esperar(espera_reconexao(tentativa, espera_max_s))
        tentativa += 1
        proxima = espera_reconexao(tentativa, espera_max_s)
        try:
            await exchange.load_markets()
        except Exception as erro:  # cancelamento (Ctrl+C) nao entra aqui
            registro.warning("%s: ainda nao consegui carregar a lista de mercados (%s: %s); tentativa %d falhou, "
                             "tento de novo em %gs", nome, type(erro).__name__, erro, tentativa, proxima)
            continue
        try:
            if nome not in coletores:
                coletores[nome] = criar_coletor(nome)
                registro.info("%s: lista de mercados carregada na tentativa %d; a corretora entra no scanner",
                              nome, tentativa)
            if not entrou:
                ao_entrar(nome)
                entrou = True
            await _ajustar_tickers(coletores, tickers, estado)
        except Exception:
            registro.exception("%s: a lista de mercados carregou, mas deu erro ao colocar a corretora no scanner; "
                               "tento de novo em %gs", nome, proxima)
            continue
        if nome not in tickers.tarefas:  # sem ticker nenhum, nada tiraria o "reconectando" do painel
            registro.warning("%s: nenhum par em comum com as outras corretoras; fica sem tickers", nome)
            estado.marcar_reconectada(nome)
        return


async def _recarregar_mercados_sempre(exchanges: dict, coletores: dict[str, ColetorDoScanner], tickers: _Tickers,
                                      estado: EstadoMercados, intervalo_s: float,
                                      esperar: Callable[[float], Awaitable] = asyncio.sleep) -> None:
    """A cada `intervalo_s` (recarregar_mercados_s), recarrega a lista de mercados. Erro vai ao log e o laco segue."""
    while True:
        await esperar(intervalo_s)
        try:
            await _recarregar_mercados(exchanges, coletores, tickers, estado)
        except Exception:
            registro.exception("erro ao recarregar a lista de mercados; tento de novo em %ss", intervalo_s)


def _resumo_dos_descartes(coletores: dict[str, ColetorDoScanner], intervalo_s: float) -> str:
    """Linha do log com os descartes de cada corretora desde o ultimo resumo (e zera os contadores)."""
    partes = []
    for nome, coletor in coletores.items():
        d = coletor.tirar_descartes()
        if any(d.values()):
            partes.append(f"{nome} livros {d['livros']}, tickers {d['tickers']}, "
                          f"sequencia quebrada {d['sequencia_quebrada']}")
    return f"descartes nos ultimos {intervalo_s:g}s: {'; '.join(partes) if partes else 'nenhum'}"


async def _registrar_recursos(motor: Motor, coletores: dict[str, ColetorDoScanner], intervalo_s: float,
                              esperar: Callable[[float], Awaitable] = asyncio.sleep,
                              medidor: MedidorCpu | None = None) -> None:
    """A cada `intervalo_s` (intervalo_log_recursos_s), registra no log memoria, CPU e o resumo dos descartes.

    Memoria = RSS do processo; CPU = % desde a linha anterior. Na mesma linha: sinais ativos e quantos
    historicos (combinacoes com amostras na memoria) o motor guarda. Um erro aqui nunca para o scanner.
    """
    medidor = MedidorCpu() if medidor is None else medidor
    while True:
        await esperar(intervalo_s)
        try:
            registro.info(linha_de_recursos(memoria_do_processo(), medidor.medir(), len(motor.sinais_ativos()),
                                            motor.quantidade_historicos()))
            registro.info(_resumo_dos_descartes(coletores, intervalo_s))
        except Exception:
            registro.exception("erro ao registrar memoria e CPU no log")


async def _ajustar_livros_da_corretora(coletor: ColetorDoScanner, simbolos: set[str], com_traceback: set[str]) -> None:
    """Etapa 2 de uma corretora: abre e cancela livros conforme a selecao do motor.

    Erro vai para o log (com traceback na primeira vez de cada corretora; depois uma linha) e o proximo
    ciclo tenta de novo.
    """
    try:
        await coletor.assinar_livros(simbolos)
    except Exception as erro:  # cancelamento (fim do programa) nao entra aqui
        primeira = coletor.nome not in com_traceback
        com_traceback.add(coletor.nome)
        registro.error("%s: erro ao ajustar os livros (%s: %s); tentando de novo no proximo ciclo",
                       coletor.nome, type(erro).__name__, erro, exc_info=primeira)


def _iniciar_ajustes(coletores: dict[str, ColetorDoScanner], selecao: dict[str, set[str]],
                     ajustes: dict[str, asyncio.Task], com_traceback: set[str]) -> None:
    """Comeca o ajuste de livros de cada corretora cujo ajuste anterior ja terminou.

    Cada corretora tem o seu: uma corretora lenta para confirmar (ou travada) nao atrasa as outras.
    Todas recebem a lista (vazia tambem), para cancelar os livros que sairam.
    """
    for nome, coletor in coletores.items():
        anterior = ajustes.get(nome)
        if anterior is not None and not anterior.done():
            registro.debug("%s: o ajuste de livros anterior ainda nao terminou; este ciclo nao abre outro", nome)
            continue
        ajustes[nome] = asyncio.create_task(
            _ajustar_livros_da_corretora(coletor, selecao.get(nome, set()), com_traceback),
            name=f"ajuste de livros {nome}",
        )


def _transmitir(publicar: Callable[[list[Evento]], None] | None, eventos: list[Evento]) -> None:
    """Entrega os eventos do ciclo aos paineis conectados. Nunca levanta erro: o motor nao pode parar por causa do painel."""
    if publicar is None:
        return
    try:
        publicar(eventos)
    except Exception:
        registro.exception("erro ao transmitir os eventos ao painel; o motor segue")


async def _servir_painel(app, cfg: Config) -> None:
    """Sobe a API do painel. Se ela nao subir (ex.: porta em uso), registra e deixa o scanner seguindo sem painel."""
    try:
        await servir_api(app, cfg)
    except Exception as erro:  # cancelamento (Ctrl+C) nao entra aqui
        registro.error("o painel parou (%s: %s); o scanner segue sem painel ate reiniciar", type(erro).__name__, erro)


async def _laco_do_motor(motor: Motor, coletores: dict[str, ColetorDoScanner], cfg: Config,
                         relogio: Callable[[], float], ciclos: int | None,
                         publicar: Callable[[list[Evento]], None] | None = None) -> None:
    """A cada `ciclo_ms`: roda o ciclo do motor, transmite os eventos ao painel e ajusta os livros abertos (etapa 2).

    O ajuste dos livros roda em tarefas proprias (uma por corretora), fora do ritmo do motor: se uma
    corretora demorar para responder, o motor e as outras corretoras continuam, e o proximo ajuste
    dela so comeca quando o anterior acabar.
    ciclos: quantos ciclos rodar (os testes usam 2); None = ate ser cancelado (Ctrl+C).
    publicar: recebe os eventos de cada ciclo (o `Transmissor` da API); o motor nunca espera por ele.
    Erro no ciclo (sinais) e erro na etapa 2 (escolha dos livros) sao separados: um nao impede o
    outro; vai para o log e o proximo ciclo tenta de novo.
    """
    intervalo_s = cfg.ciclo_ms / 1000
    relogio_do_laco = asyncio.get_running_loop().time  # relogio que nao pula (so para marcar o ritmo)
    proximo = relogio_do_laco() + intervalo_s  # espera um ciclo antes do primeiro: da tempo dos tickers chegarem
    ultimo_log: float | None = None
    ajustes: dict[str, asyncio.Task] = {}  # corretora -> ajuste de livros em andamento
    com_traceback: set[str] = set()  # corretoras cujo erro de ajuste ja saiu com traceback no log
    feitos = 0
    try:
        while ciclos is None or feitos < ciclos:
            await asyncio.sleep(max(0.0, proximo - relogio_do_laco()))
            # Ritmo fixo de ciclo_ms, descontando o tempo gasto no ciclo; se atrasar, segue sem "recuperar" ciclos.
            proximo = max(proximo + intervalo_s, relogio_do_laco())
            agora = relogio()
            try:
                eventos = motor.ciclo(agora)
            except Exception:
                registro.exception("erro no ciclo do motor; tentando de novo no proximo ciclo")
            else:
                _transmitir(publicar, eventos)
            try:
                selecao = motor.selecao_livros(agora)
            except Exception:
                registro.exception("erro na etapa 2 (escolha dos livros); tentando de novo no proximo ciclo")
            else:
                _iniciar_ajustes(coletores, selecao, ajustes, com_traceback)
            feitos += 1
            if ultimo_log is None or agora - ultimo_log >= INTERVALO_LOG_SINAIS_S:
                ultimo_log = agora
                registro.info("sinais ativos: %d", len(motor.sinais_ativos()))
    finally:
        for tarefa in ajustes.values():
            tarefa.cancel()
        await asyncio.gather(*ajustes.values(), return_exceptions=True)


# ---------------------------------------------------------------------------
# Metricas de validacao e limpeza do banco (especificacao, secoes 8 e 9)
# ---------------------------------------------------------------------------

SEGUNDOS_POR_DIA = 86400


def _ate_a_meia_noite_utc(agora: float) -> float:
    """Quantos segundos faltam para a proxima meia-noite UTC (onde o dia das metricas vira)."""
    return SEGUNDOS_POR_DIA - agora % SEGUNDOS_POR_DIA


async def _limpar_banco(banco: Banco, cfg: Config, agora: float) -> None:
    """Apaga do banco o que passou de `retencao_dias`. Erro vai para o log e o scanner segue.

    Roda em outra thread: com o banco grande a limpeza leva segundos, e assim o motor espera no maximo um
    lote dela por vez (veja Banco.limpar_antigos), em vez de o laco inteiro parar.
    """
    try:
        apagadas = await asyncio.to_thread(banco.limpar_antigos, agora, cfg.retencao_dias)
    except Exception:
        registro.exception("erro ao limpar os dados antigos do banco; tento de novo na proxima virada do dia")
    else:
        registro.info("limpeza do banco: %d amostras com mais de %g dias apagadas", apagadas, cfg.retencao_dias)


def _gravar_corretoras(motor: Motor, banco: Banco, dia: str, agora: float) -> None:
    """Grava no dia `dia` o atraso medio e as reconexoes de cada corretora (Motor.status). Erro vai para o log."""
    try:
        banco.registrar_corretoras(dia, motor.status(agora))
    except Exception:
        registro.exception("erro ao gravar o atraso e as reconexoes das corretoras no banco")


async def _escrever_resumo(banco: Banco, dia: str) -> None:
    """Escreve no log o resumo do dia `dia`: uma linha por modo e uma das corretoras. Erro vai para o log."""
    try:
        resumo = await asyncio.to_thread(resumo_do_dia, banco, dia)  # le o dia inteiro do banco: fora do laco
    except Exception:
        registro.exception("erro ao montar o resumo do dia %s", dia)
        return
    for linha in linhas_do_resumo(resumo):
        registro.info(linha)


async def _validacao_diaria(motor: Motor, banco: Banco, cfg: Config, relogio: Callable[[], float],
                            esperar: Callable[[float], Awaitable] = asyncio.sleep) -> None:
    """Metricas de validacao por dia (especificacao, secao 9) e limpeza do banco (secao 8).

    - ao ligar: apaga do banco o que passou de `retencao_dias`;
    - a cada `intervalo_log_recursos_s`: grava o atraso medio e as reconexoes de cada corretora no dia UTC;
    - quando o dia UTC vira (a espera e encurtada para acordar na meia-noite): essa leitura fecha o dia que
      acabou, o resumo dele vai para o log e a limpeza roda de novo;
    - ao sair (Ctrl+C): grava a ultima leitura.
    Nenhum erro aqui para o scanner: vai para o log e o laco segue.
    """
    dia = dia_utc(relogio())  # o dia que esta correndo
    try:
        await _limpar_banco(banco, cfg, relogio())
        while True:
            await esperar(min(cfg.intervalo_log_recursos_s, _ate_a_meia_noite_utc(relogio())))
            agora = relogio()
            _gravar_corretoras(motor, banco, dia, agora)  # na virada, a leitura ainda e do dia que acabou
            if dia_utc(agora) != dia:
                await _escrever_resumo(banco, dia)
                dia = dia_utc(agora)
                await _limpar_banco(banco, cfg, agora)
    finally:
        _gravar_corretoras(motor, banco, dia_utc(relogio()), relogio())


async def _encerrar(tarefas: list[asyncio.Task], coletores: dict[str, ColetorDoScanner], exchanges: dict,
                    banco: Banco | None) -> None:
    """Para tudo e libera as conexoes e o banco. Um erro ao fechar uma coisa nao impede fechar as outras."""
    for tarefa in tarefas:
        tarefa.cancel()
    await asyncio.gather(*tarefas, return_exceptions=True)
    # O coletor fecha os livros e a corretora; a corretora sem coletor (nao carregou) fecha direto.
    fechamentos = [c.fechar() for c in coletores.values()]
    fechamentos += [ex.close() for nome, ex in exchanges.items() if nome not in coletores]
    for erro in await asyncio.gather(*fechamentos, return_exceptions=True):
        if isinstance(erro, Exception):
            registro.warning("erro ao fechar uma corretora (%s: %s)", type(erro).__name__, erro)
    if banco is not None:
        banco.fechar()


async def rodar(
    cfg: Config,
    criar_exchange: Callable[[str], object] | None = None,
    relogio: Callable[[], float] = time.time,
    ciclos: int | None = None,
) -> None:
    """Liga as pecas do scanner e roda ate ser cancelado (ou por `ciclos` ciclos).

    criar_exchange: monta a corretora pelo nome (os testes passam corretoras falsas); None = criar_corretora
        (a Lighter pelo conector proprio, as outras pelo ccxt).
    relogio: hora atual em segundos (os testes passam um relogio fixo).
    Levanta ErroConfig se o bloco `painel` for inseguro (antes de conectar em qualquer corretora) e
    RuntimeError se nenhuma corretora carregar a lista de mercados.
    """
    if criar_exchange is None:  # Task 16: a Lighter pelo conector proprio, as outras pelo ccxt
        criar_exchange = functools.partial(criar_corretora, cfg=cfg)
    estado = EstadoMercados(cfg.max_silencio_conexao_s, cfg.max_idade_livro_s, cfg.atraso_max_valido_s)
    dados_lentos = DadosLentos(
        cfg.taxas, intervalos_funding=cfg.intervalo_funding_h, estado=estado, fonte_cotacao=cfg.fonte_cotacao,
        fontes_cotacao=cfg.fontes_cotacao, faixa_cotacao_pct=cfg.faixa_cotacao_pct, relogio=relogio,
    )
    exchanges: dict = {}
    coletores: dict[str, ColetorDoScanner] = {}
    tickers = _Tickers(coletores, dados_lentos)
    tarefas: list[asyncio.Task] = []
    banco: Banco | None = None
    try:
        for nome in dict.fromkeys(cfg.corretoras):  # sem nomes repetidos, na ordem da config
            exchanges[nome] = criar_exchange(nome)
        banco = Banco(cfg.banco)
        # As cotacoes (USDC/USDT...) vem dos tickers spot da fonte: o motor converte para USDT com elas.
        motor = Motor(cfg, estado, dados_lentos, banco=banco, cotacoes=dados_lentos.cotacoes)
        app = criar_app(motor, banco, cfg, relogio=relogio)  # antes de conectar: painel mal configurado para tudo

        carregadas = await _carregar_mercados(exchanges)
        if not carregadas:
            raise RuntimeError("nenhuma corretora carregou a lista de mercados; confira a internet e o config.yaml")

        def criar_coletor(nome: str) -> ColetorDoScanner:
            if e_lighter(nome):  # Task 16: conector proprio (funding e taxa vao direto ao DadosLentos)
                return criar_coletor_lighter(nome, exchanges[nome], cfg, estado, dados_lentos, relogio)
            return ColetorCcxt(
                nome, exchanges[nome], estado=estado, relogio=relogio,
                timeout_desassinar_s=cfg.timeout_desassinar_s,
                espera_max_reconexao_s=cfg.espera_max_reconexao_s,
                limite_assinaturas=cfg.limites_assinatura.get(nome),
                mercados=cfg.mercados_da_corretora(nome),
                metodo_ticker=cfg.metodo_ticker.get(nome),
                mensagens_sem_bid_ask=cfg.mensagens_sem_bid_ask,
            )

        for nome in carregadas:
            coletores[nome] = criar_coletor(nome)

        # Etapa 1: tickers so dos pares que formam alguma combinacao (spot x futuro ou futuro x futuro),
        # mais o par de cotacao de cada moeda fora de USDT na fonte (ex.: USDC/USDT da Bitget).
        simbolos, _ = _listas_de_tickers(coletores, dados_lentos)
        for nome in coletores:
            if simbolos.get(nome):
                registro.info("%s: acompanhando %d pares", nome, len(simbolos[nome]))
                tickers.iniciar(nome, simbolos[nome])
            else:
                registro.warning("%s: nenhum par em comum com as outras corretoras; fica sem tickers", nome)

        # O laco REST do DadosLentos le as corretoras do ccxt; a Lighter entrega funding e taxa pelo coletor.
        tarefas.append(asyncio.create_task(
            dados_lentos.rodar({nome: exchanges[nome] for nome in carregadas if not e_lighter(nome)},
                               cfg.intervalo_dados_lentos_s),
            name="dados lentos"))

        def ao_entrar(nome: str) -> None:  # corretora que entrou depois: liga os dados lentos dela
            if e_lighter(nome):  # Task 16: funding e taxa da Lighter chegam pelo proprio coletor
                return
            tarefas.append(asyncio.create_task(
                dados_lentos.acompanhar(nome, exchanges[nome], cfg.intervalo_dados_lentos_s),
                name=f"dados lentos {nome}"))

        # Quem nao carregou a lista de mercados ao ligar tenta de novo sozinha, com espera crescente.
        for nome in exchanges:
            if nome not in coletores:
                tarefas.append(asyncio.create_task(
                    _trazer_de_volta(nome, exchanges[nome], coletores, tickers, estado, criar_coletor, ao_entrar,
                                     cfg.espera_max_reconexao_s),
                    name=f"volta de {nome}"))
        tarefas.append(asyncio.create_task(_servir_painel(app, cfg), name="painel"))
        # A recarga passa por todas as corretoras que estao em `coletores` (inclusive as que entrarem depois).
        tarefas.append(asyncio.create_task(
            _recarregar_mercados_sempre(exchanges, coletores, tickers, estado, cfg.recarregar_mercados_s),
            name="recarga dos mercados"))
        tarefas.append(asyncio.create_task(
            _registrar_recursos(motor, coletores, cfg.intervalo_log_recursos_s), name="recursos"))

        # Metricas de validacao por dia e limpeza do banco (ao ligar e a cada virada do dia UTC).
        tarefas.append(asyncio.create_task(_validacao_diaria(motor, banco, cfg, relogio), name="validacao"))

        await _laco_do_motor(motor, coletores, cfg, relogio, ciclos, publicar=app.state.transmissor.publicar)
    finally:
        # A recarga dos mercados (em `tarefas`) e cancelada junto: ela nao comeca tickers novos depois disto.
        await _encerrar(tarefas + list(tickers.tarefas.values()), coletores, exchanges, banco)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scanner", description="Scanner de arbitragem cripto")
    parser.add_argument("--config", default="config.yaml", help="caminho do config.yaml (padrao: ./config.yaml)")
    parser.add_argument("--validar", action="store_true",
                        help="so confere o config.yaml e sai, sem conectar nas corretoras")
    args = parser.parse_args(argv)

    try:
        cfg = carregar_config(args.config)
        conferir_painel(cfg)  # usuario sem senha (ou o contrario) para aqui, antes de conectar em qualquer coisa
    except ErroConfig as erro:
        print(f"Erro na configuracao: {erro}", file=sys.stderr)
        return 1

    if args.validar:
        print("config ok")
        return 0

    configurar_log(cfg)
    try:
        asyncio.run(rodar(cfg))
    except KeyboardInterrupt:  # Ctrl+C: o asyncio.run ja cancelou tudo e o `rodar` fechou as conexoes
        print("\nEncerrado.")
    except Exception as erro:  # mostra o motivo em uma linha, sem o textao do traceback
        print(f"Erro: {type(erro).__name__}: {erro}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
