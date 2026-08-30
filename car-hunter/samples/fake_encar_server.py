# -*- coding: utf-8 -*-
"""오프라인 검증용 가짜 엔카 API 서버.

브라우저에서 확인된 실제 응답 구조를 흉내내서 collect.py --probe / --discover
가 제대로 동작하는지 네트워크 없이 확인한다. 실제 엔카가 아니다.

일부러 현실적인 '불완전함'을 섞어 뒀다:
  - /general 은 404 (엔드포인트 하나가 죽은 상황)
  - inspection 은 404 (일부 매물에 성능점검 정보 없음)
  - Year.range 필터는 실제로 적용된다 (필터 작동 케이스)

  python samples/fake_encar_server.py --port 8799
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

MANUFACTURERS = [("BMW", 812), ("벤츠", 934), ("아우디", 401), ("테슬라", 288)]
MODEL_GROUPS = {"BMW": [("iX", 42), ("i4", 31), ("iX3", 12)],
                "벤츠": [("EQE", 27), ("EQS", 14), ("EQB", 9)]}
MODELS = {"EQE": [("EQE SUV", 15), ("EQE 세단", 12)], "iX": [("iX", 42)]}

BADGES = ["xDrive50", "xDrive40", "M60"]


def make_listings(n=40):
    out = []
    for i in range(n):
        year = 2021 + (i % 5)                 # 2021~2025 (범위 밖도 섞임)
        month = (i % 12) + 1
        out.append({
            "Id": str(39100000 + i),
            "Manufacturer": "BMW",
            "ModelGroup": "iX",
            "Model": "iX",
            "Badge": BADGES[i % 3],
            "BadgeDetail": BADGES[i % 3] + " 기본형",
            "FuelType": "전기",
            "Year": int(f"{year}{month:02d}"),
            "FormYear": str(year),
            "Mileage": 12000 + i * 1900,
            "Price": 9500 - i * 70,
            "OfficeCityState": ["경기", "서울", "인천"][i % 3],
            "Photo": f"/carpicture/ce{39100000+i}/001.jpg",
            "SellType": "일반",
        })
    return out


ALL = make_listings()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        body = json.dumps(obj, ensure_ascii=False).encode() if obj is not None else b"not found"
        self.send_response(code)
        self.send_header("Content-Type",
                         "application/json;charset=UTF-8" if obj is not None else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)

        # --- 검색 ---
        if u.path == "/search/car/list/general":
            return self._send(404)                      # 일부러 죽여 둔다

        if u.path == "/search/car/list/premium":
            q = unquote(qs.get("q", [""])[0])
            sr = unquote(qs.get("sr", ["|ModifiedDate|0|20"])[0])
            parts = sr.split("|")
            offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            limit = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 20

            # 실제 엔카는 검색 결과와 facet 집합을 '함께' 돌려준다.
            mf = re.search(r"Manufacturer\.([^._)]+)", q)
            mg = re.search(r"ModelGroup\.([^._)]+)", q)

            rows = ALL
            m = re.search(r"Year\.range\((\d{6})\.\.(\d{6})\)", q)
            if m:                                        # 연식 필터 실제 적용
                lo, hi = int(m.group(1)), int(m.group(2))
                rows = [r for r in rows if lo <= r["Year"] <= hi]
            if mf and mf.group(1) != "BMW":               # 표본은 BMW 뿐
                rows = []

            page = rows[offset:offset + limit]            # 페이징 실제 적용
            resp = {"Count": len(rows), "SearchResults": page,
                    "ManufacturerSet": [{"Value": v, "Count": c} for v, c in MANUFACTURERS]}
            if mf:
                resp["ModelGroupSet"] = [{"Value": v, "Count": c}
                                         for v, c in MODEL_GROUPS.get(mf.group(1), [])]
            if mg:
                resp["ModelSet"] = [{"Value": v, "Count": c}
                                    for v, c in MODELS.get(mg.group(1), [])]
            return self._send(200, resp)

        # --- 상세 ---
        m = re.match(r"^/v1/readside/vehicle/(\d+)$", u.path)
        if m:
            vid = m.group(1)
            row = next((r for r in ALL if r["Id"] == vid), None)
            if not row:
                return self._send(404)
            return self._send(200, {
                "vehicleId": int(vid),
                "vehicleNo": f"1{vid[-2:]}가{vid[-4:]}",
                "category": {"manufacturerName": "BMW", "modelName": "iX",
                             "gradeName": row["Badge"], "formYear": int(row["FormYear"]),
                             "yearMonth": row["Year"]},
                "spec": {"mileage": row["Mileage"], "fuelName": "전기"},
                "advertisement": {"price": row["Price"]},
                "options": {"standard": ["에어서스펜션", "인테그럴 액티브 스티어링",
                                          "하만카돈", "파노라마 선루프"]},
                "photos": [{"path": row["Photo"]}],
            })

        m = re.match(r"^/v1/readside/record/vehicle/(\d+)/open$", u.path)
        if m:
            return self._send(200, {"myAccidentCnt": 0, "otherAccidentCnt": 0,
                                    "myAccidentCost": 0, "ownerChangeCnt": 1,
                                    "historyText": "무사고 / 침수이력: 없음"})

        if re.match(r"^/v1/readside/inspection/vehicle/(\d+)$", u.path):
            return self._send(404)                       # 일부러 없는 경로

        if re.match(r"^/v1/readside/diagnosis/vehicle/(\d+)$", u.path):
            return self._send(200, {"diagnosisYn": "Y", "grade": "A"})

        return self._send(404)


def serve(port: int):
    srv = HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    a = ap.parse_args()
    print(f"가짜 엔카 API: http://127.0.0.1:{a.port}  (Ctrl+C 종료)")
    serve(a.port)
    threading.Event().wait()
