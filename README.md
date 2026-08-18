# ETL — Atualizar VPS (refatorado)

Sincroniza dados de vendas do SAP para o MySQL da VPS, que alimenta os
dashboards e o Forecast.

Refatoração de um pipeline anterior, que permanece intocado — este é um projeto
novo, lado a lado.

---

## Estrutura

```
config/
  settings.py       caminhos e credenciais (antes hardcoded em 6 arquivos)
  tables.py         estratégia de carga + contratos de schema por tabela
src/
  io/readers.py     leitura resiliente (encoding, separador, XML, decimal BR)
  io/database.py    conexão, DDL, insert em lote — SQL parametrizado
  transform/        clientes, faturamento, itens — funções puras, sem I/O
  load/strategies.py replace / truncate / date_range / upsert
  load/views.py     VIEW ItensCompleto
  quality/contracts.py validação de schema e normalização de colunas
pipelines/
  preparar.py       origens → CSVs tratados
  upload.py         CSVs → MySQL
  faturamento_full.py tabela analítica (JOIN no banco)
  sku_custo.py      carga incremental por hash (a cada 5 min)
dados/              origens brutas do SAP (uma pasta por entidade)
  para_vps/         saída tratada — o nome da pasta vira o nome da tabela
tests/              127 testes, rodam sem banco e sem acesso à rede
.claude/            skills e relatórios (fora do git)
```

## Documentação

`.claude/relatorios/RELATORIO.md` — documento único, na ordem em que faz sentido
ler se está chegando agora:

1. o que o pipeline faz e as decisões de design (seções 1-2)
2. as 15 cicatrizes de produção e os 3 críticos corrigidos (3-4)
3. onde o tempo vai e o que foi otimizado, 52s → 27,5s (5)
4. a decisão em aberto mais importante — LONGTEXT — e por que a recomendação
   é não mexer (6)
5. auditoria por dimensão, scorecard e pendências (7-9)

As skills que produziram esses documentos estão em `.claude/skills/`
(`refatorar-etl` e `auditar-etl`).

## Uso

```powershell
pip install -r requirements.txt
copy .env.example .env          # preencher credenciais

.\rodar_etl.ps1                 # diário: clientes e itens
.\rodar_faturamento_horario.ps1 # de hora em hora: faturamento + faturamento_full
.\rodar_sku_custo.ps1           # a cada 5 min: custo por depósito

python -m pipelines.preparar clientes             # uma etapa só
python -m pipelines.faturamento_horario --status  # relata sem escrever
python -m pytest tests/ -v                        # testes
```

## O que roda quando

| Tarefa Agendada | Frequência | O que atualiza |
|---|---|---|
| `rodar_faturamento_horario.ps1` | 1 hora | `Faturamento` + `faturamento_full` |
| `rodar_etl.ps1` | 1 dia | `Clientes`, `Itens`, `ItensExtra*` |
| `rodar_sku_custo.ps1` | 5 min | `SkuCustoCdGiba` |

O faturamento **não** está no diário — se as duas cargas rodassem, disputariam
a mesma tabela. O `upload.py` pula a pasta `Faturamento` por padrão
(`PASTAS_DE_OUTRO_PIPELINE`).

Os dois pipelines frequentes comparam o hash da origem antes de trabalhar: sem
mudança, saem calados. E usam lock, então se uma carga demorar mais que o
intervalo, a rodada seguinte é pulada em vez de rodar em paralelo.

### Carga incremental do faturamento

A origem publica o CSV inteiro (100 MB, 256 mil linhas) toda hora, mas o que
muda é quase só o mês corrente — 1,3% do total. Reprocessar tudo pra atualizar
isso seria desperdício.

O pipeline converte o CSV tratado num Parquet particionado por mês e carrega no
MySQL só os **2 últimos meses**. Dois em vez de um por causa de nota
retroativa: se a origem lançar hoje uma nota com emissão do mês passado, uma
janela de 1 mês não a pegaria.

| | antes | agora |
|---|---|---|
| leitura | 11s (CSV 100 MB) | 0,1s (Parquet 16 MB) |
| linhas carregadas | 256.352 | 11.315 |
| INSERT | ~45s | ~3s |
| ciclo completo | ~140s | **~76s** |

O `faturamento_full` continua sendo refeito **inteiro** (46s do ciclo). Não dá
pra fazer só a janela: a ilha do cliente vem da última compra da marca foco em
todo o histórico, então quem comprou há meses apareceria como `outros`.

Para recarregar o histórico completo (depois de corrigir dado antigo, por
exemplo): `python -m pipelines.faturamento_horario --tudo`.

---

## O que mudou, e por quê

### 3 correções críticas

**1. Duplicidade permanente no `date_range`** — `src/load/strategies.py`

O original avisava sobre datas ilegíveis e as inseria assim mesmo. O `DELETE`
da estratégia filtra por `STR_TO_DATE`, que devolve `NULL` nessas linhas, e
`NULL BETWEEN x AND y` nunca é verdadeiro — então elas entravam no banco e
**nenhuma carga futura conseguia removê-las**. Cada rodada somava outra cópia.
Agora a carga aborta com a lista dos valores problemáticos.

**2. Backup de arquivo que não carregou** — `pipelines/upload.py`

O original movia o arquivo para o `_backup` mesmo quando a leitura falhava
(`upload_file` devolvia `0` sem levantar exceção). O dado do dia sumia da pasta
de entrada sem nunca ter entrado no banco. Aconteceu em 05/08/2026
(`File is not a zip file`). Agora o backup só ocorre após commit bem-sucedido;
o que falha permanece na entrada para nova tentativa.

**3. Falha silenciosa no exit code** — `pipelines/upload.py`

O original logava o erro por arquivo e saía com código 0. O `rodar_etl.ps1` só
checa `$LASTEXITCODE`, então registrava **sucesso** enquanto arquivos falhavam.
Foi o que escondeu a quebra de 01 a 03/08/2026 — três dias seguidos. Agora
qualquer falha propaga para o exit code.

### Correção adicional (encontrada nos dados reais)

**Acento no nome da pasta corrompia o nome da tabela.** A pasta de produção
`tabela-preço-promocao` gerava `` `TabelaPreOPromocao` `` — o split em
`[^a-zA-Z0-9]` tratava o `ç` como separador. Agora translitera antes de separar.

### Reorganização

| Antes | Depois |
|---|---|
| 4 implementações divergentes de leitura de CSV | 1 em `src/io/readers.py` |
| Caminho de rede hardcoded em 6 arquivos | `config/settings.py` |
| Transformação executada no import do módulo | funções puras + `main()` |
| SQL por concatenação de string | parâmetros + validação de identificador |
| `except Exception: pass` | erro com causa e sugestão |
| Porta padrão 8080 em 2 arquivos, 3306 em 2 | 3306 em todos |
| `.gitignore` com `*.txt` engolia `requirements.txt` | corrigido |
| 0 testes | 114 testes |

---

## As 15 cicatrizes foram preservadas

Todo trecho defensivo do original migrou intacto — cada um previne uma falha
real e documentada. Os testes marcados `CICATRIZ` reproduzem o incidente:

- caracteres de controle ilegais no XML do SAP quebrando o openpyxl
- header técnico virando português (clientes 01/07, itens 23/07)
- coluna opcional sumindo da origem sem aviso (23/07)
- vendedor duplicado multiplicando linhas de cliente no LEFT JOIN
- decimais BR sem aspas (`399,89`) quebrando o split por vírgula
- exportação alternando entre UTF-8 e UTF-16
- typo na origem (`confins` por `cofins`)
- origem alternando entre `1.399,90` e `759.90`
- separador vírgula lido como `;` → base parada 3 dias (01–03/08)
- `NaN` serializado como literal `nan` no INSERT
- `STR_TO_DATE` abortando query em vez de devolver NULL
- publicar tabela sem custo derrubando a MCB para fallback silencioso
- arquivo vazio substituindo tabela boa
- parcela vazia zerando a MC inteira por NULL propagation
- limite de ~196 colunas LONGTEXT forçando o fatiamento de itens

---

## Verificação executada

- **114 testes passam** (`python -m pytest tests/`)
- **Regressão confirmada**: revertendo a correção 1, 2 testes falham; restaurada,
  todos passam — os testes checam comportamento, não passam por acidente
- **Os 8 arquivos reais de produção leem corretamente**, incluindo o
  `estoquePorDepositoCustoVPS.csv` separado por vírgula, que o código antigo
  transformava numa coluna única
- **`Base NFs.csv` real** (255.379 linhas): nenhuma data ilegível hoje; janela
  02/01/2024 → 11/08/2026 — a correção 1 não bloquearia a carga atual

## Carga validada contra o banco

Faturamento, Clientes, Itens e faturamento_full já foram carregados e
conferidos contra uma baseline tirada antes:

| Tabela | Antes | Depois |
|---|---|---|
| Faturamento | 254.390 | 255.439 |
| Clientes | 14.581 | 14.599 |
| Itens | 2.901 | 2.922 |
| faturamento_full | 254.390 | 255.439 |

Estrutura idêntica nas quatro (mesmas colunas), `mc` batendo com a fórmula em
255.439 linhas sem nenhum NULL, e a view `ItensCompleto` respondendo.

## Pontos em aberto

- Tipagem do destino (`LONGTEXT` para tudo) continua como está. Ver a seção 6 do
  `RELATORIO.md`: mudar quebraria o Forecast em silêncio.
- `ETL/mcb/` do projeto antigo não foi portado — são scripts manuais, fora do
  fluxo agendado.
- O usuário do banco não tem `SESSION_VARIABLES_ADMIN`. O código lida com isso,
  mas vale saber ao criar tabela nova com muitas colunas.
