# Atualiza Faturamento + faturamento_full de hora em hora.
# Chamado pela Tarefa Agendada "ETL - Faturamento 1h".
# Somente ASCII: o PowerShell 5.1 le .ps1 como ANSI e acentos quebram o parser.

$ErrorActionPreference = 'Continue'
$BASE   = $PSScriptRoot
$PYTHON = "$BASE\.venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path $PYTHON)) { $PYTHON = "python" }
Set-Location $BASE

# O script compara o hash da origem e sai calado quando nada mudou, entao
# pode rodar de hora em hora sem encher o log. Se ainda houver uma carga em
# andamento, ele pula a rodada.
& $PYTHON -m pipelines.faturamento_horario
exit $LASTEXITCODE
