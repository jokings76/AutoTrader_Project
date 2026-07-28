import sys
sys.path.insert(0, ".")

import requests
import json
from collections import defaultdict
from api.auth import get_access_token


def fn_ka10080(token, data, cont_yn='N', next_key=''):
    host = 'https://mockapi.kiwoom.com'
    endpoint = '/api/dostk/chart'
    url = host + endpoint
    headers = {
        'Content-Type': 'application/json;charset=UTF-8',
        'authorization': f'Bearer {token}',
        'cont-yn': cont_yn,
        'next-key': next_key,
        'api-id': 'ka10080',
    }
    response = requests.post(url, headers=headers, json=data)
    print('Code:', response.status_code)
    print('Header:', json.dumps({key: response.headers.get(key) for key in ['next-key', 'cont-yn', 'api-id']}, indent=4, ensure_ascii=False))
    try:
        body = response.json()
        print('Body:', json.dumps(body, indent=4, ensure_ascii=False))
    except ValueError:
        print('Body: (응답이 JSON이 아닙니다)', response.text)
        body = None
    return response, body


def test_without_base_dt(token, stk_cd='005930', tic_scope='1', upd_stkpc_tp='1', max_pages=10):
    params = {
        'stk_cd': stk_cd,
        'tic_scope': tic_scope,
        'upd_stkpc_tp': upd_stkpc_tp,
    }
    all_records = []
    cont = 'N'
    next_key = ''
    page = 0
    while True:
        page += 1
        if page > max_pages:
            print(f'최대 페이지({max_pages}) 도달, 중단합니다.')
            break
        resp, body = fn_ka10080(token=token, data=params, cont_yn=cont, next_key=next_key)
        if body and 'stk_min_pole_chart_qry' in body:
            all_records.extend(body['stk_min_pole_chart_qry'])
        resp_cont = resp.headers.get('cont-yn', 'N')
        resp_next = resp.headers.get('next-key', '')
        print(f'페이지 {page} cont-yn={resp_cont} next-key={"(있음)" if resp_next else "(없음)"}')
        if resp_cont == 'Y' and resp_next:
            cont = 'Y'
            next_key = resp_next
        else:
            break
    unique_dates = set()
    for it in all_records:
        tm = it.get('cntr_tm', '')
        if len(tm) >= 8:
            unique_dates.add(tm[:8])
    print('수신된 고유 날짜들(YYYYMMDD):', sorted(unique_dates))
    print('총 레코드 수:', len(all_records))
    for i, r in enumerate(all_records[:5]):
        print(f'레코드 {i+1}: cntr_tm={r.get("cntr_tm")}, cur_prc={r.get("cur_prc")}')
    return unique_dates, all_records


if __name__ == '__main__':
    MY_ACCESS_TOKEN = get_access_token()
    dates, records = test_without_base_dt(token=MY_ACCESS_TOKEN, stk_cd='005930', tic_scope='1', upd_stkpc_tp='1', max_pages=5)
import time


def fetch_n_days_history(token, stk_cd='005930', tic_scope='1', upd_stkpc_tp='1',
                           target_days=20, request_delay_sec=0.5, max_pages=500):
    """base_dt 없이 페이징만으로 target_days만큼의 고유 날짜를 모을 때까지 수집.
    429(요청 초과) 발생 시 지수 백오프로 재시도."""
    params = {
        'stk_cd': stk_cd,
        'tic_scope': tic_scope,
        'upd_stkpc_tp': upd_stkpc_tp,
    }
    all_records = []
    unique_dates = set()
    cont = 'N'
    next_key = ''
    page = 0
    backoff = 1.0

    while len(unique_dates) < target_days:
        page += 1
        if page > max_pages:
            print(f'최대 페이지({max_pages}) 도달, {len(unique_dates)}일치만 수집됨')
            break

        resp, body = fn_ka10080(token=token, data=params, cont_yn=cont, next_key=next_key)

        if resp.status_code == 429 or (body and body.get('return_code') == 5):
            print(f'요청 초과, {backoff:.1f}초 대기 후 재시도... (페이지 {page})')
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)  # 최대 30초까지 백오프
            page -= 1  # 이 페이지는 재시도이므로 카운트 안 함
            continue
        backoff = 1.0  # 성공하면 백오프 리셋

        if body and 'stk_min_pole_chart_qry' in body:
            chunk = body['stk_min_pole_chart_qry']
            all_records.extend(chunk)
            for it in chunk:
                tm = it.get('cntr_tm', '')
                if len(tm) >= 8:
                    unique_dates.add(tm[:8])

        resp_cont = resp.headers.get('cont-yn', 'N')
        resp_next = resp.headers.get('next-key', '')
        print(f'페이지 {page} cont-yn={resp_cont} 누적일수={len(unique_dates)}/{target_days} 누적레코드={len(all_records)}')

        if resp_cont == 'Y' and resp_next:
            cont, next_key = 'Y', resp_next
            time.sleep(request_delay_sec)  # 요청 간 간격 -- 429 방지 핵심
        else:
            print('연속조회 종료 (더 이상 과거 데이터 없음)')
            break

    print(f'\n완료: {len(unique_dates)}일치, 레코드 {len(all_records)}개 수집')
    print('수집된 날짜:', sorted(unique_dates))
    return all_records


import time


def fetch_n_days_history(token, stk_cd='005930', tic_scope='1', upd_stkpc_tp='1',
                           target_days=20, request_delay_sec=0.5, max_pages=500):
    """base_dt 없이 페이징만으로 target_days만큼의 고유 날짜를 모을 때까지 수집.
    429(요청 초과) 발생 시 지수 백오프로 재시도."""
    params = {
        'stk_cd': stk_cd,
        'tic_scope': tic_scope,
        'upd_stkpc_tp': upd_stkpc_tp,
    }
    all_records = []
    unique_dates = set()
    cont = 'N'
    next_key = ''
    page = 0
    backoff = 1.0

    while len(unique_dates) < target_days:
        page += 1
        if page > max_pages:
            print(f'최대 페이지({max_pages}) 도달, {len(unique_dates)}일치만 수집됨')
            break

        resp, body = fn_ka10080(token=token, data=params, cont_yn=cont, next_key=next_key)

        if resp.status_code == 429 or (body and body.get('return_code') == 5):
            print(f'요청 초과, {backoff:.1f}초 대기 후 재시도... (페이지 {page})')
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)  # 최대 30초까지 백오프
            page -= 1  # 이 페이지는 재시도이므로 카운트 안 함
            continue
        backoff = 1.0  # 성공하면 백오프 리셋

        if body and 'stk_min_pole_chart_qry' in body:
            chunk = body['stk_min_pole_chart_qry']
            all_records.extend(chunk)
            for it in chunk:
                tm = it.get('cntr_tm', '')
                if len(tm) >= 8:
                    unique_dates.add(tm[:8])

        resp_cont = resp.headers.get('cont-yn', 'N')
        resp_next = resp.headers.get('next-key', '')
        print(f'페이지 {page} cont-yn={resp_cont} 누적일수={len(unique_dates)}/{target_days} 누적레코드={len(all_records)}')

        if resp_cont == 'Y' and resp_next:
            cont, next_key = 'Y', resp_next
            time.sleep(request_delay_sec)  # 요청 간 간격 -- 429 방지 핵심
        else:
            print('연속조회 종료 (더 이상 과거 데이터 없음)')
            break

    print(f'\n완료: {len(unique_dates)}일치, 레코드 {len(all_records)}개 수집')
    print('수집된 날짜:', sorted(unique_dates))
    return all_records


if __name__ == '__main__':
    MY_ACCESS_TOKEN = get_access_token()
    records = fetch_n_days_history(token=MY_ACCESS_TOKEN, stk_cd='005930',
                                     tic_scope='1', target_days=20, request_delay_sec=0.5)