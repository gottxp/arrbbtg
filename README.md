# Scanner de Arbitragem

1. [Instalar no Windows](#1-instalar-no-windows)
2. [Rodar no Windows](#2-rodar-no-windows)
3. [Instalar e rodar na VPS (Ubuntu)](#3-instalar-e-rodar-na-vps-ubuntu)
4. [Conferir cada marco](#4-conferir-cada-marco)
5. [Ler o painel](#5-ler-o-painel)
6. [Mudar a configuração](#6-mudar-a-configuração)
7. [Atualizar para uma versão nova](#7-atualizar-para-uma-versão-nova)
8. [Quando algo dá erro](#8-quando-algo-dá-erro)

Regra para todos os comandos: rode sempre **dentro da pasta do projeto** (a que tem o `config.yaml`). O log, o banco e a configuração são procurados na pasta onde o comando roda.

---

## 1. Instalar no Windows

1. Instale o Python 3.12:
   - Abra <https://www.python.org/downloads/windows/> e baixe o **Windows installer (64-bit)** da versão 3.12 mais nova.
   - Na primeira tela do instalador, marque **Add python.exe to PATH** e clique em **Install Now**.
2. Copie a pasta do projeto para `C:\scanner-arbitragem`. Se veio em zip: clique com o botão direito no zip, **Extrair tudo...**, e mova a pasta para que o caminho fique `C:\scanner-arbitragem\config.yaml`.
3. Abra o PowerShell (menu Iniciar, digite `PowerShell`, Enter) e entre na pasta:

   ```powershell
   cd C:\scanner-arbitragem
   Test-Path config.yaml
   ```

   Tem que aparecer `True`. Se aparecer `False`, a pasta está um nível acima ou abaixo: corrija o passo 2.
4. Confira o Python:

   ```powershell
   python --version
   ```

   Tem que aparecer `Python 3.12.` e um número. Outra versão ou erro: veja a [parte 8](#8-quando-algo-dá-erro).
5. Crie o ambiente do projeto (`.venv`) e instale as bibliotecas (leva alguns minutos):

   ```powershell
   if (Test-Path .venv) { Remove-Item -Recurse -Force .venv }
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements-dev.txt
   ```

   A primeira linha apaga uma `.venv` que tenha vindo de outro computador. Perto do fim aparece `Successfully installed ...`. Entre as bibliotecas vem o `protobuf`: sem ele o spot da MEXC não chega (as mensagens dele vêm nesse formato).
6. Confira a configuração:

   ```powershell
   .venv\Scripts\python -m scanner --validar
   ```

   Tem que aparecer `config ok`.
7. Rode os testes automáticos (não acessam a internet, levam menos de 1 minuto):

   ```powershell
   .venv\Scripts\python -m pytest -q
   ```

   A última linha tem que ser parecida com `1159 passed in 21.00s` (versões novas têm mais testes). Pode vir junto `N skipped` (testes que só rodam no Linux ou com o Node instalado). Não pode aparecer `failed` nem `error`.

## 2. Rodar no Windows

1. Ligar (PowerShell):

   ```powershell
   cd C:\scanner-arbitragem
   .venv\Scripts\python -m scanner
   ```

   Em até 1 minuto aparecem linhas como `INFO scanner.principal: mexc: acompanhando 812 pares` e `INFO scanner.api: painel em http://127.0.0.1:8000`. Deixe essa janela aberta: fechar a janela desliga o scanner.
2. Abrir o painel: no navegador, abra <http://localhost:8000>.
3. Parar: clique na janela do PowerShell e aperte `Ctrl+C`. Aparece `Encerrado.`.
4. Logs: um arquivo por dia em `C:\scanner-arbitragem\logs\scanner-AAAA-MM-DD.log`, no horário do PC. Ficam o de hoje e os 30 dias anteriores. Para acompanhar o log de hoje numa segunda janela do PowerShell:

   ```powershell
   cd C:\scanner-arbitragem
   Get-Content "logs\scanner-$(Get-Date -Format yyyy-MM-dd).log" -Wait -Tail 50
   ```

   Para procurar um texto em todos os logs (troque o texto entre aspas):

   ```powershell
   Select-String -Path "logs\scanner-*.log" -Pattern "sinais ativos"
   ```

5. Banco: `C:\scanner-arbitragem\scanner.db` (histórico dos sinais, amostras do gráfico, grupos de filtros e métricas da validação). O scanner apaga sozinho o que passou de 7 dias. Para começar do zero: pare o scanner e apague `scanner.db`, `scanner.db-wal` e `scanner.db-shm`.

## 3. Instalar e rodar na VPS (Ubuntu)

Use uma VPS com **Ubuntu 24.04** (já vem com o Python 3.12). Nos comandos, troque `usuario` pelo usuário da VPS (muitas vezes `root`) e `IP` pelo IP dela.

### 3.1 Instalar

1. No PowerShell do PC, empacote a pasta (sem a `.venv`, os logs e o banco do PC) e mande para a VPS:

   ```powershell
   cd C:\scanner-arbitragem
   tar --exclude=.venv --exclude=logs --exclude=scanner.db* -czf "$HOME\scanner-arbitragem.tgz" .
   scp "$HOME\scanner-arbitragem.tgz" usuario@IP:~/
   ```

2. Entre na VPS (PowerShell do PC):

   ```powershell
   ssh usuario@IP
   ```

   Daqui até o fim da parte 3.4, os comandos são no **terminal da VPS**.
3. Instale o Python e confira a versão:

   ```bash
   sudo apt update
   sudo apt install -y python3 python3-venv
   python3 --version
   ```

   Tem que aparecer `Python 3.12.` e um número. Se aparecer `3.10` ou `3.11` (Ubuntu 22.04), instale o 3.12 assim e, no passo 4, escreva `python3.12` no lugar de `python3`:

   ```bash
   sudo add-apt-repository -y ppa:deadsnakes/ppa
   sudo apt update
   sudo apt install -y python3.12 python3.12-venv
   ```

4. Desempacote, crie o `.venv`, instale e confira:

   ```bash
   mkdir -p ~/scanner-arbitragem
   tar -xzf ~/scanner-arbitragem.tgz -C ~/scanner-arbitragem
   cd ~/scanner-arbitragem
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements-dev.txt
   .venv/bin/python -m pytest -q
   ```

   A última linha tem que mostrar `passed` (pode ter `skipped`), sem `failed` nem `error`.
5. Deixe o relógio da VPS no horário de Brasília (o log usa o horário da máquina):

   ```bash
   sudo timedatectl set-timezone America/Sao_Paulo
   ```

### 3.2 Usuário e senha do painel

1. Abra o `config.yaml`:

   ```bash
   cd ~/scanner-arbitragem
   nano config.yaml
   ```

2. No bloco `painel:` (perto do fim do arquivo), troque `usuario: null` e `senha: null` pelo seu usuário e senha. Use só letras e números, ou ponha a senha entre aspas. Deixe `host: 127.0.0.1` para usar o túnel SSH (recomendado, parte 3.5); só troque para `host: 0.0.0.0` se for abrir a porta na internet (parte 3.5, opção B). Com `host: 0.0.0.0` sem usuário e senha, o scanner não liga. Fica assim (as linhas de comentário com `#` entre os itens podem ficar):

```yaml
painel:
  host: 127.0.0.1
  porta: 8000
  usuario: dono
  senha: "troque-por-uma-senha-forte"
```

3. Salve com `Ctrl+O`, `Enter`, e saia com `Ctrl+X`.
4. Confira:

   ```bash
   .venv/bin/python -m scanner --validar
   ```

   Tem que aparecer `config ok`.

### 3.3 Serviço systemd (religa sozinho e liga junto com a máquina)

1. Crie o arquivo do serviço. Copie e cole o bloco inteiro de uma vez, até a linha `EOF` (se o terminal avisar que o texto tem várias linhas, confirme). Ele já preenche o seu usuário e a pasta:

```bash
sudo tee /etc/systemd/system/scanner.service > /dev/null <<EOF
[Unit]
Description=Scanner de arbitragem
After=network-online.target
Wants=network-online.target

[Service]
User=$(whoami)
WorkingDirectory=$HOME/scanner-arbitragem
ExecStart=$HOME/scanner-arbitragem/.venv/bin/python -m scanner
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
```

2. Confira o arquivo: as linhas `User=`, `WorkingDirectory=` e `ExecStart=` têm que mostrar o seu usuário e a pasta `.../scanner-arbitragem`.

   ```bash
   cat /etc/systemd/system/scanner.service
   ```

3. Ligue o serviço e confira:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now scanner
   systemctl status scanner
   ```

   Tem que aparecer `active (running)`. Aperte `q` para sair do status.
4. Comandos do dia a dia:

   ```bash
   sudo systemctl restart scanner
   sudo systemctl stop scanner
   sudo systemctl start scanner
   systemctl status scanner
   ```

   `restart` depois de mudar o `config.yaml`; `stop` para desligar (ele volta a ligar sozinho no próximo boot, a não ser que você rode `sudo systemctl disable scanner`).

### 3.4 Ver o log na VPS

```bash
sudo journalctl -u scanner -f
```

(acompanha ao vivo; `Ctrl+C` para sair; o scanner continua rodando)

```bash
tail -f ~/scanner-arbitragem/logs/scanner-$(date +%F).log
```

(o arquivo do dia; `Ctrl+C` para sair)

```bash
grep -h "sinais ativos" ~/scanner-arbitragem/logs/scanner-*.log
```

(procura um texto em todos os dias; troque o texto entre aspas)

### 3.5 Abrir o painel da VPS no seu PC

O painel usa HTTP sem HTTPS: pela internet, a senha viaja em texto aberto. Por isso o túnel SSH é o caminho recomendado.

**Opção A, túnel SSH (recomendada).** Com `host: 127.0.0.1` no `config.yaml`:

1. No PowerShell do PC:

   ```powershell
   ssh -L 8000:127.0.0.1:8000 usuario@IP
   ```

   Deixe essa janela aberta enquanto usa o painel.
2. No navegador do PC, abra <http://localhost:8000> e digite o usuário e a senha do `config.yaml`.

**Opção B, porta aberta na internet.** Com `host: 0.0.0.0`, `usuario` e `senha` preenchidos:

1. No terminal da VPS:

   ```bash
   sudo ufw allow OpenSSH
   sudo ufw allow 8000/tcp
   sudo ufw enable
   sudo systemctl restart scanner
   ```

   Responda `y` se o `ufw enable` perguntar. Se o provedor da VPS tiver firewall no painel dele, libere a porta 8000 lá também.
2. No navegador, abra `http://IP:8000` e digite o usuário e a senha.

---

## 4. Conferir cada marco

Faça os marcos nesta ordem. Os comandos são do Windows (PowerShell, na pasta `C:\scanner-arbitragem`); na VPS troque `.venv\Scripts\python` por `.venv/bin/python`.

### Marco 1: preços de cada corretora

1. Rode (Ctrl+C para parar):

   ```powershell
   .venv\Scripts\python -m scanner.ferramentas.imprimir_precos mexc gate bitget bingx xt hyperliquid kraken lighter
   ```

   A cada 5 segundos aparece um bloco assim (o horário do cabeçalho é UTC, 3 horas a mais que Brasília):

   ```text
   --- 2026-10-22T15:04:12Z ---
   mexc     spot    BTC/USDT         bid 67012.5        ask 67012.6
   mexc     futuro  BTC/USDT:USDT    bid 67010.1        ask 67010.2
   ```

2. Confira que aparecem estes pares (BTC, ETH e SOL de cada um):

   | Corretora | O que aparece |
   | --- | --- |
   | mexc, gate, bitget, bingx | spot (`BTC/USDT`) e futuro (`BTC/USDT:USDT`) |
   | xt | só futuro (`BTC/USDT:USDT`); veja o [aviso B](#aviso-b-xt-só-futuros-decisão-pendente) |
   | hyperliquid | só futuro, em USDC (`BTC/USDC:USDC`) |
   | kraken | só spot (`BTC/USDT`) |
   | lighter | só futuro, em USDC (`BTC/USDC:USDC`) |

   `sem dados ainda` só pode aparecer nos primeiros segundos. Uma linha `Erro: ... nao consegui carregar a lista de mercados (...)`: anote a mensagem inteira.
3. Abra o site de cada corretora no mesmo par e compare o melhor preço de compra (bid) e de venda (ask). A diferença tem que ser de até 0,1% (BTC a 67.000: até 67 dólares). Na Hyperliquid e na Lighter o site mostra em USDC, igual ao terminal.

### Marco 2: scanner rodando 30 minutos

1. Ligue o scanner ([parte 2](#2-rodar-no-windows)) e deixe 30 minutos.
2. Procure as linhas que têm que aparecer:

   ```powershell
   Select-String -Path "logs\scanner-*.log" -Pattern "acompanhando|cotacao de|perpetuos ativos|painel em|sinais ativos"
   ```

   | Linha esperada | Quando |
   | --- | --- |
   | `mexc: acompanhando N pares` (uma por corretora) | ao ligar |
   | `cotacao de USDC em USDT: ticker spot USDC/USDT da bitget` | ao ligar |
   | `cotacao de USD em USDT: ticker spot USDT/USD da kraken` | ao ligar |
   | `lighter: N perpetuos ativos; M inativos ficam de fora` | ao ligar |
   | `painel em http://127.0.0.1:8000` | ao ligar |
   | `sinais ativos: N` | a cada minuto, sem buracos |
   | `recursos: memoria ... MB (atual), CPU ...%` e `descartes nos ultimos 300s: ...` | a cada 5 minutos |
   | `mexc: limite de 30 assinaturas no spot (limites_assinatura_mercado), 10 delas para os livros: N de M pares ficaram fora dos tickers` | ao ligar (é o esperado; veja o [aviso A](#aviso-a-mexc-futuros-um-par-por-assinatura)) |
   | `kraken: N pares cotados em outras moedas ficam de fora (EUR ..., GBP ...)` | ao ligar (é o esperado: o scanner só compara preços em USDT, USDC, USDG e USD) |

3. Procure as linhas que **não** deveriam aparecer:

   ```powershell
   Select-String -Path "logs\scanner-*.log" -Pattern "nenhum metodo de ticker|parece de limite|mercado inativo|relogio do PC|fora da faixa|sem cotacao|ficam de fora ate a taxa|nunca mandaram ticker|nao mandou nada|intervalo do funding nao veio|Traceback"
   ```

   | Se aparecer | O que fazer |
   | --- | --- |
   | `nenhum metodo de ticker traz bid/ask` | anote a corretora e o mercado e avise (não pode aparecer para BingX, XT futuro e Hyperliquid) |
   | `parece de limite de assinaturas` | preencha `limites_assinatura` daquela corretora ([aviso A](#aviso-a-mexc-futuros-um-par-por-assinatura) mostra como) |
   | `mercado inativo` com muitos pares da XT | avise (a XT pode estar marcando pares bons como inativos) |
   | `difere do relogio do PC` | acerte o relógio do PC (Windows: Configurações > Hora e idioma > Data e hora > Sincronizar agora); se continuar só com a bingx, avise |
   | `fora da faixa de seguranca` | avise (cotação USDC ou USD estranha) |
   | `sem cotacao de USDC em USDT` ou `sem cotacao de USD em USDT` | a bitget (USDC) ou a kraken (USD) não está ligada ou não conectou |
   | `ficam de fora ate a taxa chegar` | preencha a taxa daquele mercado em `taxas` ([parte 6](#6-mudar-a-configuração), ajuste 1) |
   | `N de M pares pedidos no spot nunca mandaram ticker em 60s` (ou no futuro) | anote a corretora e o N. Se N for grande, a corretora está recusando assinaturas: preencha `limites_assinatura_mercado` dela (como a linha `mexc: spot: 30` do `config.yaml`) e avise |
   | `o intervalo do funding nao veio ... eles usam 8h` | confira no site da corretora de quantas em quantas horas ela cobra e preencha `intervalo_funding_h` dela ([parte 6](#6-mudar-a-configuração)) |
   | `o livro pedido nao mandou nada em 60s` | anote a corretora e o par. Se acontecer com muitos pares da mesma corretora, ela está recusando assinaturas: preencha `limites_assinatura_mercado` dela e avise |
   | `Traceback` | copie as linhas em volta e avise |

4. Confira os sinais sem o painel: abra <http://localhost:8000/api/sinais>. Aparece uma lista (pode ser `[]` se não houver oportunidade agora).

### Marco 3: painel

Com o scanner ligado, abra <http://localhost:8000> e confira:

1. **Sinal novo em até 1 s:** quando uma linha nova aparece, o horário `desde HH:MM:SS` dela é o horário do relógio do PC (no máximo 1 s de diferença). (O log não escreve cada sinal: só `sinais ativos: N` a cada minuto.)
2. **Filtro sem recarregar:** digite `1` em **Spread mín. (A atual)**. A tabela muda sozinha, sem apertar F5. Apague o `1` e ela volta.
3. **Pausar:** clique em **Pausar**. Aparece `PAUSADO` no rodapé e a tabela para. Clique em **Continuar** e ela volta a mudar.
4. **Gráfico das últimas 4 h:** clique no botão de gráfico da coluna Histórico de um sinal. A janela mostra `Período: 4h` e o eixo do tempo cobre as últimas 4 horas (com o scanner ligado há pelo menos 4 h; antes disso, desde que o par apareceu).
5. **Horários em Brasília:** `Última atualização`, `desde` e o gráfico mostram o horário de Brasília. (O JSON da API mostra UTC, terminando em `Z`: 3 horas a mais.)
6. **Corretoras:** no rodapé, todas com bolinha verde (conectada). Laranja = reconectando; vermelha = atrasada.
7. **PEPE:** digite `PEPE` em **Buscar moeda**. Se aparecer um sinal entre o futuro da Hyperliquid (kPEPE) e a MEXC ou BingX (1000PEPE), os preços das duas pontas têm que estar próximos (é o mesmo ativo, já ajustado).
8. **Farm com Lighter:** clique em **Farm**. Os sinais com ponta na Lighter mostram `Custo a cada $1.000`, e os preços batem com o app da Lighter.
9. **Última atualização sem sinais:** com a tabela vazia (ex.: digite `99` em **Spread mín. (A atual)**), o horário de `Última atualização` muda a cada 5 s. Apague o `99`.
10. **Corretoras 2 (ponta short):** abra a lista. Só aparecem os mercados ligados: `Hyperliquid futuros` sim, `Hyperliquid spot` não; `Kraken Pro spot` e `Kraken Futuros` (com a krakenfutures ligada) aparecem uma vez cada.
11. **Funding com 4 casas:** num sinal com ponta na Hyperliquid ou na Lighter, o `FR` aparece com 4 casas, ex.: `+0,0012% (1h)`.

### Marco 4: testes técnicos (especificação, seção 9)

**Internet desligada 1 minuto** (no PC):

1. Com o scanner ligado há 10 minutos, desligue o Wi-Fi (ou tire o cabo) por 1 minuto e ligue de novo.
2. Durante a queda, o painel mostra as corretoras em laranja (`reconectando`) ou vermelho (`atrasada`), a tabela esvazia e nenhum sinal novo aparece.
3. No log, para cada corretora cuja conexão caiu, aparecem as linhas de queda com a espera crescente (1s, 2s, 4s, 8s, 16s, 32s...) e depois a de volta. A primeira pode demorar alguns segundos depois do corte, e o texto entre parênteses muda:

   ```text
   WARNING scanner.coletores: gate: a conexao dos tickers caiu (NetworkError: ...); tentando de novo em 1s (tentativa 1).
   WARNING scanner.coletores: gate: a conexao dos tickers caiu (NetworkError: ...); tentando de novo em 2s (tentativa 2).
   WARNING scanner.coletores: gate: a conexao dos tickers caiu (NetworkError: ...); tentando de novo em 4s (tentativa 3).
   INFO scanner.coletores: gate: tickers de volta depois de 63.2s fora
   ```

   Na Lighter o texto é `lighter: a conexao da lighter caiu (...)`. Também podem aparecer linhas `livro parou (...); tentando de novo em ...`: é normal na queda. Toda corretora com linha `caiu` tem que ter depois a linha `tickers de volta depois de`:

   ```powershell
   Select-String -Path "logs\scanner-*.log" -Pattern "caiu|de volta depois"
   ```

4. Depois da volta: todas as corretoras verdes de novo e o scanner continua rodando (nenhuma linha `Erro:` na janela). Passando o mouse numa corretora que caiu, aparece `1 reconexões` (ou mais).

**24 horas seguidas:**

1. No PC, desligue a suspensão (Configurações > Sistema > Energia > Quando conectado, colocar o dispositivo em suspensão após: Nunca). Na VPS não precisa.
2. Deixe 24 h ligado e veja as linhas `recursos:`:

   ```powershell
   Select-String -Path "logs\scanner-*.log" -Pattern "recursos:"
   ```

   ```text
   INFO scanner.principal: recursos: memoria 182.4 MB (atual), CPU 9.1%, sinais ativos 4, historicos 350
   ```

   Esperado: uma linha a cada 5 minutos, sem buraco; a memória sobe nas primeiras 4 horas e depois fica estável (não cresce o dia todo). `memoria indisponivel`: veja o [aviso F](#aviso-f-memória-no-windows).

Os outros dois testes da seção 9 (corretora com dado atrasado e par sem liquidez) são conferidos pelos testes automáticos (`tests/test_estado.py` e `tests/test_motor.py`).

### Marco 5: validação de 3 dias

1. Deixe o scanner ligado 3 dias seguidos (VPS, ou PC que não dorme).
2. No painel, ligue **Modo comparação** e use os mesmos filtros e as mesmas corretoras do Arbimax (crie um grupo, por exemplo `Validacao`; [parte 5](#52-filtros-e-grupos)). Com o modo comparação, o mínimo do modo Lucro vale para o RP bruto, como no Arbimax.
3. Várias vezes por dia, com o Arbimax aberto ao lado, anote:
   - para cada sinal do Arbimax: se aparece no scanner em até 5 s;
   - para os que aparecem nos dois: A, F e RP bruto do Arbimax e do scanner;
   - em alguns sinais: o preço de uma ponta no scanner e no site da corretora, no mesmo instante.
4. Todo dia depois das 21:00 (Brasília) o dia UTC fechou. Veja as métricas do dia que acabou (troque a data):

   ```text
   http://localhost:8000/api/validacao?dia=2026-10-23
   ```

   Vem um JSON com, para `lucro` e `farm`: `quantidade_sinais`, `abertos`, `rp_liquido_medio_pct`, `duracao_media_s`, `pct_sumiu_5s`, `pct_sumiu_30s`; e em `corretoras`: `atraso_medio_ms` e `reconexoes` de cada uma. O mesmo resumo vai para o log:

   ```powershell
   Select-String -Path "logs\scanner-*.log" -Pattern "resumo do dia"
   ```

5. Na VPS, confira que o scanner nunca precisou ser religado pelo systemd:

   ```bash
   systemctl show scanner -p NRestarts
   ```

   Tem que aparecer `NRestarts=0`.
6. Aprovado se os 4 critérios passarem:

   | Critério | Como medir | Passa se |
   | --- | --- | --- |
   | 1. RP bruto igual ao Arbimax | diferença entre os dois RP brutos anotados (0,63% e 0,60% = 0,03 ponto) | até 0,05 ponto percentual |
   | 2. Mesmos sinais | sinais do Arbimax que apareceram no scanner em até 5 s ÷ total do Arbimax | pelo menos 80% |
   | 3. Preço igual ao site | (diferença ÷ preço do site) × 100 | até 0,1% |
   | 4. Sem travar | `NRestarts=0`, `sinais ativos` a cada minuto sem buraco, toda linha `caiu` seguida de `de volta depois de` | tudo certo |

### Avisos específicos

#### Aviso A: MEXC futuros (um par por assinatura)

1. A MEXC usa `watch_ticker` (`metodo_ticker: mexc: watch_ticker` no `config.yaml`): uma assinatura por par, no spot e no futuro (centenas no futuro). Não apague essa linha: sem ela o spot da MEXC fica sem nenhum ticker.
2. Procure se a MEXC reclamou de limite:

   ```powershell
   Select-String -Path "logs\scanner-*.log" -Pattern "mexc.*parece de limite de assinaturas"
   ```

3. Se aparecer, troque no `config.yaml` a linha `limites_assinatura: {}` por:

```yaml
limites_assinatura:
  mexc: 200
```

4. Rode `--validar` e religue. O log passa a dizer `... 200 assinaturas ... N de M pares ficaram de fora (os de menor volume 24h)`: é o esperado. Se o aviso de limite continuar, baixe para `100`.
5. MEXC spot: a MEXC aceita 30 assinaturas por conexão no spot. Com `limites_assinatura_mercado: mexc: spot: 30`, só os 20 pares de maior volume da MEXC spot entram na etapa 1 e sobram 10 para os livros. É o esperado (decisão pendente: a etapa 1 da MEXC spot pela API REST, que precisa da sua aprovação). O ticker da MEXC spot não traz o volume 24h: o `VOL` dela vem pela API REST (ao ligar e a cada `intervalo_dados_lentos_s`).
6. MEXC futuros e XT: a cada 15 minutos (`intervalo_reassinar_livros_s`) cada livro é assinado de novo, porque o ccxt delas não confere a sequência do livro. Por alguns segundos o par fica sem livro: um sinal com MEXC futuros ou XT pode sumir e voltar (o histórico continua no mesmo registro se voltar em até 60 s).

#### Aviso B: XT só futuros (decisão pendente)

O `config.yaml` de exemplo usa só o futuro da XT (`xt: [futuro]` em `mercados_por_corretora`), porque o ccxt 4.5.89 não traz bid/ask no spot da XT. A especificação (seção 2) pede spot e futuros. Decida:

1. Aceitar só futuros: não faça nada.
2. Seguir a especificação: apague a linha `  xt: [futuro]` do `config.yaml`, rode `--validar` e religue. Esperado no log: `ERROR ... xt: nenhum metodo de ticker traz bid/ask no spot (...)`, e o futuro da XT continua. O spot só geraria sinal com a taxa preenchida em `taxas: xt: spot:`.

Avise o desenvolvedor da decisão.

#### Aviso C: impactPxs da Hyperliquid

O scanner usa os `impactPxs` da Hyperliquid (preços de impacto, um pouco piores que o melhor preço) para decidir quais livros abrir. Um par da Hyperliquid com spread perto de 0,3% pode ficar sem livro e sem sinal. Na validação: se o Arbimax mostrar um sinal com a Hyperliquid que o scanner não mostra, anote o par, o horário e o spread. Não mude nada; o ajuste fica para depois da validação.

#### Aviso D: Lighter, conferir a mensagem real com o modelo do teste

O formato da Lighter foi montado pela documentação, sem uma mensagem real (`tests\dados\lighter_livro.json` e `lighter_market_stats.json`).

1. No PC, no PowerShell na pasta do projeto, cole este bloco inteiro de uma vez, até a linha `'@ | ...` (se o Windows avisar que o texto tem várias linhas, confirme). Ele conecta na Lighter só para leitura, mostra 3 mensagens e sai:

```powershell
@'
import asyncio, json
from scanner.config.carregar import carregar_config
from scanner.coletores.lighter import criar_cliente_lighter

async def principal():
    cliente = criar_cliente_lighter("lighter", carregar_config("config.yaml"))
    conexao = await cliente.conectar_ws()
    await conexao.enviar({"type": "subscribe", "channel": "market_stats/all"})
    await conexao.enviar({"type": "subscribe", "channel": "order_book/0"})
    faltam = {"MARKET_STATS", "SUBSCRIBED/ORDER_BOOK", "UPDATE/ORDER_BOOK"}
    try:
        async with asyncio.timeout(60):
            while faltam:
                msg = await conexao.receber()
                if not isinstance(msg, dict):
                    continue
                tipo = str(msg.get("type", "")).upper()
                if "market_stats" in msg and "MARKET_STATS" in faltam:
                    faltam.discard("MARKET_STATS")
                    print("MARKET_STATS:", json.dumps(msg)[:900], "\n")
                elif tipo in faltam:
                    faltam.discard(tipo)
                    livro = msg.get("order_book") if isinstance(msg.get("order_book"), dict) else {}
                    resumo = {"timestamp": msg.get("timestamp"), "nonce": livro.get("nonce"),
                              "begin_nonce": livro.get("begin_nonce"),
                              "asks": livro.get("asks", [])[:2], "bids": livro.get("bids", [])[:2]}
                    print(tipo + ":", json.dumps(resumo), "\n")
    except TimeoutError:
        print("NAO CHEGOU em 60 s:", ", ".join(sorted(faltam)))
    finally:
        await conexao.fechar()
        await cliente.close()

asyncio.run(principal())
'@ | .venv\Scripts\python -
```

2. Esperado (os números mudam):

   ```text
   MARKET_STATS: {"channel": "market_stats:all", "market_stats": {"0": {"symbol": "ETH", "market_id": 0, ... "best_bid_price": "2064.50", "best_ask_price": "2064.54", ...

   SUBSCRIBED/ORDER_BOOK: {"timestamp": 1774884082326, "nonce": 9182390020, "begin_nonce": 9182389998, "asks": [{"price": "2064.54", "size": "0.3285"}, ...], "bids": [{"price": "2064.50", "size": "0.8100"}, ...]}

   UPDATE/ORDER_BOOK: {"timestamp": 1774884082400, "nonce": 9182390031, "begin_nonce": 9182390020, "asks": [...], "bids": [...]}
   ```

3. Confira:
   - apareceram as 3 linhas (nenhuma `NAO CHEGOU`);
   - `MARKET_STATS` tem `best_bid_price` e `best_ask_price`;
   - nas duas linhas do livro, `nonce` e `begin_nonce` são números (não `null`) e o `begin_nonce` do `UPDATE` é igual ao `nonce` do `SUBSCRIBED`;
   - `asks` e `bids` no formato `{"price": "...", "size": "..."}`;
   - `timestamp` com 13 dígitos.

   Qualquer diferença: copie a saída inteira e mande para o desenvolvedor.
4. Com o scanner rodando, estas linhas **não** podem aparecer:

   ```powershell
   Select-String -Path "logs\scanner-*.log" -Pattern "passa a vir dos livros|begin_nonce/nonce|sem o livro completo|livro completo nao chegou|a Lighter respondeu com erro|nenhuma mensagem em"
   ```

   E `sequencia do livro quebrada` ou `a conexao da lighter caiu` não podem ficar se repetindo o tempo todo. Se aparecer `passa a vir dos livros`, a Lighter passa a assinar um livro por par: confira que a memória e a CPU das linhas `recursos:` ficam estáveis e, se pesar, preencha `limites_assinatura: lighter:` (como no aviso A).
5. Confira no app da Lighter:
   - a taxa da sua conta: Standard é 0 e não precisa de nada; Premium: preencha `taxas` com `lighter: futuro: 0.028` ([parte 6](#6-mudar-a-configuração), ajuste 1);
   - o funding de um par: o `FR` da ponta Lighter no painel é o funding por hora, em %, com `(1h)`; tem que bater com o app.

#### Aviso E: Kraken futuros e Lighter Robinhood desligadas

`krakenfutures` e `lighter_rh` estão comentadas no `config.yaml` (especificação, seção 11). Antes de ligar, confirme:

- `krakenfutures`: os futuros da Kraken Pro estão liberados para a sua conta;
- `lighter_rh`: a Lighter Robinhood aceita contas do Brasil.

Para ligar, veja a [parte 6](#6-mudar-a-configuração), ajuste 4. Depois, rode o marco 1 com elas:

```powershell
.venv\Scripts\python -m scanner.ferramentas.imprimir_precos krakenfutures lighter_rh
```

Esperado: krakenfutures com `BTC/USD:USD` (preço em USD) e lighter_rh com `BTC/USDG:USDG`. Na Robinhood, o log pode avisar `sem cotacao de USDG em USDT`: os sinais dela saem com a etiqueta `COTAÇÃO 1:1`.

#### Aviso F: memória no Windows

A leitura de memória no Windows nunca rodou num Windows real. Uns 5 minutos depois de ligar:

```powershell
Select-String -Path "logs\scanner-*.log" -Pattern "recursos:"
```

Tem que aparecer `recursos: memoria NNN.N MB (atual), ...`. Se aparecer `memoria indisponivel`, avise (o scanner continua funcionando).

---

## 5. Ler o painel

### 5.1 Topo

- **Lucro / Farm**: o modo do scanner.
  - Lucro: sinais com RP líquido acima do mínimo.
  - Farm: sinais com pelo menos uma ponta numa corretora de farm, do menor para o maior custo.
- **Modo comparação**: aparece a faixa amarela `MODO COMPARAÇÃO`. O destaque do Resultado passa a ser o RP bruto (sem taxas, slippage e funding), como o Arbimax calcula, e o mínimo do modo Lucro passa a valer para o RP bruto. Use para validar.
- **Filtros**: mostra ou esconde o bloco de filtros.

### 5.2 Filtros e grupos

- Toda mudança vale na hora (não tem botão salvar) e fica salva no grupo selecionado.
- Campo vazio = filtro desligado. Percentuais em % (`0,4` = 0,4%). Vírgula ou ponto como decimal. Nos campos em $, `50.000` = 50 mil.
- Listas de moedas: `BTC, ETH`.
- `Carregando filtros…`: os campos ficam travados até o scanner responder.
- **Grupo de filtros**: a lista escolhe o grupo. Botões: lápis = renomear; X = apagar (pede confirmação); + = criar um grupo com os filtros atuais; dois quadrados = duplicar. O nome não pode ter `/`.

| Bloco | Campo | O que faz |
| --- | --- | --- |
| Corretoras | Corretoras 1 (ponta long) | só sinais com a ponta long nessas corretoras (`Gate spot` = só o spot; `Gate (todos)` = spot e futuro). A lista mostra só os mercados ligados de cada corretora. Marcado com `(desligado)` (ex.: `kraken:futuro (desligado)`, de um grupo antigo) não pega nenhum sinal: desmarque. Vazio = todas |
| Corretoras | Corretoras 2 (ponta short) | o mesmo para a ponta short |
| Corretoras | Tipos de oportunidade | Spot — Futuros e/ou Futuros — Futuros |
| Geral | Período das métricas (h) | janela do Histórico e das métricas. Vazio = o do `config.yaml` (4 h). Mudar zera as janelas |
| Geral | Volume mín. 24h ($) | volume mínimo nas duas pontas |
| Geral | Spread mín. (A atual) (%) | spread de abertura mínimo agora |
| Geral | Lote mín. ($) | lote mínimo agora nas duas pontas; nunca fica abaixo do tamanho de referência ($100) |
| Geral | Lote médio mín. ($) | lote médio do período, nas duas pontas |
| Geral | Limite de ordem mín. ($) | tamanho máximo de ordem da corretora; na v1 ele é desconhecido (coluna MAX vazia) e passa |
| Geral | Invertidas mín. | quantas vezes o F ficou ≥ 0 no período |
| Geral | Invertidas: duração mín. (s) | quanto tempo o F precisa ficar ≥ 0 para contar uma invertida |
| Geral | Funding mín. / Funding máx. (%) | funding de cada ponta futura |
| Geral | FR Yield mín. (%) | funding líquido (recebido na short menos pago na long), normalizado para 8h |
| Geral | Intervalo de funding | só pontas futuras que cobram em 1h, 4h e/ou 8h |
| Geral | Funding sincronizado | as duas pontas futuras com o mesmo intervalo de cobrança |
| Geral | Maior spread de abertura / de fechamento mín., Média de spread de abertura / de fechamento mín. (%) | métricas do período |
| Geral | Métrica de spread | qual F entra no RP bruto: médio, maior ou média dos 10 maiores. Vazio = o do `config.yaml` |
| Geral | Resultado projetado mín. (Lucro) (%) | RP mínimo do modo Lucro. Vazio = o do `config.yaml` (0,2%) |
| Geral | Custo máx. (Farm) (%) | menor RP aceito no modo Farm (negativo). Vazio = o do `config.yaml` (-0,1%) |
| Organização | Sempre mostrar | moedas que aparecem mesmo fora dos filtros, das listas e do modo (nunca com lote abaixo do mínimo nem suspeitas) |
| Organização | Nunca mostrar / Apenas mostrar | moedas escondidas / só estas moedas |
| Organização | Ordenar por | Resultado (padrão: RP líquido; Farm: menor custo; comparação: RP bruto), Horário, Spread ou Volume (o menor das duas pontas) |
| Organização | Mostrar suspeitos | mostra os sinais marcados `SUSPEITO` |
| Organização | Fixados no topo | quantos sinais estão fixados; `Desafixar todos` |
| Lista negra | Moedas excluídas | moedas que nunca aparecem |

### 5.3 Tabela de sinais

Verde = positivo; vermelho = negativo. Linha com faixa amarela à esquerda = fixada.

- **Moeda**:
  - a moeda e a cotação (ex.: `AKAM USDT`);
  - etiquetas: `SUSPEITO` (spread de abertura acima de 30%: pode ser outro token com o mesmo nome), `COTAÇÃO 1:1` (sem cotação USDC/USDG/USD, preço convertido 1:1), `STOCK` (ação tokenizada; vazia na v1);
  - o tipo: `SPOT — FUTUROS` ou `FUTUROS — FUTUROS`;
  - `desde HH:MM:SS`: quando o sinal apareceu (Brasília; passe o mouse para ver o dia).
- **Ponta long / Ponta short**:
  - linha 1: corretora, mercado (`spot` ou `futuros`), preço (já em USDT), `(Long)` ou `(Short)`, lote agora em $ e, entre parênteses, o lote médio do período. Passe o mouse para ver o símbolo na corretora e a taxa taker usada;
  - linha 2: `VOL` volume 24h; `FR` funding com o intervalo e 4 casas, ex.: `+0,0100% (8h)` (`—` no spot); `MAX` limite de ordem (`—` na v1); `VAR` variação 24h;
  - nomes: `Kraken Pro spot` = a kraken; `Kraken Futuros` = a krakenfutures; `Lighter Robinhood` = a lighter_rh.
- **Resultado**:
  - `A` spread de abertura agora; `F` spread de fechamento agora;
  - `FR Yield`: funding líquido (recebido na short menos pago na long), normalizado para 8h, com 4 casas;
  - destaque `RP líq.` (RP bruto − taxas − slippage extra + funding esperado), com o `bruto` ao lado. No modo comparação o destaque é o `RP bruto`, com o `líq.` ao lado. Passe o mouse para ver as taxas, a margem de slippage e o funding esperado;
  - modo Farm: `Custo a cada $1.000` de volume (vermelho = você gasta; verde = você ganha).
- **Histórico (4h)**:
  - `N invertidas` no período;
  - `A` e `F` médios do período;
  - botão de gráfico: abre o gráfico do par.
- **Fixar** (alfinete, última coluna): leva o sinal para o topo; fica salvo no grupo.

### 5.4 Barra de status (rodapé)

- `Última atualização`: horário de Brasília, com milissegundos, da última mensagem que o painel recebeu do scanner. Sem sinais, o motor manda um sinal de vida a cada 5 s (só depois de um ciclo que deu certo), então o horário continua andando. `Ao vivo` com o horário parado há mais de 10 s: veja o log. Com `erro no ciclo do motor`, o motor está falhando: mande o log ao desenvolvedor. Sem erro no log, aperte F5.
- `N oportunidades` (ou `X de N oportunidades` quando a busca esconde alguns).
- Conexão do painel com o scanner: `Ao vivo`; `Sem conexão: tentando de novo em Ns` (a tabela fica apagada). Não é a internet das corretoras: é o scanner desligado ou o túnel caído.
- `PAUSADO`: a tabela está parada (botão **Pausar**).
- Uma bolinha por corretora: verde = conectada; laranja = reconectando; vermelha = atrasada (sem mensagem há mais de 2 s). Passe o mouse para ver o atraso médio em ms e as reconexões. `Kraken Pro spot` e `Kraken Futuros` são bolinhas separadas.
- **Buscar moeda**: filtra só a tela, pela moeda (`BTC` acha `BTC/USDT`).
- **Pausar / Continuar**: congela a tabela; os dados continuam chegando por trás.

### 5.5 Gráfico do histórico

- Título: o par e o tipo. Subtítulo: as duas pontas.
- **Abertura / Fechamento**: qual spread o gráfico usa.
- `Período: 4h`: o período dos filtros.
- Resumo no topo: `Maior spread`, `Média dos 10 maiores`, `Spread médio` e o lote médio de cada ponta.
- Duas linhas: preço da ponta long (azul) e da short (vermelha).
- Mouse em cima: horário, preço e lote de cada ponta, spread naquele ponto e `Spread médio HH:MM – HH:MM` (daquele ponto até agora).
- `Esc` ou o X fecha.
- `Não deu para carregar a biblioteca do gráfico`: o gráfico vem da internet (unpkg.com); o resto do painel funciona. O navegador só aceita o arquivo exato da versão 4.2.3 (conferido pelo hash); se o arquivo do unpkg mudar, o gráfico fica desligado (veja a parte 8).

### 5.6 Limitações conhecidas (o que você vê e é assim mesmo)

- As linhas de preço do gráfico são as de abertura (ask do long e bid do short) também na intenção Fechamento; o que muda entre as duas é o spread.
- O resumo do dia no log (`resumo do dia ... UTC`) é provisório para os sinais que ainda estavam abertos na virada (`N ainda aberto(s)`): eles entram na quantidade e no RP médio, mas não nas durações nem nos `%`. A `/api/validacao` do mesmo dia, aberta depois, já conta o que encerrou.
- O "dia" da validação é o dia UTC de quando o sinal apareceu: vira às 21:00 de Brasília. Sinal das 22:00 de Brasília conta no dia seguinte.
- Várias abas com o mesmo grupo de filtros: a aba aberta há mais tempo, ao mudar um filtro, grava o formulário velho dela no grupo inteiro. Use o painel numa aba só. O último navegador aberto define os filtros do scanner.
- As colunas `MAX` e a etiqueta `STOCK` ficam vazias na v1.
- `Funding sincronizado` só confere se as pontas têm o mesmo intervalo de cobrança (não o horário da próxima cobrança).

---

## 6. Mudar a configuração

Cada campo está explicado no próprio `config.yaml` (o que faz, unidade, padrão e quando mexer).

1. Abra o arquivo:
   - Windows (PowerShell): `notepad config.yaml`
   - VPS (terminal): `nano config.yaml` (salvar: `Ctrl+O`, `Enter`; sair: `Ctrl+X`)
2. Mude o valor depois do `:`. Regras: um espaço depois do `:`; recuo de 2 espaços (nunca Tab); percentuais em % (`0.3` = 0,3%) com ponto decimal; sem acento.
3. Confira (tem que aparecer `config ok`):
   - Windows: `.venv\Scripts\python -m scanner --validar`
   - VPS: `.venv/bin/python -m scanner --validar`
4. Religue: Windows, `Ctrl+C` e ligue de novo; VPS, `sudo systemctl restart scanner`.

### Os 5 ajustes mais comuns

**1. Taxas da sua conta (nível VIP).** Troque a linha `taxas: {}` por (exemplo; use as suas taxas taker, em %):

```yaml
taxas:
  mexc:
    futuro: 0.02
  gate:
    spot: 0.1
    futuro: 0.05
  lighter:
    futuro: 0.028
```

**2. RP mínimo do modo Lucro.** Para ver também sinais menores (0,05% líquido):

```yaml
rp_min_lucro_pct: 0.05
```

**3. Corretoras de farm.** Só as corretoras onde você faz farm:

```yaml
corretoras_farm: [lighter, kraken]
```

**4. Ligar uma corretora (ex.: krakenfutures).** Apague o `# ` do começo das 3 linhas dela. Antes:

```yaml
  # - krakenfutures
  # krakenfutures: [futuro]
  # krakenfutures: 1
```

Depois:

```yaml
  - krakenfutures
  krakenfutures: [futuro]
  krakenfutures: 1
```

A primeira está em `corretoras`, a segunda em `mercados_por_corretora` e a terceira em `intervalo_funding_h`. Para a `lighter_rh`: `# - lighter_rh` em `corretoras` e `# lighter_rh: 1` em `intervalo_funding_h`. Uma corretora nova que não está no arquivo: acrescente `  - nome` em `corretoras`.

**5. Usuário e senha do painel** (na VPS):

```yaml
painel:
  host: 127.0.0.1
  porta: 8000
  usuario: dono
  senha: "troque-por-uma-senha-forte"
```

Sem senha: `usuario: null` e `senha: null`, só com `host: 127.0.0.1` (ou `localhost`). Usuário sem senha (ou o contrário), ou `host: 0.0.0.0` sem usuário e senha, impede o scanner de ligar.

### Todos os campos

O padrão vale quando a linha não está no `config.yaml`.

| Campo | Padrão | Unidade | O que é |
| --- | --- | --- | --- |
| `corretoras` | `[mexc, gate, bitget]` | lista | corretoras ligadas |
| `corretoras_farm` | `[]` | lista | corretoras de farm (modo Farm) |
| `tamanho_referencia_usd` | `100` | USDT | tamanho da ordem do cálculo e lote mínimo obrigatório |
| `max_silencio_conexao_s` | `2` | s | sem mensagem há mais que isto = corretora atrasada |
| `max_idade_livro_s` | `30` | s | idade máxima de livro ou ticker parado |
| `spread_min_livro_pct` | `0.3` | % | spread de abertura mínimo para abrir o livro do par |
| `manter_livro_s` | `60` | s | livro continua aberto depois que o spread cai |
| `periodo_metricas_h` | `4` | h | janela das métricas do período |
| `rp_min_lucro_pct` | `0.2` | % | RP líquido mínimo do modo Lucro |
| `custo_max_farm_pct` | `-0.1` | % | menor RP líquido aceito no modo Farm |
| `limite_suspeito_pct` | `30` | % | spread de abertura acima disto = suspeito |
| `anti_repeticao_s` | `60` | s | sinal que volta antes disto é o mesmo registro |
| `ciclo_ms` | `500` | ms | intervalo do cálculo |
| `amostra_s` | `5` | s | intervalo das amostras gravadas |
| `retencao_dias` | `7` | dias | idade máxima no banco |
| `tempo_operacao_h` | `8` | h | duração estimada da operação (funding esperado) |
| `margem_slippage_pct` | `0.0` | % | slippage extra descontado |
| `metrica_spread` | `medio` | — | F do RP bruto: medio, maior ou media_10_maiores |
| `taxas` | `{}` | % | taxas taker da sua conta |
| `intervalo_dados_lentos_s` | `60` | s | intervalo do funding e das taxas (REST) |
| `recarregar_mercados_s` | `3600` | s | intervalo da recarga da lista de mercados |
| `timeout_desassinar_s` | `5` | s | espera pela confirmação ao cancelar um livro |
| `espera_max_reconexao_s` | `60` | s | maior espera entre tentativas de reconexão |
| `limites_assinatura` | `{}` | assinaturas | máximo de assinaturas por corretora |
| `atraso_max_valido_s` | `30` | s | atraso acima disto = relógio errado |
| `pasta_logs` | `logs` | — | pasta dos logs |
| `dias_logs` | `30` | dias | logs antigos guardados |
| `intervalo_log_recursos_s` | `300` | s | intervalo da linha `recursos:` e das métricas das corretoras |
| `mercados_por_corretora` | `{}` | — | spot e/ou futuro de cada corretora |
| `metodo_ticker` | `{}` | — | método de ticker que cada corretora tenta primeiro |
| `mensagens_sem_bid_ask` | `10` | mensagens | tickers sem bid/ask seguidos para trocar de método |
| `fonte_cotacao` | `bitget` | — | corretora da cotação USDC/USDT etc. |
| `fontes_cotacao` | `{}` | — | fonte diferente por moeda |
| `faixa_cotacao_pct` | `10` | % | cotação mais longe de 1 que isto é ignorada |
| `intervalo_funding_h` | `{}` | h | intervalo do funding por corretora |
| `url_rest_lighter` | `https://mainnet.zklighter.elliot.ai` | — | API REST da Lighter |
| `url_ws_lighter` | `wss://mainnet.zklighter.elliot.ai/stream` | — | WebSocket da Lighter |
| `url_rest_lighter_rh` | `https://api.rh.lighter.xyz` | — | API REST da Lighter Robinhood |
| `url_ws_lighter_rh` | `wss://api.rh.lighter.xyz/stream` | — | WebSocket da Lighter Robinhood |
| `intervalo_ping_lighter_s` | `30` | s | intervalo do ping à Lighter (menos de 120) |
| `pings_sem_resposta_lighter` | `2` | pings | sem resposta = reconecta |
| `tempo_max_rede_lighter_s` | `10` | s | tempo máximo de uma chamada à Lighter |
| `niveis_livro_lighter` | `100` | níveis | níveis de cada lado do livro da Lighter |
| `tempo_max_livro_completo_lighter_s` | `10` | s | livro completo que não chega é pedido de novo |
| `mudancas_sem_livro_completo_lighter` | `10` | mensagens | mudanças sem livro completo para avisar no log |
| `tempo_min_modo_etapa1_lighter_s` | `300` | s | tempo mínimo em cada modo da etapa 1 da Lighter |
| `painel.host` | `127.0.0.1` | — | endereço do painel (0.0.0.0 = internet, só com usuário e senha) |
| `painel.porta` | `8000` | — | porta do painel |
| `painel.usuario` | `null` | — | usuário do painel (null = sem senha) |
| `painel.senha` | `null` | — | senha do painel |
| `banco` | `scanner.db` | — | arquivo do banco |
| `nivel_log` | `normal` | — | normal ou detalhado |
| `limites_assinatura_mercado` | `{}` | assinaturas | máximo por corretora e mercado (tickers e livros juntos) |
| `livros_reservados_limite_mercado` | `10` | assinaturas | parte de cada limite por mercado que fica para os livros |
| `reassinar_livros_corretoras` | `[mexc, xt]` | lista | corretoras cujos livros são assinados de novo de tempos em tempos |
| `intervalo_reassinar_livros_s` | `900` | s | intervalo entre duas assinaturas do mesmo livro nessas corretoras |
| `prazo_primeiro_ticker_s` | `60` | s | par pedido sem nenhum ticker neste tempo vai para um aviso no log |
| `intervalo_montar_livro_lighter_ms` | `100` | ms | intervalo mínimo entre duas montagens do livro de um par da Lighter |
| `prazo_primeiro_livro_s` | `60` | s | livro pedido sem nada neste tempo vai para um aviso no log |

---

## 7. Atualizar para uma versão nova

**Windows (PowerShell):**

1. Pare o scanner (`Ctrl+C`).
2. Guarde o seu `config.yaml`:

   ```powershell
   Copy-Item C:\scanner-arbitragem\config.yaml "$HOME\config-antigo.yaml"
   ```

3. Copie os arquivos da versão nova por cima de `C:\scanner-arbitragem` (aceite substituir). Não copie uma pasta `.venv` que venha junto. Se a pasta veio do GitHub, no lugar disso rode `git pull` na pasta.
4. Veja o que mudou entre o seu `config.yaml` e o novo:

   ```powershell
   cd C:\scanner-arbitragem
   Compare-Object (Get-Content "$HOME\config-antigo.yaml") (Get-Content config.yaml)
   ```

   `<=` = linhas só no seu; `=>` = linhas só no novo. Se você tinha mudado valores, mude de novo no `config.yaml` novo.
5. Reinstale as bibliotecas, rode os testes e confira a configuração:

   ```powershell
   .venv\Scripts\python -m pip install -r requirements-dev.txt
   .venv\Scripts\python -m pytest -q
   .venv\Scripts\python -m scanner --validar
   ```

   Testes sem `failed` e `config ok`.
6. Ligue de novo ([parte 2](#2-rodar-no-windows)).

**VPS:**

1. No PowerShell do PC, com a versão nova já em `C:\scanner-arbitragem`, empacote e mande (igual à [parte 3.1](#31-instalar), passo 1):

   ```powershell
   cd C:\scanner-arbitragem
   tar --exclude=.venv --exclude=logs --exclude=scanner.db* -czf "$HOME\scanner-arbitragem.tgz" .
   scp "$HOME\scanner-arbitragem.tgz" usuario@IP:~/
   ```

2. No terminal da VPS (o `config.yaml` da VPS, com a senha, é guardado e volta no lugar):

   ```bash
   sudo systemctl stop scanner
   cp ~/scanner-arbitragem/config.yaml ~/config-vps.yaml
   tar -xzf ~/scanner-arbitragem.tgz -C ~/scanner-arbitragem
   cp ~/config-vps.yaml ~/scanner-arbitragem/config.yaml
   cd ~/scanner-arbitragem
   .venv/bin/python -m pip install -r requirements-dev.txt
   .venv/bin/python -m pytest -q
   .venv/bin/python -m scanner --validar
   sudo systemctl start scanner
   ```

   Testes sem `failed` e `config ok` antes do `start`.

---

## 8. Quando algo dá erro

Para qualquer erro fora desta tabela: copie a mensagem inteira (e as linhas em volta, no log) e mande para o desenvolvedor.

| Mensagem | O que fazer |
| --- | --- |
| `O termo 'python' não é reconhecido...`, uma mensagem falando da Microsoft Store, ou `python --version` mostra outra versão | O Python 3.12 não está no PATH. Crie o `.venv` com `py -3.12 -m venv .venv` no lugar de `python -m venv .venv` e siga a [parte 1](#1-instalar-no-windows) a partir daí. Se `py` também não existir, reinstale o Python marcando **Add python.exe to PATH**. |
| `o painel parou (RuntimeError: nao consegui abrir o painel em 127.0.0.1:8000 (a porta ja esta em uso?)); o scanner segue sem painel ate reiniciar` | Já tem outro scanner aberto (outra janela do PowerShell ou, na VPS, o serviço). Feche o outro (`Ctrl+C`; na VPS `sudo systemctl stop scanner`) e ligue de novo. Para ver quem usa a porta: Windows `Get-NetTCPConnection -LocalPort 8000 -State Listen` (coluna `OwningProcess`, depois `Stop-Process -Id NUMERO`); VPS `sudo ss -ltnp 'sport = :8000'`. Ou troque `painel.porta` para `8001` e abra `http://localhost:8001`. |
| `Erro na configuracao: ...` | A mensagem diz o campo. Exemplos: `rp_min_lucro: campo desconhecido (erro de digitacao?)` (nome errado); `rp_min_lucro_pct: precisa ser um numero`; `o arquivo 'config.yaml' nao e um yaml valido: ... line 2, column 3` (recuo ou `[` `]` errado na linha indicada); `painel.senha: o painel tem usuario mas nao tem senha`; `limites_assinatura: mexcc nao esta em corretoras`. Corrija e rode `--validar` até dar `config ok`. |
| `UnicodeDecodeError: 'utf-8' codec can't decode byte ...` | O `config.yaml` foi salvo em outra codificação. No Bloco de Notas: Arquivo > Salvar como > Codificação: **UTF-8** > Salvar. Ou tire os acentos que você escreveu. |
| `Erro: ValueError: corretora 'mexcc' nao existe no ccxt (nome com erro de digitacao?)` | Nome errado em `corretoras`. Os nomes aceitos: `mexc`, `gate`, `bitget`, `bingx`, `xt`, `hyperliquid`, `kraken`, `krakenfutures`, `lighter`, `lighter_rh`. |
| `mexc: nao consegui carregar a lista de mercados (...); a corretora fica de fora por enquanto e eu tento de novo sozinho em instantes` | A corretora não respondeu ao ligar. As outras seguem e ela tenta de novo sozinha (`ainda nao consegui carregar a lista de mercados ... tentativa N falhou`). Confira se o site dela abre. Se ficar horas assim, avise. |
| `Erro: RuntimeError: nenhuma corretora carregou a lista de mercados; confira a internet e o config.yaml` | Sem internet ao ligar. Confira a internet e ligue de novo. Na VPS o systemd tenta de novo sozinho a cada 10 s. |
| `o pacote 'websockets' nao esta instalado: o painel ao vivo (/ws/sinais) nao vai funcionar. Instale com: pip install websockets` (o painel fica em `Sem conexão: tentando de novo em Ns`) | Rode `.venv\Scripts\python -m pip install -r requirements.txt` (VPS: `.venv/bin/python -m pip install -r requirements.txt`) e religue. |
| `Could not request local forwarding.` ao abrir o túnel SSH | A porta 8000 do PC está ocupada (o scanner do PC ligado). Use `ssh -L 8001:127.0.0.1:8000 usuario@IP` e abra <http://localhost:8001>. |
| O navegador pede usuário e senha e não aceita | Use exatamente o `painel.usuario` e o `painel.senha` do `config.yaml` da VPS (maiúsculas contam). |
| `Erro na configuracao: painel.host 0.0.0.0 abre o painel para fora desta maquina, e ele esta sem senha: ...` | Escolha um: preencha `painel.usuario` e `painel.senha` (parte 3.2); ou volte para `host: 127.0.0.1` e abra o painel pelo túnel SSH (parte 3.5, opção A). Rode `--validar` até dar `config ok`. |
| `Invalid host header` no navegador | O painel sem senha só abre por <http://localhost:8000> ou <http://127.0.0.1:8000> (proteção contra sites que se passam pelo seu PC). Abra por um desses endereços. |
| `pedido recusado: veio de uma pagina de outro site` na caixa vermelha dos filtros | O painel só aceita mudanças vindas dele mesmo. Abra o painel direto pelo endereço (<http://localhost:8000>, ou `http://IP:8000` na opção B) e aperte F5. |
| Gráfico com `Não deu para carregar a biblioteca do gráfico` e a internet funcionando | Aperte F12 > aba **Console**. Se aparecer `Failed to find a valid digest in the 'integrity' attribute`, o arquivo do gráfico no unpkg.com mudou e foi bloqueado por segurança: o resto do painel funciona; mande a mensagem ao desenvolvedor. |
| Testes com `failed` ou `error` | Não ligue o scanner. Copie a saída inteira do `pytest` e mande. |
