# Scanner de arbitragem cripto

Programa que compara o preço do mesmo ativo em corretoras diferentes (spot x futuros e futuros x futuros) e mostra as oportunidades de arbitragem já descontando as taxas. Ele **só lê dados públicos**: não usa chaves de API e não faz nenhuma ordem.

A especificação completa está em `docs/especificacao.md`. Este README ainda é curto; a explicação de cada campo do `config.yaml` e o passo a passo completo chegam na última etapa do projeto.

## Instalação

Precisa do Python 3.12 ou mais novo. Abra o terminal na pasta do projeto.

**Windows (PowerShell):**

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
```

**Linux (ou VPS):**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

O `venv` é uma pasta `.venv` só do projeto, para não misturar as bibliotecas com o resto do computador. Toda vez que abrir um terminal novo, ative de novo (a linha com `activate`).

## Configuração

Tudo o que dá para ajustar fica no arquivo `config.yaml`, na raiz do projeto. Ele já vem com os valores padrão (corretoras MEXC, Gate e Bitget). Se apagar uma linha, vale o valor padrão. Se escrever um valor inválido, o programa avisa qual campo está errado.

## Como rodar

Verificar se a configuração está certa:

```bash
python -m scanner
```

Deve aparecer `config ok`. Para usar outro arquivo de configuração:

```bash
python -m scanner --config caminho/do/arquivo.yaml
```

## Como rodar os testes

```bash
python -m pytest
```

Todos os testes devem passar. Eles não acessam a internet.
