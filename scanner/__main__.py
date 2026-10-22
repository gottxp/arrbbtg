"""Ponto de entrada: python -m scanner [--config caminho] [--validar]

Sobe o scanner: le o config.yaml, conecta nas corretoras (so dados publicos), acompanha os
tickers de todos os pares, abre o livro dos pares interessantes e, a cada `ciclo_ms`, roda o
motor (candidatos -> filtros -> sinais -> banco). Junto sobe a API do painel (rotas /api e o
WebSocket /ws/sinais, que recebe cada evento do motor). A cada minuto o log mostra quantos
sinais estao ativos. Ctrl+C encerra fechando as conexoes, o painel e o banco.

Robustez (especificacao, secao 8): conexao que cai volta sozinha (coletor.manter_tickers), a lista
de mercados e recarregada a cada `recarregar_mercados_s`, e a cada `intervalo_log_recursos_s` o log
registra memoria, CPU e o resumo dos descartes. Log no terminal e num arquivo por dia (scanner/logs.py).

Com --validar, so confere o config.yaml e sai (nao conecta em nada).
"""

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable

from scanner.api import conferir_painel, criar_app, servir_api
from scanner.armazenamento import Banco
from scanner.coletores.ccxt_coletor import ColetorCcxt, criar_exchange_ccxt
from scanner.coletores.dados_lentos import DadosLentos
from scanner.config.carregar import Config, ErroConfig, carregar_config
from scanner.estado import EstadoMercados
from scanner.logs import MedidorCpu, configurar_logs, linha_de_recursos, memoria_do_processo
from scanner.motor import Motor, simbolos_para_tickers
from scanner.sinais import Evento

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

    Uma corretora que falha vai para o log e fica de fora; as outras seguem.
    """
    nomes = list(exchanges)
    resultados = await asyncio.gather(*(exchanges[n].load_markets() for n in nomes), return_exceptions=True)
    carregadas = []
    for nome, resultado in zip(nomes, resultados):
        if isinstance(resultado, BaseException):
            registro.error("%s: nao consegui carregar a lista de mercados (%s: %s); a corretora fica de fora",
                           nome, type(resultado).__name__, resultado)
        else:
            carregadas.append(nome)
    return carregadas


async def _acompanhar_tickers(coletor: ColetorCcxt, simbolos: list[str]) -> None:
    """Etapa 1 de uma corretora, com reconexao (veja ColetorCcxt.manter_tickers).

    Quedas de conexao sao tratadas pelo coletor (espera crescente e tenta de novo). Aqui so chega um
    defeito inesperado do proprio scanner: vai para o log, com traceback, e as outras corretoras seguem.
    """
    try:
        await coletor.manter_tickers(simbolos)
    except Exception:  # cancelamento (Ctrl+C) nao entra aqui
        registro.exception("%s: os tickers pararam por um erro inesperado; a corretora fica sem sinais ate "
                           "reiniciar o scanner", coletor.nome)


class _Tickers:
    """As tarefas da etapa 1 (uma por corretora) e a lista de simbolos que cada uma acompanha.

    A recarga da lista de mercados usa isto para reiniciar so as corretoras cuja lista mudou.
    """

    def __init__(self, coletores: dict[str, ColetorCcxt]):
        self._coletores = coletores
        self.tarefas: dict[str, asyncio.Task] = {}
        self.simbolos: dict[str, list[str]] = {}

    def iniciar(self, nome: str, simbolos: list[str]) -> None:
        """Comeca a acompanhar os tickers de `simbolos` na corretora `nome` (numa tarefa propria)."""
        self.simbolos[nome] = list(simbolos)
        self.tarefas[nome] = asyncio.create_task(
            _acompanhar_tickers(self._coletores[nome], list(simbolos)), name=f"tickers {nome}"
        )

    async def parar(self, nome: str) -> None:
        """Para os tickers da corretora `nome` e espera a tarefa terminar de verdade."""
        self.simbolos.pop(nome, None)
        tarefa = self.tarefas.pop(nome, None)
        if tarefa is not None:
            tarefa.cancel()
            await asyncio.gather(tarefa, return_exceptions=True)


async def _recarregar_mercados(exchanges: dict, coletores: dict[str, ColetorCcxt], tickers: _Tickers,
                               estado: EstadoMercados) -> None:
    """Recarrega a lista de mercados de todas as corretoras (especificacao, secao 8: mudancas nas corretoras).

    1. load_markets(reload=True) em todas ao mesmo tempo; a que falhar vai para o log e segue com a
       lista anterior.
    2. Recalcula os simbolos da etapa 1; o stream de tickers so reinicia nas corretoras cuja lista mudou.
    3. Par que saiu da lista (a corretora tirou, ou ele deixou de formar combinacao) sai do estado,
       ticker e livro: some do scanner sem erro e o sinal dele e encerrado pelo fluxo normal.
    """
    nomes = list(coletores)
    resultados = await asyncio.gather(*(exchanges[n].load_markets(reload=True) for n in nomes),
                                      return_exceptions=True)
    for nome, resultado in zip(nomes, resultados):
        if isinstance(resultado, Exception):
            registro.warning("%s: nao consegui recarregar a lista de mercados (%s: %s); sigo com a lista anterior",
                             nome, type(resultado).__name__, resultado)
    vencedores = {nome: coletor.simbolos_por_chave() for nome, coletor in coletores.items()}
    novos = simbolos_para_tickers(vencedores)
    for nome in nomes:
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


async def _recarregar_mercados_sempre(exchanges: dict, coletores: dict[str, ColetorCcxt], tickers: _Tickers,
                                      estado: EstadoMercados, intervalo_s: float,
                                      esperar: Callable[[float], Awaitable] = asyncio.sleep) -> None:
    """A cada `intervalo_s` (recarregar_mercados_s), recarrega a lista de mercados. Erro vai ao log e o laco segue."""
    while True:
        await esperar(intervalo_s)
        try:
            await _recarregar_mercados(exchanges, coletores, tickers, estado)
        except Exception:
            registro.exception("erro ao recarregar a lista de mercados; tento de novo em %ss", intervalo_s)


def _resumo_dos_descartes(coletores: dict[str, ColetorCcxt], intervalo_s: float) -> str:
    """Linha do log com os descartes de cada corretora desde o ultimo resumo (e zera os contadores)."""
    partes = []
    for nome, coletor in coletores.items():
        d = coletor.tirar_descartes()
        if any(d.values()):
            partes.append(f"{nome} livros {d['livros']}, tickers {d['tickers']}, "
                          f"sequencia quebrada {d['sequencia_quebrada']}")
    return f"descartes nos ultimos {intervalo_s:g}s: {'; '.join(partes) if partes else 'nenhum'}"


async def _registrar_recursos(motor: Motor, coletores: dict[str, ColetorCcxt], intervalo_s: float,
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


async def _ajustar_livros_da_corretora(coletor: ColetorCcxt, simbolos: set[str], com_traceback: set[str]) -> None:
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


def _iniciar_ajustes(coletores: dict[str, ColetorCcxt], selecao: dict[str, set[str]],
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


async def _laco_do_motor(motor: Motor, coletores: dict[str, ColetorCcxt], cfg: Config,
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


async def _encerrar(tarefas: list[asyncio.Task], coletores: dict[str, ColetorCcxt], exchanges: dict,
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
    criar_exchange: Callable[[str], object] = criar_exchange_ccxt,
    relogio: Callable[[], float] = time.time,
    ciclos: int | None = None,
) -> None:
    """Liga as pecas do scanner e roda ate ser cancelado (ou por `ciclos` ciclos).

    criar_exchange: monta a corretora do ccxt pelo nome (os testes passam corretoras falsas).
    relogio: hora atual em segundos (os testes passam um relogio fixo).
    Levanta ErroConfig se o bloco `painel` for inseguro (antes de conectar em qualquer corretora) e
    RuntimeError se nenhuma corretora carregar a lista de mercados.
    """
    estado = EstadoMercados(cfg.max_silencio_conexao_s, cfg.max_idade_livro_s, cfg.atraso_max_valido_s)
    dados_lentos = DadosLentos(cfg.taxas)
    exchanges: dict = {}
    coletores: dict[str, ColetorCcxt] = {}
    tickers = _Tickers(coletores)
    tarefas: list[asyncio.Task] = []
    banco: Banco | None = None
    try:
        for nome in dict.fromkeys(cfg.corretoras):  # sem nomes repetidos, na ordem da config
            exchanges[nome] = criar_exchange(nome)
        banco = Banco(cfg.banco)
        motor = Motor(cfg, estado, dados_lentos, banco=banco)
        app = criar_app(motor, banco, cfg, relogio=relogio)  # antes de conectar: painel mal configurado para tudo

        carregadas = await _carregar_mercados(exchanges)
        if not carregadas:
            raise RuntimeError("nenhuma corretora carregou a lista de mercados; confira a internet e o config.yaml")
        for nome in carregadas:
            coletores[nome] = ColetorCcxt(
                nome, exchanges[nome], estado=estado, relogio=relogio,
                timeout_desassinar_s=cfg.timeout_desassinar_s,
                espera_max_reconexao_s=cfg.espera_max_reconexao_s,
                limite_assinaturas=cfg.limites_assinatura.get(nome),
            )

        # Etapa 1: tickers so dos pares que formam alguma combinacao (spot x futuro ou futuro x futuro).
        simbolos = simbolos_para_tickers({nome: c.simbolos_por_chave() for nome, c in coletores.items()})
        for nome in coletores:
            if simbolos.get(nome):
                registro.info("%s: acompanhando %d pares", nome, len(simbolos[nome]))
                tickers.iniciar(nome, simbolos[nome])
            else:
                registro.warning("%s: nenhum par em comum com as outras corretoras; fica sem tickers", nome)

        das_carregadas = {nome: exchanges[nome] for nome in carregadas}
        tarefas.append(asyncio.create_task(
            dados_lentos.rodar(das_carregadas, cfg.intervalo_dados_lentos_s), name="dados lentos"))
        tarefas.append(asyncio.create_task(_servir_painel(app, cfg), name="painel"))
        tarefas.append(asyncio.create_task(
            _recarregar_mercados_sempre(das_carregadas, coletores, tickers, estado, cfg.recarregar_mercados_s),
            name="recarga dos mercados"))
        tarefas.append(asyncio.create_task(
            _registrar_recursos(motor, coletores, cfg.intervalo_log_recursos_s), name="recursos"))
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
