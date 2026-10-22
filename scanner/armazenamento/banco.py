"""Banco de dados SQLite do scanner (especificacao, secoes de armazenamento e do painel).

Um arquivo so guarda tres coisas:

- `sinais`: o historico de oportunidades. Um registro por sinal, aberto quando o sinal nasce
  (`encerrado_em` vazio) e fechado quando ele some. O sinal completo (formato JSON da spec,
  secao 5) fica guardado como texto em `sinal_json`; as outras colunas existem para buscar rapido.
- `amostras`: a cada ~5 s, os valores de um par monitorado (spread de abertura `a`, de
  fechamento `f`, precos e lotes). Alimentam o grafico de historico e sao apagadas depois de
  alguns dias (`limpar_antigos`).
- `grupos`: os grupos de filtros salvos pelo usuario (ex.: "Farm"), como JSON.

Tempo: `ts` das amostras e `agora` sao segundos desde 1970. As datas dos sinais sao texto ISO 8601
em UTC terminando em `Z` (ex.: "2026-10-20T15:04:12.345Z"), como no JSON da spec; nesse formato
a ordem alfabetica e a ordem do tempo, por isso comparar os textos com < e >= funciona.

Uma unica conexao por `Banco`, em modo WAL (o motor grava enquanto a API le). A conexao pode ser
usada por mais de uma thread: um `Lock` faz uma operacao esperar a outra terminar.

Desempenho: o motor grava os eventos e as amostras de cada ciclo em lote (uma transacao por ciclo) e,
no modo WAL, com `synchronous=NORMAL` (veja `__init__`).
"""

import json
import sqlite3
import threading
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
"""


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


def _json(valor) -> str:
    """Dict -> texto JSON. NaN e infinito sao recusados (ValueError): o JSON da spec nao os aceita."""
    return json.dumps(valor, allow_nan=False)


def _iso_utc(segundos: float) -> str:
    """Segundos desde 1970 -> "2026-10-20T15:04:12.345Z" (mesmo formato das datas dos sinais)."""
    momento = datetime.fromtimestamp(segundos, tz=timezone.utc)
    return momento.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Banco:
    """Abre (ou cria) o arquivo SQLite em `caminho` e oferece as operacoes que o scanner precisa."""

    def __init__(self, caminho: str):
        # check_same_thread=False: o motor e a API podem ficar em threads diferentes.
        self._conn = sqlite3.connect(caminho, check_same_thread=False)
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

        Cada evento segue as regras de `gravar_evento`. Evento cujo sinal nao vira JSON valido (ex.: NaN)
        ou nao tem os campos obrigatorios fica de fora, sem impedir os outros: a lista devolvida traz cada
        recusado com o erro, para quem chamou registrar no log. Erro do proprio banco (disco cheio...)
        levanta a excecao e nada do lote e gravado.
        """
        linhas, recusados = [], []
        for e in eventos:
            if e.tipo == "atualizado":
                continue
            try:
                linhas.append(self._linha_do_evento(e))  # antes de tocar no banco: JSON invalido nao entra
            except (ValueError, TypeError, KeyError) as erro:
                recusados.append((e, erro))
        if linhas:
            with self._trava, self._conn:
                self._conn.executemany(_GRAVAR_SINAL, linhas)
        return recusados

    @staticmethod
    def _linha_do_evento(e: Evento) -> tuple:
        """Os valores da linha da tabela `sinais` para o evento (na ordem de _GRAVAR_SINAL)."""
        sinal = e.sinal
        texto = _json(sinal)
        encerrado_em = sinal.get("encerrado_em") if e.tipo == "encerrado" else None
        return (sinal["id"], sinal["modo"], sinal["par"], sinal["tipo"], sinal["detectado_em"],
                encerrado_em, sinal.get("rp_liquido_pct"), texto)

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

    def limpar_antigos(self, agora: float, dias: float) -> int:
        """Apaga o que e mais velho que `agora - dias` dias e devolve quantas amostras foram apagadas.

        Alem das amostras, apaga os sinais ja encerrados antes desse limite. Sinais ainda abertos
        nunca sao apagados.
        """
        corte = agora - dias * 86400
        with self._trava, self._conn:
            apagadas = self._conn.execute("DELETE FROM amostras WHERE ts < ?", (corte,)).rowcount
            self._conn.execute(
                "DELETE FROM sinais WHERE encerrado_em IS NOT NULL AND encerrado_em < ?", (_iso_utc(corte),)
            )
        return apagadas

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
