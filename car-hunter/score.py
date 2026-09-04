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
import copy
import os
import shutil
import sys
from datetime import date as _date

import config
import history
import report as report_mod
import scoring
from common import (
    to_float, to_int,
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
    "value_gap_pct", "value_gap_sigma", "sigma_manwon",
    "value_verdict", "value_verdict_note", "discount_priced_in",
    "discount_extra", "discount_extra_manwon", "discount_unexplained_manwon",
    "seller_option_claims", "seller_text_len", "insp_vin",
    "vin_option_state", "vin_option_manwon", "vin_option_source",
    "vin_option_verified_at",
    "trim_key", "trim_offset_manwon", "battery_maker", "battery_risk",
    "battery_note", "view_count", "subscribe_count", "view_per_day",
    "listing_signal", "listing_signal_note",
    "discount_notes", "insurance_gap_dealer", "insurance_gap_personal",
    "insurance_gap_unknown", "first_advertised",
    "days_on_market", "days_on_market_basis", "first_seen", "last_seen",
    "price_first_manwon", "price_prev_manwon", "price_change_manwon",
    "price_change_count", "insurance_not_joined", "re_registered", "loan_count",
    "sample_only", "sample_only_reason", "origin_price_manwon", "sell_type",
    "page_is_image",
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
    "page_unmatched_parts", "page_status_unknown", "page_ranks_read",
    "page_mileage_gauge", "page_mileage",
    "page_vin_state", "page_tuning", "page_special_history", "page_usage_change",
    "page_recall", "page_recall_done", "page_accident_history", "page_simple_repair",
    "page_first_registration", "page_inspection_valid", "page_inspector_note",
    "page_js_suspect", "page_detail_bad", "page_detail_unknown",
    "page_ev_hv_bad", "page_ev_hv_unknown", "page_ev_hv_checked", "page_parse_note",
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
    # 통합 시세선을 쓰면 표본 수는 이 차종이 아니라 '합친 표본' 의 것이다.
    # 그대로 '전체 N대' 옆에 붙이면 무사고가 전체보다 많아 보이는 이상한
    # 표시가 된다. 어느 표본으로 그린 선인지 분명히 적는다.
    print(f"  기준선    {market.basis} 매물 기준 · 시세선 표본 {market.n}대"
          f"(무사고 {market.n_clean}대), 사고 할인 계수 "
          f"x{market.accident_scale:.1f}")
    if market.n != len(rows):
        print(f"            이 차종 매물은 {len(rows)}대이고, 시세선은 위 표본으로 "
              f"그렸습니다")
    origins = sorted(r["origin_price_manwon"] for r in rows
                     if r.get("origin_price_manwon"))
    if origins:
        print(f"  신차가    최저 {fmt_manwon(origins[0])}  /  "
              f"최고 {fmt_manwon(origins[-1])}  (트림 차이)")

    if market.method == "ratio":
        # 트림이 섞여 있으므로 가격이 아니라 '잔존율' 을 회귀한다.
        # 적정가 = 잔존율 × 그 매물의 신차가.
        print(f"  시세선    잔존율 ≈ {market.intercept*100:,.1f}% "
              f"{market.coef_age*100:+,.1f}%p×연수 "
              f"{market.coef_km*100:+,.3f}%p×(km/1000)"
              f"   R²={market.r2:.2f}")
        print(f"            (적정가 = 잔존율 × 신차가 — 트림 차이를 흡수합니다)")
        print(f"            잔차 표준편차 ±{market.resid_std*100:.2f}%p"
              f"  = 신차가 1억 기준 ±{market.resid_std*10000:,.0f}만원")
    elif market.method == "absolute":
        print(f"  시세선    가격 ≈ {market.intercept:,.0f} "
              f"{market.coef_age:+,.0f}×연수 {market.coef_km:+,.1f}×(km/1000)"
              f"   R²={market.r2:.2f}")
        print(f"            잔차 표준편차 ±{market.resid_std:,.0f}만원")
        print("            ! 신차가를 못 받은 매물이 많아 트림 보정 없이 "
              "가격을 직접 회귀했습니다")
    else:
        print("  시세선    표본 5건 미만 — 중앙값 기준 비교로 대체")

    # 통합 시세선이면 market 이 두 차종에 공유된다. 트림 표는 이 차종의
    # 트림만 보여야 한다 (안 그러면 BMW 요약에 EQE 트림이 섞인다).
    own = {scoring.normalize_trim(r.get("trim")) for r in rows}
    stats = [st for st in (getattr(market, "trim_stats", None) or [])
             if st.get("trim") in own]
    if stats:
        print("  트림 편차  (기준선 표본에서 실측 — 계수를 사람이 정하지 않음)")
        print(f"    {'트림':12}{'n':>4}{'편차':>10}{'t':>7}  판정")
        for st in stats:
            t = st["t"]
            mark = "반영" if st["applied"] else "  - "
            print(f"    {st['trim']:12}{st['n']:>4}{st['mean_pct']:>+9.2f}%p"
                  f"{(t if t is not None else float('nan')):>7.2f}  {mark} {st['why']}")
        applied = [st for st in stats if st["applied"]]
        if applied:
            print("    -> 반영된 트림은 기준 시세가 그만큼 조정됩니다. "
                  "구조적으로 기피되는 트림이 매주 '저평가' 로 뜨는 것을 막습니다.")
    if getattr(market, "km_coverage_note", ""):
        print(f"  ! 주행거리 범위  {market.km_coverage_note}")
    if market.n_dropped:
        print(f"  이상치    {market.dropped_note}")
        print("            표본 하나가 시세선을 통째로 끌고 가는 것을 막습니다")
    n_lease_here = sum(1 for r in rows if scoring.is_lease_listing(r))
    if n_lease_here:
        print(f"  리스·렌트 이 차종 {len(rows)}대 중 {n_lease_here}대 "
              f"(순위에서는 제외 — 표시 가격이 인수금이라 차값과 다릅니다)")
    if market.low_confidence:
        print("  ! 표본이 적어 시세선 신뢰도가 낮습니다.")


PRICE_BANDS = [
    (0, 6000, "6천만원 미만"),
    (6000, 8000, "6~8천만원"),
    (8000, 10000, "8천만~1억"),
    (10000, 13000, "1억~1억3천"),
    (13000, 10 ** 9, "1억3천 이상"),
]


def _band(price):
    for lo, hi, lab in PRICE_BANDS:
        if price is not None and lo <= price < hi:
            return lab
    return "가격 미상"


def print_price_bands(rows) -> None:
    """가격대별로 묶어서 본다.

    목적이 '예산 안에서 고르기' 가 아니라 '저평가 탐지' 라서 가격으로
    거르지 않는다. 대신 어느 가격대에 기회가 몰려 있는지는 보여준다.
    """
    if not rows:
        return
    print(f"\n{_hr('━')}")
    print(" 가격대별 요약")
    print(_hr('━'))
    print(f" {'가격대':<14}{'대수':>4}  {'최고 저평가':>28}   {'중앙 σ':>8}")
    for _lo, _hi, lab in PRICE_BANDS:
        g = [r for r in rows if _band(to_float(r.get("price_manwon"))) == lab]
        if not g:
            continue
        best = max(g, key=lambda r: to_float(r.get("value_gap_sigma")) or -9e9)
        sg = to_float(best.get("value_gap_sigma"))
        gap = to_float(best.get("value_gap_manwon"))
        pct = to_float(best.get("value_gap_pct"))
        sigs = sorted(x for x in (to_float(r.get("value_gap_sigma")) for r in g)
                      if x is not None)
        med = sigs[len(sigs) // 2] if sigs else None
        head = (f"{best.get('plate_no') or '?'} {gap:+,.0f}만 "
                f"({pct:+.1f}%, {sg:+.2f}σ)") if sg is not None else "산출 불가"
        print(f" {lab:<14}{len(g):>4}대  {head:>28}   "
              f"{(f'{med:+.2f}σ' if med is not None else '-'):>8}")


def print_lease_impact(groups: list) -> None:
    """리스·렌트를 시세 표본에 넣었을 때와 뺐을 때를 나란히 보여준다.

    리스·렌트 승계는 표시 가격이 차값이 아니라 인수금이다. 표본에 넣으면
    시세선이 아래로 당겨지고, 그러면 멀쩡한 매물이 죄다 '고평가' 로
    보인다. 차이가 작으면 표본을 키우는 쪽이 낫고, 크면 빼야 한다.
    """
    rows_all = [r for _k, _t, g in groups for r in g]
    lease = [r for r in rows_all if scoring.is_lease_listing(r)]
    if not lease:
        return
    print(f"\n{_hr('━')}")
    print(f" 리스·렌트 {len(lease)}대를 시세 표본에 넣을 것인가")
    print(_hr('━'))
    print(" 승계 매물의 표시 가격은 차값이 아니라 인수금입니다. 표본에 넣으면")
    print(" 시세선이 아래로 당겨져 멀쩡한 매물이 죄다 '고평가' 로 보일 수 있습니다.")
    print("")
    print(f" {'차종':<24}{'포함 잔존율':>12}{'제외 잔존율':>12}{'차이':>10}"
          f"{'포함 잔차':>10}{'제외 잔차':>10}{'잔차증가':>10}")
    big = False
    for key, target, group in groups:
        with_l = scoring.fit_market(group, key, target["label"])
        without = scoring.fit_market(
            [r for r in group if not scoring.is_lease_listing(r)], key, target["label"])
        a = history.reference_retention(with_l)
        b = history.reference_retention(without)
        if a is None or b is None:
            continue
        d = (b - a) * 100
        # 판단 기준은 잔존율의 이동이 아니라 '잔차' 다.
        # 인수금 매물은 시세선을 한쪽으로 끌기보다 위아래로 흩뿌리기
        # 때문에, 평균은 거의 그대로인데 산포만 커진다. 저평가 판정은
        # σ(=잔차)로 하므로 잔차가 커지면 진짜 저평가가 묻힌다.
        inflate = (with_l.resid_std / without.resid_std
                   if without.resid_std else 1.0)
        if inflate >= 1.20:
            big = True
        print(f" {target['label']:<24}{a*100:>11.1f}%{b*100:>11.1f}%"
              f"{d:>+9.2f}%p{with_l.resid_std*100:>9.2f}%p"
              f"{without.resid_std*100:>9.2f}%p{inflate:>8.2f}배")
    print("")
    if big:
        print(" ! 리스를 넣으면 잔차가 20% 이상 커집니다 — 인수금이 차값과 무관하게")
        print("   흩어지기 때문입니다. 평균은 그대로인데 산포만 커지므로 진짜")
        print("   저평가가 잡음에 묻힙니다. INCLUDE_LEASE_IN_BASELINE = False 를 권합니다.")
    else:
        print(" 잔차가 크게 나빠지지 않습니다 — 표본을 키우는 쪽(포함)도 괜찮습니다.")
    print(f"   현재 설정: INCLUDE_LEASE_IN_BASELINE = "
          f"{getattr(config, 'INCLUDE_LEASE_IN_BASELINE', True)}")
    print("   (순위·추천에서는 설정과 무관하게 항상 제외됩니다)")


def print_pooling(pool: dict, per_model: list) -> None:
    """차종을 합친 시세선이 나은지 따로가 나은지, 근거와 함께 보여준다."""
    if len(per_model) < 2:
        return
    print(f"\n{_hr('━')}")
    print(" 차종 통합 시세선 검토")
    print(_hr('━'))
    print(" 잔존율(가격/신차가)로 정규화했으니 브랜드가 달라도 원리상 한 줄로")
    print(" 세울 수 있습니다. 합치면 표본이 배로 늘어 계수가 안정됩니다.")
    print(" 다만 브랜드별 감가 속도가 다르면 합치는 순간 양쪽 다 틀린 선이 됩니다.")
    print("")
    for m, _g in per_model:
        print(f"   {m.label:<28} n={m.n:>3}  잔차 ±{m.resid_std*100:.2f}%p  "
              f"R²={m.r2:.3f}")
    sep, pooled = pool["sep_resid"], pool["pooled"]
    print(f"   {'따로 그린 두 선의 결합 잔차':<28}        ±{sep*100:.2f}%p")
    print(f"   {'합친 선':<28} n={pooled.n:>3}  잔차 ±{pooled.resid_std*100:.2f}%p  "
          f"R²={pooled.r2:.3f}")
    print("")
    print(f"   판단({pool['mode']}): {pool['verdict']}")


def print_brief(brief: dict) -> None:
    """맨 위 한 문단. 매주 여기만 읽어도 되게."""
    print(f"\n{_hr('━')}")
    print(f"  이번 주 결론:  {brief['headline']}")
    print(_hr('━'))
    for p_ in brief["picks"]:
        sg = p_["sigma"]
        print(f"   {p_['plate']}  {p_['model']} {p_['trim']}  "
              f"{p_['price']:,.0f}만원")
        print(f"      적정가 대비 {p_['gap']:+,.0f}만원 ({p_['pct']:+.1f}%, "
              f"{sg:+.2f}σ) — {p_['verdict']}")
        if p_.get("risk"):
            print(f"      주의: {p_['risk']}")
        if p_["why"]:
            print(f"      참고: {p_['why']}")
        if p_["url"]:
            print(f"      {p_['url']}")
    print(f"\n   지난주 대비:  " + " / ".join(brief["changes"]))
    print("   (아래는 상세입니다. 급하지 않으면 여기까지만 보셔도 됩니다.)")


def print_alerts(alerts: dict) -> None:
    """이번 주에 손댈 것만. 없으면 한 줄로 끝낸다."""
    A = config.ALERTS
    print(f"\n{_hr('━')}")
    print(" 이번 주 주목할 매물")
    print(_hr('━'))
    if not alerts.get("any"):
        print(" 없습니다. 아래 전체 순위는 참고용입니다.")
        return

    def _show(rows, title, note):
        if not rows:
            return
        print(f"\n {title}  ({len(rows)}건)")
        print(f"   {note}")
        for r in rows[:8]:
            sg = to_float(r.get("value_gap_sigma"))
            gap = to_float(r.get("value_gap_manwon"))
            pct = to_float(r.get("value_gap_pct"))
            dom = to_int(r.get("days_on_market"))
            chg = to_float(r.get("price_change_manwon"))
            bits = []
            if gap is not None:
                bits.append(f"{gap:+,.0f}만원")
            if pct is not None:
                bits.append(f"{pct:+.1f}%")
            if sg is not None:
                bits.append(f"{sg:+.2f}σ")
            if chg is not None:
                bits.append(f"가격 {chg:+,.0f}만원")
            if dom is not None:
                bits.append(f"보유 {dom}일")
            print(f"     {r.get('plate_no') or r.get('vehicle_id'):<11}"
                  f"{fmt_manwon(r.get('price_manwon')):>11}  "
                  f"{' · '.join(bits)}")
            if r.get("value_verdict"):
                print(f"       -> {r['value_verdict']}")
            if r.get("listing_url"):
                print(f"       {r['listing_url']}")

    _show(alerts["opportunity"],
          f"진짜 기회 — {A['opportunity_sigma']:.0f}σ 이상이고 싼 이유도 못 찾음",
          "가장 먼저 보세요. 통계적으로 유의미하고 할인 사유가 설명되지 않습니다.")
    _show(alerts["price_drop"],
          f"지난 실행 대비 {A['price_drop_manwon']:,}만원 이상 내림",
          "딜러가 값을 내렸다 = 안 팔리고 있다 = 협상이 열렸다는 뜻입니다.")
    _show(alerts["new_strong"],
          f"새로 올라온 매물 중 {A['new_listing_sigma']}σ 이상",
          "좋은 매물은 초기에 사라집니다.")
    _show(alerts["long_held"],
          f"딜러 보유 {A['days_on_market']}일 초과",
          "오래 안 팔린 매물입니다. 협상 여지가 크지만 남들이 지나쳤다는 신호이기도 합니다.")


def print_run_diff(diff: dict) -> None:
    """지난 실행과 무엇이 달라졌나.

    매주 돌리며 지켜보는 용도라, 전체 순위보다 '이번 주에 바뀐 것' 이
    먼저 보여야 한다.
    """
    if not diff.get("has_prev"):
        print(f"\n{_hr('━')}")
        print(" 지난 실행 기록이 없습니다 — 이번이 첫 수집입니다.")
        print(" 다음 실행부터 신규·가격인하·사라진 매물을 여기에 보여드립니다.")
        print(_hr('━'))
        return

    print(f"\n{_hr('━')}")
    print(f" 지난 실행({diff['prev_date']}) 이후 달라진 것")
    print(_hr('━'))

    def _line(r, extra=""):
        return (f"   {r.get('plate_no') or r.get('vehicle_id'):<11} "
                f"{r.get('model_label',''):<12} "
                f"{fmt_manwon(r.get('price_manwon')):>11}  "
                f"{fmt_km(r.get('mileage_km')):>11}  {extra}")

    if diff["price_down"]:
        print(f"\n 가격 내림 {len(diff['price_down'])}건  <- 먼저 보세요")
        for r in diff["price_down"][:12]:
            d = r["price_change_manwon"]
            print(_line(r, f"{d:+,}만원 (이전 {r['price_prev_manwon']:,}만원)"))
    if diff["new"]:
        print(f"\n 새로 올라온 매물 {len(diff['new'])}건")
        for r in diff["new"][:12]:
            print(_line(r))
    if diff["price_up"]:
        print(f"\n 가격 올림 {len(diff['price_up'])}건")
        for r in diff["price_up"][:6]:
            d = r["price_change_manwon"]
            print(_line(r, f"{d:+,}만원"))
    if diff["gone"]:
        print(f"\n 사라진 매물 {len(diff['gone'])}건 (팔렸거나 내렸습니다)")
        for r in diff["gone"][:12]:
            print(_line(r))
    print(f"\n 변동 없음 {diff['unchanged']}건")


def print_market_trend() -> None:
    """시세선 자체가 어느 쪽으로 움직이는가.

    한 매물이 싼지는 시세선으로 보지만, 시장 전체가 빠지는 중이라면
    지금 저평가인 차도 몇 주 뒤엔 평범한 가격이 된다.
    """
    trend = history.market_trend()
    rows = [t for t in trend if t.get("ref_retention") not in ("", None)]
    if len(rows) < 2:
        return
    print(f"\n{_hr('━')}")
    print(f" 시세 추이 — 기준점 {history.REFERENCE_AGE:.0f}년 / "
          f"{history.REFERENCE_KM:,}km 에서의 잔존율")
    print(_hr('━'))
    print(" 표본 구성이 주마다 달라지므로, 한 점을 정해 두고 그 점의 값을 비교합니다.")
    by_model: dict[str, list] = {}
    for t in rows:
        by_model.setdefault(t["model_key"], []).append(t)
    for key, ts in by_model.items():
        ts.sort(key=lambda x: x["date"])
        label = ts[-1].get("label") or key
        print(f"\n  {label}")
        prev = None
        for t in ts[-8:]:
            r = float(t["ref_retention"]) * 100
            mark = ""
            if prev is not None:
                d = r - prev
                mark = f"  {d:+.2f}%p" + ("  내림" if d < -0.05 else
                                           "  오름" if d > 0.05 else "")
            print(f"    {t['date']}  잔존율 {r:5.1f}%  (표본 {t['n']}대){mark}")
            prev = r


def print_top(rows, n, sort_label: str = "") -> None:
    print(f"\n{_hr('━')}")
    print(f" 저평가 상위 {min(n, len(rows))}대"
          + (f"  —  기준: {sort_label}" if sort_label else ""))
    print(_hr('━'))
    print(" 적정가 = 기준 시세에서 흠결(과주행·사고·배터리·이력)을 금액으로 뺀 값.")
    print(" 흠결이 있어도 그만큼 싸면 위로 올라옵니다.")
    print("")
    print(" 저평가를 세 가지로 함께 봅니다:")
    print("   만원  절대금액 — 실제로 아끼는 돈. 비싼 차일수록 크게 나옵니다.")
    print("   %     비율    — 싼 차일수록 크게 나옵니다.")
    print("   σ     유의성  — 시세선 자체의 오차로 나눈 값. 두 왜곡을 걷어냅니다.")
    print("         |σ|<1 이면 시세선 오차 범위 안이라 '싸다' 고 말하기 어렵습니다.")
    print("         2σ 를 넘으면 우연으로 보기 어려운 수준입니다.")
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
            pct = to_float(r.get("value_gap_pct"))
            sg = to_float(r.get("value_gap_sigma"))
            sigma = to_float(r.get("sigma_manwon"))
            verdict = "저평가 (기회)" if gap > 0 else "고평가"
            print(f"       적정가 {fair:,.0f}만원 / 판매가 {price:,.0f}만원"
                  f"  ->  {gap:+,.0f}만원 {verdict}")
            bits = []
            if pct is not None:
                bits.append(f"비율 {pct:+.1f}%")
            if sg is not None:
                bits.append(f"유의성 {sg:+.2f}σ")
            if sigma:
                bits.append(f"시세선 오차 ±{sigma:,.0f}만원")
            if bits:
                print(f"       {' · '.join(bits)}")
            if sg is not None and abs(sg) < 1.0:
                print("       ! 시세선 오차 범위 안입니다 — 통계적으로 "
                      "'싸다' 고 보기 어렵습니다")

        risks = scoring.risk_flags(r)
        if risks:
            for f in risks[:4]:
                mark = "!!" if f["level"] == "bad" else " !"
                print(f"       {mark} 주의: {f['label']} — {f['detail']}")

        verdict = r.get("value_verdict") or ""
        if verdict:
            mark = {"설명되지 않는 저평가": ">>", "일부 설명됨": " -",
                    "할인 이유 충분": " x", "고평가": "  "}.get(verdict, "  ")
            print(f"     {mark} [{verdict}] {r.get('value_verdict_note','')}")
        for piece in str(r.get("discount_extra") or "").split(" ; "):
            if piece and "=" in piece:
                lab, amt = piece.rsplit("=", 1)
                print(f"          싼 이유  {lab:<50} {amt:>10}")
        if r.get("battery_maker") or r.get("battery_risk") == "unknown":
            risk = r.get("battery_risk")
            tag = {"high": "!! 기피", "low": "선호", "normal": "보통",
                   "unknown": "확인 필요"}.get(risk, "")
            print(f"       배터리  {r.get('battery_maker') or '미확인'} [{tag}]"
                  + (f" — {r['battery_note']}" if r.get("battery_note") else ""))
        st = r.get("vin_option_state") or "unverified"
        if st == "verified":
            amt = to_float(r.get("vin_option_manwon")) or 0
            print(f"       [VIN 검증됨] 에어서스/후륜조향 확인 "
                  f"(+{amt:,.0f}만원, 출처 {r.get('vin_option_source') or '-'})")
        elif st == "verified_none":
            print("       [VIN 검증됨] 에어서스·후륜조향 없음 (감점 없음)")
        elif r.get("seller_option_claims"):
            print(f"       딜러 주장 옵션: {r['seller_option_claims']} (미검증)")
        if st == "unverified" and len(str(r.get("insp_vin") or "")) == 17:
            print(f"       >> VIN 확인 필요: {r['insp_vin']}")
        elif st == "unverified":
            print("       >> VIN 없음 — 딜러에게 차대번호 요청")
        if r.get("listing_signal"):
            print(f"       매물반응 {r['listing_signal']}")
            print(f"          {r.get('listing_signal_note','')}")
        if r.get("discount_notes"):
            for n in str(r["discount_notes"]).split(" ; "):
                if n:
                    print(f"       (참고) {n}")
        loan = to_int(r.get("loan_count"))
        if loan:
            print(f"       [확인필요] 저당 설정 있음 ({loan}건) — 계약 전 말소 확인 필수 "
                  f"(감점 아님)")
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
    p.add_argument("--sort", choices=["pct", "amount", "sigma"], default="pct",
                   help="순위 기준. pct=비율(기본), amount=절대금액(만원), "
                        "sigma=통계적 유의성")
    p.add_argument("--all-trims", action="store_true",
                   help="구매 후보 트림 필터를 끄고 전 트림을 순위에 넣는다 "
                        "(기본은 config.BUY_CANDIDATE_TRIMS 만)")
    p.add_argument("--include-lease", action="store_true",
                   help="리스·렌트 승계 매물도 순위에 포함 "
                        "(표시 가격이 인수금이라 기본은 제외)")
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
    for r in raw:
        for k in ("price_manwon", "year", "month", "mileage_km", "origin_price_manwon"):
            r[k] = to_int(r.get(k))
        rows.append(scoring.enrich(r))

    targets = {t["key"]: t for t in config.TARGETS}
    groups = []
    for key, target in targets.items():
        group = [r for r in rows if r.get("model_key") == key]
        if not group:
            warn(f"{target['label']}: 매물 0건 — 건너뜁니다.")
            continue
        groups.append((key, target, group))

    if not groups:
        die("점수를 매길 매물이 없습니다.")

    # 1) 차종별 시세선
    per_model = [(scoring.fit_baseline(g, k, t["label"]), g) for k, t, g in groups]

    # 2) 합쳐도 되는지 실제로 재 본다. 잔존율로 정규화했으니 브랜드가
    #    달라도 원리상 한 줄로 세울 수 있고, 합치면 표본이 배로 늘어난다.
    pool = scoring.compare_pooling(per_model, [r for _k, _t, g in groups for r in g])
    print_pooling(pool, per_model)
    print_lease_impact(groups)

    models, scored_all = [], []
    for (key, target, group), (m, _g) in zip(groups, per_model):
        market = pool["pooled"] if pool["use_pooled"] else m
        if pool["use_pooled"]:
            # 표시는 차종 이름으로 하되 계수는 통합선을 쓴다.
            market = copy.copy(market)
            market.key, market.label = key, target["label"] + " (통합 시세선)"
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

    def _sample_only(r) -> bool:
        return str(r.get("sample_only", "")).strip().lower() in ("true", "1")

    cand = [r for r in scored_all if _detailed(r) and not _excluded(r)]
    excluded = [r for r in scored_all if _detailed(r) and _excluded(r)]
    lease = [r for r in cand if _sample_only(r)]
    if args.include_lease:
        ranked, lease = cand, []
    else:
        ranked = [r for r in cand if not _sample_only(r)]

    # 실제로 살 트림만 순위에 남긴다. 나머지는 시세 회귀 표본으로만 쓴다
    # (표본이 많을수록 시세선이 정확해지므로 수집·회귀에서는 빼지 않는다).
    off_trim = []
    if not args.all_trims:
        off_trim = [r for r in ranked if not scoring.is_buy_candidate(r)]
        ranked = [r for r in ranked if scoring.is_buy_candidate(r)]
    skipped = len(scored_all) - len(cand) - len(excluded)

    if excluded:
        warn(f"후보 제외 {len(excluded)}건 (침수/전손·배터리팩 손상·골격C):")
        for r in excluded[:5]:
            warn(f"    {r.get('plate_no') or r.get('vehicle_id')} — {r.get('excluded_reason')}")
    if off_trim:
        want = ", ".join(getattr(config, "BUY_CANDIDATE_TRIMS", []))
        log(f"구매 후보 트림({want})만 순위에 남겼습니다 — "
            f"{len(off_trim)}건은 시세 표본으로만 사용합니다 "
            f"(--all-trims 로 전부 보기)")
    if lease:
        log(f"리스·렌트 승계 {len(lease)}건은 순위에서 제외했습니다 "
            f"(표시 가격이 인수금이라 차값과 같은 자로 못 잽니다). "
            f"시세 표본으로는 씁니다. --include-lease 로 포함할 수 있습니다.")
    if skipped:
        log(f"순위 대상 {len(ranked)}건 (상세 미확보 {skipped}건은 시세 표본으로만 사용)")

    # 저평가를 세 가지로 잰다. 기본은 σ — 시세선 자체의 오차로 나눈 값이라
    # 2억짜리와 5천짜리를 같은 자로 비교할 수 있다.
    SORT_KEYS = {
        "pct": ("value_gap_pct", "비율(%) — 적정가 대비 몇 % 싼가"),
        "amount": ("value_gap_manwon", "절대금액(만원)"),
        "sigma": ("value_gap_sigma", "통계적 유의성(σ)"),
    }
    sort_field, sort_label = SORT_KEYS[args.sort]

    def _key(r) -> float:
        v = to_float(r.get(sort_field))
        return v if v is not None else -9e9   # 산출 불가 매물은 맨 뒤로

    # 저평가가 '이유 있는 할인' 인지 '설명되지 않는 기회' 인지 가른다.
    # 이 도구의 핵심 질문이다 — 싼 차가 아니라 '이유 없이 싼 차' 를 찾는다.
    # 조회수·찜·보유기간 신호는 표본 전체를 봐야 중앙값을 낼 수 있다.
    scoring.add_listing_signals(scored_all)
    for r in scored_all:
        scoring.judge_value(r)

    ranked.sort(key=_key, reverse=True)
    log(f"순위 기준: {sort_label}")
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    for r in scored_all:
        r.setdefault("rank", "")

    write_csv(SCORED_CSV,
              ranked + off_trim + lease + excluded
              + [r for r in scored_all if not _detailed(r)],
              SCORED_FIELDS)
    market_dump = {}
    for m, _rs in models:
        d = dict(m.__dict__)
        # 기준점(3년/4.5만km) 잔존율 — 주마다 표본 구성이 달라도
        # 이 한 점을 비교하면 시장이 오르는지 내리는지 보인다.
        d["ref_retention"] = history.reference_retention(m)
        market_dump[m.key] = d
    write_json(MARKET_JSON, market_dump)
    history.record_markets(market_dump)
    log(f"저장: {SCORED_CSV} (순위 {len(ranked)}건 + 표본 {skipped}건)")

    diff = history.diff_runs(scored_all)
    alerts = scoring.build_alerts(ranked, diff)
    brief = scoring.weekly_brief(alerts, diff, ranked)
    print_brief(brief)
    print_alerts(alerts)
    print_run_diff(diff)
    print_market_trend()
    print_top(ranked, args.top, sort_label=sort_label)
    print_price_bands(ranked)

    if not args.no_report:
        html = report_mod.build_html(
            models, ranked[:max(args.top, 20)], stage="stage2",
            diff=diff, trend=history.market_trend(), alerts=alerts,
            brief=brief)
        with open(REPORT_HTML, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"저장: {REPORT_HTML}")

        # 날짜별 사본 & 요약 JSON (캐시 우회용)
        _today = _date.today().isoformat()

        _dated_html = os.path.join(os.path.dirname(REPORT_HTML),
                                   f"report_{_today}.html")
        shutil.copy(REPORT_HTML, _dated_html)
        log(f"저장: {_dated_html}")

        _diff = diff or {}
        _sum: dict = {
            "run_date": _today,
            "brief": {
                "headline": brief.get("headline", ""),
                "tone": brief.get("tone", ""),
                "changes": brief.get("changes", []),
            },
            "top_listings": [],
            "weekly_diff": {},
        }
        for _r in ranked[:max(args.top, 20)]:
            _flags = scoring.risk_flags(_r)
            _sum["top_listings"].append({
                "rank": to_int(_r.get("rank")),
                "plate_no": _r.get("plate_no", ""),
                "model_label": _r.get("model_label", ""),
                "trim": _r.get("trim", ""),
                "year": to_int(_r.get("year")),
                "month": to_int(_r.get("month")),
                "mileage_km": to_int(_r.get("mileage_km")),
                "price_manwon": to_int(_r.get("price_manwon")),
                "fair_price_manwon": to_int(_r.get("fair_price_manwon")),
                "value_gap_manwon": to_int(_r.get("value_gap_manwon")),
                "value_gap_pct": to_float(_r.get("value_gap_pct")),
                "value_gap_sigma": to_float(_r.get("value_gap_sigma")),
                "value_verdict": _r.get("value_verdict", ""),
                "risk_flags": [{"label": f["label"], "level": f["level"]}
                               for f in _flags],
                "vin_verified": _r.get("vin_option_state") == "verified",
                "listing_url": _r.get("listing_url", ""),
            })
        if _diff.get("has_prev"):
            _sum["weekly_diff"] = {
                "price_down": [
                    {"plate_no": _r.get("plate_no", ""),
                     "model_label": _r.get("model_label", ""),
                     "price_manwon": to_int(_r.get("price_manwon")),
                     "price_change_manwon": to_int(_r.get("price_change_manwon"))}
                    for _r in (_diff.get("price_down") or [])[:10]
                ],
                "new_listings": [
                    {"plate_no": _r.get("plate_no", ""),
                     "model_label": _r.get("model_label", ""),
                     "price_manwon": to_int(_r.get("price_manwon")),
                     "mileage_km": to_int(_r.get("mileage_km"))}
                    for _r in (_diff.get("new") or [])[:10]
                ],
                "gone_count": len(_diff.get("gone") or []),
            }
        _summary_path = os.path.join(os.path.dirname(REPORT_HTML),
                                     "data", f"summary_{_today}.json")
        write_json(_summary_path, _sum)
        log(f"저장: {_summary_path}")

    print(f"\n{_hr('━')}")
    print(f" 다음 단계: 위 상위 {args.top}대의 차량번호를 헤이딜러 앱 '숨은이력찾기'로")
    print(f" 조회한 뒤 스크린샷을 hidden/ 폴더에 넣고  `python merge.py --show`  실행")
    print(_hr('━'))
    return 0


if __name__ == "__main__":
    sys.exit(main())
