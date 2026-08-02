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

# 자동 재시작 (2026-08-02 추가)
#
# 왜: 모바일 앱에서 '종료'를 누르면(특히 실수로 두 번 눌러 비정상 종료되면)
# claude --remote-control 프로세스가 끝나버린다. 예전엔 그러면 이 창이
# "아무 키나 누르면 닫습니다" 프롬프트에서 그대로 멈췄고, PC 앞에 사람이
# 없으면 그날 남은 시간 내내 폰 접속이 끊긴 채 방치됐다. 세션이 끝나도 이
# 창(프로세스)은 살려두고 자동으로 새 세션을 다시 띄운다(새 URL 생성, 이
# 창에 그대로 표시됨). 트레이딩 봇(main.py)은 완전히 별도 프로세스라 이
# 창에서 무슨 일이 나든 매매에는 영향이 없다.
#
# 진짜로 끄고 싶으면: 이 PowerShell 창 자체를 닫을 것 — 그러면 재시작
# 루프도 같이 죽는다. 모바일 '종료' 버튼은 세션만 끝낼 뿐 이 창을 못 닫는다.
#
# 연속 즉시종료(5초 미만) 3회면 자동재시작을 멈춘다 — 이건 클릭 실수가
# 아니라 PATH/인증 등 설정 문제일 가능성이 높아서, 무한 재시작 대신
# 사람이 보도록 멈춘다(main.py의 task_remote_control_watchdog가 09:06에
# 이 상태를 감지해 텔레그램으로도 알린다).
$maxRestarts = 30
$fastFailThreshold = 5
$maxConsecutiveFastFails = 3
$restartCount = 0
$consecutiveFastFails = 0
$stoppedForFastFail = $false

while ($restartCount -lt $maxRestarts) {
    $restartCount++
    $attemptStart = Get-Date
    Write-Log ("remote-control session starting (attempt {0})" -f $restartCount) ("원격제어 세션 시작 ({0}번째 시도)" -f $restartCount)

    # 세션 이름을 고정해두면 폰 앱 목록에서 찾기 쉽다(자동 생성 이름은 매번 바뀜).
    #
    # 기본은 --continue로 직전 대화(전날 것 포함)를 그대로 이어간다(2026-08-02
    # 사용자 지정) — 08:59 최초 기동/장중 자동재시작 구분 없이 항상 이어간다.
    # 안 그러면 자동재시작만으로도 "방금 폰에서 하던 얘기"가 매번 날아간다.
    #
    # 대화가 매우 길어져 한도에 가까워지면(다윤님이 판단) 세션 안에서
    # "이관작업 해줘"라고 요청해 CLAUDE.md에 맥락을 정리시킨 뒤, 프로젝트
    # 루트에 NEW_SESSION_REQUESTED 파일을 만들어두면 다음 기동 딱 1회만
    # --continue 없이 새 대화로 시작한다(플래그는 사용 즉시 삭제, 그 다음
    # 기동부터는 다시 자동으로 이어감). STOP_SIGNAL과 동일한 방식.
    $newSessionFlag = Join-Path $ProjectPath "NEW_SESSION_REQUESTED"
    if (Test-Path $newSessionFlag) {
        Remove-Item $newSessionFlag -Force -ErrorAction SilentlyContinue
        Write-Log "NEW_SESSION_REQUESTED found - starting fresh (no --continue) this attempt" "새 세션 요청 플래그 발견 -- 이번만 새 대화로 시작"
        claude --remote-control autotrader
    } else {
        claude --remote-control autotrader --continue
    }
    $exitCode = $LASTEXITCODE

    $elapsedSec = [int]((Get-Date) - $attemptStart).TotalSeconds
    Write-Log ("remote-control session ended (exit={0}, elapsed={1}s)" -f $exitCode, $elapsedSec) ("원격제어 세션 종료됨 (exit={0}, 지속시간={1}초)" -f $exitCode, $elapsedSec)

    if ($elapsedSec -lt $fastFailThreshold) {
        $consecutiveFastFails++
        if ($consecutiveFastFails -ge $maxConsecutiveFastFails) {
            Write-Log "auto-restart stopped: consecutive fast-fail limit reached" ("!! 연속 즉시종료 {0}회 -- 자동재시작 중단(설정/인증 문제 의심)" -f $maxConsecutiveFastFails)
            $stoppedForFastFail = $true
            break
        }
    } else {
        $consecutiveFastFails = 0
    }

    Write-Host ""
    Write-Host "5초 후 새 세션으로 자동 재시작합니다... (완전히 끄려면 이 창을 닫으세요)" -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}

if (-not $stoppedForFastFail -and $restartCount -ge $maxRestarts) {
    Write-Log "auto-restart limit reached" ("원격제어 자동재시작 한도 도달 ({0}회) -- 오늘분 소진" -f $maxRestarts)
}

Write-Host ""
Write-Host "더 이상 자동 재시작하지 않습니다. 아무 키나 누르면 창을 닫습니다..." -ForegroundColor Yellow
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
