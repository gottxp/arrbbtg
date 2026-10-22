# Especificação — Scanner de Arbitragem v1

21/10/2026 · Ambição Digital

## 1. Visão geral e escopo

A v1 é um scanner de oportunidades de arbitragem cripto entre corretoras, no estilo do Arbimax, para uso próprio e só em modo leitura. Ele compara o preço do mesmo ativo em mercados diferentes, calcula o resultado já descontando custos e mostra os sinais num painel web ao vivo.

**Tipos de oportunidade, nesta ordem de construção:**

1. **Spot x Futuros:** compra no spot de um mercado e short no futuro de outro.
2. **Futuros x Futuros:** long no futuro de uma corretora e short no futuro de outra. Só começa depois que o tipo 1 estiver testado.

**Modos de uso:**

- **Lucro:** achar operações com lucro líquido positivo.
- **Farm:** gerar volume nas corretoras que dão pontos de airdrop, gastando o mínimo.

**Regras fixas:**

- **Só leitura.** Não envia ordens e não usa chave de API; só dados públicos.
- **Referência:** o Arbimax. As fórmulas foram conferidas com os números dele, e ele é o gabarito da validação (seção 9).

**Fora da v1:** notificador de fechamento (acompanhar posições abertas), execução automática de ordens, alertas no Telegram, calculadora e alertas sonoros.

**Critério de sucesso:** depois de 3 dias rodando, os sinais batem com o Arbimax e com os preços reais nas corretoras, dentro dos critérios da seção 9.

## 2. Corretoras e pares

As corretoras entram em 3 fases; cada fase só começa depois que a anterior estiver coletando preço certo.

| Fase | Corretora | Mercados |
| --- | --- | --- |
| 1 | MEXC | Spot e futuros |
| 1 | Gate | Spot e futuros |
| 1 | Bitget | Spot e futuros |
| 2 | BingX | Spot e futuros |
| 2 | XT | Spot e futuros |
| 2 | Hyperliquid | Só futuros (cotação em USDC) |
| 3 | Kraken Pro | Spot e futuros |
| 3 | Lighter (principal) | Só futuros (cotação em USDC) |
| 3 | Lighter Robinhood | Só futuros (cotação em USDG) |

**Quais pares escanear:**

- Todos os pares que existem em pelo menos 2 mercados das corretoras ativas.
- **Spot x Futuros** pode ser na mesma corretora (ex.: spot da Bitget x futuro da Bitget), como no Arbimax.
- **Futuros x Futuros** é sempre entre corretoras diferentes.
- Pares abaixo do volume mínimo e os da lista negra ficam de fora.

**Normalização (para comparar o mesmo ativo):**

- **Prefixos de multiplicador:** 1000PEPE, 1MBABYDOGE e kPEPE viram o ativo base (o k só vale minúsculo, para não confundir com ativos como KAS ou KAVA), e o preço é ajustado pelo multiplicador.
- **Tamanho de contrato:** o preço é sempre convertido para o valor de 1 unidade do ativo.
- **Moeda de cotação:** a base é USDT. Preços em USDC e USDG são convertidos pela cotação real (USDC/USDT, USDG/USDT). Sem cotação disponível, usa 1:1 e marca o sinal com `aviso_cotacao = true`.
- **Mesmo nome, ativo diferente:** se a diferença entre os preços das duas pontas passar de um limite (padrão 30%, configurável), nos dois sentidos (pela razão dos preços, `(maior ÷ menor − 1) × 100`), o sinal é marcado como **suspeito**. (Revisão final: só o A positivo deixava passar o token diferente na direção em que o long é o caro.)
- **Ações tokenizadas** recebem a etiqueta STOCK.

## 3. Fórmulas e custos

O número que decide um sinal é o **RP líquido**: o resultado projetado depois de taxas, slippage e funding. As fórmulas de A, F e RP bruto foram conferidas com os números do Arbimax.

**Preço de execução:**

- A ponta **long** compra no **ask**; a ponta **short** vende no **bid**.
- Usa o preço médio comendo o livro até o **tamanho de referência** (configurável, padrão $100). Isso já embute o slippage.
- Se o livro não tem liquidez para esse tamanho, o sinal é descartado.

**Spreads:**

```
A = (bid_short − ask_long) / ask_long
F = (bid_long − ask_short) / bid_long
```

**Métricas do período** (janela configurável, padrão 4h): A médio, F médio, maior A, maior F e média dos 10 maiores.

**De onde vêm as amostras das métricas** (alteração aprovada pelo dono em 22/10/2026): para o F médio de um spread que acabou de abrir refletir o período inteiro, como no Arbimax, todo par com ticker nas duas pontas é amostrado pelos tickers (melhor bid/ask, convertido para USDT) a cada `amostra_ticker_s` (padrão 120 s; com período maior que 4h o intervalo cresce junto, para a memória não crescer). Quando o par tem livro aberto, a amostra passa a vir do livro (preço de execução para o tamanho de referência) a cada `amostra_s` (padrão 5 s). As duas alimentam a mesma janela do período, e as médias são ponderadas pelo tempo que cada amostra cobre (uma amostra pelo ticker de 120 s vale 24 amostras do livro de 5 s), para a média continuar sendo a média no tempo do período. Os maiores, a média dos 10 maiores e as invertidas usam as amostras como vieram (na resolução do ticker quando não há livro). As amostras pelos tickers ficam só na memória (não vão ao SQLite nem ao gráfico); a memória a mais esperada é de ~100 a 400 MB. O lote médio vem só das amostras do livro.

**Resultado projetado:**

- **RP bruto** = A atual + F da métrica escolhida (padrão: F médio, como no Arbimax; pode trocar por maior F ou média dos 10 maiores).
- **RP líquido** = RP bruto − taxas − margem extra de slippage + funding esperado.

**Custos:**

- **Taxas:** 4 ordens taker (abrir e fechar nas duas pontas). A taxa de cada corretora vem da API e pode ser sobrescrita no `config.yaml` (nível VIP, descontos).
- **Margem extra de slippage:** configurável, padrão 0%.
- **Funding esperado** = (funding do short − funding do long) × número de cobranças no tempo estimado da operação (configurável, padrão 8h). Considera o intervalo de cada corretora (1h, 4h, 8h). Ponta spot não tem funding. É uma aproximação: não considera o horário da próxima cobrança.
- **FR Yield** no painel é normalizado para 8h, para comparar corretoras com intervalos diferentes.

**Restrição do Spot x Futuros:** o spot é sempre a ponta comprada e o futuro a vendida. O inverso precisaria de empréstimo de margem e fica fora da v1.

**Exemplos para os testes** (taxa de 0,06% por ordem = 0,24% nas 4):

| Caso | A atual | F médio | RP bruto | Taxas | Funding | RP líquido | Resultado |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Lucrativo (AKAM do print) | 12,54% | −11,91% | 0,63% | 0,24% | +0,01% | 0,40% | Aparece no modo Lucro |
| Parece bom, mas não é | — | — | 0,15% | 0,24% | 0% | −0,09% | Só aparece no modo Farm |

## 4. Filtros, modos e condição de sinal

Um sinal só aparece se passar em todos os filtros ativos, na regra do modo e nas checagens de liquidez e de dado atualizado. Todos os filtros são configuráveis; campo vazio = desligado.

| Grupo | Filtro | Unidade |
| --- | --- | --- |
| Corretoras | Corretoras do lado 1, do lado 2 e tipos de oportunidade | lista |
| Geral | Período das métricas | h |
| Geral | Volume mínimo 24h, nas duas pontas | $ |
| Geral | Spread mínimo (A atual) | % |
| Liquidez | Lote mínimo por ponta (no livro agora) | $ |
| Liquidez | Lote médio mínimo (no período) | $ |
| Liquidez | Limite de ordem mínimo (tamanho máximo que a corretora aceita) | $ |
| Spreads do período | Maior A mín., maior F mín., A médio mín., F médio mín. | % |
| Funding | Funding mínimo e máximo por ponta | % |
| Funding | FR Yield mínimo | % |
| Funding | Intervalo de cobrança (1h, 4h, 8h) | lista |
| Funding | Sincronizado (as duas pontas cobram no mesmo horário) | sim/não |
| Invertidas | Quantidade mínima no período e duração mínima | nº, s |
| Organização | Sempre mostrar, nunca mostrar, apenas mostrar (por moeda) | lista |
| Organização | Ordenar por (resultado, horário, spread, volume) e fixar sinais no topo | — |
| Lista negra | Moedas excluídas | lista |

Os filtros podem ser salvos em **grupos** com nome (ex.: "Farm", "Default").

**Invertida:** momento em que o F fica ≥ 0, ou seja, dá para fechar a operação sem perder spread. Conta se durar pelo menos a duração mínima. **[PENDENTE: confirmar comparando com o Arbimax]**

**Modos:**

- **Lucro:** RP líquido ≥ mínimo (padrão 0,2%).
- **Farm:** pelo menos uma ponta numa corretora marcada como farm (lista configurável, ex.: Lighter, Lighter Robinhood, Kraken Pro). Aceita RP líquido ≥ −custo máximo (padrão −0,1%) e ordena pelo **menor custo a cada $1.000 de volume** gerado na corretora de farm. Custo a cada $1.000 = −RP líquido × 5, porque cada operação gera volume ao abrir e ao fechar.

**Condição para virar sinal** (tudo ao mesmo tempo):

1. Passa em todos os filtros ativos e na regra do modo.
2. Lote mínimo atendido nas duas pontas. **Obrigatório**, padrão = tamanho de referência ($100).
3. Conexão das duas corretoras viva (última mensagem, inclusive heartbeat, há no máximo 2s) e livro de cada ponta com no máximo 30s sem mudança (ambos configuráveis).
4. Não marcado como suspeito (suspeitos ficam ocultos por padrão, com opção de mostrar).

**Anti-repetição:** o sinal fica na tabela enquanto continuar válido. No histórico, o mesmo par + tipo + pontas só gera novo registro depois de 60s sem aparecer (configurável). Cada registro guarda quando apareceu e quando sumiu.

## 5. Formato do sinal (JSON)

Todo sinal segue exatamente esta estrutura; painel, histórico e qualquer integração futura (Telegram, execução) leem o mesmo formato. **Os valores abaixo são só exemplo** (linha do AKAM dos prints; os números das métricas que não apareciam no print são ilustrativos). O que é obrigatório são os nomes dos campos e as regras.

```json
{
  "id": "AKAM-futuro_futuro-bitget-deepcoin-20261020T150412Z",
  "par": "AKAM/USDT",
  "tipo": "futuro_futuro",
  "modo": "lucro",
  "etiquetas": ["STOCK"],
  "suspeito": false,
  "aviso_cotacao": false,
  "ponta_long": {
    "corretora": "bitget",
    "mercado": "futuro",
    "simbolo_original": "AKAMUSDT",
    "preco": 89.14,
    "lote_usd": 1847.87,
    "lote_medio_usd": 1800.00,
    "volume_24h_usd": 56300,
    "funding_pct": 0.0,
    "funding_intervalo_h": 8,
    "limite_ordem_usd": 891400,
    "variacao_24h_pct": 4.63,
    "taxa_taker_pct": 0.06
  },
  "ponta_short": {
    "corretora": "deepcoin",
    "mercado": "futuro",
    "simbolo_original": "AKAMUSDT",
    "preco": 100.32,
    "lote_usd": 1364.35,
    "lote_medio_usd": 1400.00,
    "volume_24h_usd": 90000,
    "funding_pct": 0.01,
    "funding_intervalo_h": 8,
    "limite_ordem_usd": 10000000,
    "variacao_24h_pct": 1.01,
    "taxa_taker_pct": 0.06
  },
  "spread_abertura_pct": 12.54,
  "spread_fechamento_pct": -12.65,
  "metricas_periodo": {
    "periodo_h": 4,
    "abertura_media_pct": 11.72,
    "fechamento_medio_pct": -11.91,
    "maior_abertura_pct": 12.80,
    "maior_fechamento_pct": -11.50,
    "invertidas": 0
  },
  "fr_yield_8h_pct": 0.01,
  "rp_bruto_pct": 0.63,
  "custo_taxas_pct": 0.24,
  "funding_esperado_pct": 0.01,
  "rp_liquido_pct": 0.40,
  "custo_por_1000_usd": null,
  "tamanho_referencia_usd": 100,
  "detectado_em": "2026-10-20T15:04:12.345Z",
  "atualizado_em": "2026-10-20T15:04:14.120Z",
  "encerrado_em": null
}
```

**Regras do formato:**

- Datas sempre em UTC (ISO 8601); o painel mostra no horário de Brasília.
- Percentuais já em %, ex.: `0.40` = 0,40%.
- Preços já convertidos para USDT e ajustados pelo multiplicador; `simbolo_original` guarda o nome na corretora.
- `tipo`: `spot_futuro` ou `futuro_futuro`. `modo`: `lucro` ou `farm`. `mercado`: `spot` ou `futuro`.
- `custo_por_1000_usd` só é preenchido no modo Farm.
- `encerrado_em` é preenchido quando o sinal some, para medir a duração.
- Na ponta spot, os campos de funding ficam `null`.

## 6. Arquitetura e stack

A stack é **obrigatória** e o projeto roda com um comando só (`python -m scanner`), igual no Windows e na VPS; para migrar, basta copiar a pasta.

**Stack:**

- Python 3.12 e asyncio
- ccxt (inclui os WebSockets do livro de ofertas) para todas as corretoras, menos a Lighter, que ganha um conector próprio usando a API pública dela
- FastAPI + uvicorn (servidor do painel e WebSocket para atualizar ao vivo)
- SQLite (histórico e filtros salvos, num arquivo só)
- `config.yaml` para configuração (lido com PyYAML); `.env` para segredos futuros
- Painel em HTML + CSS + JavaScript puro, com Lightweight Charts para o gráfico
- pytest para os testes
- **Nenhuma biblioteca além dessas** sem aprovação do dono do projeto

**Módulos** (uma responsabilidade cada):

| Pasta | Responsabilidade |
| --- | --- |
| `scanner/config/` | Lê e valida o `config.yaml` |
| `scanner/coletores/` | Um por corretora; mantém livro, funding e taxas atualizados |
| `scanner/normalizador/` | Símbolos, multiplicadores, tamanho de contrato, conversão para USDT |
| `scanner/estado/` | Último livro de cada mercado em memória, com horário de chegada |
| `scanner/calculo/` | Preço de execução, A, F, custos, RP bruto e líquido |
| `scanner/metricas/` | Médias, maiores e invertidas da janela do período |
| `scanner/filtros/` | Filtros e regras dos modos Lucro e Farm |
| `scanner/sinais/` | Cria, atualiza e encerra sinais; anti-repetição |
| `scanner/armazenamento/` | Grava histórico e amostras no SQLite |
| `scanner/api/` | FastAPI: sinais ao vivo, filtros salvos, histórico de um par |
| `scanner/painel/` | A página web |
| `tests/` | Testes automáticos |

**Fluxo de dados:** coletores em duas etapas (etapa 1: melhor bid/ask de todos os pares por stream leve de ticker; etapa 2: livro completo só dos pares com spread de abertura acima do mínimo, cancelado depois de 60s abaixo; no modo Farm, as combinações com ponta numa corretora de farm usam um mínimo próprio, com no máximo `max_livros_farm_por_mercado` livros por mercado de farm, e as moedas de "sempre mostrar" sempre abrem livro) → estado em memória → a cada ciclo (padrão 500 ms) o motor percorre os pares candidatos → cálculo → métricas → filtros e modo → gerenciador de sinais → painel (ao vivo) + SQLite. Os spreads A e F de cada par candidato são gravados a cada 5s (configurável) para alimentar o gráfico e as métricas; os pares sem livro aberto também alimentam as métricas, pelos tickers (seção 3, "De onde vêm as amostras das métricas").

**Ordem de construção:**

1. Config + coletor de **uma** corretora, com teste imprimindo os preços no terminal para conferir com o site
2. Normalizador
3. As 3 corretoras da fase 1
4. Cálculo **spot x futuros**, com os testes numéricos da seção 3
5. Métricas do período
6. Filtros e modos
7. Sinais + SQLite
8. API + painel
9. **Futuros x futuros**
10. Fases 2 e 3 de corretoras

## 7. Painel web

O painel replica o essencial do Arbimax: tabela de sinais ao vivo, filtros com grupos salvos, gráfico de histórico e barra de status.

**Tabela de sinais ao vivo:**

- **Colunas:** Moeda (com tipo e etiquetas STOCK/suspeito) · Ponta long · Ponta short · Resultado · Histórico.
- **Cada ponta mostra:** corretora, preço, lado, lote ($ agora e médio), VOL, FR com intervalo, MAX e VAR.
- **Resultado:** A e F atuais, FR Yield, RP bruto e **RP líquido em destaque**. No modo Farm, também o custo a cada $1.000.
- **Histórico:** número de invertidas e A/F médios do período, com botão que abre o gráfico.
- Verde para positivo, vermelho para negativo. Sinais fixados ficam no topo.
- **Modo comparação:** chave que mostra o RP bruto sem custos, igual o Arbimax calcula, para a validação lado a lado.

**Painel de filtros** (acima da tabela):

- Todos os filtros da seção 4, agrupados como no Arbimax (Corretoras, Geral, Organização, Lista negra).
- Seletor de **modo** (Lucro/Farm) e de **grupo de filtros salvo**, com botões de criar, editar, duplicar e apagar.
- Mudou o filtro, a tabela atualiza na hora.

**Gráfico de histórico** (abre ao clicar num sinal):

- Linhas de preço das duas pontas no período.
- Alternar intenção: **abertura** ou **fechamento**.
- Resumo no topo: maior spread, média dos 10 maiores, spread médio e lote médio de cada ponta.
- Passando o mouse: preços, lotes, spread naquele momento e spread médio daquele ponto até agora.

**Barra de status** (rodapé):

- Horário da última atualização e quantidade de oportunidades.
- Saúde de cada corretora: conectada, reconectando ou com dado atrasado.
- Botão de pausar a atualização da tela e busca por moeda.

**Acesso:**

- No PC: `http://localhost:8000`, só na própria máquina.
- Na VPS: protegido por usuário e senha antes de ficar acessível pela internet.

## 8. Erros e riscos técnicos

Regra geral: um dado em que não se pode confiar nunca gera sinal, e uma corretora com problema nunca derruba o scanner inteiro.

| Risco | Como tratar |
| --- | --- |
| Queda de conexão | Reconecta sozinho com espera crescente (1s, 2s, 4s… até 60s). Enquanto desconectada, nenhum sinal usa os preços dela e os que dependiam dela são encerrados. |
| Dado atrasado | Cada conexão guarda o horário da última mensagem, inclusive heartbeat; sem mensagem há mais de 2s, a corretora é tratada como atrasada e não gera sinal. Um livro parado vale até 30s, porque o WebSocket só envia quando o livro muda. Se a corretora informa o horário do dado, registra o atraso médio de cada uma. |
| Livro inconsistente | Bid maior que ask, livro vazio ou preço zero → descarta e registra no log. Sequência quebrada → pede um livro completo novo. |
| Limite de requisições | WebSocket para preço e livro; REST só para dados lentos (mercados, taxas, funding) com intervalo configurável. Respeita o rate limit do ccxt; bloqueado → espera e registra. |
| Muitas assinaturas ao mesmo tempo | Se uma corretora limitar, prioriza os pares de maior volume e registra quantos ficaram de fora. |
| Mudanças nas corretoras | Lista de mercados recarregada a cada 1h (configurável). Par removido sai do scanner sem erro. |
| Recursos do PC | Amostras com mais de 7 dias são apagadas do SQLite (configurável). Memória e CPU registradas no log a cada 5 min. |

**Logs:** um arquivo por dia, com conexões, reconexões, erros e descartes. Nível configurável (normal ou detalhado).

**Latência:** rodando no Brasil, os preços chegam algumas centenas de milissegundos mais tarde do que numa VPS em Singapura. Não atrapalha a validação nem o modo Farm; pesa só em spreads que somem muito rápido.

## 9. Validação

O scanner só é considerado confiável depois de 3 dias rodando em modo leitura e de cumprir os 4 critérios abaixo.

**Testes automáticos** (entregues junto com o código):

- Os exemplos numéricos da seção 3, incluindo o caso "parece bom, mas não é".
- Normalização: 1000PEPE, kPEPE, conversão USDC e USDG para USDT.
- Preço de execução comendo o livro, e descarte quando falta liquidez.
- Anti-repetição e encerramento de sinais.

**Testes técnicos** (rodando de verdade):

- Desligar a internet por 1 min → volta sozinho, sem sinais falsos durante a queda.
- Corretora com dado atrasado → os sinais dela somem.
- 24h seguidas sem travar e sem estourar memória.
- Par sem liquidez → nunca aparece como sinal.

**Comparação:** com os mesmos filtros e as mesmas corretoras no Arbimax e no scanner (modo comparação ligado), conferir se aparecem os mesmos pares e se A, F e RP bruto batem. Em alguns sinais, conferir o preço direto no site da corretora.

**Métricas registradas automaticamente** (por dia e por modo): quantidade de sinais, RP líquido médio, duração média, % de sinais que sumiram em menos de 5s e de 30s, atraso médio e reconexões de cada corretora.

**Critérios de aprovação:**

1. RP bruto bate com o Arbimax com diferença de até 0,05 ponto percentual.
2. Pelo menos 80% dos sinais do Arbimax também aparecem no scanner em até 5s.
3. Nos preços conferidos, a diferença para o site da corretora é de até 0,1%.
4. Nenhum travamento, e todas as quedas recuperadas sozinhas.

**Por que um sinal pode parecer lucrativo e não ser** (checar antes de operar):

- Taxas maiores que o nível real da conta.
- Atraso: o preço já mudou na hora de executar.
- Lote pequeno: só dá para operar poucos dólares.
- Funding virando contra durante a operação.
- Ticker igual, mas token diferente.
- Limite de posição ou de ordem da corretora.
- Risco de liquidação de uma ponta com alavancagem alta.
- Par perto de ser removido (delisting).

## 10. Regras de trabalho do dev

Esta especificação é a fonte da verdade: precisou mudar algo, muda primeiro aqui, com aprovação do dono, e só depois no código.

**Decisões:**

- Dúvida ou ponto marcado como [PENDENTE]: perguntar antes de decidir. Nada de inventar regra.

**Código:**

- Repositório Git do dono do projeto, com o dev como colaborador.
- Um commit a cada módulo que funcionar, com mensagem clara do que mudou.
- Código simples e comentado em português, para o dono conseguir ler e ajustar via IA depois.
- Só a stack da seção 6; nenhuma biblioteca extra sem aprovação.
- Todos os limites e padrões no `config.yaml`, nunca fixos no código.

**Entregas:**

- Seguir a ordem de construção da seção 6, uma etapa por vez.
- Cada entrega traz: o que foi feito, como rodar, como testar e os testes automáticos passando.
- A próxima etapa só começa depois que o dono testar a anterior.
- Primeiro marco: o coletor de uma corretora imprimindo preços no terminal, para conferir com o site.

**Documentação:**

- `README` com instalação e como rodar no Windows e na VPS, passo a passo para iniciante.
- Explicação de cada campo do `config.yaml`.

**Segurança:**

- Sem chaves de API na v1 (só dados públicos).
- Quando houver execução, chaves só no `.env` e nunca no Git.
- Painel na VPS só com usuário e senha.

## 11. Pendências a validar

Estes pontos não bloqueiam o início da construção, mas precisam ser confirmados antes da etapa indicada.

| Pendência | O que fazer | Antes da etapa |
| --- | --- | --- |
| Definição de "invertida" (F ≥ 0 pela duração mínima) | Comparar a contagem do scanner com a do Arbimax | 5. Métricas |
| Valor entre parênteses do lote no Arbimax | Conferir se é o lote médio do período; o scanner já usa `lote_medio_usd` | 5. Métricas |
| "Limite de ordem" do Arbimax | Conferir se é o tamanho máximo de ordem da corretora (como definido aqui) | 6. Filtros |
| Taxas reais da sua conta (nível VIP, descontos) | Preencher no `config.yaml` por corretora | 4. Cálculo |
| Elegibilidade para perps da Lighter Robinhood a partir do Brasil | Confirmar no app antes de ativar a corretora | 10. Fase 3 |
| Disponibilidade de futuros da Kraken Pro para a sua conta | Confirmar antes de ativar a corretora | 10. Fase 3 |

**Notas da revisão:**

- O exemplo de JSON da seção 5 usa a Deepcoin porque veio do print do Arbimax; ela **não** está no escopo de corretoras.
- O conector próprio da Lighter usa o cliente HTTP/WebSocket que já vem com o ccxt (aiohttp), sem biblioteca nova.
