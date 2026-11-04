# Run on Windows PowerShell after installing Git and GitHub CLI (gh).
# The script creates a PRIVATE repository and pushes only tracked, non-secret files.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Install Git first.' }
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'Install GitHub CLI (gh) first.' }
& gh auth status
if ($LASTEXITCODE -ne 0) { throw 'Run gh auth login first.' }
if (-not (Test-Path '.git')) {
  & git init -b main
  if ($LASTEXITCODE -ne 0) { throw 'git init failed.' }
}
& git add README.md .gitignore .env.example requirements.txt usernames.txt username_monitor deploy tests .github publish-to-github.ps1 LICENSE
if ($LASTEXITCODE -ne 0) { throw 'git add failed.' }
& git commit -m 'feat: lightweight Telegram username monitor MVP'
if ($LASTEXITCODE -ne 0) { throw 'git commit failed. Set git user.name and user.email.' }
& gh repo create lxstjxck/TelegramUsernameMonitor --private --source . --remote origin --push
if ($LASTEXITCODE -ne 0) { throw 'Repository creation failed; inspect gh output.' }
Write-Host 'Repository: https://github.com/lxstjxck/TelegramUsernameMonitor'
