# -*- coding: utf-8 -*-
"""2단계: 가성비 1차 스코어링.

  python score.py              # data/listings.csv -> data/scored.csv + report.html
  python score.py --top 15     # 상위 N개 출력
  python score.py --no-report  # HTML 생략

콘솔에는 모델별 시세 요약과 상위 매물을 출력하며, 헤이딜러 조회용
**차량번호**를 눈에 띄게 표시한다.
"""

from __future__ import annotations

import argparse
import os
import sys

import config
import report as report_mod
import scoring
from common import (
    BLOCKED_FLAG, LISTINGS_CSV, MARKET_JSON, REPORT_HTML, SCORED_CSV,
    die, ensure_dirs, fmt_km, fmt_manwon, log, read_csv, warn,
    write_csv, write_json,
)

SCORED_FIELDS = [
    "rank", "model_key", "model_label", "vehicle_id", "plate_no",
    "price_manwon", "predicted_price_manwon", "residual_manwon", "value_pct",
    "origin_price_manwon", "depreciation_pct",
    "year", "month", "age_years", "mileage_km", "annual_km", "region",
    "trim", "trim_detail",
    "battery_years_left", "battery_km_left", "battery_remaining_pct", "battery_binding",
    "accident_free", "accident_my_count", "accident_other_count",
    "accident_my_cost_won", "owner_change_count",
    "flood_or_total_loss", "rental_or_commercial", "one_owner", "encar_diagnosed",
    "has_airsus_keyword", "airsus_status", "airsus_keyword_hits",
    "option_source", "warranty", "view_count", "subscribe_count",
    "score_value", "score_battery", "bonus_total", "penalty_overrun", "penalty_total",
    "score_stage2", "score_total", "market_confidence",
    "reasons_plus", "reasons_minus",
    "inspection_summary", "options", "photo_url", "listing_url",
]

BOX_W = 78


def _hr(ch="─"):
    return ch * BOX_W


def print_market_summary(market, rows) -> None:
    prices = sorted(r["price_manwon"] for r in rows if r.get("price_manwon"))
    print(f"\n{_hr('━')}")
    print(f" {market.label}   ({len(rows)}대)")
    print(_hr())
    if prices:
        med = prices[len(prices) // 2]
        print(f"  가격      최저 {fmt_manwon(prices[0])}  /  중앙 {fmt_manwon(med)}"
              f"  /  최고 {fmt_manwon(prices[-1])}")
    kms = sorted(r["mileage_km"] for r in rows if r.get("mileage_km") is not None)
    if kms:
        print(f"  주행거리  최저 {fmt_km(kms[0])}  /  중앙 {fmt_km(kms[len(kms)//2])}"
              f"  /  최고 {fmt_km(kms[-1])}")
    if market.method == "regression":
        print(f"  시세선    가격 ≈ {market.intercept:,.0f} "
              f"{market.coef_age:+,.0f}×연수 {market.coef_km:+,.1f}×(km/1000)"
              f"   R²={market.r2:.2f}")
        print(f"            잔차 표준편차 ±{market.resid_std:,.0f}만원")
    else:
        print("  시세선    표본 5건 미만 — 중앙값 기준 비교로 대체")
    if market.low_confidence:
        print("  ! 표본이 적어 시세선 신뢰도가 낮습니다.")


def print_top(rows, n) -> None:
    print(f"\n{_hr('━')}")
    print(f" 종합점수 상위 {min(n, len(rows))}대  —  아래 [차량번호]를 헤이딜러"
          f" '숨은이력찾기'에 입력하세요")
    print(_hr('━'))
    for r in rows[:n]:
        plate = r.get("plate_no") or "(차량번호 미확보)"
        print(f"\n  #{r['rank']:<2}  ★ 차량번호  【 {plate} 】   점수 {r['score_total']}")
        print(f"       {r.get('model_label')} · {r.get('trim')} · {r.get('region')}")
        print(f"       {fmt_manwon(r.get('price_manwon'))}"
              f"  (시세예측 {fmt_manwon(r.get('predicted_price_manwon'))},"
              f" {r.get('value_pct')}%)"
              f"  |  {r.get('year')}.{str(r.get('month') or '').zfill(2)}"
              f"  |  {fmt_km(r.get('mileage_km'))}"
              f"  |  연 {r.get('annual_km') or '-'}km")
        print(f"       배터리 보증 잔여 {r.get('battery_remaining_pct')}%"
              f" ({r.get('battery_years_left')}년 / "
              f"{(r.get('battery_km_left') or 0):,}km, {r.get('battery_binding')} 기준)")
        for s in (r.get("reasons_plus") or "").split(" ; "):
            if s:
                print(f"       + {s}")
        for s in (r.get("reasons_minus") or "").split(" ; "):
            if s:
                print(f"       - {s}")
        if r.get("listing_url"):
            print(f"       {r['listing_url']}")


def main() -> int:
    p = argparse.ArgumentParser(description="가성비 스코어링 (2단계)")
    p.add_argument("--top", type=int, default=config.TOP_N)
    p.add_argument("--no-report", action="store_true")
    p.add_argument("--input", default=LISTINGS_CSV)
    args = p.parse_args()

    ensure_dirs()
    if os.path.exists(BLOCKED_FLAG):
        warn(f"직전 수집이 차단으로 중단된 흔적이 있습니다: {BLOCKED_FLAG}")
        warn("데이터가 불완전할 수 있습니다.")

    raw = read_csv(args.input)
    if not raw:
        die(f"매물 데이터가 없습니다: {args.input}\n먼저 `python collect.py` 를 실행하세요.")

    # 타입 정리 + 파생 지표
    rows = []
    from common import to_int
    for r in raw:
        for k in ("price_manwon", "year", "month", "mileage_km", "origin_price_manwon",
                  "accident_my_count", "accident_other_count", "accident_my_cost_won"):
            r[k] = to_int(r.get(k))
        rows.append(scoring.enrich(r))

    targets = {t["key"]: t for t in config.TARGETS}
    models, scored_all = [], []

    for key, target in targets.items():
        group = [r for r in rows if r.get("model_key") == key]
        if not group:
            warn(f"{target['label']}: 매물 0건 — 건너뜁니다.")
            continue
        market = scoring.fit_market(group, key, target["label"])
        for r in group:
            scoring.score_row(r, market, target)
        models.append((market, group))
        scored_all.extend(group)
        print_market_summary(market, group)

    if not scored_all:
        die("점수를 매길 매물이 없습니다.")

    scored_all.sort(key=lambda r: r.get("score_total", 0), reverse=True)
    for i, r in enumerate(scored_all, 1):
        r["rank"] = i

    write_csv(SCORED_CSV, scored_all, SCORED_FIELDS)
    write_json(MARKET_JSON, {m.key: m.__dict__ for m, _ in models})
    log(f"저장: {SCORED_CSV} ({len(scored_all)}건)")

    print_top(scored_all, args.top)

    if not args.no_report:
        html = report_mod.build_html(models, scored_all[:max(args.top, 20)], stage="stage2")
        with open(REPORT_HTML, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"저장: {REPORT_HTML}")

    print(f"\n{_hr('━')}")
    print(f" 다음 단계: 위 상위 {args.top}대의 차량번호를 헤이딜러 앱 '숨은이력찾기'로")
    print(f" 조회한 뒤 스크린샷을 hidden/ 폴더에 넣고  `python merge.py --show`  실행")
    print(_hr('━'))
    return 0


if __name__ == "__main__":
    sys.exit(main())
