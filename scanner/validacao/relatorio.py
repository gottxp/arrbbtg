"""Metricas de validacao do scanner (especificacao, secao 9), por dia e por modo.

`resumo_do_dia(banco, dia)` le do banco os sinais do dia e o atraso/reconexoes de cada corretora e devolve:

    {
      "dia": "2026-10-20",
      "lucro": {"quantidade_sinais": 3, "abertos": 0, "rp_liquido_medio_pct": 0.4, "duracao_media_s": 17.333,
                "pct_sumiu_5s": 33.33, "pct_sumiu_30s": 66.67},
      "farm":  {...o mesmo para o modo farm...},
      "corretoras": {"gate": {"atraso_medio_ms": 180.5, "reconexoes": 2}, ...},
    }

O __main__ escreve esse resumo no log quando o dia vira (`linhas_do_resumo`) e a API o entrega em
GET /api/validacao.
"""

import math
from datetime import date, datetime, timedelta, timezone

from scanner.armazenamento import Banco
from scanner.motor import MODOS

# "% de sinais que sumiram em menos de 5s e de 30s" (especificacao, secao 9). Estritamente menos: 5 s
# cravados nao contam como "menos de 5 s".
SUMIU_RAPIDO_S = 5
SUMIU_EM_30_S = 30


def dia_utc(segundos: float) -> str:
    """Segundos desde 1970 -> o dia UTC "AAAA-MM-DD" (o mesmo dia que as datas dos sinais mostram)."""
    # 1970 + segundos (e nao datetime.fromtimestamp, que no Windows recusa horario antes de 1970).
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=segundos)).date().isoformat()


def _media(valores: list[float]) -> float | None:
    return math.fsum(valores) / len(valores) if valores else None


def _arredondar(valor: float | None, casas: int) -> float | None:
    return None if valor is None else round(valor, casas)


def _duracao_s(sinal: dict) -> float:
    """Segundos entre detectado_em e encerrado_em ("2026-10-20T15:04:12.345Z"). Nunca negativa (relogio que voltou)."""
    inicio = datetime.fromisoformat(sinal["detectado_em"])
    fim = datetime.fromisoformat(sinal["encerrado_em"])
    return max(0.0, (fim - inicio).total_seconds())


def _resumo_do_modo(sinais: list[dict]) -> dict:
    """As metricas de um modo a partir dos registros do dia (os itens de Banco.sinais_do_dia desse modo)."""
    encerrados = [s for s in sinais if s["encerrado_em"] is not None]
    duracoes = [_duracao_s(s) for s in encerrados]
    rps = [s["rp_liquido_pct"] for s in sinais if s["rp_liquido_pct"] is not None]

    def pct_menos_de(limite_s: float) -> float | None:
        if not duracoes:
            return None
        return round(100 * sum(1 for d in duracoes if d < limite_s) / len(duracoes), 2)

    return {
        "quantidade_sinais": len(sinais),
        "abertos": len(sinais) - len(encerrados),
        "rp_liquido_medio_pct": _arredondar(_media(rps), 4),
        "duracao_media_s": _arredondar(_media(duracoes), 3),
        "pct_sumiu_5s": pct_menos_de(SUMIU_RAPIDO_S),
        "pct_sumiu_30s": pct_menos_de(SUMIU_EM_30_S),
    }


def resumo_do_dia(banco: Banco, dia: str, status_corretoras: dict | None = None) -> dict:
    """As metricas de validacao do dia `dia` ("AAAA-MM-DD"), por modo e por corretora (formato no topo do arquivo).

    Dia = dia UTC do `detectado_em` do sinal (como Banco.sinais_do_dia): um sinal que nasce as 23:59:50 UTC
    e some depois da meia-noite conta inteiro no dia em que nasceu.

    Por modo (lucro e farm):
    - quantidade_sinais: todos os sinais detectados no dia, inclusive os ainda abertos (`abertos` diz quantos);
    - rp_liquido_medio_pct: media do RP liquido gravado de cada sinal (o ultimo visto; o do aberto e o do inicio);
    - duracao_media_s, pct_sumiu_5s e pct_sumiu_30s: so sobre os sinais ja encerrados; sem nenhum, ficam None.
    Modo sem sinais: quantidades 0 e o resto None.

    Corretoras: atraso medio (ms) e reconexoes do dia, gravados pelo __main__ (Banco.registrar_corretoras).
    status_corretoras: o `Motor.status` de agora; passe so quando `dia` e hoje, para completar o dia com o
    que ainda nao foi gravado.

    Levanta ValueError se `dia` nao for uma data valida.
    """
    sinais = banco.sinais_do_dia(dia)
    resumo = {"dia": date.fromisoformat(dia).isoformat()}
    for modo in MODOS:
        resumo[modo] = _resumo_do_modo([s for s in sinais if s["modo"] == modo])
    resumo["corretoras"] = banco.corretoras_do_dia(dia, status_corretoras)
    return resumo


# ---------------------------------------------------------------------------
# Linhas do log
# ---------------------------------------------------------------------------


def _texto(valor: float | None, formato: str, unidade: str = "") -> str:
    """Numero formatado com a unidade, ou "-" quando nao ha valor."""
    return "-" if valor is None else f"{valor:{formato}}{unidade}"


def linhas_do_resumo(resumo: dict) -> list[str]:
    """O resumo do dia em linhas para o log: uma por modo e uma com as corretoras."""
    inicio = f"resumo do dia {resumo['dia']} UTC"
    linhas = []
    for modo in MODOS:
        m = resumo[modo]
        linhas.append(
            f"{inicio}, modo {modo}: {m['quantidade_sinais']} sinais ({m['abertos']} ainda aberto(s)); "
            f"RP liquido medio {_texto(m['rp_liquido_medio_pct'], '.4f', '%')}; "
            f"duracao media {_texto(m['duracao_media_s'], '.1f', ' s')}; "
            f"sumiram em menos de {SUMIU_RAPIDO_S} s: {_texto(m['pct_sumiu_5s'], '.1f', '%')}; "
            f"em menos de {SUMIU_EM_30_S} s: {_texto(m['pct_sumiu_30s'], '.1f', '%')}"
        )
    partes = [f"{nome} atraso medio {_texto(c['atraso_medio_ms'], '.1f', ' ms')}, {c['reconexoes']} reconexoes"
              for nome, c in resumo["corretoras"].items()]
    linhas.append(f"{inicio}, corretoras: {'; '.join(partes) if partes else 'nenhuma leitura gravada'}")
    return linhas
