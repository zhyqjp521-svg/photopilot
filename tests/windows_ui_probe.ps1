$ErrorActionPreference = 'Stop'

$photo = $env:PHOTOPILOT_SMOKE_PHOTO
$out = $env:PHOTOPILOT_SMOKE_OUT
$port = [int]$env:PHOTOPILOT_SMOKE_PORT
$serverOut = Join-Path $out 'ui-server-stdout.log'
$serverErr = Join-Path $out 'ui-server-stderr.log'
$server = Start-Process -FilePath .\dist\PhotoPilot.exe `
  -ArgumentList @('ui', '--port', $port, '--no-browser') `
  -RedirectStandardOutput $serverOut -RedirectStandardError $serverErr `
  -PassThru
try {
  $probeArgs = @('./tests/windows_ui_smoke.py', '--photo', $photo,
                 '--port', $port, '--output', (Join-Path $out 'ui-smoke.json'))
  & python @probeArgs
  if ($LASTEXITCODE -ne 0) { throw "Packaged UI import probe exited with $LASTEXITCODE" }
  $result = Get-Content (Join-Path $out 'ui-smoke.json') -Raw | ConvertFrom-Json
  if (-not $result.ok) { throw 'Packaged UI import probe did not report success' }
  Get-Content (Join-Path $out 'ui-smoke.json')
}
finally {
  if (-not $server.HasExited) {
    Stop-Process -Id $server.Id -Force
    $server.WaitForExit()
  }
  if (Test-Path $serverErr) { Get-Content $serverErr }
}
