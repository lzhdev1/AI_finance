from datetime import date

import pytest

from quantlab.replay_data import compare_closes, parse_finance, verify_databases


def test_news_archive_uses_publisher_day_and_minute():
    entry = """<li><div class="dd_lm">[<a href="/cj/">财经</a>]</div>
    <div class="dd_bt"><a href="/cj/2026/05-21/123.shtml">产业数据发布</a></div>
    <div class="dd_time">5-21 14:31</div></li>"""
    html = '<div class="btl">即时新闻 (2026-05-21)</div>' + entry + entry
    html += entry.replace("5-21", "5-22") + entry.replace("财经", "体育")
    html += entry.replace("2026/05-21", "2025/05-21")
    html += entry.replace("/cj/2026/05-21/123.shtml", "https://example.org/another")
    items = parse_finance(date(2026, 5, 21), html)
    assert len(items) == 1
    source, _, published_at, title, url, digest = items[0]
    assert source == "chinanews-finance" and title == "产业数据发布"
    assert published_at.isoformat() == "2026-05-21T14:31:00+08:00"
    assert url == "https://www.chinanews.com.cn/cj/2026/05-21/123.shtml" and len(digest) == 64
    with pytest.raises(ValueError):
        parse_finance(date(2025, 5, 21), html)


def test_benchmark_truth_check_distinguishes_precision_from_bad_or_absent_prices():
    day = date(2026, 11, 20)
    stored = {day: "3954.565"}
    assert compare_closes(stored, {day: "3954.57"}, [day])["matched"] == 1
    for invalid in ({}, {day: "3955.57"}, {day: "NaN"}):
        with pytest.raises(ValueError):
            compare_closes(stored, invalid, [day])


@pytest.mark.parametrize(
    "source,target",
    [
        ("quantlab", "quantlab"),
        ("quantlab", "postgres"),
        ("quantlab_replay_test", "quantlab_replay_test"),
    ],
)
def test_refuse_live_or_shared_destination(source, target):
    with pytest.raises(ValueError):
        verify_databases(source, target)
