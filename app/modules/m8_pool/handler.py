from fastapi import APIRouter, Body
from app.errors import BusinessError, object_payload
from app.modules.m8_pool.service import execute, evaluate

router = APIRouter()


def _text(payload):
    text = payload.get("text")
    if not isinstance(text, str):
        raise BusinessError("TEXT", "输入须为文本")
    return text


@router.post("/api/8")
def calculate(payload: object = Body(default=None)):
    return execute(_text(object_payload(payload)))


@router.get("/api/8")
def sample():
    from app.modules.m8_pool.sample import sample_text
    return execute(sample_text())


@router.post("/api/8/preview")
def preview(payload: object = Body(default=None)):
    return evaluate(_text(object_payload(payload)))


@router.get("/api/8/runs")
def runs():
    from app import store
    return store.pool_history()


@router.get("/api/8/runs/{ident}")
def detail(ident: str):
    from app import store
    return store.get_pool(ident)
