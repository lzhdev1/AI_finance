"""Read two independently frozen account histories on identical dates."""

import argparse
import json
from functools import lru_cache
from pathlib import Path

from quantlab import db, replay_archive
from quantlab.config import CONFIG_DIR


def cash_day_reasons(days):
    reasons = dict.fromkeys(("budget_zero", "budget_limited", "hard_stop", "no_target", "other"), 0)
    for day in days:
        if day["cash"] != day["total_value"]:
            continue
        # Read the frozen execution checks. Reapplying today's constraints to an older
        # order would change its explanation.
        budget = next((s for s in day["risk_checks"] if s.startswith("申购最多动用 ")), "")
        if day["rationale"].startswith("已确认净值回撤") and "硬止损线" in day["rationale"]:
            key = "hard_stop"
        elif not any(t["target_weight"] > 0 for t in day["targets"]):
            key = "no_target"
        elif budget.startswith("申购最多动用 0.00 元"):
            key = "budget_zero"
        elif budget:
            key = "budget_limited"
        else:
            key = "other"
        reasons[key] += 1
    return reasons


def _account_summary(archive, version):
    summary = archive["summary"]
    if not summary.get("available"):
        return None
    days = archive["days"]
    account = dict(summary["account"])
    fees = sum(o["fee"] for day in days for o in day["orders"])
    cash_reasons = cash_day_reasons(days)
    account.update(
        total_fees=fees,
        net_pnl=account["total_value"] - account["initial_capital"],
        gross_pnl=account["total_value"] - account["initial_capital"] + fees,
        fee_to_capital=fees / account["initial_capital"],
        cash_days=sum(cash_reasons.values()),
        cash_day_reasons=cash_reasons,
        average_exposure=sum((d["total_value"] - d["cash"]) / d["total_value"] for d in days)
        / len(days)
        if days
        else None,
    )
    if summary["status"] == "ready":
        review = attribution(version)
        fee_audit = review.get("redemption_fee_audit")
        if review.get("run_id") == summary["id"] and review.get("complete") and fee_audit:
            difference = fee_audit["covered_difference"] if fee_audit["closed_lots"] else 0.0
            complete = (
                fee_audit["uncovered_lots"] == 0
                and fee_audit["uncovered_orders"] == 0
                and difference is not None
            )
            account.update(
                fee_audit_complete=complete,
                covered_fee_difference=difference,
                fixed_fill_fees=fees + difference if complete else None,
                fixed_fill_return=(account["net_pnl"] - difference) / account["initial_capital"]
                if complete
                else None,
            )
    return {**summary, "account": account, "strategy_version": version}


def _scored_by_key(archive):
    return {
        (
            f.get("base_date") or day["trade_date"],
            f["target_date"],
            f["horizon_days"],
            f["sector_name"],
            f["proxy_code"],
        ): f
        for day in archive["days"]
        for f in day["forecasts"]
        if f["actual_return"] is not None
    }


def matched_forecasts(original, optimized):
    first, second = _scored_by_key(original), _scored_by_key(optimized)
    result = []
    for horizon in (1, 5):
        shared = [
            (first[k], second[k]) for k in sorted(first.keys() & second.keys()) if k[2] == horizon
        ]
        if any(abs(a["actual_return"] - b["actual_return"]) > 1e-8 for a, b in shared):
            raise ValueError("共同预测使用了不同的实际回报，不能直接比较")
        directed = [(a, b) for a, b in shared if a["hit"] is not None and b["hit"] is not None]
        row = {"horizon_days": horizon, "scored": len(shared), "directional": len(directed)}
        for ix, version in enumerate(("original", "optimized")):
            hits = sum(int(pair[ix]["hit"]) for pair in directed)
            row[f"{version}_hits"] = hits
            row[f"{version}_hit_rate"] = hits / len(directed) if directed else None
            row[f"{version}_brier"] = (
                sum(
                    (pair[ix]["up_probability"] - int(pair[ix]["actual_return"] > 0)) ** 2
                    for pair in shared
                )
                / len(shared)
                if shared
                else None
            )
        result.append(row)
    return result


def comparison():
    first, second = (replay_archive.current(replay_archive.version_path(v)) for v in ("v1", "v2"))
    original, optimized = _account_summary(first, "v1"), _account_summary(second, "v2")
    matched = []
    if original and optimized and original["status"] == optimized["status"] == "ready":
        if (
            original["account"]["initial_capital"] != optimized["account"]["initial_capital"]
            or original["account"]["benchmark_anchor"] != optimized["account"]["benchmark_anchor"]
        ):
            raise ValueError("两版回放的期初资金或沪深300基准起点不一致")
        if original["period"] != optimized["period"] or any(
            (a["trade_date"], a["benchmark_return"]) != (b["trade_date"], b["benchmark_return"])
            for a, b in zip(original["history"], optimized["history"], strict=True)
        ):
            raise ValueError("两版回放的日期或基准收益不一致")
        matched = matched_forecasts(first, second)
    return {"original": original, "optimized": optimized, "matched_forecasts": matched}


def _attribution_path(version):
    return replay_archive.version_path(version).with_name(f"{version}-attribution.json")


@lru_cache(maxsize=4)
def _read_report(path: Path, stamp):
    return json.loads(path.read_bytes())


def _optional_report(path):
    try:
        info = path.stat()
        return _read_report(path, (info.st_mtime_ns, info.st_size, info.st_ino))
    except FileNotFoundError:
        return None


def attribution(version):
    return _optional_report(_attribution_path(version)) or {"available": False, "strategy_version": version}


def opportunity_evaluation():
    report = _optional_report(replay_archive.ARCHIVE.with_name("opportunity-evaluation.json"))
    audit = _optional_report(CONFIG_DIR / "replay_june_2026_audit.json")
    return {**(report or {"status": "empty"}), "available": report is not None, "june_audit": audit}


def publish_attribution(version):
    from quantlab.replay_attribution import build_report

    account = db.query_one(
        f"SELECT entry_evidence FROM {db.qualified('pod')} WHERE strategy_key='agent_manager'"
    )
    manifest = account["entry_evidence"] if account else {}
    summary = replay_archive.current(replay_archive.version_path(version))["summary"]
    if (
        manifest.get("mode") != "historical_replay"
        or manifest.get("strategy_version", "v1") != version
        or summary.get("status") != "ready"
        or summary["id"] != manifest["run_id"]
        or db.query_one("SELECT current_database() AS name")["name"] != manifest["run_id"]
    ):
        raise ValueError("逐笔归因需要与已完成归档对应的独立账户")
    report = {
        **build_report(),
        "available": True,
        "strategy_version": version,
        "run_id": manifest["run_id"],
    }
    replay_archive.write_snapshot(_attribution_path(version), report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="为封存的回放账户生成逐笔复盘")
    parser.add_argument("--attribution", required=True, choices=("v1", "v2"))
    publish_attribution(parser.parse_args().attribution)
