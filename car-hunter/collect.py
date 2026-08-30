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
import glob
import json
import os
import re
import sys
from datetime import datetime

import config
import encar_api as api
import history
from common import (
    BLOCKED_FLAG, DETAILS_JSON, LISTINGS_CSV, SAMPLES_DIR,
    die, ensure_dirs, log, read_csv, read_json, warn, write_csv, write_json,
)

LISTING_FIELDS = [
    "model_key", "model_label", "vehicle_id", "plate_no",
    "price_manwon", "year", "month", "mileage_km", "region",
    "trim", "trim_detail", "sell_type",
    "record_available", "accident_free", "accident_summary",
    "accident_my_count", "accident_other_count", "accident_count",
    "accident_my_cost_won", "accident_other_cost_won",
    "owner_change_count", "plate_change_count",
    "total_loss_count", "flood_total_count", "flood_part_count", "theft_count",
    "past_rental_count", "past_business_count", "past_government_count",
    "past_commercial_use",
    "inspection_available", "insp_leak", "insp_corrosion", "insp_tire",
    "insp_repair_notes", "insp_repair_penalty", "insp_worst_rank",
    "insp_worst_status",
    "insp_unclassified", "insp_diagnostics", "battery_pack_damage",
    "repair_source",
    "insp_mileage", "insp_waterlog", "insp_recall", "insp_recall_types",
    "insp_comments", "insp_tuning", "insp_usage_change", "insp_serious",
    "insp_vin", "insp_accident_flag", "insp_simple_repair", "insp_needs_repair",
    "accident_lines", "accident_type_verdict",
    "use_history", "record_fields_null",
    "first_registration_date",
    "flood_or_total_loss", "rental_or_commercial", "one_owner",
    "encar_diagnosed", "encar_check", "direct_inspected",
    "page_available", "repair_grade_source", "page_repair_notes",
    "page_repair_penalty", "page_worst_rank", "page_worst_status",
    "page_unmatched_parts", "page_status_unknown", "page_ranks_read",
    "page_mileage_gauge", "page_mileage",
    "page_vin_state", "page_tuning", "page_special_history", "page_usage_change",
    "page_recall", "page_recall_done", "page_accident_history", "page_simple_repair",
    "page_first_registration", "page_inspection_valid", "page_inspector_note",
    "page_js_suspect", "page_detail_bad", "page_detail_unknown",
    "page_ev_hv_bad", "page_ev_hv_unknown", "page_ev_hv_checked", "page_parse_note",
    "page_is_image",
    "first_advertised", "days_on_market", "re_registered", "lease_rent_info",
    "insurance_not_joined", "loan_count", "first_seen", "last_seen",
    "price_first_manwon", "price_prev_manwon", "price_change_manwon",
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

    # ---------------- 9-1. 보험이력(record) 전체 구조 ----------------
    record = got.get("사고이력(record)")
    print("\n[9-1] 보험이력(record) 응답 전체 구조")
    if not record:
        warn("record 응답을 못 받았습니다. 사고이력 점수를 매길 수 없습니다.")
    else:
        keys = list(record) if isinstance(record, dict) else []
        print(f"    최상위 필드 {len(keys)}개:")
        for line in _describe(record, max_depth=2):
            print("      " + line)

        rec = api.normalize_record(record)
        nulls = set(rec.get("record_fields_null", []))
        print("\n    -- 우리가 쓰는 항목으로의 매핑 --")
        for std in api.RECORD_FIELDS:
            v = rec.get(std)
            if v is not None:
                mark = ""
            elif std in nulls:
                # 키는 응답에 있는데 값이 null 인 경우. 0 으로 바꾸지 않는다.
                mark = "   (응답에 있으나 값이 null — 정보없음)"
            else:
                mark = "   <-- 응답에 없음"
            print(f"      {std:22}= {v!r}{mark}")
        if rec.get("use_history"):
            print(f"      {'use_history':22}= {rec['use_history']}")
        missing = [k for k in api.RECORD_FIELDS
                   if rec.get(k) is None and k not in nulls]
        if missing:
            warn(f"매핑 못한 항목 {len(missing)}개: {missing}")
            print("      위 '최상위 필드' 목록에서 대응되는 실제 키를 찾아 알려주시면")
            print("      encar_api.RECORD_FIELDS 에 추가하겠습니다.")
        if rec["accident_details"]:
            print(f"\n    -- 사고 건별 상세 {len(rec['accident_details'])}건 --")
            for a in rec["accident_details"][:5]:
                print(f"      {a}")
        else:
            print("\n    사고 건별 상세 배열은 없습니다 "
                  "(수리 유형 구분은 성능점검이 필요합니다).")

    # ---------------- 9-2. 성능점검 경로 찾기 ----------------
    print("\n[9-2] 성능점검(누유/부식/타이어) 경로 후보 시험")
    insp_found = None
    for label, url, params in api.inspection_candidates(vid):
        try:
            code_i, payload_i, snip_i, _ = client.raw_get(url, params, stage=f"insp:{label}")
        except api.EncarUnreachable as e:
            print(f"    {label:22} 연결실패")
            continue
        except RuntimeError as e:
            print(f"    {label:22} 실패 ({str(e)[:50]})")
            continue

        hint = ""
        if code_i == 200 and payload_i:
            txt = " ".join(api.strings_of(payload_i))
            hits = [w for w in ("누유", "부식", "타이어", "판금", "교환", "성능점검")
                    if w in txt]
            if hits:
                hint = f"  <== 성능점검 정보로 보임! (발견: {', '.join(hits)})"
                insp_found = insp_found or (label, url, payload_i)
            else:
                hint = f"  (200 이지만 성능점검 키워드 없음, 필드 {len(payload_i) if isinstance(payload_i, dict) else '-'}개)"
        print(f"    {label:22} {_status(code_i):10}{hint}")

    # 확정 경로(INSPECT_URL)에서 이미 받았으면 그것을 먼저 쓴다
    if got.get("성능점검(inspection)") and not insp_found:
        insp_found = ("inspection/vehicle (확정 경로)",
                      api.INSPECT_URL.format(vid=vid), got["성능점검(inspection)"])

    if insp_found:
        label, url, payload_i = insp_found
        write_json(os.path.join(os.path.dirname(DETAILS_JSON), "raw_inspection.json"),
                   payload_i)
        print(f"\n    -> '{label}' 에서 찾았습니다. 전체 저장: data/raw_inspection.json")
        print(f"       {url}")

        print("\n    -- 성능점검 응답 전체 구조 (children 까지 전부) --")
        for line in _describe(payload_i, max_depth=12)[:400]:
            print("      " + line)

        # 섹션별로 트리를 펼쳐 부위명/상태부호가 어디에 있는지 눈으로 본다
        for sec in ("inners", "outers", "etcs"):
            items = payload_i.get(sec) if isinstance(payload_i, dict) else None
            if items is None:
                continue
            print(f"\n    -- {sec}: {len(items)}개 --")
            if not items:
                print("      (비어 있음)")
                continue
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    print(f"      [{i}] {item!r}")
                    continue
                t = api._title_of(item) or "?"
                st = api._status_of(item)
                ch = item.get("children") or []
                print(f"      [{i}] {t}  code={api._code_of(item)}  "
                      f"status={st or '-'}  children={len(ch)}")
                for j, c in enumerate(ch):
                    if isinstance(c, dict):
                        print(f"          [{i}.{j}] {api._title_of(c) or '?':16} "
                              f"code={api._code_of(c) or '-':6} "
                              f"status={api._status_of(c) or '-':4} "
                              f"raw={ {k: v for k, v in c.items() if k != 'children'} }"[:160])
                    else:
                        print(f"          [{i}.{j}] {c!r}")

        entries = api.find_repair_entries(payload_i)
        print(f"\n    -- 발견한 (부위, 상태부호) {len(entries)}쌍 --")
        for e in entries[:30]:
            rank = api.classify_part(e["part"])
            tag = f"-> {rank}" if rank else "-> 미분류  <== 등급표에 없는 이름"
            print(f"      {e['part']:20} [{e['status']}]  {tag}   ({e['path']})")
        if not entries:
            warn("부위/상태 쌍을 못 찾았습니다. 위 구조에서 수리 부위 배열이 "
                 "어디 있는지 알려주시면 파서를 맞추겠습니다.")

        tree_res = api.score_repairs(entries)
        if tree_res["entries"]:
            print(f"\n    -- 트리에서 나온 부위 (참고) --")
            for g in tree_res["entries"]:
                print(f"      {g['note']}")
        else:
            print("\n    트리에는 판정값이 실려 있지 않습니다 "
                  "(status 가 전부 '-'). 부위 등급은 코멘트에서 뽑습니다.")
        if tree_res.get("diagnostics"):
            print(f"    자기진단 항목: {tree_res['diagnostics']}")

        ni = api.normalize_inspection(payload_i)
        print("\n    -- 누유 / 부식 / 타이어 --")
        for k in ("leak_note", "corrosion_note", "tire_note"):
            v = ni.get(k)
            print(f"      {k:16}= {v if v else '(응답에 없음)'}")

        ni_pre = api.normalize_inspection(payload_i)
        print("\n    -- 점검자 코멘트에서 뽑은 수리 부위 --")
        print(f"      원문: {(ni_pre.get('insp_comments') or '(없음)')[:250]}")
        cres = api.score_comment_parts(ni_pre.get("insp_comments") or "")
        if cres.get("boilerplate_removed"):
            pass
        for g in cres["entries"]:
            print(f"      {g['note']}   [-{g['penalty']}]")
        if not cres["entries"]:
            print("      (부위 언급 없음)")
        print(f"      감점 합계: {cres['penalty']}")
        if cres.get("accident_mentions"):
            print(f"      코멘트 속 사고 언급: {cres['accident_mentions']}")
        if cres.get("unmatched"):
            warn(f"코멘트에서 못 알아본 단어: {cres['unmatched']}")
            print("      부위명이면 알려주세요. config.COMMENT_PART_ALIASES 에 넣겠습니다.")

        print("\n    -- master.detail 주요 필드 --")
        for k in ("insp_mileage", "insp_waterlog", "insp_recall", "insp_recall_types",
                  "insp_tuning", "insp_usage_change", "insp_serious", "insp_vin",
                  "insp_accident_flag", "insp_simple_repair", "insp_needs_repair",
                  "insp_comments"):
            v = ni.get(k)
            print(f"      {k:20}= {v if v not in (None, '') else '(응답에 없음)'}")
        if ni.get("insp_mileage") is not None:
            shown = norm.get("mileage_km")
            gap = ni["insp_mileage"] - (shown or 0)
            print(f"\n      성능점검 주행거리 {ni['insp_mileage']:,}km vs "
                  f"매물 표시 {shown:,}km  -> 격차 {gap:,}km")
            if gap > 100:
                warn("성능점검 주행거리가 매물 표시보다 큽니다 — 주행거리 조작 의심")

        # outers 가 비어 있으면 다른 매물로도 확인한다.
        # 한 대만 보고 '외판 수리 없음' 이라고 단정할 수 없다.
        if isinstance(payload_i, dict) and not payload_i.get("outers"):
            others = [p[0] for p in passed[1:6]] if len(passed) > 1 else []
            if others:
                print(f"\n    -- outers 가 비어 있어 다른 매물 {len(others)}대로 확인 --")
                for li in others:
                    ovid = li.get("vehicle_id")
                    try:
                        c2, p2, _, _ = client.raw_get(
                            api.INSPECT_URL.format(vid=ovid), None, stage=f"insp2:{ovid}")
                    except (api.EncarUnreachable, RuntimeError):
                        continue
                    if c2 != 200 or not isinstance(p2, dict):
                        print(f"      {ovid}: {_status(c2)}")
                        continue
                    ents = api.find_repair_entries(p2)
                    by_sec: dict[str, int] = {}
                    for e in ents:
                        by_sec[e["section"]] = by_sec.get(e["section"], 0) + 1
                    print(f"      {ovid}: outers={len(p2.get('outers') or [])} "
                          f"inners={len(p2.get('inners') or [])} "
                          f"etcs={len(p2.get('etcs') or [])}  발견 {by_sec}")
                    if p2.get("outers"):
                        write_json(os.path.join(os.path.dirname(DETAILS_JSON),
                                                "raw_inspection_outers.json"), p2)
                        print(f"        -> outers 가 있는 매물을 찾았습니다. "
                              f"저장: data/raw_inspection_outers.json")
                        for e in ents:
                            if e["section"] == "outers":
                                rk = api.classify_part(e["part"])
                                print(f"           {e['part']:16} [{e['status']}] "
                                      f"-> {rk or '미분류'}")
                        break
    else:
        warn("성능점검 경로를 못 찾았습니다. 누유/부식/타이어와 교환·골격 구분은 "
             "당분간 '응답에 없음' 으로 남습니다.")
        print("\n    브라우저에서 직접 찾는 방법:")
        print("      1. 엔카 매물 상세 페이지를 열고 '성능점검기록부' 를 화면에 띄운다")
        print("      2. F12 -> Network 탭 -> Fetch/XHR 필터 -> F5 로 새로고침")
        print("      3. 성능점검 내용이 보이는 시점에 새로 뜨는 요청을 클릭")
        print("      4. Response 탭에서 Ctrl+F 로 '누유' 또는 '타이어' 검색")
        print("      5. 찾으면 그 요청 우클릭 -> Copy -> Copy link address")
        print("      Network 필터 칸에 inspect / performance / record / diagnosis 를")
        print("      쳐 보면 후보가 줄어듭니다.")

    # ---------------- 10. 성능기록부 HTML 페이지 (주 소스) ----------------
    print("\n[10] 성능기록부 HTML 페이지")
    page_url = config.INSPECTION_PAGE_URL.format(vid=vid)
    print(f"    {page_url}")
    page_html = client.inspection_page(vid)
    if not page_html:
        warn("성능기록부 페이지를 못 받았습니다.")
    else:
        raw_page = os.path.join(os.path.dirname(DETAILS_JSON), "raw_inspection_page.html")
        with open(raw_page, "w", encoding="utf-8") as f:
            f.write(page_html)
        print(f"    원본 저장 -> {raw_page} ({len(page_html):,} bytes)")

        parsed = api.parse_inspection_page(page_html)
        print(f"\n    -- 수리 부위 {len(parsed['repairs'])}건 --")
        for r in parsed["repairs"]:
            print(f"      {r['raw'][:26]:28} [{r['status']}] -> "
                  f"{r['part']} / {r['rank']}")
        if not parsed["repairs"]:
            print("      (수리 부위 없음)")
        if parsed["unmatched_parts"]:
            warn(f"못 알아본 부위 표기: {parsed['unmatched_parts']}")
            print("      부위명이면 알려주세요. config 의 부위표에 넣겠습니다.")

        print("\n    -- 라벨 항목 --")
        for k, v in parsed["fields"].items():
            print(f"      {k:22}= {v}")
        missing = [k for k, _ in config.INSPECTION_PAGE_FIELDS
                   if k not in parsed["fields"]]
        if missing:
            warn(f"페이지에서 못 읽은 항목: {missing}")

        print(f"\n    -- 고전원전기장치 (전기차 핵심) --")
        if not parsed["ev_hv"]:
            warn("고전원전기장치 3항목을 못 읽었습니다.")
        for k, v in parsed["ev_hv"].items():
            flag = "  <== 불량" if v["state"] == "불량" else ""
            print(f"      {k:22}= {v['raw']}{flag}")

        print(f"\n    -- 자동차 세부상태 불량 --")
        print(f"      {parsed['detail_bad'] or '(없음)'}")

        np_ = api.normalize_inspection_page(parsed)
        print(f"\n    -- 등급 채점 --")
        for note in (np_["page_repair_notes"] or "").split(" | "):
            if note:
                print(f"      {note}")
        print(f"      감점(등급): {np_['page_repair_penalty']} / "
              f"최악 {np_['page_worst_rank'] or '-'}")
        print(f"      주행거리 계기상태: {np_['page_mileage_gauge'] or '(못 읽음)'}")
        if np_["page_mileage"] is not None:
            shown = norm.get("mileage_km") or 0
            print(f"      성능점검 주행거리 {np_['page_mileage']:,}km vs "
                  f"매물 표시 {shown:,}km -> 격차 {np_['page_mileage']-shown:,}km")

    nd = api.normalize_detail(vid, detail, got.get("사고이력(record)"),
                              got.get("성능점검(inspection)"),
                              got.get("엔카진단(diagnosis)"), target,
                              inspection_html=page_html)
    print(f"\n[11] 최종 파싱 결과")
    for k in ("plate_no", "repair_grade_source", "insp_worst_rank",
              "insp_worst_status", "insp_repair_penalty", "page_mileage_gauge",
              "page_ev_hv_bad", "page_detail_bad", "first_registration_date",
              "accident_free", "encar_diagnosed", "encar_check",
              "origin_price_manwon", "warranty", "view_count", "subscribe_count"):
        v = nd.get(k)
        print(f"    {k:22}= {v!r}")
    if not nd.get("page_available"):
        warn("성능기록부 페이지를 못 읽었습니다. 부위 등급 판정이 불가능합니다.")

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


INCREMENTAL_SLOW_DAYS = 30      # 보험이력·성능기록부를 다시 받는 주기


def plan_detail_fetch(picked: list[dict], full: bool = False) -> dict:
    """매물별로 '새로 받을지 / 저장분을 쓸지' 를 정한다.

    매주 돌리는데 매번 전량을 다시 받으면 40분이 넘고 엔카에도 부담이다.
    대부분의 매물은 지난주와 똑같다. 달라지는 것은 가격뿐이다.

    세 갈래로 나눈다:
      fetch        새 매물이거나 30일이 지났다 -> 전부 새로
      fetch_fast   가격만 바뀌었다 -> 차량정보·진단만 새로 받고,
                   보험이력·성능점검·성능기록부는 저장분을 쓴다
                   (이 셋은 차가 팔리기 전까지 거의 바뀌지 않는다)
      reuse        가격도 그대로다 -> 아무것도 받지 않는다

    --full 이면 전부 fetch 다.
    """
    from common import to_int

    prev_rows = {str(r.get("vehicle_id")): r for r in read_csv(LISTINGS_CSV)}         if os.path.exists(LISTINGS_CSV) else {}
    prev_details = read_json(DETAILS_JSON) or {}
    today = datetime.now()

    out = {"mode": {}, "fetch": [], "fetch_fast": [], "reuse": [],
           "refresh_slow": []}
    for r in picked:
        vid = str(r.get("vehicle_id"))
        cached = prev_details.get(vid) or {}
        old_row = prev_rows.get(vid)

        if full or not cached or not cached.get("detail"):
            out["mode"][vid] = "fetch"
            out["fetch"].append(vid)
            continue

        # 보험이력·성능기록부가 오래됐으면 통째로 다시 받는다.
        stale = True
        ts = cached.get("slow_fetched_at") or cached.get("fetched_at") or ""
        if ts:
            try:
                age = (today - datetime.fromisoformat(ts[:19])).days
                stale = age >= INCREMENTAL_SLOW_DAYS
            except ValueError:
                stale = True
        if stale:
            out["mode"][vid] = "fetch"
            out["fetch"].append(vid)
            out["refresh_slow"].append(vid)
            continue

        price_now = to_int(r.get("price_manwon"))
        price_old = to_int(old_row.get("price_manwon")) if old_row else None
        if old_row is None or price_old is None or price_now != price_old:
            out["mode"][vid] = "fetch_fast"
            out["fetch_fast"].append(vid)
        else:
            out["mode"][vid] = "reuse"
            out["reuse"].append(vid)
    return out


def collect_target(client, target: dict, limit: int, with_detail: bool,
                   details_sink: dict, full: bool = False,
                   prev_details: dict | None = None) -> list[dict]:
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

    _p = plan_detail_fetch(picked, full=full)
    _req = len(_p["fetch"]) * 5 + len(_p["fetch_fast"]) * 2
    est_min = _req * config.COLLECT["request_interval_sec"] / 60
    log(f"  상세 조회 대상 {len(picked)}/{len(rows)}건 (예상 {est_min:.0f}분)")
    if len(rows) > len(picked):
        log(f"  나머지 {len(rows) - len(picked)}건은 시세 회귀 표본으로만 사용합니다")

    for r in rows:
        r["detail_fetched"] = r["vehicle_id"] in picked_ids
        r["collected_at"] = datetime.now().isoformat(timespec="seconds")

    plan = plan_detail_fetch(picked, full=full)
    if not full:
        log(f"  증분: 새로 받을 매물 {len(plan['fetch'])}건, "
            f"저장분 재사용 {len(plan['reuse'])}건"
            + (f", 성능기록부·보험이력만 갱신 {len(plan['refresh_slow'])}건"
               if plan["refresh_slow"] else ""))

    for i, listing in enumerate(picked, 1):
        vid = listing["vehicle_id"]
        mode = plan["mode"].get(vid, "fetch")
        cached = (prev_details or {}).get(vid) or {}

        if mode == "reuse":
            # 가격도 그대로고 최근에 받았다. 다시 두들길 이유가 없다.
            bucket = details_sink.setdefault(vid, {})
            bucket.update({k: cached.get(k) for k in
                           ("detail", "record", "inspection", "diagnosis",
                            "inspection_html")})
            bucket["fetched_at"] = cached.get("fetched_at", "")
            bucket["slow_fetched_at"] = cached.get("slow_fetched_at", "")
            listing.update(api.normalize_detail(
                vid, cached.get("detail"), cached.get("record"),
                cached.get("inspection"), cached.get("diagnosis"), target,
                inspection_html=cached.get("inspection_html")))
            listing["detail_fetched"] = True
            listing["detail_source"] = "저장분 재사용"
            listing["collected_at"] = datetime.now().isoformat(timespec="seconds")
            continue

        slow = (mode == "fetch")          # 성능기록부·보험이력까지 새로
        label = "전체" if slow else "가격변동(빠른 항목만)"
        log(f"  상세 {i}/{len(picked)} (id={vid}) — {label}")

        detail = client.detail(vid)
        diagnosis = client.diagnosis(vid)
        if slow:
            record = client.record(vid)
            inspection = client.inspection(vid)
            page_html = client.inspection_page(vid)
        else:
            # 보험이력·성능점검·성능기록부는 잘 바뀌지 않는다.
            # 30일이 안 지났으면 저장분을 그대로 쓴다.
            record = cached.get("record")
            inspection = cached.get("inspection")
            page_html = cached.get("inspection_html")

        now = datetime.now().isoformat(timespec="seconds")
        bucket = details_sink.setdefault(vid, {})
        bucket.update({"detail": detail, "record": record,
                       "inspection": inspection, "diagnosis": diagnosis,
                       "inspection_html": page_html,
                       "fetched_at": now,
                       "slow_fetched_at": (now if slow else
                                           cached.get("slow_fetched_at", ""))})

        listing.update(api.normalize_detail(vid, detail, record, inspection,
                                            diagnosis, target,
                                            inspection_html=page_html))
        listing["detail_fetched"] = True
        listing["detail_source"] = "전체 조회" if slow else "가격변동 갱신"
        listing["collected_at"] = now

    detailed = [r for r in rows if r.get("detail_fetched")]
    got_page = sum(1 for r in detailed if r.get("page_available"))
    if detailed:
        log(f"  성능기록부 페이지 확보 {got_page}/{len(detailed)}건")
        unmatched = sorted({x for r in detailed
                            for x in str(r.get("page_unmatched_parts") or "").split(", ") if x})
        if unmatched:
            warn(f"성능기록부에서 못 알아본 부위 표기: {unmatched[:12]}")

    missing_plate = [r["vehicle_id"] for r in detailed if not r.get("plate_no")]
    if missing_plate:
        warn(f"차량번호 미확보 {len(missing_plate)}건 — 상세 응답 키 확인 필요 "
             f"(예: {missing_plate[:3]})")

    rows = dedupe_by_plate(rows)
    return rows


def dedupe_by_plate(rows: list[dict]) -> list[dict]:
    """같은 차가 매물번호만 다르게 두 번 올라온 것을 하나로 합친다.

    중복 제거는 vehicle_id 로 하는데, 딜러가 같은 차를 다시 등록하면
    매물번호가 새로 붙는다. 실제로 33건 중 9쌍이 차량번호·가격·주행거리가
    전부 같은 같은 차였다 (33건 = 실제 24대).

    이걸 두면 두 군데가 틀어진다:
      - 시세 회귀선이 중복된 차 쪽으로 끌려간다 (그 차만 표본에 2표)
      - 상위 10대에 같은 차가 두 번 올라와 헤이딜러 조회를 한 칸 낭비한다

    차량번호를 못 받은 매물은 합치지 않는다 (같은 차인지 알 수 없으므로).
    남길 쪽은 정보가 더 많은 것 — 성능기록부 > 보험이력 > 최근 등록 순.
    """
    from common import to_int

    def _rank(r: dict) -> tuple:
        return (
            1 if r.get("page_available") else 0,
            1 if str(r.get("record_available", "")).lower() in ("true", "1", "y") else 0,
            1 if r.get("detail_fetched") else 0,
            to_int(r.get("vehicle_id")) or 0,      # 매물번호가 큰 쪽 = 최근 등록
        )

    best: dict[str, dict] = {}
    order: list = []
    dropped: list[str] = []
    for r in rows:
        plate = str(r.get("plate_no") or "").strip()
        if not plate:
            order.append(r)                        # 차량번호 미확보 — 그대로 둔다
            continue
        cur = best.get(plate)
        if cur is None:
            best[plate] = r
            order.append(plate)
        elif _rank(r) > _rank(cur):
            dropped.append(f"{plate}:{cur.get('vehicle_id')}")
            best[plate] = r
        else:
            dropped.append(f"{plate}:{r.get('vehicle_id')}")

    if dropped:
        log(f"  같은 차량번호 중복 {len(dropped)}건 제거 "
            f"(매물번호만 다른 재등록) — 남은 {len(order)}대")
        log(f"    제거된 매물번호: {', '.join(dropped[:12])}"
            + (" ..." if len(dropped) > 12 else ""))

    out = []
    for x in order:
        out.append(best[x] if isinstance(x, str) else x)
    return out


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
    # 지난 실행에서 받아 둔 상세. 증분 수집이 여기서 재사용한다.
    prev_details = {} if args.full else (read_json(DETAILS_JSON) or {})
    if args.full:
        log("--full: 저장분을 쓰지 않고 전부 다시 받습니다")
    elif prev_details:
        log(f"증분 수집: 저장된 상세 {len(prev_details)}건을 재사용합니다 "
            f"(전부 다시 받으려면 --full)")
    blocked: api.EncarBlocked | None = None
    unreachable: api.EncarUnreachable | None = None

    for target in targets:
        try:
            all_rows.extend(collect_target(
                client, target, limit, not args.no_detail, details,
                full=args.full, prev_details=prev_details))
        except api.EncarBlocked as e:
            blocked = e
            break
        except api.EncarUnreachable as e:
            unreachable = e
            break

    for r in all_rows:
        r.setdefault("collected_at", datetime.now().isoformat(timespec="seconds"))
    # 이력에서 온 값(딜러 보유 기간, 처음 본 날, 가격 변동)을 먼저 채운다.
    history.annotate(all_rows)
    write_csv(LISTINGS_CSV, all_rows, LISTING_FIELDS)
    write_json(DETAILS_JSON, details)
    log(f"저장: {LISTINGS_CSV} ({len(all_rows)}건), {DETAILS_JSON}")
    # 오늘 수집분을 날짜 폴더에 보관한다. 매주 돌리며 지켜보는 용도라
    # 지난 실행과 비교할 수 있어야 한다.
    history.snapshot(all_rows, LISTING_FIELDS)

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


def _decode_js_escapes(text: str) -> str:
    r"""자바스크립트 문자열의 \uXXXX 이스케이프를 실제 글자로 바꾼다.

    JS 안의 한글은 보통 "\uc9c4\ub2e8" 처럼 인코딩돼 있어서, 브라우저에서
    '교환' 으로 검색하면 안 나온다. 원본 HTML 을 뒤질 때는 반드시 풀어야 한다.
    """
    def _sub(m):
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return m.group(0)
    return re.sub(r"\\u([0-9a-fA-F]{4})", _sub, text)


def cmd_hunt_page(args) -> int:
    r"""원본 HTML 안에 수리 부위 데이터가 어떤 형태로 숨어 있는지 찾는다.

    JS 가 별도 API 호출 없이 목록을 그린다면 데이터는 이미 페이지 안에 있다.
    script 변수, JSON 문자열, hidden input, data-* 속성을 훑고,
    \uXXXX 로 인코딩된 한글도 풀어서 본다.
    """
    path = args.hunt if isinstance(args.hunt, str) and os.path.isfile(args.hunt) else None
    if path is None:
        cands = glob.glob(os.path.join(
            os.path.dirname(DETAILS_JSON), "raw_inspection_page*.html"))
        if not cands:
            die("성능기록부 HTML 이 없습니다. "
                "`python collect.py --inspect-page --carid <번호>` 를 먼저 실행하세요.")
        # 파일 이름이 아니라 '가장 최근에 받은' 것을 본다.
        # 이름순으로 고르면 방금 --carid 로 받은 페이지가 아니라
        # 차량번호가 가장 큰 페이지를 열게 된다.
        path = max(cands, key=os.path.getmtime)
    raw = open(path, encoding="utf-8", errors="replace").read()
    decoded = _decode_js_escapes(raw)

    print("=" * 74)
    print(f" 숨은 데이터 탐색 — {path} ({len(raw):,} bytes)")
    print("=" * 74)

    KEYS = ["tit_part", "uiListLank", "uiLankNone", "insrresult", "lank",
            "교환", "판금", "용접", "부식", "휀더", "펜더", "도어", "필러", "멤버"]
    print("\n[키워드 출현 횟수]  (원본 / \\u 디코딩 후)")
    for k in KEYS:
        a, b = raw.count(k), decoded.count(k)
        mark = "   <== 디코딩해야 보임" if b > a else ""
        print(f"    {k:12} {a:4} / {b:4}{mark}")

    hits = [k for k in ("교환", "판금", "휀더", "필러", "tit_part")
            if decoded.count(k) > 0]
    if not hits:
        # 성능기록부를 사진으로만 올린 매물이면 페이지에 표도 script 도 없다.
        # 우회 URL 을 아무리 찾아도 나올 것이 없으므로 그렇게 안내한다.
        if "사진으로 등록한" in decoded or "등록 사진" in decoded:
            warn("이 매물은 성능기록부를 사진(이미지)으로만 올렸습니다.")
            print("    -> 페이지에 읽을 데이터가 원래 없습니다. 파서 문제가 아닙니다.")
            print("       이 매물은 성능기록부를 눈으로 직접 확인해야 합니다.")
            return 0
        warn("부위/상태 관련 문자열이 페이지 어디에도 없습니다.")
        print("    -> 데이터가 원본 HTML 에 없다는 뜻입니다. 아래 우회로를 시험하세요:")
        print("       python collect.py --try-urls --carid <번호>")
        return 1

    print(f"\n    발견: {hits}  -> 데이터가 페이지 안에 있습니다.")

    # --- script 안 변수: point / opt / lank-value 레코드 ---
    print("\n[script 안 변수 해석]")
    try:
        sd = api.extract_page_script_data(raw)
    except Exception as e:
        sd = {}
        warn(f"script 해석 실패: {e}")

    if sd:
        pt = sd.get("point") or []
        print(f"    상태 코드표(point): {pt if pt else '못 찾음'}")
        if sd.get("point_from"):
            print(f"        정의 위치: {sd['point_from']}")
        cats = sd.get("catalogs") or {}
        if cats:
            print(f"    부위 목록(opt): {len(cats)}개")
            for nm, entries in list(cats.items())[:8]:
                names = [e.get("name") for e in entries if e.get("name")]
                legacy = [e.get("legacy") for e in entries if e.get("legacy")]
                print(f"        {nm}: {len(entries)}개  {names[:4]}")
                if legacy:
                    print(f"            구버전 표기(name201806): {legacy[:3]}")
        else:
            print("    부위 목록(opt): 못 찾음")

        tbl = sd.get("part_table") or {}
        print(f"    부위 표(dataGroup): {len(tbl)}개 키 "
              f"({sd.get('part_table_from') or '못 찾음'})")
        dmg = sd.get("damage")
        if dmg is None:
            print("    부위별 손상표(init data): 못 찾음")
        else:
            hurt = {k: v for k, v in dmg.items() if v}
            print(f"    부위별 손상표(init data): {len(dmg)}개 부위 중 "
                  f"{len(hurt)}개에 손상  ({sd.get('damage_from') or '-'})")
            for k, v in hurt.items():
                e = tbl.get(k) or {}
                print(f"        {k:24} {v}  -> "
                      f"{e.get('name') or '(부위 표에 없음)'} / 랭크 {e.get('lank') or '?'}")

        recs = sd.get("records") or []
        print(f"    수리 레코드(lank/value): {len(recs)}건")
        if sd.get("record_from"):
            print(f"        정의 위치: {sd['record_from']}")
        for r in recs[:12]:
            print(f"        {r}")
        if sd.get("record_raw"):
            print("\n    --- 그 변수의 원문 ---")
            print("    " + sd["record_raw"][:1200])

    # 사장님이 지목한 변수들이 어디서 정의되는지 그대로 보여 준다.
    WANT = ("current", "initlankflag", "stats", "point", "opt", "lank", "value")
    defs = (sd.get("defs") or []) if sd else []
    print("\n[변수 정의 위치]  (script 번호 / 문자 위치)")
    shown_def = 0
    for d in defs:
        base = d["name"].split(".")[-1].lower()
        if not any(w in base for w in WANT):
            continue
        print(f"    {d['name']:22} script #{d['script'] + 1} "
              f"@{d['offset']:>7}  {d['kind']}({d['size']})")
        print(f"        {d['raw'][:400]}")
        shown_def += 1
        if shown_def >= 12:
            break
    if not shown_def:
        print("    (대입 형태로는 못 찾음 — 아래 원문 조각을 보세요)")

    # 변수 이름이 예상과 달라도 찾을 수 있게, 이름 없이 위치만이라도 보여 준다.
    print("\n[이름으로 찾은 원문 조각]")
    NAMES = ["current", "initLankFlag", "stats", "this.point", "uiListLank"]
    for nm in NAMES:
        i = decoded.find(nm)
        if i < 0:
            print(f"    {nm:16} 없음")
            continue
        seg = re.sub(r"\s+", " ", decoded[max(0, i - 160): i + 340])
        print(f"    {nm:16} @{i}")
        print(f"        {seg}")

    # --- hidden input / data-* 속성 ---
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw, "lxml")
    print("\n[hidden input / data-* 속성]")
    found_attr = 0
    for el in soup.find_all(True):
        if el.name == "input" and (el.get("type") or "").lower() == "hidden":
            v = _decode_js_escapes(el.get("value") or "")
            if any(k in v for k in ("교환", "판금", "휀더", "필러", "lank")):
                print(f"    <input name={el.get('name')!r}> {v[:300]}")
                found_attr += 1
        for a, v in (el.attrs or {}).items():
            if not a.startswith("data-") or not isinstance(v, str):
                continue
            dv = _decode_js_escapes(v)
            if any(k in dv for k in ("교환", "판금", "휀더", "필러", "lank")):
                print(f"    <{el.name} {a}=...> {dv[:300]}")
                found_attr += 1
        if found_attr >= 6:
            break
    if not found_attr:
        print("    (hidden input / data-* 에는 없음)")

    # --- 그 외 위치: 본문 어디쯤인지 ---
    print("\n[본문에서의 위치 (앞뒤 300자)]")
    for k in ("tit_part", "교환", "휀더"):
        i = decoded.find(k)
        if i < 0:
            continue
        print(f"\n    --- '{k}' 주변 ---")
        print("    " + re.sub(r"\s+", " ", decoded[max(0, i-300): i+300]))
        break

    # 지금 파서로 실제로 무엇이 읽히는지 바로 보여 준다.
    print("\n[이 파일에서 실제로 읽히는 수리 내역]")
    try:
        parsed = api.parse_inspection_page(raw)
    except Exception as e:
        parsed = None
        warn(f"파싱 실패: {e}")
    if parsed:
        print(f"    경로: {parsed.get('parse_note') or '못 읽음'}")
        if parsed.get("repairs"):
            for r in parsed["repairs"]:
                st = r["status_text"] or "판정 불가"
                src_note = r.get("resolved_by") or r.get("source") or ""
                print(f"      [{r['rank']}] {r['raw']:24} {st:10} "
                      f"({r['status']})  {src_note}")
        else:
            print("    (수리 부위 0건)")
        for rk, txt in (parsed.get("rank_sections") or {}).items():
            print(f"      {rk}: {txt}")
        if parsed.get("unmatched_parts"):
            warn(f"미분류: {parsed['unmatched_parts']}")
        for k in ("lank_flag", "rank_mismatch"):
            if parsed.get("field_notes", {}).get(k):
                warn(parsed["field_notes"][k])

    print("\n" + "=" * 74)
    if parsed and parsed.get("repairs"):
        print(" script 안 데이터를 읽었습니다. 부위/상태가 브라우저 화면과")
        print(" 같은지 한 번만 확인해 주세요. 다르면 그 출력을 보내주세요.")
    else:
        print(" 위 출력을 그대로 보내주시면 그 형태에 맞춰 파서를 붙이겠습니다.")
    print("=" * 74)
    return 0


def cmd_try_urls(args) -> int:
    """정적 HTML 로 성능기록부를 주는 다른 경로가 있는지 시험한다.

    JS 렌더링을 피할 우회로: 인쇄용 / 모바일 / ajax 조각 페이지 등.
    """
    vid = str(args.carid or "").strip()
    if not vid:
        die("--carid <매물번호> 를 함께 주세요.")
    client = api.EncarClient(config.COLLECT)

    base = "https://www.encar.com/md/sl/mdsl_regcar.do"
    cands = [
        ("현재 사용 중", config.INSPECTION_PAGE_URL.format(vid=vid)),
        ("인쇄용 1", f"{base}?method=inspectionPrint&carid={vid}"),
        ("인쇄용 2", f"{base}?method=inspectionViewNew&carid={vid}&print=Y"),
        ("구버전 뷰", f"{base}?method=inspectionView&carid={vid}"),
        ("ajax 조각", f"{base}?method=inspectionViewNewAjax&carid={vid}"),
        ("부위 조각", f"{base}?method=inspectionPartList&carid={vid}"),
        ("모바일", f"http://m.encar.com/md/sl/mdsl_regcar.do"
                   f"?method=inspectionViewNew&carid={vid}"),
        ("성능점검 상세", f"https://www.encar.com/dc/dc_cardetailview.do"
                          f"?method=inspectionView&carid={vid}"),
    ]

    print("=" * 74)
    print(f" 정적 HTML 우회로 탐색 — carid={vid}")
    print("=" * 74)
    best = None
    for label, url in cands:
        try:
            code, _payload, snippet, _ = client.raw_get(url, None, stage=f"try:{label}")
        except api.EncarUnreachable as e:
            print(f"  {label:14} 연결실패")
            continue
        except api.EncarBlocked:
            raise
        except RuntimeError as e:
            print(f"  {label:14} 실패 ({str(e)[:44]})")
            continue

        body = ""
        if code == 200:
            try:
                r = client.s.get(url, timeout=client.timeout,
                                 headers={"Accept": "text/html"})
                body = r.text or ""
            except Exception:
                body = snippet or ""
        dec = _decode_js_escapes(body)
        marks = [k for k in ("tit_part", "교환", "판금", "휀더", "필러")
                 if k in dec]
        note = f"  <== 부위 데이터 있음! {marks}" if marks else ""
        print(f"  {label:14} {code if code else '연결실패'}  "
              f"{len(body):>7,} bytes{note}")
        if marks and best is None:
            best = (label, url, body)

    if best:
        label, url, body = best
        out = os.path.join(os.path.dirname(DETAILS_JSON),
                           f"raw_inspection_alt_{vid}.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"\n  -> '{label}' 에서 부위 데이터를 찾았습니다.")
        print(f"     {url}")
        print(f"     저장: {out}")
        print(f"\n     확인:  python collect.py --inspect-page {out}")
    else:
        warn("정적 HTML 우회로를 못 찾았습니다.")
        print("\n  먼저 이것부터 — 원본 페이지의 script 안 데이터를 읽습니다:")
        print(f"    python collect.py --inspect-page --carid {vid}")
        print("    (JS 가 그리기 전이라도 데이터는 script 변수에 들어 있습니다)")
        print("\n  그래도 0건이면 그때 — 브라우저에서 렌더된 DOM 을 직접 저장:")
        print("    1. 성능기록부 페이지를 연다")
        print("    2. F12 -> Elements 탭 -> 최상단 <html> 우클릭")
        print("    3. Copy -> Copy outerHTML")
        print("    4. 메모장에 붙여넣고 rendered.html 로 저장 (인코딩 UTF-8)")
        print("    5. python collect.py --inspect-page rendered.html")
        print("\n  이러면 JS 실행 결과가 그대로 담겨 파서가 읽을 수 있습니다.")
    return 0


def cmd_inspect_page(args) -> int:
    """저장된 성능기록부 HTML 을 분석하고, 판정 불가 항목의 마크업을 보여준다.

    파서가 값을 못 읽었을 때 '실제 HTML 이 어떻게 생겼는지' 를 그대로
    보여주기 위한 것이다. 그 출력을 그대로 보내주면 파서를 맞출 수 있다.
    """
    record = None
    if args.carid:
        # 특정 매물 페이지를 새로 받아 분석한다.
        # 사고 있는 매물로 시험해야 랭크 목록이 실제로 채워지는지 알 수 있다.
        client = api.EncarClient(config.COLLECT)
        vid = str(args.carid).strip()
        log(f"carid={vid} 성능기록부 페이지 요청")
        html_text = client.inspection_page(vid)
        if not html_text:
            die(f"carid={vid} 페이지를 못 받았습니다.")
        path = os.path.join(os.path.dirname(DETAILS_JSON),
                            f"raw_inspection_page_{vid}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_text)
        log(f"저장: {path}")
        # 보험이력도 같이 받아 '사고가 있는데 랭크가 비었는지' 대조한다
        try:
            record = client.record(vid)
        except (api.EncarBlocked, api.EncarUnreachable):
            raise
        except Exception:
            record = None
    else:
        path = args.inspect_page or os.path.join(
            os.path.dirname(DETAILS_JSON), "raw_inspection_page.html")
        if not os.path.isfile(path):
            die(f"성능기록부 HTML 을 못 찾았습니다: {path}\n"
                "`python collect.py --probe` 를 먼저 실행하세요.")
        html_text = open(path, encoding="utf-8", errors="replace").read()

    print("=" * 74)
    print(f" 성능기록부 HTML 분석 — {path} ({len(html_text):,} bytes)")
    print("=" * 74)

    parsed = api.parse_inspection_page(html_text)
    ss = parsed.get("script_summary") or {}
    if parsed.get("parse_note"):
        warn(parsed["parse_note"])

    if parsed.get("page_is_image"):
        print("\n[이 매물은 성능기록부를 사진으로만 올렸습니다]")
        print("    페이지에 표도 script 데이터도 없습니다. 파서 문제가 아니라")
        print("    읽을 것이 원래 없는 페이지입니다. 아래 항목이 전부")
        print("    '판정 불가' 로 나오는 것이 정상이며, 무사고로 읽어서는 안 됩니다.")
        print("    -> 이 매물은 성능기록부를 눈으로 직접 확인해야 합니다.")

    print("\n[랭크별 내용]")
    for rank, body in (parsed.get("rank_sections") or {}).items():
        print(f"  {rank:6} {body}")
    if not parsed.get("rank_sections") and not parsed.get("page_is_image"):
        warn("랭크 행을 못 찾았습니다.")

    # --- 랭크가 비었을 때: 정말 무사고인가, JS 렌더링인가 ---
    if not parsed["repairs"]:
        print("\n[랭크가 비어 있음 — 무사고인지 JS 렌더링인지 판별]")
        rec = api.normalize_record(record) if record else None
        if rec:
            my = rec.get("my_accident_count")
            ot = rec.get("other_accident_count")
            print(f"    보험이력: 내차 {my} 건 / 타차 {ot} 건")
            if ss.get("damage_from"):
                # 페이지가 부위별로 '수리 없음' 이라고 명시한 경우다.
                # 보험 처리가 있어도 성능기록부상 교환·판금 부위가 없을 수
                # 있다(유리·범퍼·상대차 수리 등). 파싱 실패가 아니다.
                print("    -> 성능기록부의 부위별 손상표를 읽었고 수리 부위가 "
                      "0건입니다.")
                print(f"       근거: {ss['damage_from']}")
                if (my or 0) or (ot or 0):
                    print("       보험 처리 이력은 있지만 성능기록부에 교환·판금")
                    print("       부위로는 잡히지 않았습니다 (유리·범퍼·상대차 수리 등).")
            elif (my or 0) or (ot or 0):
                warn("사고 기록이 있는데 랭크 목록이 비었습니다 "
                     "-> 자바스크립트로 나중에 채워지는 구조로 보입니다.")
            else:
                print("    -> 보험이력도 무사고입니다. 랭크가 비는 게 정상입니다.")
        elif args.carid:
            print("    보험이력을 못 받아 대조하지 못했습니다.")
        else:
            print("    (--carid 를 주면 보험이력과 대조해 판별합니다)")

        if parsed.get("js_render_suspect"):
            warn(f"JS 렌더링 의심 클래스 발견: {parsed['js_hints']}")
        if parsed.get("js_scripts"):
            print("\n    랭크 목록을 채우는 스크립트 단서:")
            for sc in parsed["js_scripts"]:
                print(f"      {sc}")
            print("\n    이 스크립트가 부르는 데이터 API 를 찾으면 그걸 직접 쓰면 됩니다.")
            print("    브라우저 F12 -> Network -> Fetch/XHR 에서 성능기록부 페이지를")
            print("    열 때 뜨는 요청 중 부위명이 든 응답을 찾아 URL 을 알려주세요.")

    print(f"\n[수리 부위 {len(parsed['repairs'])}건]")
    for r in parsed["repairs"]:
        mark = "" if r.get("status_known") else "   <-- 상태 부호를 못 읽음"
        print(f"  [{r['rank']:5}] {r['raw'][:24]:26} [{r['status']}]{mark}")
    if parsed["unmatched_parts"]:
        warn(f"부위를 못 알아본 랭크: {parsed['unmatched_parts']}")
    for _k, _msg in (("lank_flag", "랭크 표시와 어긋남"),
                     ("rank_mismatch", "부위별 법정 랭크와 어긋남")):
        if parsed.get("field_notes", {}).get(_k):
            warn(f"{_msg}: {parsed['field_notes'][_k]}")

    # script 안 데이터에서 읽었다면 어디서 왔는지 밝힌다. 자리번호를
    # 부위명으로 바꾸는 단계라 한 칸만 밀려도 엉뚱한 부위가 되기 때문이다.
    if ss.get("records") or ss.get("damage_from"):
        print("\n[script 안 데이터 출처]")
        print(f"  상태 코드표 : {ss.get('point')} ({ss.get('point_from') or '-'})")
        if ss.get("damage_from"):
            print(f"  부위 표     : {ss.get('part_table')}개 키 "
                  f"({ss.get('part_table_from') or '-'})")
            print(f"  손상 표     : {ss.get('damage_total')}개 부위 중 "
                  f"{ss.get('damage_parts')}개에 손상  ({ss['damage_from']})")
        else:
            print(f"  부위 목록   : {ss.get('catalogs')}")
            print(f"  수리 레코드 : {ss.get('records')}건 ({ss.get('record_from') or '-'})")
        for r in parsed["repairs"]:
            if r.get("resolved_by"):
                print(f"    {r['raw']:24} <- {r['resolved_by']}")

    print("\n[라벨 항목]")
    unknown_keys = []
    for key, _labels, kind in config.INSPECTION_PAGE_FIELDS:
        v = parsed["fields"].get(key)
        why = parsed["field_notes"].get(key, "")
        if v in (None, ""):
            unknown_keys.append(key)
            print(f"  {key:22}= (판정 불가)   {why}")
        else:
            print(f"  {key:22}= {v!r:14} {why}")

    print("\n[고전원전기장치]")
    for k, v in (parsed.get("ev_hv") or {}).items():
        print(f"  {k:22}= {v['state'] or '(판정 불가)':10} {v['why']}")
    if parsed.get("ev_hv_unknown"):
        warn(f"판정 불가: {parsed['ev_hv_unknown']}")

    print("\n[자동차 세부상태]")
    print(f"  불량      : {parsed.get('detail_bad') or '(없음)'}")
    print(f"  판정 불가  : {parsed.get('detail_unknown') or '(없음)'}")

    # --- 판정 불가 항목의 실제 마크업을 전부 보여준다 ---
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "lxml")

    def _dump(label: str, keywords: list[str], limit: int = 1) -> int:
        """키워드가 든 가장 작은 블록의 원문을 보여준다.

        <tr> 만 뒤지면 div/ul 구조를 못 찾는다. 태그를 가리지 않고
        '키워드를 담은 가장 작은 요소' 의 부모까지 보여준다.
        """
        shown = 0
        seen_html: set[str] = set()
        for kw in keywords:
            flat = kw.replace(" ", "")
            best = None
            for el in soup.find_all(True):
                if el.name in ("html", "body", "head", "script", "style"):
                    continue
                t = el.get_text(" ", strip=True).replace(" ", "")
                if flat not in t:
                    continue
                if best is None or len(t) < len(
                        best.get_text(" ", strip=True).replace(" ", "")):
                    best = el
            if best is None:
                continue
            node = best.parent if best.parent is not None and \
                best.parent.name not in ("body", "html") else best
            frag = str(node)[:1000]
            if frag in seen_html:
                continue
            seen_html.add(frag)
            print(f"\n--- {label} / '{kw}' ---")
            print(frag)
            shown += 1
            if shown >= limit:
                break
        if not shown:
            print(f"\n--- {label} --- 키워드 {keywords} 를 페이지에서 못 찾았습니다")
        return shown

    problems: list[tuple[str, list[str]]] = []
    for key, labels, _kind in config.INSPECTION_PAGE_FIELDS:
        if parsed["fields"].get(key) in (None, ""):
            problems.append((f"항목:{key}", labels))
    for u in (parsed.get("ev_hv_unknown") or []):
        problems.append((f"고전원:{u[:20]}", [u.split("(")[0].strip()]))
    for u in (parsed.get("detail_unknown") or []):
        nm = u.split("(")[0].strip()
        problems.append((f"세부상태:{nm}", [nm]))
    if not parsed["repairs"]:
        problems.append(("수리부위 랭크", ["1랭크", "2랭크", "A랭크", "B랭크"]))
        problems.append(("수리부위 예시", ["휀더", "펜더", "도어", "필러", "멤버"]))

    if args.find:
        problems = [("직접 지정", [k.strip() for k in args.find.split(",") if k.strip()])]

    if not problems:
        print("\n모든 항목을 읽었습니다. 추가 진단이 필요 없습니다.")
        return 0

    # 페이지에 라벨 자체가 없는 것과, 라벨은 있는데 값을 못 읽은 것을 나눈다.
    # 앞쪽은 파싱 문제가 아니므로 원문을 뒤질 필요가 없다.
    page_text = soup.get_text(" ", strip=True).replace(" ", "")
    absent, unparsed = [], []
    for label, kws in problems:
        if any(k.replace(" ", "") in page_text for k in kws):
            unparsed.append((label, kws))
        else:
            absent.append(label)

    if absent:
        print(f"\n[페이지에 항목 자체가 없음 — 파싱 문제 아님] {len(absent)}건")
        print("  " + ", ".join(a.replace("항목:", "") for a in absent))

    if not unparsed:
        print("\n라벨이 있는 항목은 모두 읽었습니다.")
        return 0

    print("\n" + "=" * 74)
    print(f" 라벨은 있는데 값을 못 읽은 항목 {len(unparsed)}건 — 실제 마크업")
    print(" (이 출력을 그대로 보내주세요)")
    print("=" * 74)
    for label, kws in unparsed[:12]:
        _dump(label, kws)
    if len(unparsed) > 12:
        print(f"\n... 외 {len(unparsed)-12}건 생략")
    return 0


def cmd_inspect_file(args) -> int:
    """저장된 성능점검 JSON 을 네트워크 없이 분석한다.

    probe 를 다시 돌리지 않고 data/raw_inspection.json 만으로
    부위/상태 파싱이 맞는지 확인할 때 쓴다.
    """
    path = args.inspect_file
    if not os.path.isfile(path):
        path = os.path.join(os.path.dirname(DETAILS_JSON), "raw_inspection.json")
    payload = read_json(path)
    if not payload:
        die(f"성능점검 JSON 을 못 읽었습니다: {path}")

    print("=" * 74)
    print(f" 성능점검 파일 분석 — {path}")
    print("=" * 74)
    for sec in ("inners", "outers", "etcs"):
        items = payload.get(sec) if isinstance(payload, dict) else None
        if items is None:
            continue
        print(f"\n[{sec}] {len(items)}개")
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            print(f"  [{i}] {api._title_of(item) or '?'}  "
                  f"code={api._code_of(item)}  status={api._status_of(item) or '-'}  "
                  f"children={len(item.get('children') or [])}")
            for j, c in enumerate(item.get("children") or []):
                if isinstance(c, dict):
                    rk = api.classify_part(api._title_of(c) or "")
                    print(f"      [{i}.{j}] {api._title_of(c) or '?':16} "
                          f"status={api._status_of(c) or '-':4} -> {rk or '미분류'}")

    tree = api.score_repairs(api.find_repair_entries(payload))
    if tree["entries"]:
        print(f"\n[트리에서 나온 부위 (참고)]")
        for g in tree["entries"]:
            print(f"  {g['note']}")
    else:
        print("\n[트리] 판정값이 실려 있지 않습니다 (status 전부 '-'). "
              "부위 등급은 코멘트에서 뽑습니다.")
    if tree.get("diagnostics"):
        print(f"[자기진단 항목] {tree['diagnostics']}")

    ni = api.normalize_inspection(payload)

    print("\n[점검자 코멘트에서 뽑은 수리 부위]")
    print(f"  원문: {(ni.get('insp_comments') or '(없음)')[:300]}")
    cres = api.score_comment_parts(ni.get("insp_comments") or "")
    for g in cres["entries"]:
        print(f"  {g['note']}   [-{g['penalty']}]")
    if not cres["entries"]:
        print("  (부위 언급 없음)")
    print(f"  감점 합계: {cres['penalty']}")
    if cres.get("accident_mentions"):
        print(f"  코멘트 속 사고 언급: {cres['accident_mentions']}")
    if cres.get("unmatched"):
        warn(f"못 알아본 단어: {cres['unmatched']}")
        print("  부위명이면 알려주세요. config.COMMENT_PART_ALIASES 에 넣겠습니다.")

    print("\n[master.detail]")
    for k in ("insp_mileage", "insp_waterlog", "insp_recall", "insp_recall_types",
              "insp_tuning", "insp_usage_change", "insp_serious", "insp_vin",
              "insp_accident_flag", "insp_simple_repair", "insp_needs_repair",
              "leak_note", "corrosion_note", "tire_note", "insp_comments"):
        v = ni.get(k)
        print(f"  {k:20}= {v if v not in (None, '') else '(응답에 없음)'}")
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
    rows = []
    for vid, bucket in details.items():
        if not isinstance(bucket, dict):
            continue
        old = prev.get(str(vid), {})
        target = targets.get(old.get("model_key")) or config.TARGETS[0]

        search = bucket.get("search")
        listing = api.normalize_listing(search, target) if search else dict(old)
        listing["model_key"] = target["key"]
        listing["model_label"] = target["label"]

        if bucket.get("detail") is not None:
            listing.update(api.normalize_detail(
                str(vid), bucket.get("detail"), bucket.get("record"),
                bucket.get("inspection"), bucket.get("diagnosis"),
                target, inspection_html=bucket.get("inspection_html")))
            listing["detail_fetched"] = True
        else:
            listing["detail_fetched"] = old.get("detail_fetched", False)

        listing["collected_at"] = old.get("collected_at", "")
        rows.append(listing)

    write_csv(LISTINGS_CSV, rows, LISTING_FIELDS)
    log(f"재해석 완료: {LISTINGS_CSV} ({len(rows)}건)")

    detailed = [r for r in rows if r.get("detail_fetched")]
    got_page = sum(1 for r in detailed if r.get("page_available"))
    print(f"\n  상세 확보 {len(detailed)}건 / 성능기록부 페이지 {got_page}건")
    print("    에어서스·후륜조향은 헤이딜러 숨은이력에서 확정합니다.")
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
                raw.get("inspection"), raw.get("diagnosis"), target,
                inspection_html=raw.get("inspection_html")))
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
    p.add_argument("--inspect-page", dest="inspect_page", nargs="?",
                   const="data/raw_inspection_page.html",
                   help="저장된 성능기록부 HTML 을 분석 + 마크업 진단")
    p.add_argument("--find", help="--inspect-page 전용: 이 키워드들의 마크업만 출력 "
                                  "(쉼표 구분, 예: 랭크,휀더,필러)")
    p.add_argument("--carid", help="--inspect-page / --try-urls 전용: 매물 번호")
    p.add_argument("--hunt", nargs="?", const=True,
                   help="원본 HTML 안에 숨은 수리 부위 데이터를 찾는다")
    p.add_argument("--try-urls", dest="try_urls", action="store_true",
                   help="정적 HTML 로 성능기록부를 주는 다른 경로를 시험 (--carid 필요)")
    p.add_argument("--inspect-file", dest="inspect_file", nargs="?",
                   const="data/raw_inspection.json",
                   help="저장된 성능점검 JSON 을 네트워크 없이 분석")
    p.add_argument("--reparse", action="store_true",
                   help="이미 받은 details.json 을 네트워크 없이 다시 해석 "
                        "(옵션 변환표를 새로 채운 뒤 사용)")
    p.add_argument("--model", metavar="KEY", help="config.TARGETS 의 key 하나만 처리")
    p.add_argument("--limit", type=int, help="모델당 최대 건수")
    p.add_argument("--no-detail", action="store_true", help="상세 API 생략")
    p.add_argument("--full", action="store_true",
                   help="증분 수집을 쓰지 않고 전부 다시 받는다 "
                        "(기본은 증분 — 가격이 그대로인 매물은 저장분 재사용, "
                        "보험이력·성능기록부는 30일마다 갱신)")
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
        if args.hunt:
            return cmd_hunt_page(args)
        if args.try_urls:
            return cmd_try_urls(args)
        if args.inspect_page:
            return cmd_inspect_page(args)
        if args.inspect_file:
            return cmd_inspect_file(args)
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
