# -*- coding: utf-8 -*-
"""점수 계산 코어. score.py(2단계)와 merge.py(3단계)가 함께 쓴다.

점수 구성
---------
  시세 잔차       0 ~ 40점   회귀 예측가 대비 얼마나 싼가
  배터리 보증잔여 0 ~ 20점   8년 / 16만km 중 먼저 도달하는 쪽 기준
  가점            +          무사고 / 엔카진단 / 1인소유 / 에어서스
  감점            -          과주행 / 침수·전손 / 렌트·영업용
  (3단계) 숨은이력 ±         배터리 제조사 / 에어서스 확정 / 보험 수리이력
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

import config
from common import age_years, to_float, to_int


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# 시세 회귀
# ---------------------------------------------------------------------------
@dataclass
class MarketModel:
    key: str
    label: str
    n: int
    method: str                 # "regression" | "median"
    intercept: float = 0.0
    coef_age: float = 0.0       # 만원 / 년
    coef_km: float = 0.0        # 만원 / 1000km
    median_price: float = 0.0
    r2: float = float("nan")
    resid_std: float = float("nan")
    price_min: float = 0.0
    price_max: float = 0.0
    low_confidence: bool = True
    basis: str = "전체"            # "무사고" | "전체"
    n_clean: int = 0               # 무사고 표본 수
    accident_scale: float = 1.0    # 사고 할인에 곱할 비율

    def predict(self, age: float | None, km: int | None) -> float | None:
        if self.method == "median":
            return self.median_price or None
        if age is None or km is None:
            return None
        return self.intercept + self.coef_age * age + self.coef_km * (km / 1000.0)


def fit_market(rows: list[dict], key: str, label: str) -> MarketModel:
    """가격 ~ (경과연수, 주행거리) 최소제곱 회귀."""
    pts = []
    for r in rows:
        p = to_float(r.get("price_manwon"))
        a = to_float(r.get("age_years"))
        km = to_int(r.get("mileage_km"))
        if p and a is not None and km is not None and p > 0:
            pts.append((a, km / 1000.0, p))

    prices = [p for _, _, p in pts]
    median = float(np.median(prices)) if prices else 0.0
    base = dict(key=key, label=label, n=len(pts), median_price=median,
                price_min=min(prices) if prices else 0.0,
                price_max=max(prices) if prices else 0.0)

    # 표본이 적으면 회귀선이 과적합된다. 중앙값 기준으로 후퇴.
    if len(pts) < 5:
        return MarketModel(method="median", low_confidence=True, **base)

    X = np.array([[1.0, a, k] for a, k, _ in pts])
    y = np.array(prices)

    # 설계행렬이 rank deficient 면(연식·주행거리가 사실상 상수) 회귀 불가
    if np.linalg.matrix_rank(X) < 3:
        return MarketModel(method="median", low_confidence=True, **base)

    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    pred = X @ beta
    resid = y - pred
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    dof = max(len(pts) - 3, 1)

    return MarketModel(
        method="regression",
        intercept=float(beta[0]), coef_age=float(beta[1]), coef_km=float(beta[2]),
        r2=r2, resid_std=float(math.sqrt(ss_res / dof)),
        low_confidence=len(pts) < 8,
        **base,
    )


# ---------------------------------------------------------------------------
# 개별 매물 점수
# ---------------------------------------------------------------------------
def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "y", "yes", "예")


def enrich(row: dict) -> dict:
    """연식/주행거리에서 파생 지표를 채운다 (회귀 적합 전에 호출)."""
    from common import age_years_from_date, parse_date

    y, m = to_int(row.get("year")), to_int(row.get("month"))
    km = to_int(row.get("mileage_km"))

    # 배터리 보증 8년의 기준일은 '최초등록일' 이다. 연식(yyyyMM)과 몇 달씩
    # 차이날 수 있으므로 실제 등록일이 있으면 그것을 쓴다.
    first_reg = parse_date(row.get("first_registration_date"))
    if first_reg is not None:
        age = age_years_from_date(first_reg)
        row["age_basis"] = f"최초등록일 {first_reg}"
    else:
        age = age_years(y, m)
        row["age_basis"] = f"연식 {y}.{str(m or '').zfill(2)} (최초등록일 응답에 없음)"

    row["age_years"] = round(age, 2) if age is not None else ""
    if age is not None and km is not None:
        row["annual_km"] = int(km / max(age, 0.5))
    else:
        row["annual_km"] = ""

    # 신차가 대비 감가율 (category.originPrice 가 있을 때만)
    origin = to_int(row.get("origin_price_manwon"))
    price = to_float(row.get("price_manwon"))
    if origin and price and origin > 0:
        row["depreciation_pct"] = round((1 - price / origin) * 100, 1)
    else:
        row["depreciation_pct"] = ""

    # 배터리 보증 잔여: 8년 / 16만km 중 먼저 도달하는 쪽
    w_yrs = config.BATTERY_WARRANTY["years"]
    w_km = config.BATTERY_WARRANTY["km"]
    if age is not None:
        yrs_left = max(0.0, w_yrs - age)
        row["battery_years_left"] = round(yrs_left, 2)
        frac_t = _clamp(yrs_left / w_yrs, 0.0, 1.0)
    else:
        row["battery_years_left"] = ""
        frac_t = None
    if km is not None:
        km_left = max(0, w_km - km)
        row["battery_km_left"] = km_left
        frac_k = _clamp(km_left / w_km, 0.0, 1.0)
    else:
        row["battery_km_left"] = ""
        frac_k = None

    if frac_t is None and frac_k is None:
        row["battery_remaining_pct"] = ""
        row["battery_binding"] = ""
    else:
        cands = [(f, name) for f, name in ((frac_t, "기간(8년)"), (frac_k, "주행(16만km)"))
                 if f is not None]
        frac, binding = min(cands)
        row["battery_remaining_pct"] = round(frac * 100, 1)
        row["battery_binding"] = binding
    return row


# ---------------------------------------------------------------------------
# 적정가 산출 — 흠결을 금액으로 환산한다
# ---------------------------------------------------------------------------
def _rank_pct(rank: str, status: str) -> float:
    """사고 등급 + 상태부호 -> 차값 대비 할인율(%)."""
    lo, hi = config.PRICING["accident_rank_pct"].get(rank, (0.0, 0.0))
    _label, weight = config.INSPECTION_STATUS.get(status, (status, 0.5))
    return lo + (hi - lo) * weight


def compute_fair_price(row: dict, market: MarketModel) -> dict:
    """기준 시세에서 흠결을 금액으로 빼 적정가를 만든다.

    반환에는 항목별 내역(breakdown)과, 값을 몰라서 반영하지 못한 항목
    목록(unknowns)이 함께 들어간다. 모르는 항목을 0 으로 처리하면
    '흠결 없음' 과 구분되지 않는다.
    """
    P = config.PRICING
    age = to_float(row.get("age_years"))
    km = to_int(row.get("mileage_km"))
    price = to_float(row.get("price_manwon"))

    out = {"breakdown": [], "unknowns": [], "fair_price_manwon": "",
           "value_gap_manwon": "", "baseline_manwon": ""}
    if age is None or km is None or not price:
        out["unknowns"].append("연식/주행거리/가격 결측 — 적정가 산출 불가")
        return out

    # 1) 기준 시세 — 같은 연식, '평균' 주행거리일 때의 시세
    expected_km = int(age * P["expected_annual_km"])
    baseline = market.predict(age, expected_km)
    if baseline is None:
        out["unknowns"].append("시세 회귀 불가 — 적정가 산출 불가")
        return out
    out["baseline_manwon"] = round(baseline, 0)
    out["expected_km"] = expected_km
    out["breakdown"].append(
        (f"기준 시세 (동일 연식 · 평균주행 {expected_km:,}km)", round(baseline, 0)))

    fair = baseline

    # 2) 주행거리 — 회귀식의 km 계수를 그대로 쓴다
    at_actual = market.predict(age, km)
    mil_adj = (at_actual - baseline) if at_actual is not None else 0.0
    if not P.get("allow_low_mileage_premium", True):
        mil_adj = min(mil_adj, 0.0)
    if abs(mil_adj) >= 1:
        diff = km - expected_km
        label = ("과주행" if diff > 0 else "주행거리 적음")
        out["breakdown"].append(
            (f"{label} {diff/10000:+.1f}만km", round(mil_adj, 0)))
        fair += mil_adj

    # 3) 사고이력 — 등급 기반 %와 수리비 기반 중 하나를 고른다
    known_record = str(row.get("record_available", "")).strip().lower() in ("true", "1", "y")
    rank = str(row.get("insp_worst_rank") or "")
    status = str(row.get("insp_worst_status") or "R")
    my_cost_won = to_int(row.get("accident_my_cost_won"))
    my_n = to_int(row.get("accident_my_count"))
    ot_n = to_int(row.get("accident_other_count"))

    rank_amt = (baseline * _rank_pct(rank, status) / 100.0) if rank else 0.0
    cost_amt = ((my_cost_won / 10000.0) * P["accident_cost_multiplier"]
                if my_cost_won else 0.0)

    if P.get("accident_combine") == "sum":
        acc_amt = rank_amt + cost_amt
    else:
        acc_amt = max(rank_amt, cost_amt)
    # 전체 매물 기준선을 쓰면 그 선에 이미 사고 영향이 섞여 있으므로 낮춘다
    acc_amt *= getattr(market, "accident_scale", 1.0)

    if acc_amt >= 1:
        bits = []
        if my_n or ot_n:
            bits.append(f"내차 {my_n or 0}건 / 타차 {ot_n or 0}건")
        if my_cost_won:
            bits.append(f"수리비 {my_cost_won/10000:,.0f}만원")
        if rank:
            bits.append(f"{config.INSPECTION_RANKS[rank]['label']}")
        out["breakdown"].append(("사고이력 " + " · ".join(bits), -round(acc_amt, 0)))
        fair -= acc_amt

    if not known_record:
        # 모르는 것을 무사고로 치지 않는다. 보수적으로 깎고 사실을 남긴다.
        unk = baseline * P["unknown_record_pct"] / 100.0
        out["breakdown"].append(("보험이력 미확인 (보수적 반영)", -round(unk, 0)))
        out["unknowns"].append("보험이력을 확인하지 못했습니다 — 실제 사고이력이 "
                               "있다면 적정가는 더 낮아집니다")
        fair -= unk
    elif not rank and (my_n or ot_n):
        out["unknowns"].append("사고는 있으나 수리 부위를 모릅니다 "
                               "(점검자 코멘트에 부위 언급 없음)")

    # 4) 배터리 보증 잔여 부족
    B = P["battery"]
    frac = to_float(row.get("battery_remaining_pct"))
    if frac is None:
        out["unknowns"].append("배터리 보증 잔여를 계산하지 못했습니다")
    elif frac < B["reference_remaining_pct"]:
        months = (B["reference_remaining_pct"] - frac) / 100.0 * 96.0
        amt = months * B["manwon_per_month"]
        out["breakdown"].append(
            (f"배터리 보증 잔여 부족 ({frac:.0f}% · 기준 {B['reference_remaining_pct']:.0f}%)",
             -round(amt, 0)))
        fair -= amt

    # 5) 소유/용도 이력
    owners = to_int(row.get("owner_change_count"))
    if owners is None:
        out["unknowns"].append("소유자 변경 횟수 정보없음")
    elif owners > 1:
        amt = baseline * P["owner_change_pct_per_extra"] / 100.0 * (owners - 1)
        out["breakdown"].append((f"소유자 변경 {owners}회", -round(amt, 0)))
        fair -= amt

    if str(row.get("past_commercial_use")) == "True":
        amt = baseline * P["past_commercial_pct"] / 100.0
        out["breakdown"].append(("과거 대여·영업용 등록", -round(amt, 0)))
        fair -= amt
    elif row.get("past_commercial_use") in ("", None):
        out["unknowns"].append("과거 용도 이력 정보없음")

    if str(row.get("rental_or_commercial")) == "True":
        amt = baseline * P["current_lease_pct"] / 100.0
        out["breakdown"].append(("현재 리스·렌트 매물", -round(amt, 0)))
        fair -= amt

    # 6) 고전원전기장치 불량 — 전기차 핵심 안전 항목
    ev_bad = [x for x in str(row.get("page_ev_hv_bad") or "").split(", ") if x]
    ev_unknown = [x for x in str(row.get("page_ev_hv_unknown") or "").split(", ") if x]
    if ev_bad:
        amt = P["ev_hv_bad_manwon"] * len(ev_bad)
        out["breakdown"].append((f"고전원전기장치 불량 ({', '.join(ev_bad)})",
                                 round(amt, 0)))
        fair += amt
    if ev_unknown:
        # 판정 불가를 '불량' 으로 치지 않는다. 감점 없이 사실만 남긴다.
        out["unknowns"].append(f"고전원전기장치 판정 불가: {', '.join(ev_unknown)[:120]}")
    elif not ev_bad and not to_int(row.get("page_ev_hv_checked")):
        out["unknowns"].append("고전원전기장치 점검 결과 없음 (전기차 핵심 항목)")

    # 7) 자동차 세부상태 불량
    det_unknown = [x for x in str(row.get("page_detail_unknown") or "").split(", ") if x]
    if det_unknown:
        out["unknowns"].append(f"세부상태 판정 불가: {', '.join(det_unknown)[:120]}")
    det_bad = [x for x in str(row.get("page_detail_bad") or "").split(", ") if x]
    if det_bad:
        amt = P["detail_bad_manwon"] * len(det_bad)
        out["breakdown"].append((f"세부상태 불량 ({', '.join(det_bad)})", round(amt, 0)))
        fair += amt

    out["breakdown"].append(("= 적정가", round(fair, 0)))
    out["fair_price_manwon"] = round(fair, 0)
    out["value_gap_manwon"] = round(fair - price, 0)
    return out


def _is_accident_free(r: dict) -> bool:
    """보험이력으로 '확인된' 무사고만 True. 모르면 False."""
    if str(r.get("record_available", "")).strip().lower() not in ("true", "1", "y"):
        return False
    my = to_int(r.get("accident_my_count"))
    ot = to_int(r.get("accident_other_count"))
    if my is None and ot is None:
        return False
    return not ((my or 0) or (ot or 0))


def fit_baseline(rows: list[dict], key: str, label: str) -> MarketModel:
    """기준 시세선을 그린다.

    전체 매물로 그린 선에는 사고차의 가격 하락이 이미 섞여 있다. 거기서
    사고 할인을 또 빼면 같은 흠결을 두 번 반영하게 된다. 무사고 매물만으로
    그리면 '무사고 기준선' 이 되어 사고 할인을 온전히 빼도 된다.

    표본이 모자라면 전체 기준선을 쓰되 사고 할인 계수를 낮춘다.
    """
    B = config.BASELINE
    clean = [r for r in rows if _is_accident_free(r)]
    m_all = fit_market(rows, key, label)
    m_clean = fit_market(clean, key, label) if clean else None

    mode = B.get("mode", "auto")
    enough = (m_clean is not None and m_clean.method == "regression"
              and m_clean.n >= B.get("min_clean_samples", 8))

    use_clean = (mode == "accident_free" and m_clean is not None) or \
                (mode == "auto" and enough)

    if use_clean:
        m = m_clean
        m.basis = "무사고"
        m.accident_scale = B.get("accident_scale_when_clean", 1.0)
    else:
        m = m_all
        m.basis = "전체"
        m.accident_scale = B.get("accident_scale_when_all", 0.6)
    m.n_clean = len(clean)
    return m


def score_row(row: dict, market: MarketModel, target: dict) -> dict:
    """1차(2단계) 점수 산출. row 를 제자리에서 갱신하고 반환한다."""
    S = config.SCORING
    plus: list[str] = []
    minus: list[str] = []

    price = to_float(row.get("price_manwon"))
    age = to_float(row.get("age_years"))
    km = to_int(row.get("mileage_km"))

    # --- 시세 잔차 ---
    pred = market.predict(age, km)
    if pred and price:
        row["predicted_price_manwon"] = round(pred, 1)
        row["residual_manwon"] = round(price - pred, 1)      # 음수 = 시세보다 쌈
        value_pct = (pred - price) / pred * 100.0            # 양수 = 저평가
        row["value_pct"] = round(value_pct, 2)
        span = S["value_pct_span"]
        value_score = _clamp((value_pct + span) / (2 * span), 0.0, 1.0) * S["value_max_pts"]
        if value_pct >= 5:
            plus.append(f"시세 대비 {value_pct:.1f}% 저평가 (예측 {pred:,.0f}만원)")
        elif value_pct <= -5:
            minus.append(f"시세 대비 {abs(value_pct):.1f}% 비쌈 (예측 {pred:,.0f}만원)")
    else:
        row["predicted_price_manwon"] = ""
        row["residual_manwon"] = ""
        row["value_pct"] = ""
        value_score = S["value_max_pts"] * 0.5   # 정보 없음 → 중립
        minus.append("시세 비교 불가 (연식/주행거리 결측)")
    row["score_value"] = round(value_score, 2)

    # --- 배터리 보증 잔여 ---
    frac = to_float(row.get("battery_remaining_pct"))
    if frac is None:
        battery_score = S["battery_max_pts"] * 0.5
    else:
        battery_score = frac / 100.0 * S["battery_max_pts"]
        yl = to_float(row.get("battery_years_left")) or 0
        kl = to_int(row.get("battery_km_left")) or 0
        if frac >= 55:
            plus.append(f"배터리 보증 여유 ({yl:.1f}년 / {kl:,}km 남음, "
                        f"{row.get('battery_binding')} 기준)")
        elif frac <= 25:
            minus.append(f"배터리 보증 잔여 부족 ({yl:.1f}년 / {kl:,}km, "
                         f"{row.get('battery_binding')} 기준)")
    row["score_battery"] = round(battery_score, 2)

    # --- 신차가 대비 감가율 ---
    # 같은 모델 안에서는 시세 잔차와 겹치지만, 모델이 다른 매물을 한 줄로
    # 세울 때는 이쪽이 실질적인 진입가 차이를 보여준다. 그래서 배점은 작다.
    dep = to_float(row.get("depreciation_pct"))
    lo, hi = S["depreciation_span"]
    if dep is None:
        dep_score = S["depreciation_max_pts"] * 0.5      # 신차가 정보 없음 → 중립
    else:
        dep_score = _clamp((dep - lo) / (hi - lo), 0.0, 1.0) * S["depreciation_max_pts"]
        if dep >= hi:
            plus.append(f"신차가 대비 {dep:.0f}% 감가 (진입가 낮음)")
        elif dep <= lo:
            minus.append(f"신차가 대비 감가 {dep:.0f}% 에 그침")
    row["score_depreciation"] = round(dep_score, 2)

    # --- 주행거리 조작 의심 ---
    # 계기판은 되감지 않는 한 늘기만 한다. 성능점검은 매물 등록보다 앞서므로
    # 성능점검 km 가 표시 km 보다 크면 조작을 의심해야 한다.
    M = getattr(config, "MILEAGE_ROLLBACK", {})
    # 성능기록부 페이지의 주행거리를 우선한다 (API 보다 정확)
    insp_km = to_int(row.get("page_mileage"))
    if insp_km is None:
        insp_km = to_int(row.get("insp_mileage"))
    shown_km = to_int(row.get("mileage_km"))
    row["mileage_gap_km"] = ""
    rollback_pen = 0.0
    if insp_km is not None and shown_km is not None:
        gap = insp_km - shown_km
        row["mileage_gap_km"] = gap
        if gap > M.get("tolerance_km", 100):
            rollback_pen = M.get("penalty", 35.0)
            minus.append(
                f"주행거리 조작 의심 — 성능점검 {insp_km:,}km 인데 매물 표시 "
                f"{shown_km:,}km ({gap:,}km 적게 표시)")
    row["penalty_mileage_rollback"] = -round(rollback_pen, 2)

    # --- 과주행 ---
    annual = to_int(row.get("annual_km"))
    over_pen = 0.0
    if annual is not None:
        if annual > config.ANNUAL_KM_BAD:
            over_pen = S["penalty"]["overrun_30k"]
            minus.append(f"과주행 심함 (연 {annual:,}km)")
        elif annual > config.ANNUAL_KM_WARN:
            over_pen = S["penalty"]["overrun_25k"]
            minus.append(f"과주행 (연 {annual:,}km)")
    row["penalty_overrun"] = -round(over_pen, 2)

    # --- 사고이력 상세 ---
    A = S["accident"]
    acc_pen = 0.0
    my_n = to_int(row.get("accident_my_count"))
    ot_n = to_int(row.get("accident_other_count"))
    my_cost = to_int(row.get("accident_my_cost_won"))
    known = str(row.get("record_available", "")).strip().lower() in ("true", "1", "y")

    if not known or (my_n is None and ot_n is None):
        # 정보가 없으면 '무사고' 로 치지 않는다. 모른다는 것 자체가 위험이다.
        acc_pen += S["penalty"]["unknown_record"]
        minus.append("보험이력을 확인하지 못했습니다 (응답에 없음)")
        row["accident_summary"] = "확인 불가"
    elif not (my_n or 0) and not (ot_n or 0):
        row["accident_summary"] = "무사고"
    else:
        bits = []
        if my_n:
            acc_pen += my_n * A["my_per_case"]
            bits.append(f"내차 피해 {my_n}건")
        if ot_n:
            acc_pen += ot_n * A["other_per_case"]
            bits.append(f"타차 가해 {ot_n}건")
        if my_cost is not None and my_cost > 0:
            for upper, pen, label in A["cost_tiers"]:
                if my_cost <= upper:
                    acc_pen += pen
                    bits.append(f"내차 수리비 {label} ({my_cost:,}원)")
                    break
        elif my_n:
            bits.append("수리비 금액 응답에 없음")
        row["accident_summary"] = " / ".join(bits)
        minus.append("사고이력: " + row["accident_summary"])
        for line in [x for x in str(row.get("accident_lines") or "").split(" | ") if x]:
            minus.append("  · " + line)

    # 수리 부위 — 성능점검기록부 법정 등급으로 차등 감점.
    #
    # 주의: '무사고' 표기를 그대로 믿으면 안 된다. 외판 1랭크(후드·펜더·도어·
    # 트렁크리드) 교환은 법적으로 무사고로 표기되므로, 무사고 매물이라도
    # 실제 수리 부위는 반드시 보여준다.
    insp_pen = to_float(row.get("insp_repair_penalty"))
    notes = str(row.get("insp_repair_notes") or "")
    if insp_pen is not None:
        acc_pen += insp_pen
        for nt in [x for x in notes.split(" | ") if x]:
            minus.append(nt)
        if not notes:
            plus.append("성능점검상 수리 부위 없음")
    else:
        row["repair_kind_note"] = "성능점검 응답에 없음 — 수리 부위 등급 판정 불가"

    if str(row.get("insp_unclassified") or ""):
        minus.append(f"미분류 부위: {row['insp_unclassified']} (등급표에 없는 이름)")

    row["penalty_accident"] = -round(acc_pen, 2)

    # --- 가점 ---
    bonus = 0.0
    if known and _truthy(row.get("accident_free")):
        bonus += S["bonus"]["no_accident"]; plus.append("무사고 (보험이력 확인)")
    if _truthy(row.get("encar_diagnosed")):
        bonus += S["bonus"]["encar_diagnosed"]; plus.append("엔카진단 매물")
    if _truthy(row.get("one_owner")):
        bonus += S["bonus"]["one_owner"]; plus.append("1인 소유")
    row["bonus_total"] = round(bonus, 2)

    # 에어서스는 점수에 넣지 않는다. 엔카의 옵션 목록/판매자 설명은 딜러가
    # 쓴 홍보 문구라 누락과 과장이 흔하다 (실측 반례 있음). 참고로만 남기고
    # 실제 판정은 3단계 헤이딜러 출고 기록에서 한다.
    row["seller_airsus_mention"] = _truthy(row.get("has_airsus_keyword"))

    # --- 감점 ---
    penalty = over_pen + acc_pen + rollback_pen
    if _truthy(row.get("past_commercial_use")):
        penalty += S["penalty"]["past_commercial_use"]
        bits = []
        for k, lab in (("past_rental_count", "대여용"), ("past_business_count", "영업용"),
                       ("past_government_count", "관용")):
            n = to_int(row.get(k))
            if n:
                bits.append(f"{lab} {n}회")
        minus.append("과거 용도 이력: " + (", ".join(bits) or "있음"))
    excluded_reasons = []
    gauge = str(row.get("page_mileage_gauge") or "")
    if gauge == "불량":
        excluded_reasons.append("주행거리 계기상태 불량 (성능기록부)")
    elif not gauge and row.get("page_available"):
        # 판정 불가를 불량으로 치지 않는다 — 멀쩡한 차를 걸러내면 안 된다
        minus.append("주행거리 계기상태 판정 불가 (성능기록부 표기 확인 필요)")
    gap = to_int(row.get("mileage_gap_km"))
    if gap is not None and gap > M.get("exclude_over_km", 5000):
        excluded_reasons.append(f"주행거리 불일치 {gap:,}km")
    if _truthy(row.get("insp_waterlog")):
        excluded_reasons.append("성능점검 침수 표기")
    if _truthy(row.get("flood_or_total_loss")):
        penalty += S["penalty"]["flood_total_loss"]
        excluded_reasons.append("침수/전손 이력")
    if _truthy(row.get("battery_pack_damage")):
        excluded_reasons.append("고전압 배터리팩 손상")
    if str(row.get("insp_worst_rank")) == "골격C":
        excluded_reasons.append("골격 C랭크 수리 (대시패널/플로어패널)")

    if excluded_reasons and getattr(config, "HARD_EXCLUDE_ON_FLOOD_TOTAL_LOSS", True):
        row["excluded"] = True
        row["excluded_reason"] = " / ".join(excluded_reasons)
        minus.append("후보 제외: " + row["excluded_reason"])
    else:
        row["excluded"] = False
        row["excluded_reason"] = ""
    if _truthy(row.get("rental_or_commercial")):
        penalty += S["penalty"]["rental_commercial"]
        minus.append("렌트/영업용 이력")
    row["penalty_total"] = -round(penalty, 2)

    total = value_score + battery_score + dep_score + bonus - penalty
    row["score_stage2"] = round(total, 2)
    row["score_points"] = round(total, 2)

    # --- 적정가 환산 (주 판단 지표) ---
    fp = compute_fair_price(row, market)
    row["baseline_manwon"] = fp.get("baseline_manwon", "")
    row["fair_price_manwon"] = fp.get("fair_price_manwon", "")
    row["value_gap_manwon"] = fp.get("value_gap_manwon", "")
    _bd = fp.get("breakdown", [])
    row["price_breakdown"] = " || ".join(
        f"{lab}={amt:,.0f}" if (i == 0 or i == len(_bd) - 1) else f"{lab}={amt:+,.0f}"
        for i, (lab, amt) in enumerate(_bd))
    row["price_unknowns"] = " ; ".join(fp.get("unknowns", []))
    row["fair_gap_stage2"] = fp.get("value_gap_manwon", "")
    row["score_total"] = fp.get("value_gap_manwon", "") or 0
    row["reasons_plus"] = " ; ".join(plus)
    row["reasons_minus"] = " ; ".join(minus)
    row["market_confidence"] = "낮음(표본부족)" if market.low_confidence else "보통"
    return row


# ---------------------------------------------------------------------------
# 3단계: 헤이딜러 숨은이력 반영
# ---------------------------------------------------------------------------
def _norm_maker(s: str) -> str:
    return (s or "").upper().replace(" ", "").replace("-", "").replace("_", "")


def battery_maker_adjust(maker: str) -> tuple[float, str]:
    key = _norm_maker(maker)
    if not key:
        return 0.0, ""
    for k, adj in config.BATTERY_MAKER_ADJ.items():
        if _norm_maker(k) and _norm_maker(k) in key:
            return adj, k
    return 0.0, maker


def insurance_adjust(total_won: int | None) -> tuple[float, str]:
    if total_won is None:
        return 0.0, "보험 수리이력 정보 없음"
    for upper, adj, label in config.INSURANCE_TIERS:
        if total_won <= upper:
            return adj, label
    return config.INSURANCE_TIERS[-1][1], config.INSURANCE_TIERS[-1][2]


def apply_hidden(row: dict, hidden: dict) -> dict:
    """헤이딜러 숨은이력을 적정가에 금액으로 반영하고 최종 판단을 다시 낸다."""
    H = config.HIDDEN_PRICING
    plus = [x for x in (row.get("reasons_plus") or "").split(" ; ") if x]
    minus = [x for x in (row.get("reasons_minus") or "").split(" ; ") if x]
    unknowns = [x for x in (row.get("price_unknowns") or "").split(" ; ") if x]

    fair = to_float(row.get("fair_price_manwon"))
    price = to_float(row.get("price_manwon"))
    extra: list[tuple[str, float]] = []
    adj_total = 0.0

    # 1) 배터리 제조사
    maker = (hidden.get("battery_maker") or "").strip()
    row["hidden_battery_maker"] = maker
    maker_adj = 0.0
    if maker:
        key = _norm_maker(maker)
        for k, v in H["battery_maker_manwon"].items():
            if _norm_maker(k) and _norm_maker(k) in key:
                maker_adj = v
                break
        if maker_adj:
            extra.append((f"배터리 제조사 {maker}", maker_adj))
            (plus if maker_adj > 0 else minus).append(
                f"배터리 제조사 {maker} ({maker_adj:+,.0f}만원)")
        else:
            plus.append(f"배터리 제조사 {maker} (중립)")
        adj_total += maker_adj
    else:
        unknowns.append("배터리 제조사 미확인")
    row["adj_battery_maker"] = round(maker_adj, 1)

    # 2) 에어서스 — 여기서 처음 확정된다
    air = hidden.get("airsus")
    row["hidden_airsus"] = "" if air is None else bool(air)
    air_adj = 0.0
    if air is True:
        air_adj = H["airsus_present_manwon"]
        plus.append("에어서스 출고 장착 확정 (헤이딜러)")
    elif air is False:
        air_adj = H["airsus_absent_manwon"]
        extra.append(("에어서스 미장착 확정", air_adj))
        minus.append(f"에어서스 미장착 확정 ({air_adj:+,.0f}만원)")
        if str(row.get("seller_airsus_mention")) == "True":
            minus.append("판매자 설명에는 에어서스가 언급돼 있었음 — 설명과 불일치")
    else:
        unknowns.append("에어서스 장착 여부 미확인")
    adj_total += air_adj
    row["adj_airsus"] = round(air_adj, 1)

    # 3) 보험 수리이력 — 2단계에서 엔카 record 로 이미 반영한 금액을 대체한다.
    #    같은 보험개발원 데이터라 두 번 깎으면 안 된다.
    cost = hidden.get("insurance_repair_won")
    cost = to_int(cost) if cost is not None else None
    row["hidden_insurance_won"] = cost if cost is not None else ""
    ins_adj = 0.0
    if cost is not None:
        stage2 = 0.0
        for part in (row.get("price_breakdown") or "").split(" || "):
            if part.startswith("사고이력"):
                try:
                    stage2 = abs(float(part.rsplit("=", 1)[1].replace(",", "")))
                except ValueError:
                    stage2 = 0.0
        hidden_amt = cost / 10000.0 * H["insurance_cost_multiplier"]
        ins_adj = stage2 - hidden_amt      # 2단계분 원복 후 헤이딜러 기준 적용
        extra.append((f"보험 수리이력 {cost:,}원 (헤이딜러 기준으로 재산정)", ins_adj))
        (plus if ins_adj > 0 else minus).append(
            f"보험 수리이력 {cost:,}원 ({ins_adj:+,.0f}만원 조정)")
        adj_total += ins_adj
    else:
        unknowns.append("보험 수리이력 미확인")
    row["adj_insurance"] = round(ins_adj, 1)

    row["hidden_insurance_summary"] = hidden.get("insurance_summary") or ""
    row["hidden_notes"] = hidden.get("notes") or ""
    row["hidden_source_image"] = hidden.get("source_image") or ""
    row["hidden_adjust_total"] = round(adj_total, 1)

    if fair is not None:
        new_fair = fair + adj_total
        bd = [x for x in (row.get("price_breakdown") or "").split(" || ")
              if not x.startswith("= 적정가")]
        for lab, amt in extra:
            bd.append(f"{lab}={amt:+,.0f}")
        bd.append(f"= 적정가(헤이딜러 반영)={new_fair:,.0f}")
        row["price_breakdown"] = " || ".join(bd)
        row["fair_price_manwon"] = round(new_fair, 0)
        if price is not None:
            row["value_gap_manwon"] = round(new_fair - price, 0)
            row["score_total"] = row["value_gap_manwon"]

    row["reasons_plus"] = " ; ".join(plus)
    row["reasons_minus"] = " ; ".join(minus)
    row["price_unknowns"] = " ; ".join(dict.fromkeys(unknowns))
    return row
