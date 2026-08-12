# sap-mysql-etl

Pipeline ETL em Python que sobrevive a uma origem de dados instável.

Todo dia ele lê exportações de um ERP, trata e carrega num MySQL que alimenta
dashboards e um modelo de forecast. O problema interessante não é o volume
(~255 mil linhas) — é que a origem **muda de formato sem avisar**: encoding,
separador, nome de coluna, formato numérico. Cada proteção aqui nasceu de uma
carga que quebrou em produção.

```
origem (xlsx/csv)  →  preparar  →  csv tratado  →  upload  →  MySQL
                                                                ↓
                                                     tabela analítica (JOIN)
```

## O que este projeto mostra

- **Falha alta, nunca silenciosa** — o pior bug de ETL é o que não aparece no
  log nem no exit code. Três correções aqui são exatamente disso.
- **Idempotência** — quatro estratégias de carga (`replace`, `truncate`,
  `date_range`, `upsert`), todas seguras pra rodar duas vezes.
- **Contrato de schema** — o pipeline declara o que espera da origem e aborta
  com mensagem útil quando o contrato quebra.
- **Testável sem infraestrutura** — 92 testes rodam em 1 segundo, sem banco e
  sem acesso à rede.

## Rodando

```bash
pip install -r requirements.txt
cp .env.example .env      # preencher credenciais do MySQL

python -m pipelines.preparar          # origem → csv tratado
python -m pipelines.upload            # csv → MySQL
python -m pipelines.faturamento_full  # tabela analítica

python -m pytest tests/ -v            # 92 testes, sem precisar de banco
```

No Windows há dois `.ps1` que orquestram tudo (`rodar_etl.ps1` para o diário,
`rodar_sku_custo.ps1` para o incremental de 5 em 5 minutos).

## Estrutura

```
config/
  settings.py          caminhos e credenciais, num lugar só
  tables.py            estratégia de carga + contrato de schema por tabela
src/
  io/readers.py        leitura resiliente: encoding, separador, XML, decimal BR
  io/database.py       conexão, DDL, insert em lote — SQL sempre parametrizado
  transform/           funções puras, sem I/O — daí os testes rodarem sem nada
  load/strategies.py   replace / truncate / date_range / upsert
  quality/contracts.py validação de schema e normalização de colunas
pipelines/             preparar, upload, tabela analítica, carga incremental
tests/                 92 testes
```

## Três bugs que valem a leitura

Os três são de **perda ou corrupção silenciosa** — o pipeline seguia como se
tivesse dado certo.

### 1. Duplicidade permanente

`src/load/strategies.py` — a estratégia `date_range` apaga a janela de datas do
arquivo e reinsere. Linhas com data ilegível eram apenas avisadas e inseridas
assim mesmo.

O problema: o `DELETE` filtra por `STR_TO_DATE`, que devolve `NULL` justamente
nessas linhas, e `NULL BETWEEN x AND y` nunca é verdadeiro. Elas entravam no
banco e **nenhuma carga futura conseguia removê-las**. Cada rodada somava outra
cópia.

Hoje a carga aborta e mostra os valores problemáticos.

### 2. Backup de arquivo que nunca carregou

`pipelines/upload.py` — o arquivo era movido para o backup mesmo quando a
leitura falhava, porque a função devolvia `0` em vez de levantar exceção. O dado
do dia sumia da pasta de entrada sem nunca ter entrado no banco.

Hoje o backup só acontece depois do commit; o que falha fica onde está, pronto
pra nova tentativa.

### 3. Sucesso mentiroso no exit code

O erro era logado por arquivo e o processo saía com `0`. O orquestrador só
checa o exit code — então registrava **sucesso** enquanto arquivos falhavam.
Foi o que escondeu uma quebra por três dias seguidos.

## O detector de separador que perdia 33% da base

Este é o meu favorito, porque começou como otimização e virou correção.

A leitura demorava 21s e caía num reconstrutor heurístico escrito em Python
puro, em vez do parser em C do pandas. Investigando: a detecção fazia
`split(",")` cru, então qualquer CSV **bem-formado** com vírgula dentro de campo
entre aspas parecia quebrado.

```
csv.reader (respeita aspas):  2.922 de 2.922 linhas OK
split(",") cru:                  88 de 2.937 linhas OK
reconstrutor heurístico:      1.952 linhas — 984 DESCARTADAS (33%)
```

O campo culpado era `"17"" 205 50 ZR17 93W XL D7"` — aspas escapadas dentro de
campo citado. **Um terço da base sumia com um aviso no log.**

A correção usa `csv.reader` na detecção, e reconstruir virou último recurso.
Ganho: leitura 9x mais rápida **e** 984 linhas recuperadas. Também entrou um
limite: descarte acima de 2% aborta a carga em vez de avisar.

## Outras proteções, e o que cada uma evita

Todas nasceram de quebra real:

| Proteção | Evita |
|---|---|
| Sanitizar XML do `.xlsx` | caracteres de controle do ERP quebrando o openpyxl |
| Realinhar colunas por posição | origem trocar cabeçalho técnico por português |
| Colunas opcionais no contrato | coluna sumir da origem sem aviso |
| Deduplicar antes do join | vendedor repetido multiplicar linhas de cliente |
| Remontar decimal BR | `399,89` sem aspas quebrando o split por vírgula |
| Detectar encoding pelo BOM | mesma origem alternando UTF-8 e UTF-16 |
| Casar coluna por nome normalizado | typo na origem (`confins` por `cofins`) |
| Detectar formato numérico por coluna | origem alternando `1.399,90` e `759.90` |
| `NaN` → `None` antes do INSERT | driver mandar o texto `nan` pro banco |
| Recusar arquivo vazio | tabela boa ser zerada por uma exportação truncada |
| Montar tabela nova antes de trocar | falha no meio deixar o banco sem a tabela |

## Performance

Medido, não estimado:

| | antes | depois |
|---|---|---|
| leitura do faturamento | 21,6s | 2,3s |
| leitura do xlsx de clientes | 40s | 8,4s |
| pipeline completo | 52s | 27,5s |

O ganho do `.xlsx` veio de trocar o engine (`calamine` no lugar do `openpyxl`).
Detalhe contraintuitivo: usar `usecols` para ler só 37 das 530 colunas quase não
ajudou — o custo está em *parsear* o XML, não em materializar colunas.

## Decisões que eu não tomei

Duas coisas que pareciam melhorias óbvias e não são:

**Tipar as colunas do MySQL** (hoje é `LONGTEXT` pra tudo). Traria índice e
eliminaria dezenas de `CAST`. Mas um consumidor a jusante faz
`.replace('.','')` pra tirar separador de milhar — com `DECIMAL`, isso passa a
comer o ponto decimal e infla o custo em **100x**, sem erro nenhum. O ganho é
uma janela noturna mais rápida; o risco é corromper o número que decide compra
de estoque.

**Spark ou Airflow.** ~130 MB de dado, uma pessoa mantendo. Spark começa a
compensar uns 100x acima disso, e Airflow é um serviço a mais pra cuidar num
pipeline que é uma fila reta de 3 passos.

Complexidade que ninguém consegue manter é pior que o script simples que
funciona.

## Notas

Este repositório é uma versão anonimizada de um pipeline em produção. Nomes de
marca, depósitos e sistemas internos foram trocados por equivalentes genéricos;
o código e a lógica são os mesmos. Nenhum dado real acompanha o repositório — as
pastas em `dados/` vêm vazias, com um README explicando o que vai em cada uma.
