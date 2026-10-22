# Levanta n8n para ArgStats. Las rutas salen de la ubicación de este script:
#   data\     base de n8n (workflows, usuario, credenciales)
#   project\  scripts de Python y workflows exportados
#   powershell -ExecutionPolicy Bypass -File .\start-n8n.ps1

$root = $PSScriptRoot

$env:N8N_USER_FOLDER = Join-Path $root "data"

# Los nodos Execute Command de los workflows usan %ARGSTATS_INGEST_DIR%
$env:ARGSTATS_INGEST_DIR = Join-Path $root "project"

$env:GENERIC_TIMEZONE = "America/Argentina/Buenos_Aires"

# n8n 2.x deshabilita Execute Command por defecto; los workflows lo necesitan
$env:NODES_EXCLUDE = "[]"

Set-Location $root

n8n start
