# -*- coding: utf-8 -*-
"""테스트용 모의 헤이딜러 '숨은이력찾기' 화면 생성기.

실제 헤이딜러 화면이 아니라, 3단계 이미지 판독 흐름을 점검하기 위한
합성 이미지다. 실사용 시에는 이 스크립트 대신 실제 스크린샷을 hidden/ 에 넣는다.
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "hidden")

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "C:/Windows/Fonts/malgun.ttf",
]


def font(size: int):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    raise SystemExit("한글 폰트를 찾지 못했습니다.")


CARDS = [
    {
        "plate": "112다2644", "model": "BMW iX xDrive50 (2023)",
        "battery": "삼성SDI (Samsung SDI)",
        "options": ["에어서스펜션 (2축)", "인테그럴 액티브 스티어링",
                    "하만카돈 사운드", "파노라마 글라스루프"],
        "ins": [("보험 처리 이력", "내차 피해 0건 / 상대차 피해 0건"),
                ("총 수리비", "0원"), ("용도 이력", "자가용 (렌트/영업용 없음)"),
                ("소유자 변경", "1회")],
    },
    {
        "plate": "114마2918", "model": "BMW iX xDrive50 (2024)",
        "battery": "파라시스 (Farasis Energy)",
        "options": ["컴포트 액세스", "하만카돈 사운드", "헤드업 디스플레이"],
        "ins": [("보험 처리 이력", "내차 피해 1건 / 상대차 피해 1건"),
                ("총 수리비", "3,800,000원"), ("용도 이력", "자가용"),
                ("소유자 변경", "2회")],
        "no_air": True,
    },
    {
        "plate": "160가9220", "model": "벤츠 EQE SUV 350 4MATIC (2023)",
        "battery": "CATL",
        "options": ["에어매틱 서스펜션", "부메스터 3D 사운드", "MBUX 하이퍼스크린"],
        "ins": [("보험 처리 이력", "내차 피해 0건 / 상대차 피해 1건"),
                ("총 수리비", "320,000원"), ("용도 이력", "자가용"),
                ("소유자 변경", "1회")],
    },
]


def draw_card(c: dict) -> Image.Image:
    W, H = 720, 980
    img = Image.new("RGB", (W, H), "#f2f4f7")
    d = ImageDraw.Draw(img)
    f_h, f_b, f_s, f_t = font(34), font(24), font(19), font(27)

    d.rectangle([0, 0, W, 96], fill="#1f6feb")
    d.text((28, 30), "숨은이력찾기 결과", font=f_h, fill="white")

    y = 128

    def section(title: str, h: int) -> int:
        nonlocal y
        d.rounded_rectangle([24, y, W - 24, y + h], 14, fill="white", outline="#d7dce3")
        d.text((44, y + 18), title, font=f_t, fill="#1f2937")
        y += h + 20
        return y

    top = y
    section("차량 정보", 150)
    d.text((44, top + 62), f"차량번호   {c['plate']}", font=f_b, fill="#111827")
    d.text((44, top + 100), f"차종       {c['model']}", font=f_s, fill="#4b5563")

    top = y
    section("고전압 배터리", 120)
    d.text((44, top + 62), f"제조사   {c['battery']}", font=f_b, fill="#111827")

    top = y
    h = 90 + 34 * len(c["options"])
    section("출고 시 옵션", h)
    for i, o in enumerate(c["options"]):
        d.text((48, top + 62 + i * 34), f"· {o}", font=f_s, fill="#111827")
    if c.get("no_air"):
        d.text((48, top + 62 + len(c["options"]) * 34),
               "· 에어서스펜션: 미장착", font=f_s, fill="#b91c1c")

    top = y
    section("보험 이력 요약", 90 + 36 * len(c["ins"]))
    for i, (k, v) in enumerate(c["ins"]):
        d.text((48, top + 62 + i * 36), f"{k}", font=f_s, fill="#6b7280")
        d.text((300, top + 62 + i * 36), v, font=f_s, fill="#111827")

    d.text((28, H - 44), "※ 테스트용 합성 이미지 (실제 헤이딜러 화면 아님)",
           font=f_s, fill="#9ca3af")
    return img


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    for c in CARDS:
        p = os.path.join(OUT, f"{c['plate']}.png")
        draw_card(c).save(p)
        print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
