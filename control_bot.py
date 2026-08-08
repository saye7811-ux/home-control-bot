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

# 전체 상태 조회 시 제외할 기기 (실제 on/off 상태를 가진 기기가 아니라 버튼/씬)
STATUS_EXCLUDE = {"일괄소등"}

DEVICE_LIST_TEXT = "\n".join(f"- {name}" for name in DEVICE_MAP.keys())


def build_help_text(with_greeting=True):
    """기기 목록을 사람이 보기 좋은 안내문으로 정리"""
    lines = []
    if with_greeting:
        lines.append("안녕하세요! 🙌 제가 할 수 있는 건 이런 것들이에요:\n")
    else:
        lines.append("제가 할 수 있는 건 이런 것들이에요:\n")
    lines.append("🔌 [켜기/끄기 가능한 기기]")
    for name in DEVICE_MAP.keys():
        lines.append(f"- {name}")
    lines.append("\n📊 [상태 확인도 가능해요]")
    lines.append("예: '안방 에어컨 켜져있어?', '지금 뭐 켜져있어?' (전체 조회)")
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
        "아래 다섯 가지 중 하나로 판단해서 JSON으로만 답해줘. 다른 설명은 절대 붙이지 마.\n\n"
        "1) 특정 기기를 켜거나 끄라는 명령이면:\n"
        '{"intent": "control", "device": "기기이름(목록과 정확히 동일하게)", "command": "on 또는 off"}\n\n'
        "2) 특정 기기 하나를 콕 집어서 켜져있는지/꺼져있는지 묻는 질문이면 (예: '안방 에어컨 켜져있어?'):\n"
        '{"intent": "status", "device": "기기이름(목록과 정확히 동일하게)"}\n\n'
        "3) 특정 기기를 지정하지 않고, 지금 전체적으로 뭐가 켜져있는지/꺼져있는지 묻는 질문이면 "
        "(예: '지금 뭐 켜져있어?', '켜져있는 거 다 보여줘', '집 상태 어때?', '지금 기기들 상태 확인해줘', "
        "'뭐 켜놓고 나왔지?'):\n"
        '{"intent": "status_all"}\n\n'
        "4) 봇이 뭘 할 수 있는지 묻거나, 사용법/명령어를 묻는 질문이면 (예: '뭐 할 수 있어?', "
        "'너한테 어떤거 시킬수있어?', '뭐 시킬 수 있어?', '사용법 알려줘', '명령어 뭐있어?' 등):\n"
        '{"intent": "help"}\n\n'
        "5) 위 네 경우가 아니거나, 목록에 없는 기기를 말했거나, 인사말/잡담이거나, 무슨 뜻인지 모르겠으면:\n"
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
    except json.JSONDecodeError:
        return {"intent": "unknown"}


def ask_claude_for_reply(user_text):
    """의도 파악이 안 되는 메시지에 대해, 고정 문구 대신 클로드가 자연스러운 대화체 답장을 생성"""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    prompt = (
        "너는 집 가전을 제어하는 친근한 텔레그램 봇이야. 이모티콘을 적절히 섞어서 대답해.\n\n"
        "네가 실제로 할 수 있는 일은 아래 기기들을 켜고 끄거나 상태를 확인해주는 것뿐이야:\n\n"
        f"{DEVICE_LIST_TEXT}\n\n"
        f"사용자가 이렇게 말했어: \"{user_text}\"\n\n"
        "이건 가전 제어나 상태 확인 요청은 아닌 것 같아. 사용자 말에 자연스럽게 반응하면서, "
        "네가 실제로 할 수 있는 건 가전 제어/상태 확인뿐이라는 걸 짧고 친근하게 알려줘. "
        "기기 목록을 전부 나열하지는 말고, 필요하면 '뭐 할 수 있는지 알려줘' 같은 말로 유도해. "
        "2~3문장 이내로, 다른 설명 없이 답변 텍스트만 출력해."
    )
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }
    res = requests.post(url, headers=headers, json=payload, timeout=30)
    result = res.json()
    return result["content"][0]["text"].strip()


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


def get_device_status(device_name):
    """SmartThings에서 기기 하나의 현재 on/off 상태 조회"""
    device_id = DEVICE_MAP.get(device_name)
    if not device_id:
        return None, "기기를 찾을 수 없습니다."

    url = f"https://api.smartthings.com/v1/devices/{device_id}/status"
    headers = {"Authorization": f"Bearer {SMARTTHINGS_TOKEN}"}
    res = requests.get(url, headers=headers, timeout=15)
    if res.status_code != 200:
        return None, f"조회 실패 (status {res.status_code})"

    try:
        switch_state = res.json()["components"]["main"]["switch"]["switch"]["value"]
        return switch_state, "성공"
    except (KeyError, TypeError):
        return None, "상태 정보를 읽지 못했습니다."


def get_all_device_status():
    """등록된 모든 기기(일괄소등 제외)를 순회하며 상태 조회, 켜진 것/꺼진 것/조회 실패로 분류"""
    on_list = []
    off_list = []
    failed_list = []

    for name in DEVICE_MAP.keys():
        if name in STATUS_EXCLUDE:
            continue
        state, _ = get_device_status(name)
        if state == "on":
            on_list.append(name)
        elif state == "off":
            off_list.append(name)
        else:
            failed_list.append(name)

    return on_list, off_list, failed_list


def build_all_status_text(on_list, off_list, failed_list):
    """전체 상태 조회 결과를 사람이 보기 좋은 메시지로 정리"""
    lines = []
    if on_list:
        lines.append(f"🟢 켜져있는 기기 ({len(on_list)}개)")
        for name in on_list:
            lines.append(f"- {name}")
    else:
        lines.append("🟢 켜져있는 기기 없음")

    lines.append("")

    if off_list:
        lines.append(f"⚪ 꺼져있는 기기 ({len(off_list)}개)")
        for name in off_list:
            lines.append(f"- {name}")

    if failed_list:
        lines.append("")
        lines.append(f"⚠️ 조회 실패 ({len(failed_list)}개)")
        for name in failed_list:
            lines.append(f"- {name}")

    return "\n".join(lines)


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
                intent = action.get("intent")

                if intent == "help":
                    send_telegram(build_help_text(), chat_id)
                    continue

                if intent == "status_all":
                    send_telegram("잠시만요, 전체 기기 상태 확인 중이에요... 🔍", chat_id)
                    on_list, off_list, failed_list = get_all_device_status()
                    send_telegram(
                        build_all_status_text(on_list, off_list, failed_list), chat_id
                    )
                    continue

                if intent == "status":
                    device_name = action.get("device")
                    if not device_name:
                        on_list, off_list, failed_list = get_all_device_status()
                        send_telegram(
                            build_all_status_text(on_list, off_list, failed_list), chat_id
                        )
                        continue
                    state, msg = get_device_status(device_name)
                    if state == "on":
                        send_telegram(f"{device_name} 지금 켜져있어요! 🟢", chat_id)
                    elif state == "off":
                        send_telegram(f"{device_name} 지금 꺼져있어요. ⚪", chat_id)
                    else:
                        send_telegram(f"앗, {device_name} 상태를 못 가져왔어요 😅 ({msg})", chat_id)
                    continue

                if intent == "control":
                    device_name = action.get("device")
                    command = action.get("command")

                    if not device_name or not command:
                        reply = ask_claude_for_reply(user_text)
                        send_telegram(reply, chat_id)
                        continue

                    success, msg = control_device(device_name, command)
                    action_kr = "켰어요" if command == "on" else "껐어요"
                    emoji = "🟢" if command == "on" else "⚪"
                    if success:
                        send_telegram(f"{device_name} {action_kr}! {emoji}", chat_id)
                    else:
                        send_telegram(f"앗, {device_name} 제어에 실패했어요 😢 ({msg})", chat_id)
                    continue

                # intent가 unknown인 경우 - 고정 문구 대신 클로드가 상황에 맞게 답장 생성
                reply = ask_claude_for_reply(user_text)
                send_telegram(reply, chat_id)

        except Exception as e:
            print(f"오류 발생: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
