"""Banco de dados SQLite do scanner (especificacao, secoes de armazenamento e do painel).

Um arquivo so guarda quatro coisas:

- `sinais`: o historico de oportunidades. Um registro por sinal, aberto quando o sinal nasce
  (`encerrado_em` vazio) e fechado quando ele some. O sinal completo (formato JSON da spec,
  secao 5) fica guardado como texto em `sinal_json`; as outras colunas existem para buscar rapido.
- `amostras`: a cada ~5 s, os valores de um par monitorado (spread de abertura `a`, de
  fechamento `f`, precos e lotes). Alimentam o grafico de historico e sao apagadas depois de
  alguns dias (`limpar_antigos`).
- `grupos`: os grupos de filtros salvos pelo usuario (ex.: "Farm"), como JSON.
- `corretoras_dia`: por dia (UTC) e corretora, as reconexoes do dia e o atraso medio das leituras do dia
  (metricas de validacao, especificacao secao 9; veja `registrar_corretoras`).

Tempo: `ts` das amostras e `agora` sao segundos desde 1970. As datas dos sinais sao texto ISO 8601
em UTC terminando em `Z` (ex.: "2026-10-20T15:04:12.345Z"), como no JSON da spec; nesse formato
a ordem alfabetica e a ordem do tempo, por isso comparar os textos com < e >= funciona.

Uma unica conexao por `Banco`, em modo WAL (o motor grava enquanto a API le). A conexao pode ser
usada por mais de uma thread: um `Lock` faz uma operacao esperar a outra terminar.

Desempenho: o motor grava os eventos e as amostras de cada ciclo em lote (uma transacao por ciclo) e,
no modo WAL, com `synchronous=NORMAL` (veja `__init__`).
"""

import json
import math
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

from scanner.sinais import Evento

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
    sinal_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sinais_detectado_em ON sinais (detectado_em);

CREATE TABLE IF NOT EXISTS amostras (
    chave       TEXT NOT NULL,
    ts          REAL NOT NULL,      -- segundos desde 1970
    a           REAL,               -- spread de abertura
    f           REAL,               -- spread de fechamento
    preco_long  REAL,
    preco_short REAL,
    lote_long   REAL,
    lote_short  REAL
);
CREATE INDEX IF NOT EXISTS idx_amostras_chave_ts ON amostras (chave, ts);

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

# A limpeza apaga em lotes de LOTE_LIMPEZA rowids (o numero de cada linha), um lote por transacao, soltando a
# trava entre um lote e outro: o motor, que grava a cada ciclo, espera no maximo um lote (dezenas de ms), e nao
# a limpeza inteira (mais de 1 s com 2 milhoes de amostras). Detalhe tecnico, nao um ajuste do dono.
LOTE_LIMPEZA = 2000
# Pausa entre um lote e outro, sem a trava, para quem estiver esperando por ela entrar (veja _apagar_em_lotes).
PAUSA_ENTRE_LOTES_S = 0.005


# Insere o sinal; se o id ja existe, atualiza tudo menos `detectado_em` (que nunca muda).
_GRAVAR_SINAL = """
INSERT INTO sinais (id, modo, par, tipo, detectado_em, encerrado_em, rp_liquido_pct, sinal_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
    """Abre (ou cria) o arquivo SQLite em `caminho` e oferece as operacoes que o scanner precisa."""

    def __init__(self, caminho: str):
        # check_same_thread=False: o motor e a API podem ficar em threads diferentes.
        self._conn = sqlite3.connect(caminho, check_same_thread=False)
        self._fechado = False  # `fechar` ja foi chamado (a limpeza em outra thread para no proximo lote)
        # Ultimo valor do contador de reconexoes de cada corretora gravado por ESTE Banco (esta execucao do
        # scanner). Comeca vazio = zero, como o contador do scanner que acabou de ligar (veja registrar_corretoras).
        self._contador_gravado: dict[str, int] = {}
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
            with self._conn:
                self._conn.executescript(_ESQUEMA)
        except BaseException:
            self._conn.close()  # deu erro preparando o banco: nao deixa a conexao (e o arquivo) aberta
            raise

    def fechar(self) -> None:
        """Fecha a conexao. Depois disso o `Banco` nao pode mais ser usado."""
        with self._trava:
            self._fechado = True
            self._conn.close()

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
        return (sinal["id"], sinal["modo"], sinal["par"], sinal["tipo"], sinal["detectado_em"],
                encerrado_em, rp, texto)

    def sinais_do_dia(self, dia: str) -> list[dict]:
        """Os sinais detectados no dia UTC `dia` ("AAAA-MM-DD"), do mais antigo ao mais novo.

        Cada item tem: id, modo, par, tipo, detectado_em, encerrado_em, rp_liquido_pct e `sinal`
        (o JSON completo, ja como dict). Levanta ValueError se `dia` nao for uma data valida.
        """
        inicio = date.fromisoformat(dia)
        # Qualquer "2026-10-20T..." e maior que "2026-10-20" e menor que "2026-10-21":
        # comparar com as datas puras pega exatamente o dia inteiro.
        de, ate = inicio.isoformat(), (inicio + timedelta(days=1)).isoformat()
        with self._trava:
            linhas = self._conn.execute(
                """
                SELECT id, modo, par, tipo, detectado_em, encerrado_em, rp_liquido_pct, sinal_json
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
                "rp_liquido_pct": linha["rp_liquido_pct"], "sinal": json.loads(linha["sinal_json"]),
            }
            for linha in linhas
        ]

    # --- Amostras ---------------------------------------------------------------

    def gravar_amostra(self, chave: str, ts: float, a: float, f: float,
                       preco_long: float, preco_short: float, lote_long: float, lote_short: float) -> None:
        """Grava uma amostra do par `chave` no instante `ts` (segundos desde 1970)."""
        with self._trava, self._conn:
            self._conn.execute(
                "INSERT INTO amostras (chave, ts, a, f, preco_long, preco_short, lote_long, lote_short) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chave, ts, a, f, preco_long, preco_short, lote_long, lote_short),
            )

    def gravar_amostras(self, amostras: list[tuple]) -> None:
        """Grava varias amostras numa transacao so: um acesso ao disco por ciclo, e nao um por amostra.

        Cada item e (chave, ts, a, f, preco_long, preco_short, lote_long, lote_short), na ordem de
        `gravar_amostra`. Lista vazia nao faz nada.
        """
        if not amostras:
            return
        with self._trava, self._conn:
            self._conn.executemany(
                "INSERT INTO amostras (chave, ts, a, f, preco_long, preco_short, lote_long, lote_short) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                amostras,
            )

    def amostras(self, chave: str, desde: float) -> list[dict]:
        """As amostras de `chave` com `ts >= desde`, da mais antiga para a mais nova."""
        with self._trava:
            linhas = self._conn.execute(
                "SELECT ts, a, f, preco_long, preco_short, lote_long, lote_short "
                "FROM amostras WHERE chave = ? AND ts >= ? ORDER BY ts",
                (chave, desde),
            ).fetchall()
        return [dict(linha) for linha in linhas]

    def limpar_antigos(self, agora: float, dias: float, lote: int = LOTE_LIMPEZA) -> int:
        """Apaga o que e mais velho que `agora - dias` dias e devolve quantas amostras foram apagadas.

        Alem das amostras, apaga os sinais ja encerrados antes desse limite (sinais ainda abertos nunca
        sao apagados) e as metricas das corretoras dos dias que terminaram antes dele.

        Apaga em lotes de `lote` rowids, uma transacao por lote, soltando a trava entre eles (veja
        LOTE_LIMPEZA): rodando em outra thread (o __main__ faz assim), o motor espera no maximo um lote.
        Se o banco for fechado no meio (scanner encerrando), para no proximo lote, sem erro.
        """
        if not isinstance(lote, int) or isinstance(lote, bool) or lote < 1:
            raise ValueError(f"lote precisa ser um inteiro maior que zero (recebi {lote!r})")
        corte = agora - dias * 86400
        apagadas = self._apagar_em_lotes("amostras", "ts < ?", (corte,), lote)
        self._apagar_em_lotes("sinais", "encerrado_em IS NOT NULL AND encerrado_em < ?", (_iso_utc(corte),), lote)
        with self._trava:
            if not self._fechado:
                with self._conn:  # tabela pequena (uma linha por corretora por dia): de uma vez
                    self._conn.execute("DELETE FROM corretoras_dia WHERE dia < ?", (_iso_utc(corte)[:10],))
        return apagadas

    def _apagar_em_lotes(self, tabela: str, condicao: str, parametros: tuple, lote: int) -> int:
        """Apaga as linhas de `tabela` que atendem `condicao`, andando pelo rowid de `lote` em `lote`. Devolve quantas.

        Andar pelo rowid (o numero da linha, que e a chave da tabela) passa por todas as linhas que existiam no
        comeco, mesmo as fora de ordem de horario (relogio que voltou), e cada lote le no maximo `lote` linhas.
        `tabela` e `condicao` sao textos fixos deste arquivo (nunca vem de fora).
        """
        with self._trava:
            if self._fechado:
                return 0
            # Duas subconsultas (e nao "SELECT min(rowid), max(rowid)"): assim o SQLite le so as duas pontas da
            # tabela; com os dois juntos ele varre a tabela inteira (~0,2 s com 2 milhoes de amostras).
            menor, maior = self._conn.execute(
                f"SELECT (SELECT min(rowid) FROM {tabela}), (SELECT max(rowid) FROM {tabela})"
            ).fetchone()
        apagadas = 0
        inicio = menor
        while menor is not None and inicio <= maior:
            resultado = self._apagar_lote(tabela, condicao, parametros, inicio, inicio + lote)
            if resultado is None:  # o banco foi fechado no meio: para aqui
                break
            apagadas += resultado
            inicio += lote
            if inicio <= maior:
                # Sem a trava por um instante: quem estava esperando (o motor) entra agora. Sem a pausa, esta
                # thread pegaria a trava de novo na hora e o motor podia esperar mais de 1 s.
                time.sleep(PAUSA_ENTRE_LOTES_S)
        return apagadas

    def _apagar_lote(self, tabela: str, condicao: str, parametros: tuple, inicio: int, fim: int) -> int | None:
        """Um lote da limpeza: apaga as linhas com rowid em [inicio, fim) que atendem `condicao`.

        Devolve quantas apagou, ou None se o banco ja foi fechado.
        """
        with self._trava:
            if self._fechado:
                return None
            with self._conn:
                return self._conn.execute(
                    f"DELETE FROM {tabela} WHERE rowid >= ? AND rowid < ? AND {condicao}", (inicio, fim, *parametros)
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
