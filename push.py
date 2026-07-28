import requests
from urllib.parse import quote
from config import BARK_KEY

def send_bark(title: str, content: str, sound="minuet") -> bool:
    """
    发送 Bark 消息推送 (对 title 和 content 进行 URL encode，防止特殊字符导致 HTTP 404)
    sound:
        - minuet/anticipate: 买入轻快提示音
        - alarm/emergency: 卖出紧急警报音
    """
    if not BARK_KEY or BARK_KEY == "YOUR_BARK_KEY_HERE":
        print(f"⚠️ [未配置 BARK_KEY] 模拟推送: {title} - {content}")
        return False

    title_encoded = quote(title, safe='')
    content_encoded = quote(content, safe='')
    url = f"https://api.day.app/{BARK_KEY}/{title_encoded}/{content_encoded}"
    params = {
        "sound": sound,
        "group": "龙魂智策"
    }

    try:
        resp = requests.get(url, params=params, timeout=4)
        if resp.status_code == 200:
            print(f"👉 [Bark 推送成功] {title}: {content}")
            return True
        else:
            print(f"❌ [Bark 推送响应异常] HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"❌ [Bark 推送网络异常] {e}")
        return False
