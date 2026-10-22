"""Log do scanner (especificacao, secao 8): terminal + um arquivo por dia, e a medida de memoria e CPU.

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
import os
import re
import sys
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
        agora = relogio()
        self._marcar_o_dia(agora)
        super().__init__(os.path.join(pasta, nome_do_arquivo(agora)), encoding="utf-8")
        self._apagar_os_antigos()

    def _marcar_o_dia(self, agora: float) -> None:
        self._comeco_do_dia = _meia_noite(agora)
        self._proxima_virada = _meia_noite(agora, dias_depois=1)

    def emit(self, record: logging.LogRecord) -> None:
        agora = self._relogio()
        if agora >= self._proxima_virada or agora < self._comeco_do_dia:  # outro dia (ou o relogio voltou)
            self._trocar_de_dia(agora)
        super().emit(record)

    def _trocar_de_dia(self, agora: float) -> None:
        """Fecha o arquivo do dia que acabou e passa a gravar no do dia novo (aberto no proximo emit)."""
        if self.stream is not None:
            self.stream.flush()
            self.stream.close()
            self.stream = None  # o FileHandler abre de novo, ja no arquivo novo, ao gravar a linha
        self.baseFilename = os.path.abspath(os.path.join(self._pasta, nome_do_arquivo(agora)))
        self._marcar_o_dia(agora)
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


def desligar_logs() -> None:
    """Tira (e fecha) os handlers que `configurar_logs` colocou. Usado ao reconfigurar e nos testes."""
    raiz = logging.getLogger()
    for handler in [h for h in raiz.handlers if getattr(h, "do_scanner", False)]:
        raiz.removeHandler(handler)
        handler.close()


def configurar_logs(nivel: str, pasta: str, dias: int, relogio: Callable[[], float] = time.time) -> None:
    """Liga o log no terminal e no arquivo do dia, dentro de `pasta` (criada se nao existir).

    nivel: "normal" (INFO do scanner) ou "detalhado" (tambem o DEBUG do scanner, ex.: cada descarte).
        As bibliotecas (ccxt, uvicorn, asyncio) so aparecem a partir de WARNING.
    dias: quantos arquivos de dias anteriores guardar, alem do de hoje (dias_logs da Config).
    Pode ser chamada de novo: troca os handlers antigos pelos novos, sem abrir o arquivo duas vezes.
    Se a pasta nao puder ser criada ou o arquivo nao abrir, segue so com o terminal e avisa.
    """
    desligar_logs()
    raiz = logging.getLogger()
    raiz.setLevel(logging.WARNING)
    logging.getLogger("scanner").setLevel(logging.DEBUG if nivel == "detalhado" else logging.INFO)

    terminal = logging.StreamHandler()  # sys.stderr
    terminal.setFormatter(_FormatoLocal(FORMATO))
    terminal.do_scanner = True
    raiz.addHandler(terminal)

    try:
        os.makedirs(pasta, exist_ok=True)
        arquivo = ArquivoDoDia(pasta, dias, relogio=relogio)
    except OSError as erro:
        registro.warning("sem arquivo de log: nao consegui usar a pasta '%s' (%s); o log fica so no terminal",
                         pasta, erro)
        return
    arquivo.setFormatter(_FormatoLocal(FORMATO))
    arquivo.do_scanner = True
    raiz.addHandler(arquivo)


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


def linha_de_recursos(memoria: tuple[int, str] | None, cpu_pct: float | None, sinais_ativos: int,
                      historicos: int) -> str:
    """Texto da linha de recursos do log, ex.: "recursos: memoria 150.0 MB (atual), CPU 12.3%, sinais ativos 3, ..."

    historicos: quantas combinacoes o motor acompanha com amostras na memoria (cada uma guarda ate 4h).
    """
    texto_memoria = "indisponivel" if memoria is None else f"{memoria[0] / (1024 * 1024):.1f} MB ({memoria[1]})"
    texto_cpu = "indisponivel" if cpu_pct is None else f"{cpu_pct:.1f}%"
    return (f"recursos: memoria {texto_memoria}, CPU {texto_cpu}, sinais ativos {sinais_ativos}, "
            f"historicos {historicos}")
