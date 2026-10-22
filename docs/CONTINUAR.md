# Como continuar o Scanner de Arbitragem

Situação em 22/10/2026: **v1 completa.** Tasks 1 a 18 prontas, e a revisão final do código inteiro foi feita, com as correções já aplicadas. Branch `v1`, **1477 testes passando**. Cada tarefa e cada correção passou por um implementador e por um revisor independente.

Falta só o que precisa do seu PC e da internet: os marcos e os 3 dias de validação (README, parte 4).

## O que tem nesta pasta

| Arquivo | Para que serve |
| --- | --- |
| `README.md` | Passo a passo: instalar, rodar, conferir cada marco, ler o painel, mudar a configuração, erros. **Comece por aqui.** |
| `config.yaml` | Toda a configuração, com um comentário em cada campo. |
| `docs/especificacao.md` | A especificação (fonte da verdade), já com as mudanças que você aprovou. |
| `docs/plano.md` | O plano original das 17 tarefas. |
| `docs/progresso-execucao.md` | O registro da execução: cada decisão ("Ruling", com o custo se estiver errada), cada tarefa concluída e os itens menores adiados. |
| `docs/CONTINUAR.md` | Este arquivo. |
| `.superpowers/sdd/plano/` | Material de trabalho: briefs, relatórios de cada tarefa, revisões e prints do Arbimax. Não vai para o Git, mas está no zip. |
| `scanner/` e `tests/` | O código e os testes. |

## Testar no seu PC

Siga o `README.md`, nesta ordem:

1. Parte 1: instalar no Windows (termina com os testes passando).
2. Parte 2: rodar e abrir o painel em `http://localhost:8000`.
3. Parte 4: conferir cada marco e os avisos específicos.

Se der erro, veja a parte 8 do README. Se não estiver lá, copie a mensagem inteira e cole na próxima conversa.

## O que foi feito nesta conta (Tasks 12 a 18 e revisão final)

| Task | O que faz |
| --- | --- |
| 12 | Painel web: tabela ao vivo, filtros com grupos salvos, gráfico, barra de status |
| 13 | Futuros x futuros, e métricas do período mais rápidas e leves (747 KB → 122 KB por par) |
| 14 | Reconexão sozinha, limites de assinatura, logs diários, memória e CPU no log |
| 15 | BingX, XT, Hyperliquid e cotação USDC → USDT |
| 16 | Kraken (spot e futuros) e Lighter (conector próprio, principal e Robinhood) |
| 17 | Métricas de validação (`/api/validacao`), limpeza diária, README completo e config comentado |
| Final | 3 revisores independentes (coleta, núcleo, painel/API) e as correções que eles pediram |
| 18 | Histórico das métricas pelos tickers de todos os pares (a sua opção 1) |

## Decisões que tomei no seu lugar (as que mais podem te afetar)

Todas estão em `docs/progresso-execucao.md`, com o custo caso estejam erradas.

1. **Painel:**
   - mudou um filtro, vale na hora e é salvo no grupo escolhido;
   - o grupo lembrado no navegador é reaplicado ao abrir: o último navegador aberto manda no motor;
   - nos campos em dólar, "50.000" é 50 mil.
2. **Sessão salva:** o modo, os filtros e a comparação ficam no banco e voltam quando você religa o scanner.
3. **Futuros x futuros:** o motor calcula sempre os dois tipos e as duas direções; o filtro "tipos" só esconde.
4. **Quedas:**
   - a corretora volta sozinha (1 s, 2 s, 4 s… até 60 s), inclusive a que não carregou ao ligar;
   - durante a queda, os dados dela são apagados (nunca sinal com dado velho);
   - "reconexões" conta só as voltas que deram certo.
5. **Logs:** um arquivo por dia em `logs/`, com o horário do PC e o fuso (`-0300`); o log nunca trava nem derruba o scanner.
6. **Memória e CPU** no log a cada 5 min, sem biblioteca nova.
7. **Suspeito:** os preços das duas pontas diferem mais de 30%, em qualquer sentido (antes só pegava um).
8. **Cotações:**
   - USDC vem da Bitget, USD vem da Kraken;
   - cotação fora de 10% em volta de 1 vale 1:1, com aviso;
   - pares em EUR, GBP etc. ficam fora (dariam sinal falso).
9. **Kraken = duas corretoras:** `kraken` (spot) e `krakenfutures` (futuros), cada uma com a sua conexão.
10. **Lighter:**
    - conector próprio, sempre só leitura;
    - taxa e funding lidos em %, funding de 1 h (conferido na documentação oficial);
    - se o `market_stats` não trouxer bid/ask, ela passa a usar os livros.
11. **Farm:** abre livro a partir de −0,1% de spread, no máximo 50 por mercado de farm. "Sempre mostrar" sempre abre livro.
12. **Banco:**
    - grava numa thread separada, com amostras compactas (209 → 82 bytes);
    - a limpeza apaga só as amostras antigas, nunca o histórico de sinais;
    - sinais abertos são fechados ao encerrar.
13. **Segurança:**
    - o scanner não liga com o painel aberto para fora (host que não é 127.0.0.1) sem usuário e senha;
    - outro site não consegue ler nem mudar nada no painel.
14. **Versões fixas** no `requirements.txt` (as testadas). O código depende de detalhes do ccxt 4.5.89.
15. **Histórico pelos tickers (Task 18):**
    - amostra a cada **120 s**, e não 60 s: com 60 s a memória passava de 600 MB, acima dos ~400 MB que você aceitou;
    - a média é ponderada pelo tempo (sem isso o sinal sumia em ~10 min);
    - a memória fica em ~380 MB em qualquer período.
16. **Teste de tempo:** o teste de escala do Farm só confere tempo com `SCANNER_TESTES_DE_TEMPO=1`, para não dar falso alarme num PC lento.

## Decisões que você aprovou nesta conta

- **Histórico pelos tickers de todos os pares (opção 1):** spec §3 atualizada, Task 18.
- **Biblioteca `protobuf==5.29.5`:** sem ela, o spot da MEXC não recebe nada no ccxt 4.5.89.

## O que continua com você

1. **XT spot:** no exemplo, a XT está só com futuros (`xt: [futuro]`), porque o ccxt 4.5.89 não traz bid/ask no spot dela. Para tentar o spot, apague essa linha; se não funcionar, o log avisa.
2. **MEXC spot por REST:** com o limite de 30 assinaturas por conexão, o scanner acompanha só ~20 pares do spot da MEXC. Pegar todos exige a etapa 1 por REST a cada 1–2 s, o que a spec §8 não permite sem você aprovar.
3. **Funding da Bitget em USDC:** só os perpétuos em USDT da Bitget recebem funding; os que só existem em USDC ficam com funding 0.
4. **RP mínimo do modo Lucro:** está em **0,2% líquido** (depois das taxas). No Arbimax você usava 0,2% bruto. Para afrouxar, mude `rp_min_lucro_pct`.
5. **Antes de ativar:**
   - `krakenfutures`: futuros da Kraken na sua conta;
   - `lighter_rh`: elegibilidade no Brasil.
   As duas estão comentadas no `config.yaml` (spec §11).
6. **Suas taxas reais (nível VIP):** preencha `taxas` no `config.yaml`.
7. **Pendências da spec §11:** definição de "invertida", o valor entre parênteses do lote e o "limite de ordem" do Arbimax.
8. **GitHub:** subir para um repositório privado seu. Na próxima conversa, informe o usuário e o nome do repositório.

## Prompt para continuar em outra conversa

1. Abra uma conversa com acesso a arquivos e execução de código.
2. Anexe o zip mais recente do projeto.
3. Cole:

```
<contexto>
Projeto: scanner de arbitragem cripto (spot x futuros e futuros x futuros), só leitura,
Python 3.12 + ccxt + FastAPI + SQLite, com painel web. O zip anexado é o repositório
completo com histórico Git. Sou iniciante em programação: explique de forma direta e curta.
</contexto>

<estado>
- v1 completa (Tasks 1 a 18 e revisão final), branch v1, todos os testes passando.
- Agora estou testando no meu PC (README, parte 4). Vou colar aqui o que der errado.
</estado>

<arquivos_para_ler_primeiro>
1. docs/CONTINUAR.md
2. docs/especificacao.md (fonte da verdade)
3. docs/progresso-execucao.md (decisões "Ruling" e itens adiados)
4. README.md
</arquivos_para_ler_primeiro>

<regras>
- Descompacte o zip, confira com "git log" e rode os testes (python -m pytest -q) antes de mudar qualquer coisa.
- Toda mudança: TDD, commit, e um revisor independente confere contra a especificação.
- Mudança de regra: primeiro na especificação, com a minha aprovação.
- Registre cada decisão nova em docs/progresso-execucao.md.
- Código, nomes e comentários em português. Nenhuma biblioteca nova sem me perguntar.
</regras>

<pedido>
{O que você quer agora: por exemplo, "o marco 1 deu este erro: ..."}
</pedido>
```
