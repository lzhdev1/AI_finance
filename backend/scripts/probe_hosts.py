"""探针：东财板块接口在多个行情节点上的可用性。

push2 被限流时，换 1.push2 / 82.push2 / push2delay 往往能通。
"""

from __future__ import annotations

import json
import urllib.request

HOSTS = [
    "https://push2.eastmoney.com",
    "https://1.push2.eastmoney.com",
    "https://7.push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
]

QUERY = (
    "/api/qt/clist/get?pn=1&pz=5&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2"
    "&fields=f2,f3,f5,f6,f8,f12,f14,f62,f104,f105,f184"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

for host in HOSTS:
    try:
        request = urllib.request.Request(host + QUERY, headers=HEADERS)
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        diff = (payload.get("data") or {}).get("diff") or []
        sample = diff[0] if diff else {}
        print(f"OK   {host:<40} rows={len(diff)} 首行={sample.get('f14')} {sample.get('f3')}% 额={sample.get('f6')}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {host:<40} {type(exc).__name__}: {str(exc)[:60]}")