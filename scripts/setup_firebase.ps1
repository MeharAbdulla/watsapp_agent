# Firebase setup helper for WhatsApp Agent SaaS
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host ""
Write-Host "=== Firebase setup ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Open https://console.firebase.google.com/"
Write-Host "2. Create a project (or select existing)"
Write-Host "3. Build -> Firestore Database -> Create database (Production or Test mode)"
Write-Host "4. Project settings (gear) -> Service accounts -> Generate new private key"
Write-Host "5. Save the downloaded JSON as:"
Write-Host "   $Root\firebase-service-account.json" -ForegroundColor Green
Write-Host ""
Write-Host "6. Edit .env and set FIREBASE_PROJECT_ID to your project id"
Write-Host "   (same as project_id inside the JSON file)"
Write-Host ""

$jsonPath = Join-Path $Root "firebase-service-account.json"
if (-not (Test-Path $jsonPath)) {
    Write-Host "[!] firebase-service-account.json not found yet." -ForegroundColor Yellow
    exit 1
}

$json = Get-Content $jsonPath -Raw | ConvertFrom-Json
$projectId = $json.project_id
Write-Host "Found project_id in JSON: $projectId" -ForegroundColor Green

$envPath = Join-Path $Root ".env"
$content = Get-Content $envPath -Raw -ErrorAction SilentlyContinue
if ($content -notmatch "FIREBASE_PROJECT_ID=") {
    Add-Content $envPath "`nFIREBASE_PROJECT_ID=$projectId"
} else {
    $content = $content -replace "FIREBASE_PROJECT_ID=.*", "FIREBASE_PROJECT_ID=$projectId"
    Set-Content $envPath $content -NoNewline
}
Write-Host "Updated .env FIREBASE_PROJECT_ID=$projectId"

Write-Host ""
Write-Host "Testing connection..."
py scripts\check_firebase.py
