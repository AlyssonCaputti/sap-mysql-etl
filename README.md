# ETL SAP → MySQL

Sincronizo dados de vendas de um SAP Business One para o MySQL que alimenta os
dashboards e o cálculo de compra de estoque. Roda em produção, sozinho, algumas
vezes por hora.

É a refatoração de um pipeline que já existia e já tinha quebrado de várias
formas. O trabalho principal não foi reescrever bonito: foi descobrir quais
falhas o código antigo escondia.

| | antes | depois |
|---|---|---|
| Falha na carga | exit 0, log dizia sucesso | exit ≠ 0, a tarefa agendada acusa |
| Linhas de itens carregadas | 1.952 (33% descartado) | 2.922 |
| Ciclo do faturamento | ~140s | ~76s |
| Testes | 0 | 164 |

As +984 linhas de itens importam mais que os segundos. Tempo eu recupero
comprando máquina melhor. Dado descartado em silêncio, não.

## As 3 falhas que eu encontrei

O pipeline antigo "funcionava". Rodava todo dia, saía com código 0, log verde.

**Registrava sucesso enquanto a base parava.** Logava o erro de cada arquivo e
saía com exit 0. O script agendado só olha o `$LASTEXITCODE`, então o painel
ficava verde. Foi assim que 3 dias seguidos sem carga passaram sem ninguém
notar: um CSV separado por vírgula estava sendo lido com `;`, virava uma coluna
só com o cabeçalho inteiro no nome, e o MySQL recusava por identificador longo
demais.

**Arquivava arquivo que não tinha carregado.** A função de upload devolvia `0`
sem levantar exceção quando a leitura falhava, e o arquivo ia pro backup de
qualquer jeito. O dado do dia saía da pasta de entrada sem nunca ter entrado no
banco. Agora o backup só acontece depois do commit.

**Criava duplicata que ninguém conseguia apagar.** A carga por período apaga a
janela de datas e reinsere. Linha com data ilegível passava pelo aviso e
entrava, mas o `DELETE` filtra por `STR_TO_DATE`, que devolve `NULL` justamente
nessas linhas, e `NULL BETWEEN x AND y` nunca é verdadeiro. Elas entravam e
nenhuma carga futura conseguia removê-las: cada rodada somava outra cópia. Agora
aborta e mostra os valores problemáticos.

### A pior, que achei medindo

Um terço da base de itens era descartado em silêncio. A detecção de "CSV
quebrado" fazia `split(",")` cru numa amostra, então qualquer arquivo
bem-formado com vírgula dentro de campo entre aspas parecia quebrado e caía num
reconstrutor heurístico:

```
csv.reader (respeita aspas):  2.922 de 2.922 linhas OK
split(",") cru:                  88 de 2.937 linhas OK
reconstrutor heurístico:      1.952 linhas (984 descartadas)
```

O campo culpado era `"17"" 205 50 ZR17 93W XL D7"`, com aspas escapadas dentro
de campo citado. Troquei a detecção por `csv.reader` e coloquei limite: acima de
2% de descarte, a carga aborta em vez de subir base incompleta.

## Carga incremental

A origem republica o CSV inteiro (100 MB, 256 mil linhas) toda hora, mas o que
muda é quase só o mês corrente, 1,3% do total. Converto num Parquet
particionado por mês e carrego no MySQL só os 2 últimos meses: a leitura cai de
11s para 0,1s, e o INSERT de ~45s para ~3s.

Uso 2 meses e não 1 por causa de nota retroativa. Se a origem lançar hoje uma
nota com emissão do mês passado, uma janela de 1 mês não pegaria.

A tabela analítica continua sendo refeita inteira, e isso é decisão, não
esquecimento: a carteira do cliente vem da última compra dele da marca foco em
todo o histórico. Com só a janela, quem comprou há meses apareceria como
"outros".

### O bug que essa otimização criou

Vale registrar porque só aparece quando você testa a composição, não as peças.
A janela saía dos meses que existiam no disco e o `DELETE` apagava o intervalo
contínuo `min..max`. Cada parte estava certa sozinha. Juntas, com um mês sem
emissão no meio:

```
partições: 2026-08, 2026-10   (setembro sem nota)
DELETE:    2026-08-01 .. 2026-10-31   ← inclui setembro
INSERT:    repõe só agosto e outubro
resultado: setembro apagado do banco
```

Agora a janela sai do calendário e o `DELETE` apaga mês a mês, com `IN` em vez
de `BETWEEN`. Emissão no futuro passa a abortar antes de gravar, porque criava
partição órfã que travava a janela pra sempre. E removi o fallback `%m/%d/%Y` do
parser de datas: ele lia `03/08/2026` como 8 de março e mandava a linha pro mês
errado, sem avisar.

## Os 3 pontos de checagem

A pergunta que quero responder olhando o log, sem abrir o banco: o dado está
bom?

```
origem → [porta 1] → transformação → [porta 2] → carga → [saída] → MySQL
```

A **porta 1** olha o dado cru: linhas, colunas, linhas vazias, queda brusca de
volume. Se falha aqui, o problema é da origem e eu cobro de quem exporta.

A **porta 2** olha depois do tratamento: quantas linhas sumiram, chave duplicada
ou vazia, datas ilegíveis e futuras. Se falha aqui, o problema é meu, entre uma
porta e outra.

A **saída** compara a contagem por mês entre origem e banco. É o que pega o caso
mais chato: linha que existe na origem, é válida, e nunca vai subir porque caiu
fora da janela de 2 meses. Rodando contra o banco real ele achou 48 linhas nessa
situação, em 3 meses distintos. Nenhuma validação normal pegaria, porque
individualmente cada linha está perfeita.

A separação é o que torna o erro acionável: porta 1 vermelha me faz abrir o
arquivo, porta 2 vermelha me faz abrir o `transform.py`.

## Como as tabelas são carregadas

Uma pasta = uma tabela. O nome da pasta vira o nome da tabela, então adicionar
tabela é criar pasta, sem tocar em código.

| Estratégia | Onde uso | Por quê |
|---|---|---|
| `date_range` | faturamento | carga parcial sem perder histórico |
| `upsert` | clientes | coluna que sumiu da origem mantém o valor antigo |
| `replace` | itens, custo | cadastro completo, sem histórico |
| `truncate` | vendedores | preserva schema ajustado na mão |

As 4 são idempotentes. O `replace` monta numa tabela temporária e só faz
`RENAME` no fim, porque quando o `DROP` vinha primeiro um erro no meio deixava o
banco sem a tabela. E a conferência pós-carga roda `COUNT(*)` dentro da
transação, antes do commit: divergência entre o enviado e o gravado levanta
exceção e cai no rollback.

## As cicatrizes

Todo trecho defensivo aqui previne uma falha que já aconteceu. Os testes
marcados `CICATRIZ` reproduzem o incidente, e o comentário no código diz qual
foi. Se algum parece paranoia, provavelmente é. A paranoia custou 3 dias de base
parada uma vez.

O que a origem já fez, sem avisar: mandou caractere de controle no XML do
`.xlsx`, trocou o cabeçalho técnico por português duas vezes, sumiu com coluna
opcional de um dia pro outro, repetiu vendedor na planilha (multiplicando linhas
de cliente no LEFT JOIN), exportou decimal BR sem aspas, alternou UTF-8 e
UTF-16, escreveu `confins` em vez de `cofins`, alternou `1.399,90` e `759.90` no
mesmo campo, trocou o separador, publicou tabela sem a coluna de custo e mandou
arquivo vazio.

Do lado do destino: o `NaN` do pandas virava o texto literal `nan` no INSERT, e
uma parcela vazia zerava a margem inteira por NULL propagation.

## Rodando

```bash
pip install -r requirements.txt
cp .env.example .env    # preencher credenciais e caminhos

python -m pipelines.preparar              # origens → CSVs tratados
python -m pipelines.upload                # CSVs → MySQL
python -m pipelines.faturamento_horario   # carga incremental + tabela analítica

pytest tests/                             # 164 testes, ~3s
```

Flags úteis: `--status` relata sem escrever, `--tudo` recarrega o histórico
completo, `--forcar` roda mesmo se o hash da origem não mudou.

No Windows, os `.ps1` na raiz são o que as Tarefas Agendadas chamam: faturamento
de hora em hora, clientes e itens uma vez por dia, custo por depósito a cada 5
minutos.

Os dois pipelines frequentes comparam o hash da origem e saem calados quando
nada mudou, porque rodando a cada 5 minutos um "sem mudança" 288 vezes por dia
enterra o que importa. E usam lock com expiração, então carga que demore mais
que o intervalo faz a rodada seguinte ser pulada em vez de duas escreverem na
mesma tabela.

## Estrutura

```
config/       caminhos, credenciais, estratégia e contrato por tabela
src/io/       leitura resiliente e acesso ao banco. Sem regra de negócio.
src/transform/  uma função pura por entidade. Sem I/O.
src/quality/  contrato de schema e os 3 pontos de checagem
src/load/     as 4 estratégias de carga
pipelines/    orquestração: quem roda em que ordem
tests/        164 testes, nenhum precisa de banco ou rede
```

A separação `io` / `transform` é o que faz os testes rodarem em 3 segundos sem
MySQL: transformação recebe e devolve DataFrame, e quem faz I/O é injetado.

A refatoração também consolidou 4 implementações divergentes de leitura de CSV
em uma, tirou o caminho de rede que estava hardcoded em 6 arquivos, trocou SQL
montado por concatenação por parâmetros com validação de identificador, e
eliminou os `except Exception: pass`.

## Testes

164, em ~3s, sem banco e sem rede. Uso cursor falso pro MySQL e pasta temporária
pros arquivos.

Priorizei três coisas. As 3 falhas silenciosas, cada uma com o cenário que a
causou (verifiquei por regressão: revertendo a correção da carga por período, 2
testes falham). O cálculo de margem, testando a composição do SQL e executando a
aritmética de verdade em SQLite, porque é o número que decide compra de estoque
e só era validável rodando contra o MySQL, então na prática ninguém validava.
Injetei 5 erros distintos na fórmula e todos foram pegos. E a janela de meses:
gap entre partições, data futura, e equivalência com o comportamento anterior
nos dados reais.

Sem cobertura ainda: `preparar.py` e `load/views.py`.

## Dívida conhecida

Tudo é `LONGTEXT` no destino. Isso mata índice, obriga `CAST` em quem consulta,
e é o motivo do fatiamento de itens (o servidor não aguenta 475 colunas LONGTEXT
numa tabela, então divido em várias e uma VIEW recompõe).

Investiguei tipar e decidi não mexer agora. O ganho é performance, mas o
gargalo medido é rede e round-trip, não CPU de query. E o risco é assimétrico:
um dos consumidores tem parser de decimal BR hardcoded que faz
`.replace('.', '')` pra tirar separador de milhar. Com a coluna virando
`DECIMAL`, esse `replace` passa a comer o ponto decimal e infla o valor em 100×,
sem exceção e sem log. O filtro seguinte (`custo > 0`) aceita valor inflado numa
boa. Errar ali corrompe o cálculo de margem em silêncio, e esse número decide
compra de estoque.

Outras pendências: não tenho validação de domínio (aceito UF inválida, CNPJ
vazio ou emissão em 2099 sem reclamar), não tenho alerta automático por volume
anormal ou freshness (o exit code ≠ 0 permite a tarefa agendada avisar, e é o
mínimo viável), e o `_backup/` acumula CSVs com dado pessoal indefinidamente,
sem política de expurgo.

## Escala

Isto é um pipeline de ~8 arquivos por dia, 260 mil linhas, mantido por 1 pessoa.
A arquitetura é proporcional a isso de propósito.

Com 10× o volume, pandas ainda serve (ajuste provável é `chunksize` na leitura).
Com 100×, deixa de servir, e o caminho não é Spark: é empurrar a transformação
pro banco, como a tabela analítica já faz, ou usar DuckDB/Polars local. Cluster
pra pipeline de 1 pessoa é trocar um problema por dois.
