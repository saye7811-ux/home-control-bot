# -*- coding: utf-8 -*-
"""엔카 내부 API 클라이언트 + 방어적 파서.

설계 원칙
---------
1) **원본 보존**: 응답 JSON 은 손대지 않고 그대로 details.json 에 저장한다.
   내 필드 매핑이 틀려도 데이터는 남으므로 오프라인 재파싱이 가능하다.
2) **키 후보 다중 매핑**: 엔카는 응답 스키마를 조용히 바꾼다. 한 값에 대해
   여러 후보 경로를 순서대로 시도하고, 전부 실패하면 None + 경고를 남긴다.
3) **차단이면 즉시 중단**: 429/403/캡차/비-JSON 응답은 재시도하지 않는다.
   재시도는 네트워크 오류와 5xx 에만 적용한다.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

import requests

from common import log, warn, to_int

# 검색 URL 은 config.ENDPOINTS 에서 가져온다 (premium / general).
DETAIL_URL = "https://api.encar.com/v1/readside/vehicle/{vid}"
RECORD_URL = "https://api.encar.com/v1/readside/record/vehicle/{vid}/open"
INSPECT_URL = "https://api.encar.com/v1/readside/inspection/vehicle/{vid}"
DIAGNOSIS_URL = "https://api.encar.com/v1/readside/diagnosis/vehicle/{vid}"
LISTING_PAGE = "http://www.encar.com/dc/dc_cardetailview.do?carid={vid}"

DETAIL_INCLUDE = (
    "ADVERTISEMENT,CATEGORY,CONDITION,CONTACT,MANAGE,"
    "OPTIONS,PHOTOS,SPEC,PARTNERSHIP,CENTER,VIEW"
)

# 응답 본문에 이게 보이면 차단으로 간주
BLOCK_MARKERS = (
    "captcha", "캡차", "자동입력", "access denied", "forbidden",
    "too many requests", "비정상적인", "일시적으로 차단",
)


class EncarBlocked(RuntimeError):
    """캡차/429/403 등 엔카 쪽 차단 신호. 잡으면 즉시 파이프라인을 멈춘다."""

    def __init__(self, stage: str, detail: str, status: int | None = None):
        self.stage = stage
        self.detail = detail
        self.status = status
        super().__init__(f"[{stage}] {detail}" + (f" (HTTP {status})" if status else ""))


class EncarUnreachable(RuntimeError):
    """엔카 서버에 도달조차 못한 경우 (프록시 정책, 방화벽, DNS, 오프라인).

    엔카가 우리를 차단한 것과는 원인도 대처도 다르므로 반드시 구분한다.
    이 경우 재시도해봐야 의미가 없고, BLOCKED.txt 를 남겨서도 안 된다.
    """

    def __init__(self, stage: str, detail: str):
        self.stage = stage
        self.detail = detail
        super().__init__(f"[{stage}] {detail}")


# 프록시/방화벽이 연결 자체를 거부했을 때 나타나는 문구들
UNREACHABLE_MARKERS = (
    "tunnel connection failed: 403",
    "tunnel connection failed: 407",
    "unable to connect to proxy",
    "proxyerror",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
)


def _is_unreachable(exc: Exception) -> bool:
    """네트워크 일시 오류(재시도 가치 있음)와 정책 거부(재시도 무의미)를 가른다."""
    s = f"{exc.__class__.__name__} {exc}".lower()
    return any(m in s for m in UNREACHABLE_MARKERS)


# ---------------------------------------------------------------------------
# 방어적 값 추출
# ---------------------------------------------------------------------------
def pick(obj: Any, *paths: str, default=None):
    """점 표기 경로 후보들을 순서대로 시도해 첫 번째로 찾은 값을 반환.

    >>> pick({"category": {"formYear": 2022}}, "formYear", "category.formYear")
    2022
    """
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return default


def _walk_strings(obj: Any) -> Iterable[str]:
    """중첩 구조 전체에서 문자열만 훑는다 (옵션/이력 키워드 탐색용)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


def strings_of(*objs: Any) -> list[str]:
    """여러 객체에서 문자열만 평탄하게 모은다. 판정은 문자열 '단위'로 해야
    "침수이력: 없음" 같은 라벨을 사고로 오탐하지 않는다."""
    parts: list[str] = []
    for o in objs:
        if o is None:
            continue
        parts.extend(_walk_strings(o))
    return parts


def haystack(*objs: Any) -> str:
    """여러 객체의 모든 문자열을 하나로 합친 검색용 텍스트."""
    return " \n ".join(strings_of(*objs))


# ---------------------------------------------------------------------------
# 쿼리 빌더
# ---------------------------------------------------------------------------
# 실제 엔카 요청에서 확인된 구조 (2026-08, 브라우저 개발자도구):
#
#   (And.
#     (And.Hidden.N._.MultiViewHidden.N._.
#       (C.CarType.N._.
#         (C.Manufacturer.BMW._.ModelGroup.iX.)))
#     _.(Or.AdType.B._.MultiViewAdType.B.))
#
# 즉 바깥 And 가 [검색조건] 과 [광고타입] 을 묶고, 검색조건 안에서
# Hidden/MultiViewHidden 으로 숨김 매물을 제외한 뒤 CarType → Manufacturer
# → ModelGroup 순으로 한 단계씩 (C. ... ) 로 감싸 내려간다.


def build_car_segment(target: dict, include_model: bool = True) -> str:
    """CarType → Manufacturer → ModelGroup [→ Model] 부분을 만든다."""
    ctype = target.get("car_type", "N")
    mfr = target["manufacturer"]
    mg = target["model_group"]
    model = target.get("model") if include_model else None

    if model:
        # 한 단계 더 내려갈 때는 ModelGroup 도 (C. ... ) 로 감싼다
        inner = f"(C.ModelGroup.{mg}._.Model.{model}.)"
        mfr_part = f"(C.Manufacturer.{mfr}._.{inner})"
    else:
        mfr_part = f"(C.Manufacturer.{mfr}._.ModelGroup.{mg}.)"

    return f"(C.CarType.{ctype}._.{mfr_part})"


def build_query(target: dict, ad_type: str = "B", include_year: bool = True,
                include_model: bool = True) -> str:
    """엔카 검색 q 파라미터 생성.

    브라우저에서 복사한 q 를 그대로 쓰려면 target["raw_q"] 에 넣으면 된다.

    include_year=False 로 부르면 연식 필터를 뺀 쿼리가 나온다.
    --probe 가 '연식 필터가 실제로 먹는지' 를 두 쿼리의 결과를 비교해
    판정하는 데 쓴다.
    """
    if target.get("raw_q"):
        return target["raw_q"]

    conds = ["Hidden.N", "MultiViewHidden.N"]
    if include_year:
        # 연식은 yyyyMM 6자리 범위로 지정한다 (추정 — probe 로 검증됨)
        conds.append(f"Year.range({target['year_from']}00..{target['year_to']}12)")

    cond_str = "._.".join(conds)
    car_seg = build_car_segment(target, include_model=include_model)
    search_part = f"(And.{cond_str}._.{car_seg})"
    ad_part = f"(Or.AdType.{ad_type}._.MultiViewAdType.{ad_type}.)"
    return f"(And.{search_part}_.{ad_part})"


def build_sr(offset: int = 0, limit: int = 20, sort: str = "ModifiedDate") -> str:
    """정렬/페이징 파라미터.  형식: |정렬키|오프셋|개수"""
    return f"|{sort}|{offset}|{limit}"


# ---------------------------------------------------------------------------
# 클라이언트
# ---------------------------------------------------------------------------
class EncarClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.interval = float(cfg.get("request_interval_sec", 3.0))
        self.retry = int(cfg.get("retry", 2))
        self.backoff = list(cfg.get("retry_backoff_sec", [5.0, 15.0]))
        self.timeout = int(cfg.get("timeout_sec", 20))
        self._last_req = 0.0
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": cfg.get("user_agent", "Mozilla/5.0"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            "Referer": "http://www.encar.com/",
            "Origin": "http://www.encar.com",
            "Connection": "keep-alive",
        })

    def _throttle(self) -> None:
        wait = self.interval - (time.monotonic() - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.monotonic()

    def get_json(self, url: str, params: dict | None = None, stage: str = "request",
                 allow_404: bool = False):
        """JSON GET. 차단이면 EncarBlocked, 네트워크 오류면 제한적으로 재시도."""
        last_err: Exception | None = None

        for attempt in range(self.retry + 1):
            self._throttle()
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                if _is_unreachable(e):
                    # 프록시 정책 거부/DNS 실패는 재시도해도 결과가 같다.
                    raise EncarUnreachable(
                        stage,
                        "엔카 서버에 연결하지 못했습니다. 엔카의 차단이 아니라 "
                        "네트워크 경로 문제입니다 (프록시 정책 거부 / 방화벽 / DNS / 오프라인).\n"
                        f"  원인: {e.__class__.__name__}: {str(e)[:200]}",
                    ) from e
                last_err = e
                if attempt < self.retry:
                    delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                    warn(f"{stage}: 네트워크 오류 ({e.__class__.__name__}), {delay}s 후 재시도")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"[{stage}] 네트워크 오류로 실패: {e}") from e

            # --- 차단 신호: 재시도 없이 즉시 중단 ---
            if r.status_code in (401, 403, 405, 429):
                raise EncarBlocked(
                    stage,
                    "차단으로 보이는 응답 (429=요청과다 / 403=접근거부). "
                    "즉시 중단합니다. 시간을 두고 다시 시도하세요.",
                    r.status_code,
                )

            if allow_404 and r.status_code == 404:
                return None

            if 500 <= r.status_code < 600:
                last_err = RuntimeError(f"HTTP {r.status_code}")
                if attempt < self.retry:
                    delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                    warn(f"{stage}: 서버 오류 {r.status_code}, {delay}s 후 재시도")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"[{stage}] 서버 오류 {r.status_code}")

            if r.status_code != 200:
                raise RuntimeError(f"[{stage}] 예상치 못한 HTTP {r.status_code}: {r.text[:200]}")

            body = r.text or ""
            low = body[:4000].lower()
            ctype = (r.headers.get("Content-Type") or "").lower()

            if any(mk in low for mk in BLOCK_MARKERS):
                raise EncarBlocked(stage, "응답 본문에 캡차/차단 문구가 감지되었습니다.", r.status_code)

            if "json" not in ctype and body.lstrip()[:1] not in ("{", "["):
                raise EncarBlocked(
                    stage,
                    f"JSON 대신 비-JSON 응답을 받았습니다 (Content-Type={ctype!r}). "
                    "차단 페이지일 가능성이 높습니다.",
                    r.status_code,
                )

            try:
                return r.json()
            except ValueError as e:
                raise EncarBlocked(stage, f"JSON 파싱 실패: {e}", r.status_code) from e

        raise RuntimeError(f"[{stage}] 실패: {last_err}")

    # --- 개별 엔드포인트 -------------------------------------------------
    def search(self, q: str, url: str, offset: int = 0, limit: int = 20,
               sort: str = "ModifiedDate", stage: str = "search"):
        params = {"count": "true", "q": q, "sr": build_sr(offset, limit, sort)}
        return self.get_json(url, params, stage=stage)

    def raw_get(self, url: str, params: dict | None = None, stage: str = "probe"):
        """probe 전용: 상태코드를 예외 없이 그대로 돌려준다.

        차단(EncarBlocked)과 도달 불가(EncarUnreachable)는 그대로 올린다 —
        진단 중이라도 이 둘은 즉시 멈춰야 하기 때문이다.
        반환: (status_code, payload_or_None, 본문 앞부분, 최종 URL)
        """
        self._throttle()
        try:
            r = self.s.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            if _is_unreachable(e):
                raise EncarUnreachable(
                    stage,
                    "엔카 서버에 연결하지 못했습니다. 엔카의 차단이 아니라 "
                    "네트워크 경로 문제입니다 (프록시 정책 거부 / 방화벽 / DNS / 오프라인).\n"
                    f"  원인: {e.__class__.__name__}: {str(e)[:200]}",
                ) from e
            return None, None, f"{e.__class__.__name__}: {e}", url

        body = r.text or ""
        if r.status_code in (401, 403, 405, 429):
            raise EncarBlocked(stage, "차단으로 보이는 응답입니다. 즉시 중단합니다.",
                               r.status_code)
        if any(mk in body[:4000].lower() for mk in BLOCK_MARKERS):
            raise EncarBlocked(stage, "응답 본문에 캡차/차단 문구가 감지되었습니다.",
                               r.status_code)

        payload = None
        if r.status_code == 200:
            try:
                payload = r.json()
            except ValueError:
                payload = None
        return r.status_code, payload, body[:300], r.url

    def detail(self, vid: str):
        return self.get_json(
            DETAIL_URL.format(vid=vid), {"include": DETAIL_INCLUDE},
            stage=f"detail:{vid}", allow_404=True,
        )

    def record(self, vid: str):
        return self.get_json(RECORD_URL.format(vid=vid), stage=f"record:{vid}", allow_404=True)

    def inspection(self, vid: str):
        return self.get_json(INSPECT_URL.format(vid=vid), stage=f"inspect:{vid}", allow_404=True)

    def diagnosis(self, vid: str):
        return self.get_json(DIAGNOSIS_URL.format(vid=vid), stage=f"diag:{vid}", allow_404=True)


# ---------------------------------------------------------------------------
# 검색 결과 파싱
# ---------------------------------------------------------------------------
def extract_search_results(payload: Any) -> list[dict]:
    """검색 응답에서 매물 배열을 꺼낸다. 키 이름이 바뀌어도 최대한 찾아낸다."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    for key in ("SearchResults", "searchResults", "results", "Results", "items", "list"):
        v = payload.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]

    # 마지막 수단: dict 리스트 중 가장 긴 것
    best: list[dict] = []
    for v in payload.values():
        if isinstance(v, list) and v and isinstance(v[0], dict) and len(v) > len(best):
            best = v
    if best:
        warn("검색 응답에서 알려진 결과 키를 못 찾아 가장 긴 리스트를 사용했습니다. "
             "--probe 로 스키마를 확인하세요.")
    return best


def extract_total_count(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    return to_int(pick(payload, "Count", "count", "totalCount", "TotalCount"))


def normalize_listing(raw: dict, target: dict) -> dict:
    """검색 결과 1건 → 표준 필드. 상세 조회 전 단계의 얕은 정보."""
    vid = pick(raw, "Id", "id", "vehicleId", "VehicleId", "CarId")
    vid = str(vid) if vid is not None else None

    year_raw = pick(raw, "Year", "year", "yearMonth", "FormYear", "formYear")
    from common import parse_year_month
    y, m = parse_year_month(year_raw)

    photo = pick(raw, "Photo", "photo", "Photos.0.location", "Image", "image")
    if isinstance(photo, str) and photo.startswith("/"):
        photo = "https://ci.encar.com" + photo

    return {
        "model_key": target["key"],
        "model_label": target["label"],
        "vehicle_id": vid,
        "price_manwon": to_int(pick(raw, "Price", "price", "advertisement.price")),
        "year": y,
        "month": m,
        "mileage_km": to_int(pick(raw, "Mileage", "mileage", "spec.mileage")),
        "region": pick(raw, "OfficeCityState", "officeCityState", "Region", "region", default=""),
        "trim": pick(raw, "Badge", "badge", "BadgeDetail", "badgeDetail",
                     "category.gradeName", default=""),
        "trim_detail": pick(raw, "BadgeDetail", "badgeDetail",
                            "category.gradeDetailName", default=""),
        "sell_type": pick(raw, "SellType", "sellType", default=""),
        "photo_url": photo or "",
        "listing_url": LISTING_PAGE.format(vid=vid) if vid else "",
    }


def matches_target(listing: dict, target: dict) -> bool:
    """연식 범위 + 트림 키워드로 최종 채택 여부 판정."""
    y = listing.get("year")
    if y is not None and not (target["year_from"] <= y <= target["year_to"]):
        return False

    needles = target.get("badge_contains") or []
    if not needles:
        return True
    hay = f"{listing.get('trim','')} {listing.get('trim_detail','')}".lower().replace(" ", "")
    return any(n.lower().replace(" ", "") in hay for n in needles)


# ---------------------------------------------------------------------------
# 상세 파싱
# ---------------------------------------------------------------------------
ACCIDENT_FREE_WORDS = ("무사고", "사고없음", "사고이력없음")
FLOOD_WORDS = ("침수", "전손", "도난")
RENTAL_WORDS = ("렌트", "영업용", "대여", "택시", "리스")
ONE_OWNER_WORDS = ("1인소유", "1인신조", "소유자변경0", "소유자변경없음")

# 이 단어가 같은 문자열 안에 있으면 앞의 키워드는 "라벨"로 보고 무시한다.
NEGATIONS = ("없음", "없습니다", "없슴", "해당없", "미해당", "이력없",
             "아니오", "아님", "무이력", ":n", "=n")


def _flagged(strings: list[str], words: tuple[str, ...]) -> bool:
    """words 중 하나가 '부정어 없이' 등장하는 문자열이 있으면 True.

    엔카 응답은 "침수이력" 처럼 필드 라벨에도 키워드가 들어가므로,
    전체 텍스트를 통으로 검색하면 전 매물이 침수로 잡힌다.
    """
    for s in strings:
        low = s.lower().replace(" ", "")
        if not any(w.lower().replace(" ", "") in low for w in words):
            continue
        if any(neg in low for neg in NEGATIONS):
            continue  # "침수이력: 없음"
        return True
    return False


def normalize_detail(vid: str, detail: Any, record: Any, inspection: Any,
                     diagnosis: Any, target: dict) -> dict:
    """상세/이력/성능점검 응답을 하나의 평탄한 행으로."""
    strs = strings_of(detail, record, inspection, diagnosis)
    hay_flat = " \n ".join(strs).lower().replace(" ", "")

    # 차량번호 — 상세 응답 최상단에 노출됨
    plate = pick(detail or {}, "vehicleNo", "VehicleNo", "carNo", "CarNo",
                 "vehicle.vehicleNo", "manage.vehicleNo", default="")

    # 옵션 목록: 표준/선택 옵션 이름들을 모은다
    options: list[str] = []
    opt_obj = pick(detail or {}, "options", "Options", default=None)
    if opt_obj is not None:
        for s in _walk_strings(opt_obj):
            s = s.strip()
            if s and not s.isdigit() and len(s) <= 60 and s not in options:
                options.append(s)

    # 사고/이력 플래그 — 부정어 가드를 거쳐 문자열 단위로 판정
    flood = _flagged(strs, FLOOD_WORDS)
    rental = _flagged(strs, RENTAL_WORDS)
    diagnosed = bool(diagnosis)

    # 에어서스 키워드: 설정 키워드가 아니라 '실제로 매칭된 옵션명'을 남긴다.
    kws = [k.lower().replace(" ", "") for k in target.get("airsus_keywords", [])]
    airsus_hits: list[str] = []
    for s in (options or strs):
        flat = s.lower().replace(" ", "")
        if len(s) <= 60 and any(k in flat for k in kws) and s not in airsus_hits:
            airsus_hits.append(s.strip())
    if not airsus_hits and any(k in hay_flat for k in kws):
        airsus_hits.append("(옵션 목록 외 텍스트에서 키워드 발견)")

    # 성능점검 요약: 사람이 읽을 짧은 문장으로
    insp_summary = ""
    if inspection:
        bits = []
        for key in ("accidentHistory", "simpleRepair", "specialHistory", "comment", "summary"):
            v = pick(inspection, key)
            if isinstance(v, str) and v.strip():
                bits.append(v.strip())
        insp_summary = " / ".join(bits)[:300]

    owner_changes = to_int(pick(record or {}, "ownerChangeCnt", "ownerChangeCount",
                                "changeCount", "owners"))
    my_acc = to_int(pick(record or {}, "myAccidentCnt", "myAccidentCount", "accidentCnt"))
    other_acc = to_int(pick(record or {}, "otherAccidentCnt", "otherAccidentCount"))
    my_cost = to_int(pick(record or {}, "myAccidentCost", "myAccidentAmount"), 0)

    # 사고 여부는 텍스트보다 record 의 건수가 신뢰도가 높다.
    if my_acc is None and other_acc is None:
        accident_free = _flagged(strs, ACCIDENT_FREE_WORDS)
    else:
        accident_free = not (my_acc or other_acc)
    if flood:
        accident_free = False

    if owner_changes is not None:
        one_owner = owner_changes <= 1
    else:
        one_owner = _flagged(strs, ONE_OWNER_WORDS)

    return {
        "vehicle_id": vid,
        "plate_no": plate or "",
        "options": " | ".join(options[:80]),
        "options_count": len(options),
        "accident_free": accident_free and not (my_acc or other_acc),
        "accident_my_count": my_acc or 0,
        "accident_other_count": other_acc or 0,
        "accident_my_cost_won": my_cost or 0,
        "owner_change_count": owner_changes if owner_changes is not None else "",
        "flood_or_total_loss": flood,
        "rental_or_commercial": rental,
        "one_owner": one_owner or (owner_changes == 1 if owner_changes is not None else False),
        "encar_diagnosed": diagnosed,
        "airsus_keyword_hits": ", ".join(airsus_hits),
        "has_airsus_keyword": bool(airsus_hits),
        "inspection_summary": insp_summary,
        "detail_ok": detail is not None,
    }
