# Build the Windows zip.  Run from the repository root:  .\packaging\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$version = (Select-String -Path pyproject.toml -Pattern 'version = "([^"]+)"').Matches[0].Groups[1].Value
$app = "dist\N6705C-SEQ"
$zip = "dist\N6705C-SEQ-$version-windows-x64.zip"

pyinstaller packaging\n6705c_seq.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }

# A build can import everything and still miss a backend; run the real path (simulator).
# Resolve to an absolute path first: the `&` call operator can misresolve a relative
# exe path when the working directory is a UNC path (e.g. a mapped \\server\share).
$cli = (Resolve-Path "$app\n6705c-seq-cli.exe").Path
& $cli run examples\powerup.csv --demo --yes
if ($LASTEXITCODE -ne 0) { throw "self-test failed" }

$web = (Resolve-Path "$app\N6705C-SEQ-web.exe").Path
$p = Start-Process -FilePath $web -ArgumentList --selftest -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "web self-test failed" }

Copy-Item examples -Destination $app -Recurse
Copy-Item README.md -Destination $app
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $app -DestinationPath $zip
Write-Host "done: $zip"
