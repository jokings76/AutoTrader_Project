class AccountInfo:
    def __init__(self):
        self.deposit = 0          # 예수금
        self.total_eval = 0       # 총 평가금액
        self.holdings = {}        # 보유 종목 리스트 {종목코드: {수량, 평균단가, 수익률...}}

    def update_balance(self, data):
        """서버에서 받은 잔고 데이터를 장부에 업데이트합니다."""
        self.deposit = data.get('deposit', 0)
        # 상세 업데이트 로직은 추후 구현