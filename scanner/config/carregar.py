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
# Metodos do ccxt para os tickers da etapa 1 (veja METODOS_DE_TICKER no coletor), na ordem de preferencia.
METODOS_TICKER = ("watch_tickers", "watch_bids_asks", "watch_ticker")


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

    # Tempo maximo esperando a corretora confirmar o cancelamento da assinatura de um livro.
    timeout_desassinar_s: float = 5

    # Robustez (Task 14).
    # Conexao que cai tenta de novo esperando 1s, 2s, 4s... ate este maximo, em segundos.
    espera_max_reconexao_s: float = 60
    # Maximo de assinaturas por corretora, ex.: {"bingx": 200}. Vazio = sem limite. Vale para os
    # livros (etapa 2) e para os tickers quando a corretora so assina um par por vez (etapa 1).
    limites_assinatura: dict = field(default_factory=dict)
    # Atraso (chegada - horario informado pela corretora) maior que isto, para mais ou para menos,
    # so acontece com relogio errado: fica fora do atraso medio. Em segundos.
    atraso_max_valido_s: float = 30
    # Logs: pasta dos arquivos (um por dia, data local) e quantos dias anteriores guardar alem do de hoje.
    pasta_logs: str = "logs"
    dias_logs: int = 30
    # De quanto em quanto tempo o log registra memoria, CPU e o resumo dos descartes, em segundos.
    intervalo_log_recursos_s: float = 300
    # Revisao final da coleta.
    # Maximo de assinaturas por (corretora, mercado), contando juntos os tickers e os livros da mesma conexao,
    # ex.: {"mexc": {"spot": 30}} (a MEXC spot aceita 30 assinaturas por conexao). Vazio = sem limite por mercado.
    limites_assinatura_mercado: dict = field(default_factory=dict)
    # Quantas assinaturas de cada limite de limites_assinatura_mercado ficam para os livros (etapa 2); o resto
    # fica para os tickers (etapa 1).
    livros_reservados_limite_mercado: int = 10
    # Corretoras cujos livros sao assinados de novo de tempos em tempos (o ccxt delas nao confere a sequencia do
    # livro: MEXC e XT) e o intervalo, em segundos.
    reassinar_livros_corretoras: list = field(default_factory=lambda: ["mexc", "xt"])
    intervalo_reassinar_livros_s: float = 900
    # Pares pedidos que nao mandam nenhum ticker neste tempo depois de (re)abrir os tickers vao para um aviso no log.
    prazo_primeiro_ticker_s: float = 60

    # Fase 2 de corretoras (Task 15).
    # Mercados usados de cada corretora, ex.: {"hyperliquid": ("futuro",)}. Corretora fora daqui usa os dois.
    mercados_por_corretora: dict = field(default_factory=dict)
    # Metodo do ccxt que cada corretora tenta primeiro para os tickers, ex.: {"hyperliquid": "watch_ticker"}.
    metodo_ticker: dict = field(default_factory=dict)
    # Mensagens seguidas de tickers sem bid/ask (sem nenhum bom antes) para trocar de metodo de ticker.
    mensagens_sem_bid_ask: int = 10
    # Corretora cujo ticker SPOT da a cotacao das outras moedas (USDC/USDT...) e a excecao por moeda,
    # ex.: {"USD": "kraken"}.
    fonte_cotacao: str = "bitget"
    fontes_cotacao: dict = field(default_factory=dict)
    # Cotacao mais longe de 1 do que isto, em %, e tratada como sem cotacao (1:1 com aviso_cotacao).
    faixa_cotacao_pct: float = 10
    # Intervalo do funding em horas por corretora, ex.: {"hyperliquid": 1}. Vale sobre o que o ccxt informa.
    intervalo_funding_h: dict = field(default_factory=dict)

    # Fase 3 de corretoras (Task 16): enderecos da API publica da Lighter (REST e WebSocket) de cada instancia.
    # O WebSocket e sempre aberto so leitura (?readonly=true): o scanner nunca opera.
    url_rest_lighter: str = "https://mainnet.zklighter.elliot.ai"
    url_ws_lighter: str = "wss://mainnet.zklighter.elliot.ai/stream"
    url_rest_lighter_rh: str = "https://api.rh.lighter.xyz"
    url_ws_lighter_rh: str = "wss://api.rh.lighter.xyz/stream"
    # De quanto em quanto tempo o scanner manda {"type": "ping"} a Lighter, em segundos. O servidor fecha a
    # conexao que passa 2 minutos sem receber nada do cliente.
    intervalo_ping_lighter_s: float = 30
    # Sem nenhuma mensagem por tantos intervalos de ping (nem o pong), a conexao da Lighter e dada como morta.
    pings_sem_resposta_lighter: int = 2
    # Tempo maximo de uma chamada REST da Lighter e da abertura da conexao WebSocket, em segundos.
    tempo_max_rede_lighter_s: float = 10
    # Niveis de cada lado do livro da Lighter que vao ao estado (os melhores): a Lighter manda o livro inteiro.
    niveis_livro_lighter: int = 100
    # Livro pedido que nao recebe o livro completo em tantos segundos e pedido de novo (com espera crescente).
    tempo_max_livro_completo_lighter_s: float = 10
    # Mudancas de livro seguidas sem o livro completo antes delas para o log avisar (uma vez por par).
    mudancas_sem_livro_completo_lighter: int = 10
    # Tempo minimo em cada modo da etapa 1 da Lighter (market_stats ou livros) antes de trocar de novo, em segundos.
    tempo_min_modo_etapa1_lighter_s: float = 300
    # Intervalo minimo entre duas montagens do livro de um par da Lighter, em ms (as mudancas que chegam no meio
    # entram na montagem seguinte). 0 = monta a cada mensagem.
    intervalo_montar_livro_lighter_ms: float = 100

    # Painel web.
    painel: dict = field(default_factory=_painel_padrao)

    banco: str = "scanner.db"
    nivel_log: str = "normal"

    def mercados_da_corretora(self, corretora: str) -> tuple[str, ...]:
        """Os mercados usados da corretora: os de mercados_por_corretora ou, se ela nao estiver la, os dois."""
        return tuple(self.mercados_por_corretora.get(corretora, MERCADOS))


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
    "timeout_desassinar_s",
    "espera_max_reconexao_s",
    "atraso_max_valido_s",
    "intervalo_log_recursos_s",
    "faixa_cotacao_pct",
    "tempo_max_rede_lighter_s",
    "tempo_max_livro_completo_lighter_s",
    "tempo_min_modo_etapa1_lighter_s",
    "intervalo_reassinar_livros_s",
    "prazo_primeiro_ticker_s",
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


def _inteiro_positivo(valor) -> bool:
    """True para numero inteiro maior que zero (bool nao conta)."""
    return isinstance(valor, int) and not isinstance(valor, bool) and valor > 0


def _validar_limites_assinatura(valor) -> dict:
    """{corretora: maximo de assinaturas}; vazio (ou o campo sem nada) = sem limite."""
    if valor is None:
        return {}
    modelo = "limites_assinatura: use o formato  corretora: 200  (numero inteiro maior que zero)"
    if not isinstance(valor, dict):
        raise ErroConfig(modelo)
    for corretora, limite in valor.items():
        if not _inteiro_positivo(limite):
            raise ErroConfig(f"{modelo}; '{corretora}' esta com {limite!r}")
    return {str(corretora): limite for corretora, limite in valor.items()}


def _validar_limites_assinatura_mercado(valor) -> dict:
    """{corretora: {mercado: maximo de assinaturas}}; vazio (ou o campo sem nada) = sem limite por mercado."""
    if valor is None:
        return {}
    modelo = ("limites_assinatura_mercado: use o formato  corretora: {spot: 30}  (mercado spot ou futuro, numero "
              "inteiro maior que zero)")
    if not isinstance(valor, dict):
        raise ErroConfig(modelo)
    resultado = {}
    for corretora, limites in valor.items():
        if not isinstance(limites, dict) or not limites:
            raise ErroConfig(f"{modelo}; '{corretora}' esta com {limites!r}")
        for mercado, limite in limites.items():
            if mercado not in MERCADOS or not _inteiro_positivo(limite):
                raise ErroConfig(f"{modelo}; '{corretora}' esta com {mercado}: {limite!r}")
        resultado[str(corretora)] = dict(limites)
    return resultado


def _validar_mercados_por_corretora(valor) -> dict:
    """{corretora: (mercados,)}; vazio (ou o campo sem nada) = as duas para todas."""
    if valor is None:
        return {}
    modelo = "mercados_por_corretora: use o formato  corretora: [futuro]  (lista com spot e/ou futuro, sem repetir)"
    if not isinstance(valor, dict):
        raise ErroConfig(modelo)
    resultado = {}
    for corretora, mercados in valor.items():
        lista_ok = isinstance(mercados, list) and bool(mercados) and all(
            isinstance(m, str) and m in MERCADOS for m in mercados)
        if not lista_ok or len(set(mercados)) != len(mercados):
            raise ErroConfig(f"{modelo}; '{corretora}' esta com {mercados!r}")
        resultado[str(corretora)] = tuple(mercados)
    return resultado


def _validar_metodo_ticker(valor) -> dict:
    """{corretora: metodo do ccxt}; vazio = cada corretora usa a ordem padrao."""
    if valor is None:
        return {}
    modelo = f"metodo_ticker: use o formato  corretora: watch_ticker  (opcoes: {', '.join(METODOS_TICKER)})"
    if not isinstance(valor, dict):
        raise ErroConfig(modelo)
    for corretora, metodo in valor.items():
        if not isinstance(metodo, str) or metodo not in METODOS_TICKER:
            raise ErroConfig(f"{modelo}; '{corretora}' esta com {metodo!r}")
    return {str(corretora): metodo for corretora, metodo in valor.items()}


def _validar_intervalo_funding(valor) -> dict:
    """{corretora: horas inteiras > 0}; vazio = vale o que o ccxt informa (ou 8h)."""
    if valor is None:
        return {}
    modelo = "intervalo_funding_h: use o formato  corretora: 1  (horas inteiras, maior que zero)"
    if not isinstance(valor, dict):
        raise ErroConfig(modelo)
    for corretora, horas in valor.items():
        if not _inteiro_positivo(horas):
            raise ErroConfig(f"{modelo}; '{corretora}' esta com {horas!r}")
    return {str(corretora): horas for corretora, horas in valor.items()}


def _validar_fontes_cotacao(valor) -> dict:
    """{MOEDA: corretora}, com a moeda em maiuscula (como o ccxt escreve); vazio = todas da fonte_cotacao."""
    if valor is None:
        return {}
    modelo = "fontes_cotacao: use o formato  MOEDA: corretora  (ex.: USD: kraken)"
    if not isinstance(valor, dict):
        raise ErroConfig(modelo)
    resultado = {}
    for moeda, corretora in valor.items():
        if not isinstance(moeda, str) or not moeda or not isinstance(corretora, str) or not corretora:
            raise ErroConfig(f"{modelo}; '{moeda}' esta com {corretora!r}")
        if moeda.upper() == "USDT":
            raise ErroConfig("fontes_cotacao: USDT e a moeda base do scanner e nao precisa de cotacao")
        resultado[moeda.upper()] = corretora
    return resultado


# Fase 3 (Task 16): enderecos da Lighter. REST com http(s)://, WebSocket com ws(s)://.
CAMPOS_URL_LIGHTER = {
    "url_rest_lighter": ("https://", "http://"),
    "url_ws_lighter": ("wss://", "ws://"),
    "url_rest_lighter_rh": ("https://", "http://"),
    "url_ws_lighter_rh": ("wss://", "ws://"),
}
# O servidor da Lighter fecha a conexao depois de 2 minutos sem nada do cliente: o ping precisa vir antes.
LIMITE_PING_LIGHTER_S = 120
# Campos da Lighter que sao numero inteiro maior que zero.
CAMPOS_INTEIROS_LIGHTER = ("pings_sem_resposta_lighter", "niveis_livro_lighter", "mudancas_sem_livro_completo_lighter")


def _validar_url_lighter(campo: str, valor) -> str:
    inicios = CAMPOS_URL_LIGHTER[campo]
    if not isinstance(valor, str) or not valor.startswith(inicios) or len(valor) <= len(inicios[0]):
        raise ErroConfig(f"{campo}: precisa ser um endereco que comeca com {' ou '.join(inicios)} "
                         f"(valor recebido: {valor!r})")
    return valor


def _validar_ping_lighter(valor) -> float:
    if not _e_numero(valor) or not 0 < valor < LIMITE_PING_LIGHTER_S:
        raise ErroConfig(f"intervalo_ping_lighter_s: precisa ser um numero maior que zero e menor que "
                         f"{LIMITE_PING_LIGHTER_S} (o servidor fecha a conexao em 2 minutos sem nada do cliente); "
                         f"valor recebido: {valor!r}")
    return valor


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
    if campo in ("corretoras_farm", "reassinar_livros_corretoras"):
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
    if campo == "limites_assinatura":
        return _validar_limites_assinatura(valor)
    if campo == "limites_assinatura_mercado":
        return _validar_limites_assinatura_mercado(valor)
    if campo == "pasta_logs":
        if not isinstance(valor, str) or not valor:
            raise ErroConfig("pasta_logs: precisa ser o nome de uma pasta, ex.: logs")
        return valor
    if campo in ("dias_logs", "mensagens_sem_bid_ask", "livros_reservados_limite_mercado", *CAMPOS_INTEIROS_LIGHTER):
        if not _inteiro_positivo(valor):
            raise ErroConfig(f"{campo}: precisa ser um numero inteiro maior que zero (valor recebido: {valor!r})")
        return valor
    if campo == "mercados_por_corretora":
        return _validar_mercados_por_corretora(valor)
    if campo == "metodo_ticker":
        return _validar_metodo_ticker(valor)
    if campo == "intervalo_funding_h":
        return _validar_intervalo_funding(valor)
    if campo == "fontes_cotacao":
        return _validar_fontes_cotacao(valor)
    if campo == "fonte_cotacao":
        if not isinstance(valor, str) or not valor:
            raise ErroConfig("fonte_cotacao: precisa ser o nome de uma corretora com spot, ex.: bitget")
        return valor
    if campo in CAMPOS_URL_LIGHTER:  # Task 16
        return _validar_url_lighter(campo, valor)
    if campo == "intervalo_ping_lighter_s":
        return _validar_ping_lighter(valor)
    if campo == "intervalo_montar_livro_lighter_ms":
        if not _e_numero(valor) or valor < 0:
            raise ErroConfig(f"{campo}: precisa ser um numero maior ou igual a zero (0 = monta o livro a cada "
                             f"mensagem); valor recebido: {valor!r}")
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
    config = Config(**valores)
    _conferir_nomes_das_corretoras(config)
    _conferir_fontes_de_cotacao(config, fonte_escrita="fonte_cotacao" in dados)
    _conferir_reserva_dos_livros(config)
    return config


# Campos {corretora: valor}: o nome da corretora precisa estar em `corretoras`.
CAMPOS_POR_CORRETORA = ("limites_assinatura", "mercados_por_corretora", "metodo_ticker", "intervalo_funding_h",
                        "limites_assinatura_mercado")


def _conferir_nomes_das_corretoras(config: Config) -> None:
    """Campos por corretora (veja CAMPOS_POR_CORRETORA) so com corretoras que estao em `corretoras`.

    Um nome com erro de digitacao seria ignorado em silencio (o ajuste ficaria desligado).
    """
    for campo in CAMPOS_POR_CORRETORA:
        fora = sorted(set(getattr(config, campo)) - set(config.corretoras))
        if fora:
            raise ErroConfig(f"{campo}: {', '.join(fora)} nao esta em corretoras (erro de digitacao?); "
                             f"corretoras: {', '.join(config.corretoras)}")


def _conferir_reserva_dos_livros(config: Config) -> None:
    """Cada limite de limites_assinatura_mercado precisa ser maior que livros_reservados_limite_mercado.

    Os livros ficam com a reserva e os tickers com o resto: sem sobra, o mercado ficaria sem nenhum ticker.
    """
    for corretora, limites in config.limites_assinatura_mercado.items():
        for mercado, limite in limites.items():
            if limite <= config.livros_reservados_limite_mercado:
                raise ErroConfig(f"limites_assinatura_mercado: {corretora} {mercado} com {limite} assinaturas nao "
                                 f"sobra nada para os tickers depois das {config.livros_reservados_limite_mercado} "
                                 "reservadas aos livros (livros_reservados_limite_mercado); aumente o limite ou "
                                 "diminua a reserva")


def _conferir_fontes_de_cotacao(config: Config, fonte_escrita: bool) -> None:
    """A fonte da cotacao (e a de cada moeda em fontes_cotacao) precisa estar em `corretoras` e ter spot.

    A cotacao vem do ticker SPOT da fonte (ex.: USDC/USDT da Bitget). A fonte padrao (bitget) so e
    conferida se foi escrita no config.yaml ou se a Bitget esta ligada: com outras corretoras (ex.: so
    [gate, mexc]) nao e erro; sem a fonte, as moedas fora de USDT usam 1:1 com aviso_cotacao (e o log avisa).
    """
    for moeda, corretora in config.fontes_cotacao.items():
        _conferir_fonte(f"fontes_cotacao ({moeda})", corretora, config)
    if fonte_escrita or config.fonte_cotacao in config.corretoras:
        campo = "fonte_cotacao" if fonte_escrita else "fonte_cotacao (padrao)"
        _conferir_fonte(campo, config.fonte_cotacao, config)


def _conferir_fonte(campo: str, corretora: str, config: Config) -> None:
    if corretora not in config.corretoras:
        raise ErroConfig(f"{campo}: {corretora} nao esta em corretoras (erro de digitacao?); "
                         f"corretoras: {', '.join(config.corretoras)}")
    if "spot" not in config.mercados_da_corretora(corretora):
        raise ErroConfig(f"{campo}: {corretora} esta sem spot em mercados_por_corretora, e a cotacao vem do "
                         "ticker spot da fonte; escolha outra corretora")
