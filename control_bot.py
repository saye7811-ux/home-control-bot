"""
가전 제어봇
- 텔레그램 메시지를 계속 감시(polling)
- 새 메시지가 오면 클로드에게 "어떤 기기에 무슨 명령을 내려야 하는지" 판단시킴
- 판단 결과로 SmartThings API에 실제 제어 명령 전송
- 결과를 텔레그램으로 답장
"""

import os
import time
import json
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
SMARTTHINGS_TOKEN = os.environ.get("SMARTTHINGS_TOKEN", "")

DEVICE_MAP = {
    "안방 에어컨": "23d517ad-5b62-4426-be23-5179795a9d26",
    "거실 에어컨": "a703e1b9-4b90-4c2f-a9e4-300a3b66f7e4",
    "주방 에어컨": "443c166a-383a-4575-af99-58375b2592ed",
    "취미방 에어컨": "bedde265-fc7a-4d6b-925d-bd3668eba9a9",
    "손님방 에어컨": "bce23093-d3a3-4012-ab37-544f66d4a157",
    "안방 조명": "7197d415-8c3e-4eb6-ba0f-c9efcc1c64e8",
    "거실 메인 조명": "46474a84-c2bc-44d5-9670-a153634ac1d9",
    "거실 보조 조명": "bb1ed665-47d4-4e76-9f94-e6d7dc886daa",
    "취미방 조명": "8e20c53d-0b1b-4aeb-a0ff-e308585a01b5",
    "손님방 조명": "782460ad-5105-4253-b25a-11f0580890e9",
    "거실 팬트리 조명": "60ddb35b-3186-455e-b9ee-7a287c45e853",
    "복도 조명": "a4c655ab-dbb6-4b28-a7ed-6af51ec560f3",
    "일괄소등": "5be56120-7200-4b5b-9eb6-35444565f969",
    "공기청정기": "63b35583-534c-d1bf-f87c-f59d9ff24887",
    "환기": "27b23ce7-1675-4ad8-a057-c7ff19685668",
    "주방 가스밸브": "1f238424-eae2-40ab-93b1-6dba1d4bad38",
}

DEVICE_LIST_TEXT = "\n".join(f"- {name}" for name in DEVICE_MAP.keys())


def build_help_text():
    """기기 목록을 사람이 보기 좋은 안내문으로 정리"""
    lines = ["안녕하세요! 🙌 제가 할 수 있는 건 이런 것들이에요:\n"]
    lines.append("🔌 [켜기/끄기 가능한 기기]")
    for name in DEVICE_MAP.keys():
        lines.append(f"- {name}")
    lines.append("\n📊 [상태 확인도 가능해요]")
    lines.append("예: '안방 에어컨 켜져있어?', '지금 상태 어때?'")
    lines.append("\n💬 예시: '안방 에어컨 켜줘', '거실 조명 꺼줘', '일괄소등 해줘'")
    return "\n".join(lines)


def ask_claude_for_action(user_text):
    """사용자 문장을 보고 어떤 기기에 어떤 동작을 할지, 혹은 기능 안내가 필요한지 클로드에게 판단시킴"""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    prompt = (
        "너는 집 가전을 제어하는 비서야. 아래는 우리 집에 있는 기기 목록이야.\n\n"
        f"{DEVICE_LIST_TEXT}\n\n"
        f"사용자가 이렇게 말했어: \"{user_text}\"\n\n"
        "아래 네 가지 중 하나로 판단해서 JSON으로만 답해줘. 다른 설명은 절대 붙이지 마.\n\n"
        "1) 특정 기기를 켜거나 끄라는 명령이면:\n"
        '{"intent": "control", "device": "기기이름(목록과 정확히 동일하게)", "command": "on 또는 off"}\n\n'
        "2) 특정 기기가 켜져있는지/꺼져있는지 상태를 묻는 질문이면:\n"
        '{"intent": "status", "device": "기기이름(목록과 정확히 동일하게)"}\n\n'
        "3) 봇이 뭘 할 수 있는지 묻거나, 사용법을 묻는 질문이면:\n"
        '{"intent": "help"}\n\n'
        "4) 위 세 경우가 아니거나, 목록에 없는 기기를 말했거나, 무슨 뜻인지 모르겠으면:\n"
        '{"intent": "unknown"}'
    )
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }
    res = requests.post(url, headers=headers, json=payload, timeout=30)
    result = res.json()
    text = result["content"][0]["text"].strip()

    text = text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(text)
    except
