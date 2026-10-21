# Plano de Implementação — Scanner de Arbitragem v1

> **Para quem executa:** siga as tarefas na ordem, marcando cada passo (`- [ ]`). Cada tarefa segue TDD: teste falhando → código mínimo → teste passando → commit.

**Objetivo:** scanner de sinais de arbitragem spot x futuros e futuros x futuros, só leitura, com painel web e modos Lucro e Farm.

**Arquitetura:** coletores por corretora (em duas etapas: ticker leve de todos os pares, livro completo só dos pares promissores) alimentam um estado em memória; um motor em ciclo de 500 ms calcula spreads, custos e métricas, aplica filtros e entrega sinais ao painel (WebSocket) e ao SQLite.

**Stack:** Python 3.12, asyncio, ccxt, FastAPI + uvicorn, SQLite (`sqlite3` da biblioteca padrão), PyYAML (leitura do config.yaml), pytest (testes assíncronos com `asyncio.run`, sem plugin extra), HTML/CSS/JS puro + Lightweight Charts (CDN).

**Spec:** `docs/especificacao.md` — leia junto com este plano; em caso de conflito, vale a especificação.

## Global Constraints

- Python 3.12; roda igual no Windows e no Linux; comando único `python -m scanner`.
- Nenhuma biblioteca fora da stack acima sem aprovação do dono. Exceção registrada: `httpx` só como dependência de teste em `requirements-dev.txt` (o `TestClient` do FastAPI precisa dela).
- Só leitura: nenhuma chamada de ordem, nenhuma chave de API na v1.
- Todo limite e padrão vem do `config.yaml` (com os padrões abaixo quando o campo não existe), nunca fixo no código.
- Percentuais internos em % (ex.: `0.40` = 0,40%). Datas em UTC ISO 8601 terminando em `Z`.
- Preços sempre em USDT por 1 unidade do ativo, depois da normalização.
- Código, nomes e comentários em português.
- Padrões: tamanho de referência $100 · conexão viva por até 2s sem mensagem · livro parado válido por até 30s · livro completo aberto a partir de 0,3% de spread de abertura e mantido 60s depois de cair abaixo · período 4h · RP mín. Lucro 0,2% · custo máx. Farm −0,1% · suspeito acima de 30% · anti-repetição 60s · ciclo 500 ms · amostra a cada 5s · retenção 7 dias · tempo estimado da operação 8h · margem extra de slippage 0%.
- Os testes automáticos nunca acessam a internet: corretoras são simuladas (objetos falsos no lugar do ccxt).

## Review Focus

1. **Mesmo ativo com multiplicador diferente** (PEPE x 1000PEPE): sem normalizar, aparece um spread falso de ~99.900%. Teste na Task 3.
2. **Livro mais raso que o tamanho de referência:** o preço médio não pode ser calculado com o que falta. Deve descartar. Teste na Task 5.
3. **Dado velho depois de reconexão / conexão em silêncio:** o último livro antes da queda não pode gerar sinal. Teste na Task 4.
4. **Intervalos de funding diferentes** (1h x 8h): o funding esperado precisa contar cobranças por ponta. Teste na Task 5.
5. **Par removido com sinal aberto:** o sinal deve ser encerrado, não ficar congelado na tabela. Teste na Task 8.

---

### Task 1: Base do projeto e configuração

**Arquivos:** criar `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `config.yaml`, `scanner/__init__.py`, `scanner/__main__.py`, `scanner/config/__init__.py`, `scanner/config/carregar.py`, `tests/__init__.py`, `tests/test_config.py`, `README.md`, `.gitignore` (inclui `.env`, `*.db`, `logs/`, `__pycache__/`, `.superpowers/`).

**Interfaces:**

- Produz: `carregar_config(caminho: str) -> Config` (dataclass congelada); levanta `ErroConfig` com o nome do campo inválido.
- Campos de `Config` e padrões (todos lidos do yaml quando presentes): `corretoras: list[str]` (padrão `["mexc", "gate", "bitget"]`), `corretoras_farm: list[str]` (padrão `[]`), `tamanho_referencia_usd=100`, `max_silencio_conexao_s=2`, `max_idade_livro_s=30`, `spread_min_livro_pct=0.3`, `manter_livro_s=60`, `periodo_metricas_h=4`, `rp_min_lucro_pct=0.2`, `custo_max_farm_pct=-0.1`, `limite_suspeito_pct=30`, `anti_repeticao_s=60`, `ciclo_ms=500`, `amostra_s=5`, `retencao_dias=7`, `tempo_operacao_h=8`, `margem_slippage_pct=0.0`, `metrica_spread="medio"` (`medio|maior|media_10_maiores`), `taxas: dict` (sobrescrita por corretora/mercado, padrão vazio), `intervalo_dados_lentos_s=60`, `recarregar_mercados_s=3600`, `painel: dict` (`host="127.0.0.1"`, `porta=8000`, `usuario=None`, `senha=None`), `banco="scanner.db"`, `nivel_log="normal"`.
- Valores numéricos que precisam ser positivos (tamanho, idades, períodos, ciclo, retenção) inválidos → `ErroConfig`.

- [ ] **Passo 1: teste falhando**

```python
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
```

- [ ] **Passo 2:** `pytest tests/test_config.py -v` → FALHA (módulo não existe)
- [ ] **Passo 3:** implementar `carregar_config` com PyYAML; `__main__.py` só carrega a config e imprime "config ok"
- [ ] **Passo 4:** `pytest tests/test_config.py -v` → PASSA; `python -m scanner` imprime "config ok"
- [ ] **Passo 5:** `git commit -m "feat: base do projeto e configuracao"`

### Task 2: Coletor de uma corretora (Bitget)

**Arquivos:** criar `scanner/modelos.py`, `scanner/coletores/__init__.py`, `scanner/coletores/base.py`, `scanner/coletores/ccxt_coletor.py`, `scanner/ferramentas/__init__.py`, `scanner/ferramentas/imprimir_precos.py`, `tests/test_coletor.py`.

**Interfaces:**

- Produz: `Livro` (dataclass: `corretora: str, mercado: Literal['spot','futuro'], simbolo_original: str, bids: list[tuple[float,float]], asks: list[tuple[float,float]], recebido_em: float, ts_corretora: float | None`).
- Produz: `livro_de_ccxt(corretora, mercado, simbolo, ob: dict, agora: float) -> Livro | None` — `None` se livro vazio, preço ≤ 0 ou melhor bid ≥ melhor ask. `ts_corretora` em segundos (o ccxt entrega ms).
- Produz: `class Coletor` (base abstrata) e `class ColetorCcxt(Coletor)` com `async def iniciar(self, simbolos: list[str], ao_atualizar: Callable[[Livro], None])` usando `watch_order_book` do ccxt. O objeto da corretora ccxt é injetável no construtor (para os testes usarem um falso).

- [ ] **Passo 1: teste falhando**

```python
def test_livro_valido():
    ob = {"bids": [[89.10, 2.0]], "asks": [[89.14, 1.5]], "timestamp": 1760972652000}
    l = livro_de_ccxt("bitget", "futuro", "AKAM/USDT:USDT", ob, agora=1760972652.5)
    assert l.bids[0] == (89.10, 2.0) and l.asks[0] == (89.14, 1.5)
    assert l.ts_corretora == 1760972652.0

def test_livro_cruzado_descartado():
    ob = {"bids": [[90.0, 1]], "asks": [[89.0, 1]], "timestamp": None}
    assert livro_de_ccxt("bitget", "spot", "X/USDT", ob, agora=0) is None

def test_livro_vazio_descartado():
    assert livro_de_ccxt("bitget", "spot", "X/USDT", {"bids": [], "asks": []}, agora=0) is None
```

- [ ] **Passo 2:** `pytest tests/test_coletor.py -v` → FALHA
- [ ] **Passo 3:** implementar; `imprimir_precos.py` conecta na corretora passada como argumento (spot e futuro de BTC, ETH, SOL) e imprime bid/ask a cada 5s
- [ ] **Passo 4:** testes PASSAM. **Marco (no PC do dono): `python -m scanner.ferramentas.imprimir_precos bitget` imprime preços que batem com o site da Bitget**
- [ ] **Passo 5:** `git commit -m "feat: coletor ccxt com bitget"`

### Task 3: Normalizador

**Arquivos:** criar `scanner/normalizador/__init__.py`, `scanner/normalizador/pares.py`, `tests/test_normalizador.py`.

**Interfaces:**

- Consome: `market` do ccxt (`base`, `quote`, `contractSize`).
- Produz: `normalizar(corretora: str, market: dict) -> ParNormalizado` (dataclass: `ativo: str, cotacao: str, multiplicador: float, tamanho_contrato: float`).
- Produz: `preco_unitario_usdt(preco: float, par: ParNormalizado, cotacoes: dict[str, float]) -> tuple[float, bool]` — retorna (preço por 1 unidade em USDT, `aviso_cotacao`).

- [ ] **Passo 1: teste falhando**

```python
def test_prefixo_1000():
    p = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 1})
    assert p.ativo == "PEPE" and p.multiplicador == 1000

def test_prefixo_k_hyperliquid():
    p = normalizar("hyperliquid", {"base": "kPEPE", "quote": "USDC", "contractSize": 1})
    assert p.ativo == "PEPE" and p.multiplicador == 1000 and p.cotacao == "USDC"

def test_prefixo_1m():
    p = normalizar("mexc", {"base": "1MBABYDOGE", "quote": "USDT", "contractSize": 1})
    assert p.ativo == "BABYDOGE" and p.multiplicador == 1_000_000

def test_k_maiusculo_nao_e_prefixo():
    assert normalizar("gate", {"base": "KAS", "quote": "USDT", "contractSize": 1}).ativo == "KAS"
    assert normalizar("gate", {"base": "KAVA", "quote": "USDT", "contractSize": 1}).multiplicador == 1

def test_mesmo_ativo_mesmo_preco():  # Foco de revisão 1
    a = normalizar("gate", {"base": "PEPE", "quote": "USDT", "contractSize": 1})
    b = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 1})
    pa, _ = preco_unitario_usdt(0.00001, a, {})
    pb, _ = preco_unitario_usdt(0.01, b, {})
    assert pa == pytest.approx(pb)

def test_conversao_usdc():
    p = normalizar("hyperliquid", {"base": "BTC", "quote": "USDC", "contractSize": 1})
    assert preco_unitario_usdt(100000.0, p, {"USDC": 0.9998}) == (pytest.approx(99980.0), False)

def test_sem_cotacao_usa_1_e_avisa():
    p = normalizar("lighter_rh", {"base": "BTC", "quote": "USDG", "contractSize": 1})
    assert preco_unitario_usdt(100000.0, p, {}) == (100000.0, True)
```

- [ ] **Passo 2:** `pytest tests/test_normalizador.py -v` → FALHA
- [ ] **Passo 3:** implementar; prefixos reconhecidos: `1000000`, `10000`, `1000` e `1M` seguidos de letra maiúscula, e `k` **minúsculo** seguido de letra maiúscula (multiplicador 1000); USDT nunca precisa de cotação
- [ ] **Passo 4:** testes PASSAM
- [ ] **Passo 5:** `git commit -m "feat: normalizador de pares"`

### Task 4: Estado em memória, duas etapas e fase 1 de corretoras

**Arquivos:** criar `scanner/estado/__init__.py`, `scanner/estado/mercados.py`, `scanner/coletores/dados_lentos.py`, `tests/fabricas.py`, `tests/test_estado.py`; alterar `scanner/coletores/ccxt_coletor.py` e `config.yaml` (MEXC, Gate, Bitget).

**Interfaces:**

- Consome: `Livro` (Task 2).
- Produz em `EstadoMercados(max_silencio_s: float = 2, max_idade_livro_s: float = 30)` (valores vêm da config): `atualizar_ticker(corretora, mercado, ativo, bid, ask, recebido_em, volume_24h_usd=None, variacao_24h_pct=None)` (etapa 1), `atualizar_livro(livro: Livro)` (etapa 2), `registrar_mensagem(corretora, agora)` (qualquer mensagem, inclusive heartbeat), `conexao_viva(corretora, agora) -> bool`, `obter_livro(corretora, mercado, ativo, agora) -> Livro | None` (`None` se a conexão não estiver viva ou o livro for mais velho que `max_idade_livro_s`), `obter_ticker(corretora, mercado, ativo, agora) -> dict | None` e `marcar_desconectada(corretora)`. O livro é guardado pela chave (corretora, mercado, ativo normalizado); `atualizar_livro` recebe o ativo normalizado no parâmetro opcional `ativo` (padrão: `simbolo_original`).
- Produz: `pares_para_livro(spreads_ticker: dict[str, float], minimo_pct: float, ultima_vez_acima: dict[str, float], agora: float, manter_s: float = 60) -> set[str]` — etapa 2: pares cujo spread de abertura pelo ticker está acima do mínimo, mantidos por `manter_s` depois de cair abaixo. Atualiza `ultima_vez_acima` para os que estão acima.
- `ColetorCcxt` ganha `async def iniciar_tickers(simbolos, ao_ticker)` (etapa 1, `watch_tickers` ou `watch_bids_asks` quando a corretora suportar) e `async def assinar_livros(simbolos: set[str])` (etapa 2, cancela os que saíram); toda mensagem recebida chama `registrar_mensagem`.
- Produz: `DadosLentos(taxas_config: dict)` com `funding(corretora, ativo) -> tuple[float, int] | None` (taxa %, intervalo h), `taxa_taker(corretora, mercado) -> float` e `atualizar(...)` por REST no intervalo da config; a taxa do `config.yaml` tem prioridade sobre a da API (`_taxas_api`).
- `tests/fabricas.py` concentra os helpers de teste (`livro(...)`, e os das tarefas seguintes).

- [ ] **Passo 1: teste falhando**

```python
def test_conexao_silenciosa_bloqueia():  # Foco de revisão 3
    e = EstadoMercados(); e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 100.0)
    assert e.obter_livro("gate", "spot", "BTC", agora=101.9) is not None
    assert e.obter_livro("gate", "spot", "BTC", agora=102.1) is None

def test_livro_parado_vale_30s_com_conexao_viva():
    e = EstadoMercados(); e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 125.0)
    assert e.obter_livro("gate", "spot", "BTC", agora=126.0) is not None
    e.registrar_mensagem("gate", 131.0)
    assert e.obter_livro("gate", "spot", "BTC", agora=131.5) is None

def test_corretora_desconectada_sem_dado():
    e = EstadoMercados(); e.atualizar_livro(livro("gate", "spot", "BTC", recebido_em=100.0))
    e.registrar_mensagem("gate", 100.0); e.marcar_desconectada("gate")
    assert e.obter_livro("gate", "spot", "BTC", agora=100.1) is None

def test_pares_para_livro():
    assert pares_para_livro({"A": 0.5, "B": 0.1}, 0.3, {}, agora=0) == {"A"}
    assert pares_para_livro({"A": 0.1}, 0.3, {"A": 0.0}, agora=50) == {"A"}
    assert pares_para_livro({"A": 0.1}, 0.3, {"A": 0.0}, agora=61) == set()

def test_taxa_config_tem_prioridade():
    d = DadosLentos(taxas_config={("mexc", "futuro"): 0.02}); d._taxas_api[("mexc", "futuro")] = 0.05
    assert d.taxa_taker("mexc", "futuro") == 0.02
```

- [ ] **Passo 2:** `pytest tests/test_estado.py -v` → FALHA
- [ ] **Passo 3:** implementar
- [ ] **Passo 4:** testes PASSAM. **Marco (no PC do dono): `imprimir_precos` roda com MEXC, Gate e Bitget ao mesmo tempo e os preços batem com os sites**
- [ ] **Passo 5:** `git commit -m "feat: estado em memoria, duas etapas e fase 1 de corretoras"`

### Task 5: Cálculo (spreads, custos, RP)

**Arquivos:** criar `scanner/calculo/__init__.py`, `scanner/calculo/formulas.py`, `tests/test_formulas.py`.

**Interfaces** (funções puras, tudo em %):

- `preco_execucao(niveis: list[tuple[float, float]], tamanho_usd: float) -> float | None` — preço médio comendo o livro até `tamanho_usd`; `None` se não houver liquidez suficiente.
- `spread_abertura(ask_long: float, bid_short: float) -> float` = (bid_short − ask_long) ÷ ask_long × 100
- `spread_fechamento(bid_long: float, ask_short: float) -> float` = (bid_long − ask_short) ÷ bid_long × 100
- `custo_taxas(taxa_long: float, taxa_short: float) -> float` = 2 × (taxa_long + taxa_short)
- `funding_esperado(fr_short, int_short_h, fr_long, int_long_h, horas: float) -> float` = fr_short × ⌊horas ÷ int_short_h⌋ − fr_long × ⌊horas ÷ int_long_h⌋; ponta com `fr = None` (spot) conta 0.
- `fr_yield_8h(fr_short, int_short_h, fr_long, int_long_h) -> float` = fr_short × 8 ÷ int_short_h − fr_long × 8 ÷ int_long_h (ponta `None` conta 0)
- `rp_bruto(a_atual: float, f_metrica: float) -> float` = a_atual + f_metrica
- `rp_liquido(rp_bruto, custo_taxas, margem_slippage, funding) -> float` = rp_bruto − custo_taxas − margem_slippage + funding

- [ ] **Passo 1: teste falhando**

```python
def test_preco_execucao_comendo_livro():
    assert preco_execucao([(100.0, 0.5), (101.0, 1.0)], 100) == pytest.approx(100.4975, abs=1e-4)

def test_livro_raso_descarta():  # Foco de revisão 2
    assert preco_execucao([(100.0, 0.5)], 100) is None

def test_spreads_do_arbimax():
    assert round(spread_abertura(89.14, 100.32), 2) == 12.54
    assert round(spread_fechamento(2.678, 3.0949), 2) == -15.57

def test_custos():
    assert custo_taxas(0.06, 0.06) == pytest.approx(0.24)

def test_funding_mesmo_intervalo():
    assert funding_esperado(0.01, 8, 0.0, 8, horas=8) == pytest.approx(0.01)

def test_funding_intervalos_diferentes():  # Foco de revisão 4
    assert funding_esperado(0.01, 1, 0.01, 8, horas=8) == pytest.approx(0.07)

def test_funding_ponta_spot():
    assert funding_esperado(0.01, 8, None, None, horas=8) == pytest.approx(0.01)

def test_rp_lucrativo():
    b = rp_bruto(12.54, -11.91)
    assert b == pytest.approx(0.63)
    assert rp_liquido(b, 0.24, 0.0, 0.01) == pytest.approx(0.40)

def test_rp_parece_bom_mas_nao_e():
    assert rp_liquido(0.15, 0.24, 0.0, 0.0) == pytest.approx(-0.09)
```

- [ ] **Passo 2:** `pytest tests/test_formulas.py -v` → FALHA
- [ ] **Passo 3:** implementar as funções
- [ ] **Passo 4:** testes PASSAM
- [ ] **Passo 5:** `git commit -m "feat: formulas de spread, custos e RP"`

### Task 6: Métricas do período

**Arquivos:** criar `scanner/metricas/__init__.py`, `scanner/metricas/janela.py`, `tests/test_metricas.py`.

**Interfaces:**

- Produz: `JanelaMetricas(periodo_s: float)` com `adicionar(ts: float, a: float, f: float)` e `resumo(agora: float, duracao_min_invertida_s: float) -> Metricas | None` (`None` sem amostras).
- Produz: `Metricas` (dataclass: `abertura_media, fechamento_medio, maior_abertura, maior_fechamento, media_10_maiores_abertura, media_10_maiores_fechamento: float, invertidas: int`).
- **Invertida:** sequência contínua de amostras com F ≥ 0 cuja duração (último ts − primeiro ts) ≥ `duracao_min_invertida_s`. Amostras mais antigas que `agora − periodo_s` são descartadas.

- [ ] **Passo 1: teste falhando**

```python
def janela_exemplo():
    j = JanelaMetricas(periodo_s=4 * 3600)
    for ts, f in zip(range(0, 35, 5), [-1, 0.1, 0.2, 0.1, -1, 0.5, -1]):
        j.adicionar(ts, a=1.0, f=f)
    return j

def test_invertidas_com_duracao_minima():
    assert janela_exemplo().resumo(agora=30, duracao_min_invertida_s=10).invertidas == 1
    assert janela_exemplo().resumo(agora=30, duracao_min_invertida_s=0).invertidas == 2

def test_maior_fechamento_e_o_menos_negativo():
    j = JanelaMetricas(periodo_s=100)
    for ts, f in [(0, -12.0), (5, -11.5), (10, -12.5)]:
        j.adicionar(ts, a=12.0, f=f)
    m = j.resumo(agora=10, duracao_min_invertida_s=0)
    assert m.maior_fechamento == -11.5 and m.fechamento_medio == pytest.approx(-12.0)

def test_descarta_fora_do_periodo():
    j = JanelaMetricas(periodo_s=10); j.adicionar(0, 5.0, -1.0); j.adicionar(20, 1.0, -1.0)
    assert j.resumo(agora=20, duracao_min_invertida_s=0).abertura_media == 1.0
```

- [ ] **Passo 2:** `pytest tests/test_metricas.py -v` → FALHA
- [ ] **Passo 3:** implementar com `collections.deque`
- [ ] **Passo 4:** testes PASSAM
- [ ] **Passo 5:** `git commit -m "feat: metricas do periodo e invertidas"`

### Task 7: Filtros e modos

**Arquivos:** alterar `scanner/modelos.py` (acrescentar `Ponta` e `Candidato`), criar `scanner/filtros/__init__.py`, `scanner/filtros/filtros.py`, `tests/test_filtros.py`; alterar `tests/fabricas.py`.

**Interfaces:**

- Produz: `Ponta` (dataclass com exatamente os campos de `ponta_long`/`ponta_short` do JSON da seção 5 da especificação).
- Produz: `Candidato` (dataclass com os mesmos campos do JSON da seção 5 da especificação, menos `id`, `detectado_em`, `atualizado_em`, `encerrado_em`; `ponta_long` e `ponta_short` são `Ponta`; `metricas_periodo` é `Metricas | None`, convertido para o formato do JSON na serialização). Propriedade `chave -> tuple` = (par, tipo, corretora e mercado do long, corretora e mercado do short).
- Produz: `Filtros` (dataclass; um campo por filtro da seção 4 da especificação, `None` = desligado; mais `mostrar_suspeitos: bool = False`, `lista_negra: list[str]`, `sempre_mostrar`, `nunca_mostrar`, `apenas_mostrar`) e `Filtros.de_dict(d: dict) -> Filtros` / `Filtros.para_dict()`.
- Produz: `passa(c: Candidato, f: Filtros, modo: Literal['lucro','farm'], cfg: Config) -> bool`. Lote mínimo obrigatório: `max(f.lote_min_usd or 0, cfg.tamanho_referencia_usd)` nas duas pontas.
- Produz: `custo_por_1000(rp_liquido_pct: float) -> float` = −rp_liquido_pct × 5.
- `tests/fabricas.py`: `cfg` (Config com padrões), `cfg_com(**campos)`, `candidato(rp_liquido=0.40, par="BTC/USDT", short="gate", lote_long=1000, suspeito=False, ...)`.

- [ ] **Passo 1: teste falhando**

```python
def test_lucro():
    assert passa(candidato(rp_liquido=0.40), Filtros(), "lucro", cfg)
    assert not passa(candidato(rp_liquido=0.15), Filtros(), "lucro", cfg)

def test_farm_exige_ponta_de_farm():
    cfg_farm = cfg_com(corretoras_farm=["lighter"])
    assert passa(candidato(rp_liquido=-0.09, short="lighter"), Filtros(), "farm", cfg_farm)
    assert not passa(candidato(rp_liquido=-0.09, short="gate"), Filtros(), "farm", cfg_farm)
    assert not passa(candidato(rp_liquido=-0.20, short="lighter"), Filtros(), "farm", cfg_farm)

def test_lote_minimo_obrigatorio():
    assert not passa(candidato(rp_liquido=0.40, lote_long=50), Filtros(), "lucro", cfg)

def test_suspeito_oculto_por_padrao():
    assert not passa(candidato(rp_liquido=0.40, suspeito=True), Filtros(), "lucro", cfg)
    assert passa(candidato(rp_liquido=0.40, suspeito=True), Filtros(mostrar_suspeitos=True), "lucro", cfg)

def test_lista_negra():
    assert not passa(candidato(rp_liquido=0.40, par="AKAM/USDT"), Filtros(lista_negra=["AKAM"]), "lucro", cfg)

def test_custo_por_1000():
    assert custo_por_1000(-0.09) == pytest.approx(0.45)
```

- [ ] **Passo 2:** `pytest tests/test_filtros.py -v` → FALHA
- [ ] **Passo 3:** implementar todos os filtros da seção 4 da especificação (cada um com pelo menos um teste próprio além destes)
- [ ] **Passo 4:** testes PASSAM
- [ ] **Passo 5:** `git commit -m "feat: filtros e modos lucro e farm"`

### Task 8: Gerenciador de sinais

**Arquivos:** criar `scanner/sinais/__init__.py`, `scanner/sinais/gerenciador.py`, `tests/test_sinais.py`.

**Interfaces:**

- Consome: `Candidato` (Task 7).
- Produz: `GerenciadorSinais(anti_repeticao_s: float)` com `processar(candidatos: list[Candidato], agora: float) -> list[Evento]` e `ativos() -> list[dict]`.
- Produz: `Evento` (dataclass: `tipo: Literal['novo','atualizado','encerrado'], sinal: dict, registrar_historico: bool`). `sinal` já está no formato JSON da especificação.
- Chave do sinal: `Candidato.chave`.

- [ ] **Passo 1: teste falhando**

```python
CAMPOS = {"id", "par", "tipo", "modo", "etiquetas", "suspeito", "aviso_cotacao", "ponta_long", "ponta_short",
          "spread_abertura_pct", "spread_fechamento_pct", "metricas_periodo", "fr_yield_8h_pct", "rp_bruto_pct",
          "custo_taxas_pct", "funding_esperado_pct", "rp_liquido_pct", "custo_por_1000_usd",
          "tamanho_referencia_usd", "detectado_em", "atualizado_em", "encerrado_em"}

def test_ciclo_de_vida():
    g = GerenciadorSinais(anti_repeticao_s=60); c = candidato(rp_liquido=0.40)
    assert [e.tipo for e in g.processar([c], agora=0)] == ["novo"]
    assert [e.tipo for e in g.processar([c], agora=1)] == ["atualizado"]
    ev = g.processar([], agora=2)
    assert ev[0].tipo == "encerrado" and ev[0].sinal["encerrado_em"] is not None

def test_par_removido_encerra():  # Foco de revisão 5
    g = GerenciadorSinais(60); g.processar([candidato(par="X/USDT")], agora=0)
    assert g.processar([], agora=1)[0].tipo == "encerrado"

def test_anti_repeticao_no_historico():
    g = GerenciadorSinais(60); c = candidato()
    g.processar([c], 0); g.processar([], 1)
    assert g.processar([c], 30)[0].registrar_historico is False
    g.processar([], 31)
    assert g.processar([c], 92)[0].registrar_historico is True

def test_formato_json():
    s = GerenciadorSinais(60).processar([candidato()], agora=0)[0].sinal
    assert set(s) == CAMPOS
    assert s["detectado_em"].endswith("Z")
```

- [ ] **Passo 2:** `pytest tests/test_sinais.py -v` → FALHA
- [ ] **Passo 3:** implementar
- [ ] **Passo 4:** testes PASSAM
- [ ] **Passo 5:** `git commit -m "feat: gerenciador de sinais"`

### Task 9: Armazenamento (SQLite)

**Arquivos:** criar `scanner/armazenamento/__init__.py`, `scanner/armazenamento/banco.py`, `tests/test_banco.py`.

**Interfaces:**

- Consome: `Evento` (Task 8).
- Produz: `Banco(caminho: str)` com `gravar_evento(e: Evento)` (só grava quando `registrar_historico` ou `tipo == 'encerrado'`; o encerrado atualiza o registro aberto do mesmo id), `sinais_do_dia(dia: str) -> list[dict]`, `gravar_amostra(chave: str, ts: float, a: float, f: float, preco_long: float, preco_short: float, lote_long: float, lote_short: float)`, `amostras(chave: str, desde: float) -> list[dict]`, `limpar_antigos(agora: float, dias: int) -> int`, `salvar_grupo(nome: str, filtros: dict)`, `listar_grupos() -> dict[str, dict]`, `apagar_grupo(nome: str)`.

- [ ] **Passo 1: teste falhando**

```python
def test_amostras_ida_e_volta(tmp_path):
    b = Banco(str(tmp_path / "s.db")); b.gravar_amostra("k", 100.0, 12.5, -12.6, 89.1, 100.3, 1800, 1400)
    assert b.amostras("k", desde=0)[0]["a"] == 12.5

def test_limpeza_7_dias(tmp_path):
    b = Banco(str(tmp_path / "s.db")); dia = 86400
    b.gravar_amostra("k", 0, 1, -1, 1, 1, 1, 1); b.gravar_amostra("k", 8 * dia, 1, -1, 1, 1, 1, 1)
    assert b.limpar_antigos(agora=8 * dia, dias=7) == 1

def test_grupos_de_filtros(tmp_path):
    b = Banco(str(tmp_path / "s.db")); b.salvar_grupo("Farm", {"volume_min_usd": 500})
    assert b.listar_grupos()["Farm"]["volume_min_usd"] == 500
    b.apagar_grupo("Farm"); assert "Farm" not in b.listar_grupos()
```

- [ ] **Passo 2:** `pytest tests/test_banco.py -v` → FALHA
- [ ] **Passo 3:** implementar com `sqlite3` (modo WAL)
- [ ] **Passo 4:** testes PASSAM
- [ ] **Passo 5:** `git commit -m "feat: armazenamento sqlite"`

### Task 10: Motor spot x futuros

**Arquivos:** criar `scanner/motor.py`, `tests/test_motor.py`; alterar `scanner/__main__.py` e `tests/fabricas.py`.

**Interfaces:**

- Consome: `EstadoMercados`, `DadosLentos`, `pares_para_livro` (Task 4), funções da Task 5, `JanelaMetricas` (Task 6), `passa` (Task 7), `GerenciadorSinais` (Task 8), `Banco` (Task 9).
- Produz: `pares_spot_futuro(mercados: dict[str, set[tuple[str, str]]]) -> list[tuple]` — todas as combinações (ativo, corretora do spot, "spot", corretora do futuro, "futuro") do mesmo ativo, **incluindo a mesma corretora**.
- Produz: `Motor(cfg, estado, dados_lentos, banco=None)` com `candidatos(agora: float) -> list[Candidato]` (antes do filtro; sem candidato se uma ponta não tiver livro válido ou liquidez para o tamanho de referência) e `ciclo(agora: float) -> list[Evento]` — filtra, passa ao gerenciador, grava eventos e, a cada `amostra_s`, amostras. Sessão atual (modo, filtros, comparação) em `Motor.sessao`.
- `__main__.py` sobe coletores + motor (ciclo de `ciclo_ms`) e registra no log a quantidade de sinais a cada minuto.
- `tests/fabricas.py`: `motor_falso(spot=None, futuro=None, long_futuro=None, short_futuro=None, taxas=0.06, f_medio=None, funding_short=None, funding_long=None, silencio_futuro_s=0)` — cada ponta é (corretora, ativo, preço), com profundidade suficiente para o tamanho de referência; `f_medio` pré-carrega a janela de métricas.

- [ ] **Passo 1: teste falhando**

```python
def test_pares_incluem_mesma_corretora():
    m = {"bitget": {("spot", "BTC"), ("futuro", "BTC")}}
    assert ("BTC", "bitget", "spot", "bitget", "futuro") in pares_spot_futuro(m)

def test_spot_sempre_e_o_long():
    for _, _, mercado_long, _, mercado_short in pares_spot_futuro({"gate": {("spot", "BTC")}, "mexc": {("futuro", "BTC")}}):
        assert mercado_long == "spot" and mercado_short == "futuro"

def test_candidato_spot_futuro():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), taxas=0.0)
    c = m.candidatos(agora=10)[0]
    assert c.tipo == "spot_futuro" and round(c.spread_abertura_pct, 2) == 1.0

def test_ponta_velha_nao_gera():
    m = motor_falso(spot=("gate", "BTC", 100.0), futuro=("bitget", "BTC", 101.0), silencio_futuro_s=3)
    assert m.candidatos(agora=10) == []

def test_suspeito_acima_de_30():
    m = motor_falso(spot=("gate", "X", 1.0), futuro=("bitget", "X", 1.5))
    assert m.candidatos(agora=10)[0].suspeito is True
```

- [ ] **Passo 2:** `pytest tests/test_motor.py -v` → FALHA
- [ ] **Passo 3:** implementar
- [ ] **Passo 4:** testes PASSAM. **Marco (no PC do dono): `python -m scanner` roda 30 min com MEXC, Gate e Bitget e o log mostra sinais; o dono confere 3 sinais nos sites**
- [ ] **Passo 5:** `git commit -m "feat: motor spot x futuros"`

### Task 11: API (FastAPI)

**Arquivos:** criar `scanner/api/__init__.py`, `scanner/api/app.py`, `tests/test_api.py`; alterar `requirements-dev.txt` (`httpx`).

**Interfaces:**

- Consome: `Motor` (Task 10), `Banco` (Task 9).
- Produz `criar_app(motor, banco, cfg) -> FastAPI` com:
    - `GET /api/sinais` → lista dos sinais ativos (JSON da especificação)
    - `GET /api/historico/{chave}?periodo_h=4&intencao=abertura|fechamento` → amostras + resumo (maior spread, média dos 10 maiores, médio, lote médio de cada ponta)
    - `GET /api/filtros`, `POST /api/filtros/{nome}`, `DELETE /api/filtros/{nome}`
    - `PUT /api/sessao` → `{modo, filtros, comparacao: bool}`
    - `GET /api/status` → por corretora: `conectada | reconectando | atrasada`, atraso médio, nº de reconexões
    - `WS /ws/sinais` → envia cada `Evento`
    - Usuário e senha (HTTP Basic) quando `painel.usuario` estiver no `config.yaml`

- [ ] **Passo 1: teste falhando**

```python
def test_sinais_ativos(cliente):
    r = cliente.get("/api/sinais"); assert r.status_code == 200 and isinstance(r.json(), list)

def test_grupo_de_filtros(cliente):
    cliente.post("/api/filtros/Farm", json={"volume_min_usd": 500})
    assert cliente.get("/api/filtros").json()["Farm"]["volume_min_usd"] == 500

def test_exige_senha_quando_configurada(cliente_com_senha):
    assert cliente_com_senha.get("/api/sinais").status_code == 401
    assert cliente_com_senha.get("/api/sinais", auth=("jr", "segredo")).status_code == 200
```

- [ ] **Passo 2:** `pytest tests/test_api.py -v` → FALHA
- [ ] **Passo 3:** implementar; os fixtures `cliente` usam o `TestClient` do FastAPI
- [ ] **Passo 4:** testes PASSAM
- [ ] **Passo 5:** `git commit -m "feat: api e websocket"`

### Task 12: Painel web

**Arquivos:** criar `scanner/painel/index.html`, `scanner/painel/app.js`, `scanner/painel/estilo.css`, `tests/test_painel.py`; alterar `scanner/api/app.py` (servir os arquivos estáticos em `/`).

**Interfaces:** consome os endpoints da Task 11. Lightweight Charts carregado por CDN.

- [ ] **Passo 1:** teste falhando: `GET /` devolve 200 com o HTML do painel, e `GET /app.js` e `GET /estilo.css` devolvem os arquivos
- [ ] **Passo 2:** tabela ao vivo pelo `WS /ws/sinais`, com as colunas e cores da seção 7 da especificação e sinais fixados no topo
- [ ] **Passo 3:** painel de filtros com todos os campos da seção 4, seletor de modo, grupos salvos (criar, editar, duplicar, apagar) e chave de modo comparação
- [ ] **Passo 4:** gráfico de histórico (abertura/fechamento, resumo no topo, tooltip com spread médio daquele ponto até agora)
- [ ] **Passo 5:** barra de status (última atualização, quantidade, saúde por corretora, pausar, busca)
- [ ] **Passo 6: verificação manual (no PC do dono)** — abrir `http://localhost:8000` e conferir:
    - um sinal novo aparece em até 1s depois de surgir no log
    - mudar um filtro atualiza a tabela sem recarregar
    - pausar congela a tabela; despausar volta
    - o gráfico de um sinal mostra as últimas 4h
    - horários aparecem em Brasília
- [ ] **Passo 7:** `git commit -m "feat: painel web"`

### Task 13: Futuros x futuros

**Arquivos:** alterar `scanner/motor.py`, `tests/test_motor.py`.

**Interfaces:**

- Produz: `pares_futuro_futuro(mercados) -> list[tuple]` — (ativo, corretora X, "futuro", corretora Y, "futuro") do mesmo ativo, X ≠ Y, nas duas direções.
- `Motor.candidatos` passa a gerar os dois tipos; o tipo ativo segue o filtro "tipos de oportunidade".

- [ ] **Passo 1: teste falhando**

```python
def test_nunca_mesma_corretora():
    for _, x, _, y, _ in pares_futuro_futuro({"gate": {("futuro", "BTC")}, "mexc": {("futuro", "BTC")}}):
        assert x != y

def test_duas_direcoes():
    p = pares_futuro_futuro({"gate": {("futuro", "BTC")}, "mexc": {("futuro", "BTC")}})
    assert ("BTC", "gate", "futuro", "mexc", "futuro") in p and ("BTC", "mexc", "futuro", "gate", "futuro") in p

def test_akam_ponta_a_ponta():
    m = motor_falso(long_futuro=("bitget", "AKAM", 89.14), short_futuro=("gate", "AKAM", 100.32),
                    f_medio=-11.91, taxas=0.06, funding_short=(0.01, 8), funding_long=(0.0, 8))
    c = m.candidatos(agora=10)[0]
    assert c.tipo == "futuro_futuro"
    assert round(c.rp_bruto_pct, 2) == 0.63 and round(c.rp_liquido_pct, 2) == 0.40
    assert round(c.fr_yield_8h_pct, 2) == 0.01
```

- [ ] **Passo 2:** `pytest tests/test_motor.py -v` → FALHA nos testes novos
- [ ] **Passo 3:** implementar
- [ ] **Passo 4:** todos os testes PASSAM
- [ ] **Passo 5:** `git commit -m "feat: futuros x futuros"`

### Task 14: Robustez (reconexão, limites, logs)

**Arquivos:** criar `scanner/coletores/reconexao.py`, `scanner/logs.py`, `tests/test_robustez.py`; alterar coletores e `__main__.py`.

**Interfaces:**

- Produz: `espera_reconexao(tentativa: int) -> float` = min(2^tentativa, 60).
- Produz: `selecionar_pares(pares: list[str], volumes: dict[str, float], limite: int) -> tuple[list[str], int]` — os de maior volume (ordem decrescente) e quantos ficaram de fora.
- Coletor desconectado chama `EstadoMercados.marcar_desconectada`; reconecta com `espera_reconexao`; lista de mercados recarregada a cada `recarregar_mercados_s`.
- Livro com sequência quebrada: o `watch_order_book` do ccxt já ressincroniza; o coletor só registra no log quando acontece.
- Logs diários com `logging.handlers.TimedRotatingFileHandler` em `logs/`; memória registrada a cada 5 min com `tracemalloc` (CPU fica de fora sem `psutil`).

- [ ] **Passo 1: teste falhando**

```python
def test_espera_crescente():
    assert [espera_reconexao(t) for t in (0, 1, 2, 6)] == [1, 2, 4, 60]

def test_prioriza_volume():
    sel, fora = selecionar_pares(["A", "B", "C"], {"A": 10, "B": 300, "C": 50}, limite=2)
    assert sel == ["B", "C"] and fora == 1
```

- [ ] **Passo 2:** `pytest tests/test_robustez.py -v` → FALHA
- [ ] **Passo 3:** implementar (inclui um teste com coletor falso que cai e volta: o estado marca desconectada e o coletor tenta de novo)
- [ ] **Passo 4:** testes PASSAM. **No PC do dono: testes técnicos da seção 9 da especificação** (internet desligada 1 min, 24h rodando)
- [ ] **Passo 5:** `git commit -m "feat: reconexao, limites e logs"`

### Task 15: Fase 2 de corretoras (BingX, XT, Hyperliquid)

**Arquivos:** alterar `config.yaml`, `scanner/coletores/dados_lentos.py`, `tests/test_normalizador.py`.

**Interfaces:**

- As 3 corretoras usam `ColetorCcxt` (Task 2).
- `DadosLentos.cotacoes() -> dict[str, float]` passa a trazer USDC/USDT de um mercado spot já coletado (padrão: Bitget; configurável em `config.yaml` como `fonte_cotacao`).

- [ ] **Passo 1: teste falhando**

```python
def test_hyperliquid_kpepe_contra_mexc():
    h = normalizar("hyperliquid", {"base": "kPEPE", "quote": "USDC", "contractSize": 1})
    m = normalizar("mexc", {"base": "1000PEPE", "quote": "USDT", "contractSize": 1})
    ph, aviso = preco_unitario_usdt(0.010, h, {"USDC": 1.0}); pm, _ = preco_unitario_usdt(0.010, m, {})
    assert ph == pytest.approx(pm) and aviso is False
```

- [ ] **Passo 2:** `pytest tests/test_normalizador.py -v` → FALHA (se o teste já passar, seguir para o Passo 4)
- [ ] **Passo 3:** ativar as corretoras e a cotação USDC/USDT (com teste de `cotacoes()` usando estado falso)
- [ ] **Passo 4:** testes PASSAM. **No PC do dono: `imprimir_precos` roda com as 6 corretoras e confere BingX, XT e Hyperliquid**
- [ ] **Passo 5:** `git commit -m "feat: fase 2 de corretoras"`

### Task 16: Fase 3 de corretoras (Kraken Pro e Lighter)

**Arquivos:** criar `scanner/coletores/lighter.py`, `tests/test_lighter.py`, `tests/dados/lighter_livro.json`; alterar `config.yaml`.

**Interfaces:**

- Kraken Pro: `ColetorCcxt` com `kraken` (spot) e `krakenfutures` (futuro).
- Produz: `ColetorLighter(Coletor)` com `instancia: Literal['principal','robinhood']`, usando o cliente HTTP/WebSocket do `aiohttp` (já vem com o ccxt).
- Produz: `livro_de_lighter(msg: dict, instancia: str, agora: float) -> Livro | None`, com as mesmas regras de descarte de `livro_de_ccxt`.
- Lighter principal cota em USDC; Lighter Robinhood em USDG. Sem cotação USDG/USDT disponível, usa 1:1 com `aviso_cotacao`.
- `corretoras_farm` no `config.yaml` de exemplo passa a incluir `lighter`, `lighter_rh` e `kraken`.
- Endpoints e formato das mensagens: **[PENDENTE: confirmar na documentação oficial da Lighter antes de codar]**. O fixture segue o formato documentado e é marcado para conferência com uma mensagem real no PC do dono.

- [ ] **Passo 1: teste falhando**

```python
def test_livro_lighter_real():
    msg = json.loads(Path("tests/dados/lighter_livro.json").read_text())
    l = livro_de_lighter(msg, "principal", agora=0)
    assert l is not None and l.bids[0][0] < l.asks[0][0]

def test_livro_lighter_cruzado_descartado():
    msg = json.loads(Path("tests/dados/lighter_livro.json").read_text())
    inverter_bids_e_asks(msg)  # helper do teste
    assert livro_de_lighter(msg, "principal", agora=0) is None
```

- [ ] **Passo 2:** `pytest tests/test_lighter.py -v` → FALHA
- [ ] **Passo 3:** implementar o conector e ativar Kraken e as duas Lighter
- [ ] **Passo 4:** testes PASSAM. **No PC do dono: o modo Farm mostra sinais com ponta na Lighter e os preços batem com os apps**
- [ ] **Passo 5:** `git commit -m "feat: fase 3 com kraken e lighter"`

### Task 17: Métricas de validação e documentação final

**Arquivos:** criar `scanner/validacao/__init__.py`, `scanner/validacao/relatorio.py`, `tests/test_relatorio.py`; alterar `scanner/api/app.py`, `scanner/__main__.py`, `README.md` e `config.yaml` (comentários de cada campo).

**Interfaces:**

- Consome: `Banco` (Task 9), status das corretoras (Task 11).
- Produz: `resumo_do_dia(banco: Banco, dia: str, status_corretoras: dict | None = None) -> dict` com, por modo: `quantidade_sinais`, `rp_liquido_medio_pct`, `duracao_media_s`, `pct_sumiu_5s`, `pct_sumiu_30s`; e em `"corretoras"`: `atraso_medio_ms`, `reconexoes` (vindos de `status_corretoras`).
- Produz: `GET /api/validacao?dia=AAAA-MM-DD`.
- `__main__.py` chama `Banco.limpar_antigos` uma vez por dia.
- `README.md` final: instalação e como rodar no Windows e na VPS, passo a passo para iniciante; o que conferir em cada marco; como ler o painel. `config.yaml` com comentário explicando cada campo.
- `tests/fabricas.py`: `banco_com_sinais(tmp_path, duracoes_s, rp_liquido, modo)`.

- [ ] **Passo 1: teste falhando**

```python
def test_duracoes(tmp_path):
    b = banco_com_sinais(tmp_path, duracoes_s=[2, 10, 40], rp_liquido=[0.3, 0.4, 0.5], modo="lucro")
    r = resumo_do_dia(b, "2026-10-20")["lucro"]
    assert r["quantidade_sinais"] == 3
    assert r["duracao_media_s"] == pytest.approx(17.33, abs=0.01)
    assert r["pct_sumiu_5s"] == pytest.approx(33.33, abs=0.01)
    assert r["pct_sumiu_30s"] == pytest.approx(66.67, abs=0.01)
    assert r["rp_liquido_medio_pct"] == pytest.approx(0.4)
```

- [ ] **Passo 2:** `pytest tests/test_relatorio.py -v` → FALHA
- [ ] **Passo 3:** implementar e completar README e config comentado
- [ ] **Passo 4:** testes PASSAM (suite inteira: `pytest -q`)
- [ ] **Passo 5:** `git commit -m "feat: metricas de validacao e documentacao"`
