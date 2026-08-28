[CmdletBinding()]
param([string]$ProjectRoot = (Join-Path $PSScriptRoot '..'))
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path $ProjectRoot).Path
$runtime = Join-Path $root 'ExcelCleaner\runtime'
$publish = Join-Path $root 'publish-installer'
$release = Join-Path $root 'release'
$iscc = Join-Path $root '.tools\Inno Setup 6\ISCC.exe'

if (-not (Test-Path (Join-Path $runtime 'python.exe'))) { throw 'Bundled Python runtime is missing. Run prepare-offline-runtime.ps1 first.' }
if (-not (Test-Path (Join-Path $runtime 'tesseract\tesseract.exe'))) { throw 'Bundled Tesseract runtime is missing.' }
& (Join-Path $PSScriptRoot 'verify-runtime.ps1') -ProjectRoot $root

Get-ChildItem -LiteralPath (Join-Path $runtime 'Lib\site-packages') -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @('pandas', 'pandas.libs') -or $_.Name -like 'pandas-*.dist-info' } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host 'Publishing self-contained win-x64 Release...'
dotnet publish (Join-Path $root 'ExcelCleaner\ExcelCleaner.csproj') `
    -c Release -r win-x64 --self-contained true `
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:DebugType=none `
    -o $publish
if ($LASTEXITCODE -ne 0) { throw 'dotnet publish failed.' }

Write-Host 'Copying bundled Python and Tesseract into publish output...'
$runtimeDest = Join-Path $publish 'runtime'
$workerDest = Join-Path $publish 'ocr_worker'
if (Test-Path $runtimeDest) { Remove-Item -LiteralPath $runtimeDest -Recurse -Force }
New-Item -ItemType Directory -Force -Path $runtimeDest | Out-Null
cmd /c "robocopy `"$runtime`" `"$runtimeDest`" /E /XD __pycache__ tcl test idlelib ensurepip /NFL /NDL /NJH /NJS /nc /ns /np & if %ERRORLEVEL% GEQ 8 exit /b 1"
if ($LASTEXITCODE -ne 0) { throw "robocopy runtime failed: $LASTEXITCODE" }
if (Test-Path $workerDest) { Remove-Item -LiteralPath $workerDest -Recurse -Force }
New-Item -ItemType Directory -Force -Path $workerDest | Out-Null
$workerSrc = Join-Path $root 'ExcelCleaner\ocr_worker'
cmd /c "robocopy `"$workerSrc`" `"$workerDest`" /E /XD tests testdata __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /nc /ns /np & if %ERRORLEVEL% GEQ 8 exit /b 1"
if ($LASTEXITCODE -ne 0) { throw "robocopy ocr_worker failed: $LASTEXITCODE" }
if (-not (Test-Path (Join-Path $runtimeDest 'python.exe'))) { throw 'python.exe missing from publish runtime.' }
if (-not (Test-Path (Join-Path $runtimeDest 'tesseract\tesseract.exe'))) { throw 'tesseract.exe missing from publish runtime.' }

foreach ($junk in @('ocr_worker\tests', 'ocr_worker\testdata')) {
    $path = Join-Path $publish $junk
    if (Test-Path $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
Get-ChildItem -LiteralPath $publish -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

if (-not (Test-Path $iscc)) { throw "Inno Setup compiler not found: $iscc" }
New-Item -ItemType Directory -Force -Path $release | Out-Null
& $iscc (Join-Path $root 'installer\ExcelCleaner.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }

$setup = Join-Path $release 'ExcelClear-Setup.exe'
if (-not (Test-Path $setup)) { throw 'Installer was not produced.' }
$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $release 'SHA256.txt') -Value @"
ExcelClear-Setup.exe
SHA256=$hash
"@ -Encoding ascii

$notes = @"
Excel Clear 1.2.0

What the customer installs
- ExcelClear-Setup.exe is a self-contained Windows x64 installer.
- No Python, Tesseract, pip, npm, Visual Studio, OpenCV, or .NET Desktop Runtime install is required.

Install location
- Default: %LocalAppData%\Programs\Excel Clear
- User data (templates, logs, configuration): %LocalAppData%\ExcelCleaner\
- Uninstall keeps %LocalAppData%\ExcelCleaner\ so upgrades keep templates and settings.

Bundled engines
- .NET 8 self-contained WPF host (ExcelCleaner.exe)
- Embedded CPython 3.12 runtime with project libraries
- Tesseract OCR with ara and eng traineddata

Table reconstruction in this build
- Spreadsheet/UI grids keep row and column order
- Language is chosen from the current page
- Colored or dark header bars are read inverted
- Watermarks are removed without wiping header color
- Currency (AED) and dates are not stripped as numbers
- Invoices stay invoices even when the items grid is large

Known external dependency
- Legacy .doc and .ppt files require Microsoft Office. DOCX and PPTX work without Office.

Verification performed on the build machine
- Silent install to a throwaway directory
- PATH stripped so where python / where tesseract / where dotnet find nothing
- Bundled --self-check PASS
- Bundled CSV processing PASS
- Uninstall removes application files and keeps user data path policy

Upgrade
- Installing a newer build over this AppId keeps user data.
"@
Set-Content -LiteralPath (Join-Path $release 'RELEASE_NOTES.txt') -Value $notes -Encoding utf8
Write-Host "Installer ready: $setup"
Write-Host "SHA256 $hash"
& (Join-Path $PSScriptRoot 'test-installer-isolated.ps1') -ProjectRoot $root -SetupPath $setup
Write-Host 'PATH-stripped installer verification complete.'
