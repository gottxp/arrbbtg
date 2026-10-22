"""Ponto de entrada: python -m scanner [--config caminho] [--validar]

Sobe o scanner: le o config.yaml, conecta nas corretoras (so dados publicos), acompanha os
tickers de todos os pares, abre o livro dos pares interessantes e, a cada `ciclo_ms`, roda o
motor (candidatos -> filtros -> sinais -> banco). Junto sobe a API do painel (rotas /api e o
WebSocket /ws/sinais, que recebe cada evento do motor). A cada minuto o log mostra quantos
sinais estao ativos. Ctrl+C encerra fechando as conexoes, o painel e o banco.

Com --validar, so confere o config.yaml e sai (nao conecta em nada).
"""

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Callable

from scanner.api import conferir_painel, criar_app, servir_api
from scanner.armazenamento import Banco
from scanner.coletores.ccxt_coletor import ColetorCcxt, criar_exchange_ccxt
from scanner.coletores.dados_lentos import DadosLentos
from scanner.config.carregar import Config, ErroConfig, carregar_config
from scanner.estado import EstadoMercados
from scanner.motor import Motor, simbolos_para_tickers
from scanner.sinais import Evento

# Nome fixo (e nao __name__, que vira "__main__" com python -m): fica debaixo do logger "scanner".
registro = logging.getLogger("scanner.principal")

# De quanto em quanto tempo o log mostra a quantidade de sinais ativos (especificacao: a cada minuto).
INTERVALO_LOG_SINAIS_S = 60


def configurar_log(nivel: str) -> None:
    """Log no terminal. "detalhado" mostra tambem as mensagens de depuracao do scanner."""
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("scanner").setLevel(logging.DEBUG if nivel == "detalhado" else logging.INFO)


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
    """Etapa 1 de uma corretora. Se o stream parar com erro, registra e deixa as outras seguindo."""
    try:
        await coletor.iniciar_tickers(simbolos)
    except Exception as erro:  # cancelamento (Ctrl+C) nao entra aqui
        registro.error("%s: os tickers pararam (%s: %s); a corretora fica sem sinais ate reiniciar o scanner",
                       coletor.nome, type(erro).__name__, erro)


async def _ajustar_livros(coletores: dict[str, ColetorCcxt], selecao: dict[str, set[str]]) -> None:
    """Etapa 2 de todas as corretoras: abre e cancela livros conforme a selecao do motor.

    Todas recebem a lista (vazia tambem), para cancelar os livros que sairam. Erro de uma
    corretora vai para o log e nao atrapalha as outras.
    """
    nomes = list(coletores)
    resultados = await asyncio.gather(
        *(coletores[n].assinar_livros(selecao.get(n, set())) for n in nomes), return_exceptions=True
    )
    for nome, resultado in zip(nomes, resultados):
        if isinstance(resultado, Exception):
            registro.error("%s: erro ao ajustar os livros (%s: %s); tentando de novo no proximo ciclo",
                           nome, type(resultado).__name__, resultado)


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

    O ajuste dos livros roda numa tarefa propria, fora do ritmo do motor: se a corretora demorar
    para responder, o motor continua rodando e o proximo ajuste so comeca quando o anterior acabar.
    ciclos: quantos ciclos rodar (os testes usam 2); None = ate ser cancelado (Ctrl+C).
    publicar: recebe os eventos de cada ciclo (o `Transmissor` da API); o motor nunca espera por ele.
    Um erro num ciclo vai para o log e o proximo ciclo tenta de novo.
    """
    intervalo_s = cfg.ciclo_ms / 1000
    relogio_do_laco = asyncio.get_running_loop().time  # relogio que nao pula (so para marcar o ritmo)
    proximo = relogio_do_laco() + intervalo_s  # espera um ciclo antes do primeiro: da tempo dos tickers chegarem
    ultimo_log: float | None = None
    ajuste_livros: asyncio.Task | None = None  # ajuste de livros em andamento (fora do ritmo do motor)
    feitos = 0
    try:
        while ciclos is None or feitos < ciclos:
            await asyncio.sleep(max(0.0, proximo - relogio_do_laco()))
            # Ritmo fixo de ciclo_ms, descontando o tempo gasto no ciclo; se atrasar, segue sem "recuperar" ciclos.
            proximo = max(proximo + intervalo_s, relogio_do_laco())
            agora = relogio()
            try:
                _transmitir(publicar, motor.ciclo(agora))
                selecao = motor.selecao_livros(agora)
            except Exception:
                registro.exception("erro no ciclo do motor; tentando de novo no proximo ciclo")
            else:
                if ajuste_livros is None or ajuste_livros.done():
                    ajuste_livros = asyncio.create_task(_ajustar_livros(coletores, selecao), name="ajuste de livros")
                else:
                    registro.debug("o ajuste de livros anterior ainda nao terminou; este ciclo nao abre outro")
            feitos += 1
            if ultimo_log is None or agora - ultimo_log >= INTERVALO_LOG_SINAIS_S:
                ultimo_log = agora
                registro.info("sinais ativos: %d", len(motor.sinais_ativos()))
    finally:
        if ajuste_livros is not None:
            ajuste_livros.cancel()
            await asyncio.gather(ajuste_livros, return_exceptions=True)


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
    estado = EstadoMercados(cfg.max_silencio_conexao_s, cfg.max_idade_livro_s)
    dados_lentos = DadosLentos(cfg.taxas)
    exchanges: dict = {}
    coletores: dict[str, ColetorCcxt] = {}
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
        coletores = {
            nome: ColetorCcxt(nome, exchanges[nome], estado=estado, relogio=relogio,
                              timeout_desassinar_s=cfg.timeout_desassinar_s)
            for nome in carregadas
        }

        # Etapa 1: tickers so dos pares que formam alguma combinacao spot x futuro.
        simbolos = simbolos_para_tickers({nome: c.simbolos_por_chave() for nome, c in coletores.items()})
        for nome, coletor in coletores.items():
            if simbolos.get(nome):
                registro.info("%s: acompanhando %d pares", nome, len(simbolos[nome]))
                tarefas.append(asyncio.create_task(_acompanhar_tickers(coletor, simbolos[nome]), name=f"tickers {nome}"))
            else:
                registro.warning("%s: nenhum par em comum com as outras corretoras; fica sem tickers", nome)

        tarefas.append(asyncio.create_task(
            dados_lentos.rodar({nome: exchanges[nome] for nome in carregadas}, cfg.intervalo_dados_lentos_s),
            name="dados lentos",
        ))
        tarefas.append(asyncio.create_task(_servir_painel(app, cfg), name="painel"))
        await _laco_do_motor(motor, coletores, cfg, relogio, ciclos, publicar=app.state.transmissor.publicar)
    finally:
        await _encerrar(tarefas, coletores, exchanges, banco)


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

    configurar_log(cfg.nivel_log)
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
