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
    y, m = to_int(row.get("year")), to_int(row.get("month"))
    age = age_years(y, m)
    km = to_int(row.get("mileage_km"))

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

    # --- 가점 ---
    bonus = 0.0
    if _truthy(row.get("accident_free")):
        bonus += S["bonus"]["no_accident"]; plus.append("무사고")
    if _truthy(row.get("encar_diagnosed")):
        bonus += S["bonus"]["encar_diagnosed"]; plus.append("엔카진단 매물")
    if _truthy(row.get("one_owner")):
        bonus += S["bonus"]["one_owner"]; plus.append("1인 소유")
    if _truthy(row.get("has_airsus_keyword")):
        bonus += S["bonus"]["airsus_keyword"]
        plus.append(f"에어서스 추정 옵션 ({row.get('airsus_keyword_hits') or '키워드 일치'})")
    row["bonus_total"] = round(bonus, 2)

    # --- 감점 ---
    penalty = over_pen
    if _truthy(row.get("flood_or_total_loss")):
        penalty += S["penalty"]["flood_total_loss"]
        minus.append("침수/전손 이력 — 사실상 제외 대상")
    if _truthy(row.get("rental_or_commercial")):
        penalty += S["penalty"]["rental_commercial"]
        minus.append("렌트/영업용 이력")
    acc_my = to_int(row.get("accident_my_count"), 0) or 0
    acc_ot = to_int(row.get("accident_other_count"), 0) or 0
    if acc_my or acc_ot:
        minus.append(f"보험 사고이력 (내차 {acc_my}건 / 상대차 {acc_ot}건)")
    row["penalty_total"] = -round(penalty, 2)

    total = value_score + battery_score + bonus - penalty
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
    air = hidden.get("airsus")           # True / False / None(불명)
    row["hidden_airsus"] = "" if air is None else bool(air)
    air_adj = 0.0
    if air is True:
        air_adj = config.AIRSUS_CONFIRMED_BONUS
        plus.append("에어서스 출고 장착 확정")
    elif air is False:
        air_adj = -config.AIRSUS_ABSENT_PENALTY
        minus.append("에어서스 미장착 확정")
        # 1차에서 키워드로 준 가점은 회수
        if _truthy(row.get("has_airsus_keyword")):
            air_adj -= config.SCORING["bonus"]["airsus_keyword"]
            minus.append("1차 에어서스 추정 가점 회수")
    adj_total += air_adj
    row["adj_airsus"] = round(air_adj, 2)

    # 3) 보험 수리이력 금액
    cost = hidden.get("insurance_repair_won")
    cost = to_int(cost) if cost is not None else None
    row["hidden_insurance_won"] = cost if cost is not None else ""
    ins_adj, ins_label = insurance_adjust(cost)
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
