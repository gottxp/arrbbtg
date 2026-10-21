"""Testes da leitura e validacao do config.yaml."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

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


def test_config_yaml_do_projeto_bate_com_os_padroes():
    # O exemplo entregue na raiz tem os padroes escritos; nao pode divergir do codigo.
    assert carregar_config(str(RAIZ / "config.yaml")) == Config()


def test_main_imprime_config_ok(tmp_path, capsys):
    assert main(["--config", escrever(tmp_path, "corretoras: [bitget]\n")]) == 0
    assert "config ok" in capsys.readouterr().out


def test_main_com_config_invalida_mostra_erro_e_sai_com_1(tmp_path, capsys):
    assert main(["--config", escrever(tmp_path, "ciclo_ms: -1\n")]) == 1
    assert "ciclo_ms" in capsys.readouterr().err
