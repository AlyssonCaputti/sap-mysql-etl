# ETL SAP → MySQL

Pipeline que sincroniza dados de vendas de um SAP Business One para o MySQL que
alimenta os dashboards e o forecast de compra de estoque. Roda em produção,
sozinho, algumas vezes por hora.

É a refatoração de um pipeline que já existia e já tinha quebrado de várias
formas. Isso muda o que importa aqui: cada proteção no código existe porque um
incidente aconteceu, e a maior parte do trabalho foi descobrir **quais** falhas
o código antigo escondia — não reescrever bonito.

## O que estava errado

O pipeline antigo "funcionava". Rodava todo dia, exit code 0, log dizendo
sucesso. Três coisas aconteciam por baixo disso:

**Ele registrava sucesso enquanto a base parava.** O uploader logava o erro de
cada arquivo e saía com código 0. O script agendado só olha o exit code, então
o painel mostrava verde. Foi assim que três dias seguidos sem carga passaram sem
ninguém notar — um CSV separado por vírgula estava sendo lido com `;`, virava
uma coluna só com o cabeçalho inteiro no nome, e o MySQL recusava por
identificador longo demais.

**Ele arquivava arquivo que não tinha carregado.** A função de upload devolvia
`0` sem levantar exceção quando a leitura falhava, e o arquivo ia pro backup de
qualquer jeito. O dado do dia saía da pasta de entrada sem nunca ter entrado no
banco.

**Ele criava duplicata que ninguém conseguia apagar.** A estratégia de carga
por período apaga a janela de datas e reinsere. Linha com data ilegível passava
pelo aviso e entrava — mas o `DELETE` filtra por `STR_TO_DATE`, que devolve
`NULL` justamente nessas linhas, e `NULL BETWEEN x AND y` nunca é verdadeiro.
Elas entravam e **nenhuma carga futura conseguia removê-las**. Cada rodada
somava outra cópia.

E o que mais doeu, encontrado medindo: **um terço da base de itens era
descartado em silêncio.** A detecção de "CSV quebrado" fazia `split(",")` cru
numa amostra, então qualquer arquivo bem-formado com vírgula dentro de campo
entre aspas parecia quebrado e caía num reconstrutor heurístico:

```
csv.reader (respeita aspas):  2.922 de 2.922 linhas OK
split(",") cru:                  88 de 2.937 linhas OK
reconstrutor heurístico:      1.952 linhas — 984 DESCARTADAS (33%)
```

O campo culpado era `"17"" 205 50 ZR17 93W XL D7"` — aspas escapadas dentro de
campo citado. Um terço dos itens sumia com um aviso no log que ninguém lia.

## O que mudou na prática

| | antes | depois |
|---|---|---|
| falha na carga | exit 0, log verde | exit ≠ 0, tarefa agendada acusa |
| arquivo que falhou | ia pro backup | fica na entrada pra nova tentativa |
| data ilegível | entrava e travava lá | aborta com a lista dos valores |
| linhas de itens | 1.952 (33% descartado) | 2.922 |
| ciclo do faturamento | ~140s | ~76s |
| preparação completa | 52s | 27,5s |
| testes | 0 | 164 |

As **+984 linhas de itens** valem mais que os segundos. Velocidade se recupera
comprando máquina; dado descartado em silêncio, não.

## Carga incremental: 256 mil linhas viram 11 mil

A origem republica o CSV inteiro (100 MB, 256 mil linhas) de hora em hora, mas
o que muda é quase só o mês corrente — 1,3% do total. Reprocessar tudo pra
atualizar isso é desperdício puro.

O pipeline converte o CSV tratado num Parquet particionado por mês e carrega no
MySQL só os **dois últimos meses**:

|  | antes | agora |
|---|---|---|
| leitura | 11s (CSV 100 MB) | 0,1s (Parquet 16 MB) |
| linhas carregadas | 256.352 | 11.315 |
| INSERT | ~45s | ~3s |

Dois meses e não um por causa de nota retroativa: se a origem lançar hoje uma
nota com emissão do mês passado, uma janela de um mês não pegaria.

A tabela analítica continua sendo refeita **inteira**, e isso é decisão, não
esquecimento: a carteira do cliente vem da última compra dele da marca foco em
todo o histórico, então com só a janela quem comprou há meses apareceria como
"outros".

### O bug que essa otimização criou

Vale registrar porque é o tipo de coisa que só aparece quando você testa a
composição, não as peças.

A janela saía dos meses que *existiam* no disco, e o `DELETE` apagava o
intervalo contínuo `min..max`. Cada função estava certa sozinha. Juntas, com um
mês sem emissão no meio:

```
partições: 2026-08, 2026-10   (setembro sem nota)
janela:    [2026-08, 2026-10]
DELETE:    2026-08-01 .. 2026-10-31   ← inclui setembro
INSERT:    repõe só agosto e outubro
resultado: setembro apagado do banco
```

Agora a janela sai do calendário e o `DELETE` apaga mês a mês, com `IN` em vez
de `BETWEEN`. Junto disso: emissão no futuro passa a abortar antes de gravar
(uma nota com ano digitado errado criava partição que travava a janela pra
sempre), e o fallback `%m/%d/%Y` do parser de datas foi removido — ele lia
`03/08/2026` como 8 de março e mandava a linha pro mês errado, calado.

## Três pontos de checagem

A pergunta que eu quero responder olhando o log, sem abrir o banco: **o dado
está bom?**

```
origem → [porta 1] → transformação → [porta 2] → carga → [saída] → MySQL
```

| ponto | o que reporta | se falha aqui, o problema é |
|---|---|---|
| **porta 1** | linhas × colunas, linhas vazias, queda brusca de volume | da origem — cobra de quem exporta |
| **porta 2** | linhas perdidas no tratamento, chave duplicada ou vazia, datas ilegíveis e futuras | meu, entre uma porta e outra |
| **saída** | contagem por mês, origem × banco | da janela de carga |

A separação é o que torna o erro acionável. Porta 1 vermelha me faz abrir o
arquivo; porta 2 vermelha me faz abrir o `transform.py`.

O checkpoint de saída é o que enxerga o caso mais chato: linha que **existe na
origem, é válida, e nunca vai subir** porque caiu fora da janela de dois meses.
Rodando contra o banco real ele achou 48 linhas nessa situação, espalhadas por
três meses distintos — nenhuma detectável pelas validações normais, porque
individualmente cada linha está perfeita.

## Como as tabelas são carregadas

Uma pasta = uma tabela. O nome da pasta vira o nome da tabela, então adicionar
tabela é criar pasta, sem tocar em código.

| estratégia | mecanismo | onde | por quê |
|---|---|---|---|
| `date_range` | DELETE da janela + INSERT | faturamento | carga parcial sem perder histórico |
| `upsert` | ON DUPLICATE KEY UPDATE | clientes | coluna que sumiu da origem mantém valor antigo |
| `replace` | tabela nova + RENAME | itens, custo | cadastro completo, sem histórico |
| `truncate` | TRUNCATE + INSERT | vendedores | preserva schema ajustado na mão |

As quatro são idempotentes — rodar duas vezes dá o mesmo estado. O `replace`
monta numa tabela temporária e só faz `RENAME` no fim: erro no meio não deixa o
banco sem a tabela (já deixou, uma vez, quando o `DROP` vinha primeiro).

A conferência pós-carga roda `COUNT(*)` **dentro da transação**, antes do
commit. Divergência entre o que mandei e o que chegou levanta exceção e cai no
rollback.

## As cicatrizes

Todo trecho defensivo aqui previne uma falha que já aconteceu. Os testes
marcados `CICATRIZ` reproduzem o incidente, e o comentário no código diz qual
foi. Se você acha que algum deles é paranoia, provavelmente é — a paranoia
custou três dias de base parada uma vez.

- caracteres de controle no XML do `.xlsx` quebrando o parser
- cabeçalho técnico do SAP virando português, duas vezes, sem aviso
- coluna opcional sumindo da origem de um dia pro outro
- vendedor duplicado multiplicando linhas de cliente no LEFT JOIN
- decimal BR sem aspas (`399,89`) partindo em dois no split por vírgula
- mesmo arquivo exportado em UTF-8 num dia e UTF-16 no outro
- typo na origem (`confins` por `cofins`) que persiste até hoje
- origem alternando entre `1.399,90` e `759.90` no mesmo campo
- separador vírgula lido como `;` → base parada três dias
- `NaN` do pandas virando o texto literal `nan` no INSERT
- `STR_TO_DATE` abortando a query inteira em vez de devolver NULL
- tabela sem custo derrubando o cálculo de margem pra fallback silencioso
- arquivo vazio substituindo tabela boa
- uma parcela vazia zerando a margem inteira por NULL propagation
- limite de ~196 colunas LONGTEXT forçando o fatiamento de itens

## Rodando

```bash
pip install -r requirements.txt
cp .env.example .env    # preencher credenciais e caminhos

python -m pipelines.preparar              # origens → CSVs tratados
python -m pipelines.upload                # CSVs → MySQL
python -m pipelines.faturamento_horario   # carga incremental + tabela analítica

python -m pipelines.faturamento_horario --status   # relata sem escrever
python -m pipelines.faturamento_horario --tudo     # recarrega o histórico todo
python -m pytest tests/                            # 164 testes, ~3s
```

No Windows, os `.ps1` na raiz são o que as Tarefas Agendadas chamam:

| tarefa | frequência | atualiza |
|---|---|---|
| `rodar_faturamento_horario.ps1` | 1 hora | faturamento + tabela analítica |
| `rodar_etl.ps1` | 1 dia | clientes, itens |
| `rodar_sku_custo.ps1` | 5 min | custo por depósito |

Os dois pipelines frequentes comparam o hash da origem antes de trabalhar e
**saem calados quando nada mudou** — rodando a cada 5 minutos, logar "sem
mudança" 288 vezes por dia enterra o que importa. Usam lock com expiração,
então carga que demore mais que o intervalo faz a rodada seguinte ser pulada em
vez de rodar em paralelo na mesma tabela.

## Estrutura

```
config/       caminhos, credenciais, estratégia e contrato por tabela
src/
  io/         leitura resiliente e acesso ao banco. Sem regra de negócio.
  transform/  uma função pura por entidade. Sem I/O — testa sem infra.
  quality/    contrato de schema e os três pontos de checagem
  load/       as quatro estratégias de carga
pipelines/    orquestração: quem roda em que ordem
tests/        164 testes, nenhum precisa de banco ou rede
```

A separação `io` / `transform` é o que permite os testes rodarem em 3 segundos
sem MySQL: transformação recebe e devolve DataFrame, e quem faz I/O é injetado.

## Testes

164, rodando em ~3s, sem banco e sem rede — cursor falso pro MySQL, pasta
temporária pros arquivos.

O que eu me preocupei em cobrir, em ordem:

- **as três falhas silenciosas**, cada uma com o cenário que a causou.
  Verifiquei por regressão: revertendo a correção da carga por período, dois
  testes falham
- **a margem de contribuição**, em duas frentes — composição do SQL (parcelas,
  sinais, contagem de colunas do INSERT batendo com o SELECT) e execução real
  da aritmética em SQLite. Era o gap que mais importava: é o número que decide
  compra de estoque, e só era validável rodando contra o MySQL, então na
  prática ninguém validava. Injetei 5 erros distintos na fórmula e todos foram
  pegos
- **a janela de meses**: gap entre partições, data futura, e equivalência com o
  comportamento anterior nos dados reais
- **edge cases que já apareceram na origem**: NULL, string vazia, data
  inválida, arquivo vazio, schema inesperado, acento, duplicado, UTF-16,
  decimal BR ambíguo, e identificador malicioso do tipo `tabela; DROP TABLE x`

Sem cobertura ainda: `preparar.py` e `load/views.py`.

## Dívida conhecida

**Tudo é `LONGTEXT` no destino.** Isso mata índice, obriga `CAST` em quem
consulta, e é o motivo do fatiamento de itens (o servidor não aguenta 475
colunas LONGTEXT numa tabela, então a origem é dividida em várias e uma VIEW
recompõe).

Investiguei tipar e a recomendação é **não mexer agora**. O ganho principal é
performance, e o gargalo medido é rede e round-trip, não CPU de query. Já o
risco é assimétrico: um dos consumidores tem um parser de decimal BR
hardcoded que faz `.replace('.', '')` pra tirar separador de milhar. Com a
coluna virando `DECIMAL`, esse `replace` passa a comer o ponto decimal e infla
o valor em 100×. Sem exceção, sem log — e o filtro seguinte (`custo > 0`)
aceita valor inflado numa boa.

Errar aqui corrompe o cálculo de margem em silêncio, e esse número decide
compra de estoque. Não vale trocar full scan por isso.

Outras pendências honestas:

- validação de domínio não existe: o pipeline aceita UF inválida, CNPJ vazio ou
  emissão em 2099 sem reclamar. É risco, não problema ativo — nenhuma dessas
  apareceu nos logs
- sem alerta automático por volume anormal ou freshness. O exit code ≠ 0
  permite que a tarefa agendada avise, e é o mínimo viável
- `_backup/` acumula CSVs indefinidamente, e eles têm dado pessoal de cliente.
  Falta política de expurgo

## Escala

Isto é um pipeline de ~8 arquivos por dia, 260 mil linhas, mantido por uma
pessoa. A arquitetura é proporcional a isso de propósito.

Com 10× o volume, pandas ainda serve (ajuste provável é `chunksize` na
leitura). Com 100×, deixa de servir — e o caminho **não é Spark**, é empurrar a
transformação pro banco (a tabela analítica já faz isso) ou DuckDB/Polars
local. Cluster pra pipeline de uma pessoa é trocar um problema por dois.
