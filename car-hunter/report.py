# -*- coding: utf-8 -*-
"""report.html 생성. 외부 CDN 없이 인라인 SVG 로 차트를 그린다."""

from __future__ import annotations

import html
from datetime import datetime

from common import fmt_km, fmt_manwon, to_float, to_int

CHECKLIST = [
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
    ("성능점검기록부 원본 대조", "엔카 표기와 실제 기록부(사고/교환 부위)가 일치하는지 대조."),
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
    if market and market.method == "regression":
        ages = [to_float(r.get("age_years")) for r in rows if to_float(r.get("age_years"))]
        if ages:
            med_age = sorted(ages)[len(ages) // 2]
            ax, bx = x0 + (x1 - x0) * 0.02, x1 * 0.98
            ay = market.predict(med_age, ax)
            by = market.predict(med_age, bx)
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

    if market.method == "regression":
        fit = (f"가격 ≈ {market.intercept:,.0f} "
               f"{market.coef_age:+,.0f}×경과연수 "
               f"{market.coef_km:+,.1f}×(주행거리/1000)  ·  R²={market.r2:.2f}")
    else:
        fit = "표본이 5건 미만이라 회귀 대신 중앙값 기준으로 비교했습니다."

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
      {warn}
      {_scatter_svg(rows, market)}
      <p class="muted">점선 = 중앙 연식({_e(market.label)}) 기준 시세선. 파랑=저평가, 빨강=고평가.</p>
    </div>"""


def _rank_rows(rows: list[dict], stage: str) -> str:
    out = []
    for i, r in enumerate(rows, 1):
        plate = r.get("plate_no") or "(차량번호 미확보)"
        photo = r.get("photo_url") or ""
        url = r.get("listing_url") or ""
        plus = [s for s in (r.get("reasons_plus") or "").split(" ; ") if s]
        minus = [s for s in (r.get("reasons_minus") or "").split(" ; ") if s]

        # 엔카의 에어서스 언급은 딜러 코멘트라 점수에 넣지 않는다. 참고 표시만.
        seller_air = str(r.get("seller_airsus_mention", "")).strip().lower() in ("true", "1")
        if stage != "final":
            note = ("판매자 설명에 에어서스 언급 있음" if seller_air
                    else "판매자 설명에 에어서스 언급 없음")
            ref = [f'<div class="ref">※ {_e(note)} — 딜러 작성 문구라 신뢰 불가</div>']
        else:
            ref = []

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

        out.append(f"""
      <tr>
        <td class="rank">{i}</td>
        <td>
          <div class="plate">{_e(plate)}</div>
          <div class="muted small">{_e(r.get('model_label'))} · {_e(r.get('trim'))}</div>
          {hidden_html}
        </td>
        <td class="num"><b>{fmt_manwon(r.get('price_manwon'))}</b>
          <div class="muted small">예측 {fmt_manwon(r.get('predicted_price_manwon'))}</div>
          {f'<div class="muted small">신차가 대비 -{_e(r.get("depreciation_pct"))}%</div>' if r.get('depreciation_pct') not in ('', None) else ''}</td>
        <td class="num">{_e(r.get('year'))}.{_e(str(r.get('month') or '').zfill(2))}
          <div class="muted small">{fmt_km(r.get('mileage_km'))}</div></td>
        <td class="num">{_e(r.get('annual_km') and f"{to_int(r.get('annual_km')):,}" or '-')}
          <div class="muted small">km/년</div></td>
        <td class="num">{_e(r.get('battery_remaining_pct'))}%
          <div class="muted small">{_e(r.get('battery_binding'))}</div></td>
        <td class="num score">{_e(r.get('score_total'))}</td>
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
table{border-collapse:collapse;width:100%;min-width:980px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--bd);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}
td.num,th.num{text-align:right;white-space:nowrap}
td.rank{font-weight:700;color:var(--muted);width:36px}
.plate{display:inline-block;background:var(--plate-bg);color:var(--plate-fg);
font-weight:800;letter-spacing:1px;padding:6px 14px;border-radius:6px;
font-size:20px;font-family:ui-monospace,Menlo,monospace;
border:2px solid var(--plate-fg)}
.hidden-tag{margin-top:5px;font-size:12px;color:var(--muted)}
.score{font-size:17px;font-weight:700}
.reasons{max-width:360px;font-size:12.5px}
.reasons .p{color:var(--good)}.reasons .m{color:var(--bad)}
.reasons .ref{color:var(--muted);font-size:11.5px;margin-top:6px;
padding-top:5px;border-top:1px dashed var(--bd)}
.banner.ok{background:transparent;border:1px solid var(--good);color:inherit}
.links a{color:var(--good);text-decoration:none;margin-right:8px;white-space:nowrap}
.links a:hover{text-decoration:underline}
.muted{color:var(--muted)}.small{font-size:12px}
ol.check{padding-left:20px}ol.check li{margin:10px 0}
ol.check b{display:block}ol.check span{color:var(--muted);font-size:13px}
footer{margin-top:40px;color:var(--muted);font-size:12px}
"""


def build_html(models: list[tuple], ranked: list[dict], stage: str,
               notes: list[str] | None = None) -> str:
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

  <h2>모델별 시세 분포</h2>
  <div class="grid">{"".join(_market_card(m, rs) for m, rs in models)}</div>

  <h2>종합점수 순위</h2>
  <p class="sub">1차 배점: 시세 잔차 40 · 배터리 보증 잔여 20 · 신차가 대비 감가율 10 · 무사고 8 · 엔카진단 5 · 1인소유 4 (감점: 과주행 6~12, 침수·전손 40, 렌트·영업용 10)</p>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>#</th><th>차량번호 / 모델</th><th class="num">가격</th>
        <th class="num">연식 / 주행</th><th class="num">연평균</th>
        <th class="num">배터리 보증</th><th class="num">점수</th>
        <th>추천 / 주의 사유</th><th>링크</th>
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
