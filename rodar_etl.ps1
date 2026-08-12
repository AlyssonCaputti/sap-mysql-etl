# Orquestrador do ETL diario.
# Somente ASCII: o PowerShell 5.1 le .ps1 como ANSI e acentos quebram o parser.
#
# Cada etapa aborta o pipeline se falhar. O upload devolve exit code != 0
# quando QUALQUER arquivo falha -- sem isso o log registra sucesso enquanto
# arquivos quebram, e a falha passa dias sem ninguem perceber.

$ErrorActionPreference = 'Continue'
$BASE   = $PSScriptRoot
$PYTHON = "$BASE\.venv\Scripts\python.exe"
$LOG    = "$BASE\logs\orquestrador.log"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path "$BASE\logs")) { New-Item -ItemType Directory -Path "$BASE\logs" | Out-Null }
if (-not (Test-Path $PYTHON)) { $PYTHON = "python" }

Set-Location $BASE

function Log($msg) {
    $linha = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $linha
    Add-Content -Path $LOG -Value $linha -Encoding UTF8
}

function Etapa($nome, $modulo) {
    Log "--- $nome ---"
    & $PYTHON -m $modulo
    if ($LASTEXITCODE -ne 0) {
        Log "ERRO em $nome (exit $LASTEXITCODE) - abortando."
        exit 1
    }
}

Log "========================================="
Log "ETL diario iniciando..."

Etapa "preparar"        "pipelines.preparar"
Etapa "upload"          "pipelines.upload"
Etapa "faturamento_full" "pipelines.faturamento_full"

Log "ETL diario concluido com sucesso."
Log "========================================="
