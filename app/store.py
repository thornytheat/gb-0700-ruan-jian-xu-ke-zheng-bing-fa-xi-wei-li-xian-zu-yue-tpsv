import sqlite3, json, hashlib
from datetime import datetime, timezone
from app.config import DATA_DIR, RULE_VERSION
from app.errors import BusinessError

def connect():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA_DIR/"runs.sqlite", timeout=15)
    db.row_factory=sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT '待复核', created TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS pool_runs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, created TEXT NOT NULL)")
    return db

def save(text, result):
    digest=hashlib.sha256((RULE_VERSION+"\n"+text).encode()).hexdigest()
    ident=digest[:16]
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,digest,result,created) VALUES(?,?,?,?)",(ident,digest,json.dumps(dict(result,rule_version=RULE_VERSION),ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
    return get(ident)

def get(ident):
    with connect() as db:
        row=db.execute("SELECT * FROM runs WHERE id=?",(ident,)).fetchone()
    if row is None: raise BusinessError("NOT_FOUND","批次不存在",404)
    item=dict(row);item["result"]=json.loads(item["result"]);item["rule_version"]=item["result"].get("rule_version","2026.11.18.1")
    return item

def history():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT id,revision,decision,created FROM runs ORDER BY created DESC LIMIT 40")]

def review(ident,revision,decision):
    if not isinstance(decision,str) or decision not in {"通过","退回"}: raise BusinessError("DECISION","复核决定无效")
    current=get(ident)
    if decision=="通过" and current["result"].get("blocked"):
        raise BusinessError("BLOCKED","存在阻断项，请先修正数据",409)
    with connect() as db:
        changed=db.execute("UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?",(decision,ident,revision)).rowcount
        if not changed: raise BusinessError("STALE","版本已更新，请刷新",409)
    return get(ident)


POOL_RULE_VERSION = "2026.11.18.8"


def save_pool(text, result):
    # 同一规则版本与输入摘要只产生一条批次；重放不新增借用、不重复占席
    digest = hashlib.sha256((POOL_RULE_VERSION + "\n" + text).encode()).hexdigest()
    ident = "pool-" + digest[:16]
    from app.report import plain
    payload = plain(dict(result, rule_version=POOL_RULE_VERSION))
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO pool_runs(id,digest,result,created) VALUES(?,?,?,?)",
            (ident, digest, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
    return get_pool(ident)


def get_pool(ident):
    with connect() as db:
        row = db.execute("SELECT * FROM pool_runs WHERE id=?", (ident,)).fetchone()
    if row is None: raise BusinessError("NOT_FOUND", "席位池批次不存在", 404)
    item = dict(row)
    item["result"] = json.loads(item["result"])
    item["rule_version"] = item["result"].get("rule_version", POOL_RULE_VERSION)
    return item


def pool_history():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT id,digest,created FROM pool_runs ORDER BY created DESC LIMIT 40")]
