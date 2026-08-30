"""실행 이력 — 날짜별 보관, 실행 간 비교, 시세 추이.

몇 달에 걸쳐 매주 돌리며 지켜보는 용도다. 한 번 찍은 값만으로는
'싸다' 를 말할 수 없고, 같은 매물이 몇 주째 안 팔리는지 / 가격이
내렸는지 / 시장 전체가 빠지는지를 봐야 판단이 선다.

보관 구조
    data/history/YYYY-MM-DD/listings.csv    그날 수집분 전체
    data/history/YYYY-MM-DD/market.json     그날의 시세선 계수
    data/history/index.json                 매물별 최초 등장 · 가격 이력

index.json 이 핵심이다. 스냅샷만 있으면 매번 전부 훑어야 하지만,
여기에 매물별 요약을 누적해 두면 '언제 처음 봤는지', '가격이 몇 번
내렸는지' 를 바로 읽을 수 있다.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

from common import DATA_DIR, log, read_csv, write_csv, write_json

HISTORY_DIR = os.path.join(DATA_DIR, "history")
INDEX_JSON = os.path.join(HISTORY_DIR, "index.json")


def _today() -> str:
    return date.today().isoformat()


def _to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _key(row: dict) -> str:
    """매물을 무엇으로 같다고 볼 것인가.

    차량번호가 있으면 그것을 쓴다. 딜러가 같은 차를 새 매물번호로 다시
    올려도 같은 차로 이어서 추적해야 '몇 주째 안 팔리는 차' 가 보인다.
    차량번호를 못 받았으면 매물번호로 대신한다.
    """
    plate = str(row.get("plate_no") or "").strip()
    return plate or f"vid:{row.get('vehicle_id')}"


def load_index() -> dict:
    if not os.path.exists(INDEX_JSON):
        return {"listings": {}, "runs": []}
    try:
        with open(INDEX_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"listings": {}, "runs": []}
    data.setdefault("listings", {})
    data.setdefault("runs", [])
    return data


def snapshot(rows: list[dict], fields: list[str], markets: dict | None = None,
             run_date: str | None = None) -> str:
    """오늘 수집분을 날짜 폴더에 보관하고 index 를 갱신한다."""
    run_date = run_date or _today()
    day_dir = os.path.join(HISTORY_DIR, run_date)
    os.makedirs(day_dir, exist_ok=True)

    write_csv(os.path.join(day_dir, "listings.csv"), rows, fields)
    if markets:
        write_json(os.path.join(day_dir, "market.json"), markets)

    idx = load_index()
    seen_now = set()
    for r in rows:
        k = _key(r)
        seen_now.add(k)
        price = _to_int(r.get("price_manwon"))
        e = idx["listings"].get(k)
        if e is None:
            e = {"first_seen": run_date, "plate_no": r.get("plate_no", ""),
                 "model_key": r.get("model_key", ""), "prices": [],
                 "vehicle_ids": []}
            idx["listings"][k] = e
        e["last_seen"] = run_date
        e["model_key"] = r.get("model_key") or e.get("model_key", "")
        e["plate_no"] = r.get("plate_no") or e.get("plate_no", "")
        vid = str(r.get("vehicle_id") or "")
        if vid and vid not in e["vehicle_ids"]:
            e["vehicle_ids"].append(vid)
        # 가격은 '바뀔 때만' 쌓는다. 매주 같은 값을 쌓으면 이력이 길어지기만
        # 하고 '언제 내렸나' 를 읽기 어려워진다.
        if price is not None and (not e["prices"] or e["prices"][-1][1] != price):
            e["prices"].append([run_date, price])

    idx["runs"] = [x for x in idx["runs"] if x.get("date") != run_date]
    idx["runs"].append({"date": run_date, "n": len(rows),
                        "markets": markets or {}})
    idx["runs"].sort(key=lambda x: x["date"])
    write_json(INDEX_JSON, idx)
    log(f"이력 보관: {day_dir} ({len(rows)}건), 누적 {len(idx['listings'])}대 "
        f"/ {len(idx['runs'])}회 실행")
    return run_date


def record_markets(markets: dict, run_date: str | None = None) -> None:
    """그날의 시세선 계수를 이력에 남긴다.

    수집(collect)과 채점(score)이 분리돼 있어서, 시세선은 채점 때 나온다.
    같은 날짜의 실행 기록에 덧붙인다.
    """
    run_date = run_date or _today()
    day_dir = os.path.join(HISTORY_DIR, run_date)
    os.makedirs(day_dir, exist_ok=True)
    write_json(os.path.join(day_dir, "market.json"), markets)

    idx = load_index()
    for run in idx.get("runs", []):
        if run.get("date") == run_date:
            run["markets"] = markets
            break
    else:
        idx.setdefault("runs", []).append({"date": run_date, "n": 0,
                                           "markets": markets})
        idx["runs"].sort(key=lambda x: x["date"])
    write_json(INDEX_JSON, idx)


def previous_run(before: str | None = None) -> tuple[str, list[dict]] | None:
    """직전 실행의 날짜와 그때의 매물 목록."""
    if not os.path.isdir(HISTORY_DIR):
        return None
    days = sorted(d for d in os.listdir(HISTORY_DIR)
                  if os.path.isdir(os.path.join(HISTORY_DIR, d))
                  and (before is None or d < before))
    for d in reversed(days):
        path = os.path.join(HISTORY_DIR, d, "listings.csv")
        if os.path.exists(path):
            return d, read_csv(path)
    return None


def diff_runs(current: list[dict], run_date: str | None = None) -> dict:
    """직전 실행과 비교해 신규 / 가격변동 / 사라진 매물을 낸다."""
    run_date = run_date or _today()
    prev = previous_run(before=run_date)
    out = {"prev_date": "", "new": [], "price_down": [], "price_up": [],
           "gone": [], "unchanged": 0, "has_prev": False}
    if prev is None:
        return out

    prev_date, prev_rows = prev
    out["prev_date"] = prev_date
    out["has_prev"] = True
    prev_map = {_key(r): r for r in prev_rows}
    cur_map = {_key(r): r for r in current}

    for k, r in cur_map.items():
        old = prev_map.get(k)
        if old is None:
            out["new"].append(r)
            continue
        p_new, p_old = _to_int(r.get("price_manwon")), _to_int(old.get("price_manwon"))
        if p_new is None or p_old is None or p_new == p_old:
            out["unchanged"] += 1
            continue
        r = dict(r)
        r["price_prev_manwon"] = p_old
        r["price_change_manwon"] = p_new - p_old
        (out["price_down"] if p_new < p_old else out["price_up"]).append(r)

    for k, r in prev_map.items():
        if k not in cur_map:
            out["gone"].append(r)

    out["price_down"].sort(key=lambda r: r["price_change_manwon"])
    out["price_up"].sort(key=lambda r: -r["price_change_manwon"])
    return out


def annotate(rows: list[dict], run_date: str | None = None) -> list[dict]:
    """매물마다 이력에서 온 값을 채운다.

    days_on_market  — 딜러가 며칠째 들고 있는가
    price_first / price_change — 처음 값 대비 얼마나 내렸는가
    """
    run_date = run_date or _today()
    idx = load_index()
    today = datetime.fromisoformat(run_date).date()

    for r in rows:
        e = idx["listings"].get(_key(r))
        # 엔카가 주는 최초 광고일이 가장 정확하다. 없으면 우리가 처음 본 날.
        first_ad = str(r.get("first_advertised") or "")[:10]
        basis = ""
        start = None
        if first_ad:
            try:
                start = datetime.fromisoformat(first_ad).date()
                basis = "엔카 최초 광고일"
            except ValueError:
                start = None
        if start is None and e:
            try:
                start = datetime.fromisoformat(e["first_seen"]).date()
                basis = "이 도구가 처음 본 날"
            except (ValueError, KeyError):
                start = None
        if start is not None:
            r["days_on_market"] = (today - start).days
            r["days_on_market_basis"] = basis
        else:
            r["days_on_market"] = ""
            r["days_on_market_basis"] = ""

        if e:
            r["first_seen"] = e.get("first_seen", "")
            r["last_seen"] = e.get("last_seen", "")
            prices = e.get("prices") or []
            if prices:
                r["price_first_manwon"] = prices[0][1]
                cur = _to_int(r.get("price_manwon"))
                if cur is not None:
                    r["price_change_manwon"] = cur - prices[0][1]
                if len(prices) > 1:
                    r["price_prev_manwon"] = prices[-2][1]
            r["price_change_count"] = max(0, len(prices) - 1)
    return rows


def market_trend() -> list[dict]:
    """실행별 평균 잔존율 추이.

    한 매물이 싼지는 시세선으로 보지만, 시장 전체가 빠지는 중이라면
    지금 '저평가' 인 차도 몇 주 뒤엔 평범한 가격이 된다. 그래서 시세선
    자체가 어느 쪽으로 움직이는지 따로 본다.
    """
    idx = load_index()
    out = []
    for run in idx.get("runs", []):
        markets = run.get("markets") or {}
        for key, m in markets.items():
            if not isinstance(m, dict):
                continue
            out.append({
                "date": run["date"], "model_key": key,
                "label": m.get("label", key),
                "n": m.get("n", 0),
                "method": m.get("method", ""),
                # 잔존율 모델일 때만 의미가 있다. 3년 / 4.5만km 라는
                # 공통 기준점에서의 잔존율로 비교한다 — 표본의 연식·주행
                # 구성이 주마다 달라도 같은 자로 잴 수 있다.
                "ref_retention": m.get("ref_retention", ""),
                "resid_std": m.get("resid_std", ""),
                "median_price": m.get("median_price", ""),
            })
    return out


REFERENCE_AGE = 3.0
REFERENCE_KM = 45_000


def reference_retention(market) -> float | None:
    """공통 기준점(3년 / 4.5만km)에서의 잔존율.

    주마다 표본 구성이 달라지므로 계수만 비교하면 헷갈린다. 한 점을
    정해 두고 그 점의 값을 비교하면 시장이 오르는지 내리는지 보인다.
    """
    if getattr(market, "method", "") != "ratio":
        return None
    return (market.intercept + market.coef_age * REFERENCE_AGE
            + market.coef_km * (REFERENCE_KM / 1000.0))
