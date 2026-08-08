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

# 기기 이름 -> (deviceId, capability) 매핑
# capability는 대부분 switch(on/off)로 처리, 필요시 확장 가능
DEVICE_MAP = {
    "안방 에어컨": "23d517ad-5b62-4426-be23-5179795a9d26",
    "거실 에어컨": "a703e1b9-4b90-4c2f-a9e4-300a3b66f7e4",
    "주방 에어컨": "443c166a-383a-4575-af99-58375b2592ed",
    "침실1 에어컨": "bedde265-fc7a-4d6b-925d-bd3668eba9a9",
    "침실2 에어컨": "bce23093-d3a3-4012-ab37-544f66d4a157",
    "안방 조명": "7197d415-8c3e-4eb6-ba0f-c9efcc1c64e8",
    "거실 메인 조명": "46474a84-c2bc-44d5-9670-a153634ac1d9",
    "거실 보조 조명": "bb1ed665-47d4-4e76-9f94-e6d7dc886daa",
    "침실1 조명": "8e20c53d-0b1b-4aeb-a0ff-e308585a01b5",
    "침실2 조명": "782460ad-5105-4253-b25a-11f0580890e9",
    "알파룸 조명": "60ddb35b-3186-455e-b9ee-7a287c45e853",
    "복도 조명": "a4c655ab-dbb6-4b28-a7ed-6af51ec560f3",
    "일괄소등": "5be56120-7200-4b5b-9eb6-35444565f969",
    "공기청정기": "63b35583-534c-d1bf-f87c-f59d9ff24887",
    "환기": "27b23ce7-1675-4ad8-a057-c7ff19685668",
    "주방 가스밸브": "1f238424-eae2-40ab-93b1-6dba1d4bad38",
}

DEVICE_LIST_TEXT = "\n".join(f"- {name}" for name in DEVICE_MAP.keys())


def ask_claude_for_action(user_text):
    """사용자 문장을 보고 어떤 기기에 어떤 동작을 할지 클로드에게 판단시킴"""
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
        "이 목록에 있는 기기 이름과 정확히 똑같은 이름, 그리고 command(on 또는 off)를 "
        "골라서 아래 JSON 형식으로만 답해줘. 다른 설명은 절대 붙이지 마.\n"
        '{"device": "기기이름", "command": "on 또는 off"}\n\n'
        "만약 목록에 없는 기기를 말했거나, 무슨 뜻인지 이해할 수 없으면 아래처럼 답해줘.\n"
        '{"device": null, "command": null}'
    )
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }
    res = requests.post(url, headers=headers, json=payload, timeout=30)
    result = res.json()
    text = result["content"][0]["text"].strip()

    # 클로드가 혹시 코드블록(```json ... ```)으로 감싸서 답할 경우 대비
    text = text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"device": None, "command": None}


def control_device(device_name, command):
    """SmartThings에 실제 제어 명령 전송"""
    device_id = DEVICE_MAP.get(device_name)
    if not device_id:
        return False, "기기를 찾을 수 없습니다."

    url = f"https://api.smartthings.com/v1/devices/{device_id}/commands"
    headers = {
        "Authorization": f"Bearer {SMARTTHINGS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "commands": [
            {"component": "main", "capability": "switch", "command": command}
        ]
    }
    res = requests.post(url, headers=headers, json=body, timeout=15)
    if res.status_code == 200:
        return True, "성공"
    return False, f"실패 (status {res.status_code}): {res.text}"


def send_telegram(text, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    params = {"chat_id": chat_id, "text": text}
    requests.get(url, params=params, timeout=10)


def get_updates(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 30, "offset": offset}
    res = requests.get(url, params=params, timeout=40)
    return res.json()


def main():
    print("가전 제어봇 시작...")
    offset = None

    while True:
        try:
            updates = get_updates(offset)
            for update in updates.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message")
                if not message or "text" not in message:
                    continue

                chat_id = str(message["chat"]["id"])
                user_text = message["text"]
                print(f"받은 메시지: {user_text}")

                action = ask_claude_for_action(user_text)
                device_name = action.get("device")
                command = action.get("command")

                if not device_name or not command:
                    send_telegram(
                        "무슨 기기를 어떻게 하라는 건지 못 알아들었어요. "
                        "예: '안방 에어컨 켜줘'",
                        chat_id,
                    )
                    continue

                success, msg = control_device(device_name, command)
                action_kr = "켰습니다" if command == "on" else "껐습니다"
                if success:
                    send_telegram(f"{device_name} {action_kr}. ✅", chat_id)
                else:
                    send_telegram(f"{device_name} 제어 실패: {msg}", chat_id)

        except Exception as e:
            print(f"오류 발생: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
