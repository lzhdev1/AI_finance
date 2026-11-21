"""确认新浪行业板块接口的字段含义（要做降级抓取，必须先把字段对上）。"""

from __future__ import annotations

import re
import urllib.request

URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
}

request = urllib.request.Request(URL, headers=HEADERS)
with urllib.request.urlopen(request, timeout=15) as response:
    text = response.read().decode("gbk", errors="replace")

match = re.search(r"=\s*(\{.*\})", text, re.S)
payload = __import__("json").loads(match.group(1))
print("板块数:", len(payload))

for code, value in list(payload.items())[:3]:
    parts = value.split(",")
    print(f"\n{code}  共 {len(parts)} 个字段")
    for index, field in enumerate(parts):
        print(f"  [{index}] {field}")