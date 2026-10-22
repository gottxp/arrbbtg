"""Log do scanner (especificacao, secao 8): terminal + um arquivo por dia, e a medida de memoria e CPU.

Fila: quem chama o log (o laco do asyncio: motor, coletores, painel) nunca espera o terminal nem o disco. Na
raiz do logging fica so um QueueHandler, que poe a linha numa fila e volta na hora; uma thread propria
(QueueListener, da biblioteca padrao) tira da fila e escreve no terminal e no arquivo. Sem isso, no Windows,
selecionar texto na janela do PowerShell ("Edicao Rapida") faz a escrita no terminal esperar o Enter, e o
scanner inteiro parava junto. Agora so a thread do log espera (o terminal congela, o scanner nao).

Arquivo: `<pasta_logs>/scanner-AAAA-MM-DD.log`, com a data LOCAL da maquina. O scanner grava direto no
arquivo do dia e, a meia-noite (horario local), passa a gravar no arquivo do dia novo. Nenhum arquivo
e renomeado: no Windows, renomear falha se outro programa estiver com o arquivo aberto (ex.: o dono
acompanhando o log no PowerShell ou num visualizador), e o log se perderia. Ficam o arquivo de hoje e
os `dias_logs` anteriores; os mais velhos sao apagados (um arquivo preso por outro programa fica para a
proxima virada, sem atrapalhar o log).

Horario de cada linha: o local da maquina, com o deslocamento do fuso (ex.: `2026-10-22 07:30:01,123 -0300`),
para nao haver duvida quando o log vier de uma VPS em outro fuso.

Memoria: o RSS do processo (memoria que ele ocupa agora), so com a biblioteca padrao. CPU: % de uso
medida pela diferenca do tempo de processador entre duas medidas.
"""

import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

registro = logging.getLogger("scanner.logs")

# Nome dos arquivos: um por dia, com a data local.
FORMATO_DO_NOME = "scanner-%Y-%m-%d.log"
_NOME_DE_ARQUIVO_DO_DIA = re.compile(r"scanner-\d{4}-\d{2}-\d{2}\.log")

# Formato de cada linha: horario local com fuso, nivel, de onde veio e a mensagem.
FORMATO = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def nome_do_arquivo(momento: float) -> str:
    """Nome do arquivo do dia (data local) do `momento` (segundos desde 1970), ex.: "scanner-2026-10-22.log"."""
    return time.strftime(FORMATO_DO_NOME, time.localtime(momento))


def _meia_noite(momento: float, dias_depois: int = 0) -> float:
    """Meia-noite local do dia do `momento` (ou `dias_depois` dias depois), em segundos desde 1970.

    O mktime acerta o horario de verao e a virada de mes/ano sozinho (dia 32 vira o dia 1 seguinte).
    """
    local = time.localtime(momento)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday + dias_depois, 0, 0, 0, 0, 0, -1))


class _FormatoLocal(logging.Formatter):
    """Horario local com o deslocamento do fuso: "2026-10-22 07:30:01,123 -0300"."""

    def formatTime(self, record, datefmt=None):
        momento = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone()  # no fuso local
        return f"{momento:%Y-%m-%d %H:%M:%S},{int(record.msecs):03d} {momento:%z}"


class ArquivoDoDia(logging.FileHandler):
    """Grava no arquivo do dia (`scanner-AAAA-MM-DD.log`, data local) e troca de arquivo a meia-noite local.

    Diferente do TimedRotatingFileHandler, nunca renomeia: na virada so fecha o arquivo do dia que
    acabou e abre (cria) o do dia novo. Ao ligar, abre o arquivo do dia de hoje (continua nele se ja
    existe). Depois de cada troca (e ao ligar) apaga os arquivos mais velhos, ficando o de hoje e os
    `dias` anteriores; arquivo que nao puder ser apagado (preso por outro programa) so gera um aviso.
    relogio: hora atual em segundos (os testes trocam por um relogio falso).
    """

    def __init__(self, pasta: str, dias: int, relogio: Callable[[], float] = time.time):
        self._pasta = pasta
        self._dias = dias
        self._relogio = relogio
        self._falhando = False  # a ultima linha nao foi gravada (o erro ja foi mostrado no terminal)
        agora = relogio()
        self._marcar_o_dia(agora)
        super().__init__(os.path.join(pasta, nome_do_arquivo(agora)), encoding="utf-8")
        self._apagar_os_antigos()

    def _marcar_o_dia(self, agora: float) -> None:
        self._comeco_do_dia = _meia_noite(agora)
        self._proxima_virada = _meia_noite(agora, dias_depois=1)
        if self._proxima_virada <= agora:
            # Protecao: num fuso cujo horario de verao comeca a meia-noite, a meia-noite de amanha nao existe,
            # e alguns sistemas (o C do Windows) a colocam ANTES de agora. Sem isto cada linha trocaria de
            # arquivo por ate 1 h. Confere de novo daqui a 1 h: o dia novo comeca dentro dessa hora.
            self._proxima_virada = agora + 3600

    def emit(self, record: logging.LogRecord) -> None:
        """Grava a linha no arquivo do dia, trocando de arquivo se o dia mudou. Nunca levanta erro.

        Se o arquivo nao abrir (pasta apagada, disco cheio, sem permissao), o erro aparece UMA vez no
        terminal (handleError, como os handlers da biblioteca fazem) e as linhas seguintes sao perdidas
        em silencio ate o arquivo abrir de novo: o FileHandler tenta abrir a cada linha, entao o log volta
        sozinho quando a pasta voltar. Quem chamou o log (ex.: o laco do motor) nunca recebe o erro.
        O dia e o da hora em que a linha foi escrita no log (marcada pela fila), e nao a da thread que grava.
        """
        try:
            agora = getattr(record, "momento_do_log", None)
            if agora is None:
                agora = self._relogio()
            if agora >= self._proxima_virada or agora < self._comeco_do_dia:  # outro dia (ou o relogio voltou)
                self._trocar_de_dia(agora)
            super().emit(record)  # FileHandler: abre o arquivo se ele estiver fechado, e grava
            self._falhando = False
        except Exception:
            if not self._falhando:
                self._falhando = True
                self.handleError(record)

    def _trocar_de_dia(self, agora: float) -> None:
        """Passa a gravar no arquivo do dia de `agora` (aberto no proximo registro). Mesmo dia: so remarca."""
        novo = os.path.abspath(os.path.join(self._pasta, nome_do_arquivo(agora)))
        self._marcar_o_dia(agora)  # antes de tudo: se fechar o arquivo velho falhar, nao tenta trocar de novo
        if novo == self.baseFilename:
            return
        self.baseFilename = novo
        antigo, self.stream = self.stream, None  # o FileHandler abre o arquivo novo ao gravar a linha
        try:
            if antigo is not None:
                antigo.flush()
                antigo.close()
        finally:
            self._apagar_os_antigos()

    def _apagar_os_antigos(self) -> None:
        """Apaga os arquivos de dias anteriores alem dos `dias` mais novos. Nunca levanta erro."""
        hoje = os.path.basename(self.baseFilename)
        try:
            nomes = os.listdir(self._pasta)
        except OSError:
            return
        # O nome tem a data no formato AAAA-MM-DD: a ordem alfabetica e a ordem dos dias.
        antigos = sorted(n for n in nomes if _NOME_DE_ARQUIVO_DO_DIA.fullmatch(n) and n < hoje)
        for nome in antigos[:max(0, len(antigos) - self._dias)]:
            try:
                os.remove(os.path.join(self._pasta, nome))
            except OSError as erro:  # ex.: arquivo aberto por outro programa no Windows
                # Nao usa o proprio log aqui (estamos dentro dele): avisa direto no terminal.
                sys.stderr.write(f"aviso do log: nao consegui apagar o log antigo {nome} ({erro}); "
                                 "tento de novo na proxima virada do dia\n")


class _FilaDoLog(logging.handlers.QueueHandler):
    """O handler da raiz: monta a linha (com o traceback, se houver) e poe na fila. Nunca espera disco nem terminal.

    Marca em cada linha a hora do `relogio` (momento_do_log): o arquivo do dia usa essa hora, mesmo que a thread
    do log so grave a linha um pouco depois (ex.: virada da meia-noite).
    """

    def __init__(self, fila: queue.SimpleQueue, relogio: Callable[[], float]):
        super().__init__(fila)
        self._relogio = relogio

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        preparado = super().prepare(record)
        preparado.momento_do_log = self._relogio()
        return preparado


class _Ouvinte(logging.handlers.QueueListener):
    """A thread do log: tira as linhas da fila e entrega ao terminal e ao arquivo. Tambem atende `descarregar_logs`."""

    def handle(self, record) -> None:
        if isinstance(record, threading.Event):  # pedido de descarregar_logs: tudo antes dele ja foi escrito
            for handler in self.handlers:
                handler.flush()
            record.set()
            return
        super().handle(record)


def _fora_do_terminal(record: logging.LogRecord) -> bool:
    """Filtro do terminal: a linha marcada com extra={"so_no_arquivo": True} vai so para o arquivo.

    Ex.: o traceback do erro inesperado que derruba o scanner (o main ja mostra a mensagem curta no terminal).
    """
    return not getattr(record, "so_no_arquivo", False)


# A fila e a thread do log ligadas agora (None = o log do scanner nao esta configurado).
_ouvinte: _Ouvinte | None = None
_fila_do_log: queue.SimpleQueue | None = None


def handlers_do_scanner() -> list[logging.Handler]:
    """Os handlers que escrevem de verdade (terminal e arquivo do dia), atras da fila. Vazio se o log esta desligado."""
    return list(_ouvinte.handlers) if _ouvinte is not None else []


def descarregar_logs(timeout: float = 5) -> bool:
    """Espera a thread do log escrever tudo o que ja estava na fila (e da flush). True se deu tempo.

    Para os testes e para quem vai ler o arquivo de log logo em seguida.
    """
    if _ouvinte is None or _fila_do_log is None:
        return True
    pronto = threading.Event()
    _fila_do_log.put(pronto)
    return pronto.wait(timeout)


def desligar_logs() -> None:
    """Para a thread do log (escrevendo antes tudo o que estava na fila), tira a fila da raiz e fecha os handlers.

    Usado ao reconfigurar, ao sair do scanner (o main chama) e nos testes.
    """
    global _ouvinte, _fila_do_log
    raiz = logging.getLogger()
    for handler in [h for h in raiz.handlers if getattr(h, "do_scanner", False)]:
        raiz.removeHandler(handler)
        handler.close()
    ouvinte, _ouvinte, _fila_do_log = _ouvinte, None, None
    if ouvinte is not None:
        ouvinte.stop()  # escreve o que ja estava na fila e termina a thread
        for handler in ouvinte.handlers:
            handler.close()


def configurar_logs(nivel: str, pasta: str, dias: int, relogio: Callable[[], float] = time.time) -> None:
    """Liga o log no terminal e no arquivo do dia, dentro de `pasta` (criada se nao existir), pela fila.

    nivel: "normal" (INFO do scanner) ou "detalhado" (tambem o DEBUG do scanner, ex.: cada descarte).
        As bibliotecas (ccxt, uvicorn, asyncio) so aparecem a partir de WARNING.
    dias: quantos arquivos de dias anteriores guardar, alem do de hoje (dias_logs da Config).
    Pode ser chamada de novo: troca os handlers antigos pelos novos, sem abrir o arquivo duas vezes.
    Se a pasta nao puder ser criada ou o arquivo nao abrir, segue so com o terminal e avisa.
    Na raiz fica so a fila (veja o topo do arquivo); terminal e arquivo ficam na thread do log.
    """
    global _ouvinte, _fila_do_log
    desligar_logs()
    raiz = logging.getLogger()
    raiz.setLevel(logging.WARNING)
    logging.getLogger("scanner").setLevel(logging.DEBUG if nivel == "detalhado" else logging.INFO)

    terminal = logging.StreamHandler()  # sys.stderr
    terminal.setFormatter(_FormatoLocal(FORMATO))
    terminal.do_scanner = True
    terminal.addFilter(_fora_do_terminal)
    handlers: list[logging.Handler] = [terminal]
    aviso = None
    try:
        os.makedirs(pasta, exist_ok=True)
        arquivo = ArquivoDoDia(pasta, dias, relogio=relogio)
    except OSError as erro:
        aviso = (pasta, erro)
    else:
        arquivo.setFormatter(_FormatoLocal(FORMATO))
        arquivo.do_scanner = True
        handlers.append(arquivo)

    _fila_do_log = queue.SimpleQueue()
    fila = _FilaDoLog(_fila_do_log, relogio)
    fila.do_scanner = True
    _ouvinte = _Ouvinte(_fila_do_log, *handlers, respect_handler_level=True)
    _ouvinte.start()
    raiz.addHandler(fila)
    if aviso is not None:
        registro.warning("sem arquivo de log: nao consegui usar a pasta '%s' (%s); o log fica so no terminal", *aviso)


# ---------------------------------------------------------------------------
# Memoria e CPU
# ---------------------------------------------------------------------------


def _memoria_linux() -> int | None:
    """RSS em bytes lido de /proc/self/statm (2o numero = paginas na memoria); None fora do Linux."""
    if not os.path.exists("/proc/self/statm"):
        return None
    with open("/proc/self/statm", encoding="ascii") as arquivo:
        paginas = int(arquivo.read().split()[1])
    return paginas * os.sysconf("SC_PAGE_SIZE")


def _memoria_windows() -> int | None:
    """RSS (WorkingSetSize) em bytes pela API do Windows (GetProcessMemoryInfo), com ctypes; None fora do Windows."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class ContadoresDeMemoria(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS da API do Windows
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32")
    # Tipos declarados: sem eles o ctypes passaria o "handle" do processo como int de 32 bits (errado no 64 bits).
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    obter = getattr(kernel32, "K32GetProcessMemoryInfo", None)  # Windows 7 ou mais novo
    if obter is None:
        obter = ctypes.WinDLL("psapi").GetProcessMemoryInfo
    obter.argtypes = [wintypes.HANDLE, ctypes.POINTER(ContadoresDeMemoria), wintypes.DWORD]
    obter.restype = wintypes.BOOL
    contadores = ContadoresDeMemoria()
    contadores.cb = ctypes.sizeof(contadores)
    if not obter(kernel32.GetCurrentProcess(), ctypes.byref(contadores), contadores.cb):
        return None
    return int(contadores.WorkingSetSize)


def _memoria_pico() -> int | None:
    """Ultimo recurso (ex.: macOS): o PICO de memoria do processo, pelo modulo `resource`; None se nao existir."""
    try:
        import resource
    except ImportError:  # Windows nao tem o modulo resource
        return None
    pico = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return pico if sys.platform == "darwin" else pico * 1024  # macOS informa em bytes; os outros, em KB


def memoria_do_processo() -> tuple[int, str] | None:
    """(bytes, "atual") com o RSS do processo; (bytes, "pico") se so o pico estiver disponivel; None se nada.

    Tenta, nesta ordem: Linux (/proc), Windows (ctypes) e o pico do modulo `resource`. Um erro numa
    das formas so faz passar para a proxima: medir memoria nunca derruba o scanner.
    """
    for medir, tipo in ((_memoria_linux, "atual"), (_memoria_windows, "atual"), (_memoria_pico, "pico")):
        try:
            valor = medir()
        except Exception:  # API do sistema indisponivel ou diferente do esperado
            valor = None
        if valor:
            return valor, tipo
    return None


class MedidorCpu:
    """% de CPU do processo entre uma medida e a anterior (a primeira conta desde a criacao do medidor).

    CPU = tempo de processador gasto (time.process_time, soma de todas as threads) / tempo de relogio
    que passou x 100. Com varias threads trabalhando ao mesmo tempo pode passar de 100%.
    relogio / relogio_cpu: trocados nos testes por relogios falsos.
    """

    def __init__(self, relogio: Callable[[], float] = time.monotonic,
                 relogio_cpu: Callable[[], float] = time.process_time):
        self._relogio = relogio
        self._relogio_cpu = relogio_cpu
        self._antes = (relogio(), relogio_cpu())

    def medir(self) -> float | None:
        """A % de CPU desde a medida anterior; None se nenhum tempo passou."""
        agora, cpu = self._relogio(), self._relogio_cpu()
        parede_antes, cpu_antes = self._antes
        self._antes = (agora, cpu)
        if agora <= parede_antes:
            return None
        return (cpu - cpu_antes) / (agora - parede_antes) * 100


class TemposDoCiclo:
    """Tempo de cada ciclo do motor (calculo + etapa 2), para a linha "recursos:" do log: media e o maior.

    O laco do motor chama `registrar` a cada ciclo; a linha de recursos chama `tirar`, que devolve
    (media, maior) em ms desde a linha anterior e zera (None se nenhum ciclo rodou).
    """

    def __init__(self):
        self._soma, self._quantos, self._maior = 0.0, 0, 0.0

    def registrar(self, ms: float) -> None:
        self._soma += ms
        self._quantos += 1
        self._maior = max(self._maior, ms)

    def tirar(self) -> tuple[float, float] | None:
        if not self._quantos:
            return None
        resultado = (self._soma / self._quantos, self._maior)
        self._soma, self._quantos, self._maior = 0.0, 0, 0.0
        return resultado


def linha_de_recursos(memoria: tuple[int, str] | None, cpu_pct: float | None, sinais_ativos: int,
                      historicos: int, ciclo: tuple[float, float] | None = None, banco_bytes: int | None = None,
                      fila_banco: int | None = None) -> str:
    """Texto da linha de recursos do log, ex.: "recursos: memoria 150.0 MB (atual), CPU 12.3%, sinais ativos 3, ..."

    historicos: quantas combinacoes o motor acompanha com amostras na memoria (cada uma guarda ate 4h).
    ciclo: (media, maior) do tempo do ciclo do motor em ms desde a linha anterior (TemposDoCiclo); perto de
        ciclo_ms (500) = o PC nao esta dando conta.
    banco_bytes: tamanho do banco no disco; fila_banco: itens esperando o escritor do banco (cresce = disco lento).
    """
    texto_memoria = "indisponivel" if memoria is None else f"{memoria[0] / (1024 * 1024):.1f} MB ({memoria[1]})"
    texto_cpu = "indisponivel" if cpu_pct is None else f"{cpu_pct:.1f}%"
    texto_ciclo = "-" if ciclo is None else f"medio {ciclo[0]:.1f} ms (maior {ciclo[1]:.1f} ms)"
    texto_banco = "indisponivel" if banco_bytes is None else f"{banco_bytes / (1024 * 1024):.1f} MB"
    linha = (f"recursos: memoria {texto_memoria}, CPU {texto_cpu}, sinais ativos {sinais_ativos}, "
             f"historicos {historicos}, ciclo {texto_ciclo}, banco {texto_banco}")
    if fila_banco is not None:
        linha += f", fila do banco {fila_banco}"
    return linha
