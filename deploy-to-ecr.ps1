# deploy-to-ecr.ps1 - log in to ECR, build the image and push it.
#
#   .\deploy-to-ecr.ps1                      uses your local AWS CLI credentials
#   .\deploy-to-ecr.ps1 -EcrToken "..."      uses a token from CloudShell instead
#
# Windows Credential Manager cannot store a credential larger than about 2.5 KB,
# and an ECR token is bigger, which is why docker login fails with
# "The stub received bad data". This script temporarily takes credsStore out of
# ~/.docker/config.json, pushes, then puts the file back exactly as it was.

param([string]$EcrToken = "")

$ErrorActionPreference = "Stop"
$Registry = "833547518781.dkr.ecr.ap-southeast-2.amazonaws.com"
$Image    = "internal-docs-rag"
$Region   = "ap-southeast-2"
$Target   = "$Registry/$Image" + ":latest"
$Instance = "i-06e797c0cc9c117e4"
$Config   = Join-Path $env:USERPROFILE ".docker\config.json"
$Backup   = "$Config.claude-backup"

function Step($n, $text) { Write-Host ""; Write-Host "[$n] $text" -ForegroundColor Cyan }
function Note($text) { Write-Host "    $text" -ForegroundColor Green }
function Fail($text) { Write-Host ""; Write-Host "FAILED: $text" -ForegroundColor Red; exit 1 }

Step 1 "Checking Docker is running"
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "Docker is not running. Start Docker Desktop and wait for the whale icon to settle." }
Note "Docker is up."

$restoreNeeded = $false
try {
    Step 2 "Working around the Windows credential store limit"
    if (Test-Path $Config) {
        Copy-Item $Config $Backup -Force
        $restoreNeeded = $true
        $json = Get-Content $Config -Raw | ConvertFrom-Json
        if ($json.PSObject.Properties.Name -contains "credsStore") {
            $json.PSObject.Properties.Remove("credsStore")
            $json | ConvertTo-Json -Depth 10 | Set-Content $Config -Encoding ascii
            Note "credsStore removed for this run (backup at config.json.claude-backup)."
        } else {
            Note "No credsStore set; nothing to change."
        }
    } else {
        Note "No docker config yet; nothing to change."
    }

    Step 3 "Logging in to ECR"
    if ($EcrToken -ne "") {
        $EcrToken | docker login --username AWS --password-stdin $Registry
    } else {
        aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $Registry
    }
    if ($LASTEXITCODE -ne 0) { Fail "ECR login still failed. Send me the exact message." }
    Note "Logged in."

    Step 4 "Building the image for linux/amd64"
    docker build --platform linux/amd64 -t $Image .
    if ($LASTEXITCODE -ne 0) { Fail "The build failed. Send me the error above." }
    Note "Built."

    Step 5 "Tagging and pushing"
    docker tag "$Image`:latest" $Target
    docker push $Target
    if ($LASTEXITCODE -ne 0) { Fail "The push failed. Send me the error above." }
}
finally {
    if ($restoreNeeded -and (Test-Path $Backup)) {
        Copy-Item $Backup $Config -Force
        Remove-Item $Backup -Force
        Write-Host ""
        Write-Host "    Your docker config has been restored." -ForegroundColor DarkGray
    }
}

Step 6 "Telling the server to pull the new image"
$cmdId = aws ssm send-command --region $Region --instance-ids $Instance `
    --document-name AWS-RunShellScript `
    --parameters 'commands=["systemctl restart internal-docs-rag"]' `
    --query "Command.CommandId" --output text
if ($LASTEXITCODE -ne 0) {
    Write-Host "    Could not reach the instance. Restart it yourself with:" -ForegroundColor Yellow
    Write-Host "    aws ssm send-command --region $Region --instance-ids $Instance --document-name AWS-RunShellScript --parameters 'commands=[\"systemctl restart internal-docs-rag\"]'" -ForegroundColor White
    exit 1
}
Note "Restart sent (command $cmdId). Waiting for the new container..."

$ok = $false
foreach ($i in 1..20) {
    Start-Sleep -Seconds 6
    try {
        $r = Invoke-WebRequest -Uri "http://13.239.26.128/health" -TimeoutSec 5 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
    Write-Host "    still starting ($($i * 6)s)..." -ForegroundColor DarkGray
}

Write-Host ""
if ($ok) {
    Write-Host "DEPLOYED. http://13.239.26.128/health is answering 200." -ForegroundColor Green
} else {
    Write-Host "Image pushed and restart sent, but /health did not answer within 2 minutes." -ForegroundColor Yellow
    Write-Host "Tell Claude and it will read the logs off the instance." -ForegroundColor Yellow
}
