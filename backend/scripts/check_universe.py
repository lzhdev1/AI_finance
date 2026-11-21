from __future__ import annotations

from quantlab.config import load_universe


def main() -> None:
    import akshare as ak

    universe = load_universe()
    funds = ak.fund_name_em()
    code_col = next(c for c in funds.columns if "代码" in c)
    name_col = next(c for c in funds.columns if "简称" in c or "名称" in c)
    lookup = dict(zip(funds[code_col].astype(str).str.zfill(6), funds[name_col].astype(str)))

    print(f"{'代码':<8}{'配置名称':<28}{'实际名称':<28}状态")
    print("-" * 80)
    for asset in universe["assets"]:
        code = asset["code"]
        actual = lookup.get(code, "未找到")
        configured = asset["name"]
        ok = actual != "未找到" and (
            configured.replace("A", "").replace("C", "")[:4]
            in actual
            or actual[:4] in configured
        )
        print(f"{code:<8}{configured:<28}{actual:<28}{'OK' if ok else 'CHECK'}")


if __name__ == "__main__":
    main()