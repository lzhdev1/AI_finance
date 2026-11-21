"""确认实验：放开 max_tokens，看 deepseek 能不能把图读全、能不能解读界面截图。"""

from __future__ import annotations

import base64
import io
import os

import requests
from PIL import Image, ImageDraw

BASE = os.getenv("LLM_BASE_URL", "").rstrip("/")
KEY = os.getenv("LLM_API_KEY", "")
MODEL = "deepseek-v4.1-flash-1017"


def ask(question: str, png_bytes: bytes, max_tokens: int = 700) -> None:
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + base64.b64encode(png_bytes).decode()
                        },
                    },
                ],
            }
        ],
        "max_tokens": max_tokens,
    }
    response = requests.post(
        f"{BASE}/chat/completions",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    print("HTTP", response.status_code)
    body = response.json()
    usage = body.get("usage", {})
    print("image_tokens:", usage.get("prompt_tokens_details", {}).get("image_tokens"))
    message = body["choices"][0]["message"]
    print("reasoning:", (message.get("reasoning_content") or "")[:300].replace("\n", " "))
    print("content:", (message.get("content") or "").strip()[:400])
    print()


# 1) 合成小图：看它能不能读全
image = Image.new("RGB", (420, 140), "white")
draw = ImageDraw.Draw(image)
draw.rectangle([6, 6, 414, 134], outline="black", width=2)
draw.text((30, 55), "VISION-PROBE-7731", fill="black")
buffer = io.BytesIO()
image.save(buffer, format="PNG")
print("=== 合成图 ===")
ask("图里写的是什么？只输出文字本身，不要解释。", buffer.getvalue(), max_tokens=800)

# 2) 真实截图：看它能不能做界面判断
print("=== 真实页面截图 ===")
with open("/app/logs/header.png", "rb") as handle:
    screenshot = handle.read()
ask(
    "这是基金模拟盘网页的截图。请回答：顶部横条里列出了哪几个指数？各自的点位和涨跌幅是多少？",
    screenshot,
    max_tokens=900,
)