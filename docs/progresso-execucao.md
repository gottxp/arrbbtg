# SDD ledger — plan: docs/plano.md

Spec: docs/especificacao.md. Branch: v1 (base main 0bc0468). Python: .venv (3.12). Sem acesso de rede às corretoras neste ambiente (proxy 403): testes só offline; marcos "no PC do dono" ficam para o usuário.

## Pre-flight scan

| Par / tarefa | Produz → consome | Achado | Ruling |
| --- | --- | --- | --- |
| T1 → T4/T10 | Config.max_silencio_conexao_s, max_idade_livro_s → EstadoMercados(max_silencio_s, max_idade_livro_s) | Plano no Docs usava `max_idade_dado_s` (anterior à correção do dado velho) | Já corrigido no plano local: dois campos separados |
| T2 → T4 | ColetorCcxt.iniciar (só livro) → duas etapas | T4 precisa de ticker + assinar/cancelar livro | T4 estende ColetorCcxt (iniciar_tickers, assinar_livros) |
| T3 → T10/T15 | normalizar/preco_unitario_usdt | "k só quando o restante é ativo conhecido" sem definição de "conhecido" | Regra: k minúsculo + maiúscula = ×1000; 1000/10000/1000000/1M + maiúscula |
| T4 → T10 | EstadoMercados.obter_livro/obter_ticker | ok | — |
| T5 → T10/T13 | fórmulas puras | ok | — |
| T6 → T7/T10 | Metricas | ok | — |
| T7 → T8/T10/T11 | Candidato, Ponta, Filtros, passa | "mesmos campos do JSON" com objetos aninhados | Ponta dataclass + Candidato.chave definidos no plano local |
| T8 → T9/T11 | Evento, ativos() | ok | — |
| T9 → T11/T17 | Banco (sinais_do_dia acrescentado para T17) | T17 precisava ler sinais por dia | sinais_do_dia adicionado em T9 |
| T10 ↔ T13 | motor_falso kwargs | nomes diferentes entre T10 e T13 | fabricas.motor_falso aceita todos |
| T11 | httpx para TestClient | fora da stack | Ruling: httpx só em requirements-dev.txt (teste) — se o dono não aprovar, remover e testar API manualmente |
| T12 | painel sem teste automático | — | Teste mínimo de arquivos estáticos (T12 Passo 1); checklist manual fica para o dono |
| T13 | "só depois que o dono liberar o spot x futuros" | portão humano no meio da execução contínua | Ruling: seguir; o portão vale para uso real, o dono valida no PC antes de operar |
| T14 | psutil para CPU | fora da stack | Ruling: só memória (tracemalloc) |
| T16 | fixture real da Lighter | sem rede às corretoras aqui | Ruling: fixture segundo a documentação oficial (WebFetch), marcado para conferência real |
| T17 | README/config comentado (spec §10) sem tarefa | — | Acrescentado à T17 |
| Marcos ao vivo (T2,T4,T10,T12,T14,T15,T16) | — | impossíveis sem rede às corretoras | Ficam para o dono; implementadores usam corretoras falsas |

## Tasks
Task 1: minor (deferred): CAMPOS_NUMERICOS_LIVRES morto + fallback numérico implícito (fonte_cotacao na T15 precisa de validador próprio)
Task 1: minor (deferred): arquivo não UTF-8 gera traceback em vez de ErroConfig
Task 1: minor (deferred): .nan/.inf passam na validação
Task 1: minor (deferred): anti_repeticao_s e tempo_operacao_h exigem >0 (0 poderia significar desligado)
Task 1: minor (deferred): lista de positivos no teste copiada à mão (falta anti_repeticao_s)
Task 1: minor (deferred): anotações de tipo frouxas; Config não hasheável; usuario sem senha aceito
Task 1: minor (deferred): requirements só com limite inferior (ccxt sem <5)
Task 1: fix round 1/5 (1 addressed, 0 open — teste de igualdade do config.yaml trocado por guarda de campos; commits 616d0b4..823e6cf)
Task 1: complete (commits 0bc0468..823e6cf, review clean)
Task 2: minor (deferred): INTERVALO_PADRAO_S=5 fixo na ferramenta (independente de amostra_s; comentar)
Task 2: minor (deferred): imprimir_precos não mostra idade do preço
Task 2: minor (deferred): nível malformado (None) em livro_de_ccxt gera TypeError em vez de descartar
Task 2: minor (deferred): log de descarte sem motivo e só em DEBUG
Task 2: minor (deferred): nome de teste enganoso (imprimir_precos_mostra_o_erro); caminhos de main sem teste; sleep real no teste
Task 2: minor (deferred): Livro sem docstring dizendo que níveis são unidades cruas do ccxt
Task 2: complete (commits 823e6cf..65531e5, review clean)
Ruling: conversão dos níveis do livro — o coletor/estado guarda níveis já em preço por 1 unidade do ativo (na moeda de cotação) e quantidade em unidades do ativo (multiplicador e contractSize aplicados) via `converter_livro(livro, par)` na Task 4; a conversão da moeda de cotação para USDT (cotações + aviso_cotacao) acontece no motor (Task 10) — porque as cotações mudam no tempo e chegam na T15 — custo se errado: refatorar a conversão para um único ponto
Ruling: liveness/heartbeat — registrar_mensagem é chamado por toda mensagem de ticker (etapa 1, todos os pares) e de livro; o stream de tickers serve de heartbeat — custo se errado: corretora com tickers parados parece morta (falso negativo, seguro)
Ruling: Task 4 assume o ciclo de vida do exchange (close separado, callback guardado, registro de tasks por símbolo, falha isolada por símbolo) e estende imprimir_precos para várias corretoras — custo se errado: Task 4 maior
Task 3: minor (deferred → passado à Task 4): docstring de tamanho_contrato enganosa; fórmula: unidades_do_ativo = quantidade_ccxt × tamanho_contrato × multiplicador
Task 3: minor (deferred): cotação 0/None/negativa não validada em preco_unitario_usdt
Task 3: minor (deferred): só os prefixos do plano (100000AIDOGE fica separado; 1MOON viraria OON)
Task 3: minor (deferred): market com base/quote None não protegido (coletor deve filtrar)
Task 3: minor (deferred): sem teste kPEPE/USDC com cotação (coberto na T15)
Task 3: complete (commits 65531e5..e18af77, review clean; trailer do commit corrigido pelo controlador para Opus 5.5)
Ruling: chave do estado (corretora, mercado, ativo) não distingue cotação — a ingestão (Task 4, fix round 1) escolhe um símbolo por chave, preferindo USDT, depois USDC, depois USDG; contratos inversos ignorados — custo se errado: perder pares só em USDC nas corretoras com USDT
Ruling: TaxaDesconhecida (sem taxa no config nem na API) — o motor (Task 10) descarta o candidato e loga uma vez por par — custo se errado: pares sem taxa somem do scanner (seguro)
Task 4: minor (deferred → T14): símbolos que falham repetem sem backoff e com traceback a cada chamada
Task 4: minor (deferred → T14): erro no ao_ticker/estado derruba o stream de tickers inteiro da corretora
Task 4: minor (deferred → T14): fechar() não para tarefas de ticker em andamento
Task 4: minor (deferred → T14): DadosLentos.rodar espera todas as corretoras (MEXC lenta atrasa as outras)
Task 4: minor (deferred): funding de contrato removido nunca expira
Task 4: minor (deferred): teste de imprimir_precos sem timeout; teste de rodar com sleeps curtos (flaky)
Task 4: minor (deferred): obter_ticker com limite de 30s pode tirar pares parados da etapa 1
Task 4: fix round 1/5 (3 addressed, 0 open — un_watch_order_book, classificação por flags do ccxt, filtro inverso/opção + preferência de cotação; commits 1e35505..e4a12e5)
Task 4: minor (deferred → T14): assinar_livros espera os un_watch sem timeout antes de criar os novos (criar novos primeiro)
Task 4: minor (deferred → T14): _indexar_markets sem try — market malformado derruba o stream de tickers
Task 4: minor (deferred): livro que morre sozinho não recebe un_watch; Livro sem campo cotacao
Task 4: note → T10: o motor deve pedir ao coletor o símbolo vencedor (preferência de cotação) para cada (mercado, ativo)
Task 4: complete (commits e18af77..e4a12e5, review clean)
Task 5: minor (deferred): sem teste exercitando margem_slippage ≠ 0 em rp_liquido
Task 5: minor (deferred): floor com intervalos fracionários; horas negativas não validadas
Task 5: minor (deferred): parâmetros sombreiam nomes de funções (assinatura do plano); PEP8 E302 nos testes
Task 5: complete (commits e4a12e5..0fcbf01, review clean)
Task 6: minor (deferred): sem teste para F == 0 exato nem para ts == agora − periodo_s
Task 6: minor (deferred → T10): resumo() descarta por agora (motor deve passar o relógio real); docstring "funções puras" imprecisa
Task 6: minor (deferred → T10): "consecutivas" conta lacunas de amostragem como tempo invertido (definição PENDENTE na spec)
Task 6: minor (deferred → T10): NaN em f não protegido (motor nunca deve passar NaN)
Task 6: fix round 1/5 (1 addressed, 0 open — cache do resumo + heapq.nlargest; commits 0b0d3b1..55abf3c)
Task 6: minor (deferred → T10): Metricas em cache é objeto compartilhado não-frozen (motor trata como somente leitura)
Task 6: complete (commits 0fcbf01..55abf3c, review clean)
Ruling: Filtros (T7) — lado1 = ponta long, lado2 = ponta short; "corretora" ou "corretora:mercado"; sempre_mostrar ignora filtros e modo mas não o lote mínimo obrigatório; período/métrica/ordem/fixados guardados no grupo mas aplicados pelo motor/painel; passa(..., comparacao=True) usa RP bruto — custo se errado: ajuste de semântica de filtros no painel
Ruling: "funding sincronizado" aproximado por intervalos iguais nas pontas futuras (a spec não tem horário da próxima cobrança) — custo se errado: filtro menos preciso; PENDENTE confirmar com o Arbimax
Ruling: sempre_mostrar também passa por cima de lista negra/nunca/apenas mostrar (é o pedido explícito do dono), mas não do lote mínimo nem do ocultar suspeitos — custo se errado: uma linha em passa
Task 7: note → T8: metricas_periodo.periodo_h vem da sessão/config (Metricas não guarda período)
Task 7: minor (deferred): Filtros(sempre_mostrar/lista_negra/nunca_mostrar=None) direto quebra (de_dict normaliza)
Task 7: minor (deferred → T8): custo_por_1000(0.0) devolve -0.0 (JSON "-0.0")
Task 7: minor (deferred → T11): de_dict não valida valores de corretoras_lado*/tipos; null em booleanos rejeitado; campos só-armazenados sem checagem
Task 7: complete (commits 55abf3c..79dd2f9, review clean)
Ruling: anti-repetição (T8) — reaparecer antes de anti_repeticao_s desde a última vez visto reusa o id e detectado_em do registro anterior (mesmo registro histórico; o encerrado atualiza esse registro) — custo se errado: histórico agrupa oportunidades intermitentes como uma só
Task 8: minor (deferred): teste de arredondamento de ms não distingue truncar de arredondar (usar 1.001)
Task 8: minor (deferred): teste lê _encerrados privado; comentário sobre redundância < / >= na fronteira
Task 8: minor (deferred → T10): gerenciador guarda referência ao Candidato — motor deve criar Candidato novo a cada ciclo
Task 8: complete (commits 79dd2f9..cec9f00, review clean)
Ruling: T9 — "novo" com registrar_historico=False (id reutilizado) reabre o registro no banco (encerrado_em volta a NULL); o próximo "encerrado" grava o fim — custo se errado: duração de oportunidades intermitentes medida diferente
Task 9: minor (deferred): .gitignore sem *.db-wal / *.db-shm
Task 9: minor (deferred): teste test_modo_wal_e_banco_novo_nao_apaga_o_antigo mal nomeado; lock nunca testado sob concorrência
Task 9: minor (deferred → T10): conexão vaza se PRAGMA falhar no __init__; fsync por amostra (considerar synchronous=NORMAL ou lote por ciclo)
Task 9: minor (deferred): _iso_utc duplicado (banco trunca, gerenciador arredonda)
Task 9: complete (commits cec9f00..86cd39a, review clean)
Ruling: T10 — lote_usd = valor (preço×quantidade) do melhor nível do lado executado; lote_medio_usd = média desses lotes no período, guardada pelo motor junto das métricas; limite_ordem_usd = None na v1 (desconhecido passa no filtro); etiquetas STOCK ficam vazias na v1 (identificação de ação tokenizada PENDENTE) — custo se errado: colunas MAX/STOCK vazias no painel até ajuste
Ruling: T10 — primeira amostra de um par é gravada na hora em que ele aparece (F médio inicial = F atual); depois a cada amostra_s — custo se errado: RP dos primeiros segundos igual ao A+F instantâneo
Ruling: T10 — `python -m scanner` inicia o scanner; `--validar` só checa a config — custo se errado: README/menção na T17
Ruling: T10 — taxa ausente logada uma vez por corretora+mercado (não por par) para não inundar o log — custo se errado: uma linha
Ruling: T10 — sessão inválida (modo/métrica/período) cai no valor da config com um aviso, em vez de falhar todo ciclo — custo se errado: erro silencioso no painel (aviso no log)
Task 10: minor (deferred → T14): um try só cobre ciclo + seleção + assinar (falha no ciclo impede a etapa 2); sem try por combo
Task 10: minor (deferred → T14): eventos gravados com um commit cada, síncrono no loop (lote por ciclo)
Task 10: minor (deferred): _simbolos_livro criado e apagado para todo combo a cada ciclo
Task 10: minor (deferred): sem teste do funding nulo na ponta spot da mesma corretora
Task 10: minor (deferred): _Historico duplica a janela de JanelaMetricas; SQL de INSERT duplicado em banco.py
Task 10: minor (deferred → T11): candidatos() tem efeitos colaterais (amostras) — API não deve chamá-lo fora do ciclo
Task 10: fix round 1/5 (2 addressed, 0 open — remover_livro ao sair da seleção/morrer; ajuste de livros em tarefa própria + timeout_desassinar_s; commits 3ee4504..61bd11b)
Task 10: minor (deferred → T14): except TimeoutError pega timeout interno do ccxt (mensagem enganosa); erro do ajuste sem traceback; ajuste compartilhado entre corretoras espera a mais lenta
Task 10: minor (deferred): fechar() não limpa _livros_no_estado
Task 10: complete (commits 86cd39a..61bd11b, review clean)
Ruling: T11 — API sobe dentro do mesmo loop asyncio do motor (uvicorn.Server como tarefa); WS com fila por cliente e descarte de cliente lento; usuário sem senha impede a inicialização — custo se errado: rearranjo de processo na VPS
Ruling: T11 — `websockets` entra no requirements.txt como dependência do uvicorn para servir WebSocket (equivale a uvicorn[standard]; a stack já prevê WebSocket via uvicorn) — custo se errado: trocar por wsproto
Ruling: T11 — filtro de aviso de depreciação do TestClient no pyproject (só testes) em vez de instalar httpx2 — custo se errado: nenhum em produção
Task 11: minor (deferred → final): periodo_h sem limite superior (consulta grande trava o lock do Banco que o motor também usa)
Task 11: minor (deferred → final, segurança): WS sem checagem de Origin com auth ligada (sequestro de WebSocket entre sites)
Task 11: minor (deferred): teste com uvicorn real não cobre WS; comentário "fecha com 1008" só vale no TestClient (real = HTTP 403)
Task 11: minor (deferred): WS serializa com allow_nan=True (inconsistente com JSONResponse)
Task 11: minor (deferred): test_api importa helpers de test_motor (convenção é fabricas.py)
Ruling: T11 — TAMANHO_FILA_PADRAO e TEMPO_MAX_ENCERRAR_S ficam como constantes de módulo (detalhes internos do servidor, não limites de negócio) — custo se errado: mover para Config
Task 11: fix round 1/5 (1 addressed, 0 open — CorpoSessao extra=forbid e campos obrigatórios; commits c5ad88f..63ac302)
Task 11: note → T12: o painel envia sempre modo+filtros+comparacao no PUT; não depender do código 1008 no WS (real = HTTP 403/1006)
Task 11: complete (commits 61bd11b..63ac302, review clean)
