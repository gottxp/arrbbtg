"""Le o config.yaml e entrega um objeto Config ja validado.

Todo limite e padrao do scanner mora aqui: nenhum outro modulo deve ter
numero fixo no codigo, e sim ler o campo correspondente da Config.
"""

from dataclasses import dataclass, field, fields

import yaml


class ErroConfig(Exception):
    """Config invalida. A mensagem sempre diz qual campo esta errado."""


# Opcoes aceitas nos campos de texto com lista fechada de valores.
METRICAS_SPREAD = ("medio", "maior", "media_10_maiores")
NIVEIS_LOG = ("normal", "detalhado")
MERCADOS = ("spot", "futuro")


def _painel_padrao() -> dict:
    return {"host": "127.0.0.1", "porta": 8000, "usuario": None, "senha": None}


@dataclass(frozen=True)
class Config:
    """Todas as configuracoes do scanner, com os valores padrao."""

    # Corretoras usadas no modo normal e no modo Farm.
    corretoras: list = field(default_factory=lambda: ["mexc", "gate", "bitget"])
    corretoras_farm: list = field(default_factory=list)

    # Filtros e janelas (percentuais em %: 0.3 = 0,3%).
    tamanho_referencia_usd: float = 100
    max_silencio_conexao_s: float = 2
    max_idade_livro_s: float = 30
    spread_min_livro_pct: float = 0.3
    manter_livro_s: float = 60
    periodo_metricas_h: float = 4
    rp_min_lucro_pct: float = 0.2
    custo_max_farm_pct: float = -0.1
    limite_suspeito_pct: float = 30
    anti_repeticao_s: float = 60
    ciclo_ms: float = 500
    amostra_s: float = 5
    retencao_dias: float = 7
    tempo_operacao_h: float = 8
    margem_slippage_pct: float = 0.0
    metrica_spread: str = "medio"

    # Taxas taker sobrescritas por (corretora, mercado). Ex.: {("mexc", "futuro"): 0.02}
    taxas: dict = field(default_factory=dict)

    # Dados lentos (REST).
    intervalo_dados_lentos_s: float = 60
    recarregar_mercados_s: float = 3600

    # Painel web.
    painel: dict = field(default_factory=_painel_padrao)

    banco: str = "scanner.db"
    nivel_log: str = "normal"


# Campos numericos que precisam ser maiores que zero.
CAMPOS_POSITIVOS = (
    "tamanho_referencia_usd",
    "max_silencio_conexao_s",
    "max_idade_livro_s",
    "manter_livro_s",
    "periodo_metricas_h",
    "anti_repeticao_s",
    "ciclo_ms",
    "amostra_s",
    "retencao_dias",
    "tempo_operacao_h",
    "intervalo_dados_lentos_s",
    "recarregar_mercados_s",
)

# Campos numericos que aceitam qualquer valor (inclusive zero ou negativo).
CAMPOS_NUMERICOS_LIVRES = (
    "spread_min_livro_pct",
    "rp_min_lucro_pct",
    "custo_max_farm_pct",
    "limite_suspeito_pct",
    "margem_slippage_pct",
)


def _e_numero(valor) -> bool:
    """True para int ou float. bool nao conta (True seria 1 sem querer)."""
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def _validar_lista_de_texto(campo: str, valor, pode_ser_vazia: bool) -> list:
    if not isinstance(valor, list) or not all(isinstance(v, str) and v for v in valor):
        raise ErroConfig(f"{campo}: precisa ser uma lista de nomes, ex.: [mexc, gate]")
    if not valor and not pode_ser_vazia:
        raise ErroConfig(f"{campo}: precisa ter pelo menos uma corretora")
    return valor


def _validar_taxas(valor) -> dict:
    """Converte {corretora: {mercado: taxa}} em {(corretora, mercado): taxa}."""
    modelo = "taxas: use o formato  corretora: {futuro: 0.02}"
    if not isinstance(valor, dict):
        raise ErroConfig(modelo)
    taxas = {}
    for corretora, mercados in valor.items():
        if not isinstance(mercados, dict):
            raise ErroConfig(f"{modelo} (corretora '{corretora}')")
        for mercado, taxa in mercados.items():
            if mercado not in MERCADOS:
                raise ErroConfig(
                    f"taxas: mercado '{mercado}' de '{corretora}' invalido; use {' ou '.join(MERCADOS)}"
                )
            if not _e_numero(taxa) or taxa < 0:
                raise ErroConfig(
                    f"taxas: a taxa de '{corretora}'/'{mercado}' precisa ser um numero maior ou igual a zero"
                )
            taxas[(str(corretora), mercado)] = taxa
    return taxas


def _validar_painel(valor) -> dict:
    """Completa o que faltar no painel com os padroes e confere cada item."""
    if not isinstance(valor, dict):
        raise ErroConfig("painel: precisa ser um bloco com host, porta, usuario e senha")
    painel = _painel_padrao()
    for chave in valor:
        if chave not in painel:
            raise ErroConfig(f"painel.{chave}: campo desconhecido (use host, porta, usuario, senha)")
    painel.update(valor)
    if not isinstance(painel["host"], str) or not painel["host"]:
        raise ErroConfig("painel.host: precisa ser um texto, ex.: 127.0.0.1")
    porta = painel["porta"]
    if not isinstance(porta, int) or isinstance(porta, bool) or not 1 <= porta <= 65535:
        raise ErroConfig("painel.porta: precisa ser um numero inteiro entre 1 e 65535")
    for chave in ("usuario", "senha"):
        if painel[chave] is not None and not isinstance(painel[chave], str):
            raise ErroConfig(f"painel.{chave}: precisa ser texto (ou deixar vazio)")
    return painel


def _validar_campo(campo: str, valor):
    """Confere um campo do yaml e devolve o valor pronto para a Config."""
    if campo == "corretoras":
        return _validar_lista_de_texto(campo, valor, pode_ser_vazia=False)
    if campo == "corretoras_farm":
        return _validar_lista_de_texto(campo, valor, pode_ser_vazia=True)
    if campo == "taxas":
        return _validar_taxas(valor)
    if campo == "painel":
        return _validar_painel(valor)
    if campo == "metrica_spread":
        if valor not in METRICAS_SPREAD:
            raise ErroConfig(f"metrica_spread: use uma destas opcoes: {', '.join(METRICAS_SPREAD)}")
        return valor
    if campo == "nivel_log":
        if valor not in NIVEIS_LOG:
            raise ErroConfig(f"nivel_log: use uma destas opcoes: {', '.join(NIVEIS_LOG)}")
        return valor
    if campo == "banco":
        if not isinstance(valor, str) or not valor:
            raise ErroConfig("banco: precisa ser o nome de um arquivo, ex.: scanner.db")
        return valor
    # Sobraram os campos numericos.
    if not _e_numero(valor):
        raise ErroConfig(f"{campo}: precisa ser um numero")
    if campo in CAMPOS_POSITIVOS and valor <= 0:
        raise ErroConfig(f"{campo}: precisa ser maior que zero (valor recebido: {valor})")
    return valor


def carregar_config(caminho: str) -> Config:
    """Le o arquivo yaml em `caminho`, valida e devolve a Config.

    Campos ausentes ficam com o valor padrao. Qualquer problema levanta
    ErroConfig com o nome do campo (ou do arquivo) que precisa ser corrigido.
    """
    try:
        with open(caminho, encoding="utf-8") as arquivo:
            dados = yaml.safe_load(arquivo)
    except OSError as erro:
        raise ErroConfig(f"nao consegui abrir o arquivo de configuracao '{caminho}': {erro.strerror}") from erro
    except yaml.YAMLError as erro:
        raise ErroConfig(f"o arquivo '{caminho}' nao e um yaml valido: {erro}") from erro

    if dados is None:  # arquivo vazio: vale tudo o que e padrao
        dados = {}
    if not isinstance(dados, dict):
        raise ErroConfig(f"o arquivo '{caminho}' precisa ter campos no formato  nome: valor")

    conhecidos = {f.name for f in fields(Config)}
    valores = {}
    for campo, valor in dados.items():
        if campo not in conhecidos:
            raise ErroConfig(f"{campo}: campo desconhecido (erro de digitacao?)")
        valores[campo] = _validar_campo(campo, valor)
    return Config(**valores)
