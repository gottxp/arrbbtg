# Como continuar o Scanner de Arbitragem

Parada em 22/10/2026, com as **Tasks 1 a 11 de 17 prontas e revisadas** (branch `v1`, 17 commits, **647 testes passando**). Falta o painel web (Task 12), futuros x futuros, robustez, fases 2 e 3 de corretoras, métricas de validação (Tasks 13 a 17) e a revisão final do código inteiro.

## O que tem nesta pasta

| Arquivo | Para que serve |
| --- | --- |
| `docs/especificacao.md` | A especificação aprovada (fonte da verdade). Em conflito com o plano, vale ela. |
| `docs/plano.md` | O plano com as 17 tarefas, testes e interfaces. |
| `docs/progresso-execucao.md` | O registro da execução: o que foi feito em cada tarefa, decisões tomadas e pendências menores adiadas. |
| `docs/CONTINUAR.md` | Este arquivo. |
| `.superpowers/sdd/plano/` | Material de trabalho da execução (briefs e relatórios de cada tarefa, instruções do revisor, prints do Arbimax em `referencias/`). Não vai para o Git, mas está no zip. |
| `scanner/` e `tests/` | O código e os testes. |

Os documentos "Especificação" e "Plano" que ficaram no Claude Docs da outra conta são uma versão **anterior**. Os arquivos em `docs/` estão atualizados (com as correções da auto-análise) e são os que valem.

## O que já está pronto (Tasks 1 a 11)

| Task | O que faz | Status |
| --- | --- | --- |
| 1 | Base do projeto e `config.yaml` com validação | Pronta e revisada |
| 2 | Coletor de livro de ofertas via ccxt | Pronta e revisada |
| 3 | Normalizador (1000PEPE, kPEPE, 1M, USDC/USDG) | Pronta e revisada |
| 4 | Estado em memória, coleta em duas etapas (ticker leve + livro só dos pares promissores), MEXC/Gate/Bitget | Pronta, revisada e corrigida |
| 5 | Fórmulas (A, F, taxas, funding, RP bruto e líquido) | Pronta e revisada |
| 6 | Métricas do período e invertidas (com cache) | Pronta, revisada e corrigida |
| 7 | Filtros e modos Lucro/Farm | Pronta e revisada |
| 8 | Gerenciador de sinais (formato JSON da spec, anti-repetição) | Pronta e revisada |
| 9 | Banco SQLite (histórico, amostras, grupos de filtros) | Pronta e revisada |
| 10 | Motor spot x futuros (junta tudo e roda o ciclo) | Pronta, revisada e corrigida |
| 11 | API (FastAPI + WebSocket, login opcional) | Pronta, revisada e corrigida |

Cada tarefa passou por um implementador e por um revisor independente. As correções importantes encontradas na revisão já estão aplicadas.

## O que falta

| Task | O que faz |
| --- | --- |
| 12 | Painel web (tabela ao vivo, filtros, gráfico, barra de status) |
| 13 | Futuros x futuros |
| 14 | Robustez: reconexão, limites de assinatura, logs diários |
| 15 | Fase 2 de corretoras: BingX, XT, Hyperliquid (cotação USDC) |
| 16 | Fase 3: Kraken Pro, Lighter e Lighter Robinhood (USDG) |
| 17 | Métricas de validação, README completo e `config.yaml` comentado |
| Final | Revisão do código inteiro por um revisor independente, correções e entrega |

## Testar no seu PC agora

Siga o `README.md` (na pasta do projeto), nesta ordem:

1. Parte 1: instalar no Windows (termina com os testes passando).
2. Parte 2: rodar no Windows e abrir o painel em `http://localhost:8000`.
3. Parte 4: conferir cada marco, do marco 1 (preços das corretoras) ao marco 5 (validação de 3 dias), e os avisos específicos A a F.

Se algo der erro, veja a parte 8 do README. O que não estiver lá: copie a mensagem inteira e cole na próxima conversa.

## Decisões que tomei no seu lugar

Durante a execução, decidi alguns pontos que a especificação não fechava. Todos estão em `docs/progresso-execucao.md` com o custo caso estejam errados. Os que mais podem te afetar:

1. **Conversão de preços:** os livros são guardados por unidade do ativo na moeda de cotação; a conversão para USDT acontece no motor. Se errado, é refatorar a conversão para um ponto só.
2. **Uma cotação por par:** quando a mesma corretora tem BTC/USDT e BTC/USDC, fica o USDT (depois USDC, depois USDG). Contratos inversos e opções são ignorados. Se errado, pares que só existem em USDC numa corretora com USDT somem.
3. **Taxa desconhecida:** par sem taxa (nem no config, nem na API) não gera sinal. É o lado seguro.
4. **Filtros:** "Corretoras 1" filtra a ponta long e "Corretoras 2" a short. "Sempre mostrar" passa por cima dos filtros e das listas, mas nunca do lote mínimo nem de sinais suspeitos.
5. **Funding sincronizado:** aproximado por "as duas pontas cobram no mesmo intervalo", porque não temos o horário da próxima cobrança. Pendente confirmar com o Arbimax.
6. **Anti-repetição:** se o mesmo sinal volta em menos de 60s, conta como a mesma oportunidade no histórico.
7. **Colunas MAX e STOCK:** ficam vazias na v1 (limite de ordem e identificação de ação tokenizada ainda não definidos).
8. **`python -m scanner`** liga o scanner; `python -m scanner --validar` só confere o `config.yaml`.
9. **API e motor no mesmo processo:** o painel sobe junto com o scanner. Se a porta 8000 estiver ocupada, o scanner segue sem painel e avisa no log.
10. **Bibliotecas extras:** `websockets` entrou no `requirements.txt` porque o uvicorn precisa dela para o WebSocket do painel. `httpx` só para testes. `psutil` não entrou: o log mede só memória, não CPU.

## Decisões que continuam com você

- **RP mínimo do modo Lucro:** está em **0,2% líquido** (depois de taxas). No Arbimax você usava 0,2% bruto, então o filtro ficou mais rígido. Para afrouxar, mude `rp_min_lucro_pct` no `config.yaml` (ex.: 0.05).
- **GitHub:** o ideal é subir o projeto para um repositório privado seu. Na próxima conta, informe o usuário e o nome do repositório.
- **Pendências da spec (seção 11):** definição de "invertida", o valor entre parênteses do lote e o "limite de ordem" do Arbimax, as suas taxas reais (nível VIP), elegibilidade da Lighter Robinhood no Brasil e futuros da Kraken na sua conta.

## Itens menores guardados para a revisão final

O registro tem 59 itens menores encontrados pelos revisores e adiados de propósito. Os que a revisão final precisa olhar primeiro:

- **Segurança do painel:** o WebSocket não confere a origem da conexão quando o login está ligado. É importante antes de abrir o painel na VPS.
- **Consulta de histórico sem limite** pode travar o banco que o motor também usa.
- **Eventos gravados um a um**, de forma síncrona, no ciclo do motor; devem ser gravados em lote.
- **Itens marcados "→ T14"** no registro: entram na Task 14 (robustez).

## Prompt para continuar em outra conta Claude

1. Na nova conta, abra uma conversa com acesso a arquivos e execução de código. Pode ser o Claude com o computador ligado, ou o Claude Code.
2. Anexe o arquivo `scanner-arbitragem-ate-task11.zip`.
3. Cole o prompt abaixo:

```
<contexto>
Estou migrando um projeto de outra conta Claude. O zip anexado é o repositório completo
(com histórico Git) de um scanner de arbitragem cripto spot x futuros e futuros x futuros,
só leitura, em Python 3.12 + ccxt + FastAPI + SQLite, com painel web.
Sou iniciante em programação: explique de forma direta e curta.
</contexto>

<estado>
- Tasks 1 a 11 de 17 prontas, revisadas e commitadas na branch v1 (647 testes passando).
- Falta: Task 12 (painel web) até a Task 17, e depois a revisão final do código inteiro.
</estado>

<arquivos_para_ler_primeiro>
1. docs/CONTINUAR.md: situação, decisões já tomadas e pendências
2. docs/especificacao.md: fonte da verdade
3. docs/plano.md: as 17 tarefas (Task N), com testes e interfaces
4. docs/progresso-execucao.md: registro de execução, decisões ("Ruling") e itens menores adiados
5. .superpowers/sdd/plano/: briefs, relatórios das tarefas, instruções do revisor e prints do Arbimax em referencias/
</arquivos_para_ler_primeiro>

<regras>
- Descompacte o zip, confira com "git log" e rode os testes (python -m pytest -q) antes de mudar qualquer coisa.
- Continue a partir da Task 12, na ordem do plano, sem refazer tarefas concluídas.
- Para cada tarefa: um implementador faz com TDD e commit; depois um revisor independente
  confere contra a especificação; problemas importantes são corrigidos antes de seguir.
  Se o plugin "superpowers" estiver disponível, use a skill subagent-driven-development.
- Siga as decisões ("Ruling") já registradas em docs/progresso-execucao.md e registre ali
  cada nova decisão, conclusão de tarefa e item menor adiado.
- Itens marcados "→ T14" no registro entram na Task 14.
- Sem rede para as corretoras no ambiente de nuvem: testes sempre com corretoras falsas;
  os marcos "no PC do dono" ficam para mim.
- Código, nomes e comentários em português. Nenhuma biblioteca nova sem me perguntar.
- No fim: revisão final do código inteiro, lista das decisões tomadas no meu lugar e um zip
  atualizado (ou push para o meu GitHub, se eu informar o repositório).
</regras>

<pedido>
Leia os arquivos acima, me diga em poucas linhas se está tudo consistente e comece a Task 12.
</pedido>
```
