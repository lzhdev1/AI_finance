"""LLM 配置自检：验证 .env 里的端点、key、模型名是否真的可用。

    docker compose run --rm scheduler python scripts/check_llm.py

它会依次做四件事：
1. 打印当前配置（key 打码）；
2. 拉取端点上的模型清单，检查你填的模型名在不在列表里；
3. 发一次真实请求，输出耗时、token 用量、回复内容；
4. 再发一次 JSON 模式请求，验证结构化输出可用（决策卡叙述层依赖它）。

任何一步失败都会直接告诉你失败原因，而不是等到跑决策时才炸。
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests

BASE = os.getenv("LLM_BASE_URL", "").rstrip("/")
KEY = os.getenv("LLM_API_KEY", "")
FAST = os.getenv("LLM_MODEL_FAST", "")
DEEP = os.getenv("LLM_MODEL_DEEP", "")
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))


def mask(value: str) -> str:
    if not value:
        return "(空)"
    return value[:7] + "..." + value[-4:] + f"（长度 {len(value)}）"


def fail(message: str) -> None:
    print(f"  ✗ {message}")


def main() -> int:
    print("=" * 62)
    print("1. 当前配置")
    print("=" * 62)
    print(f"  LLM_PROVIDER   = {os.getenv('LLM_PROVIDER', '(未设)')}")
    print(f"  LLM_BASE_URL   = {BASE or '(空)'}")
    print(f"  LLM_API_KEY    = {mask(KEY)}")
    print(f"  LLM_MODEL_FAST = {FAST or '(空)'}")
    print(f"  LLM_MODEL_DEEP = {DEEP or '(空)'}")
    print(f"  LLM_JSON_MODE  = {os.getenv('LLM_JSON_MODE', '(未设)')}")

    if not BASE or not KEY or not FAST:
        fail("BASE_URL / API_KEY / MODEL_FAST 有缺项，先补 .env")
        return 1
    if not BASE.startswith("https://"):
        fail("BASE_URL 必须以 https:// 开头")
        return 1
    if not BASE.endswith("/v1"):
        print("  ! BASE_URL 通常以 /v1 结尾（OpenAI 兼容接口），确认一下")

    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

    print()
    print("=" * 62)
    print("2. 模型清单")
    print("=" * 62)
    try:
        response = requests.get(f"{BASE}/models", headers=headers, timeout=30)
    except Exception as exc:  # noqa: BLE001
        fail(f"拉取模型清单失败：{type(exc).__name__} {exc}")
        return 2

    if response.status_code != 200:
        fail(f"HTTP {response.status_code}：{response.text[:300]}")
        return 2

    try:
        models = [item["id"] for item in response.json().get("data", [])]
    except Exception:  # noqa: BLE001
        fail(f"返回不是标准模型清单：{response.text[:300]}")
        return 2

    print(f"  端点共 {len(models)} 个模型")
    for name in (FAST, DEEP):
        if not name:
            continue
        mark = "✓" if name in models else "✗ 不在清单里"
        print(f"  {mark}  {name}")
    if FAST not in models:
        similar = [name for name in models if FAST.replace(".", "") in name.replace(".", "")]
        if similar:
            print(f"  ! 你是不是想写：{', '.join(similar[:5])}")
        else:
            print("  ! 可用模型示例：")
            for name in models[:8]:
                print(f"      {name}")

    print()
    print("=" * 62)
    print("3. 普通对话")
    print("=" * 62)
    started = time.time()
    try:
        response = requests.post(
            f"{BASE}/chat/completions",
            headers=headers,
            json={
                "model": FAST,
                "messages": [{"role": "user", "content": "用一句话说明 A 股场外基金的申赎截止时间。"}],
                "max_tokens": 512,   # 推理型模型会先消耗推理 token，给小了会拿到空回复
                "temperature": 0.2,
            },
            timeout=TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        fail(f"请求失败：{type(exc).__name__} {exc}")
        return 3

    if response.status_code != 200:
        fail(f"HTTP {response.status_code}：{response.text[:400]}")
        return 3

    payload = response.json()
    message = payload["choices"][0]["message"]
    content = (message.get("content") or "").strip()
    reasoning = (message.get("reasoning_content") or "").strip()
    print(f"  ✓ HTTP 200，耗时 {time.time() - started:.1f}s")
    print(f"  token 用量：{payload.get('usage')}")
    if reasoning:
        print(f"  （推理内容 {len(reasoning)} 字，已省略）")
    print(f"  回复：{content[:300] or '(空回复 —— 可能 max_tokens 太小，全被推理吃掉了)'}")

    print()
    print("=" * 62)
    print("4. JSON 模式（决策卡叙述层依赖）")
    print("=" * 62)
    system = (
        "你是 A 股基金研究员。只输出 JSON，不要任何多余文字。"
        "禁止改动输入中的任何数字。"
    )
    user = (
        "把下面的决策依据改写为不超过 60 字的专业表述，数字必须原样保留。"
        '输出 {"narrative": "..."}。\n'
        "输入：白酒板块综合分 +1.69，5 日动量 +4.0%，资金流强度 +0.0%，目标仓位 35%。"
    )
    started = time.time()
    try:
        response = requests.post(
            f"{BASE}/chat/completions",
            headers=headers,
            json={
                "model": FAST,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 1200,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        fail(f"请求失败：{type(exc).__name__} {exc}")
        return 4

    if response.status_code != 200:
        fail(f"HTTP {response.status_code}：{response.text[:400]}")
        print("  ! 部分端点不支持 response_format，需要在 .env 里设 LLM_JSON_MODE=false")
        return 4

    content = (response.json()["choices"][0]["message"].get("content") or "").strip()
    print(f"  ✓ HTTP 200，耗时 {time.time() - started:.1f}s")
    try:
        parsed = json.loads(content)
        print(f"  ✓ JSON 可解析：{json.dumps(parsed, ensure_ascii=False)[:300]}")
    except json.JSONDecodeError:
        fail(f"返回不是合法 JSON：{content[:300]}")
        print("  ! 把 LLM_JSON_MODE 设为 false，或换一个支持 JSON 模式的模型")
        return 4

    print()
    print("=" * 62)
    print("全部通过。可以跑决策了。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())