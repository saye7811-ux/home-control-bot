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
    row["score_total"] = round(total, 2)
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
    """헤이딜러 숨은이력 추출 결과를 반영해 최종 점수 재계산."""
    plus = [s for s in (row.get("reasons_plus") or "").split(" ; ") if s]
    minus = [s for s in (row.get("reasons_minus") or "").split(" ; ") if s]
    adj_total = 0.0

    # 1) 배터리 제조사
    maker = (hidden.get("battery_maker") or "").strip()
    row["hidden_battery_maker"] = maker
    maker_adj = 0.0
    if maker:
        maker_adj, _matched = battery_maker_adjust(maker)
        adj_total += maker_adj
        if maker_adj > 0:
            plus.append(f"배터리 제조사 {maker} ({maker_adj:+.0f}점)")
        elif maker_adj < 0:
            minus.append(f"배터리 제조사 {maker} ({maker_adj:+.0f}점)")
        else:
            plus.append(f"배터리 제조사 {maker} (중립)")
    row["adj_battery_maker"] = round(maker_adj, 2)

    # 2) 에어서스 확정 여부
    # 에어서스는 여기서 '처음으로' 확정된다. 1차 점수에는 들어가 있지 않으므로
    # 회수할 가점도 없다.
    air = hidden.get("airsus")           # True / False / None(불명)
    row["hidden_airsus"] = "" if air is None else bool(air)
    air_adj = 0.0
    if air is True:
        air_adj = config.AIRSUS_CONFIRMED_BONUS
        plus.append("에어서스 출고 장착 확정 (헤이딜러)")
    elif air is False:
        air_adj = -config.AIRSUS_ABSENT_PENALTY
        minus.append("에어서스 미장착 확정 (헤이딜러)")
        if _truthy(row.get("seller_airsus_mention")):
            minus.append("판매자 설명에는 에어서스가 언급돼 있었음 — 설명과 불일치")
    adj_total += air_adj
    row["adj_airsus"] = round(air_adj, 2)

    # 3) 보험 수리이력 금액
    #    2단계에서 엔카 record 로 이미 수리비 감점을 줬다면, 같은 보험개발원
    #    데이터를 두 번 깎는 셈이 된다. 헤이딜러 값이 더 정확하므로
    #    2단계 사고 감점을 되돌리고 헤이딜러 기준으로 다시 매긴다.
    cost = hidden.get("insurance_repair_won")
    cost = to_int(cost) if cost is not None else None
    row["hidden_insurance_won"] = cost if cost is not None else ""
    ins_adj, ins_label = insurance_adjust(cost)

    if cost is not None:
        stage2_acc = abs(to_float(row.get("penalty_accident"), 0.0) or 0.0)
        if stage2_acc:
            adj_total += stage2_acc          # 2단계 사고 감점 원복
            row["adj_accident_revert"] = round(stage2_acc, 2)
            notes_revert = f"2단계 사고 감점 {stage2_acc:.1f}점 원복 (헤이딜러 기준으로 재산정)"
            plus.append(notes_revert)
    adj_total += ins_adj
    row["adj_insurance"] = round(ins_adj, 2)
    if cost is not None:
        (plus if ins_adj > 0 else minus).append(
            f"보험 수리이력 {ins_label} ({cost:,}원)")

    row["hidden_insurance_summary"] = hidden.get("insurance_summary") or ""
    row["hidden_notes"] = hidden.get("notes") or ""
    row["hidden_source_image"] = hidden.get("source_image") or ""

    base = to_float(row.get("score_stage2"), 0.0) or 0.0
    row["hidden_adjust_total"] = round(adj_total, 2)
    row["score_total"] = round(base + adj_total, 2)
    row["reasons_plus"] = " ; ".join(plus)
    row["reasons_minus"] = " ; ".join(minus)
    return row
