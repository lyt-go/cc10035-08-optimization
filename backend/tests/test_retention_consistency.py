"""时序存储策略开通一致性测试。

覆盖题面场景：
- 按测点真实采样间隔 + 策略期限 + 容量口径生成一致估算；
- 估算单 / 容量确认单冻结当时输入和值；
- 10s -> 30s 后旧单开通返回服务端有效依据与可续办待核项；
- 续办得到当前口径新单，历史确认单仍对应 10s；
- 失联重试 / 重复领取同一份估算只确认一份策略；
- 另一次独立测算始终允许。
"""
from __future__ import annotations

import copy

import pytest

from app.seed import SEED_ROWS
from app.store import store
from app.services.ts_store import TsStoreService
from app.services.ts_point import TsPointService


@pytest.fixture(autouse=True)
def isolated_store():
    """每个用例前后还原内存仓库，避免本文件的造数污染其它测试模块。"""
    snapshot = copy.deepcopy(store._tables)
    yield
    store._tables = snapshot


@pytest.fixture
def ts_store():
    return TsStoreService()


@pytest.fixture
def ts_point():
    return TsPointService()


def make_point(point_id: str = "TS-1030", interval: str = "10s") -> dict:
    rows = store.rows("ts_point")
    point = {
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "测点编号": point_id,
        "设备编号": "DEVI-X",
        "测点名称": "间隔一致性测点",
        "采集间隔": interval,
        "status": "采集中",
        "pending": False,
        "abnormal": False,
    }
    rows.append(point)
    return point


class TestConsistentEstimate:
    def test_estimate_uses_real_interval_and_full_basis(self, ts_store):
        make_point(interval="10s")
        result = ts_store.create_retention_estimate(
            point_id="TS-1030", retention_days=30, compression_algorithm="zstd"
        )
        assert result["ok"] is True
        est = result["estimate"]
        assert est["sampling_interval_seconds"] == 10
        assert est["retention_days"] == 30
        # 86400 / 10 = 8640 点/日；32 字节/点；30 天
        assert est["daily_points"] == 8640
        assert est["raw_bytes_estimate"] == 8640 * 32 * 30
        assert est["compression_ratio"] == 6
        assert "32" in est["容量口径"]

    def test_estimate_at_30s_is_one_third_of_10s(self, ts_store):
        make_point(point_id="P10", interval="10s")
        make_point(point_id="P30", interval="30s")
        e10 = ts_store.create_retention_estimate(point_id="P10", retention_days=30)["estimate"]
        e30 = ts_store.create_retention_estimate(point_id="P30", retention_days=30)["estimate"]
        assert e10["daily_points"] == 8640
        assert e30["daily_points"] == 2880
        assert e10["raw_bytes_estimate"] == 3 * e30["raw_bytes_estimate"]

    def test_estimate_for_missing_point_fails(self, ts_store):
        result = ts_store.create_retention_estimate(point_id="NO-SUCH", retention_days=30)
        assert result["ok"] is False


class TestConflictAndRenewal:
    def test_interval_change_blocks_stale_activation_with_basis_and_pending(self, ts_store):
        make_point(interval="10s")
        est = ts_store.create_retention_estimate(point_id="TS-1030", retention_days=30)["estimate"]

        ts_store.update_point_sampling("TS-1030", "30s")
        result = ts_store.activate_from_estimate(est["estimate_no"])

        assert result["ok"] is False
        assert result.get("conflict") is True
        # 服务端有效依据必须是当前 30s
        assert result["server_basis"]["sampling_interval_seconds"] == 30
        assert result["有效依据"]["sampling_interval_seconds"] == 30
        # 同时给出可续办的待核项
        pending = result["pending_item"]
        assert pending["status"] == "待核"
        assert pending["renewable"] is True
        assert pending["old_interval_seconds"] == 10
        assert pending["new_interval_seconds"] == 30

    def test_transfer_before_change_produces_frozen_confirmation(self, ts_store):
        make_point(interval="10s")
        est = ts_store.create_retention_estimate(point_id="TS-1030", retention_days=30)["estimate"]
        transferred = ts_store.transfer_to_strategy(est["estimate_no"])
        assert transferred["ok"] is True
        confirmation = transferred["confirmation"]
        assert confirmation["sampling_interval_seconds"] == 10
        assert confirmation["daily_points"] == 8640
        assert confirmation["usable_for_activation"] is True

    def test_history_confirmation_keeps_10s_while_current_strategy_uses_30s(self, ts_store):
        make_point(interval="10s")
        # 第一轮（10s）：估算 + 容量确认单（历史）
        e1 = ts_store.create_retention_estimate(point_id="TS-1030", retention_days=30)["estimate"]
        c1 = ts_store.transfer_to_strategy(e1["estimate_no"])["confirmation"]
        assert c1["sampling_interval_seconds"] == 10

        # 采样参数修订 10 -> 30，旧单开通冲突
        ts_store.update_point_sampling("TS-1030", "30s")
        conflict = ts_store.activate_from_estimate(e1["estimate_no"])
        assert conflict.get("conflict") is True

        # 续办得到 30s 新轮次并开通
        e2 = ts_store.renew_estimate(e1["estimate_no"])["estimate"]
        assert e2["sampling_interval_seconds"] == 30
        activated = ts_store.activate_from_estimate(e2["estimate_no"])
        assert activated["ok"] is True
        strategy = activated["strategy"]

        # 历史确认单仍是 10s / 8640 点
        old_confirmation = ts_store.get_confirmation(c1["confirmation_no"])["confirmation"]
        assert old_confirmation["sampling_interval_seconds"] == 10
        assert old_confirmation["daily_points"] == 8640
        assert old_confirmation["raw_bytes_estimate"] == e1["raw_bytes_estimate"]

        # 当前启用策略对应 30s / 2880 点
        current = ts_store.current_strategy("TS-1030")
        assert current["total"] == 1
        assert current["strategy"]["sampling_interval_seconds"] == 30
        assert current["strategy"]["daily_points"] == 2880
        assert strategy["daily_points"] == 2880

    def test_renew_preserves_old_estimate_inputs(self, ts_store):
        make_point(interval="10s")
        e1 = ts_store.create_retention_estimate(point_id="TS-1030", retention_days=45)["estimate"]
        ts_store.update_point_sampling("TS-1030", "30s")
        new = ts_store.renew_estimate(e1["estimate_no"])["estimate"]
        # 新单使用当前间隔，但保留原期限选择
        assert new["sampling_interval_seconds"] == 30
        assert new["retention_days"] == 45
        assert new["source_estimate_no"] == e1["estimate_no"]
        # 旧单冻结值不变
        old = ts_store.find_estimate(e1["estimate_no"])
        assert old["sampling_interval_seconds"] == 10
        assert old["daily_points"] == 8640


class TestIdempotentActivation:
    def test_retry_same_estimate_creates_single_strategy(self, ts_store):
        make_point(interval="30s")
        est = ts_store.create_retention_estimate(point_id="TS-1030", retention_days=30)["estimate"]
        first = ts_store.activate_from_estimate(est["estimate_no"])
        second = ts_store.activate_from_estimate(est["estimate_no"])
        third = ts_store.claim_strategy(est["estimate_no"])

        assert first["ok"] is True and first.get("duplicate") is False
        assert second["ok"] is True and second.get("duplicate") is True
        assert third["ok"] is True and third.get("duplicate") is True
        assert second["strategy"]["id"] == first["strategy"]["id"]
        assert third["strategy"]["id"] == first["strategy"]["id"]

        rows = [s for s in store.rows("ts_store") if s.get("来源估算单") == est["estimate_no"]]
        assert len(rows) == 1

    def test_repeated_claim_with_idempotency_key_no_extra_strategy(self, ts_store):
        make_point(interval="30s")
        est = ts_store.create_retention_estimate(point_id="TS-1030", retention_days=14)["estimate"]
        r1 = ts_store.activate_from_estimate(est["estimate_no"], idempotency_key="key-abc")
        r2 = ts_store.activate_from_estimate(est["estimate_no"], idempotency_key="key-abc")
        assert r1["strategy"]["策略编号"] == r2["strategy"]["策略编号"]
        assert r2.get("idempotent_replay") is True

    def test_independent_second_estimate_still_allowed(self, ts_store):
        make_point(interval="30s")
        e1 = ts_store.create_retention_estimate(point_id="TS-1030", retention_days=30)
        ts_store.activate_from_estimate(e1["estimate_no"])
        # 另一次独立测算不被幂等拦截
        e2 = ts_store.create_retention_estimate(point_id="TS-1030", retention_days=90)
        assert e2["ok"] is True
        assert e2["estimate_no"] != e1["estimate_no"]

    def test_only_one_current_active_strategy_per_point(self, ts_store):
        make_point(interval="30s")
        e1 = ts_store.create_retention_estimate(point_id="TS-1030", retention_days=30)["estimate"]
        s1 = ts_store.activate_from_estimate(e1["estimate_no"])["strategy"]
        e2 = ts_store.create_retention_estimate(point_id="TS-1030", retention_days=60)["estimate"]
        s2 = ts_store.activate_from_estimate(e2["estimate_no"])["strategy"]
        assert s2["id"] != s1["id"]
        current = ts_store.current_strategy("TS-1030")
        assert current["total"] == 1
        assert current["strategy"]["id"] == s2["id"]
        assert store.find("ts_store", s1["id"])["status"] == "已停用"


class TestPointUpdate:
    def test_generic_update_normalizes_interval_and_version(self, ts_point):
        point = make_point(interval="10s")
        entry, message = ts_point.update_entry(point["id"], {"采集间隔": "30秒"})
        assert entry is not None
        assert entry["采集间隔"] == "30s"
        assert entry["采样间隔秒数"] == 30
        assert int(entry["参数版本"]) == 1
        # 业务模块数量不被流程单据污染
        assert set(store.module_names()) == set(SEED_ROWS.keys())
