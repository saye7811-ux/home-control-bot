# -*- coding: utf-8 -*-
"""report.html 생성. 외부 CDN 없이 인라인 SVG 로 차트를 그린다."""

from __future__ import annotations

import html
from datetime import datetime

import config

from common import fmt_km, fmt_manwon, to_float, to_int

CHECKLIST = [
    ("성능기록부 원본 요구 (사진뿐인 매물)", "리포트에서 '성능기록부 사진뿐' 으로 "
     "표시된 매물은 수리 부위·고전원전기장치·주행거리 계기상태를 하나도 확인할 수 "
     "없다. 무사고라서 비어 있는 것이 아니라 읽을 데이터가 없는 것이다. "
     "계약 전에 딜러에게 성능·상태점검기록부 원본(또는 구조화된 페이지)을 반드시 "
     "요구하고, 못 받으면 그 매물은 후보에서 빼는 것이 안전하다. "
     "이 항목을 먼저 처리해야 나머지 확인이 의미가 있다."),
    ("배터리 SOH 진단", "서비스센터 또는 사설 진단으로 SOH(잔존용량) 측정. "
     "iX는 BMW 진단기(ISTA), EQE는 XENTRY 기준 수치를 요청할 것. 90% 미만이면 재협상 근거."),
    ("고전압 배터리 보증 승계 확인", "최초등록일 기준 8년/16만km 잔여분이 실제로 승계되는지 "
     "제조사 고객센터에 차대번호로 직접 확인 (수입차는 이력에 따라 제한될 수 있음)."),
    ("에어서스펜션 실차 확인", "시동 후 차고 조절 동작, 주차 후 한쪽으로 주저앉음 여부, "
     "컴프레서 소음. 옵션표기만 믿지 말 것."),
    ("충전 실측", "급속 충전기에서 실제 충전 속도(kW)와 충전 곡선 확인. "
     "완속 완충 후 표시 주행가능거리 확인."),
    ("하부/언더커버 육안", "침수 흔적(부식, 진흙), 배터리 팩 하부 스크래치·찍힘 여부."),
    ("타이어 4본 제조주차", "전기차는 타이어 마모가 빠름. 4본 교체 시 100만원 이상 추가 비용."),
    ("성능점검기록부 원본 대조", "엔카 표기와 실제 기록부가 일치하는지 대조. "
     "특히 교환 부위가 볼트온(범퍼·펜더·도어)인지 판금·용접이 필요한 골격"
     "(사이드멤버·필러·대시패널·플로어)인지 확인. 골격 수리는 가치 하락이 크다."),
    ("누유·부식 하부 점검", "리프트에 올려 엔진·감속기 오일 누유, 하부 부식, "
     "배터리 팩 하우징 상태 확인. 성능점검 표기만 믿지 말 것."),
    ("타이어 4본 잔여 트레드", "전기차는 마모가 빠르다. 트레드 4mm 미만이면 "
     "4본 교체 비용(100만원 이상)을 가격 협상에 반영."),
    ("저당(담보) 말소 확인", "자동차등록원부 을구에 저당권이 남아 있으면 명의 이전이 "
     "막힌다. 잔금 전에 말소 완료를 등록원부로 직접 확인할 것. 딜러가 처리하는 것이 "
     "일반적이지만 확인은 사는 쪽 몫이다. 리포트에 '저당 설정 있음' 이 뜬 매물은 특히."),
    ("보험개발원 카히스토리 직접 조회", "헤이딜러 결과와 교차 검증. 소유자 변경 횟수와 용도이력 확인."),
    ("리콜/캠페인 미이행 확인", "차대번호로 제조사 리콜 조회. 미이행 건 인수 전 처리 요구."),
    ("실차 시승", "회생제동 단계별 작동, 경고등, 에어컨/히트펌프, 12V 배터리 상태."),
]


def _e(v) -> str:
    return html.escape(str(v if v is not None else ""))


# ---------------------------------------------------------------------------
# SVG 산점도
# ---------------------------------------------------------------------------
def _scatter_svg(rows: list[dict], market) -> str:
    W, H = 620, 340
    ML, MR, MT, MB = 62, 18, 18, 46
    pw, ph = W - ML - MR, H - MT - MB

    pts = []
    for r in rows:
        km = to_int(r.get("mileage_km"))
        pr = to_float(r.get("price_manwon"))
        if km is None or pr is None:
            continue
        pts.append((km, pr, to_float(r.get("value_pct")) or 0.0, r))
    if not pts:
        return '<p class="muted">차트를 그릴 데이터가 없습니다.</p>'

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = 0, max(xs) * 1.08 or 1
    y_pad = (max(ys) - min(ys)) * 0.15 or max(ys) * 0.1 or 1
    y0, y1 = min(ys) - y_pad, max(ys) + y_pad

    def sx(v): return ML + (v - x0) / (x1 - x0) * pw
    def sy(v): return MT + ph - (v - y0) / (y1 - y0) * ph

    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
           f'aria-label="주행거리 대비 가격 분포">']
    out.append(f'<rect x="{ML}" y="{MT}" width="{pw}" height="{ph}" fill="var(--grid-bg)"/>')

    # 격자 + 눈금
    for i in range(5):
        gy = MT + ph * i / 4
        val = y1 - (y1 - y0) * i / 4
        out.append(f'<line x1="{ML}" y1="{gy:.1f}" x2="{ML+pw}" y2="{gy:.1f}" '
                   f'stroke="var(--grid)" stroke-width="1"/>')
        out.append(f'<text x="{ML-8}" y="{gy+4:.1f}" text-anchor="end" '
                   f'class="tick">{val:,.0f}</text>')
    for i in range(5):
        gx = ML + pw * i / 4
        val = x0 + (x1 - x0) * i / 4
        out.append(f'<line x1="{gx:.1f}" y1="{MT}" x2="{gx:.1f}" y2="{MT+ph}" '
                   f'stroke="var(--grid)" stroke-width="1"/>')
        out.append(f'<text x="{gx:.1f}" y="{MT+ph+18}" text-anchor="middle" '
                   f'class="tick">{val/10000:.1f}만</text>')

    # 회귀선 (중앙 연식 기준 단면)
    if market and market.method in ("regression", "ratio", "absolute"):
        ages = [to_float(r.get("age_years")) for r in rows if to_float(r.get("age_years"))]
        # 잔존율 모델은 신차가가 있어야 금액이 나온다. 트림이 섞여 있으므로
        # '중앙 신차가' 단면을 그린다 (점 하나하나는 각자의 신차가로 평가된다).
        origins = [to_float(r.get("origin_price_manwon")) for r in rows
                   if to_float(r.get("origin_price_manwon"))]
        med_origin = sorted(origins)[len(origins) // 2] if origins else None
        if ages:
            med_age = sorted(ages)[len(ages) // 2]
            ax, bx = x0 + (x1 - x0) * 0.02, x1 * 0.98
            ay = market.predict(med_age, ax, med_origin)
            by = market.predict(med_age, bx, med_origin)
            if ay and by:
                out.append(
                    f'<line x1="{sx(ax):.1f}" y1="{sy(ay):.1f}" '
                    f'x2="{sx(bx):.1f}" y2="{sy(by):.1f}" '
                    f'stroke="var(--line)" stroke-width="2" stroke-dasharray="6 4"/>')

    # 점: 저평가(파랑) ~ 고평가(빨강)
    for km, pr, vpct, r in pts:
        c = "var(--good)" if vpct >= 5 else "var(--bad)" if vpct <= -5 else "var(--mid)"
        title = (f"{r.get('plate_no') or r.get('vehicle_id')} · "
                 f"{fmt_manwon(pr)} · {fmt_km(km)} · 시세대비 {vpct:+.1f}%")
        out.append(f'<circle cx="{sx(km):.1f}" cy="{sy(pr):.1f}" r="5.5" fill="{c}" '
                   f'fill-opacity="0.78" stroke="var(--dot-stroke)" stroke-width="1">'
                   f'<title>{_e(title)}</title></circle>')

    out.append(f'<text x="{ML+pw/2:.0f}" y="{H-6}" text-anchor="middle" '
               f'class="axis">주행거리 (km)</text>')
    out.append(f'<text x="14" y="{MT+ph/2:.0f}" transform="rotate(-90 14 {MT+ph/2:.0f})" '
               f'text-anchor="middle" class="axis">가격 (만원)</text>')
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------
# 조각들
# ---------------------------------------------------------------------------
def _market_card(market, rows: list[dict]) -> str:
    prices = [to_float(r.get("price_manwon")) for r in rows if to_float(r.get("price_manwon"))]
    kms = [to_int(r.get("mileage_km")) for r in rows if to_int(r.get("mileage_km")) is not None]
    med = sorted(prices)[len(prices) // 2] if prices else 0
    med_km = sorted(kms)[len(kms) // 2] if kms else 0

    if market.method == "ratio":
        fit = (f"잔존율 ≈ {market.intercept*100:,.1f}% "
               f"{market.coef_age*100:+,.1f}%p×경과연수 "
               f"{market.coef_km*100:+,.3f}%p×(주행거리/1000)  ·  R²={market.r2:.2f}"
               f"  ·  잔차 ±{market.resid_std*100:.2f}%p")
    elif market.method == "absolute":
        fit = (f"가격 ≈ {market.intercept:,.0f} "
               f"{market.coef_age:+,.0f}×경과연수 "
               f"{market.coef_km:+,.1f}×(주행거리/1000)  ·  R²={market.r2:.2f}"
               f"  ·  잔차 ±{market.resid_std:,.0f}만원")
    else:
        fit = "표본이 5건 미만이라 회귀 대신 중앙값 기준으로 비교했습니다."

    extra = ""
    if market.method == "ratio":
        extra += ('<p class="muted">트림이 섞여 있어 가격이 아니라 '
                  '<b>잔존율(가격÷신차가)</b>을 회귀했습니다. 적정가는 '
                  '잔존율 × 그 매물의 신차가입니다 — 비싼 트림이 '
                  '&lsquo;고평가&rsquo;로 잘못 잡히는 것을 막습니다.</p>')
    if market.n_dropped:
        extra += f'<p class="muted">이상치 제외: {_e(market.dropped_note)}</p>'
    if market.n_lease:
        extra += (f'<p class="muted">시세 표본에 리스·렌트 승계 {market.n_lease}대가 '
                  f'포함돼 있습니다 (순위에서는 제외).</p>')

    warn = ('<p class="warn">표본이 적어 시세선의 신뢰도가 낮습니다. '
            '잔차 점수를 절대적으로 믿지 마세요.</p>') if market.low_confidence else ""

    return f"""
    <div class="card">
      <h3>{_e(market.label)}</h3>
      <div class="stats">
        <div><span class="k">매물 수</span><span class="v">{len(rows)}대</span></div>
        <div><span class="k">가격 중앙값</span><span class="v">{fmt_manwon(med)}</span></div>
        <div><span class="k">가격 범위</span><span class="v">{fmt_manwon(market.price_min)} ~ {fmt_manwon(market.price_max)}</span></div>
        <div><span class="k">주행 중앙값</span><span class="v">{fmt_km(med_km)}</span></div>
      </div>
      <p class="fit">{_e(fit)}</p>
      {extra}
      {warn}
      {_scatter_svg(rows, market)}
      <p class="muted">점선 = 중앙 연식({_e(market.label)}) 기준 시세선. 파랑=저평가, 빨강=고평가.</p>
    </div>"""


def _gap_cell(r: dict) -> str:
    """적정가 대비 차액을 세 가지로 보여준다.

    절대금액만 보면 비싼 차가 늘 위로 오고, 비율만 보면 싼 차가 늘 위로
    온다. σ 는 시세선 자체의 오차로 나눈 값이라 둘을 같은 자로 잰다.
    |σ|<1 이면 시세선 오차 범위 안이라 '싸다' 고 말하기 어렵다.
    """
    v = to_float(r.get("value_gap_manwon"))
    if v is None:
        return '<span class="muted small">산출 불가</span>'
    pct = to_float(r.get("value_gap_pct"))
    sg = to_float(r.get("value_gap_sigma"))
    sigma = to_float(r.get("sigma_manwon"))
    cls = "good" if v > 0 else "bad"
    out = [f'<span class="gap {cls}">{v:+,.0f}</span>'
           f'<div class="muted small">만원</div>']
    if pct is not None:
        out.append(f'<div class="gap-sub {cls}">{pct:+.1f}%</div>')
    if sg is not None:
        weak = ' weak' if abs(sg) < 1.0 else ''
        out.append(f'<div class="gap-sig{weak}">{sg:+.2f}&sigma;</div>')
        if abs(sg) < 1.0:
            out.append('<div class="muted small">시세선 오차 범위 안</div>')
    if sigma:
        out.append(f'<div class="muted small">&plusmn;{sigma:,.0f}만원</div>')
    return "".join(out)


VERDICT_CLASS = {
    "설명되지 않는 저평가": "v-gold",
    "일부 설명됨": "v-warm",
    "할인 이유 충분": "v-cool",
    "고평가": "v-flat",
}


def _verdict_html(r: dict) -> str:
    """'왜 싼가' — 이 도구의 핵심 판정.

    적정가가 이미 사고·과주행 같은 흠결을 깎았는데도 더 싸다면, 그 이유가
    따로 있는지 본다. 성능기록부가 사진뿐이거나, 자차보험 미가입 기간이
    있거나, 몇 달째 안 팔리고 있다면 그럴 만해서 싼 것이다.
    """
    v = r.get("value_verdict") or ""
    if not v:
        return '<span class="muted small">판정 불가</span>'
    cls = VERDICT_CLASS.get(v, "v-flat")
    out = [f'<div class="verdict {cls}">{_e(v)}</div>',
           f'<div class="muted small">{_e(r.get("value_verdict_note") or "")}</div>']

    extra = [x for x in str(r.get("discount_extra") or "").split(" ; ") if "=" in x]
    if extra:
        rows = []
        for piece in extra:
            lab, amt = piece.rsplit("=", 1)
            rows.append(f'<tr><td>{_e(lab)}</td><td class="num">{_e(amt)}</td></tr>')
        out.append('<div class="small" style="margin-top:6px"><b>싼 이유로 설명되는 것</b></div>')
        out.append(f'<table class="bd">{"".join(rows)}</table>')
    else:
        out.append('<div class="muted small" style="margin-top:6px">'
                   '적정가에 안 들어간 할인 사유를 찾지 못했습니다.</div>')

    dom = to_int(r.get("days_on_market"))
    if dom is not None:
        basis = r.get("days_on_market_basis") or ""
        out.append(f'<div class="muted small">딜러 보유 {dom}일 ({_e(basis)})</div>')

    # 저당은 감점 항목이 아니다. 계약 전 말소하면 끝나는 절차 문제라
    # 매물 가치와 직접 관련이 없다. 다만 놓치면 명의 이전이 막힌다.
    loan = to_int(r.get("loan_count"))
    if loan:
        out.append(f'<div class="flag-check">저당 설정 있음 ({loan}건) '
                   f'— 계약 전 말소 확인 필수</div>')
    return "".join(out)


def _breakdown_html(r: dict) -> str:
    """적정가 산출 내역을 항목별 금액으로 펼친다."""
    raw = str(r.get("price_breakdown") or "")
    items = [x for x in raw.split(" || ") if "=" in x]
    if not items:
        return '<span class="muted small">-</span>'
    rows = []
    for i, item in enumerate(items):
        lab, amt = item.rsplit("=", 1)
        last = i == len(items) - 1
        cls = "bd-total" if last else ("bd-minus" if amt.strip().startswith("-") else "")
        rows.append(f'<tr class="{cls}"><td>{_e(lab)}</td>'
                    f'<td class="num">{_e(amt)}</td></tr>')
    rows.append(f'<tr class="bd-price"><td>판매가</td>'
                f'<td class="num">{fmt_manwon(r.get("price_manwon"))}</td></tr>')
    return f'<table class="bd">{"".join(rows)}</table>'


def _rank_rows(rows: list[dict], stage: str) -> str:
    out = []
    for i, r in enumerate(rows, 1):
        plate = r.get("plate_no") or "(차량번호 미확보)"
        photo = r.get("photo_url") or ""
        url = r.get("listing_url") or ""
        plus = [s for s in (r.get("reasons_plus") or "").split(" ; ") if s]
        minus = [s for s in (r.get("reasons_minus") or "").split(" ; ") if s]

        # 엔카의 에어서스 언급은 딜러 코멘트라 점수에 넣지 않는다. 참고 표시만.
        ref = []

        # 성능점검 — 있으면 표시, 없으면 없다고 표시
        insp = [r.get("insp_leak"), r.get("insp_corrosion"), r.get("insp_tire")]
        if any(insp):
            ref.append('<div class="ref">※ 성능점검: '
                       + _e(" / ".join(x for x in insp if x))[:160] + '</div>')
        else:
            ref.append('<div class="ref warnref">※ 성능점검(누유·부식·타이어): '
                       '응답에 없음 — 실차에서 직접 확인 필요</div>')

        # 무사고 표기라도 실제 수리 부위는 반드시 보여준다.
        # 외판 1랭크 교환은 법적으로 무사고로 표기되기 때문이다.
        if r.get("page_ev_hv_bad"):
            ref.append('<div class="ref warnref"><b>※ 고전원전기장치 불량: '
                       + _e(r["page_ev_hv_bad"]) + ' (전기차 핵심 안전 항목)</b></div>')
        elif to_int(r.get("page_ev_hv_checked")):
            ref.append('<div class="ref">※ 고전원전기장치 3항목 양호</div>')
        if r.get("page_detail_bad"):
            ref.append('<div class="ref warnref">※ 세부상태 불량: '
                       + _e(r["page_detail_bad"]) + '</div>')
        if r.get("page_mileage_gauge"):
            ref.append(f'<div class="ref">※ 주행거리 계기상태: '
                       f'{_e(r["page_mileage_gauge"])}</div>')
        elif r.get("page_available"):
            ref.append('<div class="ref warnref">※ 주행거리 계기상태 판정 불가 '
                       '— 성능기록부 표기 직접 확인 필요</div>')
        if r.get("page_ev_hv_unknown"):
            ref.append('<div class="ref warnref">※ 고전원전기장치 판정 불가: '
                       + _e(r["page_ev_hv_unknown"])[:140] + '</div>')
        if r.get("page_status_unknown"):
            ref.append('<div class="ref warnref">※ 상태 부호를 못 읽은 부위: '
                       + _e(r["page_status_unknown"])[:120] + '</div>')
        if str(r.get("page_js_suspect")) == "True":
            ref.append('<div class="ref warnref">※ 성능기록부 수리 부위가 '
                       '비어 있으나 JS 로 채워지는 구조로 보입니다 — 부위 정보 미반영</div>')
        if r.get("page_unmatched_parts"):
            ref.append('<div class="ref warnref">※ 성능기록부에서 못 알아본 부위: '
                       + _e(r["page_unmatched_parts"]) + '</div>')

        if r.get("insp_repair_notes"):
            src = r.get("repair_grade_source") or r.get("repair_source") or "점검자 코멘트"
            ref.append(f'<div class="ref">※ 수리 부위({_e(src)}): '
                       + _e(str(r["insp_repair_notes"]).replace(" | ", " · "))[:320]
                       + '</div>')
        elif r.get("insp_comments"):
            ref.append('<div class="ref">※ 점검자 코멘트에 수리 부위 언급 없음</div>')
        else:
            ref.append('<div class="ref warnref">※ 수리 부위 확인 불가 '
                       '(성능점검 코멘트 없음) — 기록부 원본 대조 필요</div>')

        if r.get("insp_unclassified"):
            ref.append('<div class="ref warnref">※ 코멘트에서 못 알아본 단어: '
                       + _e(r["insp_unclassified"])
                       + ' (구어체 표현 추가 필요)</div>')

        # 주행거리 조작 — 가장 눈에 띄어야 한다
        gap = to_int(r.get("mileage_gap_km"))
        if gap is not None and gap > 100:
            ref.append('<div class="ref warnref"><b>※ 주행거리 조작 의심: '
                       f'성능점검 {to_int(r.get("insp_mileage")) or 0:,}km / '
                       f'매물 표시 {to_int(r.get("mileage_km")) or 0:,}km '
                       f'({gap:,}km 차이)</b></div>')

        if r.get("insp_needs_repair"):
            ref.append('<div class="ref warnref">※ 지금 수리 필요: '
                       + _e(r["insp_needs_repair"])[:160] + '</div>')
        if r.get("insp_comments"):
            ref.append('<div class="ref">※ 점검자 코멘트: '
                       + _e(r["insp_comments"])[:220] + '</div>')
        # 리콜은 감점 대상이 아니다. 대상이어도 이행했으면 문제가 아니고,
        # 미이행이면 인수 전 처리하면 된다. 표시만 한다.
        if r.get("page_recall"):
            done = r.get("page_recall_done") or ""
            ref.append(f'<div class="ref">※ 리콜: {_e(r["page_recall"])}'
                       + (f" / 이행여부 {_e(done)}" if done else "")
                       + ' (감점 대상 아님)</div>')
        elif r.get("insp_recall") not in ("", None):
            rc = "대상" if str(r["insp_recall"]) == "True" else "해당없음"
            ref.append(f'<div class="ref">※ 리콜: {_e(rc)} (감점 대상 아님)</div>')
        if r.get("insp_diagnostics"):
            ref.append('<div class="ref">※ 자기진단: '
                       + _e(r["insp_diagnostics"])[:140] + '</div>')
        if r.get("insp_usage_change"):
            ref.append('<div class="ref warnref">※ 용도 변경 이력: '
                       + _e(r["insp_usage_change"])[:120] + '</div>')
        if r.get("accident_type_verdict") and "불일치" in str(r["accident_type_verdict"]):
            ref.append('<div class="ref warnref">※ 사고 유형 코드 '
                       + _e(r["accident_type_verdict"])[:180] + '</div>')

        for u in [x for x in str(r.get("price_unknowns") or "").split(" ; ") if x]:
            ref.append('<div class="ref warnref">※ 정보없음: ' + _e(u) + '</div>')

        if r.get("use_history"):
            ref.append('<div class="ref">※ 용도 이력: '
                       + _e(str(r["use_history"]))[:120] + '</div>')

        basis = str(r.get("age_basis") or "")
        if "응답에 없음" in basis:
            ref.append('<div class="ref warnref">※ 최초등록일이 응답에 없어 '
                       '연식으로 배터리 보증을 계산했습니다 (수개월 오차 가능)</div>')

        hidden_bits = []
        if stage == "final":
            if r.get("hidden_battery_maker"):
                hidden_bits.append(f"배터리 {r['hidden_battery_maker']}")
            if r.get("hidden_airsus") not in ("", None):
                hidden_bits.append("에어서스 O" if str(r["hidden_airsus"]) == "True"
                                   else "에어서스 X")
            if r.get("hidden_insurance_won") not in ("", None):
                hidden_bits.append(f"수리이력 {to_int(r['hidden_insurance_won']) or 0:,}원")
        hidden_html = (f'<div class="hidden-tag">숨은이력: {_e(" · ".join(hidden_bits))}</div>'
                       if hidden_bits else "")

        links = []
        if url:
            links.append(f'<a href="{_e(url)}" target="_blank" rel="noopener">매물</a>')
        if photo:
            links.append(f'<a href="{_e(photo)}" target="_blank" rel="noopener">사진</a>')

        # 성능기록부가 사진뿐이면 행 전체를 표시해 둔다. 수리 이력을
        # 아예 모르는 매물이라 다른 매물과 같은 눈으로 보면 안 된다.
        photo_only = str(r.get("page_is_image", "")).strip().lower() in ("true", "1")
        row_cls = ' class="row-photo"' if photo_only else ""
        photo_flag = ('<div class="flag-photo">성능기록부 사진뿐 — 원본 요구 필수</div>'
                      if photo_only else "")

        out.append(f"""
      <tr{row_cls}>
        <td class="rank">{i}</td>
        <td>
          <div class="plate">{_e(plate)}</div>
          <div class="muted small">{_e(r.get('model_label'))} · {_e(r.get('trim'))}</div>
          {photo_flag}
          {hidden_html}
        </td>
        <td class="num"><b>{fmt_manwon(r.get('price_manwon'))}</b>
          <div class="muted small">적정가 {fmt_manwon(r.get('fair_price_manwon'))}</div>
          {f'<div class="muted small">신차가 대비 -{_e(r.get("depreciation_pct"))}%</div>' if r.get('depreciation_pct') not in ('', None) else ''}</td>
        <td class="num">{_e(r.get('year'))}.{_e(str(r.get('month') or '').zfill(2))}
          <div class="muted small">{fmt_km(r.get('mileage_km'))}</div></td>
        <td class="num">{_e(r.get('annual_km') and f"{to_int(r.get('annual_km')):,}" or '-')}
          <div class="muted small">km/년</div></td>
        <td class="num">{_e(r.get('battery_remaining_pct'))}%
          <div class="muted small">{_e(r.get('battery_binding'))}</div></td>
        <td class="num">{_gap_cell(r)}</td>
        <td class="vcell">{_verdict_html(r)}</td>
        <td class="bdcell">{_breakdown_html(r)}</td>
        <td class="reasons">
          {"".join(f'<div class="p">＋ {_e(s)}</div>' for s in plus) or '<div class="muted">-</div>'}
          {"".join(f'<div class="m">－ {_e(s)}</div>' for s in minus)}
          {"".join(ref)}
        </td>
        <td class="links">{" ".join(links) or "-"}</td>
      </tr>""")
    return "".join(out)


CSS = """
:root{--bg:#f7f7f5;--fg:#1c1c1a;--muted:#6b6b66;--card:#fff;--bd:#e2e2dd;
--good:#2563eb;--bad:#dc2626;--mid:#9ca3af;--line:#7c3aed;--grid:#ececE6;
--grid-bg:#fbfbf9;--dot-stroke:#fff;--plate-bg:#111;--plate-fg:#ffd400;--warnbg:#fff7ed;--warnfg:#9a3412}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#16161a;--fg:#ececec;--muted:#9a9a94;--card:#1e1e23;--bd:#33333a;
--grid:#2c2c33;--grid-bg:#1a1a1f;--dot-stroke:#1e1e23;--mid:#6b7280;
--warnbg:#3a2a12;--warnfg:#fdba74}}
:root[data-theme=dark]{--bg:#16161a;--fg:#ececec;--muted:#9a9a94;--card:#1e1e23;
--bd:#33333a;--grid:#2c2c33;--grid-bg:#1a1a1f;--dot-stroke:#1e1e23;--mid:#6b7280;
--warnbg:#3a2a12;--warnfg:#fdba74}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 12px;
padding-bottom:6px;border-bottom:1px solid var(--bd)}h3{font-size:15px;margin:0 0 10px}
.sub{color:var(--muted);margin:0 0 8px}
.banner{background:var(--warnbg);color:var(--warnfg);border:1px solid var(--bd);
border-radius:8px;padding:10px 14px;margin:14px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px}
.stats{display:flex;flex-wrap:wrap;gap:16px;margin-bottom:8px}
.stats .k{color:var(--muted);display:block;font-size:12px}
.stats .v{font-weight:600}
.fit{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted);margin:4px 0 8px}
.warn{color:var(--warnfg);background:var(--warnbg);padding:6px 10px;border-radius:6px;font-size:13px}
.tick{font-size:10px;fill:var(--muted)}.axis{font-size:11px;fill:var(--muted)}
.tablewrap{overflow-x:auto;border:1px solid var(--bd);border-radius:10px;background:var(--card)}
table{border-collapse:collapse;width:100%;min-width:1360px}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--bd);
vertical-align:top;word-break:break-word}
th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}
td.num,th.num{text-align:right;white-space:nowrap}
td.rank{font-weight:700;color:var(--muted);width:34px}
td.links,th.links{width:56px}
.plate{display:inline-block;background:var(--plate-bg);color:var(--plate-fg);
font-weight:800;letter-spacing:1px;padding:6px 14px;border-radius:6px;
font-size:20px;font-family:ui-monospace,Menlo,monospace;
border:2px solid var(--plate-fg)}
.hidden-tag{margin-top:5px;font-size:12px;color:var(--muted)}
.gap{font-size:19px;font-weight:800}
.gap.good{color:var(--good)}.gap.bad{color:var(--bad)}
.gap-sub{font-size:13px;font-weight:700;margin-top:2px}
.gap-sub.good{color:var(--good)}.gap-sub.bad{color:var(--bad)}
.gap-sig{font-size:13px;font-weight:800;margin-top:3px;
         padding:1px 6px;border-radius:999px;display:inline-block;
         background:var(--good);color:#fff}
.gap-sig.weak{background:transparent;color:var(--muted);
              border:1px solid var(--border);font-weight:600}
.vcell{max-width:280px}
.verdict{display:inline-block;padding:3px 9px;border-radius:6px;
         font-weight:800;font-size:13px;margin-bottom:4px}
.v-gold{background:#b45309;color:#fff}
.v-warm{background:#a16207;color:#fff}
.v-cool{background:var(--border);color:var(--muted)}
.v-flat{background:transparent;color:var(--muted);border:1px solid var(--border)}
.flag-check{margin-top:6px;padding:4px 8px;border-radius:6px;font-size:12px;
            font-weight:700;background:rgba(180,83,9,.14);color:#b45309;
            border:1px solid rgba(180,83,9,.35)}
.flag-photo{margin-top:6px;padding:5px 9px;border-radius:6px;font-size:12px;
            font-weight:800;background:#7c2d12;color:#fff}
.alerts{margin:22px 0 8px}
.alerts.none{padding:16px 18px;border:1px dashed var(--border);border-radius:10px}
.alerts.none h2{margin:0 0 4px;font-size:17px}
.alert-card{border:1px solid var(--border);border-radius:10px;padding:14px 16px;
            margin:10px 0;background:var(--card)}
.alert-card h3{margin:0 0 2px;font-size:15px}
.alert-card.a-gold{border-left:5px solid #b45309}
.alert-card.a-drop{border-left:5px solid var(--good)}
.alert-card.a-new{border-left:5px solid #2563eb}
.alert-card.a-held{border-left:5px solid var(--muted)}
.alist{margin:8px 0 0;padding-left:18px}
.alist li{margin:6px 0;line-height:1.5}
tr.row-photo td{background:rgba(124,45,18,.06)}
.bdcell{min-width:250px}
table.bd{width:100%;border-collapse:collapse;font-size:11.5px;min-width:auto}
table.bd td{padding:2px 4px;border:0;vertical-align:top}
table.bd td:first-child{white-space:normal;line-height:1.35}
table.bd td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
table.bd tr.bd-minus td{color:var(--bad)}
table.bd tr.bd-total td{border-top:1px solid var(--bd);font-weight:700;padding-top:4px}
table.bd tr.bd-price td{color:var(--muted)}
.reasons{font-size:12.5px;max-width:330px}
.reasons .p{color:var(--good)}.reasons .m{color:var(--bad)}
.reasons .ref{color:var(--muted);font-size:11.5px;margin-top:5px;
padding-top:4px;border-top:1px dashed var(--bd)}
.reasons .warnref{color:var(--warnfg)}
.banner.ok{background:transparent;border:1px solid var(--good);color:inherit}
.links a{color:var(--good);text-decoration:none;margin-right:8px;white-space:nowrap}
.links a:hover{text-decoration:underline}
.muted{color:var(--muted)}.small{font-size:12px}
ol.check{padding-left:20px}ol.check li{margin:10px 0}
ol.check b{display:block}ol.check span{color:var(--muted);font-size:13px}
footer{margin-top:40px;color:var(--muted);font-size:12px}
"""


def _diff_section(diff: dict | None) -> str:
    """지난 실행 이후 달라진 것. 매주 지켜보는 용도라 맨 위에 온다."""
    if not diff:
        return ""
    if not diff.get("has_prev"):
        return ('<h2>지난 실행과 비교</h2><div class="card">'
                '<p class="muted">이번이 첫 수집입니다. 다음 실행부터 새로 올라온 매물, '
                '가격이 내린 매물, 사라진 매물을 여기에 보여드립니다.</p></div>')

    def _tbl(rows, cols, empty):
        if not rows:
            return f'<p class="muted">{empty}</p>'
        head = "".join(f"<th>{c}</th>" for c, _ in cols)
        body = "".join(
            "<tr>" + "".join(f"<td>{fn(r)}</td>" for _, fn in cols) + "</tr>"
            for r in rows[:15])
        return (f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead>'
                f'<tbody>{body}</tbody></table></div>')

    def _plate(r):
        return _e(r.get("plate_no") or r.get("vehicle_id") or "?")

    def _model(r):
        return _e(r.get("model_label") or "")

    def _price(r):
        return fmt_manwon(r.get("price_manwon"))

    def _km(r):
        return fmt_km(r.get("mileage_km"))

    def _chg(r):
        d = to_float(r.get("price_change_manwon"))
        if d is None:
            return "-"
        cls = "good" if d < 0 else "bad"
        prev = to_float(r.get("price_prev_manwon"))
        extra = f' <span class="muted small">(이전 {prev:,.0f}만원)</span>' if prev else ""
        return f'<b class="{cls}">{d:+,.0f}만원</b>{extra}'

    base = [("차량번호", _plate), ("모델", _model), ("가격", _price), ("주행", _km)]
    return f"""
  <h2>지난 실행({_e(diff['prev_date'])}) 이후 달라진 것</h2>
  <div class="card">
    <h3>가격이 내린 매물 <span class="muted">{len(diff['price_down'])}건</span></h3>
    <p class="muted">딜러가 값을 내렸다는 것은 안 팔리고 있다는 뜻입니다. 협상 여지가 큽니다.</p>
    {_tbl(diff['price_down'], base + [("변동", _chg)], "없습니다.")}
    <h3>새로 올라온 매물 <span class="muted">{len(diff['new'])}건</span></h3>
    {_tbl(diff['new'], base, "없습니다.")}
    <h3>사라진 매물 <span class="muted">{len(diff['gone'])}건</span></h3>
    <p class="muted">팔렸거나 딜러가 내린 매물입니다. 시장이 얼마나 빨리 도는지 보여줍니다.</p>
    {_tbl(diff['gone'], base, "없습니다.")}
    <p class="muted">변동 없음 {diff['unchanged']}건</p>
  </div>"""


def _trend_section(trend: list[dict] | None) -> str:
    """시세선 자체의 추이. 시장이 빠지는 중이면 지금 싼 차도 곧 평범해진다."""
    rows = [t for t in (trend or []) if t.get("ref_retention") not in ("", None)]
    if len(rows) < 2:
        return ""
    by: dict[str, list] = {}
    for t in rows:
        by.setdefault(t["model_key"], []).append(t)
    blocks = []
    for key, ts in by.items():
        ts.sort(key=lambda x: x["date"])
        label = ts[-1].get("label") or key
        cells, prev = [], None
        for t in ts[-10:]:
            r = float(t["ref_retention"]) * 100
            d = "" if prev is None else f"{r - prev:+.2f}%p"
            cls = "" if prev is None else ("bad" if r < prev else "good")
            cells.append(f"<tr><td>{_e(t['date'])}</td>"
                         f"<td class='num'>{r:.1f}%</td>"
                         f"<td class='num'>{t.get('n', 0)}대</td>"
                         f"<td class='num {cls}'>{d}</td></tr>")
            prev = r
        blocks.append(
            f"<h3>{_e(label)}</h3><div class='tablewrap'><table>"
            f"<thead><tr><th>실행일</th><th class='num'>잔존율</th>"
            f"<th class='num'>표본</th><th class='num'>변화</th></tr></thead>"
            f"<tbody>{''.join(cells)}</tbody></table></div>")
    return f"""
  <h2>시세 추이</h2>
  <div class="card">
    <p class="muted">기준점 3년 / 45,000km 에서의 잔존율입니다. 표본 구성이 주마다
      달라지므로 한 점을 정해 두고 그 점의 값을 비교합니다.
      <b>잔존율이 내려가는 중이면</b> 지금 저평가로 보이는 매물도 몇 주 뒤엔
      평범한 가격이 됩니다. 서두를지 기다릴지를 여기서 판단하세요.</p>
    {"".join(blocks)}
  </div>"""


def _alerts_section(alerts: dict | None) -> str:
    """이번 주에 손댈 것만 맨 위에. 없으면 한 줄로 끝낸다."""
    if alerts is None:
        return ""
    if not alerts.get("any"):
        return ('<div class="alerts none"><h2>이번 주 주목할 매물 없음</h2>'
                '<p>알림 조건에 걸리는 매물이 없습니다. 아래 전체 순위는 참고용입니다.</p>'
                '</div>')

    def _card(rows, title, note, cls):
        if not rows:
            return ""
        items = []
        for r in rows[:8]:
            bits = []
            gap = to_float(r.get("value_gap_manwon"))
            pct = to_float(r.get("value_gap_pct"))
            sg = to_float(r.get("value_gap_sigma"))
            chg = to_float(r.get("price_change_manwon"))
            dom = to_int(r.get("days_on_market"))
            if gap is not None:
                bits.append(f"{gap:+,.0f}만원")
            if pct is not None:
                bits.append(f"{pct:+.1f}%")
            if sg is not None:
                bits.append(f"{sg:+.2f}&sigma;")
            if chg is not None:
                bits.append(f"가격 {chg:+,.0f}만원")
            if dom is not None:
                bits.append(f"보유 {dom}일")
            url = r.get("listing_url") or ""
            plate = _e(r.get("plate_no") or r.get("vehicle_id") or "?")
            link = f'<a href="{_e(url)}" target="_blank">{plate}</a>' if url else plate
            items.append(
                f'<li><b>{link}</b> <span class="muted">{_e(r.get("model_label") or "")}</span> '
                f'{fmt_manwon(r.get("price_manwon"))} · {" · ".join(bits)}'
                + (f'<div class="muted small">{_e(r.get("value_verdict") or "")}'
                   f' — {_e(r.get("value_verdict_note") or "")}</div>'
                   if r.get("value_verdict") else "")
                + '</li>')
        return (f'<div class="alert-card {cls}"><h3>{title} '
                f'<span class="muted">{len(rows)}건</span></h3>'
                f'<p class="muted">{note}</p><ul class="alist">{"".join(items)}</ul></div>')

    A = config.ALERTS
    return f"""
  <div class="alerts">
    <h2>이번 주 주목할 매물</h2>
    {_card(alerts['opportunity'],
           f"진짜 기회 — {A['opportunity_sigma']:.0f}&sigma; 이상이고 싼 이유도 못 찾음",
           "가장 먼저 보세요. 통계적으로 유의미하고 할인 사유가 설명되지 않습니다.", "a-gold")}
    {_card(alerts['price_drop'],
           f"지난 실행 대비 {A['price_drop_manwon']:,}만원 이상 내림",
           "딜러가 값을 내렸다 = 안 팔리고 있다 = 협상이 열렸다는 뜻입니다.", "a-drop")}
    {_card(alerts['new_strong'],
           f"새로 올라온 매물 중 {A['new_listing_sigma']}&sigma; 이상",
           "좋은 매물은 초기에 사라집니다.", "a-new")}
    {_card(alerts['long_held'],
           f"딜러 보유 {A['days_on_market']}일 초과",
           "오래 안 팔린 매물입니다. 협상 여지가 크지만 남들이 지나쳤다는 "
           "신호이기도 합니다.", "a-held")}
  </div>"""


def build_html(models: list[tuple], ranked: list[dict], stage: str,
               notes: list[str] | None = None,
               diff: dict | None = None,
               trend: list[dict] | None = None,
               alerts: dict | None = None) -> str:
    stage_label = "최종 (헤이딜러 숨은이력 반영)" if stage == "final" else "1차 (엔카 데이터만)"
    banner = ""
    if stage != "final":
        banner = (
            '<div class="banner"><b>1차 결과 — 엔카의 객관적 데이터만 반영했습니다.</b><br>'
            '가격 · 연식 · 주행거리 · 신차가 · 사고 유무 · 엔카진단 · 소유자 변경까지가 '
            '여기서 쓴 전부입니다.<br>'
            '<b>에어서스와 배터리 제조사는 아직 반영되지 않았습니다.</b> 엔카의 옵션 목록과 '
            '판매자 설명글은 딜러가 쓴 홍보 문구라 누락·과장이 흔해 판정 근거로 쓰지 않습니다.<br>'
            '아래 <b>차량번호</b>를 헤이딜러 \'숨은이력찾기\'에서 조회한 뒤 '
            '<code>merge.py</code> 로 병합하면 출고 기록 기반으로 최종 순위가 확정됩니다.</div>')
    else:
        banner = (
            '<div class="banner ok"><b>최종 결과 — 헤이딜러 출고 기록이 반영되었습니다.</b><br>'
            '에어서스 장착 여부와 배터리 제조사는 출고 기록에 근거한 확정 정보이며, '
            '1차 점수를 뒤집을 만큼 크게 반영됩니다. 숨은이력을 조회하지 않은 매물은 '
            '해당 항목이 미반영 상태이므로 같은 기준으로 비교되지 않습니다.</div>')

    note_html = ""
    if notes:
        note_html = ('<div class="banner">' +
                     "<br>".join(_e(n) for n in notes) + "</div>")

    return f"""<title>car-hunter 리포트</title>
<style>{CSS}</style>
<div class="wrap">
  <h1>중고 전기차 매물 분석 리포트</h1>
  <p class="sub">단계: <b>{_e(stage_label)}</b> · 생성 {datetime.now():%Y-%m-%d %H:%M} ·
     분석 대상 {len(ranked)}대 표시</p>
  {banner}{note_html}
  {_alerts_section(alerts)}
  {_diff_section(diff)}
  {_trend_section(trend)}

  <h2>모델별 시세 분포</h2>
  <div class="grid">{"".join(_market_card(m, rs) for m, rs in models)}</div>

  <h2>적정가 대비 순위</h2>
  <p class="sub">적정가 대비 차액이 플러스면 저평가(기회), 마이너스면 고평가입니다.
    흠결이 있어도 그만큼 싸면 위로 올라옵니다. 침수·전손·골격C·배터리팩 손상·
    주행거리 조작은 금액 환산이 불가능한 리스크라 후보에서 제외됩니다.</p>
  <div class="banner"><b>&sigma;(시그마)를 먼저 보세요.</b><br>
    저평가 폭을 시세선 자체의 오차로 나눈 값입니다. 절대금액만 보면 비싼 차가
    항상 위로 오고(2억의 5%는 1,000만원), 비율만 보면 싼 차가 항상 위로 옵니다.
    &sigma;는 둘을 같은 자로 잽니다.<br>
    <b>|&sigma;| &lt; 1</b> — 시세선 오차 범위 안. 통계적으로 &lsquo;싸다&rsquo;고 말하기 어렵습니다.<br>
    <b>|&sigma;| &gt; 2</b> — 우연으로 보기 어려운 수준. 실제로 확인할 값어치가 있습니다.</div>
  <div class="banner"><b>이 금액은 절대값이 아니라 매물 간 상대 비교용입니다.</b><br>
    적정가 = 기준 시세(동일 연식 · 평균주행)에서 과주행 · 사고이력 · 배터리 보증
    잔여 부족 · 소유/용도 이력을 금액으로 뺀 값입니다. 환산 계수는 추정치이며
    <code>config.py</code> 의 <code>PRICING</code> 에서 조정할 수 있습니다.<br>
    기준 시세 자체가 사고차를 포함한 시장 평균이라, 사고 할인을 다시 빼면 같은
    흠결을 일부 중복해 반영하는 셈입니다. 순위를 가르는 용도로만 쓰세요.</div>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>#</th><th>차량번호 / 모델</th><th class="num">가격</th>
        <th class="num">연식 / 주행</th><th class="num">연평균</th>
        <th class="num">배터리 보증</th><th class="num">적정가 대비<br><span class="small">만원 / % / &sigma;</span></th>
        <th>왜 싼가 (판정)</th>
        <th>적정가 산출 내역</th><th>추천 / 주의 사유</th><th>링크</th>
      </tr></thead>
      <tbody>{_rank_rows(ranked, stage)}</tbody>
    </table>
  </div>

  <h2>계약 전 수동 확인 항목</h2>
  <div class="card">
    <ol class="check">
      {"".join(f"<li><b>{_e(t)}</b><span>{_e(d)}</span></li>" for t, d in CHECKLIST)}
    </ol>
  </div>

  <footer>
    개인 검토용 자동 분석 결과입니다. 점수는 공개 매물 정보에 기반한 상대 비교이며
    차량의 실제 상태를 보증하지 않습니다. 계약 전 위 체크리스트를 직접 확인하세요.
  </footer>
</div>"""
