from app.modules.m8_pool import rules


def evaluate(text):
    ctx = {"text": text, "quarantine": [], "reports": []}
    view, history = rules.apply(ctx)
    return {
        "module": 8,
        "report": view,
        "borrow_history": history,
        "segments": ctx["pool_segments"],
        "audit": ctx["borrow_audit"],
        "quarantine": ctx["quarantine"],
        "balance": ctx["balance"],
        "horizon": ctx["pool_horizon"],
    }


def execute(text):
    from app import store
    return store.save_pool(text, evaluate(text))
