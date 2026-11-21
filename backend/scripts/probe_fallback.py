"""探针：东财被限流时，还有哪些公开接口能给板块行情/资金。"""

from __future__ import annotations

import json
import urllib.request

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn",
}

TARGETS = [
    ("sina_industry", "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"),
    ("sina_node", "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page=1&num=3&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=page"),
    ("sina_class_flow", "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssi_ssfx_flzjtj?daima=sh600519"),
    ("em_datacenter", "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DMSK_TS_STOCKNEW&columns=ALL&pageSize=3"),
    ("em_quote_old", "https://quote.eastmoney.com/api/qt/clist/get?pn=1&pz=3&fs=m:90+t:2&fields=f3,f14"),
    ("tencent_sector", "https://qt.gtimg.cn/q=sh000001"),
]

for name, url in TARGETS:
    try:
        request = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read()
        text = raw.decode("utf-8", errors="replace")
        print(f"OK   {name:<18} bytes={len(raw)} head={text[:120]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {name:<18} {type(exc).__name__}: {str(exc)[:70]}")