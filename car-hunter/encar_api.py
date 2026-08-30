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

import re
import time
from typing import Any, Iterable

import requests

from common import log, warn, to_int

# 검색 URL 은 config.ENDPOINTS 에서 가져온다 (premium / general).
DETAIL_URL = "https://api.encar.com/v1/readside/vehicle/{vid}"
RECORD_URL = "https://api.encar.com/v1/readside/record/vehicle/{vid}/open"
# 확인됨 (2026-08): 성능점검은 이 경로에서 온다
INSPECT_URL = "https://api.encar.com/v1/readside/inspection/vehicle/{vid}"
DIAGNOSIS_URL = "https://api.encar.com/v1/readside/diagnosis/vehicle/{vid}"
LISTING_PAGE = "http://www.encar.com/dc/dc_cardetailview.do?carid={vid}"
# 옵션 코드→이름 변환표가 있을 만한 곳 (미확인 — probe 가 시험한다)
OPTIONS_MASTER_URL = "https://api.encar.com/v1/readside/options"


def option_map_candidates(vid: str) -> list[tuple[str, str, dict | None]]:
    """옵션 코드→이름 변환표가 있을 만한 경로 후보들.

    실제 경로가 확인되면 OPTIONS_MASTER_URL 만 고치면 된다.
    """
    d = DETAIL_URL.format(vid=vid)
    base = "https://api.encar.com/v1/readside"
    return [
        ("옵션 마스터", OPTIONS_MASTER_URL, None),
        ("차량 옵션 상세", d + "/options", None),
        ("include=OPTIONS만", d, {"include": "OPTIONS"}),
        ("include 없이 전체", d, None),
        ("코드 테이블", f"{base}/code/options", None),
        ("공통 코드", f"{base}/common/options", None),
        ("메타 옵션", f"{base}/meta/options", None),
        ("차량 스펙", d + "/spec", None),
    ]

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


def _join(elems: list[str]) -> str:
    """엔카 q 문법의 원소 연결.

    스칼라 원소는 끝에 '.' 을 붙이고, 원소 사이는 '_.' 로 잇는다.
    그룹은 '(' 로 시작하는 원소이며 '.' 을 붙이지 않는다.

    그룹 판별을 여는 괄호로 하는 것이 중요하다. 닫는 괄호로 판별하면
    'Year.range(202200..202412)' 같은 스칼라 값이 그룹으로 오인되어
    뒤따르는 구분자가 '._.' 가 아닌 '_.' 로 깨진다.

        ["Hidden.N", "Year.range(202200..202412)", "(C...)"]
        -> "Hidden.N._.Year.range(202200..202412)._.(C...)"
    """
    return "_.".join(e if e.startswith("(") else e + "." for e in elems)


def build_car_segment(target: dict, include_model: bool = True,
                      include_car_type: bool = True) -> str:
    """CarType → Manufacturer → ModelGroup [→ Model] 부분."""
    mfr = target["manufacturer"]
    mg = target["model_group"]
    model = target.get("model") if include_model else None

    if model:
        inner = f"(C.ModelGroup.{mg}._.Model.{model}.)"
        seg = f"(C.Manufacturer.{mfr}._.{inner})"
    else:
        seg = f"(C.Manufacturer.{mfr}._.ModelGroup.{mg}.)"

    if include_car_type:
        ctype = target.get("car_type", "N")
        seg = f"(C.CarType.{ctype}._.{seg})"
    return seg


def build_query(target: dict, ad_type: str = "B", include_year: bool = True,
                include_model: bool = True, include_hidden: bool = True,
                include_car_type: bool = True, include_ad: bool | None = None) -> str:
    """엔카 검색 q 파라미터.

    각 조건을 개별로 끌 수 있다. --probe 가 '어떤 조건이 매물을 걸러내는지'
    를 조건별로 빼 보며 건수를 비교하는 데 쓴다.

    브라우저에서 복사한 q 를 그대로 쓰려면 target["raw_q"] 에 넣는다.
    """
    if target.get("raw_q"):
        return target["raw_q"]

    if include_ad is None:
        import config as _cfg
        include_ad = getattr(_cfg, "INCLUDE_AD_TYPE", True)

    conds: list[str] = []
    if include_hidden:
        conds += ["Hidden.N", "MultiViewHidden.N"]
    if include_year:
        conds.append(f"Year.range({target['year_from']}00..{target['year_to']}12)")
    conds.append(build_car_segment(target, include_model, include_car_type))

    search_part = conds[0] if len(conds) == 1 else f"(And.{_join(conds)})"
    if not include_ad:
        return search_part
    ad_part = f"(Or.AdType.{ad_type}._.MultiViewAdType.{ad_type}.)"
    return f"(And.{_join([search_part, ad_part])})"


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

    def option_code_map(self) -> dict[str, str]:
        """옵션 코드→이름 변환표를 한 번만 확보해 캐시한다.

        순서: (1) 사용자가 직접 채운 data/option_codes.json
              (2) 엔카 옵션 마스터 API

        엔카가 옵션을 숫자 코드로 내려주는 경우 이 표가 없으면
        에어서스 판별이 불가능하다. 실패해도 수집은 계속한다.
        """
        if getattr(self, "_code_map", None) is not None:
            return self._code_map
        self._code_map = load_local_option_map()
        if self._code_map:
            return self._code_map
        try:
            payload = self.get_json(OPTIONS_MASTER_URL, stage="옵션변환표", allow_404=True)
            if payload:
                self._code_map = build_code_map(payload)
        except (EncarBlocked, EncarUnreachable):
            raise
        except Exception as e:
            warn(f"옵션 코드 변환표를 못 받았습니다: {e}")
        return self._code_map

    def inspection_page(self, vid: str) -> str | None:
        """성능기록부 HTML 페이지를 가져온다. JSON 이 아니므로 별도 처리."""
        import config as _cfg
        url = _cfg.INSPECTION_PAGE_URL.format(vid=vid)
        self._throttle()
        try:
            r = self.s.get(url, timeout=self.timeout,
                           headers={"Accept": "text/html,application/xhtml+xml"})
        except requests.RequestException as e:
            if _is_unreachable(e):
                raise EncarUnreachable(
                    f"inspection_page:{vid}",
                    "엔카 서버에 연결하지 못했습니다 (프록시/방화벽/DNS).\n"
                    f"  원인: {e.__class__.__name__}: {str(e)[:200]}") from e
            warn(f"성능기록부 페이지 조회 실패({vid}): {e}")
            return None

        if r.status_code in (401, 403, 405, 429):
            raise EncarBlocked(f"inspection_page:{vid}",
                               "차단으로 보이는 응답입니다.", r.status_code)
        if r.status_code != 200:
            return None
        body = r.text or ""
        if any(mk in body[:4000].lower() for mk in BLOCK_MARKERS):
            raise EncarBlocked(f"inspection_page:{vid}",
                               "응답 본문에 캡차/차단 문구가 감지되었습니다.",
                               r.status_code)
        return body

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


def match_reason(listing: dict, target: dict) -> str | None:
    """채택하지 않을 이유를 돌려준다. 채택 대상이면 None.

    이유를 남기는 이유: --probe 에서 '왜 걸러졌는지' 를 보여줘야
    필터가 과한지 부족한지 판단할 수 있다.
    """
    import config as _cfg

    y = listing.get("year")
    if y is not None and not (target["year_from"] <= y <= target["year_to"]):
        return f"연식 범위 밖 ({y})"

    hay = f"{listing.get('trim','')} {listing.get('trim_detail','')}"
    flat = hay.lower().replace(" ", "")

    for bad in (target.get("badge_excludes") or []):
        if bad.lower().replace(" ", "") in flat:
            return f"제외 트림 '{bad}' ({hay.strip()})"

    needles = target.get("badge_contains") or []
    if needles and not any(n.lower().replace(" ", "") in flat for n in needles):
        return f"트림 불일치 ({hay.strip() or '표기 없음'})"

    sell = str(listing.get("sell_type") or "")
    for bad in getattr(_cfg, "EXCLUDE_SELL_TYPES", []):
        if bad in sell:
            return f"판매형태 제외 ({sell})"

    return None


def matches_target(listing: dict, target: dict) -> bool:
    """연식 + 트림 + 판매형태로 최종 채택 여부 판정."""
    return match_reason(listing, target) is None


# ---------------------------------------------------------------------------
# 보험이력(record) 파싱
# ---------------------------------------------------------------------------
# 엔카 record 응답의 실제 필드명은 확인되지 않았다. 아래는 후보 목록이며,
# 어느 것도 없으면 값을 0 으로 채우지 않고 None(=응답에 없음) 으로 둔다.
# 없는 값을 0 으로 채우면 '정보 없음' 이 '사고 없음' 으로 둔갑한다.
RECORD_FIELDS: dict[str, tuple[str, ...]] = {
    # 사고 건수
    "my_accident_count":    ("myAccidentCnt", "myAccidentCount", "myAccdCnt"),
    "other_accident_count": ("otherAccidentCnt", "otherAccidentCount", "otherAccdCnt"),
    "accident_count":       ("accidentCnt", "accidentCount", "totalAccidentCnt"),
    # 사고 금액 (원)
    "my_accident_cost":     ("myAccidentCost", "myAccidentAmount", "myAccdCost"),
    "other_accident_cost":  ("otherAccidentCost", "otherAccidentAmount", "otherAccdCost"),
    # 소유/번호 변경
    "owner_change_count":   ("ownerChangeCnt", "ownerChangeCount", "changeCount", "owners"),
    "plate_change_count":   ("carNoChangeCnt", "carNoChangeCount", "noChangeCnt"),
    # 특수 이력
    "total_loss_count":     ("totalLossCnt", "totalLossCount"),
    "flood_total_count":    ("floodTotalLossCnt", "floodTotalCnt"),
    "flood_part_count":     ("floodPartLossCnt", "floodPartCnt"),
    "theft_count":          ("robberCnt", "theftCnt", "robberyCnt"),
    # 과거 용도 이력 (현재 판매형태와 별개 — 과거에 그렇게 '등록' 된 적이 있는가)
    "rental_use_count":     ("loan", "loanCnt", "rentCnt", "rentalCnt"),
    "business_use_count":   ("business", "businessCnt", "businessCount"),
    "government_use_count": ("government", "governmentCnt", "governmentCount"),
    # 최초등록일
    "first_registration":   ("firstDate", "firstRegDate", "firstRegistrationDate",
                             "firstRegisterDate"),
}

# 사고 상세 배열이 있을 만한 경로
ACCIDENT_LIST_PATHS = ("accidents", "accidentList", "records", "history", "list")

# 용도 이력 배열 (대여/영업용 등록 이력이 여기에 들어온다)
USE_HISTORY_PATHS = ("carInfoUse1s", "carInfoUse2s", "useHistory", "usageHistory")
RENTAL_USE_WORDS = ("대여", "렌트", "렌터", "리스")
BUSINESS_USE_WORDS = ("영업", "사업", "택시", "화물", "운수")
GOVERNMENT_USE_WORDS = ("관용", "국가", "지자체")

# 성능점검 부위 분류 — 엔카 성능점검기록부 기준
BOLT_ON_PARTS = ("후드", "프론트펜더", "펜더", "도어", "트렁크리드", "범퍼",
                 "라디에이터서포트", "쿼터패널", "루프패널", "사이드실")
FRAME_PARTS = ("사이드멤버", "필러", "대시패널", "플로어", "휠하우스",
               "인사이드패널", "크로스멤버", "리어패널", "패키지트레이",
               "트렁크플로어", "프론트패널")
WELD_WORDS = ("판금", "용접", "골격", "부식")


def _blank(v):
    """None(응답에 없음)은 빈 문자열로. 0 과 구분하기 위함."""
    return "" if v is None else v


def normalize_record(record: Any) -> dict:
    """보험이력 응답을 표준 필드로. 못 찾은 값은 None 으로 둔다.

    반환 dict 에는 각 값과 함께 'record_fields_found'(찾은 필드 이름들)이
    들어간다. probe 가 무엇이 실제로 왔는지 보여주는 데 쓴다.
    """
    out: dict[str, Any] = {k: None for k in RECORD_FIELDS}
    out["record_available"] = isinstance(record, dict) and bool(record)
    out["record_fields_found"] = []
    out["record_fields_null"] = []
    out["use_history"] = []
    out["accident_details"] = []

    if not isinstance(record, dict):
        return out

    out["record_fields_null"] = []
    for std, cands in RECORD_FIELDS.items():
        # 키가 아예 없는 경우와, 키는 있는데 값이 null 인 경우를 구분한다.
        present = any(c in record for c in cands)
        v = pick(record, *cands)
        if v is None:
            if present:
                out["record_fields_null"].append(std)   # 응답에 있으나 값이 없음
            continue
        out["record_fields_found"].append(std)
        if std == "first_registration":
            out[std] = str(v)
        else:
            out[std] = to_int(v)

    # 용도 이력 배열 — 내용을 보고 대여/영업/관용을 판정한다
    out["use_history"] = []
    for path in USE_HISTORY_PATHS:
        arr = pick(record, path)
        if isinstance(arr, list) and arr:
            for item in arr[:30]:
                txt = " ".join(strings_of(item)) if not isinstance(item, str) else item
                if txt.strip():
                    out["use_history"].append(txt.strip()[:80])
            out["record_fields_found"].append(f"use_history({path})")

    # 용도 이력 배열이 있으면 정수 필드보다 우선한다.
    # loan=0 인데 carInfoUse2s 에 '대여용(렌터카)' 가 들어 있는 경우가 있다.
    # 정수 필드만 믿으면 대여 이력을 놓친다. 둘 중 큰 값을 쓴다.
    if out["use_history"]:
        for field, words in (("rental_use_count", RENTAL_USE_WORDS),
                             ("business_use_count", BUSINESS_USE_WORDS),
                             ("government_use_count", GOVERNMENT_USE_WORDS)):
            n = sum(1 for t in out["use_history"] if any(w in t for w in words))
            if n:
                cur = out[field]
                out[field] = n if cur is None else max(cur, n)
                if field not in out["record_fields_found"]:
                    out["record_fields_found"].append(f"{field}(용도이력배열)")

    # 사고 건별 상세 (있으면)
    for path in ACCIDENT_LIST_PATHS:
        arr = pick(record, path)
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            for a in arr[:20]:
                out["accident_details"].append({
                    "date": pick(a, "date", "accidentDate", "insuranceDate"),
                    "type": pick(a, "type", "accidentType", "gubun"),
                    "part_cost": to_int(pick(a, "partCost", "partAmount")),
                    "labor_cost": to_int(pick(a, "laborCost", "laborAmount")),
                    "paint_cost": to_int(pick(a, "paintingCost", "paintCost",
                                              "paintAmount")),
                    "total": to_int(pick(a, "insuranceBenefit", "totalCost", "amount")),
                })
            out["record_fields_found"].append(f"accident_details({path})")
            break
    return out


def _rank_table() -> list[tuple[str, str]]:
    """(부위명, 랭크키) 목록을 '긴 이름 먼저' 로 정렬해서 돌려준다.

    긴 것부터 봐야 '트렁크플로어'(골격A)가 '플로어패널'(골격C)로,
    '프론트펜더'(외판1)가 '프론트패널'(골격A)로 잘못 잡히지 않는다.
    """
    import config as _cfg
    pairs = [(p, rank) for rank, spec in _cfg.INSPECTION_RANKS.items()
             for p in spec["parts"]]
    return sorted(pairs, key=lambda t: len(t[0]), reverse=True)


def classify_part(name: str) -> str | None:
    """부위명을 랭크키로. 표에 없으면 None (= 미분류)."""
    flat = (name or "").replace(" ", "")
    if not flat:
        return None
    for part, rank in _rank_table():
        if part.replace(" ", "") in flat:
            return rank
    return None


STATUS_KEYS = ("statustype", "status", "statuscode", "state", "code", "value", "gubun")
PART_KEYS = ("title", "name", "partname", "part", "label", "typename", "itemname")


def _status_of(d: dict) -> str | None:
    """dict 안에서 상태 부호(X/W/A/U/C/T)를 찾는다.

    'statusType': 'X' 뿐 아니라 'statusType': {'code': 'X', 'title': '교환'}
    같은 형태도 받는다. 다만 부호 한 글자를 아무 필드에서나 줍지 않도록
    키 이름이 상태를 뜻하는 경우로 한정한다.
    """
    import config as _cfg
    for k, v in d.items():
        if not any(sk in k.lower() for sk in STATUS_KEYS):
            continue
        if isinstance(v, str) and v.strip().upper() in _cfg.INSPECTION_STATUS:
            return v.strip().upper()
        if isinstance(v, dict):
            for vv in v.values():
                if isinstance(vv, str) and vv.strip().upper() in _cfg.INSPECTION_STATUS:
                    return vv.strip().upper()
    return None


def _part_of(d: dict) -> str | None:
    """dict 안에서 부위명을 찾는다."""
    for k, v in d.items():
        if isinstance(v, str) and v.strip() and any(pk in k.lower() for pk in PART_KEYS):
            return v.strip()
        if isinstance(v, dict):
            inner = _part_of(v)
            if inner:
                return inner
    return None


def _status_label(code: str) -> str:
    import config as _cfg
    return _cfg.INSPECTION_STATUS.get(code, (code, 0.0))[0]


def _title_of(d: dict) -> str | None:
    """dict 의 이름표. type.title / title / name 순으로 본다."""
    t = pick(d, "type.title", "type.name", "title", "name", "partName", "itemName")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return None


def _code_of(d: dict) -> str | None:
    c = pick(d, "type.code", "code", "partCode")
    return str(c).strip() if isinstance(c, (str, int)) and str(c).strip() else None


def find_repair_entries(inspection: Any) -> list[dict]:
    """성능점검 응답에서 (부위, 상태부호) 쌍을 모두 찾아낸다.

    엔카 성능점검은 inners / outers / etcs 아래에 children 이 한 단계 더
    있는 트리다. 부위명이 부모(type.title)에, 상태 부호가 자식에 있는
    경우가 있으므로 부모 이름을 물고 내려간다.
    """
    out: list[dict] = []
    seen: set[tuple] = set()

    def walk(obj, path: str = "", parents: tuple[str, ...] = ()):
        if isinstance(obj, dict):
            title = _title_of(obj)
            status = _status_of(obj)
            here = parents + ((title,) if title else ())

            if status:
                # 부위명은 자기 이름 우선, 없으면 가장 가까운 부모 이름
                part = title or (parents[-1] if parents else None)
                if part:
                    key = (part, status, path)
                    if key not in seen:
                        seen.add(key)
                        out.append({
                            "part": part, "status": status, "path": path,
                            "code": _code_of(obj) or "",
                            "context": " > ".join(here) if here else "",
                        })
            for k, v in obj.items():
                # 상태 부호 dict 안으로는 내려가지 않는다.
                # {'statusType': {'code':'W','title':'판금'}} 에 들어가면
                # '판금' 이 부위명으로 잡힌다.
                if any(sk in k.lower() for sk in STATUS_KEYS):
                    continue
                walk(v, f"{path}.{k}" if path else k, here)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]", parents)

    walk(inspection)
    for e in out:
        # 최상위 섹션(inners/outers/etcs)을 기록해 둔다.
        # inners 는 자기진단 항목이라 차체 수리 부위와 성격이 다르다.
        e["section"] = e["path"].split("[")[0].split(".")[0]
    return out


def score_repairs(entries: list[dict]) -> dict:
    """수리 기록을 법정 등급으로 분류하고 감점을 계산한다."""
    import config as _cfg

    graded, unclassified, diagnostics = [], [], []
    for e in entries:
        rank = classify_part(e["part"])
        if rank is None:
            # 자기진단(inners) 항목은 차체 부위가 아니므로 '미분류' 로
            # 보고하지 않는다. 별도로 모아 참고 표시만 한다.
            if e.get("section") == "inners":
                st = _status_label(e["status"])
                diagnostics.append(f"{e['part']} {st}({e['status']})")
            else:
                unclassified.append(e["part"])
            continue
        spec = _cfg.INSPECTION_RANKS[rank]
        lo, hi = spec["range"]
        st_label, weight = _cfg.INSPECTION_STATUS.get(e["status"], (e["status"], 0.5))
        penalty = lo + (hi - lo) * weight
        graded.append({
            "part": e["part"], "status": e["status"], "status_label": st_label,
            "rank": rank, "rank_label": spec["label"], "desc": spec["desc"],
            "penalty": round(penalty, 1),
            # 리포트 표기: "사이드멤버 판금/용접(W) — 골격 B랭크, 충돌이 뼈대까지 전달됨"
            "note": f"{e['part']} {st_label}({e['status']}) — {spec['label']}, {spec['desc']}",
        })

    graded.sort(key=lambda g: g["penalty"], reverse=True)
    if graded:
        total = graded[0]["penalty"] + sum(
            g["penalty"] for g in graded[1:]) * _cfg.INSPECTION_EXTRA_RATIO
    else:
        total = 0.0

    worst = graded[0] if graded else None
    return {
        "entries": graded,
        "worst_status": worst["status"] if worst else None,
        "diagnostics": diagnostics,
        "unclassified": unclassified,
        "penalty": round(total, 1),
        "worst_rank": worst["rank"] if worst else None,
        "worst_note": worst["note"] if worst else None,
    }


# 사고 건별 type 코드의 의미는 문서화돼 있지 않다. 아래는 추정이며,
# infer_accident_types() 가 myAccidentCnt / otherAccidentCnt 와 대조해
# 맞는지 검증한다. 검증에 실패하면 '추정' 딱지를 붙여 표시한다.
ACCIDENT_TYPE_GUESS = {
    "1": "내차 피해",
    "2": "내차 피해(부분)",
    "3": "타차 가해",
    "4": "타차 가해(부분)",
}


def infer_accident_types(rec: dict) -> tuple[dict, str]:
    """사고 건별 type 코드의 의미를 건수와 대조해 검증한다.

    myAccidentCnt / otherAccidentCnt 를 알고 있으므로, type 별 건수를 세어
    일치하면 추정이 맞다고 볼 수 있다.
    """
    details = rec.get("accident_details") or []
    if not details:
        return {}, "사고 상세 없음"

    counts: dict[str, int] = {}
    for a in details:
        t = str(a.get("type") or "?")
        counts[t] = counts.get(t, 0) + 1

    my_n, other_n = rec.get("my_accident_count"), rec.get("other_accident_count")
    mine = sum(n for t, n in counts.items() if ACCIDENT_TYPE_GUESS.get(t, "").startswith("내차"))
    others = sum(n for t, n in counts.items() if ACCIDENT_TYPE_GUESS.get(t, "").startswith("타차"))

    if my_n is None or other_n is None:
        return counts, "검증 불가 (건수 필드 없음)"
    if mine == my_n and others == other_n:
        return counts, f"검증됨 (내차 {mine}건 / 타차 {others}건 일치)"
    return counts, (f"추정과 불일치 — type별 {counts} 인데 "
                    f"내차 {my_n}건 / 타차 {other_n}건. 코드 의미 재확인 필요")


def describe_accidents(rec: dict) -> tuple[list[str], str]:
    """사고 건별 상세를 사람이 읽을 문장으로."""
    counts, verdict = infer_accident_types(rec)
    lines = []
    for a in (rec.get("accident_details") or []):
        t = str(a.get("type") or "?")
        label = ACCIDENT_TYPE_GUESS.get(t, f"유형 {t}")
        if "검증됨" not in verdict:
            label += "(추정)"
        parts = []
        for key, nm in (("part_cost", "부품"), ("labor_cost", "공임"),
                        ("paint_cost", "도장")):
            v = a.get(key)
            if v:
                parts.append(f"{nm} {v:,}원")
        total = a.get("total")
        head = f"{a.get('date') or '일자미상'} {label}"
        if total:
            head += f" 보험금 {total:,}원"
        lines.append(head + (" (" + ", ".join(parts) + ")" if parts else ""))
    return lines, verdict


# ---------------------------------------------------------------------------
# 성능기록부 HTML 페이지 파서 (주 소스)
# ---------------------------------------------------------------------------
def resolve_part(name: str) -> tuple[str | None, str | None]:
    """부위 표기 -> (표준 부위명, 랭크). 못 알아보면 (None, None).

    페이지는 '프론트 휀더(우)', '필러 패널 A(우)' 처럼 띄어쓰기와 좌우
    표기가 섞인 이름을 준다. 공백을 지우고 긴 이름부터 맞춰 본다.
    """
    import config as _cfg
    flat = re.sub(r"[\s()（）\[\]]", "", name or "")
    flat = re.sub(r"[좌우전후상하]$", "", flat)
    if not flat:
        return None, None

    # 1) 등급표의 표준 부위명
    for part, rank in _rank_table():
        if part.replace(" ", "") in flat:
            return part, rank
    # 2) 구어체/표기 변형 별칭
    for alias, canon in _alias_table():
        if alias.replace(" ", "") in flat:
            return canon, classify_part(canon)
    return None, None


def _norm_state(text: str) -> str | None:
    """'양호' / '불량' 판정. 판단 못 하면 None."""
    import config as _cfg
    t = (text or "").strip()
    if not t:
        return None
    for w in _cfg.BAD_STATE_WORDS:
        if w in t:
            return "불량"
    for w in _cfg.GOOD_STATE_WORDS:
        if w in t:
            return "양호"
    return None


def _cells(soup) -> list[list[str]]:
    """표의 행별 셀 텍스트. 표가 아니면 빈 목록."""
    rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)
    return rows


# 상태 부호 옆에 붙어 나오는 말들. 맨 글자 표기를 받아들일 근거가 된다.
STATUS_HINT_WORDS = ("교환", "판금", "용접", "부식", "흠집", "요철", "손상", "상태")


def _symbols_in(text: str) -> list[str]:
    """텍스트 안의 상태 부호를 표준 코드로.

    동그라미 기호(ⓧ ⓦ ...)를 우선한다. 맨 글자 X/W/C/A/U/T 는 부위 이름의
    일부일 수 있어서(예: '필러 패널 A(우)' 의 A) 제한적으로만 받는다.
    """
    import config as _cfg
    out = [_cfg.INSPECTION_SYMBOLS[ch] for ch in text
           if ch in _cfg.INSPECTION_SYMBOLS]
    if out:
        return list(dict.fromkeys(out))

    # 동그라미가 하나도 없을 때만 맨 글자를 본다.
    # 그것도 상태를 뜻하는 낱말이 같이 있거나, 셀 자체가 부호 한 글자일 때만.
    stripped = text.strip()
    has_hint = any(w in text for w in STATUS_HINT_WORDS)
    if not has_hint and len(stripped) > 3:
        return []
    for m in re.finditer(r"(?<![A-Za-z가-힣])([XWCAUT])(?![A-Za-z가-힣(])", text):
        out.append(m.group(1))
    return list(dict.fromkeys(out))


STATE_PAIRS = {
    "state": ("양호", "불량"),
    "yesno": ("없음", "있음"),
}


def _selection_score(el) -> int:
    """이 요소가 '선택된 값' 으로 보이는 정도. 양수면 선택, 음수면 비선택."""
    import config as _cfg
    M = _cfg.SELECTED_MARKERS
    score = 0
    node = el
    for _ in range(3):            # 자기 자신 + 부모 2단계까지 본다
        if node is None or not getattr(node, "name", None):
            break
        if node.name in M["tags"]:
            score += 2
        cls = " ".join(node.get("class") or []).lower()
        if cls:
            if any(w in cls for w in M["class_off"]):
                score -= 3
            elif any(w in cls for w in M["class_on"]):
                score += 3
        style = (node.get("style") or "").lower().replace(" ", "")
        if style:
            if any(w.replace(" ", "") in style for w in M["style_off"]):
                score -= 3
            elif any(w.replace(" ", "") in style for w in M["style_on"]):
                score += 3
        node = node.parent
    for img in (el.find_all("img") if hasattr(el, "find_all") else []):
        src = (img.get("src") or "") + " " + (img.get("alt") or "")
        if any(w in src.lower() for w in M["img_on"]):
            score += 2
    return score


def _pick_selected(cell, kind: str) -> tuple[str | None, str]:
    """선택지가 둘 다 적힌 칸에서 '선택된' 값을 고른다.

    반환: (값, 근거). 판별 못 하면 (None, 이유).

    이게 이 파서에서 가장 중요한 부분이다. 텍스트만 읽으면 "양호 불량" 이
    통째로 값이 되고, 거기에 '불량' 이 들어 있으니 멀쩡한 차가 전부
    불량으로 판정된다. 확실하지 않으면 '판정 불가' 를 돌려준다.
    """
    import config as _cfg
    good, bad = STATE_PAIRS.get(kind, ("양호", "불량"))
    text = cell.get_text(" ", strip=True) if hasattr(cell, "get_text") else str(cell)

    # txt_state span 이 있으면 문구가 무엇이든(해당/미이행 등) 그대로 읽는다
    spans0 = [el for el in cell.find_all(True)
              if _cfg.STATE_SPAN_CLASS in " ".join(el.get("class") or [])] \
        if hasattr(cell, "find_all") else []
    if spans0:
        picked0 = [el for el in spans0
                   if any(c in (el.get("class") or [])
                          for c in _cfg.STATE_SELECTED_CLASSES)]
        if len(picked0) == 1:
            return picked0[0].get_text(strip=True), "txt_state on/active"
        if not picked0:
            return None, f"{_cfg.STATE_SPAN_CLASS} 에 on/active 가 없음 (판정 불가)"
        return None, f"선택 표시가 {len(picked0)}개 (판정 불가)"

    has_good, has_bad = good in text, bad in text
    if has_good and not has_bad:
        return good, "한쪽만 표기"
    if has_bad and not has_good:
        return bad, "한쪽만 표기"
    if not has_good and not has_bad:
        return None, "선택지 문구가 없음"

    # 1순위: 엔카가 주석으로 명시한 확정 규칙.
    #   class="txt_state on"  또는 "txt_state active" 가 선택된 값.
    spans = [el for el in cell.find_all(True)
             if _cfg.STATE_SPAN_CLASS in " ".join(el.get("class") or [])]
    if spans:
        picked = [el for el in spans
                  if any(c in (el.get("class") or [])
                         for c in _cfg.STATE_SELECTED_CLASSES)]
        if len(picked) == 1:
            return picked[0].get_text(strip=True), "txt_state on/active"
        if not picked:
            return None, f"{_cfg.STATE_SPAN_CLASS} 에 on/active 가 없음 (판정 불가)"
        return None, f"선택 표시가 {len(picked)}개 (판정 불가)"

    # 2순위: 굵기/색/클래스 휴리스틱
    cands: list[tuple[str, int]] = []
    for word in (good, bad):
        for el in cell.find_all(string=lambda t, w=word: t and w in t):
            parent = el.parent
            if parent is None:
                continue
            # 그 요소의 텍스트가 해당 낱말만인 경우가 선택 표시 대상
            if parent.get_text(strip=True) not in (word,):
                continue
            cands.append((word, _selection_score(parent)))
            break

    if len(cands) < 2:
        return None, "선택 표시를 구분할 마크업이 없음 (판정 불가)"

    cands.sort(key=lambda t: t[1], reverse=True)
    if cands[0][1] == cands[1][1]:
        return None, "선택 표시가 같아 구분 불가 (판정 불가)"
    if cands[0][1] <= 0:
        return None, "선택된 값을 특정할 수 없음 (판정 불가)"
    return cands[0][0], f"마크업 판별 (점수 {cands[0][1]} vs {cands[1][1]})"


RANK_LABELS = [
    ("외판1", ["외판부위 1랭크", "외판 1랭크", "1랭크"]),
    ("외판2", ["외판부위 2랭크", "외판 2랭크", "2랭크"]),
    ("골격A", ["주요골격 A랭크", "골격 A랭크", "A랭크"]),
    ("골격B", ["주요골격 B랭크", "골격 B랭크", "B랭크"]),
    ("골격C", ["주요골격 C랭크", "골격 C랭크", "C랭크"]),
]


def _status_from_element(el) -> str | None:
    """요소에서 상태 부호를 읽는다. 동그라미 문자 / class / img 순으로."""
    import config as _cfg
    txt = el.get_text(" ", strip=True) if hasattr(el, "get_text") else str(el)
    for ch in txt:
        if ch in _cfg.INSPECTION_SYMBOLS:
            return _cfg.INSPECTION_SYMBOLS[ch]

    if not hasattr(el, "find_all"):
        return None
    # class="ico_x" / "mark_w" / "state-x" 같은 표기
    for node in [el] + el.find_all(True):
        classes = [c.lower() for c in (node.get("class") or [])]
        cls = " ".join(classes)
        m = re.search(r"(?:ico|icon|mark|state|status|stat)[_\-]?([xwcaut])\b", cls)
        if m:
            return m.group(1).upper()
        # class="ico_state x" 처럼 부호가 별도 토큰인 경우
        if any(re.search(r"(ico|icon|mark|state|status|stat)", c) for c in classes):
            for c in classes:
                if c in ("x", "w", "c", "a", "u", "t"):
                    return c.upper()
        for img in ([node] if node.name == "img" else []):
            src = ((img.get("src") or "") + " " + (img.get("alt") or "")).lower()
            m2 = re.search(r"[_/\-]([xwcaut])[._\-]", src)
            if m2:
                return m2.group(1).upper()
            for sym, code in _cfg.INSPECTION_SYMBOLS.items():
                if sym in (img.get("alt") or ""):
                    return code
    return None


def parse_inspection_page(html_text: str) -> dict:
    """성능기록부 HTML 을 구조화한다.

    핵심 원칙: 확실하지 않으면 '판정 불가' 로 둔다. 이 페이지는 선택지를
    양쪽 다 글자로 적어 두고 굵기·색·클래스로만 실제 판정을 표시하므로,
    텍스트만 읽으면 멀쩡한 차가 전부 '불량' 이 된다.
    """
    import config as _cfg
    from bs4 import BeautifulSoup

    out: dict[str, Any] = {
        "page_available": False, "repairs": [], "unmatched_parts": [],
        "detail_bad": [], "detail_unknown": [], "ev_hv": {}, "ev_hv_bad": [],
        "ev_hv_unknown": [], "fields": {}, "field_notes": {},
        "rank_sections": {}, "parse_note": "",
    }
    if not html_text or not html_text.strip():
        out["parse_note"] = "페이지 내용이 비어 있습니다"
        return out

    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception:
        soup = BeautifulSoup(html_text, "html.parser")

    out["page_available"] = bool(soup.get_text(strip=True))

    def _match_score(cell, labels) -> int:
        """라벨 일치 품질. 0 이면 불일치.

        '주행거리' 로 찾을 때 '주행거리 계기상태' 칸이 먼저 걸리면 안 된다.
        정확히 같은 칸을 가장 높게 친다.
        """
        ct = cell.get_text(" ", strip=True).replace(" ", "")
        best = 0
        for lb in labels:
            l = lb.replace(" ", "")
            if not l or l not in ct:
                continue
            if ct == l:
                best = max(best, 3)
            elif ct.startswith(l) and len(ct) - len(l) <= 2:
                best = max(best, 2)
            else:
                best = max(best, 1)
        return best

    def _match(cell, labels) -> bool:
        return _match_score(cell, labels) > 0

    def _has_state(cell) -> bool:
        return bool([el for el in cell.find_all(True)
                     if _cfg.STATE_SPAN_CLASS in " ".join(el.get("class") or [])])

    # 라벨 -> 값 칸(요소)을 찾는다.
    #
    # 이 페이지는 두 가지 배치를 섞어 쓴다:
    #   (가) 같은 행:   <th>라벨</th><td>값</td>
    #   (나) 헤더 행 + 데이터 행:
    #        <tr><th>사고이력</th><th>단순수리</th></tr>
    #        <tr><td>값</td><td>값</td></tr>
    # (나)를 (가)로 읽으면 옆 헤더('상태' 등)를 값으로 집는다.
    def _cell_for(labels: list[str]):
        cands: list[tuple[int, int, Any]] = []   # (일치품질, 우선순위, 값칸)

        # (가) 같은 행에서 라벨 다음 칸
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            header_only = all(c.name == "th" for c in cells)
            if header_only:
                continue            # 헤더 행은 (나)에서 처리
            for i, c in enumerate(cells):
                q = _match_score(c, labels)
                if not q:
                    continue
                for nxt in cells[i + 1:]:
                    if nxt.get_text(strip=True):
                        cands.append((q, 3, nxt))
                        break

        # (나) 헤더 행에서 라벨을 찾고, 다음 데이터 행의 같은 열
        for tr in soup.find_all("tr"):
            heads = tr.find_all(["th", "td"])
            if not heads or not all(h.name == "th" for h in heads):
                continue
            for idx, h in enumerate(heads):
                q = _match_score(h, labels)
                if not q:
                    continue
                nxt_row = tr.find_next_sibling("tr")
                while nxt_row is not None:
                    data = nxt_row.find_all(["td", "th"])
                    if data and not all(d.name == "th" for d in data):
                        if idx < len(data):
                            cands.append((q, 2, data[idx]))
                        break
                    nxt_row = nxt_row.find_next_sibling("tr")

        # (다) dl/dt/dd 또는 라벨 요소 바로 다음 형제
        for tag in soup.find_all(["dt", "th", "span", "strong", "label"]):
            q = _match_score(tag, labels)
            if not q:
                continue
            sib = tag.find_next_sibling()
            if sib is not None and sib.get_text(strip=True):
                cands.append((q, 1, sib))

        if not cands:
            return None
        cands.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return cands[0][2]

    # --- 1) 라벨/값 ---
    for key, labels, kind in _cfg.INSPECTION_PAGE_FIELDS:
        cell = _cell_for(labels)
        if cell is None:
            continue
        raw = cell.get_text(" ", strip=True)
        if kind in STATE_PAIRS:
            val, why = _pick_selected(cell, kind)
            out["fields"][key] = val
            out["field_notes"][key] = why if val else f"{why} (원문: {raw[:40]})"
        elif kind == "number":
            out["fields"][key] = to_int(raw)
            out["field_notes"][key] = raw[:40]
        else:
            out["fields"][key] = raw[:400]
            out["field_notes"][key] = "텍스트"

    # --- 2) 수리 부위: 랭크 '블록' 단위로 ---
    # 랭크가 <tr> 이 아니라 <div>/<li> 구조일 수 있으므로 태그를 가리지 않는다.
    all_rank_labels = [lb for _r, lbs in RANK_LABELS for lb in lbs]

    def _rank_block(labels: list[str]):
        """랭크 라벨과 '그 랭크의 내용' 을 함께 담은 가장 작은 요소.

        라벨만 든 <span class="tit">1랭크</span> 를 그대로 쓰면 내용이 비어
        전부 '없음' 이 된다. 내용이 나올 때까지 부모로 올라가되, 다른 랭크가
        섞이기 시작하면 멈춘다.
        """
        matched = [el for el in soup.find_all(True)
                   if el.name not in ("html", "body")
                   and any(lb.replace(" ", "") in
                           el.get_text(" ", strip=True).replace(" ", "")
                           for lb in labels)]
        if not matched:
            return None
        node = min(matched, key=lambda e: len(e.get_text(" ", strip=True)))

        def _body_of(el) -> str:
            t = el.get_text(" ", strip=True)
            for lb in labels:
                t = t.replace(lb, " ")
            return re.sub(r"\s+", " ", t).strip(" -–—:")

        def _other_rank_in(el) -> bool:
            t = el.get_text(" ", strip=True).replace(" ", "")
            mine = {lb.replace(" ", "") for lb in labels}
            return any(lb.replace(" ", "") in t
                       for lb in all_rank_labels
                       if lb.replace(" ", "") not in mine)

        cur = node
        for _ in range(5):
            if _body_of(cur) and not _other_rank_in(cur):
                return cur
            if cur.parent is None:
                break
            if _other_rank_in(cur.parent):
                break
            cur = cur.parent
        return cur

    for rank, labels in RANK_LABELS:
        block = _rank_block(labels)
        if block is None:
            continue

        # 라벨 자체를 뺀 나머지가 그 랭크의 내용
        body = block.get_text(" ", strip=True)
        for lb in labels:
            body = body.replace(lb, " ")
        body = re.sub(r"\s+", " ", body).strip(" -–—:")
        if not body or body in ("없음", "-", "해당없음"):
            out["rank_sections"][rank] = "없음"
            continue
        out["rank_sections"][rank] = body[:120]

        def _is_container(node) -> bool:
            """하위에 또 부위명이 있으면 이 요소는 묶음이지 부위가 아니다.

            이걸 안 걸러내면 '프론트 휀더(우) 교환 프론트 도어(우) 교환' 이
            통째로 하나의 부위로 잡힌다.
            """
            for d in node.find_all(True):
                t = d.get_text(" ", strip=True)
                if t and len(t) <= 30 and resolve_part(t)[0]:
                    return True
            return False

        found_here = 0
        for node in block.find_all(True):
            name = node.get_text(" ", strip=True)
            if not name or len(name) > 30:
                continue
            if any(lb.replace(" ", "") in name.replace(" ", "") for lb in labels):
                continue
            canon, _r = resolve_part(name)
            if not canon:
                continue
            if _is_container(node):
                continue
            st = _status_from_element(node)
            sib = node.find_next_sibling()
            if not st and sib is not None:
                st = _status_from_element(sib)
            if not st and node.parent is not None:
                st = _status_from_element(node.parent)
            pos = ""
            m = re.search(r"[(（]\s*([좌우전후])\s*[)）]", name)
            if m:
                pos = m.group(1)
            key = (canon, st or "?", pos)
            if any((r["part"], r["status"], r["position"]) == key for r in out["repairs"]):
                continue
            out["repairs"].append({
                "part": canon, "raw": name, "position": pos,
                "status": st or "?", "rank": rank, "status_known": bool(st),
            })
            found_here += 1
        if not found_here:
            out["unmatched_parts"].append(f"[{rank}] {body[:60]}")

    # 랭크 행을 아예 못 찾았으면 마크업이 예상과 다르다는 뜻
    if not out["rank_sections"]:
        out["parse_note"] = ("랭크 행(외판 1랭크 등)을 찾지 못했습니다 — "
                             "마크업이 예상과 다릅니다")

    # --- 3) 자동차 세부상태 ---
    seen_detail: set[str] = set()
    for item in _cfg.INSPECTION_DETAIL_ITEMS:
        key = item.replace(" ", "")
        if key in seen_detail:
            continue
        cell = _cell_for([item])
        if cell is None:
            continue
        seen_detail.add(key)
        val, why = _pick_selected(cell, "state")
        if val == "불량":
            out["detail_bad"].append(item)
        elif val is None:
            out["detail_unknown"].append(f"{item}({why})")

    # --- 4) 고전원전기장치 ---
    for canonical, aliases in _cfg.EV_HV_ITEMS.items():
        cell = _cell_for(list(aliases))
        if cell is None:
            out["ev_hv_unknown"].append(f"{canonical}(항목 없음)")
            continue
        val, why = _pick_selected(cell, "state")
        out["ev_hv"][canonical] = {"state": val, "why": why,
                                   "raw": cell.get_text(" ", strip=True)[:40]}
        if val == "불량":
            out["ev_hv_bad"].append(canonical)
        elif val is None:
            out["ev_hv_unknown"].append(f"{canonical}({why})")

    return out


def normalize_inspection_page(parsed: dict) -> dict:
    """파싱 결과를 수집 컬럼으로 평탄화하고 등급 감점을 계산한다."""
    f = parsed.get("fields", {})
    repairs = parsed.get("repairs", [])

    # 상태 부호를 못 읽은 부위는 'R'(방식 미상)로 채점한다.
    gradable = [{"part": r["part"],
                 "status": r["status"] if r.get("status_known") else "R"}
                for r in repairs]
    res = score_repairs(gradable)

    notes, unknown_status = [], []
    for r in repairs:
        st = r["status"] if r.get("status_known") else "R"
        g = next((x for x in res["entries"]
                  if x["part"] == r["part"] and x["status"] == st), None)
        if not g:
            continue
        pos = f"({r['position']})" if r["position"] else ""
        notes.append(f"{r['part']}{pos} {g['status_label']}({st}) — "
                     f"{g['rank_label']}, {g['desc']}")
        if not r.get("status_known"):
            unknown_status.append(f"{r['part']}{pos}")

    ranks_read = len(parsed.get("rank_sections", {}))
    return {
        "page_available": parsed.get("page_available", False),
        "page_ranks_read": ranks_read,
        "page_repair_notes": " | ".join(dict.fromkeys(notes)),
        "page_repair_penalty": res["penalty"] if repairs else (0.0 if ranks_read else ""),
        "page_worst_rank": res["worst_rank"] or "",
        "page_worst_status": res.get("worst_status") or "",
        "page_status_unknown": ", ".join(unknown_status),
        "page_unmatched_parts": ", ".join(parsed.get("unmatched_parts", [])),
        # 상태값들 — None 이면 '' (판정 불가). '불량' 으로 넘기지 않는다.
        "page_mileage_gauge": f.get("mileage_gauge_state") or "",
        "page_mileage": f.get("page_mileage"),
        "page_vin_state": f.get("vin_state") or "",
        "page_tuning": f.get("tuning") or "",
        "page_special_history": f.get("special_history") or "",
        "page_usage_change": f.get("usage_change") or "",
        "page_recall": f.get("recall") or "",
        "page_recall_done": f.get("recall_done") or "",
        "page_accident_history": f.get("accident_history") or "",
        "page_simple_repair": f.get("simple_repair") or "",
        "page_first_registration": f.get("first_registration", ""),
        "page_inspection_valid": f.get("inspection_valid", ""),
        "page_inspector_note": (f.get("inspector_note", "") or "")[:400],
        "page_detail_bad": ", ".join(parsed.get("detail_bad", [])),
        "page_detail_unknown": ", ".join(parsed.get("detail_unknown", [])),
        "page_ev_hv_bad": ", ".join(parsed.get("ev_hv_bad", [])),
        "page_ev_hv_unknown": ", ".join(parsed.get("ev_hv_unknown", [])),
        "page_ev_hv_checked": sum(1 for v in parsed.get("ev_hv", {}).values()
                                  if v.get("state")),
        "page_parse_note": parsed.get("parse_note", ""),
    }


# ---------------------------------------------------------------------------
# 점검자 코멘트에서 수리 부위 뽑기
# ---------------------------------------------------------------------------
def _alias_table() -> list[tuple[str, str]]:
    """(구어체 표현, 표준 부위명) 을 긴 것부터 정렬해서 돌려준다.

    '휀다 뒤쪽'(쿼터패널)이 '휀다'(프론트펜더)보다 먼저 잡혀야 한다.
    """
    import config as _cfg
    pairs = [(a, canon) for canon, aliases in _cfg.COMMENT_PART_ALIASES.items()
             for a in aliases]
    return sorted(pairs, key=lambda t: len(t[0]), reverse=True)


def _action_status(text: str) -> str:
    """코멘트의 수리 방식 단어를 상태 부호로. 없으면 R(방식 미상)."""
    import config as _cfg
    for word, code in _cfg.COMMENT_ACTION_WORDS:
        if word in text:
            return code
    return "R"


def parse_comment_parts(comment: str) -> dict:
    """점검자 코멘트에서 수리 부위를 뽑아 등급을 매긴다.

    성능점검 API 가 부위별 판정값을 싣지 않아서, 실제 수리 부위는
    코멘트 자연어가 유일한 단서다.

        "...내차 피해 1회 (2,693,577원)정비이력 - 뒷문"
        -> 뒷문 -> 도어 -> 외판 1랭크
    """
    import re as _re
    import config as _cfg

    out = {"entries": [], "unmatched": [], "accident_mentions": [],
           "sections": [], "boilerplate_removed": False}
    if not comment or not str(comment).strip():
        return out
    text = str(comment)

    # 정형 면책문구 제거. 모든 매물에 붙는 문장이라 이 차의 수리 사실이 아니다.
    for pat in getattr(_cfg, "COMMENT_BOILERPLATE", []):
        new_text = _re.sub(pat, " ", text)
        if new_text != text:
            out["boilerplate_removed"] = True
            text = new_text

    # 사고 언급: "내차 피해 1회 (2,693,577원)"
    for label in ("내차 피해", "타차 가해", "내차피해", "타차가해"):
        for m in _re.finditer(
                _re.escape(label) + r"\s*(\d+)\s*회[^0-9]{0,4}\(?\s*([\d,]+)\s*원?\)?", text):
            out["accident_mentions"].append({
                "label": label, "count": to_int(m.group(1)),
                "amount": to_int(m.group(2)),
            })

    # 부위가 적히는 구간: '정비이력' 같은 표지 뒤쪽
    segments = []
    for marker in _cfg.COMMENT_SECTION_MARKERS:
        idx = 0
        while True:
            i = text.find(marker, idx)
            if i < 0:
                break
            seg = text[i + len(marker): i + len(marker) + 60]
            segments.append(seg)
            idx = i + len(marker)
    if not segments:
        segments = [text]
    out["sections"] = [sg.strip(" -–—:,.") for sg in segments if sg.strip(" -–—:,.")]

    seen = set()
    for seg in segments:
        # 수리 방식은 그 부위가 적힌 구간 안에서만 읽는다.
        # 코멘트 전체에서 찾으면 다른 문장의 단어를 끌어온다.
        status = _action_status(seg)
        remaining = seg
        for alias, canon in _alias_table():
            if alias not in remaining:
                continue
            if canon in seen:
                remaining = remaining.replace(alias, " ")
                continue
            seen.add(canon)
            out["entries"].append({
                "part": canon, "alias": alias, "status": status,
                "context": seg.strip(" -–—:,.")[:60],
            })
            remaining = remaining.replace(alias, " ")

        # 매칭 안 된 한글 토큰 — 구어체 표현을 늘려가기 위한 보고용
        for tok in _re.split(r"[^가-힣A-Za-z]+", remaining):
            tok = tok.strip()
            if (2 <= len(tok) <= 10 and tok not in _cfg.COMMENT_STOPWORDS
                    and not any(w in tok for w, _ in _cfg.COMMENT_ACTION_WORDS)):
                out["unmatched"].append(tok)
    out["unmatched"] = list(dict.fromkeys(out["unmatched"]))
    return out


def score_comment_parts(comment: str) -> dict:
    """코멘트에서 뽑은 부위를 법정 등급으로 채점한다."""
    parsed = parse_comment_parts(comment)
    entries = [{"part": e["part"], "status": e["status"], "alias": e["alias"],
                "context": e["context"]} for e in parsed["entries"]]
    res = score_repairs(entries)
    # 어떤 구어체 표현에서 왔는지 표기에 남긴다
    by_part = {e["part"]: e["alias"] for e in parsed["entries"]}
    for g in res["entries"]:
        alias = by_part.get(g["part"])
        if alias and alias != g["part"]:
            g["note"] = f"{alias}({g['part']}) " + g["note"].split(" ", 1)[1]
    res["unmatched"] = parsed["unmatched"]
    res["accident_mentions"] = parsed["accident_mentions"]
    res["sections"] = parsed["sections"]
    return res


def normalize_inspection(inspection: Any) -> dict:
    """성능점검 API 응답 (보조 소스)."""
    import config as _cfg
    out = {
        "inspection_available": bool(inspection),
        "leak_note": None,
        "corrosion_note": None,
        "tire_note": None,
        "repair_notes": None,       # 사람이 읽는 수리 부위 설명들
        "repair_penalty": None,     # 등급 기반 감점
        "repair_worst_rank": None,
        "repair_worst_status": None,
        "repair_unclassified": None,
        "repair_source": None,
        "comment_accident_mentions": None,
        "diagnostics": None,
        "battery_pack_damage": None,
        "insp_mileage": None, "insp_waterlog": None, "insp_recall": None,
        "insp_recall_types": None, "insp_comments": None, "insp_tuning": None,
        "insp_usage_change": None, "insp_serious": None, "insp_vin": None,
        "insp_accident_flag": None, "insp_simple_repair": None,
        "insp_needs_repair": None,
    }
    if not inspection:
        return out

    # 정보가 값이 아니라 '키 이름' 에 들어 있는 경우가 있다.
    #   {"engineOilLeak": "없음"}  ->  값만 보면 '없음' 이라 누유 항목인 줄 모른다.
    # 그래서 키와 값을 함께 훑는다.
    pairs: list[tuple[str, str]] = []

    def _walk_pairs(obj, key=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _walk_pairs(v, k)
        elif isinstance(obj, list):
            for v in obj:
                _walk_pairs(v, key)
        elif obj is not None and str(obj).strip():
            pairs.append((key, str(obj).strip()))

    _walk_pairs(inspection)

    def _near(words: tuple[str, ...], label: str) -> str | None:
        hits = []
        for k, v in pairs:
            kv = f"{k} {v}".lower()
            if not any(w.lower() in kv for w in words):
                continue
            if len(v) > 120:
                continue
            # 키에서만 걸렸으면 무슨 항목인지 라벨을 붙여 준다
            hits.append(v if any(w in v for w in words if not w.isascii())
                        else f"{label} {v}")
        return " / ".join(dict.fromkeys(hits))[:200] or None

    out["leak_note"] = _near(("누유", "누수", "oilleak", "leak"), "누유:")
    out["corrosion_note"] = _near(("부식", "corrosion", "rust"), "부식:")
    out["tire_note"] = _near(("타이어", "트레드", "마모", "tire", "tread"), "타이어:")

    # [2] master 하위의 유용한 필드들
    md = pick(inspection, "master.detail", "detail", default={}) or {}
    out["insp_mileage"] = to_int(pick(md, "mileage", "km"))
    wl = pick(md, "waterlog", "waterLog", "flooding")
    out["insp_waterlog"] = None if wl is None else bool(wl)
    out["insp_recall"] = pick(md, "recall", "recallYn")
    out["insp_recall_types"] = ", ".join(strings_of(
        pick(md, "recallFullFillTypes", "recallTypes", default=[]))) or None
    out["insp_comments"] = " / ".join(strings_of(
        pick(md, "comments", "comment", default=[])))[:400] or None
    out["insp_tuning"] = ", ".join(strings_of(pick(md, "tuning", default=[]))) or None
    out["insp_usage_change"] = ", ".join(strings_of(
        pick(md, "usageChangeTypes", default=[]))) or None
    out["insp_serious"] = ", ".join(strings_of(
        pick(md, "seriousTypes", default=[]))) or None
    out["insp_vin"] = pick(md, "vin", "vinNo")
    acc_flag = pick(inspection, "master.accdient", "master.accident", "accdient")
    out["insp_accident_flag"] = None if acc_flag is None else bool(acc_flag)
    sr = pick(inspection, "master.simpleRepair", "simpleRepair")
    out["insp_simple_repair"] = None if sr is None else bool(sr)

    # '수리필요' 로 분류된 항목들 — 지금 손봐야 하는 부분이라 중요하다
    need = []
    for sec in ("etcs", "inners", "outers"):
        for item in (pick(inspection, sec) or []):
            if not isinstance(item, dict):
                continue
            if "수리" not in (_title_of(item) or ""):
                continue
            for ch in (item.get("children") or []):
                nm = _title_of(ch) if isinstance(ch, dict) else None
                st = _status_of(ch) if isinstance(ch, dict) else None
                if nm:
                    need.append(f"{nm}({_status_label(st)})" if st else nm)
    out["insp_needs_repair"] = ", ".join(dict.fromkeys(need)) or None

    # 부위별 등급은 '점검자 코멘트' 에서 뽑는다.
    #
    # 성능점검 API 의 outers 는 비어 있고 inners/etcs 항목은 status 가 '-' 로
    # 와서 판정값이 실리지 않는다. 트리로는 부위 등급을 매길 수 없다.
    # inners 는 애초에 차체 부위가 아니라 자기진단(원동기·변속기) 항목이다.
    # 그래서 트리 기반 채점과 그로 인한 미분류 경고는 쓰지 않는다.
    tree = score_repairs(find_repair_entries(inspection))
    out["diagnostics"] = " | ".join(dict.fromkeys(tree.get("diagnostics", []))) or None

    # 코멘트 파싱은 보조 수단이다. 성능기록부 페이지가 부위별 랭크를 정확히
    # 주므로 페이지를 읽었으면 코멘트는 쓰지 않는다. 딜러 자유 기술이라
    # 면책문구에서 엉뚱한 단어를 부위로 잡는 일이 잦다.
    if not _cfg.USE_COMMENT_FALLBACK:
        out["repair_notes"] = None
        out["repair_penalty"] = None
        out["repair_worst_rank"] = None
        out["repair_worst_status"] = None
        out["repair_source"] = None
        out["repair_unclassified"] = None
        out["comment_accident_mentions"] = None
        out["battery_pack_damage"] = None
        return out

    res = score_comment_parts(out.get("insp_comments") or "")
    out["repair_notes"] = " | ".join(g["note"] for g in res["entries"]) or None
    out["repair_penalty"] = res["penalty"]
    out["repair_worst_rank"] = res["worst_rank"]
    out["repair_worst_status"] = res.get("worst_status")
    out["repair_source"] = "점검자 코멘트" if res["entries"] else None
    out["repair_unclassified"] = ", ".join(res.get("unmatched", [])) or None
    out["comment_accident_mentions"] = res.get("accident_mentions") or None
    out["battery_pack_damage"] = any(g["rank"] == "배터리팩" for g in res["entries"])
    return out


def inspection_candidates(vid: str) -> list[tuple[str, str, dict | None]]:
    """성능점검 정보가 있을 만한 경로 후보. probe 가 하나씩 시험한다."""
    base = "https://api.encar.com/v1/readside"
    d = DETAIL_URL.format(vid=vid)
    return [
        ("inspection/vehicle", f"{base}/inspection/vehicle/{vid}", None),
        ("vehicle/inspection", f"{d}/inspection", None),
        ("vehicleInspection", f"{base}/vehicleInspection/{vid}", None),
        ("inspection/{id}", f"{base}/inspection/{vid}", None),
        ("performance", f"{d}/performance", None),
        ("record/inspection", f"{base}/record/vehicle/{vid}/inspection", None),
        ("diagnosis/vehicle", f"{base}/diagnosis/vehicle/{vid}", None),
        ("include=INSPECTION", d, {"include": "INSPECTION,DIAGNOSIS,CONDITION"}),
        ("extend/inspection", f"{base}/extend/inspection/{vid}", None),
    ]


# ---------------------------------------------------------------------------
# 상세 응답 계측 — 옵션 배열이 어디 있는지 찾아내기
# ---------------------------------------------------------------------------
def find_arrays(obj: Any, prefix: str = "", out: list | None = None,
                depth: int = 0, max_depth: int = 7) -> list[dict]:
    """중첩 구조 안의 모든 배열을 경로/길이/원소타입/샘플과 함께 찾는다.

    옵션 목록이 응답 어디에 숨어 있는지 모를 때 쓰는 진단용 도구.
    """
    out = out if out is not None else []
    if depth > max_depth:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            find_arrays(v, f"{prefix}.{k}" if prefix else k, out, depth + 1, max_depth)
    elif isinstance(obj, list):
        kinds = sorted({type(x).__name__ for x in obj})
        out.append({
            "path": prefix or "(root)",
            "len": len(obj),
            "kinds": kinds,
            "sample": obj[:6],
            "ref": obj,          # 변환표를 만들 때 전체가 필요하다
        })
        for i, v in enumerate(obj[:3]):
            if isinstance(v, (dict, list)):
                find_arrays(v, f"{prefix}[{i}]", out, depth + 1, max_depth)
    return out


def looks_like_option_codes(arr: list, min_len: int = 1) -> bool:
    """숫자 코드 배열로 보이는가 (엔카는 옵션을 코드로 내려주기도 한다)."""
    if not arr or len(arr) < min_len:
        return False
    return all(isinstance(x, int) or (isinstance(x, str) and x.isdigit()) for x in arr)


def looks_like_option_names(arr: list, min_len: int = 1) -> bool:
    """사람이 읽는 옵션명 배열로 보이는가.

    min_len 은 자동 탐색처럼 오탐이 위험한 곳에서만 크게 잡는다.
    알려진 옵션 경로에서는 원소가 하나뿐인 배열도 정상이다.
    """
    if len(arr) < min_len or not arr or not all(isinstance(x, str) for x in arr):
        return False

    # 숫자 코드 배열('001','002')을 이름으로 오인하면 안 된다.
    # '001'.isupper() 는 False 라서 아래 대문자 검사만으로는 걸러지지 않는다.
    # 이걸 놓치면 옵션을 '못 읽은' 상태가 '이름에 없다' 로 둔갑해
    # 에어서스가 달린 매물이 조용히 탈락한다.
    if looks_like_option_codes(arr):
        return False

    lens = [len(x) for x in arr]
    if not (1 <= sum(lens) / len(lens) <= 40):
        return False
    # 값이 전부 대문자 코드('CAR','Y','N')뿐이면 옵션명이 아니다
    return not all(x.isascii() and x.isupper() for x in arr)


def load_local_option_map() -> dict[str, str]:
    """data/option_codes.json 을 읽어 코드→이름 표로 만든다.

    변환표 API 를 못 찾았을 때 사용자가 직접 채워 넣을 수 있는 탈출구.
    두 형식을 모두 받는다:
        {"001": "에어서스펜션", "002": "통풍시트"}
        {"options": [{"code": "001", "name": "에어서스펜션"}, ...]}
    """
    from common import OPTION_MAP_JSON, read_json, log

    data = read_json(OPTION_MAP_JSON)
    if not data:
        return {}
    if isinstance(data, dict) and all(isinstance(v, str) for v in data.values()):
        out = {str(k): v for k, v in data.items()}
    else:
        out = build_code_map(data)
    if out:
        log(f"옵션 코드 변환표를 파일에서 읽었습니다: {OPTION_MAP_JSON} ({len(out)}개)")
    return out


CODE_KEYS = ("code", "cd", "id", "value", "optioncode")
NAME_KEYS = ("name", "nm", "title", "label", "text", "optionname")


def looks_like_code_map(arr: list) -> bool:
    """[{"code":1,"name":"에어서스펜션"}, ...] 형태의 코드→이름 표인가."""
    head = [x for x in arr[:6] if isinstance(x, dict)]
    if len(head) < 2 or len(head) != len(arr[:6]):
        return False
    keys = {k.lower() for k in head[0]}
    has_code = any(any(c in k for c in CODE_KEYS) for k in keys)
    has_name = any(any(n in k for n in NAME_KEYS) for k in keys)
    return has_code and has_name


def build_code_map(payload: Any) -> dict[str, str]:
    """응답 어디에 있든 옵션 코드→이름 표를 찾아 만든다."""
    out: dict[str, str] = {}
    for arr in find_arrays(payload):
        items = arr.get("ref") or []
        if not looks_like_code_map(items):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            code = name = None
            for k, v in it.items():
                kl = k.lower()
                if code is None and any(c in kl for c in CODE_KEYS) \
                        and isinstance(v, (int, str)):
                    code = str(v)
                elif name is None and any(n in kl for n in NAME_KEYS) \
                        and isinstance(v, str) and v.strip():
                    name = v.strip()
            if code and name:
                out.setdefault(code, name)
        if out:
            break
    return out


# 옵션이 들어 있을 만한 경로들 (앞쪽이 우선순위)
OPTION_NAME_PATHS = (
    "options.standardNames", "options.names", "optionNames",
    "options.standard", "options.choice", "options.etc", "options.tuning",
    "optionList", "optionItems", "option.list", "spec.options",
)
OPTION_CODE_PATHS = (
    "options.standard", "options.choice", "options.etc", "options.tuning",
    "optionCodes", "options.codes",
)


def extract_options(detail: Any,
                    code_map: dict[str, str] | None = None) -> tuple[list[str], list[str], str]:
    """상세 응답에서 옵션을 뽑는다.

    반환: (옵션명 목록, 옵션코드 목록, 어디서 찾았는지 설명)

    엔카는 옵션을 숫자 코드 배열로 내려주는 경우가 있다. 그때는 이름을
    알 수 없으므로 코드만 담고, 호출부가 그 사실을 알 수 있게 표시한다.
    """
    if not isinstance(detail, dict):
        return [], [], "상세 응답 없음"

    names: list[str] = []
    codes: list[str] = []
    sources: list[str] = []

    # 1) 알려진 경로 우선
    for path in OPTION_NAME_PATHS:
        v = pick(detail, path)
        if isinstance(v, list) and looks_like_option_names(v):
            for x in v:
                if x.strip() and x.strip() not in names:
                    names.append(x.strip())
            sources.append(path)
    for path in OPTION_CODE_PATHS:
        v = pick(detail, path)
        if isinstance(v, list) and looks_like_option_codes(v):
            for x in v:
                if str(x) not in codes:
                    codes.append(str(x))
            if path not in sources:
                sources.append(path + "(코드)")

    # 2) 못 찾았으면 응답 전체에서 옵션처럼 생긴 배열을 뒤진다
    if not names:
        for arr in find_arrays(detail):
            if arr["len"] >= 3 and looks_like_option_names(arr["sample"], min_len=3):
                for x in arr["sample"]:
                    if x.strip() and x.strip() not in names:
                        names.append(x.strip())
                sources.append(arr["path"] + "(자동탐색)")
                break

    # 3) 코드는 변환표로 푼다.
    #    이름이 이미 일부 있어도(딜러가 etc 에 적어 둔 것) 반드시 함께 푼다.
    #    'names 가 비었을 때만' 풀면, etc 에 '스마트키' 한 줄만 있어도
    #    standard 의 코드 전체가 해석되지 않은 채 남는다.
    if codes and code_map:
        resolved = [code_map[c] for c in codes if c in code_map]
        added = 0
        for r in resolved:
            if r not in names:
                names.append(r)
                added += 1
        if added:
            sources.append(f"코드→이름 변환 {len(resolved)}/{len(codes)}건")

    src = ", ".join(sources) if sources else "찾지 못함"
    return names, codes, src


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
                     diagnosis: Any, target: dict,
                     code_map: dict[str, str] | None = None,
                     inspection_html: str | None = None) -> dict:
    """상세/이력/성능점검 응답을 하나의 평탄한 행으로."""
    strs = strings_of(detail, record, inspection, diagnosis)
    hay_flat = " \n ".join(strs).lower().replace(" ", "")

    # 차량번호 — 상세 응답 최상단에 노출됨
    plate = pick(detail or {}, "vehicleNo", "VehicleNo", "carNo", "CarNo",
                 "vehicle.vehicleNo", "manage.vehicleNo", default="")

    # 엔카 옵션은 수집하지 않는다.
    # 엔카 옵션 목록에는 에어서스펜션·후륜조향 항목 자체가 없고(옵션 페이지에서
    # 직접 확인), 나머지도 숫자 코드로만 와서 쓸 수 없다. 옵션 확인은
    # 헤이딜러 숨은이력 전담이다.
    options: list[str] = []

    # 사고/이력 플래그 — 부정어 가드를 거쳐 문자열 단위로 판정
    flood = _flagged(strs, FLOOD_WORDS)
    rental = _flagged(strs, RENTAL_WORDS)


    # 성능점검 요약: 사람이 읽을 짧은 문장으로
    insp_summary = ""
    if inspection:
        bits = []
        for key in ("accidentHistory", "simpleRepair", "specialHistory", "comment", "summary"):
            v = pick(inspection, key)
            if isinstance(v, str) and v.strip():
                bits.append(v.strip())
        insp_summary = " / ".join(bits)[:300]

    rec = normalize_record(record)

    # 성능기록부 HTML 페이지가 주 소스. API 는 보조.
    # 페이지를 '먼저' 읽어야 코멘트 폴백을 쓸지 말지 정할 수 있다.
    page = (normalize_inspection_page(parse_inspection_page(inspection_html))
            if inspection_html else {})

    import config as _cfg2
    _saved = _cfg2.USE_COMMENT_FALLBACK
    if page.get("page_ranks_read"):
        # 페이지에서 랭크를 읽었으면 코멘트 파싱은 쓰지 않는다.
        _cfg2.USE_COMMENT_FALLBACK = False
    try:
        insp = normalize_inspection(inspection)
    finally:
        _cfg2.USE_COMMENT_FALLBACK = _saved

    _acc_lines, _acc_verdict = describe_accidents(rec)

    owner_changes = rec["owner_change_count"]
    my_acc = rec["my_accident_count"]
    other_acc = rec["other_accident_count"]
    my_cost = rec["my_accident_cost"]

    # 사고 여부는 보험이력 건수로만 판정한다.
    # 응답에 건수가 없으면 '무사고' 라고 단정하지 않는다 — 딜러가 쓴
    # '무사고' 문구는 근거가 되지 못한다. 모르면 모른다고 둔다.
    if my_acc is None and other_acc is None:
        accident_free = None          # 알 수 없음
    else:
        accident_free = not ((my_acc or 0) or (other_acc or 0))
    if flood or (rec["flood_total_count"] or 0) or (rec["total_loss_count"] or 0):
        accident_free = False

    one_owner = (owner_changes <= 1) if owner_changes is not None else None

    # 과거 용도 이력 — 지금 일반 매물이어도 과거에 대여/영업용이었을 수 있다
    past_use_counts = [rec["rental_use_count"], rec["business_use_count"],
                       rec["government_use_count"]]
    if all(v is None for v in past_use_counts):
        past_commercial_use = None
    else:
        past_commercial_use = any((v or 0) > 0 for v in past_use_counts)

    # 최초등록일 — 배터리 보증 8년의 기준일. 연식(yyyyMM)과 몇 달씩 다르다.
    first_reg = pick(detail or {}, "category.firstRegistrationDate",
                     "firstRegistrationDate", "spec.firstRegistrationDate",
                     "manage.firstRegistrationDate", "category.firstAdvertisedDate")
    first_reg = first_reg or rec["first_registration"]

    # 실제 응답에서 확인된 유용한 필드들
    origin_price = to_int(pick(detail or {}, "category.originPrice", "originPrice"))
    warranty = pick(detail or {}, "category.warranty", "warranty", default="")
    if isinstance(warranty, (dict, list)):
        warranty = " ".join(str(x) for x in _walk_strings(warranty))[:200]
    view_count = to_int(pick(detail or {}, "manage.viewCount", "viewCount"))
    subscribe_count = to_int(pick(detail or {}, "manage.subscribeCount", "subscribeCount"))
    encar_check = pick(detail or {}, "advertisement.encarCheck", "encarCheck")
    direct_inspected = pick(detail or {}, "advertisement.directInspected", "directInspected")

    def _yes(v) -> bool:
        return str(v).strip().lower() in ("y", "true", "1", "yes")

    # 엔카진단은 별도 endpoint 보다 광고 필드가 더 신뢰할 만하다
    diagnosed = bool(diagnosis) or _yes(encar_check) or _yes(direct_inspected)

    return {
        "vehicle_id": vid,
        "plate_no": plate or "",
        "origin_price_manwon": origin_price if origin_price is not None else "",
        "warranty": str(warranty)[:200] if warranty else "",
        "view_count": view_count if view_count is not None else "",
        "subscribe_count": subscribe_count if subscribe_count is not None else "",
        "encar_check": _yes(encar_check),
        "direct_inspected": _yes(direct_inspected),
        # 보험이력 — 값이 없으면 "" (응답에 없음). 0 으로 채우지 않는다.
        "record_available": rec["record_available"],
        "record_fields_found": ", ".join(rec["record_fields_found"]),
        "accident_free": "" if accident_free is None else accident_free,
        "accident_my_count": _blank(my_acc),
        "accident_other_count": _blank(other_acc),
        "accident_count": _blank(rec["accident_count"]),
        "accident_my_cost_won": _blank(my_cost),
        "accident_other_cost_won": _blank(rec["other_accident_cost"]),
        "owner_change_count": _blank(owner_changes),
        "plate_change_count": _blank(rec["plate_change_count"]),
        "record_fields_null": ", ".join(rec.get("record_fields_null", [])),
        "use_history": " | ".join(rec.get("use_history", [])),
        "total_loss_count": _blank(rec["total_loss_count"]),
        "flood_total_count": _blank(rec["flood_total_count"]),
        "flood_part_count": _blank(rec["flood_part_count"]),
        "theft_count": _blank(rec["theft_count"]),
        # 과거 용도 이력
        "past_rental_count": _blank(rec["rental_use_count"]),
        "past_business_count": _blank(rec["business_use_count"]),
        "past_government_count": _blank(rec["government_use_count"]),
        "past_commercial_use": "" if past_commercial_use is None else past_commercial_use,
        # 성능점검
        "inspection_available": insp["inspection_available"],
        "insp_leak": insp["leak_note"] or "",
        "insp_corrosion": insp["corrosion_note"] or "",
        "insp_tire": insp["tire_note"] or "",
        "insp_repair_notes": (page.get("page_repair_notes")
                              or insp["repair_notes"] or ""),
        "insp_repair_penalty": _blank(
            page.get("page_repair_penalty") if page.get("page_repair_notes")
            else insp["repair_penalty"]),
        **page,
        # 부위 등급은 성능기록부 페이지를 우선하고, 없으면 코멘트 파싱으로.
        "repair_grade_source": ("성능기록부" if page.get("page_repair_notes")
                                else ("점검자 코멘트" if insp["repair_notes"] else "")),
        "insp_worst_rank": (page.get("page_worst_rank")
                            or insp["repair_worst_rank"] or ""),
        "insp_worst_status": (page.get("page_worst_status")
                              or insp.get("repair_worst_status") or ""),
        "insp_unclassified": insp["repair_unclassified"] or "",
        "battery_pack_damage": _blank(insp["battery_pack_damage"]),
        "insp_diagnostics": insp.get("diagnostics") or "",
        "insp_mileage": _blank(insp.get("insp_mileage")),
        "insp_waterlog": _blank(insp.get("insp_waterlog")),
        "insp_recall": _blank(insp.get("insp_recall")),
        "insp_recall_types": insp.get("insp_recall_types") or "",
        "insp_comments": insp.get("insp_comments") or "",
        "insp_tuning": insp.get("insp_tuning") or "",
        "insp_usage_change": insp.get("insp_usage_change") or "",
        "insp_serious": insp.get("insp_serious") or "",
        "insp_vin": insp.get("insp_vin") or "",
        "insp_accident_flag": _blank(insp.get("insp_accident_flag")),
        "insp_simple_repair": _blank(insp.get("insp_simple_repair")),
        "insp_needs_repair": insp.get("insp_needs_repair") or "",
        "repair_source": insp.get("repair_source") or "",
        "comment_accident_amount": _blank(
            (insp.get("comment_accident_mentions") or [{}])[0].get("amount")
            if insp.get("comment_accident_mentions") else None),
        "accident_lines": " | ".join(_acc_lines),
        "accident_type_verdict": _acc_verdict,
        # 최초등록일 (배터리 보증 기준일)
        "first_registration_date": str(
            page.get("page_first_registration") or first_reg or ""),
        "flood_or_total_loss": flood or bool((rec["flood_total_count"] or 0)),
        "rental_or_commercial": rental,
        "one_owner": "" if one_owner is None else one_owner,
        "encar_diagnosed": diagnosed,
        "inspection_summary": insp_summary,
        "detail_ok": detail is not None,
    }
