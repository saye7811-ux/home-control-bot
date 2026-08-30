# -*- coding: utf-8 -*-
"""3단계: 헤이딜러 '숨은이력찾기' 결과 병합 (반자동).

흐름
----
  1) python merge.py --list      상위 N대 차량번호 출력 → 헤이딜러 앱에서 수동 조회
  2) 결과 화면 스크린샷을 hidden/ 폴더에 저장
  3) python merge.py --show      hidden/ 의 이미지 경로와 입력 템플릿을 출력
                                 → 이 목록을 클로드 코드에게 주면 클로드가 이미지를
                                   직접 읽어 hidden/extracted.json 을 채운다
  4) python merge.py --apply     extracted.json 병합 → data/merged.csv + report.html

외부 OCR/비전 API 를 호출하지 않는다. 이미지 판독은 클로드 코드가 세션 안에서
직접 수행하므로 추가 비용이 발생하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import config
import report as report_mod
import scoring
from common import (
    EXTRACTED_JSON, HIDDEN_DIR, MARKET_JSON, MERGED_CSV, REPORT_HTML, SCORED_CSV,
    die, ensure_dirs, fmt_km, fmt_manwon, log, read_csv, read_json,
    warn, write_csv, write_json,
)
from score import SCORED_FIELDS

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".heic", ".gif", ".bmp")

HIDDEN_FIELDS = [
    "hidden_battery_maker", "hidden_airsus", "hidden_insurance_won",
    "hidden_insurance_summary", "hidden_notes", "hidden_source_image",
    "adj_battery_maker", "adj_airsus", "adj_insurance", "hidden_adjust_total",
]
MERGED_FIELDS = SCORED_FIELDS + HIDDEN_FIELDS + ["adj_accident_revert"]

TEMPLATE = {
    "_설명": [
        "hidden/ 의 헤이딜러 스크린샷을 읽고 records 배열을 채우세요.",
        "plate_no 는 필수(매칭 키). 값을 못 읽으면 vehicle_id 로 대체 가능.",
        "airsus: 출고옵션에 에어서스/에어매틱이 있으면 true, 명시적으로 없으면 false,",
        "        화면에서 판단 불가면 null.",
        "insurance_repair_won: 보험 수리이력 총액(원). 이력 없으면 0, 불명이면 null.",
    ],
    "records": [
        {
            "plate_no": "123가4567",
            "vehicle_id": "",
            "source_image": "hidden/IMG_0001.png",
            "battery_maker": "삼성SDI",
            "airsus": True,
            "insurance_repair_won": 0,
            "insurance_summary": "내차 피해 0건 / 상대차 피해 1건 (32만원)",
            "notes": "출고옵션 목록에 에어서스펜션 포함 확인",
        }
    ],
}


def _norm_plate(s: str) -> str:
    return "".join(ch for ch in str(s or "") if not ch.isspace() and ch not in "-·")


def _load_scored() -> list[dict]:
    rows = read_csv(SCORED_CSV)
    if not rows:
        die(f"{SCORED_CSV} 가 없습니다. 먼저 `python score.py` 를 실행하세요.")
    # 상세 미확보 매물은 차량번호가 없어 헤이딜러 조회 대상이 될 수 없다.
    rows = [r for r in rows if str(r.get("rank") or "").strip()]
    from common import to_int
    for r in rows:
        for k in ("price_manwon", "year", "month", "mileage_km", "annual_km",
                  "battery_km_left"):
            r[k] = to_int(r.get(k))
    return rows


def _images() -> list[str]:
    if not os.path.isdir(HIDDEN_DIR):
        return []
    return sorted(
        os.path.join(HIDDEN_DIR, f) for f in os.listdir(HIDDEN_DIR)
        if f.lower().endswith(IMAGE_EXT)
    )


# ---------------------------------------------------------------------------
def cmd_list(args) -> int:
    rows = _load_scored()[: args.top]
    print("\n헤이딜러 '숨은이력찾기' 수동 조회 대상\n")
    print(f"{'순위':<5}{'차량번호':<14}{'점수':>7}   모델 / 가격 / 주행")
    print("─" * 78)
    for r in rows:
        print(f"{r.get('rank',''):<5}{r.get('plate_no') or '(미확보)':<14}"
              f"{r.get('score_total',''):>7}   "
              f"{r.get('model_label')} / {fmt_manwon(r.get('price_manwon'))}"
              f" / {fmt_km(r.get('mileage_km'))}")
    print("\n조회 후 결과 화면 스크린샷을 hidden/ 폴더에 저장하고"
          "  `python merge.py --show`  를 실행하세요.")
    print("파일명에 차량번호를 넣어두면 매칭이 쉬워집니다. 예: hidden/113라2781.png\n")
    return 0


def cmd_init(args) -> int:
    ensure_dirs()
    tpl = os.path.join(HIDDEN_DIR, "extracted.template.json")
    write_json(tpl, TEMPLATE)
    readme = os.path.join(HIDDEN_DIR, "README.md")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(
            "# hidden/ — 헤이딜러 숨은이력 스크린샷\n\n"
            "1. `python merge.py --list` 로 상위 매물 차량번호 확인\n"
            "2. 헤이딜러 앱 '숨은이력찾기'에서 각 차량번호 조회\n"
            "3. 결과 화면 스크린샷을 이 폴더에 저장 "
            "(파일명에 차량번호를 넣으면 자동 매칭됨)\n"
            "4. `python merge.py --show` 출력을 클로드 코드에 전달\n"
            "5. 클로드가 이미지를 읽어 `extracted.json` 생성\n"
            "6. `python merge.py --apply` 로 병합 및 최종 리포트 생성\n\n"
            "`extracted.template.json` 이 입력 형식 예시입니다.\n"
        )
    log(f"생성: {tpl}")
    log(f"생성: {readme}")
    return 0


def cmd_show(args) -> int:
    ensure_dirs()
    rows = _load_scored()[: args.top]
    imgs = _images()

    print("\n" + "=" * 78)
    print(" 헤이딜러 스크린샷 판독 요청")
    print("=" * 78)

    if not imgs:
        print(f"\nhidden/ 폴더에 이미지가 없습니다: {HIDDEN_DIR}")
        print("헤이딜러 조회 결과 스크린샷을 이 폴더에 넣고 다시 실행하세요.")
        return 1

    print(f"\n[읽을 이미지 {len(imgs)}개] — 클로드 코드가 Read 도구로 직접 엽니다:\n")
    for p in imgs:
        guess = ""
        stem = _norm_plate(os.path.splitext(os.path.basename(p))[0])
        for r in rows:
            if r.get("plate_no") and _norm_plate(r["plate_no"]) in stem:
                guess = f"   ← 파일명 매칭: {r['plate_no']} (#{r.get('rank')})"
                break
        print(f"  {p}{guess}")

    print(f"\n[대상 차량번호 {len(rows)}대]")
    for r in rows:
        print(f"  #{r.get('rank'):<3} {r.get('plate_no') or '(미확보)':<14}"
              f" {r.get('model_label')}")

    print("\n" + "-" * 78)
    print(" 클로드 코드에게 아래와 같이 요청하세요:")
    print("-" * 78)
    print("""
  "hidden/ 폴더의 이미지들을 읽고 각 화면에서 다음을 추출해서
   hidden/extracted.json 으로 저장해줘:
     - 차량번호(plate_no)
     - 배터리 제조사(battery_maker)
     - 출고 옵션에 에어서스/에어매틱 포함 여부(airsus: true/false/null)
     - 보험 수리이력 총액 원 단위(insurance_repair_won)
     - 보험이력 요약(insurance_summary)
   형식은 hidden/extracted.template.json 참고."
""")
    print("그 다음  `python merge.py --apply`  를 실행하면 병합됩니다.\n")

    if not os.path.exists(os.path.join(HIDDEN_DIR, "extracted.template.json")):
        cmd_init(args)
    return 0


def cmd_apply(args) -> int:
    ensure_dirs()
    rows = _load_scored()
    data = read_json(args.extracted or EXTRACTED_JSON)
    if not data:
        die(f"{args.extracted or EXTRACTED_JSON} 가 없습니다.\n"
            "`python merge.py --show` 안내에 따라 클로드 코드가 먼저 생성해야 합니다.")

    records = data.get("records") if isinstance(data, dict) else data
    if not isinstance(records, list):
        die("extracted.json 형식이 잘못되었습니다: 최상위에 records 배열이 필요합니다.")

    by_plate: dict[str, dict] = {}
    dupes: list[str] = []
    for r in rows:
        if not r.get("plate_no"):
            continue
        k = _norm_plate(r["plate_no"])
        if k in by_plate:
            dupes.append(r["plate_no"])
            continue          # 먼저 나온(점수 높은) 매물을 유지
        by_plate[k] = r
    if dupes:
        warn(f"차량번호가 중복된 매물이 있습니다: {sorted(set(dupes))}. "
             "번호판 추출 오류일 수 있으니 vehicle_id 로 매칭하세요.")
    by_vid = {str(r.get("vehicle_id")): r for r in rows if r.get("vehicle_id")}

    applied, unmatched = 0, []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        target = None
        key = _norm_plate(rec.get("plate_no"))
        if key and key in by_plate:
            target = by_plate[key]
        elif rec.get("vehicle_id") and str(rec["vehicle_id"]) in by_vid:
            target = by_vid[str(rec["vehicle_id"])]
        if target is None:
            unmatched.append(rec.get("plate_no") or rec.get("vehicle_id") or "?")
            continue
        scoring.apply_hidden(target, rec)
        applied += 1

    for r in rows:
        r.setdefault("hidden_adjust_total", 0.0)
        for f in HIDDEN_FIELDS:
            r.setdefault(f, "")
        if not r.get("score_total"):
            r["score_total"] = r.get("score_stage2")

    if unmatched:
        warn(f"매칭 실패 {len(unmatched)}건: {unmatched}")
        warn("차량번호 표기를 data/scored.csv 의 plate_no 와 맞춰주세요.")

    # 적정가 대비 차액으로 다시 줄세운 뒤 저장해야 CSV 의 rank 가 맞는다
    def _gap(r) -> float:
        try:
            return float(r.get("value_gap_manwon"))
        except (TypeError, ValueError):
            return -9e9

    rows.sort(key=_gap, reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    write_csv(MERGED_CSV, rows, MERGED_FIELDS)
    log(f"저장: {MERGED_CSV} ({len(rows)}건, 숨은이력 반영 {applied}건)")

    # 최종 리포트
    markets = read_json(MARKET_JSON, {}) or {}
    models = []
    for key, md in markets.items():
        group = [r for r in rows if r.get("model_key") == key]
        if not group:
            continue
        mm = scoring.MarketModel(**{k: v for k, v in md.items()
                                    if k in scoring.MarketModel.__dataclass_fields__})
        models.append((mm, group))

    notes = []
    if applied < args.top:
        notes.append(f"상위 {args.top}대 중 {applied}대만 헤이딜러 숨은이력이 반영되었습니다. "
                     "나머지는 1차 점수 그대로입니다.")
    if unmatched:
        notes.append(f"차량번호 매칭 실패: {', '.join(map(str, unmatched))}")

    html = report_mod.build_html(models, rows[: max(args.top, 20)], stage="final", notes=notes)
    with open(REPORT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"저장: {REPORT_HTML}")

    print("\n" + "━" * 78)
    print(f" 최종 순위 — 적정가 대비 (숨은이력 반영 {applied}건)")
    print("━" * 78)
    print(" ※ 금액은 절대값이 아니라 매물 간 상대 비교용입니다.")
    print("━" * 78)
    for r in rows[: args.top]:
        gap = r.get("value_gap_manwon")
        fair = r.get("fair_price_manwon")
        price = r.get("price_manwon")
        try:
            gap_f, fair_f, price_f = float(gap), float(fair), float(price)
        except (TypeError, ValueError):
            print(f"  #{r['rank']:<3}【{r.get('plate_no') or '미확보':^12}】 적정가 산출 불가")
            continue
        delta = float(r.get("hidden_adjust_total") or 0)
        mark = f"  (헤이딜러 {delta:+,.0f})" if delta else ""
        bits = []
        if r.get("hidden_battery_maker"):
            bits.append(f"배터리 {r['hidden_battery_maker']}")
        if r.get("hidden_airsus") not in ("", None):
            bits.append("에어서스 O" if str(r["hidden_airsus"]) == "True" else "에어서스 X")
        print(f"  #{r['rank']:<3}【{r.get('plate_no') or '미확보':^12}】 "
              f"적정가 {fair_f:>6,.0f} / 판매가 {price_f:>6,.0f} = {gap_f:+,.0f}만원{mark}")
        print(f"       {r.get('model_label')}"
              + (f"  |  {' · '.join(bits)}" if bits else ""))
        if r.get("price_unknowns"):
            print(f"       ! 정보없음: {r['price_unknowns'][:100]}")
    print()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="헤이딜러 숨은이력 병합 (3단계)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="상위 N대 차량번호 출력")
    g.add_argument("--show", action="store_true", help="hidden/ 이미지 목록 + 판독 요청문 출력")
    g.add_argument("--apply", action="store_true", help="extracted.json 병합 및 최종 리포트")
    g.add_argument("--init", action="store_true", help="hidden/ 템플릿 생성")
    p.add_argument("--top", type=int, default=config.TOP_N)
    p.add_argument("--extracted", help="extracted.json 경로 (기본 hidden/extracted.json)")
    args = p.parse_args()

    if args.list:
        return cmd_list(args)
    if args.init:
        return cmd_init(args)
    if args.apply:
        return cmd_apply(args)
    return cmd_show(args)


if __name__ == "__main__":
    sys.exit(main())
