$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (Get-Command py -ErrorAction SilentlyContinue) {
    $launcher = 'py'
    $launcherArgs = @('-3')
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $launcher = 'python'
    $launcherArgs = @()
}
else {
    throw 'Install Python 3.11 or later and run this script again.'
}
& $launcher @launcherArgs -m ci seed-known
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize prior research.' }
& $launcher @launcherArgs -m ci serve
exit $LASTEXITCODE
