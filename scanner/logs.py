"""Log do scanner (especificacao, secao 8): terminal + um arquivo por dia, e a medida de memoria e CPU.

Arquivo: `<pasta_logs>/scanner.log` e o do dia; a meia-noite (UTC) ele vira `scanner-AAAA-MM-DD.log`
e um `scanner.log` novo comeca. Ficam os `dias_logs` arquivos mais novos; os mais velhos sao apagados.
A troca usa o `logging.handlers.TimedRotatingFileHandler` da biblioteca padrao: e o proprio handler que
fecha o arquivo antes de renomea-lo, entao funciona no Windows (onde nao se renomeia arquivo aberto),
desde que so UM scanner use a pasta de logs (dois processos no mesmo arquivo nao funcionam no Windows).

Horarios do log em UTC, no formato da spec (ex.: 2026-10-22T15:04:12.345Z), para bater com os horarios
dos sinais (detectado_em, encerrado_em).

Memoria: o RSS do processo (memoria que ele ocupa agora), so com a biblioteca padrao. CPU: % de uso
medida pela diferenca do tempo de processador entre duas medidas.
"""

import logging
import logging.handlers
import os
import sys
import time
from collections.abc import Callable

registro = logging.getLogger("scanner.logs")

# Nome do arquivo do dia, dentro da pasta de logs.
NOME_DO_ARQUIVO = "scanner.log"

# Formato de cada linha: horario UTC, nivel, de onde veio e a mensagem.
FORMATO = "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s: %(message)s"
FORMATO_DA_DATA = "%Y-%m-%dT%H:%M:%S"


def _nome_do_dia(nome_padrao: str) -> str:
    """Nome do arquivo de um dia que acabou: ".../scanner.log.2026-10-21" vira ".../scanner-2026-10-21.log".

    O TimedRotatingFileHandler chama esta funcao (namer) na virada do dia e tambem para achar os
    arquivos antigos que vai apagar; ".log" no fim deixa o arquivo abrir com duplo clique no Windows.
    """
    pasta, nome = os.path.split(nome_padrao)
    base, _, data = nome.rpartition(".")
    raiz, extensao = os.path.splitext(base)  # "scanner", ".log"
    return os.path.join(pasta, f"{raiz}-{data}{extensao or '.log'}")


def _formatador() -> logging.Formatter:
    formatador = logging.Formatter(FORMATO, datefmt=FORMATO_DA_DATA)
    formatador.converter = time.gmtime  # horario em UTC (o "Z" do formato)
    return formatador


def desligar_logs() -> None:
    """Tira (e fecha) os handlers que `configurar_logs` colocou. Usado ao reconfigurar e nos testes."""
    raiz = logging.getLogger()
    for handler in [h for h in raiz.handlers if getattr(h, "do_scanner", False)]:
        raiz.removeHandler(handler)
        handler.close()


def configurar_logs(nivel: str, pasta: str, dias: int) -> None:
    """Liga o log no terminal e no arquivo do dia, dentro de `pasta` (criada se nao existir).

    nivel: "normal" (INFO do scanner) ou "detalhado" (tambem o DEBUG do scanner, ex.: cada descarte).
        As bibliotecas (ccxt, uvicorn, asyncio) so aparecem a partir de WARNING.
    dias: quantos arquivos de dias anteriores guardar (dias_logs da Config).
    Pode ser chamada de novo: troca os handlers antigos pelos novos, sem abrir o arquivo duas vezes
    (no Windows, dois handlers no mesmo arquivo impediriam a troca do dia).
    Se a pasta nao puder ser criada ou o arquivo nao abrir, segue so com o terminal e avisa.
    """
    desligar_logs()
    raiz = logging.getLogger()
    raiz.setLevel(logging.WARNING)
    logging.getLogger("scanner").setLevel(logging.DEBUG if nivel == "detalhado" else logging.INFO)

    terminal = logging.StreamHandler()  # sys.stderr
    terminal.setFormatter(_formatador())
    terminal.do_scanner = True
    raiz.addHandler(terminal)

    try:
        os.makedirs(pasta, exist_ok=True)
        arquivo = logging.handlers.TimedRotatingFileHandler(
            os.path.join(pasta, NOME_DO_ARQUIVO), when="midnight", backupCount=dias, encoding="utf-8", utc=True,
        )
    except OSError as erro:
        registro.warning("sem arquivo de log: nao consegui usar a pasta '%s' (%s); o log fica so no terminal",
                         pasta, erro)
        return
    arquivo.namer = _nome_do_dia
    arquivo.setFormatter(_formatador())
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
