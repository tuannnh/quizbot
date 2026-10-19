# QuizBot agent installer for Windows.
# Run from the agent folder:  powershell -ExecutionPolicy Bypass -File .\install_windows.ps1
# Uninstall:                  powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 -Uninstall
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv    = Join-Path $here ".venv"
$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "QuizBot Agent.lnk"

if ($Uninstall) {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*quizbot*agent.py*" -or $_.CommandLine -like "*$here*agent.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Remove-Item $lnkPath -ErrorAction SilentlyContinue
    Write-Host "QuizBot agent removed from startup."
    exit 0
}

if (-not (Get-Command py -ErrorAction SilentlyContinue) -and -not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3.10+ is required. Install it from python.org (tick 'Add to PATH') and re-run."
}
$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }

if (-not (Test-Path $venv)) { & $py -m venv $venv }
& "$venv\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
& "$venv\Scripts\python.exe" -m pip install -r (Join-Path $here "requirements.txt")

$envFile = Join-Path $here "quizbot.env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $here "quizbot.env.example") $envFile
    Write-Host "Created quizbot.env - edit QUIZBOT_SERVER and QUIZBOT_TOKEN, then re-run this script." -ForegroundColor Yellow
    notepad $envFile
    exit 0
}

# Start at login, no console window (pythonw).
$ws  = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath       = "$venv\Scripts\pythonw.exe"
$lnk.Arguments        = "`"$here\agent.py`""
$lnk.WorkingDirectory = $here
$lnk.Description      = "QuizBot agent (Left Shift + `` to capture)"
$lnk.Save()

Start-Process -FilePath "$venv\Scripts\pythonw.exe" -ArgumentList "`"$here\agent.py`"" -WorkingDirectory $here
Write-Host "QuizBot agent installed and running. It starts automatically at login."
Write-Host "Log file: $here\agent.log"
