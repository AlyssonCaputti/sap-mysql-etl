# Resumo diario do ETL por e-mail. Chamado pela Tarefa Agendada
# "ETL-Resumo-Diario-11h", todo dia as 11:00.
# Somente ASCII: o PowerShell 5.1 le .ps1 como ANSI e acentos quebram o parser.
#
# A janela do relatorio e 11h->11h (ultimas 24h), nao o dia civil: as 11h a
# rodada das 15h de hoje ainda nao aconteceu, entao o que interessa e a de
# ontem. Ver o docstring de pipelines/resumo_diario.py.

$ErrorActionPreference = 'Continue'
$BASE   = $PSScriptRoot
$PYTHON = "$BASE\.venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"

# Nao ha .venv nesta maquina (04/09/2026): as dependencias (pandas, mysql-
# connector, dotenv) estao no Python 3.14 do usuario, que esta no PATH DE
# USUARIO -- e por isso que o fallback "python" resolve certo na Tarefa
# Agendada, que roda como alysson.farias. Mesmo arranjo das outras tarefas.
# Rodando na mao de um shell com outro venv ativo, o "python" seria o do venv
# ativo; nesse caso chame o interpretador pelo caminho completo.
if (-not (Test-Path $PYTHON)) { $PYTHON = "python" }
Set-Location $BASE

& $PYTHON -m pipelines.resumo_diario
exit $LASTEXITCODE
