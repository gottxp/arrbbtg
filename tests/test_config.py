"""Testes da leitura e validacao do config.yaml."""

import re
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest
import yaml

from scanner.__main__ import main
from scanner.config.carregar import Config, ErroConfig, carregar_config

RAIZ = Path(__file__).resolve().parent.parent


def escrever(tmp_path, texto):
    """Grava um config.yaml temporario e devolve o caminho como texto."""
    arq = tmp_path / "config.yaml"
    arq.write_text(texto)
    return str(arq)


def test_padroes_quando_campo_ausente(tmp_path):
    arq = tmp_path / "config.yaml"; arq.write_text("corretoras: [bitget]\n")
    c = carregar_config(str(arq))
    assert c.tamanho_referencia_usd == 100
    assert c.max_silencio_conexao_s == 2
    assert c.max_idade_livro_s == 30
    assert c.periodo_metricas_h == 4
    assert c.rp_min_lucro_pct == 0.2
    assert c.custo_max_farm_pct == -0.1
    assert c.limite_suspeito_pct == 30


def test_valor_invalido(tmp_path):
    arq = tmp_path / "config.yaml"; arq.write_text("tamanho_referencia_usd: -5\n")
    with pytest.raises(ErroConfig, match="tamanho_referencia_usd"):
        carregar_config(str(arq))


def test_arquivo_vazio_usa_todos_os_padroes(tmp_path):
    c = carregar_config(escrever(tmp_path, ""))
    assert c == Config()
    assert c.corretoras == ["mexc", "gate", "bitget"]
    assert c.corretoras_farm == []
    assert c.metrica_spread == "medio"
    assert c.taxas == {}
    assert c.painel == {"host": "127.0.0.1", "porta": 8000, "usuario": None, "senha": None}
    assert c.banco == "scanner.db"
    assert c.nivel_log == "normal"


def test_valores_do_yaml_sobrescrevem_padroes(tmp_path):
    c = carregar_config(escrever(tmp_path, (
        "corretoras: [gate, mexc]\n"
        "corretoras_farm: [lighter]\n"
        "ciclo_ms: 250\n"
        "spread_min_livro_pct: 0.5\n"
        "metrica_spread: maior\n"
        "banco: outro.db\n"
        "nivel_log: detalhado\n"
    )))
    assert c.corretoras == ["gate", "mexc"]
    assert c.corretoras_farm == ["lighter"]
    assert c.ciclo_ms == 250
    assert c.spread_min_livro_pct == 0.5
    assert c.metrica_spread == "maior"
    assert c.banco == "outro.db"
    assert c.nivel_log == "detalhado"


def test_taxas_viram_dicionario_com_chave_corretora_mercado(tmp_path):
    c = carregar_config(escrever(tmp_path, (
        "taxas:\n"
        "  mexc:\n"
        "    futuro: 0.02\n"
        "    spot: 0\n"
        "  gate:\n"
        "    futuro: 0.045\n"
    )))
    assert c.taxas == {
        ("mexc", "futuro"): 0.02,
        ("mexc", "spot"): 0,
        ("gate", "futuro"): 0.045,
    }


def test_painel_parcial_completa_com_padroes(tmp_path):
    c = carregar_config(escrever(tmp_path, "painel:\n  porta: 9000\n  usuario: jr\n"))
    assert c.painel == {"host": "127.0.0.1", "porta": 9000, "usuario": "jr", "senha": None}


def test_config_e_congelada(tmp_path):
    c = carregar_config(escrever(tmp_path, ""))
    with pytest.raises(FrozenInstanceError):
        c.ciclo_ms = 1


@pytest.mark.parametrize("campo", [
    "tamanho_referencia_usd", "max_silencio_conexao_s", "max_idade_livro_s",
    "manter_livro_s", "periodo_metricas_h", "ciclo_ms", "amostra_s",
    "retencao_dias", "tempo_operacao_h", "intervalo_dados_lentos_s",
    "recarregar_mercados_s",
])
@pytest.mark.parametrize("valor", ["0", "-1"])
def test_numeros_que_precisam_ser_positivos(tmp_path, campo, valor):
    with pytest.raises(ErroConfig, match=campo):
        carregar_config(escrever(tmp_path, f"{campo}: {valor}\n"))


@pytest.mark.parametrize("texto, campo", [
    ("tamanho_referencia_usd: muito\n", "tamanho_referencia_usd"),
    ("tamanho_referencia_usd: true\n", "tamanho_referencia_usd"),
    ("metrica_spread: qualquer\n", "metrica_spread"),
    ("nivel_log: barulhento\n", "nivel_log"),
    ("corretoras: mexc\n", "corretoras"),
    ("corretoras: []\n", "corretoras"),
    ("corretoras_farm: [1]\n", "corretoras_farm"),
    ("taxas: 5\n", "taxas"),
    ("taxas:\n  mexc:\n    margem: 0.1\n", "taxas"),
    ("taxas:\n  mexc:\n    futuro: -1\n", "taxas"),
    ("painel:\n  porta: 0\n", "painel"),
    ("painel:\n  porta: abc\n", "painel"),
    ("painel:\n  cor: azul\n", "painel"),
    ("banco: 7\n", "banco"),
])
def test_valores_com_formato_errado(tmp_path, texto, campo):
    with pytest.raises(ErroConfig, match=campo):
        carregar_config(escrever(tmp_path, texto))


def test_campo_desconhecido_e_rejeitado(tmp_path):
    # Evita que um erro de digitacao seja ignorado em silencio.
    with pytest.raises(ErroConfig, match="tamanho_referencia_uds"):
        carregar_config(escrever(tmp_path, "tamanho_referencia_uds: 50\n"))


def test_arquivo_inexistente(tmp_path):
    with pytest.raises(ErroConfig, match="nao_existe.yaml"):
        carregar_config(str(tmp_path / "nao_existe.yaml"))


def test_yaml_quebrado_e_raiz_que_nao_e_mapa(tmp_path):
    with pytest.raises(ErroConfig):
        carregar_config(escrever(tmp_path, "corretoras: [mexc\n"))
    with pytest.raises(ErroConfig):
        carregar_config(escrever(tmp_path, "- a\n- b\n"))


def test_config_yaml_do_projeto_e_valido_e_so_tem_campos_conhecidos():
    # Guarda contra divergencia entre o exemplo da raiz e o codigo. Nao compara valores
    # (o dono pode editar o config.yaml); so exige que carregue e que cada chave exista na Config.
    caminho = RAIZ / "config.yaml"
    carregar_config(str(caminho))  # levanta ErroConfig se algum valor estiver invalido
    chaves = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    campos = {f.name for f in fields(Config)}
    assert set(chaves) <= campos, f"chaves fora da Config: {set(chaves) - campos}"


def test_main_imprime_config_ok(tmp_path, capsys):
    assert main(["--config", escrever(tmp_path, "corretoras: [bitget]\n"), "--validar"]) == 0
    assert "config ok" in capsys.readouterr().out


def test_main_com_config_invalida_mostra_erro_e_sai_com_1(tmp_path, capsys):
    assert main(["--config", escrever(tmp_path, "ciclo_ms: -1\n")]) == 1
    assert "ciclo_ms" in capsys.readouterr().err


def test_timeout_desassinar_padrao_5s_e_precisa_ser_positivo(tmp_path):
    # Revisao da Task 10: tempo maximo esperando a corretora confirmar o cancelamento de um livro.
    assert carregar_config(escrever(tmp_path, "corretoras: [bitget]\n")).timeout_desassinar_s == 5
    assert carregar_config(escrever(tmp_path, "timeout_desassinar_s: 2.5\n")).timeout_desassinar_s == 2.5
    with pytest.raises(ErroConfig, match="timeout_desassinar_s"):
        carregar_config(escrever(tmp_path, "timeout_desassinar_s: 0\n"))


# ---------------------------------------------------------------------------
# Campos da Task 14 (reconexao, limites, logs e recursos)
# ---------------------------------------------------------------------------


def test_campos_da_robustez_com_os_padroes(tmp_path):
    c = carregar_config(escrever(tmp_path, ""))
    assert c.espera_max_reconexao_s == 60
    assert c.limites_assinatura == {}
    assert c.pasta_logs == "logs" and c.dias_logs == 30
    assert c.intervalo_log_recursos_s == 300
    assert c.atraso_max_valido_s == 30


def test_campos_da_robustez_lidos_do_yaml(tmp_path):
    c = carregar_config(escrever(tmp_path, (
        "corretoras: [bingx, mexc]\n"
        "espera_max_reconexao_s: 30\n"
        "limites_assinatura:\n  bingx: 200\n  mexc: 30\n"
        "pasta_logs: registros\n"
        "dias_logs: 7\n"
        "intervalo_log_recursos_s: 60\n"
        "atraso_max_valido_s: 10\n"
    )))
    assert c.espera_max_reconexao_s == 30
    assert c.limites_assinatura == {"bingx": 200, "mexc": 30}
    assert (c.pasta_logs, c.dias_logs, c.intervalo_log_recursos_s, c.atraso_max_valido_s) == ("registros", 7, 60, 10)


def test_limites_assinatura_vazio_ou_nulo_e_sem_limite(tmp_path):
    assert carregar_config(escrever(tmp_path, "limites_assinatura: {}\n")).limites_assinatura == {}
    assert carregar_config(escrever(tmp_path, "limites_assinatura:\n")).limites_assinatura == {}


@pytest.mark.parametrize("campo", ["espera_max_reconexao_s", "intervalo_log_recursos_s", "atraso_max_valido_s",
                                   "dias_logs"])
@pytest.mark.parametrize("valor", ["0", "-1"])
def test_campos_da_robustez_precisam_ser_positivos(tmp_path, campo, valor):
    with pytest.raises(ErroConfig, match=campo):
        carregar_config(escrever(tmp_path, f"{campo}: {valor}\n"))


@pytest.mark.parametrize("texto, campo", [
    ("limites_assinatura: 5\n", "limites_assinatura"),
    ("limites_assinatura: [bingx]\n", "limites_assinatura"),
    ("limites_assinatura:\n  bingx: 0\n", "limites_assinatura"),
    ("limites_assinatura:\n  bingx: 2.5\n", "limites_assinatura"),
    ("limites_assinatura:\n  bingx: true\n", "limites_assinatura"),
    ("limites_assinatura:\n  bingx: muitos\n", "limites_assinatura"),
    ("dias_logs: 2.5\n", "dias_logs"),
    ("dias_logs: true\n", "dias_logs"),
    ("pasta_logs: ''\n", "pasta_logs"),
    ("pasta_logs: 5\n", "pasta_logs"),
])
def test_campos_da_robustez_com_formato_errado(tmp_path, texto, campo):
    with pytest.raises(ErroConfig, match=campo):
        carregar_config(escrever(tmp_path, texto))


def test_config_yaml_do_projeto_traz_os_campos_da_robustez():
    chaves = yaml.safe_load((RAIZ / "config.yaml").read_text(encoding="utf-8")) or {}
    for campo in ("espera_max_reconexao_s", "limites_assinatura", "pasta_logs", "dias_logs",
                  "intervalo_log_recursos_s", "atraso_max_valido_s"):
        assert campo in chaves, campo


def test_limite_de_corretora_que_nao_esta_em_corretoras_da_erro_claro(tmp_path):
    # Erro de digitacao no nome desligaria o limite em silencio.
    with pytest.raises(ErroConfig, match="limites_assinatura.*bingks.*corretoras"):
        carregar_config(escrever(tmp_path, "corretoras: [bingx, gate]\nlimites_assinatura:\n  bingks: 200\n"))
    with pytest.raises(ErroConfig, match="limites_assinatura.*bingx"):  # corretoras padrao: mexc, gate, bitget
        carregar_config(escrever(tmp_path, "limites_assinatura:\n  bingx: 200\n"))
    c = carregar_config(escrever(tmp_path, "corretoras: [bingx, gate]\nlimites_assinatura:\n  bingx: 200\n"))
    assert c.limites_assinatura == {"bingx": 200}


# ---------------------------------------------------------------------------
# Campos da Task 15 (fase 2 de corretoras: mercados, tickers, cotacao e funding)
# ---------------------------------------------------------------------------

SEIS = "corretoras: [mexc, gate, bitget, bingx, xt, hyperliquid]\n"


def test_campos_da_fase_2_com_os_padroes(tmp_path):
    c = carregar_config(escrever(tmp_path, ""))
    assert c.mercados_por_corretora == {}
    assert c.metodo_ticker == {}
    assert c.mensagens_sem_bid_ask == 10
    assert c.fonte_cotacao == "bitget"
    assert c.fontes_cotacao == {}
    assert c.faixa_cotacao_pct == 10
    assert c.intervalo_funding_h == {}
    assert c.mercados_da_corretora("mexc") == ("spot", "futuro")  # sem nada: os dois mercados


def test_campos_da_fase_2_lidos_do_yaml(tmp_path):
    c = carregar_config(escrever(tmp_path, SEIS + (
        "mercados_por_corretora:\n  hyperliquid: [futuro]\n  xt: [futuro, spot]\n"
        "metodo_ticker:\n  hyperliquid: watch_ticker\n  xt: watch_tickers\n"
        "mensagens_sem_bid_ask: 3\n"
        "fonte_cotacao: gate\n"
        "fontes_cotacao:\n  usdg: bingx\n"
        "faixa_cotacao_pct: 2.5\n"
        "intervalo_funding_h:\n  hyperliquid: 1\n"
    )))
    assert c.mercados_por_corretora == {"hyperliquid": ("futuro",), "xt": ("futuro", "spot")}
    assert c.mercados_da_corretora("hyperliquid") == ("futuro",)
    assert c.mercados_da_corretora("gate") == ("spot", "futuro")
    assert c.metodo_ticker == {"hyperliquid": "watch_ticker", "xt": "watch_tickers"}
    assert c.mensagens_sem_bid_ask == 3
    assert c.fonte_cotacao == "gate"
    assert c.fontes_cotacao == {"USDG": "bingx"}  # a moeda fica em maiuscula, como o ccxt escreve
    assert c.faixa_cotacao_pct == 2.5
    assert c.intervalo_funding_h == {"hyperliquid": 1}


def test_campos_da_fase_2_vazios_ou_nulos(tmp_path):
    c = carregar_config(escrever(tmp_path, "mercados_por_corretora:\nmetodo_ticker: {}\nfontes_cotacao:\n"
                                           "intervalo_funding_h: {}\n"))
    assert (c.mercados_por_corretora, c.metodo_ticker, c.fontes_cotacao, c.intervalo_funding_h) == ({}, {}, {}, {})


@pytest.mark.parametrize("texto, campo", [
    ("mercados_por_corretora: [mexc]\n", "mercados_por_corretora"),
    ("mercados_por_corretora:\n  mexc: futuro\n", "mercados_por_corretora"),
    ("mercados_por_corretora:\n  mexc: []\n", "mercados_por_corretora"),
    ("mercados_por_corretora:\n  mexc: [margem]\n", "mercados_por_corretora"),
    ("mercados_por_corretora:\n  mexc: [futuro, futuro]\n", "mercados_por_corretora"),
    ("metodo_ticker: watch_ticker\n", "metodo_ticker"),
    ("metodo_ticker:\n  mexc: watch_order_book\n", "metodo_ticker"),
    ("mensagens_sem_bid_ask: 0\n", "mensagens_sem_bid_ask"),
    ("mensagens_sem_bid_ask: 2.5\n", "mensagens_sem_bid_ask"),
    ("mensagens_sem_bid_ask: true\n", "mensagens_sem_bid_ask"),
    ("fonte_cotacao: 5\n", "fonte_cotacao"),
    ("fonte_cotacao: ''\n", "fonte_cotacao"),
    ("fontes_cotacao: [bitget]\n", "fontes_cotacao"),
    ("fontes_cotacao:\n  USDC: 5\n", "fontes_cotacao"),
    ("fontes_cotacao:\n  USDT: bitget\n", "fontes_cotacao"),  # USDT e a base: nao tem cotacao
    ("faixa_cotacao_pct: 0\n", "faixa_cotacao_pct"),
    ("faixa_cotacao_pct: -1\n", "faixa_cotacao_pct"),
    ("intervalo_funding_h: 8\n", "intervalo_funding_h"),
    ("intervalo_funding_h:\n  mexc: 0\n", "intervalo_funding_h"),
    ("intervalo_funding_h:\n  mexc: 2.5\n", "intervalo_funding_h"),
    ("intervalo_funding_h:\n  mexc: true\n", "intervalo_funding_h"),
])
def test_campos_da_fase_2_com_formato_errado(tmp_path, texto, campo):
    with pytest.raises(ErroConfig, match=campo):
        carregar_config(escrever(tmp_path, texto))


@pytest.mark.parametrize("texto, campo", [
    ("mercados_por_corretora:\n  hyperliqiud: [futuro]\n", "mercados_por_corretora.*hyperliqiud"),
    ("metodo_ticker:\n  bingks: watch_ticker\n", "metodo_ticker.*bingks"),
    ("intervalo_funding_h:\n  hyperliqiud: 1\n", "intervalo_funding_h.*hyperliqiud"),
    ("fontes_cotacao:\n  USDC: krakn\n", "fontes_cotacao.*krakn"),
    ("fonte_cotacao: bitgte\n", "fonte_cotacao.*bitgte"),
])
def test_corretora_dos_campos_da_fase_2_precisa_estar_em_corretoras(tmp_path, texto, campo):
    # Erro de digitacao no nome desligaria o ajuste em silencio.
    with pytest.raises(ErroConfig, match=campo):
        carregar_config(escrever(tmp_path, SEIS + texto))


def test_fonte_da_cotacao_precisa_ter_spot(tmp_path):
    # A cotacao vem do ticker SPOT da fonte (ex.: USDC/USDT da Bitget).
    with pytest.raises(ErroConfig, match="fonte_cotacao.*hyperliquid.*spot"):
        carregar_config(escrever(tmp_path, SEIS + "mercados_por_corretora:\n  hyperliquid: [futuro]\n"
                                                  "fonte_cotacao: hyperliquid\n"))
    with pytest.raises(ErroConfig, match="fontes_cotacao.*USDC.*bitget.*spot"):
        carregar_config(escrever(tmp_path, SEIS + "mercados_por_corretora:\n  bitget: [futuro]\n"
                                                  "fontes_cotacao:\n  USDC: bitget\n"))
    with pytest.raises(ErroConfig, match="fonte_cotacao.*bitget.*spot"):  # o padrao (bitget) tambem
        carregar_config(escrever(tmp_path, SEIS + "mercados_por_corretora:\n  bitget: [futuro]\n"))


def test_fonte_padrao_fora_das_corretoras_nao_e_erro(tmp_path):
    # Sem escrever fonte_cotacao, a Bitget pode nem estar ligada (ex.: so [gate, mexc]): sem erro; no uso,
    # a cotacao fica de fora (1:1 com aviso_cotacao) e o log avisa.
    c = carregar_config(escrever(tmp_path, "corretoras: [gate, mexc]\n"))
    assert c.fonte_cotacao == "bitget"


def test_config_yaml_do_projeto_traz_as_seis_corretoras_e_os_campos_da_fase_2():
    caminho = RAIZ / "config.yaml"
    c = carregar_config(str(caminho))
    assert c.corretoras[:6] == ["mexc", "gate", "bitget", "bingx", "xt", "hyperliquid"]  # e as da fase 3
    assert c.mercados_da_corretora("hyperliquid") == ("futuro",)   # spec, secao 2: so futuros
    assert c.metodo_ticker.get("hyperliquid") == "watch_ticker"     # o lote (allMids) nao tem bid/ask
    assert c.intervalo_funding_h.get("hyperliquid") == 1            # funding de hora em hora
    assert c.fonte_cotacao == "bitget"
    chaves = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    for campo in ("mercados_por_corretora", "metodo_ticker", "mensagens_sem_bid_ask", "fonte_cotacao",
                  "fontes_cotacao", "faixa_cotacao_pct", "intervalo_funding_h"):
        assert campo in chaves, campo



def test_cabecalho_do_config_yaml_lista_os_campos_que_nao_sao_o_padrao():
    # O cabecalho diz que apagar uma linha volta ao padrao: os campos do exemplo que NAO sao o padrao do
    # codigo precisam estar listados la, com o motivo.
    caminho = RAIZ / "config.yaml"
    cabecalho = []
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        if not linha.startswith("#"):
            break
        cabecalho.append(linha)
    texto = "\n".join(cabecalho)
    exemplo, padrao = carregar_config(str(caminho)), Config()
    diferentes = [f.name for f in fields(Config) if getattr(exemplo, f.name) != getattr(padrao, f.name)]
    assert diferentes, "o exemplo deveria ter campos fora do padrao (ex.: corretoras da fase 2)"
    for campo in diferentes:
        assert campo in texto, f"{campo} difere do padrao e nao aparece no cabecalho do config.yaml"
    assert "Todos os valores abaixo sao os padroes" not in texto
    assert "confirme com o dono" in texto  # o xt: [futuro] e um desvio da especificacao (secao 2)


# ---------------------------------------------------------------------------
# Guardas da documentacao (Task 17, parte B): a documentacao nao pode ficar velha
# ---------------------------------------------------------------------------

def _comentario_logo_acima(linhas: list[str], indice: int, recuo: str) -> list[str]:
    """As linhas de comentario coladas acima de linhas[indice] que comecam com `recuo` + "#"."""
    bloco = []
    for linha in reversed(linhas[:indice]):
        if not linha.startswith(recuo + "#"):
            break
        bloco.append(linha)
    return bloco[::-1]


# O que o comentario de cada campo do config.yaml precisa dizer (alem do que o campo faz).
PARTES_DO_COMENTARIO = ("Unidade", "Padrao", "Quando mexer")


def _falta_no_comentario(bloco: list[str]) -> list[str]:
    texto = "\n".join(bloco)
    return [parte for parte in PARTES_DO_COMENTARIO if parte not in texto]


def test_todo_campo_da_config_tem_comentario_completo_logo_acima_no_config_yaml():
    # Comentario de cada campo: o que faz, unidade, padrao e quando mexer, colado acima do campo. Um campo novo
    # na Config sem esse comentario quebra o teste.
    linhas = (RAIZ / "config.yaml").read_text(encoding="utf-8").splitlines()
    posicoes = {}
    for i, linha in enumerate(linhas):
        if not linha.startswith((" ", "#")) and ":" in linha:
            posicoes[linha.split(":", 1)[0]] = i
    for campo in (f.name for f in fields(Config)):
        assert campo in posicoes, f"{campo} nao aparece no config.yaml"
        bloco = _comentario_logo_acima(linhas, posicoes[campo], "")
        assert bloco, f"{campo}: falta o comentario logo acima do campo no config.yaml"
        assert not _falta_no_comentario(bloco), f"{campo}: falta {_falta_no_comentario(bloco)} no comentario"
    for item in Config().painel:  # os itens do bloco painel, com recuo de 2 espacos
        indice = next(i for i in range(posicoes["painel"] + 1, len(linhas)) if linhas[i].startswith(f"  {item}:"))
        bloco = _comentario_logo_acima(linhas, indice, "  ")
        assert bloco, f"painel.{item}: falta o comentario logo acima do item no config.yaml"
        assert not _falta_no_comentario(bloco), f"painel.{item}: falta {_falta_no_comentario(bloco)} no comentario"


def test_todo_campo_da_config_esta_na_tabela_do_readme_com_o_padrao_do_codigo():
    # README, parte 6 (Mudar a configuracao), tabela "Todos os campos": uma linha por campo, com o padrao em yaml.
    texto = (RAIZ / "README.md").read_text(encoding="utf-8")
    inicio = texto.index("\n## 6. ")
    fim = texto.index("\n## ", inicio + 1)
    linhas = re.findall(r"^\| `([a-z_0-9.]+)` \| `([^`]*)` \|", texto[inicio:fim], flags=re.MULTILINE)
    tabela = dict(linhas)
    assert len(tabela) == len(linhas), "campo repetido na tabela do README"
    padrao = Config()
    esperado = {f.name: getattr(padrao, f.name) for f in fields(Config) if f.name != "painel"}
    esperado.update({f"painel.{item}": valor for item, valor in padrao.painel.items()})
    assert set(tabela) == set(esperado), (
        f"faltam no README: {sorted(set(esperado) - set(tabela))}; sobram: {sorted(set(tabela) - set(esperado))}")
    for campo, valor in tabela.items():
        assert yaml.safe_load(valor) == esperado[campo], f"{campo}: o README diz {valor}, o codigo {esperado[campo]!r}"
