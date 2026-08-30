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


def cmd_probe(args) -> int:
    """검색 API 를 1회만 호출해 원본 응답과 구조 요약을 남긴다."""
    ensure_dirs()
    client = api.EncarClient(config.COLLECT)
    target = next((t for t in config.TARGETS if t["key"] == args.model), config.TARGETS[0])
    q = args.q or api.build_query(target)

    log(f"probe: {target['label']}")
    log(f"  q = {q}")
    payload = client.search(q, offset=0, limit=args.limit or 3)

    raw_path = os.path.join(os.path.dirname(DETAILS_JSON), "raw_probe.json")
    write_json(raw_path, payload)
    log(f"원본 응답 저장 → {raw_path}")

    print("\n=== 응답 최상위 구조 ===")
    for line in _describe(payload, max_depth=1):
        print("  " + line)

    results = api.extract_search_results(payload)
    count = api.extract_total_count(payload)
    print(f"\n총 매물 수(Count): {count}")
    print(f"이번 응답 결과 건수: {len(results)}")

    if results:
        print("\n=== 결과 1건 전체 필드 ===")
        for line in _describe(results[0], max_depth=2):
            print("  " + line)
        print("\n=== normalize_listing() 매핑 결과 ===")
        norm = api.normalize_listing(results[0], target)
        for k, v in norm.items():
            flag = "" if v not in (None, "") else "   <-- 매핑 실패, 위 필드 목록에서 실제 키 확인 필요"
            print(f"  {k:>14} = {v!r}{flag}")

        vid = norm.get("vehicle_id")
        if vid and not args.no_detail:
            print(f"\n=== 상세 API probe (vehicleId={vid}) ===")
            d = client.detail(vid)
            write_json(os.path.join(os.path.dirname(DETAILS_JSON), "raw_probe_detail.json"), d)
            if d is None:
                warn("상세 응답이 404 입니다. DETAIL_URL 형식을 확인하세요.")
            else:
                for line in _describe(d, max_depth=2)[:120]:
                    print("  " + line)
                plate = api.pick(d, "vehicleNo", "VehicleNo", "carNo", "CarNo")
                print(f"\n  차량번호 추출: {plate!r}")
                if not plate:
                    warn("차량번호를 못 찾았습니다. 위 목록에서 실제 키를 찾아 "
                         "encar_api.normalize_detail() 의 pick() 후보에 추가하세요.")
    else:
        warn("결과가 비었습니다. --discover 로 제조사/모델 표기를 확인하세요.")
    return 0


def cmd_discover(args) -> int:
    """facet(제조사/모델 집합)을 덤프해 쿼리 문자열을 교정할 수 있게 한다."""
    ensure_dirs()
    client = api.EncarClient(config.COLLECT)
    # 전기차 전체를 넓게 조회해서 facet 만 본다
    q = args.q or "(And.Hybrid.N._.(C.CarType.Y._.FuelType.전기.))"
    log(f"discover q = {q}")
    payload = client.search(q, offset=0, limit=1)
    write_json(os.path.join(os.path.dirname(DETAILS_JSON), "raw_discover.json"), payload)

    if not isinstance(payload, dict):
        warn("dict 응답이 아닙니다.")
        return 1

    printed = False
    for key, val in payload.items():
        if not isinstance(val, list) or not val or not isinstance(val[0], dict):
            continue
        if "Set" not in key and "set" not in key:
            continue
        printed = True
        print(f"\n=== {key} ===")
        for item in val[:80]:
            name = api.pick(item, "Value", "value", "Name", "name", "Code", "code")
            cnt = api.pick(item, "Count", "count")
            print(f"  {name}  ({cnt})")
    if not printed:
        print("facet 집합을 못 찾았습니다. 최상위 키 목록:")
        for line in _describe(payload, max_depth=1):
            print("  " + line)
    return 0


# ---------------------------------------------------------------------------
# 본 수집
# ---------------------------------------------------------------------------
def collect_target(client, target: dict, limit: int, with_detail: bool,
                   details_sink: dict) -> list[dict]:
    q = api.build_query(target)
    log(f"[{target['label']}] 검색 시작")
    log(f"  q = {q}")

    rows: list[dict] = []
    offset, page_size = 0, 20
    total = None

    while len(rows) < limit:
        payload = client.search(q, offset=offset, limit=min(page_size, limit - len(rows)))
        if total is None:
            total = api.extract_total_count(payload)
            log(f"  엔카 검색 결과 총 {total}건")
        results = api.extract_search_results(payload)
        if not results:
            break

        for raw in results:
            listing = api.normalize_listing(raw, target)
            if not listing.get("vehicle_id"):
                continue
            if not api.matches_target(listing, target):
                continue
            rows.append(listing)
            details_sink.setdefault(listing["vehicle_id"], {})["search"] = raw

        offset += len(results)
        if total is not None and offset >= total:
            break

    log(f"  조건 부합 {len(rows)}건 (연식 {target['year_from']}~{target['year_to']}, 트림 필터 적용)")

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

    for target in targets:
        try:
            all_rows.extend(collect_target(client, target, limit, not args.no_detail, details))
        except api.EncarBlocked as e:
            blocked = e
            break

    for r in all_rows:
        r.setdefault("collected_at", datetime.now().isoformat(timespec="seconds"))
    write_csv(LISTINGS_CSV, all_rows, LISTING_FIELDS)
    write_json(DETAILS_JSON, details)
    log(f"저장: {LISTINGS_CSV} ({len(all_rows)}건), {DETAILS_JSON}")

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


if __name__ == "__main__":
    sys.exit(main())
