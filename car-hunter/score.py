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
    to_float,
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
    "record_available", "accident_free", "accident_summary",
    "accident_my_count", "accident_other_count", "accident_my_cost_won",
    "owner_change_count", "past_commercial_use", "past_rental_count",
    "insp_leak", "insp_corrosion", "insp_tire",
    "insp_repair_notes", "insp_repair_penalty", "insp_worst_rank",
    "insp_worst_status",
    "baseline_manwon", "fair_price_manwon", "value_gap_manwon",
    "price_breakdown", "price_unknowns", "score_points",
    "insp_unclassified", "battery_pack_damage",
    "insp_diagnostics", "repair_source",
    "insp_mileage", "mileage_gap_km",
    "penalty_mileage_rollback", "insp_waterlog", "insp_recall",
    "insp_recall_types", "insp_comments", "insp_needs_repair",
    "insp_usage_change", "insp_serious", "insp_vin",
    "accident_lines", "accident_type_verdict",
    "excluded", "excluded_reason", "use_history",
    "plate_change_count", "theft_count", "record_fields_null",
    "first_registration_date", "age_basis",
    "flood_or_total_loss", "rental_or_commercial", "one_owner", "encar_diagnosed",
    "page_available", "repair_grade_source", "page_repair_notes",
    "page_repair_penalty", "page_worst_rank", "page_worst_status",
    "page_unmatched_parts", "page_mileage_gauge", "page_mileage",
    "page_vin_state", "page_tuning", "page_special_history", "page_usage_change",
    "page_recall", "page_recall_done", "page_accident_history", "page_simple_repair",
    "page_first_registration", "page_inspection_valid", "page_inspector_note",
    "page_detail_bad", "page_ev_hv_bad", "page_ev_hv_checked", "page_parse_note",
    "warranty", "view_count", "subscribe_count",
    "score_value", "score_battery", "bonus_total", "penalty_overrun", "penalty_total",
    "score_value", "score_battery", "score_depreciation", "penalty_accident",
    "score_stage2", "score_total", "market_confidence", "detail_fetched",
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
    print(f"  기준선    {market.basis} 매물 기준 "
          f"(무사고 {market.n_clean}대 / 전체 {len(rows)}대, "
          f"사고 할인 계수 x{market.accident_scale:.1f})")
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
    print(f" 적정가 대비 저평가 상위 {min(n, len(rows))}대")
    print(_hr('━'))
    print(" 적정가 = 기준 시세에서 흠결(과주행·사고·배터리·이력)을 금액으로 뺀 값.")
    print(" 흠결이 있어도 그만큼 싸면 위로 올라옵니다.")
    print("")
    print(" ※ 계수는 추정치입니다. 금액의 절대값이 아니라 매물 간 상대 비교로 보세요.")
    print(" ※ 에어서스·배터리 제조사는 아직 반영 전입니다 (헤이딜러 조회 후 확정).")
    print("")
    print(" ▼ 아래 차량번호를 헤이딜러 앱 '숨은이력찾기'에 입력하세요.")
    print(_hr('━'))

    for r in rows[:n]:
        plate = r.get("plate_no") or "(차량번호 미확보)"
        gap = to_float(r.get("value_gap_manwon"))
        fair = to_float(r.get("fair_price_manwon"))
        price = to_float(r.get("price_manwon"))

        print(f"\n  ┏{'━' * 34}┓")
        print(f"  ┃  #{r['rank']:<2}   {plate:^20}   ┃")
        print(f"  ┗{'━' * 34}┛")
        if gap is None or fair is None:
            print("       적정가 산출 불가")
        else:
            verdict = "저평가 (기회)" if gap > 0 else "고평가"
            print(f"       적정가 {fair:,.0f}만원 / 판매가 {price:,.0f}만원"
                  f"  ->  {gap:+,.0f}만원 {verdict}")
        print(f"       {r.get('model_label')} · {r.get('trim')} · {r.get('region')}"
              f"  |  {r.get('year')}.{str(r.get('month') or '').zfill(2)}"
              f"  |  {fmt_km(r.get('mileage_km'))}")

        for item in (r.get("price_breakdown") or "").split(" || "):
            if not item or "=" not in item:
                continue
            lab, amt = item.rsplit("=", 1)
            print(f"         {lab:<44} {amt:>12}")

        if r.get("price_unknowns"):
            for u in str(r["price_unknowns"]).split(" ; "):
                if u:
                    print(f"       ! 정보없음: {u}")
        src = r.get("repair_grade_source") or r.get("repair_source") or ""
        if src:
            print(f"       ※ 수리 부위 출처: {src}")
        if r.get("page_ev_hv_bad"):
            print(f"       !! 고전원전기장치 불량: {r['page_ev_hv_bad']}")
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
        for k in ("price_manwon", "year", "month", "mileage_km", "origin_price_manwon"):
            r[k] = to_int(r.get(k))
        rows.append(scoring.enrich(r))

    targets = {t["key"]: t for t in config.TARGETS}
    models, scored_all = [], []

    for key, target in targets.items():
        group = [r for r in rows if r.get("model_key") == key]
        if not group:
            warn(f"{target['label']}: 매물 0건 — 건너뜁니다.")
            continue
        market = scoring.fit_baseline(group, key, target["label"])
        for r in group:
            scoring.score_row(r, market, target)
        models.append((market, group))
        scored_all.extend(group)
        print_market_summary(market, group)

    if not scored_all:
        die("점수를 매길 매물이 없습니다.")

    # 시세 회귀는 전량을 쓰지만, 순위는 상세를 확보한 매물만 대상으로 한다.
    # 상세가 없으면 무사고/옵션/에어서스를 알 수 없어 가점이 0 이 되고,
    # 그대로 줄세우면 '정보가 없어서 낮은' 매물과 '실제로 나쁜' 매물이
    # 구분되지 않는다.
    def _detailed(r) -> bool:
        v = r.get("detail_fetched")
        if v in (None, ""):
            return True          # 옛 형식 CSV 호환
        return str(v).strip().lower() in ("true", "1", "y", "yes")

    def _excluded(r) -> bool:
        return str(r.get("excluded", "")).strip().lower() in ("true", "1")

    ranked = [r for r in scored_all if _detailed(r) and not _excluded(r)]
    excluded = [r for r in scored_all if _detailed(r) and _excluded(r)]
    skipped = len(scored_all) - len(ranked) - len(excluded)
    if excluded:
        warn(f"후보 제외 {len(excluded)}건 (침수/전손·배터리팩 손상·골격C):")
        for r in excluded[:5]:
            warn(f"    {r.get('plate_no') or r.get('vehicle_id')} — {r.get('excluded_reason')}")
    if skipped:
        log(f"순위 대상 {len(ranked)}건 (상세 미확보 {skipped}건은 시세 표본으로만 사용)")

    # 순위 기준은 '적정가 - 판매가'. 흠결이 있어도 그만큼 싸면 위로 올라온다.
    def _gap(r) -> float:
        v = to_float(r.get("value_gap_manwon"))
        return v if v is not None else -9e9   # 산출 불가 매물은 맨 뒤로

    ranked.sort(key=_gap, reverse=True)
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    for r in scored_all:
        r.setdefault("rank", "")

    write_csv(SCORED_CSV,
              ranked + excluded + [r for r in scored_all if not _detailed(r)],
              SCORED_FIELDS)
    write_json(MARKET_JSON, {m.key: m.__dict__ for m, _ in models})
    log(f"저장: {SCORED_CSV} (순위 {len(ranked)}건 + 표본 {skipped}건)")

    print_top(ranked, args.top)

    if not args.no_report:
        html = report_mod.build_html(models, ranked[:max(args.top, 20)], stage="stage2")
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
