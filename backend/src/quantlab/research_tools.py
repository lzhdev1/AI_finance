"""The live manager selects bounded, read-only investigations against its decision clock."""

from datetime import timedelta

from . import db

TOOLS = ("inspect_sector", "read_news", "recall")


def select_calls(requests, candidates):
    valid = {r["board_id"] for r in candidates}
    selected = []
    for item in requests if isinstance(requests, list) else ():
        if not isinstance(item, dict):
            continue
        tool, bid = item.get("tool"), item.get("board_id")
        if tool not in TOOLS or type(bid) is not int or bid not in valid:
            continue
        if (tool, bid) not in selected:
            selected.append((tool, bid))
        if len(selected) == 4:
            break
    return selected


def _board_news(board, as_of):
    rows = db.query(
        f"""SELECT news_id id,title,left(body,240) excerpt,source,url,published_at
        FROM {db.qualified("news_item")}
        WHERE published_at BETWEEN %s AND %s AND ingested_at<=%s
        AND (strpos(title,%s)>0 OR strpos(coalesce(body,''),%s)>0)
        ORDER BY published_at DESC,news_id DESC LIMIT 4""",
        [as_of - timedelta(days=7), as_of, as_of, board, board],
    )
    return [{**r, "published_at": r["published_at"].isoformat()} for r in rows]


def investigate(requests, candidates, memories, as_of):
    by_id = {r["board_id"]: r for r in candidates}
    calls = []
    for tool, bid in select_calls(requests, candidates):
        row = by_id[bid]
        if tool == "read_news":
            articles = _board_news(row["board"], as_of)
            result = {"articles": articles}
            has_data = bool(articles)
            # A selected article can now be cited against this board's actual evidence.
            row["news"] = list({n["id"]: n for n in [*articles, *row["news"]]}.values())
            row["evidence_ids"] = list(
                dict.fromkeys([*row["evidence_ids"], *(f"N{n['id']}" for n in articles)])
            )
        elif tool == "recall":
            previous = db.query(
                f"""SELECT report_id,as_of,payload->>'summary' AS summary
                FROM {db.qualified("agent_research")}
                WHERE as_of<%s AND payload->'watchlist' @> jsonb_build_array(jsonb_build_object('board_id',%s::int))
                ORDER BY as_of DESC,report_id DESC LIMIT 2""",
                [as_of, bid],
            )
            result = {
                "memories": [
                    m
                    for m in memories
                    if m["board_id"] == bid and m["proxy_code"] == row["proxy_code"]
                ],
                "prior_research": [{**r, "as_of": r["as_of"].isoformat()} for r in previous],
            }
            has_data = any(result.values())
        else:
            result = {
                key: row.get(key)
                for key in (
                    "as_of",
                    "source",
                    "proxy_code",
                    "momentum_5",
                    "momentum_20",
                    "flow_ratio",
                    "capital_flow",
                    "intraday",
                    "fund_status",
                    "behavior",
                )
            }
            has_data = True
        calls.append(
            {
                "tool": tool,
                "board_id": bid,
                "board": row["board"],
                "as_of": as_of.isoformat(),
                "status": "ok" if has_data else "empty",
                "result": result,
            }
        )
    return calls
