# Lancador do ETL incremental da SkuCustoCdGiba (custo/estoque por deposito).
# Chamado pela Tarefa Agendada "ETL - SkuCustoCdGiba 5min".
# Somente ASCII: o PowerShell 5.1 le .ps1 como ANSI e acentos quebram o parser.

$ErrorActionPreference = 'Continue'
$BASE   = $PSScriptRoot
$PYTHON = "$BASE\.venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path $PYTHON)) { $PYTHON = "python" }
Set-Location $BASE

# O script decide sozinho se ha o que fazer (compara hash do CSV) e fica quieto
# quando nada mudou -- por isso pode rodar de 5 em 5 minutos sem encher o log.
& $PYTHON -m pipelines.sku_custo
exit $LASTEXITCODE
