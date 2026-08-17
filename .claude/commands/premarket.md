---
description: 장 시작 전 상태 점검 — DB 미청산 · 봇/스케줄러 · 플래그 잔존 · 기대값 대조
---

장 시작 전 점검이다. **읽기만 한다. 아무것도 고치지 마라.**

아래를 순서대로 확인하고 표로 보고하라.

## 1. DB 미청산

```bash
python -c "from db.connection import get_connection
with get_connection() as c:
    cur=c.cursor(); cur.execute(\"SELECT stock_code,stock_name,buy_price,buy_quantity,status,buy_time FROM trades WHERE status!='closed' ORDER BY buy_time DESC\")
    r=cur.fetchall(); print('미청산', len(r), '건'); [print(' ', x) for x in r]"
```

- `holding`이 있으면 → 전일 보유분이다. 기동 시 `OVERNIGHT_RESTORE_AS_MANUAL`이 **자동으로 `manual` 격리**한다(정상).
- `manual`은 **봇 관리 밖**이다. 손절·익절 대상이 아니다.
- 🔴 **격리분이 쌓이면 예수금이 잠긴다.** 첫 증상은 "신규 매수가 안 된다".
  `python manual_position_tool.py list`로 확인하고, 사용자에게 정리 필요 여부를 보고하라.

## 2. 봇·스케줄러

```bash
Get-Process python -ErrorAction SilentlyContinue
```
```powershell
$i=Get-ScheduledTaskInfo -TaskName "AutoTrader_Start"; $t=Get-ScheduledTask -TaskName "AutoTrader_Start"
[PSCustomObject]@{State=$t.State; NextRun=$i.NextRunTime; LastRun=$i.LastRunTime; LastResult=$i.LastTaskResult} | Format-List
```

- `State`가 `Ready`, `NextRun`이 **다음 평일 08:59**, 요일마스크 **62**(월~금)여야 한다.
- ⚠️ 로그아웃/로그인 화면 상태면 `LogonType=Interactive` 때문에 **안 뜬다.** 로그인하면 즉시 기동한다.
- `AutoTrader_PremarketScan`은 **`Disabled`가 정상**이다(08-09 사용자 지정). 고장이 아니다.

## 3. 플래그 잔존

`STOP_SIGNAL` / `NEW_SESSION_REQUESTED` 가 프로젝트 루트에 있으면 보고하라.
- `STOP_SIGNAL`이 남아 있으면 → **기동 즉시 종료된다.** 지워야 한다.
- `NEW_SESSION_REQUESTED`가 있으면 → 다음 원격 세션이 새 대화로 시작(의도된 것일 수 있음).

## 4. 기대값 ↔ 코드 대조

```bash
PYTHONIOENCODING=utf-8 python audit_20260805.py 2>/dev/null | tail -3
```

**182건 / 실패 0**이어야 한다. 실패하면 상수와 문서가 어긋난 것이다.
⚠️ 봇이 이미 돌고 있으면 이 단계는 **건너뛰고 그 사실을 보고하라**(`import main` + 토큰 충돌).

## 5. 오늘 볼 것

`CLAUDE.md` 「다음 할 일」의 **「🔬 … 장중에 반드시 볼 것」** 표를 읽고,
**오늘 처음 도는 변경**이 무엇인지, 장중에 무엇을 봐야 하는지 요약하라.

## 출력

```
■ 장전 점검 <날짜>
| 항목 | 결과 | 판정 |
|---|---|---|
| DB 미청산 | holding N · manual N | ✅/⚠️ |
| 봇 프로세스 | | |
| 스케줄러 | State · NextRun | |
| 플래그 | | |
| 기대값 대조 | 182/0 | |

■ 오늘 처음 도는 변경: <목록>
■ 장중 볼 것: <Q0~Qn 요약>
■ 🔴 조치 필요: 없음 / <무엇을>
```
