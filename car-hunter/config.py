# -*- coding: utf-8 -*-
"""car-hunter 전역 설정.

수집 대상 차종, 요청 정책, 점수 가중치를 한 곳에서 관리한다.
점수 튜닝은 이 파일만 고치면 되고, 나머지 코드는 건드릴 필요 없다.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1단계: 수집 정책
# ---------------------------------------------------------------------------
COLLECT = {
    # 개인 검토용 소량 수집. 간격을 줄이지 말 것.
    "request_interval_sec": 3.0,
    "retry": 2,                          # 네트워크 오류 시 재시도 횟수
    "retry_backoff_sec": [5.0, 15.0],    # 재시도 대기 (retry 개수만큼 사용)
    "timeout_sec": 20,
    # 모델당 최대 수집 건수. 개인 검토 목적이라 넉넉히 잡아도 100 이하 권장.
    "max_listings_per_model": 60,
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# ---------------------------------------------------------------------------
# 엔카 검색 엔드포인트
# ---------------------------------------------------------------------------
# 2026-08 브라우저 개발자도구에서 실제 확인된 경로는 /premium 이다.
# /premium 은 AdType.B (프리미엄 광고 상품) 매물만, /general 은 AdType.A
# 매물만 돌려주는 것으로 보인다. 시세 회귀가 한쪽에 치우치지 않도록
# 기본값은 둘 다 수집해 vehicle_id 로 중복 제거한다.
ENDPOINTS = {
    "premium": {
        "url": "https://api.encar.com/search/car/list/premium",
        "ad_type": "B",
        "confirmed": True,   # 실제 요청에서 확인됨
    },
    "general": {
        "url": "https://api.encar.com/search/car/list/general",
        "ad_type": "A",
        "confirmed": False,  # 미확인 — probe 로 확인할 것
    },
}
# 수집에 사용할 엔드포인트 순서. 404 가 나는 것은 자동으로 건너뛴다.
USE_ENDPOINTS = ["premium", "general"]

# 정렬 키 (sr 파라미터의 첫 칸). ModifiedDate = 최신 수정순.
SORT_KEY = "ModifiedDate"

# 한 번의 요청으로 받아올 매물 수. 요청 횟수를 줄이려면 크게 잡는 편이
# 서버에도 낫다 (3초 간격 × 요청 수가 총 소요시간이므로).
PAGE_SIZE = 50
PROBE_PAGE_SIZE = 50

# ---------------------------------------------------------------------------
# 대상 차종
# ---------------------------------------------------------------------------
# manufacturer / model_group / model 은 엔카 내부 표기를 '그대로' 써야 한다.
# confirmed=True 는 실제 요청에서 확인된 값, False 는 추정값이다.
# 추정값으로 결과가 0건이면 아래로 실제 표기를 확인해서 고칠 것:
#     python collect.py --discover                  # 제조사 목록
#     python collect.py --discover --mfr 벤츠        # 그 제조사의 모델그룹 목록
#     python collect.py --discover --mfr 벤츠 --mg EQE   # 하위 모델 목록
TARGETS = [
    {
        "key": "ix_xdrive50",
        "label": "BMW iX xDrive50",
        # CarType.N — 브라우저 확인값 (BMW=수입차가 N 으로 나감)
        "car_type": "N",
        "manufacturer": "BMW",
        "model_group": "iX",
        "model": None,          # 확인된 요청은 ModelGroup 까지만 내려간다
        "confirmed": True,
        # 상세 트림(Badge)에 아래 문자열 중 하나가 들어가야 최종 채택.
        # ModelGroup 만으로는 xDrive40 / M60 이 섞여 들어오므로 필수.
        "badge_contains": ["xDrive50", "xDrive 50", "xdrive50"],
        "year_from": 2022,
        "year_to": 2024,
        "airsus_keywords": [
            "에어서스", "에어 서스", "에어서스펜션",
            "다이나믹 핸들링", "다이내믹 핸들링",
            "인테그럴 액티브", "인테그럴액티브",
        ],
    },
    {
        "key": "eqe_suv_350",
        "label": "벤츠 EQE SUV 350 4MATIC",
        "car_type": "N",
        # ↓ 추정값. BMW 가 'BMW' 로 나가는 걸 보면 브랜드 표기를 그대로 쓰는
        #   방식이라 벤츠는 '벤츠' 일 가능성이 높지만 확인되지 않았다.
        "manufacturer": "벤츠",
        "model_group": "EQE",
        "model": "EQE SUV",
        "confirmed": False,
        "badge_contains": ["350 4MATIC", "350 4매틱", "350 4마틱", "EQE SUV 350"],
        "year_from": 2023,
        "year_to": 2024,
        "airsus_keywords": [
            "에어매틱", "에어 매틱", "AIRMATIC", "airmatic",
            "에어서스", "에어서스펜션",
        ],
    },
]

# ---------------------------------------------------------------------------
# 2단계: 스코어링 가중치
# ---------------------------------------------------------------------------
SCORING = {
    # 시세 회귀 잔차 점수 (가장 큰 비중)
    "value_max_pts": 40.0,
    # 예측가 대비 ±이 % 만큼 싸면/비싸면 만점/0점. 15% => -15%는 0점, +15%는 40점
    "value_pct_span": 15.0,

    # 배터리 보증 잔여 점수 (0 ~ battery_max_pts)
    "battery_max_pts": 20.0,

    "bonus": {
        "no_accident": 8.0,        # 무사고
        "encar_diagnosed": 5.0,    # 엔카진단
        "one_owner": 4.0,          # 1인 소유
        "airsus_keyword": 6.0,     # 에어서스 관련 옵션 키워드 (1단계 추정)
    },
    "penalty": {
        "overrun_25k": 6.0,        # 연평균 2.5만km 초과
        "overrun_30k": 12.0,       # 연평균 3만km 초과 (25k 감점을 대체)
        "flood_total_loss": 40.0,  # 침수 / 전손
        "rental_commercial": 10.0, # 렌트 / 영업용 이력
    },
}

# 고전압 배터리 보증: 8년 / 16만km (둘 중 먼저 도달)
BATTERY_WARRANTY = {
    "years": 8,
    "km": 160_000,
}

# 과주행 판정 기준 (km/년)
ANNUAL_KM_WARN = 25_000
ANNUAL_KM_BAD = 30_000

# ---------------------------------------------------------------------------
# 3단계: 헤이딜러 숨은이력 병합 후 재계산 가중치
# ---------------------------------------------------------------------------
# 배터리 제조사별 가감점. 키는 대문자/공백 제거 후 비교한다.
BATTERY_MAKER_ADJ = {
    "삼성SDI": 8.0,
    "SAMSUNGSDI": 8.0,
    "SDI": 8.0,
    "LG에너지솔루션": 2.0,
    "LGES": 2.0,
    "LG": 2.0,
    "SK온": 2.0,
    "SKON": 2.0,
    "CATL": 0.0,
    "파라시스": -25.0,
    "FARASIS": -25.0,
}

# 에어서스 확정 여부 (헤이딜러 출고옵션 기준)
AIRSUS_CONFIRMED_BONUS = 8.0   # 확정 장착
AIRSUS_ABSENT_PENALTY = 4.0    # 미장착으로 확정 (1단계 키워드 가점 회수 성격)

# 보험 수리이력 총액(원) 구간별 감점. (상한, 가감점, 라벨) — 오름차순
INSURANCE_TIERS = [
    (0,          5.0,   "없음"),
    (500_000,   -2.0,   "50만원 이하 경미"),
    (2_000_000, -6.0,   "50~200만원"),
    (5_000_000, -14.0,  "200~500만원"),
    (float("inf"), -25.0, "500만원 초과"),
]

# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
TOP_N = 10  # 헤이딜러 수동 조회 대상 상위 N대
