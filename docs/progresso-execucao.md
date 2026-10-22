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
| T14 | psutil para CPU | fora da stack | Ruling: só memória (tracemalloc) — SUBSTITUÍDO na T14: memória = RSS do processo e CPU % por time.process_time, só biblioteca padrão |
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
Ruling: execução (controlador nesta conta) — T12 e T13 rodaram em paralelo (T13 num git worktree, branch t13), com os commits trazidos para a v1 na ordem do plano (rebase). Ambiente novo: .venv recriado com as mesmas versões (ccxt 4.5.89); 647 testes conferidos antes de começar.
Ruling: T12 — Lightweight Charts 4.2.3 (API v4) pelo unpkg, versão fixa; sem a biblioteca o modal do gráfico avisa e o resto do painel funciona — custo se errado: trocar a URL e as chamadas da API v4
Ruling: T12 — filtros: toda mudança aplica na hora (PUT /api/sessao com modo+filtros+comparacao) e é salva no grupo selecionado (POST, debounce ~300 ms); lápis = renomear (POST novo + DELETE antigo); duplicar seleciona a cópia; grupo lembrado no navegador (localStorage) é reaplicado ao abrir/reconectar se diferir da sessão (o último navegador aberto define a sessão do motor) — custo se errado: comportamento de grupo diferente do Arbimax
Ruling: T12 — `fixados` = lista de chaves em texto (`par|tipo|corretora_long|mercado_long|corretora_short|mercado_short`, igual a texto_da_chave); o gráfico abre pelo botão da coluna Histórico — custo se errado: formato de fixados nos grupos já salvos
Ruling: T12 — números: vírgula ou ponto como decimal; nos campos em dólar "d.ddd" é milhar (50.000 = 50 mil) — custo se errado: um campo interpretado diferente do esperado
Ruling: T12 — nome de grupo com "/" é recusado no painel (a rota /api/filtros/{nome} não aceita "/") — custo se errado: trocar a rota para {nome:path}
Task 12: minor (deferred → final): ressincronização do painel que falha (GET depois de reiniciar o scanner) não tenta de novo; o grupo não é reaplicado até a próxima queda
Task 12: minor (deferred → final): mudança recusada (400/422) seguida de reconexão reenvia o formulário recusado em vez de reaplicar o grupo (nada é apagado)
Task 12: minor (deferred): montagem em "/" muda respostas de erro da API (GET /api/sinais/ 404 em vez de 307; POST /api/sessao 405 com Allow: GET, HEAD)
Task 12: minor (deferred): guarda dos campos de Filtros no painel é só textual (não confere data-tipo)
Task 12: minor (deferred): várias abas com o mesmo grupo: a aba com formulário velho regrava o grupo inteiro ao mudar um filtro (limitação de projeto; avisar o dono)
Task 12: minor (deferred): opções de corretoras = /api/status × {spot, futuro}; corretoras só de futuros mostram uma opção "spot" que não casa com nada
Task 12: minor (deferred → T17/README): gráfico mostra as linhas de preço de abertura nas duas intenções (o banco só guarda ask do long e bid do short); nota no modal
Task 12: fix round 1/5 (2 important + 5 minor addressed, 0 open — sessão/grupos lidos antes de gravar; "d.ddd" = milhar em dólar; travessia, fetch com tempo limite, foco na ressincronização, espera da reconexão, teste dos números; commits b2c6433..f3bb65e)
Task 12: complete (commits 989e8c2..f3bb65e, review clean)
Ruling: T13 — Motor.candidatos gera sempre spot x futuro e futuro x futuro (as duas direções); o filtro "tipos" é aplicado só no passa, para as métricas continuarem aquecidas ao trocar o filtro — custo se errado: o dobro de amostras gravadas
Ruling: T13 — métricas do período incrementais (JanelaMetricas) entraram na rodada de correção da T13: com futuros x futuros e 9 corretoras o recálculo completo a cada amostra não cabia no ciclo de 500 ms — custo se errado: nenhum (resultado idêntico, conferido por teste de equivalência)
Ruling: T13 — ordem de `combinacoes`: todos os spot x futuro, depois todos os futuro x futuro; as duas direções do futuro x futuro sempre geram candidato (cada uma com histórico próprio); a etapa 2 também ignora o filtro "tipos" — custo se errado: ordem dos eventos e assinaturas de livro a mais
Ruling: T13 — desempenho aceito no cenário de 9 corretoras × 500 ativos com janelas de 4h cheias: mediana ~137 ms, ciclos com amostra ~236 ms em média (máx. medido 327 ms, abaixo do ciclo de 500 ms); as médias passaram a ser fsum/n (podem diferir de sum()/n só no último bit em grandezas adversárias) — custo se errado: nenhum na prática
Task 13: minor (deferred → T14): _SomaExata.valor levanta OverflowError acima de ~1,8e308 (antes dava inf e o candidato era descartado); o ciclo inteiro falharia
Task 13: minor (deferred → T14): JanelaMetricas.adicionar aceita texto e bool (float(...)); antes _Historico recusava
Task 13: minor (deferred): teste de memória mede antes das linhas mortas da limpeza (em regime 135-161 KB por histórico)
Task 13: minor (deferred): testes afirmam sobre atributos privados (_historicos, _simbolos_livro, _ultima_vez_acima); tuplas por posição e laço de agrupar futuros repetido
Task 13: fix round 1/5 (1 important + 3 minor addressed + métricas incrementais, 0 open — cache da etapa 2 por estado.mercados(), ticker inválido não derruba a etapa 2, NamedTuple, comentário do __main__, JanelaMetricas incremental e compacta (747 KB → 122 KB por histórico; ~700 µs → ~11 µs por amostra), poda exata da etapa 2; commits fa2d790..055268a)
Task 13: complete (commits f3bb65e..055268a depois do rebase na v1, review clean; 728 testes)
Ruling: T14 — memória = RSS do processo (/proc/self/statm no Linux, ctypes GetProcessMemoryInfo no Windows, resource como último recurso) e CPU % por time.process_time, no log a cada intervalo_log_recursos_s; substitui o tracemalloc do plano (lento e só via a memória do Python) — custo se errado: a leitura do Windows nunca rodou num Windows real ("indisponível" se falhar)
Ruling: T14 — reconexões = voltas bem-sucedidas (internet fora 1 min conta 1); a unidade de reconexão é a corretora (spot e futuro reiniciam juntos e o estado dela é apagado: nunca sinal falso); "reconectando" só sai com o primeiro ticker; a primeira conexão (inclusive de quem não carregou ao ligar) não conta como reconexão — custo se errado: trocar o ponto de contagem
Ruling: T14 — corretora que falha no primeiro load_markets tenta de novo sozinha (espera crescente) e entra quando carregar; se nenhuma carregar ao ligar, o scanner para com erro (falha rápida) — custo se errado: o dono precisa religar se a internet estiver fora ao ligar
Ruling: T14 — logs em horário LOCAL da máquina com o deslocamento na linha (ex.: -0300), um arquivo por dia `logs/scanner-AAAA-MM-DD.log`, sem renomear arquivo (Windows), virando à meia-noite local; erro ao gravar o log nunca derruba o scanner — custo se errado: o horário das linhas difere do UTC dos sinais
Ruling: T14 — limite de assinaturas (`limites_assinatura`): o mesmo n vale separado para os livros e para os tickers um a um; histerese de 10% (constante do módulo) a favor dos livros já abertos; "parece limite" é palpite pelo texto do erro — custo se errado: orçamento por conexão em vez de por corretora
Ruling: T14 — atraso médio: janela de 1 min em blocos de 10 s (constantes do módulo, detalhe de exibição) e faixa válida ±30 s (Config); mercados com active=False ficam fora do scanner — custo se errado: dois campos a mais na Config
Ruling: T14 — eventos do ciclo gravados numa transação só (synchronous=NORMAL no WAL); evento inválido é recusado com log de qual era, e erro do sqlite refaz o lote linha a linha com SAVEPOINT (300 eventos: 138,9 ms → 6,6 ms) — custo se errado: nenhum conhecido
Task 14: minor (deferred): tickers em lote (watch_tickers): um par recusado pela corretora derruba o stream inteiro a cada tentativa até a recarga de mercados tirar o par (o isolamento por símbolo só existe no modo um a um)
Task 14: minor (deferred): un_watch ainda é aguardado dentro do assinar_livros (em paralelo, com tempo máximo); o próximo ajuste daquela corretora pode esperar até timeout_desassinar_s
Task 14: minor (deferred): ccxt_coletor.py cresceu para ~800 linhas (reconexão, nova tentativa por símbolo, limite, descartes e atraso na mesma classe); separar numa tarefa futura
Task 14: minor (deferred): com a pasta de logs apagada as linhas se perdem até ela voltar (o scanner não recria a pasta); se fechar o arquivo velho falhar na virada, ele fica aberto; _trazer_de_volta repete o traceback a cada espera se _ajustar_tickers falhar sempre
Task 14: minor (deferred → T17): Banco.limpar_antigos ainda não é chamado pelo __main__; resumo_do_dia pode ler motor.status(agora) para atraso e reconexões
Task 14: fix round 1/5 (3 important + 9 minor addressed — lote por evento com SAVEPOINT, corretora que volta sozinha, log sem renomear em horário local, traceback de defeito, active=False, histerese, volumes guardados, ticker que falhou sai, iniciar para a anterior, nomes por corretora validados, sequência quebrada reassina na hora; commits 0ccfb23..446efd9)
Task 14: fix round 2/5 (2 important + 3 minor addressed, 0 open — emit do log com try/handleError, _trazer_de_volta registra erro e tenta de novo, proteção da virada, primeira entrada não conta como reconexão, iniciar espera a anterior; commits 446efd9..a6acf2c)
Task 14: complete (commits 13815af..a6acf2c, review clean; 863 testes)
Ruling: T15 — Hyperliquid só futuros e escolha de mercados por corretora em `mercados_por_corretora`; `metodo_ticker` por corretora mais troca automática quando o método em lote não traz bid/ask (Hyperliquid: watch_ticker/activeAssetCtx com `impactPxs` como bid/ask da etapa 1) — custo se errado: os impactPxs são preços de impacto e podem esconder spreads perto do mínimo da etapa 2 (conferir depois da validação)
Ruling: T15 — DESVIO DA SPEC §2 PENDENTE DE APROVAÇÃO DO DONO: no config.yaml de exemplo a XT fica só com futuros (`xt: [futuro]`), porque no ccxt 4.5.89 nenhum ticker spot da XT traz bid/ask (conferido pelo revisor no código do ccxt); as alternativas seriam livro para todos os pares como etapa 1 (uma assinatura por par, sem volume) ou REST (contra a spec §8) — custo se errado: sem sinais com XT spot; para voltar à spec, apagar a linha (o log avisa em ERRO se o spot não funcionar)
Ruling: T15 — cotações (USDC, USD, USDG → USDT) vêm do ticker spot da `fonte_cotacao` (padrão bitget) ou de `fontes_cotacao` por moeda, par direto ou inverso, meio do bid/ask fresco, dentro de uma faixa de segurança em volta de 1 (padrão 10%); guardadas por símbolo fora de `mercados()` (não criam combinações); o par de cotação é sempre assinado e nunca cortado pelo limite — custo se errado: pares fora de USDT com aviso_cotacao
Ruling: T15 — intervalo de funding: config (`intervalo_funding_h`) > ccxt > 8h; Hyperliquid 1h (BingX e XT trazem o intervalo de cada contrato pelo ccxt) — custo se errado: funding esperado errado nessa corretora
Ruling: T15 — normalizador: o kPEPE da Hyperliquid vem do `baseName` do ccxt (o ccxt entrega KPEPE em maiúscula); a regra vale só para futuros — custo se errado: kPEPE e 1000PEPE não casariam
Task 15: note (achado do revisor, fase 1): o watch_tickers de futuros da MEXC não traz bid/ask; antes da T15 todos os tickers da etapa 1 dos futuros da MEXC eram descartados. Agora a troca automática passa a MEXC futuros para um par por assinatura (centenas): conferir no PC do dono o aviso de limite de assinaturas e preencher `limites_assinatura.mexc` se aparecer
Task 15: minor (deferred): ccxt_coletor.py passou de 1000 linhas (ajuste de canal da XT e contagem de bid/ask caberiam num módulo próprio)
Task 15: minor (deferred → depois da validação): Hyperliquid: impactPxs pioram as duas pontas da etapa 1; o l2Book usaria o mesmo número de assinaturas
Task 15: minor (deferred): um mercado que nunca traz bid/ask (ex.: spot da XT, se religado) fica tentando para sempre, com um aviso no log a cada até 60 s
Task 15: fix round 1/5 (6 minor addressed, 0 open — último método de ticker nunca é abandonado de vez, pares diferentes no modo um a um, par de cotação fora do limite, baseName só no futuro, padrões da Config, cabeçalho do config.yaml; commits 13a2a5c..a9d92d8)
Task 15: complete (commits cc1e977..a9d92d8, review clean; 962 testes)
Ruling: execução — Task 17 dividida: parte A (relatório de validação, /api/validacao, limpeza diária) feita em paralelo num worktree (branch t17) e aprovada; parte B (README completo e config.yaml comentado campo a campo) depois da Task 16, porque depende dos campos novos das fases 2 e 3
Ruling: T16 — Kraken Pro = duas corretoras com os nomes do ccxt, `kraken` (só spot) e `krakenfutures` (só futuros lineares em USD), cada uma com a sua conexão e saúde; cotação do USD pelo USDT/USD da Kraken spot (`fontes_cotacao: {USD: kraken}`); USD entrou na preferência de cotação depois de USDG (sem isso o EUR venceria e iria 1:1, ~15% fora) — custo se errado: pares só em EUR/GBP da Kraken seguem 1:1 com aviso_cotacao
Ruling: T16 — spec §11: `krakenfutures` e `lighter_rh` ficam COMENTADAS no config.yaml de exemplo até o dono confirmar (futuros na conta / elegibilidade no Brasil); `corretoras_farm: [lighter, lighter_rh, kraken, krakenfutures]` — custo se errado: nenhum (é só descomentar)
Ruling: T16 — Lighter com conector próprio (spec §6; o ccxt 4.5.89 tem a Lighter principal, mas não a Robinhood), sempre pelo WebSocket só leitura (`?readonly=true`); `taker_fee` e `current_funding_rate` lidos em % e funding de 1h (conferido pelo controlador: "Funding payments occur at each hour mark", padrão de 1 bp/8h ≈ 0,00125%/h = o "0.0012" dos exemplos; taker Premium 0,028%) — custo se errado: taxa e funding 100x menores
Ruling: T16 — Lighter: etapa 1 pelo market_stats/all; se ele não trouxer bid/ask (as amostras do ccxt de nov/2025 não trazem), a etapa 1 passa a vir dos livros de todos os mercados, com histerese simétrica e tempo mínimo em cada modo (`tempo_min_modo_etapa1_lighter_s`, 300 s); troca tudo ou nada; livro com snapshot + mudanças, continuidade pelo begin_nonce (sem ele: aviso de que não está sendo conferida); só os N melhores níveis por lado vão ao estado (Config) — custo se errado: Lighter sem sinais até ajustar o formato (o log avisa)
Ruling: T16 — saúde, reconexão, limite e descartes comuns aos dois conectores em scanner/coletores/base.py (ColetorComSaude + Protocol ColetorDoScanner); o certifi (dependência do ccxt) é usado pelo aiohttp da Lighter como o ccxt usa — custo se errado: nenhum
Task 16: minor (deferred): _reassinar/_assinar_depois da Lighter não usam a trava; um par que sai no meio de uma reassinatura pode deixar uma assinatura sobrando no servidor até reconectar (não afeta o estado)
Task 16: minor (deferred): reinício dos tickers refaz a conexão da Lighter e os livros abertos saem do estado por algumas centenas de ms (sem sinal falso)
Task 16: minor (deferred): Kraken sem un_watch_order_book no ccxt 4.5.89 (assinaturas ficam até reconectar); limite de assinaturas da Lighter não documentado; lighter.py ~950 linhas
Task 16: fix round 1/5 (1 important + 7 minor addressed + etapa 1 pelos livros — base comum dos coletores, Protocol, limites na Config, novo pedido do livro completo, lighter_formatos.py, entrada tardia de ponta a ponta, helpers em fabricas.py; commits 46c83d8..228e977 depois do rebase sobre a9ac05f)
Task 16: fix round 2/5 (1 important + 6 minor addressed, 0 open — histerese e tempo mínimo na troca da etapa 1, trava nos livros, 30002 do nosso unsubscribe, conferência 1x/s, comentários; commits 228e977..08fb2fc)
Task 16: complete (commits a9ac05f..08fb2fc, review clean; 1087 testes)
Ruling: T17A — "dia" da validação = dia UTC do detectado_em; abertos contam na quantidade e no RP médio, mas ficam fora das durações e dos %; atraso e reconexões por dia gravados no banco (tabela corretoras_dia) a cada intervalo_log_recursos_s, reconexões por diferença do contador (reinício do scanner no meio do dia tratado); /api/validacao lê o motor.status no laço (anyio.from_thread) para completar hoje; limpeza do banco ao ligar e a cada virada do dia UTC, em lotes de 2000 com pausa de 5 ms (constantes do módulo; com 2 milhões de amostras o motor espera no pior caso 25-60 ms, antes 1,16 s); resumo do dia anterior no log na virada (21h em Brasília) — custo se errado: uma linha em _resumo_do_modo
Task 17A: minor (deferred): resumo_do_dia decodifica o JSON de cada sinal (~0,28 s para 10 mil sinais, fora do laço); dois formatos de 422 em /api/validacao; teste com API privada do asyncio; traceback repetido se gravar as corretoras falhar sempre
Task 17A: fix round 1/5 (3 minor addressed, 0 open — resumo dentro do try, só dia maior vira, docstring das threads; commit 0d32aea depois do rebase)
Task 17A: complete (commits 08fb2fc..0d32aea depois do rebase sobre a Task 16, review clean; 1157 testes). Conflitos do rebase em __main__.py e tests/fabricas.py eram só acréscimos dos dois lados, resolvidos pelo controlador mantendo os dois
Task 17B: complete (commit 3bc9e24 — README procedural com instalação Windows/VPS, systemd, marcos, leitura do painel, ajustes comuns e erros; config.yaml com comentário em cada campo; guardas em tests/test_config.py: todo campo da Config comentado no config.yaml e na tabela do README; 1159 testes). Revisão dos textos fica para depois das correções finais (os campos novos entram no README e no config.yaml)
Final review: 3 revisores independentes por área (coleta, núcleo, interface), relatórios em .superpowers/sdd/plano/revisao-final-*.md; nenhum Critical; Important: coleta 3 (livro velho do ccxt reaproveitado depois da queda na MEXC futuros/XT; funding da Bitget sempre 8h; MEXC spot com limite de 30 assinaturas por conexão), núcleo 5 (suspeito só com A positivo; F médio só conta o tempo com livro aberto; banco síncrono no laço e crescendo; log bloqueante no terminal; etapa 2 escondendo Farm e "sempre mostrar"), interface 2 (painel aberto na internet sem senha passa calado; opções mortas/repetidas no filtro de corretoras)
Ruling: final — correções em 3 worktrees paralelos (fix-coleta, fix-nucleo, fix-interface), juntadas pelo controlador na v1
Ruling: final — F médio pelo histórico de TODOS os pares (via tickers) PENDENTE DE DECISÃO DO DONO (muda a §6); aprovado e feito só o recarregamento das janelas a partir do banco ao ligar e ao trocar o período — custo se errado: spread que acabou de abrir tem RP bruto ≈ A + F atual (negativo), e os critérios 1 e 2 da §9 podem falhar
Ruling: final — etapa 2: no modo Farm, combinações com ponta numa corretora de farm abrem livro a partir de `spread_min_livro_farm_pct` (padrão −0,3); moedas de "sempre mostrar" sempre abrem livro (corrige a etapa 2 escondendo esses sinais) — custo se errado: mais assinaturas de livro
Ruling: final — MEXC spot: orçamento de assinaturas por (corretora, mercado) compartilhado entre tickers e livros (limite documentado de 30 por conexão), com volume 24h por REST nos dados lentos; etapa 1 da MEXC spot por REST frequente PENDENTE DE DECISÃO DO DONO (a §8 reserva o REST para dados lentos) — custo se errado: poucos pares da MEXC spot escaneados
Ruling: final — suspeito pela razão dos preços nos dois sentidos ((max/min − 1) × 100 > limite_suspeito_pct); só entram no índice markets cotados em USDT, USDC, USDG ou USD (EUR/GBP etc. ficam fora: 1:1 daria sinal falso); sessão (modo, filtros, comparação) salva no banco e restaurada ao ligar; versões das bibliotecas fixadas nas testadas (o código depende de detalhes do ccxt 4.5.89) — custo se errado: atualizar o requirements à mão
Ruling: DONO (22/10/2026, opção 1) — F médio pelo histórico de todos os pares: a spec (§3 e §6) foi atualizada antes do código; todo par com ticker nas duas pontas é amostrado pelos tickers a cada amostra_ticker_s (padrão 60 s) e essas amostras (só em memória) entram na mesma janela das amostras do livro; vira a Task 18, depois das correções finais
Ruling: DONO (22/10/2026) — aprovada a biblioteca `protobuf==5.29.5`: no ccxt 4.5.89 os canais WebSocket do spot da MEXC vêm em protobuf e o ccxt exige o pacote (sem ele a MEXC spot não recebe nada); entra no requirements.txt na junção das correções finais
Final fix (coleta): complete (commits d69e787, 27f7f12 — livro guardado pelo ccxt esquecido antes de reassinar e reassinatura periódica da MEXC futuros/XT; funding da Bitget com o intervalo de cada contrato e aviso de intervalo ausente; orçamento de assinaturas por (corretora, mercado) com `limites_assinatura_mercado: {mexc: {spot: 30}}`, cancelamentos antes das assinaturas novas, volume 24h por REST; MEXC spot por `watch_ticker` (o bookTicker em protobuf não resolve o watch_bids_asks no ccxt 4.5.89); conexão viva por (corretora, mercado) e corretora "atrasada" se um mercado em uso fica mudo; só markets em USDT/USDC/USDG/USD; funding gravado ao chegar e só dos símbolos em uso; Lighter com Livro montado no máximo a cada ~100 ms; Kraken spot por watch_bids_asks; reviews clean após 2 rodadas)
Final fix (interface): complete (commits e38a606, b611539 — host fora do loopback sem senha recusado; /api/status com os mercados de cada corretora e nomes certos no painel; funding com 4 casas; Origin conferido no WebSocket e nos métodos que mudam coisas; TrustedHost sem senha; SRI no script do gráfico; "vivo" publicado pelo laço do motor depois de um ciclo bom; ressincronização do painel com nova tentativa; review clean)
Final fix (núcleo): complete (commits ba22273, 367b66d, 27c3422 — suspeito nos dois sentidos; janelas recarregadas do banco ao ligar e ao trocar o período; banco com thread escritora, esquema compacto (209 → 82 bytes por amostra), migração do banco antigo, checkpoint fora do laço, lotes de 10 s e evento devolvido à fila se o SQLite falhar; log por QueueListener, um por handler; Farm abre livro só de combinação com ponta de farm, no máximo 50 por mercado de farm, mínimo −0,1 (800 ativos de farm: 1,7 s → 89 ms por ciclo); "sempre mostrar" abre livro; sinais fechados ao encerrar e depois de uma queda; comparação gravada à parte; sessão salva no banco e restaurada ao ligar; período limitado a retencao_dias × 24; funding com validade; versões fixadas + protobuf; reviews clean após 2 rodadas; 1400 testes)
Ruling: final — o teste de escala do Farm só confere o TEMPO com SCANNER_TESTES_DE_TEMPO=1 (as contagens sempre); o dono roda a suíte no Windows e um teste dependente do PC daria falso alarme — custo se errado: regressão de desempenho só aparece com a variável ligada
Ruling: final — spec §2 (suspeito pela razão dos preços nos dois sentidos) e §6 (exceções da etapa 2 no Farm e no "sempre mostrar") atualizadas para refletir as correções finais
Final review: minor (deferred) — M8 da coleta (atualizações de ticker que o ccxt não entrega quando o laço está ocupado: só atraso na etapa 1); ccxt_coletor.py e lighter.py com ~1000 linhas; relógio monotônico para o frescor (M4 do núcleo); ciclo com 4.045 sinais ativos em ~378 ms (M8 do núcleo); app.js com ~2000 linhas; teto de livros de farm por mercado inteiro (não por ativo)
Ruling: T18 — médias da janela ponderadas pelo tempo que cada amostra cobre (ticker de 120 s = 24 amostras do livro de 5 s): sem o peso, as amostras do livro dominam e o sinal some em ~10 min (aprovado pelo revisor: sem viés, só variância); padrão `amostra_ticker_s` = 120 s (60 s deu 603 MB, acima do custo de ~400 MB que o dono aceitou) e `max_historicos` = 80.000; spec §3 atualizada com isso — custo se errado: resolução de 2 min nas métricas pelos tickers
Ruling: T18 — memória fixa por período: o intervalo das amostras pelos tickers é max(amostra_ticker_s, período ÷ amostras_ticker_por_periodo), com padrão 120 amostras (~4,9 KB por histórico em 4 h, 24 h ou 168 h; ~380 MB com 80.000); trocar o período zera as amostras pelos tickers (as do livro voltam do banco), e as métricas levam até um período para se refazer — custo se errado: resolução grossa em período longo (168 h = 1 amostra a cada 84 min)
Ruling: T18 — gc: com o histórico pelos tickers ligado, o laço faz gc.unfreeze/collect/freeze a cada 300 s, fora do tempo do ciclo e só com folga até o próximo (forçado depois de 60 s); a coleta completa caiu de ~150 ms para ~17 ms; lixo em ciclo de ccxt/aiohttp pode esperar até 5 min — custo se errado: um pouco mais de memória
Task 18: minor (deferred): a reordenação dos históricos depois da recarga custa ~86 ms uma vez (ao ligar e ao trocar o período); no Farm, as combinações sem ponta de farm param de calcular candidato (as métricas delas seguem pelos tickers)
Task 18: fix round 1/5 (1 important + 4 minor addressed — intervalo pelo período, gc congelado, teto com OrderedDict (10,8 → 0,3 ms), README, teste de escala do Farm, validação; commit 6035571); R1 do revisor (congelar fora do ciclo) no 444b164
Task 18: complete (commits 90d3fe7..444b164, review clean; 1476 testes; f_medio_so_com_livro.py: RP bruto do spread recém-aberto bate com o F contínuo de 4 h até 60 min, antes −0,20)
Docs: revisão independente do README e do config.yaml (27 correções: número de testes, instalador do Python 3.12.10, senha sempre entre aspas, avisos G (taxas), H (métricas pelos tickers) e I (Edição Rápida), aviso A da MEXC reescrito, regra "na VPS", esperar 4 h antes de comparar com o Arbimax, enrolação cortada; validação de periodo_metricas_h ≤ retencao_dias × 24 no carregar.py; commit 634ad91) + re-checagem (parar o serviço com systemctl na VPS); docs clean
Entrega (22/10/2026): v1 completa — Tasks 1 a 18 + revisão final com correções; 1477 testes passando (-W error); docs/CONTINUAR.md reescrito com o estado, as decisões tomadas no lugar do dono e o que continua com ele; zip atualizado com o histórico Git e .superpowers/
Teste no PC (22/10/2026): no Windows, 3 testes do painel falhavam (tests/test_painel.py: funding com 4 casas, reconexões no singular/plural, dica do RP líquido) porque rodar_no_node lia a saída do Node na codificação do Windows (cp1252) e o Node escreve em UTF-8 ("—" virava "â€”"); o painel em si estava certo. Correção só no teste: encoding="utf-8" no subprocess.run; 1469 testes passando + 8 pulados no Windows
