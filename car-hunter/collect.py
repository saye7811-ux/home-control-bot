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
    die, ensure_dirs, log, read_json, warn, write_csv, write_json,
)

LISTING_FIELDS = [
    "model_key", "model_label", "vehicle_id", "plate_no",
    "price_manwon", "year", "month", "mileage_km", "region",
    "trim", "trim_detail", "sell_type",
    "accident_free", "accident_my_count", "accident_other_count",
    "accident_my_cost_won", "owner_change_count",
    "flood_or_total_loss", "rental_or_commercial", "one_owner",
    "encar_diagnosed", "has_airsus_keyword", "airsus_keyword_hits",
    "options_count", "options", "inspection_summary",
    "photo_url", "listing_url", "collected_at",
]


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
    n = args.limit or 5

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
        bad = v in (None, "")
        if bad:
            missing.append(k)
        print(f"    {k:>14} = {v!r}{'   <-- 매핑 실패' if bad else ''}")
    if missing:
        warn(f"매핑 실패 필드: {missing} — 위 [4] 목록에서 실제 키를 찾아 알려주세요.")

    # ---------------- 6. 연식 필터 ----------------
    print("\n[6] 연식 필터가 실제로 작동하는지")
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

    # ---------------- 7. 페이징 ----------------
    print("\n[7] 페이징(sr 파라미터)이 실제로 작동하는지")
    q_page = q_year if (code_y == 200 and payload_y) else q_noyear
    page_size = max(2, min(n, 5))
    pages = {}
    for off in (0, page_size):
        code_p, payload_p, _, _ = client.raw_get(
            ep["url"], {"count": "true", "q": q_page,
                        "sr": api.build_sr(off, page_size, config.SORT_KEY)},
            stage=f"search:page{off}")
        ids = _ids_of(api.extract_search_results(payload_p)) if payload_p else []
        pages[off] = ids
        print(f"    sr=|{config.SORT_KEY}|{off}|{page_size}  ->  {len(ids)}건  {ids}")

    a, b = pages.get(0, []), pages.get(page_size, [])
    overlap = set(a) & set(b)
    if not a:
        warn("첫 페이지가 비어 페이징을 판정할 수 없습니다.")
    elif not b:
        print("    -> 2페이지가 비었습니다. 전체 매물이 1페이지 분량이거나 "
              "오프셋이 범위를 넘었습니다.")
    elif overlap:
        warn(f"두 페이지가 {len(overlap)}건 겹칩니다 — 페이징이 안 먹는 것으로 보입니다.")
    else:
        print("    -> 페이징 작동 확인 (두 페이지 중복 0건)")

    # ---------------- 8. 상세 API ----------------
    vid = norm.get("vehicle_id")
    if not vid:
        warn("매물 ID 를 못 찾아 상세 API 를 시험하지 못했습니다.")
        return 0

    print(f"\n[8] 상세 API 시험 호출  (매물 ID = {vid})")
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
    if detail:
        write_json(os.path.join(os.path.dirname(DETAILS_JSON), "raw_probe_detail.json"), detail)
        print("\n    -- 상세 응답 구조 --")
        for line in _describe(detail, max_depth=2)[:60]:
            print("      " + line)
        plate = api.pick(detail, "vehicleNo", "VehicleNo", "carNo", "CarNo",
                         "vehicle.vehicleNo", "manage.vehicleNo")
        print(f"\n    차량번호 추출: {plate!r}")
        if not plate:
            warn("차량번호를 못 찾았습니다. 위 구조에서 번호판이 담긴 키를 찾아 알려주세요.")
        nd = api.normalize_detail(vid, detail, got.get("사고이력(record)"),
                                  got.get("성능점검(inspection)"),
                                  got.get("엔카진단(diagnosis)"), target)
        print(f"    옵션 {nd['options_count']}개 추출: {nd['options'][:120]}")
        print(f"    무사고={nd['accident_free']} 엔카진단={nd['encar_diagnosed']} "
              f"에어서스키워드={nd['airsus_keyword_hits'] or '없음'}")
    else:
        warn("상세 응답을 못 받아 차량번호/옵션을 확인하지 못했습니다.")

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

    for ep_name in config.USE_ENDPOINTS:
        if len(rows) >= limit:
            break
        ep = config.ENDPOINTS[ep_name]
        q = api.build_query(target, ep["ad_type"], include_year=True,
                            include_model=bool(target.get("model")))
        log(f"  [{ep_name}] q = {q}")

        offset, page_size, total = 0, 20, None
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
        return rows

    for i, listing in enumerate(rows, 1):
        vid = listing["vehicle_id"]
        log(f"  상세 {i}/{len(rows)} (id={vid})")
        detail = client.detail(vid)
        record = client.record(vid)
        inspection = client.inspection(vid)
        diagnosis = client.diagnosis(vid)

        bucket = details_sink.setdefault(vid, {})
        bucket.update({"detail": detail, "record": record,
                       "inspection": inspection, "diagnosis": diagnosis})

        listing.update(api.normalize_detail(vid, detail, record, inspection, diagnosis, target))
        listing["collected_at"] = datetime.now().isoformat(timespec="seconds")

    missing_plate = [r["vehicle_id"] for r in rows if not r.get("plate_no")]
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
