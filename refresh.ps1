$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m ci refresh --feed all }
else { python -m ci refresh --feed all }
if ($LASTEXITCODE -ne 0) {
    throw 'A refresh failed. See Data Health for the source error.'
}
