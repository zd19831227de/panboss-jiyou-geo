# Restart the GEO backend (uvicorn :18770) fully detached from the calling shell.
# Only kills the uvicorn process itself; publish browsers are detached Chrome
# processes and are never touched. WMI Create survives the originating session.
$ErrorActionPreference = 'Stop'

$backendDir = Join-Path $PSScriptRoot '..\backend'
$py = Join-Path $backendDir '.venv\Scripts\python.exe'

# find pids listening on 18770 (backend only)
$conns = Get-NetTCPConnection -LocalPort 18770 -State Listen -ErrorAction SilentlyContinue
$pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $pids) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$p"
    if ($proc.CommandLine -match 'uvicorn') {
        Write-Host "stopping backend pid $p"
        Stop-Process -Id $p -Force
    }
}
Start-Sleep -Seconds 2

$q = [char]34
$cmdLine = "$q$py$q -m uvicorn app.main:app --port 18770 --host 127.0.0.1"
Write-Host "launching: $cmdLine"
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine      = $cmdLine
    CurrentDirectory = $backendDir
}
if ($result.ReturnValue -ne 0) {
    throw "WMI create failed: $($result.ReturnValue)"
}
Write-Host "backend started pid $($result.ProcessId)"
