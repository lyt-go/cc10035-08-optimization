"""时序存储策略开通一致性接口。

同一组流程端点会挂在多组路径前缀下（``/api/retention``、``/api/ts-store``、
``/api/ts_store``），并同时兼容 JSON body、``{"values": {...}}`` 信封与 query
参数，方便页面、脚本和失联重试以同一份估算单号幂等调用。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.services import service_for

router = APIRouter(tags=["时序存储开通一致性"])


def ts_store_service():
    return service_for("ts_store")


async def merged_params(request: Request) -> dict[str, Any]:
    data: dict[str, Any] = dict(request.query_params)
    # 幂等键也允许放在请求头
    idem_header = request.headers.get("Idempotency-Key")
    if idem_header and not data.get("idempotency_key"):
        data["idempotency_key"] = idem_header
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        values = body.get("values")
        if isinstance(values, dict):
            for key, value in values.items():
                data.setdefault(key, value)
        for key, value in body.items():
            if key == "values":
                continue
            data[key] = value
    return data


# ---------------- 测点依据 / 采样参数修订 ----------------

@router.get("/points")
def list_points():
    from app.store import store
    service = ts_store_service()
    rows = store.rows("ts_point")
    items = []
    for row in rows:
        basis = service.point_basis(row)
        items.append({**row, "basis": basis, "server_basis": basis})
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/points/{ref}/basis")
@router.get("/basis/{ref}")
def point_basis(ref: str):
    basis = ts_store_service().point_basis(ref)
    if basis is None:
        return {"ok": False, "message": f"测点 {ref} 不存在"}
    return {"ok": True, "basis": basis, "server_basis": basis, "有效依据": basis}


@router.post("/points/{ref}/sampling")
async def update_sampling(ref: str, request: Request):
    params = await merged_params(request)
    interval = params.pop("sampling_interval", None) or params.pop("interval", None) \
        or params.pop("采集间隔", None) or params.pop("sampling_interval_seconds", None)
    return ts_store_service().update_point_sampling(ref, interval, **params)


@router.post("/sampling")
async def update_sampling_by_body(request: Request):
    params = await merged_params(request)
    ref = params.get("point_id") or params.get("测点编号") or params.get("point_ref") or params.get("ref")
    interval = params.get("sampling_interval") or params.get("采集间隔") \
        or params.get("sampling_interval_seconds") or params.get("interval")
    return ts_store_service().update_point_sampling(ref, interval, **params)


# ---------------- 保留估算单 ----------------

@router.post("/estimates")
async def create_estimate(request: Request):
    params = await merged_params(request)
    return ts_store_service().create_retention_estimate(**params)


@router.get("/estimates")
def list_estimates(point_id: str | None = None, 测点编号: str | None = None):
    return ts_store_service().list_retention_estimates(point_id or 测点编号)


@router.get("/estimates/{ref}")
def get_estimate(ref: str):
    return ts_store_service().get_retention_estimate(ref)


@router.post("/estimates/{ref}/check-capacity")
@router.post("/estimates/{ref}/check")
async def check_capacity(ref: str, request: Request):
    params = await merged_params(request)
    return ts_store_service().check_capacity(ref, **params)


@router.post("/estimates/{ref}/confirm")
@router.post("/estimates/{ref}/confirm-capacity")
async def confirm_estimate(ref: str, request: Request):
    params = await merged_params(request)
    return ts_store_service().confirm_capacity(ref, **params)


@router.post("/estimates/{ref}/transfer")
async def transfer_estimate(ref: str, request: Request):
    params = await merged_params(request)
    return ts_store_service().transfer_to_strategy(ref, **params)


@router.post("/estimates/{ref}/activate")
async def activate_estimate(ref: str, request: Request):
    params = await merged_params(request)
    params["estimate_ref"] = ref
    return ts_store_service().activate_from_estimate(**params)


@router.post("/estimates/{ref}/claim")
async def claim_estimate(ref: str, request: Request):
    params = await merged_params(request)
    params["estimate_ref"] = ref
    return ts_store_service().activate_from_estimate(**params)


@router.post("/estimates/{ref}/renew")
async def renew_estimate(ref: str, request: Request):
    params = await merged_params(request)
    return ts_store_service().renew_estimate(ref, **params)


# 不带 estimates 前缀的短动作别名（部分页面/脚本可能直接按单号操作）
@router.post("/estimate")
async def estimate_short_create(request: Request):
    params = await merged_params(request)
    return ts_store_service().create_retention_estimate(**params)


@router.post("/activate")
async def activate_short(request: Request):
    params = await merged_params(request)
    ref = params.get("estimate_no") or params.get("estimate_ref") or params.get("ref") \
        or params.get("confirmation_no") or params.get("单号")
    params["estimate_ref"] = ref
    return ts_store_service().activate_from_estimate(**params)


@router.post("/transfer")
async def transfer_short(request: Request):
    params = await merged_params(request)
    ref = params.get("estimate_no") or params.get("estimate_ref") or params.get("ref") or params.get("单号")
    return ts_store_service().transfer_to_strategy(ref, **params)


@router.post("/confirm")
async def confirm_short(request: Request):
    params = await merged_params(request)
    ref = params.get("estimate_no") or params.get("estimate_ref") or params.get("ref") or params.get("单号")
    return ts_store_service().confirm_capacity(ref, **params)


@router.post("/renew")
async def renew_short(request: Request):
    params = await merged_params(request)
    ref = params.get("estimate_no") or params.get("estimate_ref") or params.get("ref") or params.get("单号")
    return ts_store_service().renew_estimate(ref, **params)


# ---------------- 容量确认单 / 待核项 / 总览 ----------------

@router.get("/confirmations")
@router.get("/capacity-confirmations")
def list_confirmations(point_id: str | None = None, 测点编号: str | None = None):
    return ts_store_service().list_confirmations(point_id or 测点编号)


@router.get("/confirmations/{ref}")
@router.get("/capacity-confirmations/{ref}")
def get_confirmation(ref: str):
    return ts_store_service().get_confirmation(ref)


@router.get("/pending")
@router.get("/pending-checks")
def list_pending(point_id: str | None = None, status: str | None = None):
    return ts_store_service().list_pending_checks(point_id, status)


@router.get("/overview")
def retention_overview(point_id: str | None = None):
    return ts_store_service().retention_overview(point_id)


@router.get("/strategies/current")
def current_strategy(point_id: str):
    return ts_store_service().current_strategy(point_id)
