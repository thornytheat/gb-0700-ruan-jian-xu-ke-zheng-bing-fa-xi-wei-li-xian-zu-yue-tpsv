import pytest
from app.modules.m8_pool.service import evaluate
from app.modules.m8_pool.sample import sample_text
from app import store
from app.errors import BusinessError

HEADER = "event_id,pool,dept,kind,start,end,version,seats,ref"
T0 = "2026-11-18T00:00:00+00:00"


def run(*lines):
    return evaluate("\n".join([HEADER, *lines]) + "\n")


def quota(pool, dept, seats, t=T0, rev=1, eid=None):
    eid = eid or f"Q-{dept}-{rev}"
    return f"{eid},{pool},{dept},quota,{t},,{rev},{seats},"


def borrow(pool, lender, borrower, seats, a, b, eid, rev=1):
    return f"{eid},{pool},{lender},borrow,{a},{b},{rev},{seats},{borrower}"


def term(kind, pool, dept, ref, a, rev, seats=1):
    return f"E-{kind}-{ref}-{rev},{pool},{dept},{kind},{a},,{rev},{seats},{ref}"


def session(pool, dept, a, b, seats=1, eid=None):
    eid = eid or f"S-{dept}-{a[-8:-3].replace(':','')}"
    return f"{eid},{pool},{dept},session,{a},{b},1,{seats},"


def latest(result, borrow_id):
    rows = [r for r in result["report"]["rows"] if r["borrow_id"] == borrow_id]
    assert len(rows) == 1
    return rows[0]


def reasons(result):
    return [q["reason"] for q in result["quarantine"]]


# ---------- 借用结果：原配额、借入方、到期安排 ----------

def test_borrow_shows_origin_quota_borrower_and_schedule():
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
    )
    row = latest(r, "B1")
    assert row["lender"] == "架构部"
    assert row["borrower"] == "制造部"
    assert row["origin_quota"] == 10          # 出借时的原固定配额
    assert row["seats"] == 3
    assert row["start"] == "2026-11-18 01:00"
    assert row["planned_return"] == "2026-11-18 09:00"  # 到期安排
    assert row["actual_return"] == "2026-11-18 09:00"
    assert row["reason"] == "BORROW_GRANTED"
    assert row["status"] == "EFFECTIVE"
    assert not any(a["borrow_id"] == "B1" and a["reason"] == "EXPIRED" for a in r["audit"])
    granted = next(h for h in r["borrow_history"] if h["rev"] == 1)
    assert granted["reason"] == "BORROW_GRANTED" and granted["status"] == "EFFECTIVE"


def test_borrow_still_effective_when_within_data_horizon():
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T18:00:00+00:00", "B1"),
        session("P", "制造部", "2026-11-18T02:00:00+00:00", "2026-11-18T12:00:00+00:00"),
    )
    row = latest(r, "B1")
    assert row["status"] == "EFFECTIVE" and row["reason"] == "BORROW_GRANTED"
    assert r["report"]["metrics"]["当前生效"] == 1


def test_origin_quota_is_snapshot_not_live_value():
    # 配额后续调高，借用仍记录出借当时的原配额
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 2, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
        quota("P", "架构部", 14, t="2026-11-18T03:00:00+00:00", rev=2),
    )
    assert latest(r, "B1")["origin_quota"] == 10


# ---------- 空闲配额与占席 ----------

def test_borrow_rejected_when_idle_insufficient():
    r = run(
        quota("P", "架构部", 4),
        borrow("P", "架构部", "制造部", 5, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
    )
    assert "BORROW_NO_IDLE" in reasons(r)
    assert r["report"]["rows"] == []


def test_concurrent_borrows_share_idle_pool_and_segment_accounting():
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 6, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
        borrow("P", "架构部", "研发部", 5, "2026-11-18T02:00:00+00:00", "2026-11-18T08:00:00+00:00", "B2"),
    )
    # 第二笔时只剩 4 个空闲席
    assert "BORROW_NO_IDLE" in reasons(r)
    assert {row["borrow_id"] for row in r["report"]["rows"]} == {"B1"}
    mid = [s for s in r["segments"] if s["dept"] == "架构部" and s["lent"] == 6]
    assert mid and all(s["available"] == 4 for s in mid)
    mfg = [s for s in r["segments"] if s["dept"] == "制造部" and s["borrowed"] == 3]
    assert not mfg  # 被拒借用不占席
    held = [s for s in r["segments"] if s["dept"] == "制造部" and s["borrowed"] == 6]
    assert held


def test_borrow_without_lender_quota_is_quarantined():
    r = run(borrow("P", "架构部", "制造部", 1, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"))
    assert "QUOTA_NOT_FOUND" in reasons(r)


def test_self_borrow_quarantined():
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "架构部", 1, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
    )
    assert "BORROW_SELF" in reasons(r)


# ---------- 收回：生产中顺延，不打断已开始的会话 ----------

def test_recall_during_production_defers_until_session_end():
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
        session("P", "制造部", "2026-11-18T02:00:00+00:00", "2026-11-18T06:00:00+00:00", seats=2),
        term("recall", "P", "架构部", "B1", "2026-11-18T04:00:00+00:00", 2, 3),
    )
    row = latest(r, "B1")
    assert row["reason"] == "RECALL_DEFERRED"
    assert row["requested_at"] == "2026-11-18 04:00"
    assert row["actual_return"] == "2026-11-18 06:00"      # 顺延到会话结束
    assert row["planned_return"] == "2026-11-18 09:00"      # 原到期安排不变
    # 04:00-06:00 仍由借入方占席
    occupied = [s for s in r["segments"] if s["dept"] == "制造部" and s["borrowed"] == 3]
    assert any(s["a"] < _ts("2026-11-18T05:00:00+00:00") < s["b"] for s in occupied)


def test_recall_defer_never_exceeds_planned_end():
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T05:00:00+00:00", "B1"),
        session("P", "制造部", "2026-11-18T02:00:00+00:00", "2026-11-18T08:00:00+00:00"),
        term("recall", "P", "架构部", "B1", "2026-11-18T03:00:00+00:00", 2, 3),
    )
    assert latest(r, "B1")["actual_return"] == "2026-11-18 05:00"


def test_recall_when_idle_takes_effect_immediately():
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
        session("P", "制造部", "2026-11-18T02:00:00+00:00", "2026-11-18T03:30:00+00:00"),
        term("recall", "P", "架构部", "B1", "2026-11-18T04:00:00+00:00", 2, 3),
    )
    row = latest(r, "B1")
    assert row["reason"] == "RECLAIMED"
    assert row["actual_return"] == "2026-11-18 04:00"


def test_session_starting_after_recall_does_not_block():
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
        session("P", "制造部", "2026-11-18T05:00:00+00:00", "2026-11-18T07:00:00+00:00"),
        term("recall", "P", "架构部", "B1", "2026-11-18T04:00:00+00:00", 2, 3),
    )
    assert latest(r, "B1")["reason"] == "RECLAIMED"


# ---------- 归还与历史版本保留 ----------

def test_return_creates_new_version_and_keeps_old_one():
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
        term("return", "P", "制造部", "B1", "2026-11-18T05:00:00+00:00", 2, 3),
    )
    versions = [h for h in r["borrow_history"] if h["borrow_id"] == "B1"]
    assert [h["rev"] for h in versions] == [1, 2]
    old, new = versions
    assert old["status"] == "SUPERSEDED" and old["reason"] == "BORROW_GRANTED"
    assert old["actual_return"] == "2026-11-18 09:00"     # 旧版到期安排原样保留
    assert new["status"] == "COMPLETED" and new["reason"] == "RETURNED"
    assert new["actual_return"] == "2026-11-18 05:00"
    assert new["origin_quota"] == old["origin_quota"] == 10
    # 对外视图只暴露最新版本
    assert latest(r, "B1")["rev"] == 2


def test_stale_termination_is_quarantined_and_never_rewrites_effective_borrow():
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
        term("return", "P", "制造部", "B1", "2026-11-18T05:00:00+00:00", 2, 3),
        term("recall", "P", "架构部", "B1", "2026-11-18T06:00:00+00:00", 2, 3),  # 同版补传
        term("recall", "P", "架构部", "B1", "2026-11-18T06:30:00+00:00", 1, 3),  # 更旧版本
    )
    assert reasons(r).count("STALE_VERSION") == 2
    row = latest(r, "B1")
    assert row["rev"] == 2 and row["reason"] == "RETURNED"
    assert [h["rev"] for h in r["borrow_history"] if h["borrow_id"] == "B1"] == [1, 2]


def test_termination_after_expiry_does_not_touch_history():
    r = run(
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T03:00:00+00:00", "B1"),
        term("return", "P", "制造部", "B1", "2026-11-18T05:00:00+00:00", 2, 3),
    )
    assert {a["reason"] for a in r["audit"] if a["borrow_id"] == "B1"} == {"EXPIRED", "TERMINATION_LAPSED"}
    assert latest(r, "B1")["reason"] == "BORROW_GRANTED"
    assert all(h["rev"] == 1 for h in r["borrow_history"] if h["borrow_id"] == "B1")


def test_termination_unknown_borrow_quarantined():
    r = run(quota("P", "架构部", 10), term("return", "P", "制造部", "NOPE", "2026-11-18T05:00:00+00:00", 2))
    assert "BORROW_NOT_FOUND" in reasons(r)


# ---------- 重跑幂等：不新增借用、不重复占席 ----------

def test_replay_same_batch_is_identical():
    text = "\n".join([
        HEADER,
        quota("P", "架构部", 10),
        borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
        term("recall", "P", "架构部", "B1", "2026-11-18T04:00:00+00:00", 2, 3),
    ]) + "\n"
    first, second = evaluate(text), evaluate(text)
    assert first["report"] == second["report"]
    assert first["borrow_history"] == second["borrow_history"]
    assert first["segments"] == second["segments"]
    assert second["report"]["metrics"]["借用关系"] == 1
    assert second["report"]["metrics"]["历史版本"] == 2


def test_duplicate_event_in_batch_is_quarantined_not_double_counted():
    line = borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1")
    r = run(quota("P", "架构部", 10), line, line)
    assert reasons(r) == ["DUPLICATE_EVENT"]
    assert len(r["report"]["rows"]) == 1


def test_persistent_pool_run_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import store as st
    st.DATA_DIR = tmp_path
    text = sample_text()
    a, b = st.save_pool(text, evaluate(text)), st.save_pool(text, evaluate(text))
    assert a["id"] == b["id"] and a["digest"] == b["digest"]
    assert len(st.pool_history()) == 1


# ---------- 补传乱序与边界 ----------

def test_out_of_order_events_replay_by_instant():
    rows = [
        term("recall", "P", "架构部", "B1", "2026-11-18T04:00:00+00:00", 2, 3),
        session("P", "制造部", "2026-11-18T02:00:00+00:00", "2026-11-18T06:00:00+00:00"),
        borrow("P", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
        quota("P", "架构部", 10),
    ]
    r = run(*rows)
    assert latest(r, "B1")["reason"] == "RECALL_DEFERRED"


def test_pools_are_isolated():
    r = run(
        quota("P1", "架构部", 2),
        quota("P2", "架构部", 10),
        # 同一借用号在两个池各自独立裁决：P1 空闲不足被拒，P2 成立
        borrow("P1", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
        borrow("P2", "架构部", "制造部", 3, "2026-11-18T01:00:00+00:00", "2026-11-18T09:00:00+00:00", "B1"),
    )
    assert "BORROW_NO_IDLE" in reasons(r)
    assert len(r["report"]["rows"]) == 1
    assert r["report"]["rows"][0]["pool"] == "P2"
    # 席位段不跨池串账
    assert {s["pool"] for s in r["segments"]} == {"P1", "P2"}
    p1 = [s for s in r["segments"] if s["pool"] == "P1" and s["dept"] == "架构部"]
    assert all(s["lent"] == 0 and s["available"] == 2 for s in p1)


def test_timezone_offsets_normalized_to_utc():
    r = run(
        quota("P", "架构部", 10, t="2026-11-18T08:00:00+08:00"),
        borrow("P", "架构部", "制造部", 1, "2026-11-18T09:00:00+08:00", "2026-11-18T17:00:00+08:00", "B1"),
    )
    row = latest(r, "B1")
    assert row["start"] == "2026-11-18 01:00"
    assert row["planned_return"] == "2026-11-18 09:00"


def test_bad_rows_quarantined_and_balance_tallies():
    r = run(
        quota("P", "架构部", 10),
        "X1,P,制造部,wat,2026-11-18T01:00:00+00:00,,1,1,",          # 非法 kind
        "X2,P,制造部,borrow,not-a-time,2026-11-18T09:00:00+00:00,1,1,架构部",  # 时间错误
        "X3,P,制造部,borrow,2026-11-18T01:00:00+00:00,2026-11-18T09:00:00+00:00,0,1,架构部",  # 版本非正
        "",
    )
    assert set(reasons(r)) == {"KIND", "TIME", "VERSION"}
    b = r["balance"]
    assert b["input"] == b["accepted"] + b["quarantined"] + b["blank"]
    assert b["blank"] == 1 and b["quarantined"] == 3


def test_sample_pack_is_coherent():
    r = evaluate(sample_text())
    ids = {row["borrow_id"] for row in r["report"]["rows"]}
    assert {"B-ARCH-MFG-1", "B-MFG-RND-1"} <= ids
    assert "B-ARCH-RND-1" not in ids                      # 空闲不足被隔离
    deferred = latest(r, "B-ARCH-MFG-1")
    assert deferred["reason"] == "RECALL_DEFERRED"
    assert deferred["origin_quota"] == 10 and deferred["borrower"] == "制造部"
    expired = latest(r, "B-MFG-RND-1")
    assert expired["reason"] == "BORROW_GRANTED" and expired["status"] == "EFFECTIVE"
    assert "BORROW_NO_IDLE" in reasons(r) and "DUPLICATE_EVENT" in reasons(r)
    assert r["balance"]["input"] == r["balance"]["accepted"] + r["balance"]["quarantined"]


def _ts(text):
    from app.timeops import instant
    return instant(text)
