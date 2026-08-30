import csv, sys, json
sys.stdout.reconfigure(encoding='utf-8')
from common import to_float, to_int
L=list(csv.DictReader(open('data/listings.csv',encoding='utf-8-sig')))
S={r['vehicle_id']:r for r in csv.DictReader(open('data/scored.csv',encoding='utf-8-sig'))}
want=['186도1630','354주4191','296다8840']
F=[("모델","model_label"),("트림","trim"),("지역","region"),("판매형태","sell_type"),
   ("가격(만원)","price_manwon"),("신차가(만원)","origin_price_manwon"),
   ("연식","year"),("최초등록일","first_registration_date"),("주행거리","mileage_km"),
   ("연평균주행","annual_km"),
   ("보험 내차사고","accident_my_count"),("보험 타차사고","accident_other_count"),
   ("내차 수리비(원)","accident_my_cost_won"),("사고 상세","accident_lines"),
   ("소유자 변경","owner_change_count"),("번호판 변경","plate_change_count"),
   ("침수/전손","flood_or_total_loss"),("과거 영업/대여","past_commercial_use"),
   ("저당 건수","loan_count"),
   ("성능기록부","page_parse_note"),("사진뿐","page_is_image"),
   ("수리 부위","page_repair_notes"),("최악 랭크","page_worst_rank"),
   ("성능기록부 사고이력","page_accident_history"),("단순수리","page_simple_repair"),
   ("계기상태","page_mileage_gauge"),("성능점검 주행거리","page_mileage"),
   ("고전원 불량","page_ev_hv_bad"),("세부상태 불량","page_detail_bad"),
   ("자차보험 미가입","insurance_not_joined"),
   ("배터리 보증 잔여%","battery_remaining_pct"),("보증 남은 기간(년)","battery_years_left"),
   ("보증 남은 주행(km)","battery_km_left"),
   ("최초 광고일","first_advertised"),("딜러 보유(일)","days_on_market"),
   ("재등록","re_registered"),("엔카진단","encar_diagnosed"),
   ("조회수","view_count"),("찜","subscribe_count"),
   ]
SF=[("적정가","fair_price_manwon"),("차액(만원)","value_gap_manwon"),
    ("비율(%)","value_gap_pct"),("유의성(σ)","value_gap_sigma"),
    ("시세선오차(만원)","sigma_manwon"),("판정","value_verdict"),
    ("판정근거","value_verdict_note"),("싼 이유","discount_extra"),
    ("참고","discount_notes"),("적정가 내역","price_breakdown"),
    ("정보없음","price_unknowns")]
for w in want:
    r=next((x for x in L if x['plate_no']==w), None)
    if not r: print(f"[{w}] 못 찾음"); continue
    sc=S.get(r['vehicle_id'],{})
    print("="*78); print(f" {w}   (매물번호 {r['vehicle_id']})"); print("="*78)
    for lab,k in F:
        v=r.get(k,'')
        if v not in ('',None): print(f"  {lab:20} {v}")
    print("  " + "-"*40)
    for lab,k in SF:
        v=sc.get(k,'')
        if v not in ('',None): print(f"  {lab:20} {v}")
    print(f"  {'매물 링크':20} {r.get('listing_url')}")
    print()
