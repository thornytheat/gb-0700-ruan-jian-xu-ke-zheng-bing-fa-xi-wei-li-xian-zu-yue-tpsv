"""M8 共享席位池：部门配额与跨部门借用裁决。

事件以补传方式到达，按事件时刻与版本重放：
- quota    部门固定配额（同部门同版本冲突隔离，高版本覆盖）
- borrow   出借方把空闲配额借给借入方，生成带到期安排的借用关系
- return   借入方提前归还
- recall   出借方临时收回；借入方正在生产时顺延到会话结束，不打断已开始的会话
- session  部门生产会话，用于判断借入方是否正在占席生产

所有借用关系只追加、不改写：归还/收回产生新版本记录，旧版本标记 SUPERSEDED 后原样保留。
同一批事件重放走纯函数重放 + 事件号去重，不新增借用、不重复占席。
"""
from app.reader import read_rows
from app.report import report, quarantine
from app.timeops import instant, display_full
from app.numbers import number
from app.errors import BusinessError

FIELDS = "event_id,pool,dept,kind,start,end,version,seats,ref".split(",")
KINDS = {"quota", "borrow", "return", "recall", "session"}


def _parse_seats(text):
    value = number(text)
    if value < 0 or value != value.to_integral_value():
        raise BusinessError("SEATS", "席位须为非负整数")
    return int(value)


def normalize(ctx):
    rows, bad, blank = read_rows(ctx["text"], FIELDS)
    ctx["quarantine"].extend(bad)
    ctx["blank_rows"] = blank
    ctx["input_count"] = len(rows) + len(bad) + blank
    out, seen = [], set()
    for r in rows:
        if not r["event_id"]:
            quarantine(ctx, r, "ID_REQUIRED")
            continue
        if (r["pool"], r["event_id"]) in seen:
            quarantine(ctx, r, "DUPLICATE_EVENT")
            continue
        try:
            if r["kind"] not in KINDS:
                raise BusinessError("KIND", "事件类型无效")
            x = dict(r)
            x["rev"] = int(r["version"])
            if x["rev"] < 1:
                raise BusinessError("VERSION", "版本须为正")
            x["seats_n"] = _parse_seats(r["seats"])
            x["a"] = instant(r["start"])
            x["b"] = instant(r["end"]) if r["end"] else x["a"]
            if r["kind"] in {"borrow", "session"} and x["b"] <= x["a"]:
                raise BusinessError("INTERVAL", "区间无效")
            if r["kind"] in {"borrow", "return", "recall"} and not r["ref"]:
                raise BusinessError("REF_REQUIRED", "借用事件须填写对方部门")
            seen.add((r["pool"], r["event_id"]))
            out.append(x)
        except (BusinessError, ValueError) as e:
            quarantine(ctx, r, getattr(e, "code", "VERSION"))
    # 乱序补传：按事件端点重放，同刻按版本与事件号稳定排序
    out.sort(key=lambda r: (r["a"], r["rev"], r["event_id"]))
    ctx["pool_events"] = out
    return out


def _quota_at(timeline, dept, t):
    """部门在 t 时刻生效的固定配额；无任何生效配额时为 0。"""
    current = 0
    for r in timeline:
        if r["dept"] == dept and r["a"] <= t:
            current = r["seats_n"]
    return current


def _active_borrows(ledger, t, pool=None):
    """t 时刻仍在占用出借方席位的有效借用（每个借用号只看最新版本）。"""
    latest = {}
    for row in ledger:
        if row["status"] == "SUPERSEDED":
            continue
        latest[row["borrow_id"]] = row
    rows = latest.values()
    if pool is not None:
        rows = (row for row in rows if row["pool"] == pool)
    return [row for row in rows if row["a"] <= t < row["actual_end"]]


def _idle_seats(ledger, timeline, pool, dept, t):
    quota = _quota_at([r for r in timeline if r["pool"] == pool], dept, t)
    lent = sum(row["seats"] for row in _active_borrows(ledger, t, pool) if row["lender"] == dept)
    return quota - lent


def _latest(ledger, pool, borrow_id):
    rows = [row for row in ledger if row["pool"] == pool and row["borrow_id"] == borrow_id and row["status"] != "SUPERSEDED"]
    return rows[-1] if rows else None


def build(ctx):
    events = ctx["pool_events"]
    # 数据视野：区间事件取最晚端点，时刻事件（归还/收回）取发生时刻；quota 的 b=a 不参与
    horizon = max(
        [r["b"] for r in events if r["kind"] in {"borrow", "session"}]
        + [r["a"] for r in events if r["kind"] in {"return", "recall"}],
        default=0,
    )
    timeline = [r for r in events if r["kind"] == "quota"]
    sessions = [r for r in events if r["kind"] == "session"]
    ledger, audit = [], []

    for ev in events:
        kind = ev["kind"]
        if kind in {"quota", "session"}:
            continue
        if kind == "borrow":
            lender, borrower = ev["dept"], ev["ref"]
            if lender == borrower:
                quarantine(ctx, ev, "BORROW_SELF")
                continue
            if not any(r["dept"] == lender and r["a"] <= ev["a"] for r in timeline):
                quarantine(ctx, ev, "QUOTA_NOT_FOUND")
                continue
            idle = _idle_seats(ledger, timeline, ev["pool"], lender, ev["a"])
            if ev["seats_n"] <= 0:
                quarantine(ctx, ev, "BORROW_EMPTY")
                continue
            if idle < ev["seats_n"]:
                quarantine(ctx, ev, "BORROW_NO_IDLE")
                continue
            ledger.append({
                "borrow_id": ev["event_id"], "pool": ev["pool"], "rev": 1,
                "lender": lender, "borrower": borrower, "seats": ev["seats_n"],
                "origin_quota": _quota_at([r for r in timeline if r["pool"] == ev["pool"]], lender, ev["a"]),
                "a": ev["a"], "planned_end": ev["b"], "actual_end": ev["b"],
                "requested_at": ev["a"], "reason": "BORROW_GRANTED",
                "line": ev["line"], "status": "EFFECTIVE",
            })
            continue

        # return / recall 都针对已生效借用
        current = _latest(ledger, ev["pool"], ev["ref"])
        if current is None:
            quarantine(ctx, ev, "BORROW_NOT_FOUND")
            continue
        if ev["rev"] <= current["rev"]:
            quarantine(ctx, ev, "STALE_VERSION")
            continue
        if ev["a"] >= current["actual_end"]:
            # 借用已自然到期，补传的归还/收回不再改写历史
            audit.append({"borrow_id": current["borrow_id"], "reason": "TERMINATION_LAPSED", "line": ev["line"]})
            continue

        if kind == "return":
            actual_end, reason = ev["a"], "RETURNED"
        else:
            blocking = [
                s for s in sessions
                if s["dept"] == current["borrower"] and s["pool"] == ev["pool"]
                and s["a"] < ev["a"] < s["b"]
            ]
            if blocking:
                # 借入方正在生产：顺延到最后一个进行中会话结束，但不越过原到期安排
                actual_end = min(current["planned_end"], max(s["b"] for s in blocking))
                reason = "RECALL_DEFERRED"
            else:
                actual_end, reason = ev["a"], "RECLAIMED"

        # 旧版借用关系原样保留，只追加新版本
        idx = next(i for i, row in enumerate(ledger) if row is current)
        old = dict(current)
        old["status"] = "SUPERSEDED"
        ledger[idx] = old
        ledger.append({
            "borrow_id": current["borrow_id"], "pool": current["pool"], "rev": ev["rev"],
            "lender": current["lender"], "borrower": current["borrower"], "seats": current["seats"],
            "origin_quota": current["origin_quota"],
            "a": current["a"], "planned_end": current["planned_end"], "actual_end": actual_end,
            "requested_at": ev["a"], "reason": reason,
            "line": ev["line"], "status": "EFFECTIVE",
        })
        audit.append({"borrow_id": current["borrow_id"], "reason": reason, "line": ev["line"]})

    # 自然到期：半开区间 [a,b) 约定，actual_end 等于视野时仍在视野内，纯重放结果确定。
    # 记录上的 reason 保留其来源（BORROW_GRANTED/RETURNED/…），EXPIRED 只进审计轨迹。
    for row in ledger:
        if row["status"] == "EFFECTIVE" and row["actual_end"] < horizon:
            row["status"] = "COMPLETED"
            audit.append({"borrow_id": row["borrow_id"], "reason": "EXPIRED", "line": row["line"]})

    ctx["borrow_ledger"] = ledger
    ctx["borrow_audit"] = audit
    ctx["pool_horizon"] = horizon
    return ledger


def _sweep(ctx):
    events = ctx["pool_events"]
    ledger = ctx["borrow_ledger"]
    timeline = [r for r in events if r["kind"] == "quota"]
    segments = []
    for pool in sorted({r["pool"] for r in events}):
        depts = sorted(
            {r["dept"] for r in timeline if r["pool"] == pool}
            | {r["lender"] for r in ledger if r["pool"] == pool}
            | {r["borrower"] for r in ledger if r["pool"] == pool}
        )
        pool_timeline = [r for r in timeline if r["pool"] == pool]
        points = sorted(
            {r["a"] for r in pool_timeline}
            | {r[k] for r in ledger if r["pool"] == pool and r["status"] != "SUPERSEDED" for k in ("a", "actual_end")}
        )
        if len(points) < 2:
            # 借用全部被拒的池也要输出配额段，保证占席账可对
            points = points + [ctx["pool_horizon"]] if points else [0, ctx["pool_horizon"]]
        for a, b in zip(points, points[1:]):
            active = _active_borrows(ledger, a, pool)
            for dept in depts:
                quota = _quota_at(pool_timeline, dept, a)
                lent = sum(r["seats"] for r in active if r["lender"] == dept)
                borrowed = sum(r["seats"] for r in active if r["borrower"] == dept)
                segments.append({
                    "pool": pool, "dept": dept, "a": a, "b": b,
                    "quota": quota, "lent": lent, "borrowed": borrowed,
                    "available": quota - lent,
                })
    ctx["pool_segments"] = segments
    return segments


def _view(row):
    return dict(row, start=display_full(row["a"]), planned_return=display_full(row["planned_end"]),
                actual_return=display_full(row["actual_end"]), requested_at=display_full(row["requested_at"]))


def apply(ctx):
    normalize(ctx)
    build(ctx)
    _sweep(ctx)
    ledger = ctx["borrow_ledger"]
    latest = [_view(row) for row in ledger if row["status"] != "SUPERSEDED"]
    history = [_view(row) for row in ledger]
    ctx["balance"] = {
        "input": ctx["input_count"],
        "accepted": ctx["input_count"] - len(ctx["quarantine"]) - ctx["blank_rows"],
        "quarantined": len(ctx["quarantine"]),
        "blank": ctx["blank_rows"],
    }
    metrics = {
        "借用关系": len({r["borrow_id"] for r in ledger}),
        "当前生效": sum(r["status"] == "EFFECTIVE" for r in ledger),
        "历史版本": len(ledger),
        "收回顺延": sum(r["reason"] == "RECALL_DEFERRED" for r in ledger),
        "隔离": len(ctx["quarantine"]),
    }
    return report(latest, **metrics), history
