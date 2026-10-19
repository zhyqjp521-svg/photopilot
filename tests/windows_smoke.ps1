$ErrorActionPreference = 'Stop'

$sample = Join-Path $env:RUNNER_TEMP 'photopilot-smoke'
$export = Join-Path $sample 'export'
New-Item -ItemType Directory -Force -Path $sample | Out-Null
python -c "from PIL import Image; Image.new('RGB', (1600, 1000), (35, 120, 205)).save(r'$sample\import-test.jpg')"

$json = Join-Path $sample 'report.json'
$process = Start-Process -FilePath .\dist\PhotoPilot.exe `
  -ArgumentList @('cull', $sample, '--no-faces', '--json', $json) `
  -PassThru -Wait
if ($process.ExitCode -ne 0) { throw "Packaged EXE cull exited with $($process.ExitCode)" }
if (-not (Test-Path $json)) { throw 'Packaged EXE did not create its JSON report' }
$report = Get-Content $json -Raw | ConvertFrom-Json
if ($report.photos.Count -ne 1) { throw "Expected one imported photo, got $($report.photos.Count)" }
if (-not (Test-Path (Join-Path $sample 'import-test.jpg'))) {
  throw 'Smoke fixture was unexpectedly removed'
}

$process = Start-Process -FilePath .\dist\PhotoPilot.exe `
  -ArgumentList @('run', $sample, '--out', $export, '--no-faces', '--no-sheet') `
  -PassThru -Wait
if ($process.ExitCode -ne 0) { throw "Packaged EXE export exited with $($process.ExitCode)" }
$products = @(Get-ChildItem $export -File | Where-Object { $_.Extension -in '.jpg', '.jpeg' })
if ($products.Count -ne 1) { throw "Expected one exported JPEG, got $($products.Count)" }
if ($products[0].Length -le 10000) { throw 'Exported JPEG is unexpectedly small' }

@{
  source = $sample
  image = (Join-Path $sample 'import-test.jpg')
  report = $json
  export = $products[0].FullName
  export_bytes = $products[0].Length
  exe_sha256 = (Get-FileHash .\dist\PhotoPilot.exe -Algorithm SHA256).Hash
} | ConvertTo-Json | Set-Content (Join-Path $sample 'smoke.json') -Encoding utf8
Get-Content (Join-Path $sample 'smoke.json')
