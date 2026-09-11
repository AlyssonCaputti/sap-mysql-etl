# sap-mysql-etl

ETL que sincroniza vendas do SAP para o MySQL que alimenta os dashboards e o
Forecast. Roda em produção: de 5 em 5 minutos, de hora em hora e diariamente.

A origem é um export de SAP que muda de formato sem avisar. Em dois meses ela
trocou o cabeçalho técnico por português, removeu uma coluna, alternou entre
UTF-8 e UTF-16 e mudou o separador de `;` para vírgula. Esse repositório é o
pipeline que aprendeu a sobreviver a isso.

Python · pandas · MySQL · pytest · 159 testes sem banco e sem rede

## As três falhas que motivaram a refatoração

As três estavam em produção e nenhuma delas quebrava o pipeline. É por isso que
demoraram a aparecer.

### 1. Duplicidade que nenhuma carga futura conseguia limpar

A estratégia `date_range` apaga a janela de datas do arquivo e reinsere. O
código antigo avisava sobre data ilegível e **inseria a linha assim mesmo**.

O `DELETE` filtra por `STR_TO_DATE`, que devolve `NULL` justamente nessas
linhas, e `NULL BETWEEN x AND y` nunca é verdadeiro. Então a linha entrava e
nenhuma carga seguinte conseguia removê-la. Cada rodada somava outra cópia.

```python
# src/load/strategies.py
if datas.isna().any():
    exemplos = df.loc[datas.isna(), coluna_data].astype(str).unique()[:10]
    raise ValueError(
        f"date_range: {int(datas.isna().sum())} de {len(df)} linha(s) com "
        f"'{coluna_data}' ilegível. Abortei — essas linhas entrariam sem "
        f"data válida e ficariam presas na tabela pra sempre. "
        f"Exemplos: {exemplos.tolist()}"
    )
```

Agora aborta com a lista dos valores problemáticos. Manter o dado de ontem é
melhor que somar cópia todo dia.

### 2. Backup de arquivo que nunca entrou no banco

`upload_file` devolvia `0` sem levantar exceção quando a leitura falhava, e o
`upload.py` movia o arquivo para `_backup` mesmo assim. O dado do dia sumia da
pasta de entrada sem ter entrado no banco.

Aconteceu em 05/08/2026, com `File is not a zip file`. Agora o backup só ocorre
depois do commit; o que falha fica na entrada para nova tentativa.

### 3. Exit code 0 em carga que falhou

O código antigo logava o erro por arquivo e saía com `0`. O `rodar_etl.ps1` só
checa `$LASTEXITCODE`, então a Tarefa Agendada registrava **sucesso** enquanto
arquivos falhavam.

Foi o que escondeu a quebra de 01 a 03/08/2026. Três dias de base parada, com
todos os indicadores verdes.

## Qualidade de dado em três pontos

Cada carga loga em três momentos, para responder "o dado está bom?" sem abrir o
banco (`src/quality/checkpoints.py`):

| Ponto | O que reporta |
|---|---|
| **porta 1** — recepção | linhas × colunas lidas, linhas vazias, avisos, queda brusca de volume |
| **porta 2** — transformação | linhas perdidas no tratamento, chave duplicada ou vazia, janela de datas, datas ilegíveis e futuras |
| **saída** — carga | contagem por mês na origem × banco, separando o que caiu dentro e fora da janela |

O checkpoint de saída é o que enxerga a nota retroativa: quando uma linha existe
na origem mas está fora da janela de 2 meses, ele avisa e sugere `--tudo`.
Medido em 18/08/2026: 48 linhas nessa situação (2024-12, 2025-01 e 2026-06).

Conversão de tipo também confessa o que perdeu, em vez de deixar o `NaN` passar
calado:

```python
# src/quality/contracts.py
perdidas = int((convertido.isna() & antes.notna()).sum())
if perdidas:
    avisos.append(
        f"{coluna}: {perdidas} de {preenchidas} valor(es) não "
        f"converteram pra {tipo} e ficaram NULL. Ex.: {exemplos}"
    )
```

## Contrato de schema

Layout da origem mudando é o que mais quebra esse pipeline, então a validação
virou módulo em vez de um `if` no meio do script
(`src/quality/contracts.py`).

Coluna obrigatória faltando aborta a carga. Opcional faltando segue com aviso.
O casamento de nome aceita grafia divergente, porque a origem já alternou entre
`CredPresumido`, `credpresumido` e `red_base_pis_confins` (com typo por
"cofins") para a mesma coluna.

Arquivo vazio também aborta: melhor ficar com o dado de ontem do que zerar uma
tabela que estava boa.

## Carga incremental do faturamento

A origem publica o CSV inteiro toda hora (100 MB, 256 mil linhas), mas o que
muda é quase só o mês corrente, 1,3% do total.

O pipeline converte para Parquet particionado por mês e carrega só os **2
últimos meses**. Dois, e não um, por causa de nota retroativa: se a origem
lançar hoje uma nota com emissão do mês passado, uma janela de 1 mês não a
pegaria.

| | antes | agora |
|---|---|---|
| leitura | 11s (CSV 100 MB) | 0,1s (Parquet 16 MB) |
| linhas carregadas | 256.352 | 11.315 |
| INSERT | ~45s | ~3s |
| ciclo completo | ~140s | **~76s** |

O `faturamento_full` continua sendo refeito inteiro (46s do ciclo). Não dá para
fazer só a janela: a ilha do cliente vem da última compra da marca foco em todo
o histórico, então quem comprou há meses apareceria como `outros`.

Para recarregar tudo depois de corrigir dado antigo:
`python -m pipelines.faturamento_horario --tudo`.

## Quatro estratégias de carga

A estratégia de cada tabela está declarada em `config/tables.py`, não espalhada
no código (`src/load/strategies.py`):

| Estratégia | O que faz | Quando uso |
|---|---|---|
| `replace` | monta em tabela temporária e troca no fim | recarga total sem janela de indisponibilidade |
| `truncate` | esvazia e reinsere, mantendo o schema | tabela pequena com índice que quero preservar |
| `date_range` | apaga só os meses do arquivo e reinsere | carga periódica com janela de datas |
| `upsert` | `ON DUPLICATE KEY UPDATE` na chave natural | origem que corrige registro antigo |

O `replace` monta em `tabela__nova` e só faz `RENAME` no fim. Antes ele dava
`DROP` primeiro, e um erro depois disso deixava o banco sem a tabela.

O `date_range` apaga mês a mês, não o intervalo inteiro: com `BETWEEN`, um mês
sem linha no arquivo mas dentro do intervalo era apagado e nunca reposto.

## As 15 cicatrizes

Todo trecho defensivo do pipeline antigo migrou intacto, porque cada um previne
uma falha real e datada. Os testes marcados `CICATRIZ` reproduzem o incidente:

- caracteres de controle ilegais no XML do SAP quebrando o openpyxl
- header técnico virando português (clientes 01/07, itens 23/07)
- coluna opcional sumindo da origem sem aviso (23/07)
- vendedor duplicado multiplicando linhas de cliente no LEFT JOIN
- decimais BR sem aspas (`399,89`) quebrando o split por vírgula
- exportação alternando entre UTF-8 e UTF-16
- typo na origem (`confins` por `cofins`)
- origem alternando entre `1.399,90` e `759.90`
- separador vírgula lido como `;`, base parada 3 dias (01 a 03/08)
- `NaN` serializado como literal `nan` no INSERT
- `STR_TO_DATE` abortando a query em vez de devolver NULL
- publicar tabela sem custo derrubando a MCB para fallback silencioso
- arquivo vazio substituindo tabela boa
- parcela vazia zerando a MC inteira por propagação de NULL
- limite de ~196 colunas LONGTEXT forçando o fatiamento de itens

Uma décima sexta apareceu durante a refatoração: a pasta de produção
`tabela-preço-promocao` gerava `` `TabelaPreOPromocao` ``, porque o split em
`[^a-zA-Z0-9]` tratava o `ç` como separador. Agora translitera antes de separar.

## Rodando

```powershell
pip install -r requirements.txt
copy .env.example .env          # preencher credenciais

.\rodar_etl.ps1                 # diário: clientes e itens
.\rodar_faturamento_horario.ps1 # horário: faturamento + faturamento_full
.\rodar_sku_custo.ps1           # 5 min: custo por depósito

python -m pipelines.preparar clientes             # uma etapa só
python -m pipelines.faturamento_horario --status  # relata sem escrever
python -m pytest tests/ -v                        # testes
```

### O que roda quando

| Tarefa Agendada | Frequência | O que atualiza |
|---|---|---|
| `rodar_faturamento_horario.ps1` | 1 hora | `Faturamento` + `faturamento_full` |
| `rodar_etl.ps1` | 1 dia | `Clientes`, `Itens`, `ItensExtra*` |
| `rodar_sku_custo.ps1` | 5 min | `SkuCustoCdGiba` |

O faturamento não está no diário de propósito: se as duas cargas rodassem,
disputariam a mesma tabela. O `upload.py` pula a pasta `Faturamento`
(`PASTAS_DE_OUTRO_PIPELINE`).

Os dois pipelines frequentes comparam o hash da origem antes de trabalhar e
saem calados quando nada mudou. E usam lock: se uma carga demorar mais que o
intervalo, a rodada seguinte é pulada em vez de rodar em paralelo.

## Estrutura

```
config/
  settings.py            caminhos e credenciais (antes hardcoded em 6 arquivos)
  tables.py              estratégia de carga + contrato de schema por tabela
src/
  io/readers.py          leitura resiliente (encoding, separador, XML, decimal BR)
  io/database.py         conexão, DDL, insert em lote, SQL parametrizado
  io/execucoes.py        registro de execução
  io/alerta.py           alerta por e-mail em falha
  transform/             clientes, faturamento, itens: funções puras, sem I/O
  load/strategies.py     replace / truncate / date_range / upsert
  quality/contracts.py   contrato de schema e normalização de coluna
  quality/checkpoints.py portas 1 e 2 + conferência de saída
pipelines/
  preparar.py            origens -> CSVs tratados
  upload.py              CSVs -> MySQL
  faturamento_full.py    tabela analítica (JOIN no banco)
  sku_custo.py           carga incremental por hash
tests/                   159 testes, sem banco e sem rede
```

## O que mudou na refatoração

| Antes | Depois |
|---|---|
| 4 implementações divergentes de leitura de CSV | 1 em `src/io/readers.py` |
| caminho de rede hardcoded em 6 arquivos | `config/settings.py` |
| transformação executada no import do módulo | funções puras + `main()` |
| SQL por concatenação de string | parâmetros + validação de identificador |
| `except Exception: pass` | erro com causa e sugestão |
| porta 8080 em 2 arquivos, 3306 em outros 2 | 3306 em todos |
| `.gitignore` com `*.txt` engolindo `requirements.txt` | corrigido |
| 0 testes | 159 testes |

## Verificação

- 159 testes passam, sem banco e sem rede
- **Teste de regressão real**: revertendo a correção 1, dois testes falham;
  restaurando, todos passam. Os testes checam comportamento, não passam por
  acidente
- Os 8 arquivos reais de produção leem corretamente, incluindo o
  `estoquePorDepositoCustoVPS.csv` separado por vírgula, que o código antigo
  transformava numa coluna única
- `Base NFs.csv` real, 255.379 linhas: nenhuma data ilegível hoje, janela
  02/01/2024 a 11/08/2026. A correção 1 não bloquearia a carga atual

Carga conferida contra baseline tirada antes da troca:

| Tabela | Antes | Depois |
|---|---|---|
| Faturamento | 254.390 | 255.439 |
| Clientes | 14.581 | 14.599 |
| Itens | 2.901 | 2.922 |
| faturamento_full | 254.390 | 255.439 |

Estrutura idêntica nas quatro, `mc` batendo com a fórmula em 255.439 linhas sem
nenhum NULL, e a view `ItensCompleto` respondendo.

## Pontos em aberto

**Tipagem do destino.** Colunas sem tipo declarado ainda vão para `LONGTEXT`,
que mata índice e obriga `CAST` em quem consulta. Quem declara `tipos` em
`config/tables.py` sai disso, então dá para migrar tabela por tabela. A migração
em massa não foi feita porque quebraria o Forecast em silêncio.

**`ETL/mcb/`** do projeto antigo não foi portado: são scripts manuais, fora do
fluxo agendado.

**O usuário do banco não tem `SESSION_VARIABLES_ADMIN`.** O código lida com
isso, mas vale saber ao criar tabela nova com muitas colunas.
