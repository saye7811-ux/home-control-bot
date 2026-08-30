# -*- coding: utf-8 -*-
"""오프라인 테스트용 픽스처 생성기.

엔카 응답 구조를 모사한 samples/sample_search.json 을 만든다.
실제 데이터가 아니며, score/merge/report 파이프라인 점검 전용이다.
"""

from __future__ import annotations

import json
import os
import random

random.seed(20260830)

HERE = os.path.dirname(os.path.abspath(__file__))

IX_OPTS_BASE = ["파노라마 선루프", "하만카돈", "헤드업 디스플레이", "통풍시트",
                "어라운드뷰", "차선유지보조", "열선 스티어링"]
IX_AIRSUS = ["에어서스펜션", "인테그럴 액티브 스티어링"]
EQE_OPTS_BASE = ["부메스터 사운드", "파노라마 선루프", "MBUX 하이퍼스크린",
                 "통풍시트", "360도 카메라", "디스트로닉"]
EQE_AIRSUS = ["에어매틱 서스펜션", "AIRMATIC"]

REGIONS = ["경기", "서울", "인천", "부산", "대구", "충남", "경남"]


def plate(i: int) -> str:
    han = "가나다라마바사아자하"
    return f"{100 + i:03d}{han[i % len(han)]}{1000 + (i * 137) % 9000:04d}"


def make(key, label, mfr, mg, model, badge, y_from, y_to, base, dep_per_yr,
         dep_per_1000km, airsus_opts, base_opts, n, start_id, seq_base):
    out = []
    for i in range(n):
        year = random.randint(y_from, y_to)
        month = random.randint(1, 12)
        age = (2026 - year) + (8 - month) / 12.0
        km = int(max(3000, random.gauss(age * 17000, 11000)))
        has_air = random.random() < 0.45
        fair = base - dep_per_yr * age - dep_per_1000km * (km / 1000.0)
        fair += 250 if has_air else 0
        price = int(max(2500, fair + random.gauss(0, 260)))

        acc_my = 0 if random.random() < 0.62 else random.randint(1, 2)
        acc_other = 0 if random.random() < 0.75 else 1
        acc_cost = 0 if acc_my == 0 else random.choice([320_000, 1_450_000, 3_800_000, 7_200_000])
        owners = random.choice([1, 1, 1, 2, 2, 3])
        flood = (i == 3 and key == "ix_xdrive50")
        rental = random.random() < 0.18
        diagnosed = random.random() < 0.55

        opts = list(base_opts)
        if has_air:
            opts = airsus_opts + opts
        random.shuffle(opts)

        vid = str(start_id + i)
        hist = []
        if flood:
            hist.append("침수 전손 이력 있음")
        if rental:
            hist.append("렌트(대여용) 이력 있음")
        if acc_my == 0 and acc_other == 0 and not flood:
            hist.append("무사고")

        out.append({
            "search": {
                "Id": vid,
                "Manufacturer": mfr,
                "ModelGroup": mg,
                "Model": model,
                "Badge": badge,
                "BadgeDetail": f"{badge} 기본형",
                "FuelType": "전기",
                "Transmission": "오토",
                "Year": int(f"{year}{month:02d}"),
                "FormYear": str(year),
                "Mileage": km,
                "Price": price,
                "OfficeCityState": random.choice(REGIONS),
                "Photo": f"/carpicture/ce{vid}/001.jpg",
                "SellType": "일반",
            },
            "detail": {
                "vehicleId": int(vid),
                "vehicleNo": plate(seq_base + i),
                "category": {
                    "manufacturerName": mfr, "modelName": model,
                    "gradeName": badge, "formYear": year,
                    "yearMonth": int(f"{year}{month:02d}"),
                },
                "spec": {"mileage": km, "fuelName": "전기"},
                "advertisement": {"price": price},
                "options": {"standard": opts},
                "photos": [{"path": f"/carpicture/ce{vid}/001.jpg"}],
            },
            "record": {
                "myAccidentCnt": acc_my,
                "otherAccidentCnt": acc_other,
                "myAccidentCost": acc_cost,
                "ownerChangeCnt": owners,
                "historyText": " / ".join(hist),
            },
            "inspection": {
                "accidentHistory": "없음" if acc_my == 0 else "사고 이력 있음",
                "simpleRepair": random.choice(["없음", "판금 1개소", "교환 1개소"]),
                "comment": ("침수 전손 확인" if flood else
                            "고전압 배터리 외관 이상 없음"),
            },
            "diagnosis": ({"diagnosisYn": "Y", "grade": "A"} if diagnosed else None),
        })
    return out


data = {
    "ix_xdrive50": make(
        "ix_xdrive50", "BMW iX xDrive50", "비엠더블유", "iX", "iX", "xDrive50",
        2022, 2024, base=10600, dep_per_yr=780, dep_per_1000km=13.0,
        airsus_opts=IX_AIRSUS, base_opts=IX_OPTS_BASE, n=16, start_id=39100001, seq_base=0),
    "eqe_suv_350": make(
        "eqe_suv_350", "벤츠 EQE SUV 350 4MATIC", "벤츠", "EQE", "EQE SUV",
        "350 4MATIC", 2023, 2024, base=11200, dep_per_yr=900, dep_per_1000km=15.0,
        airsus_opts=EQE_AIRSUS, base_opts=EQE_OPTS_BASE, n=13, start_id=39200001, seq_base=50),
}

path = os.path.join(HERE, "sample_search.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
print(f"wrote {path}: " + ", ".join(f"{k}={len(v)}" for k, v in data.items()))
