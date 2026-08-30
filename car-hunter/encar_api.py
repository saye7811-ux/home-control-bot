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
                    "paint_cost": to_int(pick(a, "paintCost", "paintAmount")),
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
    """dict 안에서 상태 부호(X/W/A/U/C/T)를 찾는다."""
    import config as _cfg
    for k, v in d.items():
        if not any(sk in k.lower() for sk in STATUS_KEYS):
            continue
        if isinstance(v, str) and v.strip().upper() in _cfg.INSPECTION_STATUS:
            return v.strip().upper()
        if isinstance(v, dict):
            inner = _status_of(v)
            if inner:
                return inner
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


def find_repair_entries(inspection: Any) -> list[dict]:
    """성능점검 응답에서 (부위, 상태부호) 쌍을 모두 찾아낸다.

    응답 구조를 모르므로 중첩 구조 전체를 훑으면서 '부위명 같은 값' 과
    '상태 부호 같은 값' 을 함께 가진 dict 를 수리 기록으로 본다.
    """
    out: list[dict] = []
    seen: set[tuple] = set()

    def walk(obj, path=""):
        if isinstance(obj, dict):
            part = _part_of(obj)
            status = _status_of(obj)
            if part and status:
                key = (part, status)
                if key not in seen:
                    seen.add(key)
                    out.append({"part": part, "status": status, "path": path})
            for k, v in obj.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")

    walk(inspection)
    return out


def score_repairs(entries: list[dict]) -> dict:
    """수리 기록을 법정 등급으로 분류하고 감점을 계산한다."""
    import config as _cfg

    graded, unclassified = [], []
    for e in entries:
        rank = classify_part(e["part"])
        if rank is None:
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
        "unclassified": unclassified,
        "penalty": round(total, 1),
        "worst_rank": worst["rank"] if worst else None,
        "worst_note": worst["note"] if worst else None,
    }


def normalize_inspection(inspection: Any) -> dict:
    """성능점검 응답에서 누유 / 부식 / 타이어 / 수리 부위를 뽑는다."""
    out = {
        "inspection_available": bool(inspection),
        "leak_note": None,
        "corrosion_note": None,
        "tire_note": None,
        "repair_notes": None,       # 사람이 읽는 수리 부위 설명들
        "repair_penalty": None,     # 등급 기반 감점
        "repair_worst_rank": None,
        "repair_unclassified": None,
        "battery_pack_damage": None,
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

    res = score_repairs(find_repair_entries(inspection))
    out["repair_notes"] = " | ".join(g["note"] for g in res["entries"]) or None
    out["repair_penalty"] = res["penalty"]
    out["repair_worst_rank"] = res["worst_rank"]
    out["repair_unclassified"] = ", ".join(dict.fromkeys(res["unclassified"])) or None
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
                     code_map: dict[str, str] | None = None) -> dict:
    """상세/이력/성능점검 응답을 하나의 평탄한 행으로."""
    strs = strings_of(detail, record, inspection, diagnosis)
    hay_flat = " \n ".join(strs).lower().replace(" ", "")

    # 차량번호 — 상세 응답 최상단에 노출됨
    plate = pick(detail or {}, "vehicleNo", "VehicleNo", "carNo", "CarNo",
                 "vehicle.vehicleNo", "manage.vehicleNo", default="")

    # 옵션 목록. 엔카는 옵션명 대신 숫자 코드를 내려주기도 하므로
    # 이름/코드를 구분해서 담고, 어디서 찾았는지도 남긴다.
    options, option_codes, option_source = extract_options(detail, code_map)

    # 사고/이력 플래그 — 부정어 가드를 거쳐 문자열 단위로 판정
    flood = _flagged(strs, FLOOD_WORDS)
    rental = _flagged(strs, RENTAL_WORDS)

    # 에어서스 키워드: 설정 키워드가 아니라 '실제로 매칭된 옵션명'을 남긴다.
    kws = [k.lower().replace(" ", "") for k in target.get("airsus_keywords", [])]
    airsus_hits: list[str] = []
    for s in (options or strs):
        flat = s.lower().replace(" ", "")
        if len(s) <= 60 and any(k in flat for k in kws) and s not in airsus_hits:
            airsus_hits.append(s.strip())
    if not airsus_hits and any(k in hay_flat for k in kws):
        airsus_hits.append("(옵션 목록 외 텍스트에서 키워드 발견)")

    # 엔카 쪽 에어서스 정보는 '판정' 이 아니라 '판매자가 뭐라고 썼는가' 일 뿐이다.
    #
    # 옵션 목록과 설명글은 딜러가 홍보용으로 쓴 자유 텍스트다. 없는데 적어
    # 두거나 있는데 안 적는 일이 흔하고, 실제로 "코드에 없으니 미장착" 으로
    # 본 매물의 설명에 "에어서스펜션 옵션적용차량" 이 적혀 있던 반례가 있었다.
    # 그래서 여기서는 결론을 내지 않고 언급 여부만 남긴다.
    # 실제 판정은 3단계(헤이딜러 출고 기록)에서 한다.
    unresolved = [c for c in option_codes if not code_map or c not in code_map]
    airsus_status = ("판매자 설명에 언급 있음" if airsus_hits
                     else "판매자 설명에 언급 없음")

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
    insp = normalize_inspection(inspection)

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

    # 주요 옵션 언급 여부 — 에어서스와 같은 이유로 점수에는 반영하지 않는다
    import config as _cfg
    option_mentions = {}
    for label, kws in getattr(_cfg, "OPTION_MENTION_KEYWORDS", {}).items():
        flat_kws = [k.lower().replace(" ", "") for k in kws]
        found = [o for o in options
                 if any(k in o.lower().replace(" ", "") for k in flat_kws)]
        if not found and any(k in hay_flat for k in flat_kws):
            found = ["(설명글에서 발견)"]
        option_mentions[f"opt_{label}"] = ", ".join(found)

    return {
        "vehicle_id": vid,
        "plate_no": plate or "",
        **option_mentions,
        "options": " | ".join(options[:80]),
        "options_count": len(options),
        "option_codes": ",".join(option_codes[:120]),
        "option_codes_unresolved": len(unresolved),
        "option_source": option_source,
        "origin_price_manwon": origin_price if origin_price is not None else "",
        "warranty": str(warranty)[:200] if warranty else "",
        "view_count": view_count if view_count is not None else "",
        "subscribe_count": subscribe_count if subscribe_count is not None else "",
        "encar_check": _yes(encar_check),
        "direct_inspected": _yes(direct_inspected),
        "airsus_status": airsus_status,
        "seller_airsus_mention": bool(airsus_hits),
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
        "insp_repair_notes": insp["repair_notes"] or "",
        "insp_repair_penalty": _blank(insp["repair_penalty"]),
        "insp_worst_rank": insp["repair_worst_rank"] or "",
        "insp_unclassified": insp["repair_unclassified"] or "",
        "battery_pack_damage": _blank(insp["battery_pack_damage"]),
        # 최초등록일 (배터리 보증 기준일)
        "first_registration_date": str(first_reg) if first_reg else "",
        "flood_or_total_loss": flood or bool((rec["flood_total_count"] or 0)),
        "rental_or_commercial": rental,
        "one_owner": "" if one_owner is None else one_owner,
        "encar_diagnosed": diagnosed,
        "airsus_keyword_hits": ", ".join(airsus_hits),
        "has_airsus_keyword": bool(airsus_hits),
        "inspection_summary": insp_summary,
        "detail_ok": detail is not None,
    }
