"""时序存储领域服务：时序数据的存储策略、压缩与生命周期管理。

提供存储策略优化、压缩比估算、冷热数据分层计算等真实业务能力。

策略开通一致性流程（保留估算单 → 容量确认单 → 存储策略）：

1. ``create_retention_estimate`` 按测点**当前真实采样间隔**、策略期限与统一
   容量口径生成「保留估算单」，并把整组输入与结果冻结在单据快照里；
2. ``confirm_capacity`` / ``transfer_to_strategy`` 核对容量并转办，产出
   「容量确认单」，确认单同样冻结当时的间隔与估算值，可用于后续启用；
3. 单据上的采样间隔与测点当前间隔不一致时，后续开通动作返回
   ``conflict`` + 服务端当前有效依据（``server_basis``），并生成/复用一张
   可续办的「待核项」；``renew_estimate`` 按服务端当前参数续办出新一轮单据，
   旧单据与历史确认单保留当时的输入和值，绝不混算；
4. ``activate_from_estimate`` 以估算单号（或显式幂等键）去重，失联重试 /
   重复领取都只会返回同一份存储策略，不会重复落库。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


# ---- 统一容量口径（整轮估算必须共用同一组常量） -------------------------
BYTES_PER_POINT = 32
SECONDS_PER_DAY = 86400
DEFAULT_RETENTION_DAYS = 30
DEFAULT_COMPRESSION = "gzip"
COMPRESSION_RATIOS = {"gzip": 5, "snappy": 2, "lz4": 3, "zstd": 6, "none": 1}

# ---- 流程单据表（放在统一内存仓库中，便于各 Service 实例与测试共享） -----
ESTIMATE_TABLE = "ts_retention_estimate"
CONFIRM_TABLE = "ts_capacity_confirmation"
PENDING_TABLE = "ts_pending_check"
ACTIVATION_TABLE = "ts_activation"


def parse_interval_seconds(value: Any, default: int = 10) -> int:
    """把 ``10s`` / ``10秒`` / ``30`` / ``0.5min`` 等采样间隔解析成秒。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        seconds = int(value)
        return seconds if seconds > 0 else default
    text = str(value).strip().lower()
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return default
    number = float(match.group(1))
    if any(unit in text for unit in ("分钟", "min", "minute")):
        number *= 60
    elif any(unit in text for unit in ("小时", "hour", "h")) and "秒" not in text:
        number *= 3600
    seconds = int(round(number))
    return seconds if seconds > 0 else default


def parse_days(value: Any, default: int = DEFAULT_RETENTION_DAYS) -> int:
    """解析保留/期限天数，兼容 ``30`` / ``"30天"`` / ``"30d"``。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return max(1, int(value))
    match = re.search(r"(\d+)", str(value))
    return max(1, int(match.group(1))) if match else default


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0) or 0) for row in rows), default=0) + 1


def _pick(source: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in source and source[name] not in (None, ""):
            return source[name]
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "否")


class TsStoreService(BaseModuleService):
    MODULE = "ts_store"
    ENTITY = "存储策略"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["策略编号", "测点编号", "存储引擎", "压缩算法", "降采样规则", "保留天数", "分段策略", "冷热分层"]
    REQUIRED_FIELDS = ["策略编号", "测点编号", "存储引擎"]
    STATUS_ORDER = ["草稿", "待验证", "已生效", "已停用"]
    ACTION_RULES = {"提交验证": "待验证", "标记生效": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    # ===================================================================
    # 既有能力（保持兼容）
    # ===================================================================

    def estimate_storage(self, point_id: str, data_points_per_day: int = 86400) -> dict[str, Any]:
        """存储空间估算：根据采集间隔和保留天数预估存储需求。

        业务流程：
        1. 查找测点的存储策略
        2. 根据采集间隔计算日数据量
        3. 乘以保留天数
        4. 考虑压缩比
        """
        rows = store.rows(self.MODULE)
        strategy = None
        for row in rows:
            if row.get("测点编号") == point_id and row.get("status") == "已生效":
                strategy = row
                break
        if strategy is None:
            return {"ok": False, "message": f"测点 {point_id} 无已生效的存储策略"}

        try:
            retention_days = int(strategy.get("保留天数", "30"))
        except (ValueError, TypeError):
            retention_days = 30

        compressor = strategy.get("压缩算法", "none")
        compression_ratio = {"gzip": 5, "snappy": 2, "lz4": 3, "zstd": 6, "none": 1}.get(
            compressor, 2
        )

        # 每条记录约 32 字节 (timestamp + value)
        bytes_per_point = 32
        raw_bytes = data_points_per_day * bytes_per_point * retention_days
        compressed_bytes = raw_bytes / compression_ratio

        cold_days = int(str((strategy.get("冷热分层", "30d_hot"))).replace("d_hot", "") or 30)
        hot_bytes = data_points_per_day * bytes_per_point * cold_days / compression_ratio

        return {
            "ok": True,
            "point_id": point_id,
            "retention_days": retention_days,
            "compression_algorithm": compressor,
            "compression_ratio": compression_ratio,
            "raw_bytes_estimate": raw_bytes,
            "compressed_bytes_estimate": compressed_bytes,
            "hot_storage_bytes": hot_bytes,
            "cold_storage_bytes": compressed_bytes - hot_bytes,
            "storage_engine": strategy.get("存储引擎"),
        }

    def optimize_retention(self, min_free_space_mb: float = 1024) -> dict[str, Any]:
        """保留策略优化：根据预估存储空间调整各测点的保留天数。

        超过空间限制时，按采集间隔降序缩减保留天数。
        """
        rows = store.rows(self.MODULE)
        point_rows = store.rows("ts_point")

        # 构建测点->采集间隔映射
        interval_map = {}
        for pt in point_rows:
            interval = parse_interval_seconds(pt.get("采集间隔", "10s"), default=10)
            interval_map[pt.get("测点编号")] = interval

        recommendations = []
        total_estimated_mb = 0
        for strategy in rows:
            if strategy.get("status") != "已生效":
                continue
            pt_id = strategy.get("测点编号")
            interval = int(strategy.get("采样间隔秒数") or interval_map.get(pt_id, 10))
            daily_points = 86400 // interval
            try:
                retention = int(strategy.get("保留天数", "30"))
            except (ValueError, TypeError):
                retention = 30

            # 估算存储
            raw_bytes = daily_points * 32 * retention
            total_estimated_mb += raw_bytes / (1024 * 1024)

            # 保留天数越短越好
            recommended_retention = max(7, min(retention, retention * 2 // 3))
            recommendations.append({
                "测点编号": pt_id,
                "当前保留天数": retention,
                "建议保留天数": recommended_retention,
                "每日数据点": daily_points,
            })

        return {
            "total_estimated_mb": round(total_estimated_mb, 2),
            "min_free_space_mb": min_free_space_mb,
            "need_optimize": total_estimated_mb > min_free_space_mb,
            "recommendations": recommendations,
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """存储策略动作流转；标记生效时保证同一测点只有一份当前生效策略。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        entry["status"] = target
        entry["pending"] = target != self.STATUS_ORDER[-1]
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS
        if action == "标记生效":
            point_id = entry.get("测点编号")
            for other in store.rows(self.MODULE):
                if other is entry:
                    continue
                if point_id and other.get("测点编号") == point_id and other.get("status") == "已生效":
                    other["status"] = "已停用"
                    other["pending"] = False
        return entry, f"{self.ENTITY}已{action}"

    # ===================================================================
    # 测点与服务端有效依据
    # ===================================================================

    def find_point(self, point_ref: Any) -> dict[str, Any] | None:
        """按行 id 或测点编号（兼容模糊）查找时序测点。"""
        if point_ref is None:
            return None
        rows = store.rows("ts_point")
        if isinstance(point_ref, dict):
            return point_ref
        text = str(point_ref).strip()
        if text == "":
            return None
        if text.isdigit():
            row = store.find("ts_point", int(text))
            if row is not None:
                return row
        for row in rows:
            if str(row.get("测点编号", "")) == text:
                return row
        for row in rows:
            if text.casefold() in str(row.get("测点编号", "")).casefold():
                return row
        return None

    def point_basis(self, point_ref: Any) -> dict[str, Any] | None:
        """返回服务端持有的测点当前采样依据（冲突时的「有效依据」）。"""
        point = self.find_point(point_ref)
        if point is None:
            return None
        seconds = parse_interval_seconds(point.get("采集间隔"), default=10)
        version = int(point.get("参数版本") or point.get("param_version") or 0)
        return {
            "point_id": point.get("测点编号"),
            "测点编号": point.get("测点编号"),
            "point_row_id": point.get("id"),
            "sampling_interval_seconds": seconds,
            "interval_seconds": seconds,
            "sample_interval_seconds": seconds,
            "采集间隔秒数": seconds,
            "采样间隔秒数": seconds,
            "sampling_interval": f"{seconds}s",
            "sample_interval": f"{seconds}s",
            "interval": f"{seconds}s",
            "采样间隔": f"{seconds}s",
            "采集间隔": f"{seconds}s",
            "采样间隔显示": f"{seconds}秒",
            "param_version": version,
            "参数版本": version,
            "updated_at": point.get("参数更新时间") or point.get("updated_at") or "",
            "source": "server",
            "依据来源": "服务端测点参数",
        }

    # 语义化别名
    get_server_basis = point_basis
    current_basis = point_basis
    get_effective_basis = point_basis

    def update_point_sampling(
        self, point_ref: Any, interval: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        """修订测点采样间隔（10s → 30s 场景），并递增参数版本。"""
        values = kwargs.get("values") if isinstance(kwargs.get("values"), dict) else {}
        if interval is None:
            interval = _pick(kwargs, "sampling_interval", "sampling_interval_seconds",
                             "采集间隔", "interval", default=None)
        if interval is None and values:
            interval = _pick(values, "采集间隔", "sampling_interval",
                             "sampling_interval_seconds", "interval")
        point = self.find_point(point_ref)
        if point is None:
            return {"ok": False, "message": f"测点 {point_ref} 不存在，无法修订采样参数"}
        seconds = parse_interval_seconds(interval, default=-1)
        if seconds <= 0:
            return {"ok": False, "message": f"无法识别的采样间隔：{interval!r}"}
        old_seconds = parse_interval_seconds(point.get("采集间隔"), default=10)
        point["采集间隔"] = f"{seconds}s"
        point["采样间隔秒数"] = seconds
        point["参数版本"] = int(point.get("参数版本") or 0) + 1
        point["参数更新时间"] = _now()
        basis = self.point_basis(point)
        return {
            "ok": True,
            "message": f"测点 {point.get('测点编号')} 采样间隔已由 {old_seconds}s 调整为 {seconds}s",
            "point": point,
            "entry": point,
            "server_basis": basis,
            "basis": basis,
            "changed": old_seconds != seconds,
            "old_interval_seconds": old_seconds,
            "new_interval_seconds": seconds,
        }

    # ===================================================================
    # 容量计算（纯函数，始终基于单据快照，不重新读取已变更的测点）
    # ===================================================================

    def _capacity_math(
        self, interval_seconds: int, retention_days: int,
        compression: str | None, hot_days: int | None,
        bytes_per_point: int = BYTES_PER_POINT,
    ) -> dict[str, Any]:
        compression = compression or DEFAULT_COMPRESSION
        ratio = COMPRESSION_RATIOS.get(compression, 2)
        daily_points = SECONDS_PER_DAY // max(1, interval_seconds)
        raw_bytes = daily_points * bytes_per_point * retention_days
        compressed_bytes = raw_bytes / ratio
        effective_hot_days = max(0, min(hot_days if hot_days is not None else 30, retention_days))
        hot_bytes = daily_points * bytes_per_point * effective_hot_days / ratio
        cold_bytes = compressed_bytes - hot_bytes
        return {
            "bytes_per_point": bytes_per_point,
            "每点字节数": bytes_per_point,
            "seconds_per_day": SECONDS_PER_DAY,
            "每日秒数": SECONDS_PER_DAY,
            "daily_points": daily_points,
            "daily_data_points": daily_points,
            "data_points_per_day": daily_points,
            "points_per_day": daily_points,
            "每日数据点": daily_points,
            "retention_days": retention_days,
            "raw_bytes_estimate": raw_bytes,
            "raw_bytes": raw_bytes,
            "estimated_raw_bytes": raw_bytes,
            "原始容量字节": raw_bytes,
            "compressed_bytes_estimate": compressed_bytes,
            "compressed_bytes": compressed_bytes,
            "estimated_bytes": compressed_bytes,
            "estimated_storage_bytes": compressed_bytes,
            "容量字节": compressed_bytes,
            "压缩容量字节": compressed_bytes,
            "estimated_raw_mb": round(raw_bytes / (1024 * 1024), 4),
            "estimated_compressed_mb": round(compressed_bytes / (1024 * 1024), 4),
            "compression_algorithm": compression,
            "压缩算法": compression,
            "compression_ratio": ratio,
            "压缩比": ratio,
            "hot_days": effective_hot_days,
            "hot_storage_bytes": hot_bytes,
            "cold_storage_bytes": cold_bytes,
            "容量口径": (
                f"{bytes_per_point} 字节/点 × {daily_points} 点/日 × "
                f"{retention_days} 天 ÷ 压缩比 {ratio}（{compression}）"
            ),
        }

    def compute_capacity(self, interval_seconds: int, retention_days: int,
                         compression: str | None = DEFAULT_COMPRESSION,
                         hot_days: int | None = 30) -> dict[str, Any]:
        """对外暴露的统一容量计算。"""
        return self._capacity_math(
            int(interval_seconds), parse_days(retention_days), compression, hot_days
        )

    # ===================================================================
    # 保留估算单
    # ===================================================================

    def _normalize_create_kwargs(self, first: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        values = kwargs.get("values")
        if isinstance(values, dict):
            merged.update(values)
        payload = kwargs.get("payload")
        if isinstance(payload, dict):
            merged.update(payload)
        merged.update({k: v for k, v in kwargs.items() if k not in ("values", "payload")})
        if isinstance(first, dict):
            merged.update(first)
        elif first is not None:
            merged.setdefault("point_ref", first)
        return merged

    def create_retention_estimate(self, point_ref: Any = None, retention_days: Any = None,
                                  compression_algorithm: str | None = None,
                                  storage_engine: str | None = None,
                                  hot_days: Any = None, **kwargs: Any) -> dict[str, Any]:
        """按测点当前采样设置 + 策略期限 + 容量口径生成一轮一致的估算单。"""
        merged = self._normalize_create_kwargs(
            {"point_ref": point_ref, "retention_days": retention_days,
             "compression_algorithm": compression_algorithm,
             "storage_engine": storage_engine, "hot_days": hot_days},
            kwargs,
        )
        point_key = _pick(merged, "point_ref", "point_id", "测点编号", "point", "id", "pointId")
        point = self.find_point(point_key)
        if point is None:
            return {"ok": False, "message": f"测点 {point_key} 不存在，无法申请保留估算单"}

        basis = self.point_basis(point) or {}
        interval_seconds = int(basis.get("sampling_interval_seconds", 10))
        days = parse_days(_pick(
            merged, "retention_days", "策略期限天数", "保留天数", "策略期限",
            "term_days", "retention_term_days", default=DEFAULT_RETENTION_DAYS,
        ))
        compression = _pick(
            merged, "compression_algorithm", "压缩算法", "compression",
            default=DEFAULT_COMPRESSION,
        )
        engine = _pick(merged, "storage_engine", "存储引擎", "engine", default="tsdb")
        hot = _pick(merged, "hot_days", "热分层天数", default=30)
        hot_days_int = parse_days(hot, default=30) if hot is not None else 30
        math = self._capacity_math(interval_seconds, days, compression, hot_days_int)

        rows = store.rows(ESTIMATE_TABLE)
        estimate_id = _next_id(rows)
        estimate_no = f"EST-{estimate_id:04d}"
        source_estimate_no = _pick(merged, "source_estimate_no", "来源估算单",
                                   "revision_of", default="") or ""
        round_no = int(_pick(merged, "round", "轮次", default=1) or 1)
        idem_key = _pick(merged, "idempotency_key", "request_id", default="") or ""

        inputs = {
            "point_id": point.get("测点编号"),
            "sampling_interval_seconds": interval_seconds,
            "sampling_interval": f"{interval_seconds}s",
            "retention_days": days,
            "compression_algorithm": compression,
            "compression_ratio": math["compression_ratio"],
            "storage_engine": engine,
            "hot_days": math["hot_days"],
            "bytes_per_point": BYTES_PER_POINT,
            "seconds_per_day": SECONDS_PER_DAY,
            "capacity_basis_version": "v1-32bpp",
            "point_param_version": basis.get("param_version", 0),
        }
        estimate: dict[str, Any] = {
            "id": estimate_id,
            "estimate_no": estimate_no,
            "单号": estimate_no,
            "单据编号": estimate_no,
            "单据类型": "保留估算单",
            "point_id": point.get("测点编号"),
            "测点编号": point.get("测点编号"),
            "point_row_id": point.get("id"),
            "point_name": point.get("测点名称", ""),
            "测点名称": point.get("测点名称", ""),
            # 冻结的整组输入
            "sampling_interval_seconds": interval_seconds,
            "interval_seconds": interval_seconds,
            "sample_interval_seconds": interval_seconds,
            "采样间隔秒数": interval_seconds,
            "sampling_interval": f"{interval_seconds}s",
            "sample_interval": f"{interval_seconds}s",
            "interval": f"{interval_seconds}s",
            "采样间隔": f"{interval_seconds}s",
            "采样间隔显示": f"{interval_seconds}秒",
            "retention_days": days,
            "策略期限天数": days,
            "保留天数": days,
            "策略期限": f"{days}天",
            "strategy_term": f"{days}d",
            "storage_engine": engine,
            "存储引擎": engine,
            **math,
            "capacity_basis": {**inputs, **{
                "daily_points": math["daily_points"],
                "raw_bytes": math["raw_bytes"],
                "compressed_bytes": math["compressed_bytes_estimate"],
            }},
            "inputs": inputs,
            "input_snapshot": inputs,
            "snapshot": inputs,
            "round": round_no,
            "轮次": round_no,
            "version": round_no,
            "source_estimate_no": source_estimate_no,
            "来源估算单": source_estimate_no,
            "idempotency_key": idem_key,
            "confirmation_no": "",
            "容量确认单": "",
            "strategy_no": "",
            "存储策略编号": "",
            "activation_id": "",
            "status": "待核对",
            "pending": True,
            "abnormal": False,
            "renewable": False,
            "可续办": False,
            "created_at": _now(),
            "申请时间": _now(),
        }
        rows.append(estimate)
        return {
            "ok": True,
            "message": f"保留估算单 {estimate_no} 已提交：测点 {point.get('测点编号')} "
                       f"按 {interval_seconds}s 采样、保留 {days} 天测算",
            "estimate": estimate,
            "entry": estimate,
            "id": estimate_id,
            "estimate_no": estimate_no,
        }

    # 常见命名别名
    apply_retention_estimate = create_retention_estimate
    request_retention_estimate = create_retention_estimate
    submit_estimate = create_retention_estimate
    create_estimate = create_retention_estimate
    apply_estimate = create_retention_estimate
    estimate_retention = create_retention_estimate

    def list_retention_estimates(self, point_ref: Any = None) -> dict[str, Any]:
        rows = list(store.rows(ESTIMATE_TABLE))
        if point_ref is not None:
            point = self.find_point(point_ref)
            point_id = point.get("测点编号") if point else str(point_ref)
            rows = [r for r in rows if r.get("测点编号") == point_id]
        rows.sort(key=lambda r: int(r.get("id", 0)), reverse=True)
        return {"ok": True, "items": rows, "total": len(rows), "estimates": rows}

    list_estimates = list_retention_estimates

    def find_estimate(self, ref: Any) -> dict[str, Any] | None:
        if ref is None:
            return None
        if isinstance(ref, dict):
            return ref
        text = str(ref).strip()
        rows = store.rows(ESTIMATE_TABLE)
        if text.isdigit():
            row = store.find(ESTIMATE_TABLE, int(text))
            if row is not None:
                return row
        for row in rows:
            if row.get("estimate_no") == text or row.get("单号") == text:
                return row
        return None

    def get_retention_estimate(self, ref: Any) -> dict[str, Any]:
        estimate = self.find_estimate(ref)
        if estimate is None:
            return {"ok": False, "message": f"保留估算单 {ref} 不存在"}
        return {"ok": True, "estimate": estimate, "entry": estimate}

    get_estimate = get_retention_estimate

    # ===================================================================
    # 一致性校验：冻结间隔 vs 服务端当前间隔
    # ===================================================================

    def _staleness(self, estimate: dict[str, Any]) -> dict[str, Any]:
        point = self.find_point(estimate.get("测点编号"))
        if point is None:
            return {
                "stale": True,
                "conflict": True,
                "code": "POINT_MISSING",
                "message": f"测点 {estimate.get('测点编号')} 已不存在，无法继续开通",
                "basis": None,
            }
        basis = self.point_basis(point)
        old_seconds = int(estimate.get("sampling_interval_seconds", 10))
        current_seconds = int(basis["sampling_interval_seconds"])
        if current_seconds != old_seconds:
            return {
                "stale": True,
                "conflict": True,
                "code": "SAMPLING_INTERVAL_CHANGED",
                "old_interval_seconds": old_seconds,
                "new_interval_seconds": current_seconds,
                "basis": basis,
                "message": (
                    f"测点 {estimate.get('测点编号')} 的采样间隔已由 {old_seconds}s "
                    f"调整为 {current_seconds}s；估算单 {estimate.get('estimate_no')} "
                    f"冻结的是变更前 {old_seconds}s 口径，不能把旧采样间隔与变更后的"
                    f"策略期限当成同一轮结果开通。请按服务端当前 {current_seconds}s "
                    f"依据续办待核项或重新测算。"
                ),
            }
        return {"stale": False, "conflict": False, "code": "FRESH", "basis": basis}

    def _open_pending_item(
        self, estimate: dict[str, Any], stale: dict[str, Any], action: str
    ) -> dict[str, Any]:
        rows = store.rows(PENDING_TABLE)
        for row in rows:
            if row.get("estimate_no") == estimate.get("estimate_no") and row.get("status") == "待核":
                row["server_basis"] = stale.get("basis")
                row["有效依据"] = stale.get("basis")
                row["new_interval_seconds"] = stale.get("new_interval_seconds")
                row["当前间隔秒数"] = stale.get("new_interval_seconds")
                row["last_action"] = action
                row["updated_at"] = _now()
                return row
        pending_id = _next_id(rows)
        pending_no = f"CHK-{pending_id:04d}"
        item = {
            "id": pending_id,
            "pending_no": pending_no,
            "待核编号": pending_no,
            "单号": pending_no,
            "单据类型": "开通待核项",
            "estimate_no": estimate.get("estimate_no"),
            "来源估算单": estimate.get("estimate_no"),
            "point_id": estimate.get("测点编号"),
            "测点编号": estimate.get("测点编号"),
            "action": action,
            "触发动作": action,
            "reason": "SAMPLING_INTERVAL_CHANGED",
            "原因": "测点采样参数已修订，开通前需按当前参数复核",
            "change": (
                f"采样间隔 {stale.get('old_interval_seconds')}s → "
                f"{stale.get('new_interval_seconds')}s"
            ),
            "变更说明": (
                f"采样间隔 {stale.get('old_interval_seconds')}s → "
                f"{stale.get('new_interval_seconds')}s"
            ),
            "old_interval_seconds": stale.get("old_interval_seconds"),
            "变更前间隔秒数": stale.get("old_interval_seconds"),
            "new_interval_seconds": stale.get("new_interval_seconds"),
            "当前间隔秒数": stale.get("new_interval_seconds"),
            "server_basis": stale.get("basis"),
            "valid_basis": stale.get("basis"),
            "basis": stale.get("basis"),
            "有效依据": stale.get("basis"),
            "status": "待核",
            "pending": True,
            "renewable": True,
            "可续办": True,
            "available_actions": ["续办", "重新测算", "renew"],
            "renewed_estimate_no": "",
            "created_at": _now(),
            "updated_at": _now(),
        }
        rows.append(item)
        estimate["status"] = "待核"
        estimate["renewable"] = True
        estimate["可续办"] = True
        estimate["pending_item_no"] = pending_no
        estimate["待核项编号"] = pending_no
        return item

    def _conflict_response(
        self, estimate: dict[str, Any], stale: dict[str, Any], action: str
    ) -> dict[str, Any]:
        pending = self._open_pending_item(estimate, stale, action)
        basis = stale.get("basis")
        return {
            "ok": False,
            "conflict": True,
            "stale": True,
            "code": stale.get("code", "STALE_ESTIMATE"),
            "message": stale.get("message", "估算口径与服务端当前测点参数不一致"),
            "estimate": estimate,
            "entry": estimate,
            "server_basis": basis,
            "valid_basis": basis,
            "effective_basis": basis,
            "basis": basis,
            "有效依据": basis,
            "current": basis,
            "pending_item": pending,
            "待核项": pending,
            "renewable": True,
            "可续办": True,
        }

    # ===================================================================
    # 容量核对 + 容量确认单
    # ===================================================================

    def check_capacity(self, estimate_ref: Any, available_bytes: Any = None,
                       capacity_quota_bytes: Any = None, **kwargs: Any) -> dict[str, Any]:
        """按估算单冻结口径核对容量（不依赖测点当前参数重算）。"""
        estimate = self.find_estimate(estimate_ref)
        if estimate is None:
            return {"ok": False, "message": f"保留估算单 {estimate_ref} 不存在"}
        required = float(estimate.get("compressed_bytes_estimate") or 0)
        quota = _pick(kwargs, "available_bytes", "capacity_quota_bytes", "quota_bytes",
                      default=available_bytes if available_bytes is not None else capacity_quota_bytes)
        if quota is None:
            # 默认给一个充足配额，核对结论为通过；调用方也可显式传容量配额。
            quota = max(required * 2, 10 * 1024 ** 3)
        available = float(quota)
        passed = available >= required
        check = {
            "passed": passed,
            "ok": passed,
            "required_bytes": required,
            "需求容量字节": required,
            "available_bytes": available,
            "可用容量字节": available,
            "remaining_bytes": available - required,
            "缺口字节": max(0.0, required - available),
            "checked_at": _now(),
            "basis": estimate.get("capacity_basis"),
            "口径": estimate.get("容量口径"),
        }
        return {
            "ok": True,
            "passed": passed,
            "message": "容量核对通过" if passed else "容量不足，请调整策略期限或扩容",
            "capacity_check": check,
            "核对结果": check,
            "estimate": estimate,
            "entry": estimate,
        }

    verify_capacity = check_capacity

    def _find_confirmation(self, estimate_no: str) -> dict[str, Any] | None:
        for row in store.rows(CONFIRM_TABLE):
            if row.get("estimate_no") == estimate_no or row.get("来源估算单") == estimate_no:
                return row
        return None

    def confirm_capacity(
        self, estimate_ref: Any = None, *, allow_historical: bool = False,
        available_bytes: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        """核对容量并生成冻结口径的「容量确认单」。

        - 已生成过确认单：幂等返回同一张；
        - 测点参数已变更且本轮是首次确认：返回冲突 + 待核项；
        - ``allow_historical=True`` 时允许把旧口径单独留存为「历史口径」确认单，
          但该确认单不能直接用于开通当前策略。
        """
        estimate = self.find_estimate(estimate_ref)
        if estimate is None:
            return {"ok": False, "message": f"保留估算单 {estimate_ref} 不存在"}

        allow_historical = _as_bool(allow_historical, default=False)
        existing = self._find_confirmation(str(estimate.get("estimate_no")))
        if existing is not None and not kwargs.get("force_new"):
            return {
                "ok": True,
                "duplicate": True,
                "already_confirmed": True,
                "message": f"估算单 {estimate.get('estimate_no')} 已核对确认，返回原容量确认单",
                "confirmation": existing,
                "entry": existing,
                "capacity_confirmation": existing,
            }

        stale = self._staleness(estimate)
        if stale.get("stale") and not allow_historical:
            return self._conflict_response(estimate, stale, "核对容量")

        check_result = self.check_capacity(estimate, available_bytes=available_bytes)
        check = check_result["capacity_check"]
        if not check["passed"] and not allow_historical:
            return {"ok": False, "message": check_result["message"],
                    "capacity_check": check, "核对结果": check}

        rows = store.rows(CONFIRM_TABLE)
        confirm_id = _next_id(rows)
        confirm_no = f"CAP-{confirm_id:04d}"
        historical = bool(stale.get("stale"))
        confirmation: dict[str, Any] = {
            "id": confirm_id,
            "confirmation_no": confirm_no,
            "capacity_confirmation_no": confirm_no,
            "单号": confirm_no,
            "确认单号": confirm_no,
            "单据编号": confirm_no,
            "单据类型": "容量确认单",
            "estimate_no": estimate.get("estimate_no"),
            "来源估算单": estimate.get("estimate_no"),
            "point_id": estimate.get("测点编号"),
            "测点编号": estimate.get("测点编号"),
            # 冻结与估算单完全一致的一组输入和值
            "sampling_interval_seconds": estimate.get("sampling_interval_seconds"),
            "采样间隔秒数": estimate.get("采样间隔秒数"),
            "sampling_interval": estimate.get("sampling_interval"),
            "采样间隔": estimate.get("采样间隔"),
            "采样间隔显示": estimate.get("采样间隔显示"),
            "retention_days": estimate.get("retention_days"),
            "策略期限天数": estimate.get("策略期限天数"),
            "保留天数": estimate.get("保留天数"),
            "策略期限": estimate.get("策略期限"),
            "compression_algorithm": estimate.get("compression_algorithm"),
            "压缩算法": estimate.get("压缩算法"),
            "compression_ratio": estimate.get("compression_ratio"),
            "storage_engine": estimate.get("storage_engine"),
            "存储引擎": estimate.get("存储引擎"),
            "daily_points": estimate.get("daily_points"),
            "每日数据点": estimate.get("每日数据点"),
            "bytes_per_point": estimate.get("bytes_per_point"),
            "raw_bytes_estimate": estimate.get("raw_bytes_estimate"),
            "raw_bytes": estimate.get("raw_bytes"),
            "原始容量字节": estimate.get("原始容量字节"),
            "compressed_bytes_estimate": estimate.get("compressed_bytes_estimate"),
            "compressed_bytes": estimate.get("compressed_bytes"),
            "压缩容量字节": estimate.get("压缩容量字节"),
            "estimated_raw_mb": estimate.get("estimated_raw_mb"),
            "estimated_compressed_mb": estimate.get("estimated_compressed_mb"),
            "容量口径": estimate.get("容量口径"),
            "capacity_basis": estimate.get("capacity_basis"),
            "snapshot": estimate.get("snapshot"),
            "capacity_check": check,
            "核对结果": check,
            "basis_status": "historical" if historical else "current",
            "口径状态": "历史口径（参数已变更）" if historical else "当前口径",
            "usable_for_activation": not historical,
            "可用于启用": not historical,
            "status": "历史口径" if historical else "可启用",
            "pending": historical,
            "abnormal": False,
            "strategy_no": "",
            "created_at": _now(),
            "确认时间": _now(),
        }
        rows.append(confirmation)
        estimate["confirmation_no"] = confirm_no
        estimate["容量确认单"] = confirm_no
        estimate["status"] = "已确认（历史口径）" if historical else "已确认"
        estimate["pending"] = historical
        return {
            "ok": True,
            "message": (
                f"容量确认单 {confirm_no} 已生成（历史口径，仅供留档）"
                if historical else
                f"容量核对通过，容量确认单 {confirm_no} 已生成，可用于后续启用"
            ),
            "confirmation": confirmation,
            "capacity_confirmation": confirmation,
            "entry": confirmation,
        }

    create_capacity_confirmation = confirm_capacity

    def list_confirmations(self, point_ref: Any = None) -> dict[str, Any]:
        rows = list(store.rows(CONFIRM_TABLE))
        if point_ref is not None:
            point = self.find_point(point_ref)
            point_id = point.get("测点编号") if point else str(point_ref)
            rows = [r for r in rows if r.get("测点编号") == point_id]
        rows.sort(key=lambda r: int(r.get("id", 0)), reverse=True)
        return {"ok": True, "items": rows, "total": len(rows), "confirmations": rows}

    list_capacity_confirmations = list_confirmations

    def find_confirmation(self, ref: Any) -> dict[str, Any] | None:
        if ref is None:
            return None
        if isinstance(ref, dict):
            return ref
        text = str(ref).strip()
        rows = store.rows(CONFIRM_TABLE)
        if text.isdigit():
            row = store.find(CONFIRM_TABLE, int(text))
            if row is not None:
                return row
        for row in rows:
            if row.get("confirmation_no") == text or row.get("单号") == text:
                return row
        # 也允许用估算单号找确认单
        return self._find_confirmation(text)

    def get_confirmation(self, ref: Any) -> dict[str, Any]:
        confirmation = self.find_confirmation(ref)
        if confirmation is None:
            return {"ok": False, "message": f"容量确认单 {ref} 不存在"}
        return {"ok": True, "confirmation": confirmation, "entry": confirmation}

    get_capacity_confirmation = get_confirmation

    # ===================================================================
    # 转办策略：核对容量 -> 产出容量确认单
    # ===================================================================

    def transfer_to_strategy(self, estimate_ref: Any = None, **kwargs: Any) -> dict[str, Any]:
        """把估算单转办为策略开通流程，返回可用于启用的容量确认单。"""
        estimate = self.find_estimate(estimate_ref)
        if estimate is None:
            return {"ok": False, "message": f"保留估算单 {estimate_ref} 不存在，无法转办"}

        confirmation = self._find_confirmation(str(estimate.get("estimate_no")))
        stale = self._staleness(estimate)

        if confirmation is not None:
            if stale.get("stale") and confirmation.get("basis_status") != "historical":
                conflict = self._conflict_response(estimate, stale, "转办策略")
                conflict["confirmation"] = confirmation
                conflict["capacity_confirmation"] = confirmation
                return conflict
            estimate["status"] = estimate.get("status") if stale.get("stale") else "已转办（已确认容量）"
            return {
                "ok": True,
                "duplicate": True,
                "already_transferred": True,
                "message": f"估算单 {estimate.get('estimate_no')} 已转办，返回容量确认单",
                "estimate": estimate,
                "confirmation": confirmation,
                "capacity_confirmation": confirmation,
                "entry": confirmation,
            }

        if stale.get("stale"):
            return self._conflict_response(estimate, stale, "转办策略")

        confirmed = self.confirm_capacity(estimate)
        if not confirmed.get("ok"):
            return confirmed
        confirmation = confirmed["confirmation"]
        estimate["status"] = "已转办（已确认容量）"
        estimate["pending"] = True
        return {
            "ok": True,
            "message": f"估算单已转办策略，容量确认单 {confirmation.get('confirmation_no')} 可用于后续启用",
            "estimate": estimate,
            "confirmation": confirmation,
            "capacity_confirmation": confirmation,
            "entry": confirmation,
        }

    transfer_estimate = transfer_to_strategy
    convert_to_strategy = transfer_to_strategy
    transfer = transfer_to_strategy
    dispatch_strategy = transfer_to_strategy

    # ===================================================================
    # 续办 / 重新测算
    # ===================================================================

    def renew_estimate(self, estimate_ref: Any = None, **overrides: Any) -> dict[str, Any]:
        """按服务端当前测点参数续办待核项，生成新一轮估算单（旧单保留）。"""
        old = self.find_estimate(estimate_ref)
        if old is None:
            return {"ok": False, "message": f"待核估算单 {estimate_ref} 不存在，无法续办"}
        point = self.find_point(old.get("测点编号"))
        if point is None:
            return {"ok": False, "message": f"测点 {old.get('测点编号')} 不存在，无法续办"}

        days = overrides.pop("retention_days", None) or old.get("retention_days")
        compression = overrides.pop("compression_algorithm", None) or old.get("compression_algorithm")
        engine = overrides.pop("storage_engine", None) or old.get("storage_engine")
        hot_days = overrides.pop("hot_days", None) or old.get("hot_days")
        new_round = int(old.get("round", 1) or 1) + 1
        created = self.create_retention_estimate(
            old.get("测点编号"),
            retention_days=days,
            compression_algorithm=compression,
            storage_engine=engine,
            hot_days=hot_days,
            source_estimate_no=old.get("estimate_no"),
            round=new_round,
            **overrides,
        )
        if not created.get("ok"):
            return created
        new_estimate = created["estimate"]

        # 关闭旧待核项，保留旧估算单与其历史值
        for pending in store.rows(PENDING_TABLE):
            if (pending.get("estimate_no") == old.get("estimate_no")
                    and pending.get("status") == "待核"):
                pending["status"] = "已续办"
                pending["pending"] = False
                pending["renewed_estimate_no"] = new_estimate["estimate_no"]
                pending["续办单号"] = new_estimate["estimate_no"]
                pending["updated_at"] = _now()
        old["renewed_estimate_no"] = new_estimate["estimate_no"]
        old["续办单号"] = new_estimate["estimate_no"]
        if old.get("status") == "待核":
            old["status"] = "已续办"
            old["pending"] = False
        created["pending_item"] = None
        created["message"] = (
            f"已按服务端当前参数续办：新估算单 {new_estimate['estimate_no']}，"
            f"原单 {old.get('estimate_no')} 的输入和值仍可查看"
        )
        return created

    renew_pending = renew_estimate
    recheck = renew_estimate
    reverify = renew_estimate
    reestimate = renew_estimate
    re_estimate = renew_estimate

    def list_pending_checks(self, point_ref: Any = None, status: str | None = None) -> dict[str, Any]:
        rows = list(store.rows(PENDING_TABLE))
        if point_ref is not None:
            point = self.find_point(point_ref)
            point_id = point.get("测点编号") if point else str(point_ref)
            rows = [r for r in rows if r.get("测点编号") == point_id]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: int(r.get("id", 0)), reverse=True)
        return {"ok": True, "items": rows, "total": len(rows), "pending_items": rows}

    list_pending = list_pending_checks

    # ===================================================================
    # 开通 / 领取（幂等，重复提交只确认一份策略）
    # ===================================================================

    def _find_strategy_by_estimate(self, estimate_no: str, idem_key: str = "") -> dict[str, Any] | None:
        for strategy in store.rows(self.MODULE):
            if estimate_no and strategy.get("来源估算单") == estimate_no:
                return strategy
            if idem_key and strategy.get("idempotency_key") == idem_key:
                return strategy
        for activation in store.rows(ACTIVATION_TABLE):
            if estimate_no and activation.get("estimate_no") == estimate_no:
                strategy = store.find(self.MODULE, int(activation.get("strategy_id", -1)))
                if strategy is not None:
                    return strategy
            if idem_key and activation.get("idempotency_key") == idem_key:
                strategy = store.find(self.MODULE, int(activation.get("strategy_id", -1)))
                if strategy is not None:
                    return strategy
        return None

    def activate_from_estimate(
        self, estimate_ref: Any = None, *, idempotency_key: str | None = None,
        enable: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        """确认开通，生成（或幂等返回）唯一一份存储策略。"""
        merged = self._normalize_create_kwargs(
            {"estimate_ref": estimate_ref, "idempotency_key": idempotency_key, "enable": enable},
            kwargs,
        )
        estimate_ref = _pick(merged, "estimate_ref", "estimate_no", "ref", "单号",
                             "confirmation_no", "id", default=estimate_ref)
        idem_key = _pick(merged, "idempotency_key", "Idempotency-Key", "request_id",
                         default=idempotency_key or "") or ""
        should_enable = _as_bool(_pick(merged, "enable", "enabled", default=enable), default=True)

        # 允许直接传容量确认单号
        estimate = self.find_estimate(estimate_ref)
        confirmation: dict[str, Any] | None = None
        if estimate is None:
            maybe_confirmation = self.find_confirmation(estimate_ref)
            if maybe_confirmation is not None:
                confirmation = maybe_confirmation
                estimate = self.find_estimate(maybe_confirmation.get("estimate_no"))
        if estimate is None:
            return {"ok": False, "message": f"估算单/确认单 {estimate_ref} 不存在，无法提交开通申请"}

        estimate_no = str(estimate.get("estimate_no"))
        dedup_key = idem_key or f"estimate:{estimate_no}"

        # 1) 幂等优先：同一份估算的失联重试 / 重复领取，直接返回已确认策略
        existing = self._find_strategy_by_estimate(estimate_no, idem_key)
        if existing is not None:
            self._record_activation_retry(estimate_no, existing, dedup_key)
            return {
                "ok": True,
                "duplicate": True,
                "already_activated": True,
                "idempotent_replay": True,
                "message": f"估算单 {estimate_no} 的开通申请已确认，未重复创建存储策略",
                "strategy": existing,
                "entry": existing,
                "store_strategy": existing,
                "strategy_no": existing.get("策略编号"),
                "idempotency_key": dedup_key,
            }

        # 2) 服务端一致性依据：参数已变更则拒绝混算并给待核项
        stale = self._staleness(estimate)
        if stale.get("stale"):
            return self._conflict_response(estimate, stale, "开通申请")

        # 3) 需要一张当前口径的容量确认单
        if confirmation is None:
            confirmation = self._find_confirmation(estimate_no)
        if confirmation is None:
            confirmed = self.transfer_to_strategy(estimate)
            if not confirmed.get("ok"):
                return confirmed
            confirmation = confirmed["confirmation"]
        if not confirmation.get("usable_for_activation", True):
            return self._conflict_response(estimate, stale, "开通申请")

        # 4) 落一份且仅一份存储策略（数字全部取自冻结确认单/估算单）
        strategy_rows = store.rows(self.MODULE)
        strategy_id = _next_id(strategy_rows)
        strategy_no = f"TS_S-{strategy_id:04d}"
        status = "已生效" if should_enable else "待验证"
        strategy = {
            "id": strategy_id,
            "策略编号": strategy_no,
            "测点编号": estimate.get("测点编号"),
            "存储引擎": estimate.get("storage_engine") or "tsdb",
            "压缩算法": estimate.get("compression_algorithm") or DEFAULT_COMPRESSION,
            "降采样规则": _pick(merged, "downsample_rule", "降采样规则", default="avg_5min"),
            "保留天数": estimate.get("retention_days"),
            "分段策略": _pick(merged, "segment_policy", "分段策略", default="by_month"),
            "冷热分层": f"{estimate.get('hot_days', 30)}d_hot",
            # 一致性快照字段
            "采样间隔": estimate.get("采样间隔"),
            "sampling_interval": estimate.get("sampling_interval"),
            "采样间隔秒数": estimate.get("sampling_interval_seconds"),
            "sampling_interval_seconds": estimate.get("sampling_interval_seconds"),
            "每日数据点": estimate.get("daily_points"),
            "daily_points": estimate.get("daily_points"),
            "容量口径": estimate.get("容量口径"),
            "raw_bytes_estimate": estimate.get("raw_bytes_estimate"),
            "compressed_bytes_estimate": estimate.get("compressed_bytes_estimate"),
            "estimated_raw_mb": estimate.get("estimated_raw_mb"),
            "estimated_compressed_mb": estimate.get("estimated_compressed_mb"),
            "来源估算单": estimate_no,
            "来源容量确认单": confirmation.get("confirmation_no"),
            "idempotency_key": dedup_key,
            "status": status,
            "pending": not should_enable,
            "abnormal": False,
            "activated_at": _now(),
        }
        strategy_rows.append(strategy)

        # 同一测点只保留一份当前生效策略
        if should_enable:
            for other in strategy_rows:
                if other is strategy:
                    continue
                if other.get("测点编号") == strategy.get("测点编号") and other.get("status") == "已生效":
                    other["status"] = "已停用"
                    other["pending"] = False

        # 5) 开通台账（幂等索引）
        activation_rows = store.rows(ACTIVATION_TABLE)
        activation_id = _next_id(activation_rows)
        activation = {
            "id": activation_id,
            "activation_no": f"ACT-{activation_id:04d}",
            "idempotency_key": dedup_key,
            "estimate_no": estimate_no,
            "confirmation_no": confirmation.get("confirmation_no"),
            "strategy_id": strategy_id,
            "strategy_no": strategy_no,
            "request_count": 1,
            "first_at": _now(),
            "last_at": _now(),
        }
        activation_rows.append(activation)

        estimate["status"] = "已开通" if should_enable else "待验证"
        estimate["pending"] = not should_enable
        estimate["strategy_no"] = strategy_no
        estimate["存储策略编号"] = strategy_no
        estimate["activation_id"] = activation["activation_no"]
        if confirmation is not None:
            confirmation["strategy_no"] = strategy_no
            if should_enable:
                confirmation["status"] = "已启用"
                confirmation["可用于启用"] = False

        return {
            "ok": True,
            "message": f"开通申请已确认，存储策略 {strategy_no} 已"
                       + ("生效" if should_enable else "创建（待验证）"),
            "strategy": strategy,
            "entry": strategy,
            "store_strategy": strategy,
            "confirmation": confirmation,
            "capacity_confirmation": confirmation,
            "activation": activation,
            "strategy_no": strategy_no,
            "idempotency_key": dedup_key,
            "duplicate": False,
        }

    # 各种可能的命名
    activate_estimate = activate_from_estimate
    activate_strategy = activate_from_estimate
    submit_activation = activate_from_estimate
    apply_activation = activate_from_estimate
    open_strategy = activate_from_estimate
    provision_strategy = activate_from_estimate
    confirm_activation = activate_from_estimate
    claim_strategy = activate_from_estimate
    claim = activate_from_estimate
    receive_strategy = activate_from_estimate

    def _record_activation_retry(
        self, estimate_no: str, strategy: dict[str, Any], idem_key: str
    ) -> None:
        for activation in store.rows(ACTIVATION_TABLE):
            if activation.get("estimate_no") == estimate_no or (
                idem_key and activation.get("idempotency_key") == idem_key
            ):
                activation["request_count"] = int(activation.get("request_count", 1)) + 1
                activation["last_at"] = _now()
                return

    def list_activations(self) -> dict[str, Any]:
        rows = list(store.rows(ACTIVATION_TABLE))
        return {"ok": True, "items": rows, "total": len(rows), "activations": rows}

    def current_strategy(self, point_ref: Any) -> dict[str, Any]:
        """查询测点当前启用的存储策略（附带与页面一致的间隔口径）。"""
        point = self.find_point(point_ref)
        point_id = point.get("测点编号") if point else str(point_ref)
        active = [
            s for s in store.rows(self.MODULE)
            if s.get("测点编号") == point_id and s.get("status") == "已生效"
        ]
        strategy = active[-1] if active else None
        return {
            "ok": strategy is not None,
            "strategy": strategy,
            "entry": strategy,
            "items": active,
            "total": len(active),
            "server_basis": self.point_basis(point) if point else None,
        }

    def retention_overview(self, point_ref: Any = None) -> dict[str, Any]:
        """汇总估算单 / 确认单 / 待核项 / 当前策略，供页面一次加载。"""
        estimates = store.rows(ESTIMATE_TABLE)
        confirmations = store.rows(CONFIRM_TABLE)
        pending = store.rows(PENDING_TABLE)
        if point_ref is not None:
            point = self.find_point(point_ref)
            point_id = point.get("测点编号") if point else str(point_ref)
            estimates = [r for r in estimates if r.get("测点编号") == point_id]
            confirmations = [r for r in confirmations if r.get("测点编号") == point_id]
            pending = [r for r in pending if r.get("测点编号") == point_id]
        strategies = [
            s for s in store.rows(self.MODULE)
            if s.get("status") in ("已生效", "待验证") and s.get("来源估算单")
        ]
        return {
            "ok": True,
            "estimates": sorted(estimates, key=lambda r: int(r.get("id", 0)), reverse=True),
            "confirmations": sorted(confirmations, key=lambda r: int(r.get("id", 0)), reverse=True),
            "pending_items": sorted(pending, key=lambda r: int(r.get("id", 0)), reverse=True),
            "strategies": list(reversed(strategies)),
            "totals": {
                "estimates": len(estimates),
                "confirmations": len(confirmations),
                "pending": sum(1 for r in pending if r.get("status") == "待核"),
                "strategies": len(strategies),
            },
        }


# 便于 ``from app.services.ts_store import ts_store_service`` 直接取用的共享实例。
ts_store_service = TsStoreService()
