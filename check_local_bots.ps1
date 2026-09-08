# ReneWise Local Bot Instance Checker
# Run this to check if any local bot instances are running

Write-Host "Checking for local ReneWise bot instances..." -ForegroundColor Cyan
Write-Host ""

# Check for Python processes
$pythonProcesses = Get-Process python* -ErrorAction SilentlyContinue

if ($pythonProcesses) {
    Write-Host "Found Python processes:" -ForegroundColor Yellow
    
    foreach ($proc in $pythonProcesses) {
        $cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)").CommandLine
        
        # Check if it's a ReneWise bot process
        if ($cmdLine -match "run\.py|bot\.py|renewise") {
            Write-Host "  [!] ReneWise bot detected!" -ForegroundColor Red
            Write-Host "      PID: $($proc.Id)" -ForegroundColor Red
            Write-Host "      Started: $($proc.StartTime)" -ForegroundColor Red
            Write-Host ""
            Write-Host "      To kill: Stop-Process -Id $($proc.Id) -Force" -ForegroundColor Yellow
        }
        elseif ($cmdLine -match "jedi|language-server") {
            Write-Host "  [OK] IDE language server (PID: $($proc.Id))" -ForegroundColor Gray
        }
    }
}
else {
    Write-Host "[OK] No Python processes running" -ForegroundColor Green
}

Write-Host ""
Write-Host "Checking network connections to Telegram..." -ForegroundColor Cyan

# Check for listening ports that might be the bot
$listeningPorts = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
    $_.LocalPort -in @(8000, 8080, 5000, 3000)
}

if ($listeningPorts) {
    Write-Host "[!] Found processes listening on common bot ports:" -ForegroundColor Yellow
    foreach ($port in $listeningPorts) {
        $proc = Get-Process -Id $port.OwningProcess -ErrorAction SilentlyContinue
        Write-Host "  Port $($port.LocalPort): $($proc.ProcessName) (PID: $($port.OwningProcess))" -ForegroundColor Yellow
    }
}

Write-Host ""
if (Test-Path .env) {
    Write-Host "[!] Warning: .env file exists locally" -ForegroundColor Yellow
    Write-Host "    If you run 'python run.py' by accident, it will conflict with Render!" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Check complete!" -ForegroundColor Green
