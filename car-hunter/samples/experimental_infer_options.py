# -*- coding: utf-8 -*-
"""[실험용 — 본 파이프라인에서 사용하지 않음] 옵션 코드 → 이름 역추적.

!! 이 방법은 실전에서 틀렸다. 쓰지 말 것. !!

options.etc 와 판매자 설명글을 정답 라벨로 삼는 설계였는데, 그 텍스트는
딜러가 홍보용으로 쓴 자유 기술이라 정답이 될 수 없다. 실제 검증에서
"코드가 없으니 에어서스 미장착" 으로 예측한 매물의 판매자 설명에
"에어서스펜션, 후륜조향 옵션적용차량" 이 적혀 있었다. 양성 표본이
1대뿐이라 통계적 근거도 없었다.

에어서스와 배터리 제조사는 3단계(헤이딜러 출고 기록)에서 확정한다.
이 파일은 접근법의 기록으로만 남긴다.

--- 원래 설명 ---
옵션 코드 → 이름 역추적.

엔카는 options.standard 를 숫자 코드('001','002')로만 내려준다.
반면 options.etc 에는 딜러가 한글로 적어 둔 항목이 들어 있다.
이미 수집한 data/details.json 을 이용해, 특정 키워드(기본: 에어서스)가
etc 에 적힌 매물들이 공통으로 가진 코드를 찾아 그 코드의 정체를 역추적한다.

  python infer_options.py                  # 에어서스 코드 추론
  python infer_options.py --keyword 파노라마  # 다른 옵션도 가능
  python infer_options.py --write          # 결과를 data/option_codes.json 에 저장

** 판정의 비대칭에 주의 **
etc 에 '에어 서스펜션' 이 적혀 있으면 그 차는 확실히 장착 차량이다(양성).
그러나 안 적혀 있다고 미장착은 아니다 — 딜러가 안 썼을 뿐일 수 있다(미상).
따라서 "양성 전부가 가진 코드" 를 찾되, 미상 그룹에서의 출현율은
낮을수록 좋다는 정도로만 해석한다.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import config
import encar_api as api
from common import (
    DETAILS_JSON, LISTINGS_CSV, OPTION_MAP_JSON,
    die, log, read_csv, read_json, warn, write_json,
)


def _codes_and_texts(detail) -> tuple[set[str], list[str]]:
    """상세 응답에서 (숫자 코드 집합, 한글로 적힌 옵션명들) 을 뽑는다."""
    codes: set[str] = set()
    texts: list[str] = []
    if not isinstance(detail, dict):
        return codes, texts

    opts = api.pick(detail, "options", "Options", default=None)
    if not isinstance(opts, dict):
        return codes, texts

    for key, val in opts.items():
        if not isinstance(val, list) or not val:
            continue
        if api.looks_like_option_codes(val):
            codes.update(str(x) for x in val)
        elif api.looks_like_option_names(val):
            texts.extend(str(x).strip() for x in val if str(x).strip())
    return codes, texts


def load_vehicles() -> list[dict]:
    details = read_json(DETAILS_JSON) or {}
    meta = {str(r.get("vehicle_id")): r for r in read_csv(LISTINGS_CSV)}

    out = []
    for vid, bucket in details.items():
        if not isinstance(bucket, dict):
            continue
        codes, texts = _codes_and_texts(bucket.get("detail"))
        if not codes and not texts:
            continue
        m = meta.get(str(vid), {})
        out.append({
            "vid": str(vid),
            "codes": codes,
            "texts": texts,
            "model_key": m.get("model_key", ""),
            "model_label": m.get("model_label", ""),
            "trim": m.get("trim", ""),
            "plate": m.get("plate_no", ""),
            "url": m.get("listing_url", ""),
        })
    return out


def keywords_for(model_key: str | None, override: list[str] | None) -> list[str]:
    if override:
        return override
    kws: list[str] = []
    for t in config.TARGETS:
        if model_key and t["key"] != model_key:
            continue
        kws.extend(t.get("airsus_keywords", []))
    return kws or ["에어서스"]


def has_keyword(texts: list[str], kws: list[str]) -> str | None:
    flat_kws = [k.lower().replace(" ", "") for k in kws]
    for t in texts:
        f = t.lower().replace(" ", "")
        if any(k in f for k in flat_kws):
            return t
    return None


def main() -> int:
    p = argparse.ArgumentParser(description="옵션 코드 역추적")
    p.add_argument("--keyword", action="append",
                   help="찾을 옵션 키워드 (여러 번 지정 가능). 생략 시 에어서스 키워드")
    p.add_argument("--model", help="config.TARGETS 의 key 로 한 차종만")
    p.add_argument("--name", help="--write 시 저장할 옵션 이름 (생략 시 첫 키워드)")
    p.add_argument("--write", action="store_true",
                   help="가장 유력한 코드를 data/option_codes.json 에 저장")
    p.add_argument("--min-score", type=float, default=0.5,
                   help="후보로 볼 최소 판별력 (기본 0.5)")
    args = p.parse_args()

    vehicles = load_vehicles()
    if args.model:
        vehicles = [v for v in vehicles if v["model_key"] == args.model]
    if not vehicles:
        die("분석할 데이터가 없습니다.\n"
            "먼저 `python collect.py` 로 상세까지 수집하세요 (data/details.json 필요).")

    kws = keywords_for(args.model, args.keyword)
    print("=" * 74)
    print(" 옵션 코드 역추적")
    print("=" * 74)
    print(f"  찾는 키워드 : {', '.join(kws)}")
    print(f"  분석 매물   : {len(vehicles)}대")

    pos, unknown, no_code = [], [], []
    for v in vehicles:
        if not v["codes"]:
            no_code.append(v)
            continue
        hit = has_keyword(v["texts"], kws)
        v["hit_text"] = hit
        (pos if hit else unknown).append(v)

    print(f"  양성(딜러가 한글로 명시) : {len(pos)}대")
    print(f"  미상(명시 없음)          : {len(unknown)}대")
    if no_code:
        print(f"  코드 없음(분석 제외)      : {len(no_code)}대")

    if not pos:
        warn("키워드가 적힌 매물이 하나도 없어 역추적이 불가능합니다.")
        print("\n  할 수 있는 것:")
        print("   1. 매물을 더 수집한다 (python collect.py)")
        print("   2. 다른 키워드로 시험한다 (--keyword 서스펜션)")
        print("   3. 엔카 상세 페이지에서 에어서스 있는 차/없는 차를 직접 찾아")
        print("      data/option_codes.json 에 코드를 직접 적는다")
        _dump_samples(vehicles[:5])
        return 1

    print("\n  [양성 매물의 근거 문구]")
    for v in pos[:10]:
        print(f"    {v['plate'] or v['vid']:14} \"{v['hit_text']}\"")

    # ---- 코드별 통계 ----
    n_pos, n_unk = len(pos), len(unknown)
    stats = []
    all_codes = sorted({c for v in vehicles for c in v["codes"]})
    for code in all_codes:
        p_with = sum(1 for v in pos if code in v["codes"])
        u_with = sum(1 for v in unknown if code in v["codes"])
        recall = p_with / n_pos                       # 양성 중 이 코드를 가진 비율
        unk_rate = (u_with / n_unk) if n_unk else 0.0  # 미상 중 이 코드를 가진 비율
        stats.append({
            "code": code, "p_with": p_with, "u_with": u_with,
            "recall": recall, "unk_rate": unk_rate,
            "score": recall - unk_rate,               # 판별력
        })

    # 양성 전원이 가진 코드만이 후보 자격이 있다
    cands = [s for s in stats if s["recall"] >= 1.0]
    cands.sort(key=lambda s: s["score"], reverse=True)

    print(f"\n  [코드별 분석]  양성 {n_pos}대 / 미상 {n_unk}대")
    print(f"    {'코드':<8}{'양성 보유':<12}{'미상 보유':<12}{'판별력':<8}해석")
    print("    " + "-" * 64)

    shown = 0
    for s in cands:
        pr = f"{s['p_with']}/{n_pos} (100%)"
        ur = f"{s['u_with']}/{n_unk} ({s['unk_rate']*100:.0f}%)"
        if s["score"] >= 0.8:
            note = "★★★ 매우 유력"
        elif s["score"] >= args.min_score:
            note = "★★ 유력"
        elif s["unk_rate"] >= 0.99:
            note = "전 매물 공통 — 기본 장착 항목"
        else:
            note = "약함"
        print(f"    {s['code']:<8}{pr:<12}{ur:<12}{s['score']:<8.2f}{note}")
        shown += 1
        if shown >= 15:
            break

    strong = [s for s in cands if s["score"] >= args.min_score]
    if not strong:
        warn("양성 전원이 공통으로 가진 코드 중 판별력 있는 것이 없습니다.")
        print("    양성 매물이 적으면 이런 결과가 나옵니다. 매물을 더 모으거나,")
        print("    아래 확인 절차로 직접 특정하세요.")
        return 1

    best = strong[0]
    name = args.name or kws[0]
    print(f"\n  => 가장 유력한 코드: '{best['code']}'  (판별력 {best['score']:.2f})")

    if len(strong) > 1:
        others = ", ".join(f"'{s['code']}'" for s in strong[1:5])
        print(f"     같이 붙어 다니는 코드: {others}")
        print("     (에어서스가 특정 패키지에 묶여 있으면 여러 개가 함께 뜬다)")

    # ---- 확인 절차 ----
    check = [v for v in unknown if best["code"] in v["codes"]]
    print("\n  [확인 방법]")
    if check:
        print(f"    미상 매물 중 '{best['code']}' 코드를 가진 {len(check)}대입니다.")
        print("    아래 링크를 열어 옵션에 에어서스가 실제로 있는지 보세요.")
        print("    있으면 이 코드가 맞습니다.")
        for v in check[:3]:
            print(f"      {v['plate'] or v['vid']:14} {v['url']}")
    no_code_cars = [v for v in unknown if best["code"] not in v["codes"]]
    if no_code_cars:
        print(f"\n    반대로 '{best['code']}' 가 '없는' 매물 (에어서스가 없어야 정상):")
        for v in no_code_cars[:2]:
            print(f"      {v['plate'] or v['vid']:14} {v['url']}")

    # ---- 저장 ----
    suggestion = {s["code"]: name for s in strong[:1]}
    print("\n  [data/option_codes.json 에 넣을 내용]")
    print("    " + str(suggestion).replace("'", '"'))

    if args.write:
        existing = read_json(OPTION_MAP_JSON) or {}
        if not isinstance(existing, dict) or not all(
                isinstance(v, str) for v in existing.values()):
            existing = {}
        existing.update(suggestion)
        write_json(OPTION_MAP_JSON, existing)
        log(f"저장: {OPTION_MAP_JSON} (총 {len(existing)}개)")
        print("\n    이제 `python collect.py` 를 다시 돌리면 에어서스가 판별됩니다.")
        print("    (이미 받은 데이터로 바로 반영하려면 python score.py 전에")
        print("     collect.py 를 한 번 더 실행해야 합니다)")
    else:
        print("\n    저장하려면:  python infer_options.py --write")
    return 0


def _dump_samples(vehicles: list[dict]) -> None:
    print("\n  [참고] 수집된 옵션 데이터 샘플")
    for v in vehicles:
        print(f"    {v['plate'] or v['vid']}  코드 {len(v['codes'])}개, "
              f"한글항목 {len(v['texts'])}개")
        if v["texts"]:
            print(f"      한글: {', '.join(v['texts'][:8])}")
        if v["codes"]:
            print(f"      코드: {', '.join(sorted(v['codes'])[:15])}")


if __name__ == "__main__":
    sys.exit(main())
