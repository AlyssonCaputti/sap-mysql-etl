# ETL SAP → MySQL

Sincronizo dados de vendas de um SAP Business One para o MySQL que alimenta os
dashboards e o cálculo de compra de estoque. Roda em produção, sozinho, algumas
vezes por hora.

É a refatoração de um pipeline que já existia e já tinha quebrado de várias
formas. O trabalho principal não foi reescrever bonito: foi descobrir **quais
falhas o código antigo escondia**.

## O resumo

| | antes | depois |
|---|---|---|
| Falha na carga | exit 0, log dizia sucesso | exit ≠ 0, a tarefa agendada acusa |
| Arquivo que falhou | ia pro backup mesmo assim | fica na entrada pra nova tentativa |
| Data ilegível | entrava e travava no banco | aborta com a lista dos valores |
| Linhas de itens carregadas | 1.952 (33% descartado) | 2.922 |
| Ciclo do faturamento | ~140s | ~76s |
| Preparação completa | 52s | 27,5s |
| Testes | 0 | 164 |

As **+984 linhas de itens** importam mais que os segundos. Tempo eu recupero
comprando máquina melhor. Dado descartado em silêncio, não.

---

## As 3 falhas que eu encontrei

O pipeline antigo "funcionava". Rodava todo dia, saía com código 0, log verde.

### 1. Registrava sucesso enquanto a base parava

| | |
|---|---|
| **O que fazia** | Logava o erro de cada arquivo e saía com exit code 0 |
| **Por que passou** | O script agendado só olha `$LASTEXITCODE`, então o painel ficava verde |
| **O custo** | 3 dias seguidos sem carga, sem ninguém notar |
| **A causa real** | Um CSV separado por vírgula lido com `;` virava uma coluna só, com o cabeçalho inteiro no nome, e o MySQL recusava por identificador longo demais |
| **Como resolvi** | Qualquer falha propaga pro exit code |

### 2. Arquivava arquivo que não tinha carregado

| | |
|---|---|
| **O que fazia** | A função de upload devolvia `0` sem levantar exceção quando a leitura falhava, e o arquivo ia pro `_backup` de qualquer jeito |
| **O custo** | O dado do dia saía da pasta de entrada sem nunca ter entrado no banco |
| **Como resolvi** | Backup só depois do commit bem-sucedido. O que falha fica na entrada |

### 3. Criava duplicata que ninguém conseguia apagar

A estratégia de carga por período apaga a janela de datas e reinsere. Linha com
data ilegível passava pelo aviso e entrava.

| Passo | O que acontece |
|---|---|
| O `DELETE` filtra por `STR_TO_DATE` | Devolve `NULL` justamente nas linhas ilegíveis |
| `NULL BETWEEN x AND y` | Nunca é verdadeiro |
| Resultado | A linha entra e **nenhuma carga futura consegue removê-la** |
| A cada rodada | Soma outra cópia |

**Como resolvi:** a carga aborta e mostra os valores problemáticos. Melhor ficar
com o dado de ontem do que acumular duplicata permanente.

### E a pior de todas, que achei medindo

Um terço da base de itens era descartado em silêncio. A detecção de "CSV
quebrado" fazia `split(",")` cru numa amostra, então qualquer arquivo
bem-formado com vírgula dentro de campo entre aspas parecia quebrado e caía num
reconstrutor heurístico.

| Método de leitura | Linhas OK |
|---|---|
| `csv.reader` (respeita aspas) | 2.922 de 2.922 |
| `split(",")` cru | 88 de 2.937 |
| Reconstrutor heurístico | 1.952 (**984 descartadas**) |

O campo culpado era `"17"" 205 50 ZR17 93W XL D7"`, com aspas escapadas dentro
de campo citado. Troquei a detecção por `csv.reader` e coloquei um limite: acima
de 2% de descarte, a carga aborta em vez de subir base incompleta.

---

## Carga incremental: 256 mil linhas viram 11 mil

A origem republica o CSV inteiro toda hora, mas o que muda é quase só o mês
corrente (1,3% do total). Reprocessar tudo pra atualizar isso é desperdício.

Converto o CSV tratado num Parquet particionado por mês e carrego no MySQL só os
**2 últimos meses**.

| Etapa | antes | agora |
|---|---|---|
| Leitura | 11s (CSV de 100 MB) | 0,1s (Parquet de 16 MB) |
| Linhas carregadas | 256.352 | 11.315 |
| INSERT | ~45s | ~3s |

Uso 2 meses e não 1 por causa de nota retroativa. Se a origem lançar hoje uma
nota com emissão do mês passado, uma janela de 1 mês não pegaria.

A tabela analítica continua sendo refeita **inteira**, e isso é decisão, não
esquecimento: a carteira do cliente vem da última compra dele da marca foco em
todo o histórico. Com só a janela, quem comprou há meses apareceria como
"outros".

### O bug que essa otimização criou

Registro porque é o tipo de coisa que só aparece quando você testa a
composição, não as peças isoladas.

| Peça | Estava correta sozinha? |
|---|---|
| A janela saía dos meses que existiam no disco | Sim |
| O `DELETE` apagava o intervalo contínuo `min..max` | Sim |
| As duas juntas | **Não** |

Com um mês sem emissão no meio:

```
partições: 2026-08, 2026-10   (setembro sem nota)
janela:    [2026-08, 2026-10]
DELETE:    2026-08-01 .. 2026-10-31   ← inclui setembro
INSERT:    repõe só agosto e outubro
resultado: setembro apagado do banco
```

Três correções nisso:

| Problema | Correção |
|---|---|
| Janela saía dos meses existentes | Passa a sair do calendário (mês corrente pra trás) |
| `DELETE` apagava intervalo contínuo | Apaga mês a mês, com `IN` em vez de `BETWEEN` |
| Emissão no futuro criava partição órfã que travava a janela | Aborta antes de gravar |

Removi também o fallback `%m/%d/%Y` do parser de datas. Ele lia `03/08/2026`
como 8 de março e mandava a linha pro mês errado, sem avisar.

---

## Os 3 pontos de checagem

A pergunta que quero responder olhando o log, sem abrir o banco: **o dado está
bom?**

```
origem → [porta 1] → transformação → [porta 2] → carga → [saída] → MySQL
```

| Ponto | O que reporta | Se falha aqui, o problema é |
|---|---|---|
| **porta 1** (recepção) | linhas × colunas, linhas vazias, queda brusca de volume | da origem, cobro de quem exporta |
| **porta 2** (transformação) | linhas perdidas no tratamento, chave duplicada ou vazia, datas ilegíveis e futuras | meu, entre uma porta e outra |
| **saída** (carga) | contagem por mês, origem × banco | da janela de carga |

A separação é o que torna o erro acionável. Porta 1 vermelha me faz abrir o
arquivo. Porta 2 vermelha me faz abrir o `transform.py`.

O checkpoint de saída pega o caso mais chato: linha que **existe na origem, é
válida, e nunca vai subir** porque caiu fora da janela de 2 meses. Rodando
contra o banco real ele achou 48 linhas nessa situação, em 3 meses distintos.
Nenhuma validação normal pegaria, porque individualmente cada linha está
perfeita.

---

## Como as tabelas são carregadas

Uma pasta = uma tabela. O nome da pasta vira o nome da tabela, então adicionar
tabela é criar pasta, sem tocar em código.

| Estratégia | Mecanismo | Onde uso | Por quê |
|---|---|---|---|
| `date_range` | DELETE da janela + INSERT | faturamento | carga parcial sem perder histórico |
| `upsert` | ON DUPLICATE KEY UPDATE | clientes | coluna que sumiu da origem mantém o valor antigo |
| `replace` | tabela nova + RENAME | itens, custo | cadastro completo, sem histórico |
| `truncate` | TRUNCATE + INSERT | vendedores | preserva schema ajustado na mão |

As 4 são idempotentes: rodar duas vezes dá o mesmo estado.

Dois detalhes que vieram de incidente:

| Proteção | Motivo |
|---|---|
| O `replace` monta em tabela temporária e só faz `RENAME` no fim | Quando o `DROP` vinha primeiro, um erro no meio deixava o banco sem a tabela |
| A conferência pós-carga roda `COUNT(*)` dentro da transação, antes do commit | Divergência entre o enviado e o gravado levanta exceção e cai no rollback |

---

## As cicatrizes

Todo trecho defensivo aqui previne uma falha que já aconteceu. Os testes
marcados `CICATRIZ` reproduzem o incidente, e o comentário no código diz qual
foi. Se algum parece paranoia, provavelmente é. A paranoia custou 3 dias de base
parada uma vez.

| Origem faz isso | E quebrava assim |
|---|---|
| Manda caractere de controle no XML do `.xlsx` | Parser estourava com "not well-formed" |
| Troca cabeçalho técnico por português | Aconteceu 2 vezes, com entidades diferentes |
| Some com coluna opcional de um dia pro outro | `KeyError` no meio da transformação |
| Repete vendedor na planilha | Multiplicava linhas de cliente no LEFT JOIN |
| Exporta decimal BR sem aspas (`399,89`) | Partia em dois no split por vírgula |
| Alterna UTF-8 e UTF-16 no mesmo arquivo | Lia caractere corrompido |
| Escreve `confins` em vez de `cofins` | Coluna não casava pelo nome |
| Alterna `1.399,90` e `759.90` no mesmo campo | Conversão numérica errava a escala |
| Troca `;` por `,` como separador | Base parada 3 dias |
| Publica tabela sem a coluna de custo | Cálculo de margem caía em fallback silencioso |
| Manda arquivo vazio | Substituía tabela boa por nada |

Mais duas do lado do destino:

| Situação | Proteção |
|---|---|
| `NaN` do pandas virava o texto literal `nan` no INSERT | Converto pra `None` antes de enviar |
| Uma parcela vazia zerava a margem inteira por NULL propagation | Nas parcelas do cálculo, vazio vira `0`, não `NULL` |

---

## Rodando

```bash
pip install -r requirements.txt
cp .env.example .env    # preencher credenciais e caminhos

python -m pipelines.preparar              # origens → CSVs tratados
python -m pipelines.upload                # CSVs → MySQL
python -m pipelines.faturamento_horario   # carga incremental + tabela analítica
```

| Comando | O que faz |
|---|---|
| `--status` | relata o estado sem escrever nada |
| `--tudo` | recarrega o histórico completo, não só a janela |
| `--forcar` | roda mesmo se o hash da origem não mudou |
| `pytest tests/` | 164 testes, ~3s, sem banco e sem rede |

No Windows, os `.ps1` na raiz são o que as Tarefas Agendadas chamam:

| Tarefa | Frequência | Atualiza |
|---|---|---|
| `rodar_faturamento_horario.ps1` | 1 hora | faturamento + tabela analítica |
| `rodar_etl.ps1` | 1 dia | clientes, itens |
| `rodar_sku_custo.ps1` | 5 min | custo por depósito |

Os dois pipelines frequentes têm duas proteções que valem explicar:

| Proteção | Por quê |
|---|---|
| Comparam o hash da origem e **saem calados** quando nada mudou | Rodando a cada 5 min, logar "sem mudança" 288 vezes por dia enterra o que importa |
| Lock com expiração | Carga que demore mais que o intervalo faz a rodada seguinte ser pulada, em vez de duas escreverem na mesma tabela |

---

## Estrutura

| Pasta | Responsabilidade |
|---|---|
| `config/` | caminhos, credenciais, estratégia e contrato por tabela |
| `src/io/` | leitura resiliente e acesso ao banco. Sem regra de negócio |
| `src/transform/` | uma função pura por entidade. Sem I/O |
| `src/quality/` | contrato de schema e os 3 pontos de checagem |
| `src/load/` | as 4 estratégias de carga |
| `pipelines/` | orquestração: quem roda em que ordem |
| `tests/` | 164 testes, nenhum precisa de banco ou rede |

A separação `io` / `transform` é o que faz os testes rodarem em 3 segundos sem
MySQL. Transformação recebe e devolve DataFrame, e quem faz I/O é injetado.

O que a refatoração consolidou:

| Antes | Depois |
|---|---|
| 4 implementações divergentes de leitura de CSV | 1 em `src/io/readers.py` |
| Caminho de rede hardcoded em 6 arquivos | `config/settings.py` |
| Transformação executada no import do módulo | funções puras + `main()` |
| SQL montado por concatenação de string | parâmetros + validação de identificador |
| `except Exception: pass` | erro com causa e sugestão |
| Porta 8080 em 2 arquivos, 3306 em outros 2 | 3306 em todos |

---

## Testes

164, em ~3s, sem banco e sem rede. Uso cursor falso pro MySQL e pasta temporária
pros arquivos.

| O que cobri | Por que priorizei |
|---|---|
| As 3 falhas silenciosas, com o cenário que causou cada uma | Verifiquei por regressão: revertendo a correção da carga por período, 2 testes falham |
| O cálculo de margem, em 2 frentes | É o número que decide compra de estoque, e só era validável rodando contra o MySQL. Na prática, ninguém validava |
| A janela de meses | Gap entre partições, data futura, e equivalência com o comportamento anterior nos dados reais |
| Edge cases que já apareceram na origem | NULL, string vazia, data inválida, arquivo vazio, schema inesperado, acento, duplicado, UTF-16, decimal BR ambíguo, e identificador malicioso do tipo `tabela; DROP TABLE x` |

Sobre a margem: testo a composição do SQL (parcelas, sinais, contagem de colunas
do INSERT batendo com o SELECT) **e** executo a aritmética de verdade em SQLite.
Injetei 5 erros distintos na fórmula e todos foram pegos.

Sem cobertura ainda: `preparar.py` e `load/views.py`.

---

## Dívida conhecida

**Tudo é `LONGTEXT` no destino.** Isso mata índice, obriga `CAST` em quem
consulta, e é o motivo do fatiamento de itens (o servidor não aguenta 475
colunas LONGTEXT numa tabela, então divido em várias e uma VIEW recompõe).

Investiguei tipar e decidi **não mexer agora**:

| Fator | Avaliação |
|---|---|
| Ganho | performance, mas o gargalo medido é rede e round-trip, não CPU de query |
| Custo | 3 fases, 2 repositórios, e uma janela em que ETL e consumidores mudam juntos |
| Risco | assimétrico, e é o que decide |

O risco concreto: um dos consumidores tem parser de decimal BR hardcoded que faz
`.replace('.', '')` pra tirar separador de milhar. Com a coluna virando
`DECIMAL`, esse `replace` passa a comer o ponto decimal e infla o valor em 100×.
Sem exceção, sem log, e o filtro seguinte (`custo > 0`) aceita valor inflado
numa boa.

Errar ali corrompe o cálculo de margem em silêncio, e esse número decide compra
de estoque. Não vale trocar full scan por isso.

Outras pendências, honestamente:

| Pendência | Situação |
|---|---|
| Validação de domínio | Não existe. Aceito UF inválida, CNPJ vazio ou emissão em 2099 sem reclamar. É risco, não problema ativo |
| Alerta automático | Não tem, por volume anormal nem freshness. O exit code ≠ 0 permite a tarefa agendada avisar, e é o mínimo viável |
| Retenção do backup | `_backup/` acumula CSVs indefinidamente, e eles têm dado pessoal. Falta política de expurgo |

---

## Escala

Isto é um pipeline de ~8 arquivos por dia, 260 mil linhas, mantido por 1 pessoa.
A arquitetura é proporcional a isso de propósito.

| Volume | Ainda serve? |
|---|---|
| 10× o atual (2,5M linhas) | Sim. Ajuste provável é `chunksize` na leitura |
| 100× (25M linhas) | Não. Pandas em memória deixa de servir |

Se chegar em 100×, o caminho **não é Spark**. É empurrar a transformação pro
banco (a tabela analítica já faz isso) ou usar DuckDB/Polars local. Cluster pra
pipeline de 1 pessoa é trocar um problema por dois.
