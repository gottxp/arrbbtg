"""Banco de dados SQLite do scanner (especificacao, secoes de armazenamento e do painel).

Um arquivo so guarda seis tabelas:

- `sinais`: o historico de oportunidades. Um registro por sinal, aberto quando o sinal nasce
  (`encerrado_em` vazio) e fechado quando ele some. O sinal completo (formato JSON da spec,
  secao 5) fica guardado como texto em `sinal_json`; as outras colunas existem para buscar rapido.
- `combos` e `amostras`: a cada ~5 s, os valores de um par monitorado (spread de abertura `a`, de
  fechamento `f`, precos e lotes). Alimentam o grafico de historico e as janelas do motor ao ligar, e sao
  apagadas depois de alguns dias (`limpar_antigos`). Formato compacto: cada combinacao (a chave em texto,
  ex.: "BTC/USDT|spot_futuro|gate|spot|bitget|futuro") ganha um numero na tabela `combos`, e cada amostra
  guarda so esse numero. A tabela e WITHOUT ROWID com a chave (combo_id, ts): as amostras de um par ficam
  juntas no disco, entao o grafico de um par le poucas paginas seguidas.
- `grupos`: os grupos de filtros salvos pelo usuario (ex.: "Farm"), como JSON.
- `corretoras_dia`: por dia (UTC) e corretora, as reconexoes do dia e o atraso medio das leituras do dia
  (metricas de validacao, especificacao secao 9; veja `registrar_corretoras`).
- `sessao`: a ultima sessao do painel (modo, filtros e modo comparacao), para o scanner religar nela.

Banco do formato antigo (amostras com a chave em texto em cada linha, antes da revisao final): convertido
uma vez so, ao abrir, com aviso no log (veja `_converter_formato_antigo`).

Tempo: `ts` das amostras e `agora` sao segundos desde 1970. As datas dos sinais sao texto ISO 8601
em UTC terminando em `Z` (ex.: "2026-10-20T15:04:12.345Z"), como no JSON da spec; nesse formato
a ordem alfabetica e a ordem do tempo, por isso comparar os textos com < e >= funciona.

Uma unica conexao por `Banco`, em modo WAL (o motor grava enquanto a API le). A conexao pode ser
usada por mais de uma thread: um `Lock` faz uma operacao esperar a outra terminar.

Desempenho: no scanner, quem grava e a thread do EscritorBanco (escritor.py), nunca o laco do asyncio: o
motor so poe eventos e amostras na fila dele. Cada lote e uma transacao, no modo WAL com `synchronous=NORMAL`,
cache de CACHE_KIB e o checkpoint do WAL feito pela thread do escritor (veja `__init__` e `checkpoint`).
"""

import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta, timezone

from scanner.sinais import Evento

registro = logging.getLogger("scanner.banco")

# Detalhes tecnicos do SQLite (nao sao ajustes do dono):
# - cache de paginas em memoria, em KiB (o padrao do SQLite e 2 MiB): as ultimas paginas de cada combinacao
#   ficam na memoria e o lote de amostras nao precisa ler o disco;
# - o checkpoint automatico (copiar o WAL para o arquivo do banco) so depois de PAGINAS_AUTOCHECKPOINT paginas
#   (~40 MB); quem faz o checkpoint de verdade, de tempos em tempos, e a thread do escritor (veja checkpoint);
# - o arquivo -wal volta a no maximo LIMITE_WAL_BYTES depois de cada checkpoint completo.
CACHE_KIB = 65536
PAGINAS_AUTOCHECKPOINT = 10000
LIMITE_WAL_BYTES = 64 * 1024 * 1024

# Criado ao abrir o arquivo. IF NOT EXISTS: abrir um banco que ja existe nao apaga nada.
_ESQUEMA = """
CREATE TABLE IF NOT EXISTS sinais (
    id             TEXT PRIMARY KEY,
    modo           TEXT NOT NULL,
    par            TEXT NOT NULL,
    tipo           TEXT NOT NULL,   -- spot_futuro ou futuro_futuro (o tipo do sinal, nao do evento)
    detectado_em   TEXT NOT NULL,   -- ISO 8601 UTC
    encerrado_em   TEXT,            -- vazio enquanto o sinal esta aberto
    rp_liquido_pct REAL,
    sinal_json     TEXT NOT NULL,
    comparacao     INTEGER NOT NULL DEFAULT 0, -- 1 = nasceu com o modo comparacao ligado (decidido pelo RP bruto)
    visto_em       TEXT             -- ultima vez em que o sinal ativo foi visto (gravada de tempos em tempos)
);
CREATE INDEX IF NOT EXISTS idx_sinais_detectado_em ON sinais (detectado_em);

CREATE TABLE IF NOT EXISTS combos (
    id    INTEGER PRIMARY KEY,      -- o numero que as amostras guardam
    chave TEXT NOT NULL UNIQUE      -- ex.: "BTC/USDT|spot_futuro|gate|spot|bitget|futuro"
);

CREATE TABLE IF NOT EXISTS amostras (
    combo_id    INTEGER NOT NULL,   -- o numero da combinacao (tabela combos)
    ts          REAL NOT NULL,      -- segundos desde 1970
    a           REAL,               -- spread de abertura
    f           REAL,               -- spread de fechamento
    preco_long  REAL,
    preco_short REAL,
    lote_long   REAL,
    lote_short  REAL,
    PRIMARY KEY (combo_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS grupos (
    nome         TEXT PRIMARY KEY,
    filtros_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corretoras_dia (
    dia             TEXT NOT NULL,     -- AAAA-MM-DD, dia UTC
    corretora       TEXT NOT NULL,
    reconexoes      INTEGER NOT NULL,  -- reconexoes no dia, somadas leitura a leitura
    soma_atraso_ms  REAL NOT NULL,     -- soma das leituras do atraso medio (para a media do dia)
    leituras_atraso INTEGER NOT NULL,  -- quantas leituras de atraso entraram na soma
    PRIMARY KEY (dia, corretora)
);

CREATE TABLE IF NOT EXISTS sessao (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- uma linha so: a ultima sessao
    sessao_json TEXT NOT NULL                        -- {"modo", "filtros", "comparacao"}
);
"""

# Soma uma leitura ao dia da corretora (cria a linha na primeira leitura do dia).
_SOMAR_CORRETORA = """
INSERT INTO corretoras_dia (dia, corretora, reconexoes, soma_atraso_ms, leituras_atraso)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (dia, corretora) DO UPDATE SET
    reconexoes = reconexoes + excluded.reconexoes,
    soma_atraso_ms = soma_atraso_ms + excluded.soma_atraso_ms,
    leituras_atraso = leituras_atraso + excluded.leituras_atraso
"""

# A limpeza apaga em lotes de no maximo LOTE_LIMPEZA amostras de uma combinacao, um lote por transacao, soltando a
# trava entre um lote e outro: o escritor (e a API) esperam no maximo um lote (milissegundos), e nao a limpeza
# inteira (segundos com milhoes de amostras). Detalhe tecnico, nao um ajuste do dono.
LOTE_LIMPEZA = 2000
# Pausa entre um lote e outro, sem a trava, para quem estiver esperando por ela entrar (veja _apagar_em_lotes).
PAUSA_ENTRE_LOTES_S = 0.005


# Insere o sinal; se o id ja existe, atualiza tudo menos `detectado_em` e `comparacao` (os dois sao do nascimento).
_GRAVAR_SINAL = """
INSERT INTO sinais (id, modo, par, tipo, detectado_em, encerrado_em, rp_liquido_pct, sinal_json, comparacao)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET
    modo = excluded.modo,
    par = excluded.par,
    tipo = excluded.tipo,
    encerrado_em = excluded.encerrado_em,
    rp_liquido_pct = excluded.rp_liquido_pct,
    sinal_json = excluded.sinal_json
"""


# Campos de texto que a tabela `sinais` exige (NOT NULL): precisam ser texto nao vazio.
_CAMPOS_DE_TEXTO_OBRIGATORIOS = ("id", "modo", "par", "tipo", "detectado_em")

# Insere uma amostra (combinacao ja numerada). A mesma combinacao no mesmo instante substitui a anterior.
_GRAVAR_AMOSTRA = (
    "INSERT OR REPLACE INTO amostras (combo_id, ts, a, f, preco_long, preco_short, lote_long, lote_short) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


# Erros que o SQLite da para UMA linha com dado que ele nao aceita (NOT NULL vazio, tipo que nao sabe
# gravar, numero de valores errado). Erro do banco em si (disco cheio, arquivo travado) nao entra aqui.
_ERROS_DE_UMA_LINHA = (sqlite3.IntegrityError, sqlite3.InterfaceError, sqlite3.ProgrammingError)


def _json(valor) -> str:
    """Dict -> texto JSON. NaN e infinito sao recusados (ValueError): o JSON da spec nao os aceita."""
    return json.dumps(valor, allow_nan=False)


_EPOCA = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _iso_utc(segundos: float) -> str:
    """Segundos desde 1970 -> "2026-10-20T15:04:12.345Z" (mesmo formato das datas dos sinais)."""
    # 1970 + segundos (e nao datetime.fromtimestamp, que no Windows recusa horario antes de 1970: o limite da
    # limpeza fica antes de 1970 com o relogio falso dos testes).
    momento = _EPOCA + timedelta(seconds=segundos)
    return momento.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _dia(dia: str) -> str:
    """Confere o dia "AAAA-MM-DD" e devolve sempre nesse formato ("20261020" vira "2026-10-20"). ValueError se invalido."""
    return date.fromisoformat(dia).isoformat()


def _numero_real(valor) -> bool:
    """True para int ou float de verdade (nem bool, nem NaN, nem infinito)."""
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


class Banco:
    """Abre (ou cria) o arquivo SQLite em `caminho` e oferece as operacoes que o scanner precisa.

    periodo_max_s: `amostras` nunca devolve amostras mais velhas que isto (em segundos, contados do `relogio`).
        O __main__ passa a retencao (retencao_dias): o grafico do painel (/api/historico) nunca le alem dela,
        por maior que seja o periodo pedido. None = sem limite (testes e ferramentas).
    relogio: hora atual em segundos desde 1970 (so para o periodo_max_s).
    """

    def __init__(self, caminho: str, periodo_max_s: float | None = None, relogio: Callable[[], float] = time.time):
        self._caminho = caminho
        self._periodo_max_s = periodo_max_s
        self._relogio = relogio
        # check_same_thread=False: o escritor, a API e a limpeza ficam em threads diferentes.
        self._conn = sqlite3.connect(caminho, check_same_thread=False)
        self._fechado = False  # `fechar` ja foi chamado (a limpeza em outra thread para no proximo lote)
        # Ultimo valor do contador de reconexoes de cada corretora gravado por ESTE Banco (esta execucao do
        # scanner). Comeca vazio = zero, como o contador do scanner que acabou de ligar (veja registrar_corretoras).
        self._contador_gravado: dict[str, int] = {}
        # Numero de cada combinacao ja gravada (chave em texto -> combos.id), para nao perguntar ao banco a cada lote.
        self._ids_combos: dict[str, int] = {}
        try:
            self._conn.row_factory = sqlite3.Row  # linhas acessiveis pelo nome da coluna
            self._trava = threading.Lock()
            modo = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(modo).lower() == "wal":
                # synchronous=NORMAL: no modo WAL o SQLite so espera o disco gravar de verdade (fsync) de vez
                # em quando, e nao a cada transacao. O arquivo nunca corrompe; numa queda de energia, as
                # ultimas transacoes (alguns segundos de amostras e eventos) podem se perder, o que e
                # aceitavel para um historico. Fora do modo WAL (ex.: pasta de rede) fica o padrao (FULL),
                # porque ai o NORMAL poderia corromper o arquivo numa queda de energia.
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute(f"PRAGMA wal_autocheckpoint={PAGINAS_AUTOCHECKPOINT}")
                self._conn.execute(f"PRAGMA journal_size_limit={LIMITE_WAL_BYTES}")
            self._conn.execute(f"PRAGMA cache_size=-{CACHE_KIB}")  # negativo = em KiB
            self._converter_formato_antigo()
            with self._conn:
                self._conn.executescript(_ESQUEMA)
        except BaseException:
            self._conn.close()  # deu erro preparando o banco: nao deixa a conexao (e o arquivo) aberta
            raise

    def _converter_formato_antigo(self) -> None:
        """Banco de antes da revisao final: converte para o formato de agora, uma vez so (com aviso no log).

        - amostras com a chave em texto em cada linha (~209 bytes por amostra) viram o formato compacto
          (combos + amostras WITHOUT ROWID);
        - a tabela sinais ganha a coluna `comparacao` (os registros antigos ficam 0: nao se sabe).
        Tudo numa transacao: se der erro no meio, o banco fica como estava. O arquivo nao diminui na hora:
        o espaco das linhas antigas e reaproveitado pelas amostras novas.
        """
        colunas_amostras = {linha[1] for linha in self._conn.execute("PRAGMA table_info(amostras)")}
        colunas_sinais = {linha[1] for linha in self._conn.execute("PRAGMA table_info(sinais)")}
        if "chave" in colunas_amostras:
            total = self._conn.execute("SELECT count(*) FROM amostras").fetchone()[0]
            registro.warning("banco no formato antigo: convertendo %d amostras para o formato compacto (so desta vez; "
                             "com muitas amostras leva alguns minutos, espere)", total)
            inicio = time.monotonic()
            with self._conn:
                self._conn.execute("BEGIN")
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS combos (id INTEGER PRIMARY KEY, chave TEXT NOT NULL UNIQUE)")
                self._conn.execute(
                    "CREATE TABLE amostras_nova (combo_id INTEGER NOT NULL, ts REAL NOT NULL, a REAL, f REAL, "
                    "preco_long REAL, preco_short REAL, lote_long REAL, lote_short REAL, "
                    "PRIMARY KEY (combo_id, ts)) WITHOUT ROWID")
                # Numeros na ordem alfabetica das chaves: a copia abaixo (na mesma ordem) so acrescenta no fim.
                self._conn.execute(
                    "INSERT OR IGNORE INTO combos (chave) SELECT DISTINCT chave FROM amostras ORDER BY chave")
                self._conn.execute(
                    "INSERT OR REPLACE INTO amostras_nova (combo_id, ts, a, f, preco_long, preco_short, lote_long, "
                    "lote_short) SELECT c.id, x.ts, x.a, x.f, x.preco_long, x.preco_short, x.lote_long, x.lote_short "
                    "FROM amostras AS x JOIN combos AS c ON c.chave = x.chave ORDER BY x.chave, x.ts")
                self._conn.execute("DROP TABLE amostras")  # leva junto o indice antigo (chave, ts)
                self._conn.execute("ALTER TABLE amostras_nova RENAME TO amostras")
            registro.info("conversao do banco terminada em %.1f s: %d amostras no formato compacto",
                          time.monotonic() - inicio, total)
        if colunas_sinais and "comparacao" not in colunas_sinais:
            with self._conn:
                self._conn.execute("ALTER TABLE sinais ADD COLUMN comparacao INTEGER NOT NULL DEFAULT 0")
            registro.info("banco: a tabela de sinais ganhou a coluna comparacao (os registros antigos ficam sem a "
                          "marca)")
        if colunas_sinais and "visto_em" not in colunas_sinais:
            with self._conn:
                self._conn.execute("ALTER TABLE sinais ADD COLUMN visto_em TEXT")

    def fechar(self) -> None:
        """Fecha a conexao. Depois disso o `Banco` nao pode mais ser usado."""
        with self._trava:
            self._fechado = True
            self._conn.close()

    def tamanho_bytes(self) -> int | None:
        """Tamanho do banco no disco (o arquivo mais o -wal), em bytes; None se nao der para ler (linha de recursos)."""
        try:
            total = os.path.getsize(self._caminho)
            if os.path.exists(self._caminho + "-wal"):
                total += os.path.getsize(self._caminho + "-wal")
        except OSError:
            return None
        return total

    def checkpoint(self) -> None:
        """Copia o WAL para o arquivo do banco sem esperar ninguem (checkpoint PASSIVE); quem chama e o escritor."""
        with self._trava:
            if not self._fechado:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()

    # --- Sinais ----------------------------------------------------------------

    def gravar_evento(self, e: Evento) -> None:
        """Atualiza o historico conforme o evento do gerenciador de sinais.

        - "novo" com `registrar_historico=True`: insere o registro (aberto).
        - "novo" com `registrar_historico=False`: o sinal voltou logo apos sumir (anti-repeticao),
          entao o registro desse id ja existe: reabre (`encerrado_em` volta a ficar vazio) e
          renova o JSON guardado. Se por algum motivo o registro nao existir, insere.
        - "encerrado": fecha o registro com o mesmo id (`encerrado_em` e JSON final). Se nao
          houver registro, insere ja fechado.
        - "atualizado": nao grava nada (so aparece na tabela ao vivo).

        Sinal com JSON invalido (ex.: NaN) levanta ValueError e nada e gravado. O motor usa
        `gravar_eventos`, que grava os eventos de um ciclo de uma vez.
        """
        recusados = self.gravar_eventos([e])
        if recusados:
            raise recusados[0][1]

    def gravar_eventos(self, eventos: list[Evento]) -> list[tuple[Evento, Exception]]:
        """Grava os eventos de um ciclo numa transacao so (um acesso ao disco por ciclo, e nao um por evento).

        Cada evento segue as regras de `gravar_evento`. Um evento ruim nunca leva os outros junto:
        - antes de tocar no banco, cada evento e conferido (`_linha_do_evento`): id, modo, par, tipo e
          detectado_em texto nao vazio, rp_liquido_pct numero real ou vazio, sinal que vira JSON valido.
          O que nao passa fica de fora;
        - se mesmo assim o SQLite recusar uma linha no meio do lote (IntegrityError, InterfaceError,
          ProgrammingError), o lote e refeito linha a linha (SAVEPOINT por evento, ainda uma transacao
          so) e so a linha recusada fica de fora.
        Devolve os recusados, cada um com o erro, para quem chamou registrar no log qual era. Erro do
        proprio banco (disco cheio, arquivo travado...) levanta a excecao e nada do lote e gravado.
        """
        linhas, recusados = [], []  # linhas: (evento, valores da linha)
        for e in eventos:
            if e.tipo == "atualizado":
                continue
            try:
                linhas.append((e, self._linha_do_evento(e)))  # antes de tocar no banco
            except (ValueError, TypeError, KeyError) as erro:
                recusados.append((e, erro))
        if not linhas:
            return recusados
        try:
            with self._trava, self._conn:
                self._conn.executemany(_GRAVAR_SINAL, [linha for _, linha in linhas])
        except _ERROS_DE_UMA_LINHA:  # alguma linha passou da conferencia mas o SQLite recusou: uma por uma
            recusados += self._gravar_uma_por_uma(linhas)
        return recusados

    def _gravar_uma_por_uma(self, linhas: list[tuple[Evento, tuple]]) -> list[tuple[Evento, Exception]]:
        """Grava as linhas numa transacao so, com um SAVEPOINT por linha: a recusada volta atras sozinha."""
        recusados = []
        with self._trava, self._conn:
            self._conn.execute("BEGIN")  # uma transacao para todas; o `with` faz o COMMIT no fim
            for evento, linha in linhas:
                self._conn.execute("SAVEPOINT evento")
                try:
                    self._conn.execute(_GRAVAR_SINAL, linha)
                except _ERROS_DE_UMA_LINHA as erro:
                    self._conn.execute("ROLLBACK TO evento")  # desfaz so esta linha
                    recusados.append((evento, erro))
                self._conn.execute("RELEASE evento")
        return recusados

    @staticmethod
    def _linha_do_evento(e: Evento) -> tuple:
        """Os valores da linha da tabela `sinais` para o evento (na ordem de _GRAVAR_SINAL).

        Levanta ValueError (dizendo qual campo) se o evento nao pode ser gravado.
        """
        sinal = e.sinal
        if not isinstance(sinal, dict):
            raise ValueError(f"o sinal do evento nao e um dict: {sinal!r}")
        for campo in _CAMPOS_DE_TEXTO_OBRIGATORIOS:
            valor = sinal.get(campo)
            if not isinstance(valor, str) or not valor:
                raise ValueError(f"campo {campo} vazio ou que nao e texto: {valor!r}")
        rp = sinal.get("rp_liquido_pct")
        numero = isinstance(rp, (int, float)) and not isinstance(rp, bool)
        if rp is not None and not (numero and math.isfinite(rp)):
            raise ValueError(f"campo rp_liquido_pct nao e um numero real: {rp!r}")
        encerrado_em = sinal.get("encerrado_em") if e.tipo == "encerrado" else None
        if encerrado_em is not None and not isinstance(encerrado_em, str):
            raise ValueError(f"campo encerrado_em nao e texto: {encerrado_em!r}")
        texto = _json(sinal)
        comparacao = 1 if getattr(e, "comparacao", False) else 0
        return (sinal["id"], sinal["modo"], sinal["par"], sinal["tipo"], sinal["detectado_em"],
                encerrado_em, rp, texto, comparacao)

    def sinais_do_dia(self, dia: str) -> list[dict]:
        """Os sinais detectados no dia UTC `dia` ("AAAA-MM-DD"), do mais antigo ao mais novo.

        Cada item tem: id, modo, par, tipo, detectado_em, encerrado_em, rp_liquido_pct, comparacao (True se o
        sinal nasceu com o modo comparacao ligado) e `sinal` (o JSON completo, ja como dict). Levanta ValueError
        se `dia` nao for uma data valida.
        """
        inicio = date.fromisoformat(dia)
        # Qualquer "2026-10-20T..." e maior que "2026-10-20" e menor que "2026-10-21":
        # comparar com as datas puras pega exatamente o dia inteiro.
        de, ate = inicio.isoformat(), (inicio + timedelta(days=1)).isoformat()
        with self._trava:
            linhas = self._conn.execute(
                """
                SELECT id, modo, par, tipo, detectado_em, encerrado_em, rp_liquido_pct, sinal_json, comparacao
                FROM sinais
                WHERE detectado_em >= ? AND detectado_em < ?
                ORDER BY detectado_em, id
                """,
                (de, ate),
            ).fetchall()
        return [
            {
                "id": linha["id"], "modo": linha["modo"], "par": linha["par"], "tipo": linha["tipo"],
                "detectado_em": linha["detectado_em"], "encerrado_em": linha["encerrado_em"],
                "rp_liquido_pct": linha["rp_liquido_pct"], "comparacao": bool(linha["comparacao"]),
                "sinal": json.loads(linha["sinal_json"]),
            }
            for linha in linhas
        ]

    def fechar_abertos(self) -> int:
        """Fecha os registros que ficaram abertos de uma execucao anterior (o scanner caiu). Devolve quantos.

        O __main__ chama ao ligar, antes de o motor comecar. Cada registro aberto recebe como `encerrado_em`
        (na coluna e no JSON) a ultima vez em que o SINAL foi visto: a mais nova entre o `atualizado_em` do JSON e
        o `visto_em` (que o motor grava de tempos em tempos enquanto o sinal esta ativo, veja marcar_vistos). Nao
        usa as amostras da combinacao: ela continua sendo amostrada enquanto tem livro, mesmo sem sinal. Sem isso
        o registro contaria para sempre como aberto no resumo do dia.
        """
        with self._trava:
            linhas = self._conn.execute(
                "SELECT id, sinal_json, visto_em FROM sinais WHERE encerrado_em IS NULL").fetchall()
            fechados = []
            for linha in linhas:
                sinal = json.loads(linha["sinal_json"])
                fim = self._ultima_vez_visto(sinal, linha["visto_em"])
                sinal["encerrado_em"] = fim
                fechados.append((fim, _json(sinal), linha["id"]))
            if fechados:
                with self._conn:
                    self._conn.executemany("UPDATE sinais SET encerrado_em = ?, sinal_json = ? WHERE id = ?", fechados)
        return len(fechados)

    def _ultima_vez_visto(self, sinal: dict, visto_em: str | None) -> str:
        """A ultima hora (ISO) em que o sinal foi visto ativo (veja fechar_abertos)."""
        candidatas = [sinal.get("detectado_em"), sinal.get("atualizado_em"), visto_em]
        candidatas = [hora for hora in candidatas if isinstance(hora, str) and hora]
        if not candidatas:  # registro sem data nenhuma (nao acontece com o motor): fecha agora
            candidatas.append(_iso_utc(self._relogio()))
        return max(candidatas)  # mesmo formato ISO: a ordem alfabetica e a ordem do tempo

    def marcar_vistos(self, vistos: list[tuple[str, str]]) -> None:
        """Anota, para cada (id, hora ISO), a ultima vez em que o sinal ativo foi visto (coluna visto_em).

        O motor manda de tempos em tempos (a cada amostra_s) pelo escritor. So muda registro ainda aberto e so para
        uma hora mais nova. Serve ao fechar_abertos depois de uma queda.
        """
        if not vistos:
            return
        with self._trava, self._conn:
            self._conn.executemany(
                "UPDATE sinais SET visto_em = ? WHERE id = ? AND encerrado_em IS NULL "
                "AND (visto_em IS NULL OR visto_em < ?)",
                [(hora, id_, hora) for id_, hora in vistos],
            )

    # --- Amostras ---------------------------------------------------------------

    def gravar_amostra(self, chave: str, ts: float, a: float, f: float,
                       preco_long: float, preco_short: float, lote_long: float, lote_short: float) -> None:
        """Grava uma amostra do par `chave` no instante `ts` (segundos desde 1970)."""
        self.gravar_amostras([(chave, ts, a, f, preco_long, preco_short, lote_long, lote_short)])

    def gravar_amostras(self, amostras: list[tuple]) -> None:
        """Grava varias amostras numa transacao so: um acesso ao disco por lote, e nao um por amostra.

        Cada item e (chave, ts, a, f, preco_long, preco_short, lote_long, lote_short), na ordem de
        `gravar_amostra`. A chave em texto vira o numero da combinacao (tabela combos; combinacao nova ganha um
        numero na hora). Lista vazia nao faz nada. Se o lote falhar, nada dele fica gravado.
        """
        if not amostras:
            return
        with self._trava:
            novos: dict[str, int] = {}  # numeros novos: so vao para a memoria se o lote der certo
            with self._conn:
                ids = self._numerar(amostras, novos)
                self._conn.executemany(_GRAVAR_AMOSTRA, [(ids[chave], *resto) for chave, *resto in amostras])
            self._ids_combos.update(novos)

    def _numerar(self, amostras: list[tuple], novos: dict[str, int]) -> dict[str, int]:
        """{chave: numero} de todas as chaves das amostras, criando as que faltam em `combos`. Chamado com a trava.

        As criadas aqui vao para `novos` (e nao direto para a memoria): se a transacao voltar atras, o numero
        nao existe mais no banco e poderia ser dado depois a outra combinacao.
        """
        ids = self._ids_combos
        faltam = sorted({item[0] for item in amostras if item[0] not in ids})
        if not faltam:
            return ids
        self._conn.executemany("INSERT OR IGNORE INTO combos (chave) VALUES (?)", [(chave,) for chave in faltam])
        for chave in faltam:
            novos[chave] = self._conn.execute("SELECT id FROM combos WHERE chave = ?", (chave,)).fetchone()[0]
        return {**ids, **novos}

    def amostras(self, chave: str, desde: float) -> list[dict]:
        """As amostras de `chave` com `ts >= desde`, da mais antiga para a mais nova.

        Com `periodo_max_s`, nunca devolve amostras mais velhas que esse periodo (veja __init__).
        """
        if self._periodo_max_s is not None:
            desde = max(desde, self._relogio() - self._periodo_max_s)
        with self._trava:
            linhas = self._conn.execute(
                "SELECT ts, a, f, preco_long, preco_short, lote_long, lote_short FROM amostras "
                "WHERE combo_id = (SELECT id FROM combos WHERE chave = ?) AND ts >= ? ORDER BY ts",
                (chave, desde),
            ).fetchall()
        return [dict(linha) for linha in linhas]

    def amostras_por_combinacao(self, desde: float) -> Iterator[tuple[str, list[tuple]]]:
        """Para recarregar as janelas do motor: (chave, [(ts, a, f, lote_long, lote_short), ...]) de cada combinacao.

        So as amostras com `ts >= desde`, em ordem de horario; combinacao sem nenhuma fica de fora. Uma consulta
        por combinacao, cada uma com a trava so durante ela: quem grava (o escritor) espera no maximo uma.
        Se o banco for fechado no meio (scanner encerrando), para sem erro.
        """
        with self._trava:
            if self._fechado:
                return
            combos = self._conn.execute("SELECT id, chave FROM combos ORDER BY id").fetchall()
        for combo_id, chave in combos:
            with self._trava:
                if self._fechado:
                    return
                cursor = self._conn.cursor()
                cursor.row_factory = None  # tuplas simples: mais rapido que sqlite3.Row para milhares de linhas
                linhas = cursor.execute(
                    "SELECT ts, a, f, lote_long, lote_short FROM amostras WHERE combo_id = ? AND ts >= ? ORDER BY ts",
                    (combo_id, desde),
                ).fetchall()
            if linhas:
                yield chave, linhas

    def limpar_antigos(self, agora: float, dias: float, lote: int = LOTE_LIMPEZA) -> int:
        """Apaga as amostras mais velhas que `agora - dias` dias e devolve quantas foram apagadas.

        So amostras (especificacao, secao 8): o historico de sinais, os grupos e as metricas das corretoras
        por dia ficam (sao pequenos e servem para conferir a validacao depois).

        Anda combinacao por combinacao e apaga em lotes de no maximo `lote` amostras, uma transacao por lote,
        soltando a trava entre eles (veja LOTE_LIMPEZA): rodando em outra thread (o __main__ faz assim), quem
        grava espera no maximo um lote. Se o banco for fechado no meio (scanner encerrando), para no proximo
        lote, sem erro.
        """
        if not isinstance(lote, int) or isinstance(lote, bool) or lote < 1:
            raise ValueError(f"lote precisa ser um inteiro maior que zero (recebi {lote!r})")
        corte = agora - dias * 86400
        with self._trava:
            if self._fechado:
                return 0
            combos = [linha[0] for linha in self._conn.execute("SELECT id FROM combos ORDER BY id")]
        apagadas = 0
        for combo_id in combos:
            while True:
                resultado = self._apagar_lote(combo_id, corte, lote)
                if resultado is None:  # o banco foi fechado no meio: para aqui
                    return apagadas
                apagadas += resultado
                if resultado < lote:
                    break  # esta combinacao nao tem mais nada velho
                # Sem a trava por um instante: quem estava esperando (o escritor) entra agora. Sem a pausa, esta
                # thread pegaria a trava de novo na hora.
                time.sleep(PAUSA_ENTRE_LOTES_S)
        return apagadas

    def _apagar_lote(self, combo_id: int, corte: float, lote: int) -> int | None:
        """Um lote da limpeza: apaga as `lote` amostras mais velhas da combinacao com ts < corte.

        Devolve quantas apagou, ou None se o banco ja foi fechado.
        """
        with self._trava:
            if self._fechado:
                return None
            with self._conn:
                return self._conn.execute(
                    "DELETE FROM amostras WHERE combo_id = ? AND ts IN "
                    "(SELECT ts FROM amostras WHERE combo_id = ? AND ts < ? ORDER BY ts LIMIT ?)",
                    (combo_id, combo_id, corte, lote),
                ).rowcount

    # --- Corretoras por dia (metricas de validacao, especificacao secao 9) -----

    def registrar_corretoras(self, dia: str, status: dict[str, dict]) -> None:
        """Grava uma leitura do status das corretoras (o que `Motor.status` devolve) no dia UTC `dia` ("AAAA-MM-DD").

        status: {corretora: {"atraso_medio_ms": numero ou None, "reconexoes": inteiro, ...}} (o resto e ignorado).
        - reconexoes: o scanner informa o total desde que ligou. Entra no dia so o quanto o total cresceu desde
          a ultima leitura gravada por este Banco (que comeca em zero, como o scanner que acabou de ligar). Se o
          total ficou menor, o contador recomecou do zero e o total inteiro e novo. Assim o scanner reiniciado
          no meio do dia nao perde reconexoes nem conta as mesmas duas vezes.
        - atraso_medio_ms: cada leitura que nao e None entra na media do dia.
        Valor invalido levanta ValueError (dizendo qual corretora) e nada e gravado.
        """
        dia = _dia(dia)
        with self._trava:
            totais, linhas = {}, []
            for corretora, dados in status.items():  # confere tudo antes de gravar qualquer coisa
                totais[corretora], atraso = self._ler_status(corretora, dados)
                novas = self._reconexoes_novas(corretora, totais[corretora])
                linhas.append((dia, corretora, novas, 0.0 if atraso is None else atraso, 0 if atraso is None else 1))
            with self._conn:
                self._conn.executemany(_SOMAR_CORRETORA, linhas)
            self._contador_gravado.update(totais)  # so depois de gravado

    def corretoras_do_dia(self, dia: str, status_atual: dict[str, dict] | None = None) -> dict[str, dict]:
        """Atraso medio e reconexoes de cada corretora no dia UTC `dia`: {corretora: {"atraso_medio_ms", "reconexoes"}}.

        atraso_medio_ms: media das leituras do dia, em ms (None se a corretora nao informou o horario).
        status_atual: o `Motor.status` de agora, para completar o dia de HOJE com o que ainda nao foi gravado
            (passe so quando `dia` e hoje). Conta como mais uma leitura, sem gravar nada.
        Levanta ValueError se `dia` nao for uma data valida.
        """
        dia = _dia(dia)
        with self._trava:
            linhas = self._conn.execute(
                "SELECT corretora, reconexoes, soma_atraso_ms, leituras_atraso FROM corretoras_dia WHERE dia = ?",
                (dia,),
            ).fetchall()
            somas = {linha["corretora"]: [linha["reconexoes"], linha["soma_atraso_ms"], linha["leituras_atraso"]]
                     for linha in linhas}
            for corretora, dados in (status_atual or {}).items():
                total, atraso = self._ler_status(corretora, dados)
                soma = somas.setdefault(corretora, [0, 0.0, 0])
                soma[0] += self._reconexoes_novas(corretora, total)
                if atraso is not None:
                    soma[1] += atraso
                    soma[2] += 1
        return {
            corretora: {"atraso_medio_ms": round(soma_atraso / leituras, 1) if leituras else None,
                        "reconexoes": reconexoes}
            for corretora, (reconexoes, soma_atraso, leituras) in sorted(somas.items())
        }

    def _reconexoes_novas(self, corretora: str, total: int) -> int:
        """Quantas reconexoes o `total` do scanner tem a mais que a ultima leitura gravada (veja registrar_corretoras)."""
        anterior = self._contador_gravado.get(corretora, 0)
        return total - anterior if total >= anterior else total  # menor = o contador recomecou do zero

    @staticmethod
    def _ler_status(corretora: str, dados: dict) -> tuple[int, float | None]:
        """(reconexoes, atraso_medio_ms) do status de uma corretora. ValueError se algum dos dois for invalido."""
        reconexoes, atraso = dados.get("reconexoes", 0), dados.get("atraso_medio_ms")
        if not isinstance(reconexoes, int) or isinstance(reconexoes, bool) or reconexoes < 0:
            raise ValueError(f"{corretora}: reconexoes precisa ser um inteiro >= 0 (recebi {reconexoes!r})")
        if atraso is not None and not _numero_real(atraso):
            raise ValueError(f"{corretora}: atraso_medio_ms precisa ser um numero ou None (recebi {atraso!r})")
        return reconexoes, atraso

    # --- Grupos de filtros ------------------------------------------------------

    def salvar_grupo(self, nome: str, filtros: dict) -> None:
        """Salva o grupo `nome` (substitui o que ja existir com esse nome)."""
        texto = _json(filtros)
        with self._trava, self._conn:
            self._conn.execute(
                "INSERT INTO grupos (nome, filtros_json) VALUES (?, ?) "
                "ON CONFLICT (nome) DO UPDATE SET filtros_json = excluded.filtros_json",
                (nome, texto),
            )

    def listar_grupos(self) -> dict[str, dict]:
        """Todos os grupos salvos: {nome: filtros}."""
        with self._trava:
            linhas = self._conn.execute("SELECT nome, filtros_json FROM grupos").fetchall()
        return {linha["nome"]: json.loads(linha["filtros_json"]) for linha in linhas}

    def apagar_grupo(self, nome: str) -> None:
        """Apaga o grupo `nome`. Se ele nao existir, nao faz nada."""
        with self._trava, self._conn:
            self._conn.execute("DELETE FROM grupos WHERE nome = ?", (nome,))

    # --- Sessao do painel (modo, filtros e comparacao) ---------------------------

    def salvar_sessao(self, sessao: dict) -> None:
        """Guarda a sessao em uso ({"modo", "filtros", "comparacao"}), no lugar da anterior: o scanner religa nela."""
        texto = _json(sessao)
        with self._trava, self._conn:
            self._conn.execute(
                "INSERT INTO sessao (id, sessao_json) VALUES (1, ?) "
                "ON CONFLICT (id) DO UPDATE SET sessao_json = excluded.sessao_json",
                (texto,),
            )

    def ler_sessao(self) -> dict | None:
        """A ultima sessao guardada, ou None se nunca foi guardada. ValueError se o texto guardado nao for JSON."""
        with self._trava:
            linha = self._conn.execute("SELECT sessao_json FROM sessao WHERE id = 1").fetchone()
        return None if linha is None else json.loads(linha["sessao_json"])
