$env:PATH = "C:\mingw32\bin;$env:PATH"
Set-Location $PSScriptRoot
g++ -shared -o dll/dbvbot.dll dll/dbvbot.cpp -lkernel32 -luser32 -static -s -O2 -std=c++17
if ($LASTEXITCODE -eq 0) { Write-Host "BUILD OK" -ForegroundColor Green } else { Write-Host "BUILD FAILED" -ForegroundColor Red }
