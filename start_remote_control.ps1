# ============================================================
# Claude Code 원격제어 세션 런처 (2026-08-02 신규)
#
# 목적: 매일 08:59 작업 스케줄러가 봇을 기동할 때, 별도 창으로
#       `claude --remote-control` 세션도 같이 띄워서 **모바일에서
#       장중 상태를 확인**할 수 있게 한다.
#
# 왜 .bat이 아니라 .ps1인가:
#   프로젝트 실제 경로에 한글("문서")이 들어 있어서 cmd.exe가 이 경로
#   문자열을 코드페이지 문제로 깨뜨린다(2026-07-28 실장애 원인). PowerShell은
#   UTF-8 소스를 안정적으로 읽으므로 한글 경로를 여기에 두는 게 안전하다.
#   start_trader.bat 쪽에는 ASCII junction 경로(C:\AutoTrader_Bot\ProjectRoot)만
#   남겨서 cmd.exe가 한글을 만나지 않게 한다.
#
# 왜 junction이 아니라 실제 경로로 cd 하는가:
#   claude CLI의 "폴더 신뢰" 승인이 실제 경로 기준으로 저장돼 있다.
#   junction 경로로 시작하면 다른 폴더로 보고 신뢰 프롬프트를 다시 띄울 수
#   있는데, 무인 기동에서 프롬프트가 뜨면 세션이 거기서 멈춰버린다.
# ============================================================

$ErrorActionPreference = "Continue"

$ProjectPath = "C:\Users\rober\OneDrive\문서\vscode\AutoTrader_Project"
$LogFile     = "C:\AutoTrader_Bot\scheduler_debug.log"

# 로그는 ASCII로만 쓴다. scheduler_debug.log는 cmd.exe(start_trader.bat)가
# 이미 ASCII로 쓰고 있는 파일이라, 여기서 한글(UTF-8)을 섞으면 한 파일에 두
# 인코딩이 공존해 나중에 읽을 때 전부 깨져 보인다(실제로 겪음).
# 화면 출력은 한글 그대로 두고, 파일에 남기는 것만 ASCII로 분리한다.
function Write-Log($msgAscii, $msgKo = $null) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host ("[{0}] {1}" -f $stamp, $(if ($msgKo) { $msgKo } else { $msgAscii }))
    try { Add-Content -Path $LogFile -Value ("[{0}] [rc] {1}" -f $stamp, $msgAscii) -Encoding ascii } catch { }
}

Write-Log "remote-control launcher started" "원격제어 세션 런처 시작"

# 작업 스케줄러가 띄운 프로세스는 PATH가 오래된 값일 수 있다(claude는
# 2026-08-01에 설치돼 User PATH에 추가됨). 명시적으로 갱신해서 확실히 잡는다.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path", "User")

$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    Write-Log "ERROR: claude CLI not found on PATH" "!! claude CLI를 PATH에서 찾지 못함"
    Write-Log "  fix: npm install -g @anthropic-ai/claude-code" "   확인: npm install -g @anthropic-ai/claude-code"
    Write-Host ""
    Write-Host "아무 키나 누르면 창을 닫습니다..." -ForegroundColor Yellow
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}
Write-Log ("claude found: " + $claude.Source)

if (-not (Test-Path $ProjectPath)) {
    Write-Log "ERROR: project path not found: $ProjectPath" "!! 프로젝트 경로 없음: $ProjectPath"
    exit 1
}
Set-Location $ProjectPath
Write-Log "workdir set (project root)" "작업 폴더: $ProjectPath"

Write-Host ""
Write-Host "===============================================" -ForegroundColor Cyan
Write-Host " AutoTrader 원격제어 세션" -ForegroundColor Cyan
Write-Host " 아래 URL을 폰에서 열거나, Claude 앱 [코드] 탭에서" -ForegroundColor Cyan
Write-Host " 세션 카드를 탭하세요. 이 창은 닫지 마세요." -ForegroundColor Cyan
Write-Host "===============================================" -ForegroundColor Cyan
Write-Host ""

# 세션 이름을 고정해두면 폰 앱 목록에서 찾기 쉽다(자동 생성 이름은 매번 바뀜).
claude --remote-control autotrader

# claude가 종료돼도 창을 유지해서 원인을 볼 수 있게 한다.
Write-Log "remote-control session ended (exit code: $LASTEXITCODE)" "원격제어 세션 종료됨 (exit code: $LASTEXITCODE)"
Write-Host ""
Write-Host "세션이 종료됐습니다. 아무 키나 누르면 창을 닫습니다..." -ForegroundColor Yellow
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
