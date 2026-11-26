"""IoT设备数据接入与协议适配平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import cross_module, resource, retention
from app.store import store

app = FastAPI(title="IoT设备数据接入与协议适配平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()


# 时序存储开通一致性流程端点先于通用资源路由注册：
# /api/ts_store/estimates 这类静态路径不能被 /api/{module}/{entry_id} 抢先匹配。
for prefix in (
    "/api/retention", "/api/ts-store", "/api/ts_store",
    "/api/retention-flow", "/api/cross/flow3", "/api",
):
    app.include_router(retention.router, prefix=prefix)

# 资源路由含 /api/{module}，必须放在 health / overview 之后注册，
# 否则固定路径会被动态路径抢先匹配。
for module in (cross_module, resource):
    app.include_router(module.router)
