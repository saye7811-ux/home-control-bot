# -*- coding: utf-8 -*-
"""1단계: 엔카 매물 수집.

사용법
------
  python collect.py --probe            # 검색 API 1회 호출 → 응답 스키마 덤프 (파서 작성 전 확인용)
  python collect.py --discover         # 제조사/모델 facet 값 덤프 (쿼리 문자열 교정용)
  python collect.py                    # 전체 수집 → data/listings.csv, data/details.json
  python collect.py --limit 10         # 모델당 10건만
  python collect.py --no-detail        # 검색 결과만 (차량번호/옵션 없음, 빠른 점검용)
  python collect.py --fixture samples/ # 네트워크 없이 샘플로 파이프라인 점검

차단(캡차/429/403) 감지 시 즉시 중단하고 data/BLOCKED.txt 에 어느 단계에서
막혔는지 기록한다. 수집 중이던 결과는 버리지 않고 저장한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

import config
import encar_api as api
from common import (
    BLOCKED_FLAG, DETAILS_JSON, LISTINGS_CSV, SAMPLES_DIR,
    die, ensure_dirs, log, read_csv, read_json, warn, write_csv, write_json,
)

LISTING_FIELDS = [
    "model_key", "model_label", "vehicle_id", "plate_no",
    "price_manwon", "year", "month", "mileage_km", "region",
    "trim", "trim_detail", "sell_type",
    "accident_free", "accident_my_count", "accident_other_count",
    "accident_my_cost_won", "owner_change_count",
    "flood_or_total_loss", "rental_or_commercial", "one_owner",
    "encar_diagnosed", "encar_check", "direct_inspected",
    "has_airsus_keyword", "airsus_status", "airsus_keyword_hits",
    "options_count", "options", "option_codes", "option_codes_unresolved",
    "option_source",
    "origin_price_manwon", "warranty", "view_count", "subscribe_count",
    "inspection_summary", "photo_url", "listing_url",
    "detail_fetched", "collected_at",
]

# 응답에 없어도 정상인 필드 — 매핑 실패로 경고하지 않는다
OPTIONAL_LISTING_FIELDS = {"trim_detail", "sell_type", "region", "month"}


# ---------------------------------------------------------------------------
# 진단 모드
# ---------------------------------------------------------------------------
def _describe(obj, prefix="", depth=0, out=None, max_depth=3):
    """응답 구조를 사람이 읽을 수 있게 요약."""
    out = out if out is not None else []
    if depth > max_depth:
        return out
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:60]:
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                kind = "dict" if isinstance(v, dict) else f"list[{len(v)}]"
                out.append(f"{p}: {kind}")
                _describe(v, p, depth + 1, out, max_depth)
            else:
                sample = repr(v)
                out.append(f"{p}: {type(v).__name__} = {sample[:70]}")
    elif isinstance(obj, list) and obj:
        _describe(obj[0], f"{prefix}[0]", depth + 1, out, max_depth)
    return out


def _years_of(results: list[dict]) -> list[int]:
    from common import parse_year_month
    out = []
    for r in results:
        y, _ = parse_year_month(api.pick(r, "Year", "year", "yearMonth", "FormYear", "formYear"))
        if y:
            out.append(y)
    return sorted(out)


def _ids_of(results: list[dict]) -> list[str]:
    return [str(api.pick(r, "Id", "id", "vehicleId", "VehicleId", "CarId") or "?")
            for r in results]


def _status(code) -> str:
    if code is None:
        return "연결실패"
    return {200: "200 OK", 404: "404 없음"}.get(code, str(code))


def cmd_probe(args) -> int:
    """검색/상세 API 를 실제로 두들겨 보고 무엇이 되고 무엇이 안 되는지 보고한다.

    파서를 믿기 전에 이 출력을 먼저 볼 것.
    """
    ensure_dirs()
    client = api.EncarClient(config.COLLECT)
    target = next((t for t in config.TARGETS if t["key"] == args.model), config.TARGETS[0])
    n = args.limit or config.PROBE_PAGE_SIZE

    print("=" * 74)
    print(f" 엔카 API 진단  —  {target['label']}")
    print("=" * 74)
    if not target.get("confirmed", False):
        warn(f"이 차종의 제조사/모델 표기('{target['manufacturer']}' / "
             f"'{target['model_group']}' / '{target.get('model')}')는 추정값입니다. "
             f"결과가 0건이면 --discover 로 실제 표기를 확인하세요.")

    # ---------------- 1. 엔드포인트 ----------------
    print("\n[1] 엔드포인트 확인 (연식 필터 없이 호출)")
    usable = []
    for name in config.USE_ENDPOINTS:
        ep = config.ENDPOINTS[name]
        q = args.q or api.build_query(target, ep["ad_type"], include_year=False,
                                      include_model=bool(target.get("model")))
        code, payload, snippet, final_url = client.raw_get(
            ep["url"], {"count": "true", "q": q, "sr": api.build_sr(0, n, config.SORT_KEY)},
            stage=f"search:{name}")
        cnt = api.extract_total_count(payload) if payload else None
        got = len(api.extract_search_results(payload)) if payload else 0
        mark = "사용 가능" if code == 200 and got else ("응답은 되나 결과 0건" if code == 200 else "사용 불가")
        print(f"    {name:8} AdType.{ep['ad_type']}  {_status(code):10} "
              f"총 {cnt if cnt is not None else '-'}건, 이번 응답 {got}건   <- {mark}")
        if code != 200 and snippet:
            print(f"             응답 앞부분: {snippet[:120]}")
        if code == 200 and got:
            usable.append((name, ep, q, payload))

    if not usable:
        warn("사용 가능한 엔드포인트가 없습니다.")
        warn("--discover 로 제조사/모델 표기를 확인하거나, 브라우저에서 복사한 "
             "q 를 --q '<값>' 으로 직접 넣어 보세요.")
        return 1

    name, ep, q_noyear, payload_noyear = usable[0]
    print(f"\n    -> 이후 진단은 '{name}' 엔드포인트로 진행합니다.")

    # ---------------- 2. 요청 URL ----------------
    print("\n[2] 실제 요청 URL (브라우저 주소창에 붙여넣어 대조 가능)")
    _, _, _, url_used = client.raw_get(
        ep["url"], {"count": "true", "q": q_noyear,
                    "sr": api.build_sr(0, n, config.SORT_KEY)}, stage="search:url")
    print(f"    {url_used}")

    # ---------------- 3. 응답 구조 ----------------
    raw_path = os.path.join(os.path.dirname(DETAILS_JSON), "raw_probe.json")
    write_json(raw_path, payload_noyear)
    print(f"\n[3] 응답 최상위 구조   (원본 저장: {raw_path})")
    for line in _describe(payload_noyear, max_depth=1):
        print("    " + line)

    results = api.extract_search_results(payload_noyear)
    print("\n[4] 결과 1건의 전체 필드")
    for line in _describe(results[0], max_depth=2):
        print("    " + line)

    # ---------------- 5. 매핑 ----------------
    print("\n[5] normalize_listing() 매핑 결과   (<- 표시가 있으면 파서 수정 필요)")
    norm = api.normalize_listing(results[0], target)
    missing = []
    for k, v in norm.items():
        empty = v in (None, "")
        if empty and k in OPTIONAL_LISTING_FIELDS:
            note = "   (선택 항목 — 없어도 정상)"
        elif empty:
            note = "   <-- 매핑 실패"
            missing.append(k)
        else:
            note = ""
        print(f"    {k:>14} = {v!r}{note}")
    if missing:
        warn(f"매핑 실패 필드: {missing} — 위 [4] 목록에서 실제 키를 찾아 알려주세요.")

    # ---------------- 6. 조건별 매물 수 ----------------
    print("\n[6] 어떤 검색 조건이 매물을 걸러내는지 (조건을 하나씩 빼며 총건수 비교)")

    def _count_for(label: str, **kw) -> int | None:
        qq = api.build_query(target, ep["ad_type"], **kw)
        code_c, payload_c, snip_c, _ = client.raw_get(
            ep["url"], {"count": "true", "q": qq, "sr": api.build_sr(0, 1, config.SORT_KEY)},
            stage=f"count:{label}")
        if code_c != 200 or payload_c is None:
            print(f"    {label:34} {_status(code_c)}  {snip_c[:60] if snip_c else ''}")
            return None
        return api.extract_total_count(payload_c)

    has_model = bool(target.get("model"))
    base_kw = dict(include_year=True, include_model=has_model)
    base = _count_for("전체 조건 (현재 설정)", **base_kw)
    print(f"    {'전체 조건 (현재 설정)':34} {base if base is not None else '-'}건   <- 기준")

    ad_on = getattr(config, "INCLUDE_AD_TYPE", True)
    variants = [
        ("연식 필터 제거",              dict(base_kw, include_year=False)),
        ("Hidden/MultiViewHidden 제거", dict(base_kw, include_hidden=False)),
        ("CarType 제거",               dict(base_kw, include_car_type=False)),
        # 기본 설정에 따라 '빼보기' 또는 '넣어보기' 로 방향을 맞춘다
        (("AdType 제거" if ad_on else "AdType 추가(광고상품만)"),
         dict(base_kw, include_ad=not ad_on)),
    ]
    if has_model:
        variants.append(("하위 Model 제거(ModelGroup만)", dict(base_kw, include_model=False)))
    variants.append(("조건 최소화(제조사+모델그룹만)",
                     dict(include_year=False, include_model=False, include_hidden=False,
                          include_car_type=False, include_ad=False)))

    for label, kw in variants:
        c = _count_for(label, **kw)
        if c is None:
            continue
        if base:
            diff = c - base
            note = f"   ({diff:+d}건)" if diff else "   (변화 없음)"
            if diff > 0:
                note += "  <-- 이 조건이 매물을 걸러내고 있음"
        else:
            note = ""
        print(f"    {label:34} {c}건{note}")

    # CarType 값이 반대일 가능성도 본다
    alt = dict(target)
    alt["car_type"] = "Y" if target.get("car_type", "N") == "N" else "N"
    q_alt = api.build_query(alt, ep["ad_type"], **base_kw)
    code_a, payload_a, _, _ = client.raw_get(
        ep["url"], {"count": "true", "q": q_alt, "sr": api.build_sr(0, 1, config.SORT_KEY)},
        stage="count:cartype-alt")
    c_alt = api.extract_total_count(payload_a) if code_a == 200 else None
    alt_label = f"CarType.{alt['car_type']} 로 바꿔보기"
    print(f"    {alt_label:34} {c_alt if c_alt is not None else '-'}건")
    if c_alt and base and c_alt > base:
        warn(f"CarType.{alt['car_type']} 가 더 많은 매물을 돌려줍니다 "
             f"({base} -> {c_alt}건). config.py 의 car_type 을 바꾸는 게 좋습니다.")

    # ---------------- 7. 연식 필터 ----------------
    print("\n[7] 연식 필터가 실제로 작동하는지")
    q_year = args.q or api.build_query(target, ep["ad_type"], include_year=True,
                                       include_model=bool(target.get("model")))
    code_y, payload_y, snip_y, _ = client.raw_get(
        ep["url"], {"count": "true", "q": q_year,
                    "sr": api.build_sr(0, n, config.SORT_KEY)}, stage="search:year")
    cnt_no = api.extract_total_count(payload_noyear)
    res_no = api.extract_search_results(payload_noyear)
    yrs_no = _years_of(res_no)
    print(f"    필터 없이 : {_status(code_y and 200)} 총 {cnt_no}건, "
          f"이번 응답 연식 {min(yrs_no) if yrs_no else '-'}~{max(yrs_no) if yrs_no else '-'}")

    if code_y != 200 or payload_y is None:
        print(f"    필터 적용 : {_status(code_y)}  <-- 연식 필터 문법이 거부되었습니다")
        if snip_y:
            print(f"                응답 앞부분: {snip_y[:160]}")
        warn("Year.range(...) 문법이 안 먹습니다. 연식은 수집 후 코드에서 걸러야 합니다 "
             "(matches_target() 이 이미 그 역할을 합니다).")
    else:
        cnt_y = api.extract_total_count(payload_y)
        res_y = api.extract_search_results(payload_y)
        yrs_y = _years_of(res_y)
        print(f"    필터 적용 : 200 OK 총 {cnt_y}건, "
              f"이번 응답 연식 {min(yrs_y) if yrs_y else '-'}~{max(yrs_y) if yrs_y else '-'}")
        want = f"{target['year_from']}~{target['year_to']}"
        out_of_range = [y for y in yrs_y if not (target["year_from"] <= y <= target["year_to"])]
        if out_of_range:
            print(f"    -> 범위({want}) 밖 매물이 {len(out_of_range)}건 섞여 있습니다: "
                  f"{sorted(set(out_of_range))}")
            warn("연식 필터가 무시되고 있습니다. 수집 후 코드에서 거릅니다.")
        elif cnt_y == cnt_no and yrs_no and (min(yrs_no) < target["year_from"]
                                             or max(yrs_no) > target["year_to"]):
            warn("총 건수가 필터 없을 때와 같습니다. 필터가 무시될 가능성이 있습니다.")
        else:
            print(f"    -> 연식 필터 작동 확인 ({want} 범위 내, "
                  f"총건수 {cnt_no} -> {cnt_y}건으로 감소)")

    # ---------------- 8. 페이징 ----------------
    print("\n[8] 페이징(sr)이 실제로 작동하는지  — 매물이 충분한 넓은 조건으로 확인")
    # 좁은 조건으로 검사하면 2페이지가 비어 판정 자체가 되지 않는다.
    # 조건을 최대한 풀어 표본을 충분히 확보한 뒤 페이징만 본다.
    q_broad = api.build_query(target, ep["ad_type"], include_year=False,
                              include_model=False, include_hidden=False,
                              include_car_type=False, include_ad=False)
    code_b, payload_b, _, _ = client.raw_get(
        ep["url"], {"count": "true", "q": q_broad, "sr": api.build_sr(0, 1, config.SORT_KEY)},
        stage="page:total")
    broad_total = api.extract_total_count(payload_b) if code_b == 200 else None
    print(f"    검증용 넓은 조건 총 매물: {broad_total}건")

    if not broad_total or broad_total < 10:
        warn("표본이 적어 페이징을 제대로 검증할 수 없습니다.")
    else:
        pages = {}
        for off in (0, 5):
            _c, _p, _, _ = client.raw_get(
                ep["url"], {"count": "true", "q": q_broad,
                            "sr": api.build_sr(off, 5, config.SORT_KEY)},
                stage=f"page:{off}")
            ids = _ids_of(api.extract_search_results(_p)) if _p else []
            pages[off] = ids
            print(f"    sr=|{config.SORT_KEY}|{off}|5   -> {len(ids)}건  {ids}")

        a, b = pages.get(0, []), pages.get(5, [])
        overlap = set(a) & set(b)
        if not a or not b:
            warn("페이지를 못 받아 판정 불가.")
        elif overlap:
            warn(f"두 페이지가 {len(overlap)}건 겹칩니다 — 페이징이 안 먹습니다.")
        else:
            print("    -> 페이징 작동 확인 (두 페이지 중복 0건)")

        # 한 번에 최대 몇 건까지 주는지 (전체를 긁으려면 이 상한을 알아야 한다)
        for size in (50, 100, 200):
            _c, _p, _, _ = client.raw_get(
                ep["url"], {"count": "true", "q": q_broad,
                            "sr": api.build_sr(0, size, config.SORT_KEY)},
                stage=f"page:size{size}")
            got_n = len(api.extract_search_results(_p)) if _p else 0
            cap = "  <-- 요청보다 적게 옴 (서버 상한)" if got_n < min(size, broad_total) else ""
            print(f"    한 번에 {size}건 요청 -> {got_n}건 수신{cap}")
            if got_n < min(size, broad_total):
                break

        # 깊은 오프셋에서도 결과가 나오는지
        deep = min(broad_total - 5, 200)
        if deep > 10:
            _c, _p, _, _ = client.raw_get(
                ep["url"], {"count": "true", "q": q_broad,
                            "sr": api.build_sr(deep, 5, config.SORT_KEY)},
                stage="page:deep")
            ids_d = _ids_of(api.extract_search_results(_p)) if _p else []
            print(f"    깊은 오프셋 {deep} -> {len(ids_d)}건 "
                  f"{'(정상)' if ids_d else '<-- 깊은 페이지를 못 받음'}")

    # ---------------- 9. 상세 API ----------------
    # 상세 조회는 '실제로 채택될' 매물로 한다. 첫 결과가 M70 리스 매물이면
    # 진단이 엉뚱한 차를 보게 된다.
    passed, rejected = [], []
    for r in results:
        li = api.normalize_listing(r, target)
        why = api.match_reason(li, target)
        (rejected if why else passed).append((li, why))

    print(f"\n    이번 응답 {len(results)}건 중 채택 {len(passed)}건 / 제외 {len(rejected)}건")
    for li, why in rejected[:6]:
        print(f"      제외: {li.get('trim') or '?':16} {li.get('sell_type') or '':8} {why}")
    if rejected and len(rejected) > 6:
        print(f"      ... 외 {len(rejected)-6}건")

    if passed:
        norm = passed[0][0]
    elif not norm.get("vehicle_id"):
        warn("채택된 매물이 없어 상세 API 를 시험하지 못했습니다.")
        return 0
    else:
        warn("조건을 통과한 매물이 없어 첫 매물로 상세 API 만 시험합니다.")

    vid = norm.get("vehicle_id")
    if not vid:
        warn("매물 ID 를 못 찾아 상세 API 를 시험하지 못했습니다.")
        return 0

    print(f"\n[9] 상세 API 시험 호출  (매물 ID = {vid})")
    detail_probes = [
        ("상세(차량번호/옵션)", api.DETAIL_URL.format(vid=vid), {"include": api.DETAIL_INCLUDE}),
        ("사고이력(record)", api.RECORD_URL.format(vid=vid), None),
        ("성능점검(inspection)", api.INSPECT_URL.format(vid=vid), None),
        ("엔카진단(diagnosis)", api.DIAGNOSIS_URL.format(vid=vid), None),
    ]
    got = {}
    for label, url, params in detail_probes:
        code_d, payload_d, snip_d, _ = client.raw_get(url, params, stage=f"detail:{label}")
        got[label] = payload_d
        note = ""
        if code_d == 200 and payload_d is not None:
            note = f"필드 {len(payload_d) if isinstance(payload_d, dict) else '-'}개"
        elif code_d == 404:
            note = "경로가 다르거나 이 매물엔 해당 정보 없음"
        elif snip_d:
            note = snip_d[:80]
        print(f"    {label:22} {_status(code_d):10} {note}")

    detail = got.get("상세(차량번호/옵션)")
    if not detail:
        warn("상세 응답을 못 받아 차량번호/옵션을 확인하지 못했습니다.")
        return 0

    # 상세 응답 '전체' 를 저장한다. 옵션 위치를 찾으려면 잘린 요약이 아니라
    # 원본이 필요하다.
    raw_detail = os.path.join(os.path.dirname(DETAILS_JSON), "raw_detail.json")
    write_json(raw_detail, {"vehicle_id": vid, "detail": detail,
                            "record": got.get("사고이력(record)"),
                            "inspection": got.get("성능점검(inspection)"),
                            "diagnosis": got.get("엔카진단(diagnosis)")})
    print(f"\n    상세 응답 전체 저장 -> {raw_detail}")

    plate = api.pick(detail, "vehicleNo", "VehicleNo", "carNo", "CarNo",
                     "vehicle.vehicleNo", "manage.vehicleNo")
    print(f"    차량번호 추출: {plate!r}")
    if not plate:
        warn("차량번호를 못 찾았습니다. raw_detail.json 에서 번호판 키를 찾아 알려주세요.")

    # ---------------- 10. 옵션 배열 위치 찾기 ----------------
    print("\n[10] 옵션 배열 탐색  (에어서스 판별의 핵심)")
    arrays = api.find_arrays(detail)
    if not arrays:
        warn("상세 응답에 배열이 하나도 없습니다.")
    else:
        print(f"    상세 응답 안의 배열 {len(arrays)}개:")
        for a in arrays:
            kinds = "/".join(a["kinds"]) or "빈배열"
            sample = repr(a["sample"])[:90]
            tag = ""
            if api.looks_like_option_names(a["sample"]):
                tag = "   <== 옵션명 배열로 보임"
            elif api.looks_like_code_map(a["sample"]):
                tag = "   <== 코드→이름 변환표로 보임"
            elif api.looks_like_option_codes(a["sample"]):
                tag = "   <== 옵션 코드 배열로 보임 (이름 없음)"
            print(f"      {a['path']:38} len={a['len']:<4} [{kinds}] {sample}{tag}")

    found_map: dict[str, str] = {}
    names, codes, src = api.extract_options(detail)
    print(f"\n    추출된 옵션명 {len(names)}개 (출처: {src})")
    if names:
        print(f"      {', '.join(names[:25])}")
    if codes:
        print(f"    추출된 옵션 코드 {len(codes)}개: {', '.join(codes[:30])}")

    if not names and codes:
        warn("옵션이 '이름' 없이 '코드' 로만 옵니다. 코드→이름 변환표가 필요합니다.")
        print("\n    코드 변환표가 있을 만한 경로를 시험합니다:")
        for label, url, params in api.option_map_candidates(vid):
            # 후보 경로 하나가 죽어도 진단 전체를 날리지 않는다.
            try:
                code_o, payload_o, snip_o, _ = client.raw_get(url, params, stage=f"opt:{label}")
            except api.EncarUnreachable as e:
                print(f"      {label:20} 연결실패   ({str(e.detail)[:60]})")
                continue
            except RuntimeError as e:
                print(f"      {label:20} 실패       ({str(e)[:60]})")
                continue
            hit = ""
            if payload_o is not None:
                cmap = api.build_code_map(payload_o)
                if cmap:
                    resolved = [cmap[c] for c in codes if c in cmap]
                    found_map = cmap
                    hit = (f"  <== 코드→이름 변환표 {len(cmap)}개 발견! "
                           f"이 매물 코드 {len(resolved)}/{len(codes)}건 해석됨")
                else:
                    n2, _c2, s2 = (api.extract_options(payload_o)
                                   if isinstance(payload_o, dict) else ([], [], ""))
                    arrs = api.find_arrays(payload_o)
                    named = [a for a in arrs if api.looks_like_option_names(a["sample"])]
                    if n2:
                        hit = f"  <== 옵션명 {len(n2)}개 발견! (출처 {s2})"
                    elif named:
                        hit = (f"  <== 이름 배열 후보: {named[0]['path']} "
                               f"{repr(named[0]['sample'])[:60]}")
            print(f"      {label:20} {_status(code_o):10}{hit}")
            if found_map:
                names2 = [found_map[c] for c in codes if c in found_map]
                print(f"         -> 이 매물 옵션: {', '.join(names2[:20]) or '(해석 실패)'}")
                break

        if not found_map:
            print("\n    변환표를 찾지 못했습니다. data/raw_detail.json 을 "
                  "저장해 두었으니 알려주세요.")

    nd = api.normalize_detail(vid, detail, got.get("사고이력(record)"),
                              got.get("성능점검(inspection)"),
                              got.get("엔카진단(diagnosis)"), target,
                              code_map=found_map or None)
    print(f"\n[11] 최종 파싱 결과")
    for k in ("plate_no", "options_count", "option_source", "airsus_status",
              "airsus_keyword_hits", "accident_free", "encar_diagnosed",
              "encar_check", "direct_inspected", "origin_price_manwon",
              "warranty", "view_count", "subscribe_count"):
        v = nd.get(k)
        print(f"    {k:22}= {v!r}")
    if nd.get("airsus_status", "").startswith("판별불가"):
        warn("에어서스 판별이 불가능한 상태입니다. 이 프로젝트의 핵심 지표이므로 "
             "[10] 출력을 알려주시면 파서를 맞추겠습니다.")

    print("\n" + "=" * 74)
    print(" 진단 끝. '<--' 표시나 경고가 있으면 그 줄을 그대로 복사해서 알려주세요.")
    print("=" * 74)
    return 0


def cmd_discover(args) -> int:
    """제조사 → 모델그룹 → 모델 순으로 엔카의 '실제 표기' 를 확인한다."""
    ensure_dirs()
    client = api.EncarClient(config.COLLECT)
    ep = config.ENDPOINTS[config.USE_ENDPOINTS[0]]
    ctype = args.car_type or "N"

    if args.q:
        q = args.q
        level = "직접 지정한 쿼리"
    elif args.mfr and args.mg:
        q = (f"(And.(And.Hidden.N._.MultiViewHidden.N._."
             f"(C.CarType.{ctype}._.(C.Manufacturer.{args.mfr}._.ModelGroup.{args.mg}.)))"
             f"_.(Or.AdType.{ep['ad_type']}._.MultiViewAdType.{ep['ad_type']}.))")
        level = f"{args.mfr} > {args.mg} 의 하위 모델"
    elif args.mfr:
        q = (f"(And.(And.Hidden.N._.MultiViewHidden.N._."
             f"(C.CarType.{ctype}._.Manufacturer.{args.mfr}.))"
             f"_.(Or.AdType.{ep['ad_type']}._.MultiViewAdType.{ep['ad_type']}.))")
        level = f"{args.mfr} 의 모델그룹"
    else:
        q = (f"(And.(And.Hidden.N._.MultiViewHidden.N._.CarType.{ctype}.)"
             f"_.(Or.AdType.{ep['ad_type']}._.MultiViewAdType.{ep['ad_type']}.))")
        level = f"CarType.{ctype} 의 제조사"

    print("=" * 74)
    print(f" 엔카 표기 확인 — {level}")
    print("=" * 74)
    print(f"  q = {q}")

    code, payload, snippet, url = client.raw_get(
        ep["url"], {"count": "true", "q": q, "sr": api.build_sr(0, 1, config.SORT_KEY)},
        stage="discover")
    print(f"  {_status(code)}   총 {api.extract_total_count(payload)}건")
    if code != 200 or payload is None:
        warn(f"조회 실패. 응답 앞부분: {snippet[:200] if snippet else '-'}")
        warn("CarType 값이 다를 수 있습니다. --car-type Y 로도 시도해 보세요.")
        return 1

    write_json(os.path.join(os.path.dirname(DETAILS_JSON), "raw_discover.json"), payload)

    printed = False
    for key, val in payload.items():
        if not isinstance(val, list) or not val or not isinstance(val[0], dict):
            continue
        if "set" not in key.lower():
            continue
        printed = True
        print(f"\n  === {key} ===")
        for item in val[:100]:
            nm = api.pick(item, "Value", "value", "Name", "name", "Code", "code")
            cnt = api.pick(item, "Count", "count")
            print(f"    {str(nm):30} {cnt if cnt is not None else ''}")

    if not printed:
        print("\n  facet 목록을 못 찾았습니다. 응답 최상위 키:")
        for line in _describe(payload, max_depth=1):
            print("    " + line)
    else:
        print("\n  위 목록에서 원하는 표기를 골라 config.py 의 TARGETS 에 넣으면 됩니다.")
    return 0


# ---------------------------------------------------------------------------
# 실패 보고
# ---------------------------------------------------------------------------
def _unreachable_msg(e, collected: int | None = None) -> str:
    lines = [
        "",
        "=" * 68,
        "엔카 서버에 연결하지 못했습니다 — 중단합니다.",
        "=" * 68,
        f"  막힌 단계 : {e.stage}",
        f"  분류      : 도달 불가 (엔카의 차단이 아님)",
        f"  {e.detail}",
    ]
    if collected is not None:
        lines.append(f"  확보한 매물: {collected}건 (저장 완료)")
    lines += [
        "",
        "  확인할 것:",
        "    1. 브라우저에서 www.encar.com 이 열리는지",
        "    2. 회사/기관 네트워크나 프록시가 막고 있지 않은지",
        "       (HTTPS_PROXY 환경변수가 설정돼 있다면 그 프록시의 정책일 수 있음)",
        "    3. VPN / 방화벽 / DNS 설정",
        "",
        "  네트워크 없이 2·3단계만 점검하려면:",
        "    python samples/make_fixture.py && python collect.py --fixture samples/",
        "=" * 68,
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 본 수집
# ---------------------------------------------------------------------------
def _pick_for_detail(rows: list[dict], target: dict, top_n: int) -> list[dict]:
    """상세를 받을 매물을 고른다.

    검색 결과만으로 계산 가능한 항목(시세 잔차 + 배터리 보증 잔여)으로
    임시 순위를 매긴다. 무사고/옵션 같은 가점은 상세가 있어야 알 수 있으므로
    여기서는 쓸 수 없다.
    """
    import scoring
    from common import to_float, to_int

    if len(rows) <= top_n:
        return list(rows)

    enriched = [scoring.enrich(dict(r)) for r in rows]
    market = scoring.fit_market(enriched, target["key"], target["label"])

    scored = []
    for orig, e in zip(rows, enriched):
        sc = 0.0
        pred = market.predict(to_float(e.get("age_years")), to_int(e.get("mileage_km")))
        price = to_float(e.get("price_manwon"))
        if pred and price:
            sc += (pred - price) / pred * 100.0       # 저평가일수록 높게
        frac = to_float(e.get("battery_remaining_pct"))
        if frac is not None:
            sc += frac / 10.0                         # 보증 잔여를 보조 지표로
        scored.append((sc, orig))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in scored[:top_n]]


def collect_target(client, target: dict, limit: int, with_detail: bool,
                   details_sink: dict) -> list[dict]:
    """한 차종을 수집한다.

    /premium 과 /general 은 서로 다른 광고 상품의 매물을 돌려주므로
    둘 다 훑어서 vehicle_id 로 중복을 제거해야 시세 표본이 한쪽으로
    치우치지 않는다. 404 가 나는 엔드포인트는 건너뛴다.
    """
    log(f"[{target['label']}] 검색 시작")
    if not target.get("confirmed", False):
        warn(f"  제조사/모델 표기가 추정값입니다 "
             f"({target['manufacturer']} / {target['model_group']} / {target.get('model')}). "
             f"0건이면 `--discover --mfr {target['manufacturer']}` 로 확인하세요.")

    rows: list[dict] = []
    seen: set[str] = set()

    # 옵션이 코드로 오는 경우를 대비해 변환표를 1회만 받아 둔다
    code_map = client.option_code_map()
    if code_map:
        log(f"  옵션 코드 변환표 {len(code_map)}개 확보")

    for ep_name in config.USE_ENDPOINTS:
        if len(rows) >= limit:
            break
        ep = config.ENDPOINTS[ep_name]
        q = api.build_query(target, ep["ad_type"], include_year=True,
                            include_model=bool(target.get("model")))
        log(f"  [{ep_name}] q = {q}")

        offset, page_size, total = 0, config.PAGE_SIZE, None
        while len(rows) < limit:
            try:
                payload = client.search(q, ep["url"], offset=offset,
                                        limit=min(page_size, limit - len(rows)),
                                        sort=config.SORT_KEY, stage=f"search:{ep_name}")
            except RuntimeError as e:
                # 404 등 이 엔드포인트만의 문제면 다음 엔드포인트로 넘어간다
                if isinstance(e, (api.EncarBlocked, api.EncarUnreachable)):
                    raise
                warn(f"  [{ep_name}] 사용 불가 — 건너뜁니다 ({e})")
                break

            if total is None:
                total = api.extract_total_count(payload)
                log(f"  [{ep_name}] 엔카 검색 결과 총 {total}건")
            results = api.extract_search_results(payload)
            if not results:
                break

            added = 0
            for raw in results:
                listing = api.normalize_listing(raw, target)
                vid = listing.get("vehicle_id")
                if not vid or vid in seen:
                    continue
                if not api.matches_target(listing, target):
                    continue
                seen.add(vid)
                rows.append(listing)
                details_sink.setdefault(vid, {})["search"] = raw
                added += 1

            offset += len(results)
            if total is not None and offset >= total:
                break
        log(f"  [{ep_name}] 누적 {len(rows)}건")

    log(f"  조건 부합 {len(rows)}건 "
        f"(연식 {target['year_from']}~{target['year_to']}, 트림 필터 적용, 중복 제거)")

    if not rows:
        warn("  0건입니다. `python collect.py --probe` 로 원인을 확인하세요.")
        return rows

    if not with_detail:
        for r in rows:
            r["detail_fetched"] = False
            r["collected_at"] = datetime.now().isoformat(timespec="seconds")
        return rows

    # 상세는 매물당 4회 요청이라 전량 조회하면 시간이 폭발한다.
    # 검색 결과만으로 매길 수 있는 1차 점수(시세 잔차 + 배터리 잔여)로
    # 상위 N 대만 고른다. 나머지는 시세 회귀 표본으로만 쓴다.
    top_n = int(config.COLLECT.get("detail_top_n", 50))
    picked = _pick_for_detail(rows, target, top_n)
    picked_ids = {r["vehicle_id"] for r in picked}

    est_min = len(picked) * 4 * config.COLLECT["request_interval_sec"] / 60
    log(f"  상세 조회 대상 {len(picked)}/{len(rows)}건 (예상 {est_min:.0f}분)")
    if len(rows) > len(picked):
        log(f"  나머지 {len(rows) - len(picked)}건은 시세 회귀 표본으로만 사용합니다")

    for r in rows:
        r["detail_fetched"] = r["vehicle_id"] in picked_ids
        r["collected_at"] = datetime.now().isoformat(timespec="seconds")

    for i, listing in enumerate(picked, 1):
        vid = listing["vehicle_id"]
        log(f"  상세 {i}/{len(picked)} (id={vid})")
        detail = client.detail(vid)
        record = client.record(vid)
        inspection = client.inspection(vid)
        diagnosis = client.diagnosis(vid)

        bucket = details_sink.setdefault(vid, {})
        bucket.update({"detail": detail, "record": record,
                       "inspection": inspection, "diagnosis": diagnosis})

        listing.update(api.normalize_detail(vid, detail, record, inspection,
                                            diagnosis, target, code_map=code_map))
        listing["detail_fetched"] = True
        listing["collected_at"] = datetime.now().isoformat(timespec="seconds")

    detailed = [r for r in rows if r.get("detail_fetched")]
    undet = [r["vehicle_id"] for r in detailed
             if str(r.get("airsus_status", "")).startswith("판별불가")]
    if undet:
        warn(f"에어서스 판별 불가 {len(undet)}건 — 옵션이 코드로만 왔고 변환표가 없습니다. "
             f"`--probe` 의 [10] 출력을 확인하세요.")

    missing_plate = [r["vehicle_id"] for r in detailed if not r.get("plate_no")]
    if missing_plate:
        warn(f"차량번호 미확보 {len(missing_plate)}건 — 상세 응답 키 확인 필요 "
             f"(예: {missing_plate[:3]})")
    return rows


def cmd_collect(args) -> int:
    ensure_dirs()
    if os.path.exists(BLOCKED_FLAG):
        os.remove(BLOCKED_FLAG)

    limit = args.limit or config.COLLECT["max_listings_per_model"]
    targets = [t for t in config.TARGETS if not args.model or t["key"] == args.model]
    if not targets:
        die(f"--model {args.model} 에 해당하는 대상이 config.TARGETS 에 없습니다.")

    client = api.EncarClient(config.COLLECT)
    all_rows: list[dict] = []
    details: dict = {}
    blocked: api.EncarBlocked | None = None
    unreachable: api.EncarUnreachable | None = None

    for target in targets:
        try:
            all_rows.extend(collect_target(client, target, limit, not args.no_detail, details))
        except api.EncarBlocked as e:
            blocked = e
            break
        except api.EncarUnreachable as e:
            unreachable = e
            break

    for r in all_rows:
        r.setdefault("collected_at", datetime.now().isoformat(timespec="seconds"))
    write_csv(LISTINGS_CSV, all_rows, LISTING_FIELDS)
    write_json(DETAILS_JSON, details)
    log(f"저장: {LISTINGS_CSV} ({len(all_rows)}건), {DETAILS_JSON}")

    if unreachable:
        print(_unreachable_msg(unreachable, len(all_rows)), file=sys.stderr)
        return 3

    if blocked:
        msg = (
            f"차단 감지 — 수집을 즉시 중단했습니다.\n"
            f"  막힌 단계 : {blocked.stage}\n"
            f"  HTTP 상태 : {blocked.status}\n"
            f"  내용      : {blocked.detail}\n"
            f"  중단 시각 : {datetime.now().isoformat(timespec='seconds')}\n"
            f"  확보한 매물: {len(all_rows)}건 (저장 완료)\n\n"
            f"대처: 최소 수십 분 이상 간격을 두고 다시 실행하세요. "
            f"요청 간격(config.COLLECT['request_interval_sec'])을 줄이지 마세요.\n"
        )
        with open(BLOCKED_FLAG, "w", encoding="utf-8") as f:
            f.write(msg)
        print("\n" + "=" * 60, file=sys.stderr)
        print(msg, file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        return 2

    if not all_rows:
        warn("수집된 매물이 0건입니다. --discover / --probe 로 쿼리를 점검하세요.")
        return 1
    return 0


def cmd_reparse(args) -> int:
    """이미 받은 data/details.json 을 네트워크 없이 다시 해석한다.

    옵션 코드 변환표(data/option_codes.json)를 새로 채웠을 때, 12분짜리
    수집을 다시 돌리지 않고 즉시 반영하기 위한 것이다.
    """
    ensure_dirs()
    details = read_json(DETAILS_JSON) or {}
    if not details:
        die(f"{DETAILS_JSON} 가 없습니다. 먼저 `python collect.py` 를 실행하세요.")

    prev = {str(r.get("vehicle_id")): r for r in read_csv(LISTINGS_CSV)}
    targets = {t["key"]: t for t in config.TARGETS}
    code_map = api.load_local_option_map()
    if code_map:
        log(f"옵션 코드 변환표 {len(code_map)}개 적용")
    else:
        warn("옵션 코드 변환표가 없습니다 (data/option_codes.json). "
             "`python infer_options.py` 로 만들 수 있습니다.")

    rows, before, after = [], 0, 0
    for vid, bucket in details.items():
        if not isinstance(bucket, dict):
            continue
        old = prev.get(str(vid), {})
        target = targets.get(old.get("model_key")) or config.TARGETS[0]

        search = bucket.get("search")
        listing = api.normalize_listing(search, target) if search else dict(old)
        listing["model_key"] = target["key"]
        listing["model_label"] = target["label"]

        if str(old.get("airsus_status", "")).startswith("판별불가"):
            before += 1

        if bucket.get("detail") is not None:
            listing.update(api.normalize_detail(
                str(vid), bucket.get("detail"), bucket.get("record"),
                bucket.get("inspection"), bucket.get("diagnosis"),
                target, code_map=code_map))
            listing["detail_fetched"] = True
        else:
            listing["detail_fetched"] = old.get("detail_fetched", False)

        if str(listing.get("airsus_status", "")).startswith("판별불가"):
            after += 1
        listing["collected_at"] = old.get("collected_at", "")
        rows.append(listing)

    write_csv(LISTINGS_CSV, rows, LISTING_FIELDS)
    log(f"재해석 완료: {LISTINGS_CSV} ({len(rows)}건)")

    detailed = [r for r in rows if r.get("detail_fetched")]
    ok_air = sum(1 for r in detailed if r.get("airsus_status") == "확인")
    print(f"\n  상세 확보 {len(detailed)}건 중")
    print(f"    에어서스 확인   : {ok_air}건")
    print(f"    판별 불가       : {after}건 (재해석 전 {before}건)")
    if before and after < before:
        print(f"    -> {before - after}건이 새로 판별되었습니다.")
    elif after:
        print("    -> 변환표에 해당 코드가 없습니다. "
              "`python infer_options.py` 를 확인하세요.")
    return 0


def cmd_fixture(args) -> int:
    """네트워크 없이 samples/ 픽스처로 listings.csv / details.json 을 만든다."""
    ensure_dirs()
    src = args.fixture
    path = src if os.path.isfile(src) else os.path.join(src, "sample_search.json")
    payload = read_json(path)
    if payload is None:
        die(f"픽스처를 못 읽었습니다: {path}")

    all_rows, details = [], {}
    for target in config.TARGETS:
        raws = payload.get(target["key"], [])
        for raw in raws:
            listing = api.normalize_listing(raw.get("search", raw), target)
            if not listing.get("vehicle_id"):
                continue
            vid = listing["vehicle_id"]
            details[vid] = raw
            listing.update(api.normalize_detail(
                vid, raw.get("detail"), raw.get("record"),
                raw.get("inspection"), raw.get("diagnosis"), target))
            listing["collected_at"] = datetime.now().isoformat(timespec="seconds")
            all_rows.append(listing)

    write_csv(LISTINGS_CSV, all_rows, LISTING_FIELDS)
    write_json(DETAILS_JSON, details)
    log(f"[fixture] 저장: {LISTINGS_CSV} ({len(all_rows)}건), {DETAILS_JSON}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="엔카 전기차 매물 수집 (1단계)")
    p.add_argument("--probe", action="store_true", help="검색 API 1회 호출 후 스키마 덤프")
    p.add_argument("--discover", action="store_true", help="제조사/모델 facet 값 덤프")
    p.add_argument("--fixture", metavar="PATH", help="네트워크 없이 샘플 데이터로 실행")
    p.add_argument("--reparse", action="store_true",
                   help="이미 받은 details.json 을 네트워크 없이 다시 해석 "
                        "(옵션 변환표를 새로 채운 뒤 사용)")
    p.add_argument("--model", metavar="KEY", help="config.TARGETS 의 key 하나만 처리")
    p.add_argument("--limit", type=int, help="모델당 최대 건수")
    p.add_argument("--no-detail", action="store_true", help="상세 API 생략")
    p.add_argument("--q", help="검색 q 파라미터 직접 지정 (브라우저에서 복사한 값)")
    p.add_argument("--mfr", help="--discover 전용: 이 제조사의 모델그룹을 조회")
    p.add_argument("--mg", help="--discover 전용: 이 모델그룹의 하위 모델을 조회")
    p.add_argument("--car-type", dest="car_type",
                   help="--discover 전용: CarType 값 (기본 N = 확인된 수입차 값)")
    args = p.parse_args()

    try:
        if args.probe:
            return cmd_probe(args)
        if args.discover:
            return cmd_discover(args)
        if args.reparse:
            return cmd_reparse(args)
        if args.fixture:
            return cmd_fixture(args)
        return cmd_collect(args)
    except api.EncarBlocked as e:
        ensure_dirs()
        msg = (f"차단 감지 — 즉시 중단.\n  막힌 단계: {e.stage}\n"
               f"  HTTP: {e.status}\n  내용: {e.detail}\n")
        with open(BLOCKED_FLAG, "w", encoding="utf-8") as f:
            f.write(msg)
        print("\n" + msg, file=sys.stderr)
        return 2
    except api.EncarUnreachable as e:
        # 엔카의 차단이 아니므로 BLOCKED.txt 는 남기지 않는다.
        print(_unreachable_msg(e), file=sys.stderr)
        return 3
    except RuntimeError as e:
        # 트레이스백을 그대로 쏟지 않고 원인만 보고한다.
        print(f"\n실패: {e}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
