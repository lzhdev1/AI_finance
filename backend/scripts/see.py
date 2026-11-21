"""用端点上的多模态模型"看"图片，把结果读给我这个纯文本模型听。

用法：
    python scripts/see.py logs/ui.png          # 描述这张图
    python scripts/see.py logs/ui.png "指出布局问题"

这是一条变通链路：我（deepseek 文本模型）看不到图，
但同一个端点上的 qwen3-vl-plus 能看，于是让它先看、再把结论写成文字给我。
"""

from __future__ import annotations

import base64
import io
import os
import sys

import requests

BASE = os.getenv("LLM_BASE_URL", "").rstrip("/")
KEY = os.getenv("LLM_API_KEY", "")
VISION_MODEL = os.getenv("LLM_MODEL_VISION", "qwen3-vl-plus")


def encode(path: str) -> str:
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode()


def describe(path: str, question: str) -> str:
    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encode(path)}"},
                    },
                ],
            }
        ],
        "max_tokens": 1600,
    }
    response = requests.post(
        f"{BASE}/chat/completions",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if response.status_code != 200:
        return f"HTTP {response.status_code}: {response.text[:400]}"
    return response.json()["choices"][0]["message"].get("content", "")


if __name__ == "__main__":
    image = sys.argv[1]
    prompt = (
        sys.argv[2]
        if len(sys.argv) > 2
        else "这是一张网页截图。请客观描述：页面分成了哪几个区域、每块放了什么、有没有明显的排版问题（错位、拥挤、大片空白、控件边框突兀）。用中文，200 字以内。"
    )
    print(describe(image, prompt))