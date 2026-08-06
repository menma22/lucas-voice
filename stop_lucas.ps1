# Stop all lucas_voice.py listener processes (python / pythonw).
# ASCII only - no BOM/encoding pitfalls with PowerShell 5.1.
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like 'python*' -and $_.CommandLine -like '*lucas_voice.py*'
}
if (-not $procs) {
    Write-Host "no listener running"
} else {
    $procs | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force
        Write-Host ("stopped " + $_.Name + " pid " + $_.ProcessId)
    }
}
