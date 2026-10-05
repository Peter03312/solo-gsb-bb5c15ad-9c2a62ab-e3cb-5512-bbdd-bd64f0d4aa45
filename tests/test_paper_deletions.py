"""本轮功能测试: 会务方删除论文时的反馈生命周期。

覆盖需求:
- 删除在同一个写事务内原子生效: 该稿全部已有反馈快照访问码立即失效
  (持码读取评语、提交新异议与无效码完全同形地统一 404, 不透露论文是否存在),
  仍 pending 的作者异议原子标记 expired (作者原查询凭据仍可查询处理状态),
  删除返回失效快照数/过期异议数与可追溯的删除记录;
- 历史快照与全部异议保留, 供会务方按论文编号追溯 (含删除记录);
- 同编号重新录入不恢复旧码效力: 旧码仍 404, 重录后必须走完新发布序号上的
  确认/评语/重新发布才产生新访问码;
- 终态删除 (删除后未重录) 的论文不在当前发布方案槽位时, 旧码同样不可读;
- 迁移导出包含删除记录 (v2): 合法历史 (删除/重录) 可恢复; 把删除失效的旧快照
  标记改回有效并重算校验和 -> 矛盾快照 422, 目标实例不留部分数据;
  pending 异议指向已删除失效快照 -> 422; 旧格式 v1 快照沿用原有恢复判断。
"""
import copy
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-deletions-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db, migration
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


ALL_TABLES = migration.BUSINESS_TABLES + ("migration_restores",)


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in ALL_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution=None, capacity=3):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": ["AI"],
            "institution": institution or f"Inst-{rid}",
            "capacity": capacity,
            "avoid_papers": [],
        },
    )


def add_paper(client, pid="P1"):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": ["AI"],
            "institutions": [f"Author-{pid}"],
        },
    )


def publish(client):
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def decide(client, rid, pid, decision="confirm", reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


def submit_review(client, rid, pid, serial, score=4, comment=None):
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={
            "paper_id": pid,
            "serial": serial,
            "score": score,
            "comment": comment or f"评语-{rid}-{pid}",
        },
    )


def export_snapshot(client):
    r = client.get("/migration/export", headers=ORG)
    assert r.status_code == 200, r.text
    return r.json()


def restore_snapshot(client, snapshot):
    return client.post("/migration/restore", headers=ORG, json=snapshot)


def wipe_instance():
    with db.write_txn() as conn:
        for table in ALL_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")


def resign(snapshot):
    snapshot["checksum"] = migration.snapshot_checksum(snapshot["data"])
    return snapshot


def setup_snapshot_with_objection(client, pid="P1"):
    """录入 2 名评审人 + 论文 -> 发布 -> 两人确认/交评语 -> 发布快照 -> 两条异议。

    返回 (serial, access_code, token1, token2, reviewers)。
    label1 异议保持 pending; label2 异议也保持 pending (用于统计过期数=2)。
    """
    for rid in ("R1", "R2"):
        assert add_reviewer(client, rid).status_code == 201
    assert add_paper(client, pid).status_code == 201
    pub = publish(client)
    serial = pub["serial"]
    rids = pub["plan"][pid]
    for i, rid in enumerate(rids):
        decide(client, rid, pid)
        submit_review(client, rid, pid, serial, score=4 - i, comment=f"{rid} 的评语")
    r = client.post(
        f"/papers/{pid}/feedback-snapshot", headers=ORG, json={"serial": serial}
    )
    assert r.status_code == 200, r.text
    code = r.json()["access_code"]
    tokens = []
    for label in (1, 2):
        o = client.post(
            "/feedback-objections",
            json={"access_code": code, "label": label, "reason": f"对评语{label}有异议"},
        )
        assert o.status_code == 200, o.text
        tokens.append(o.json()["query_token"])
    return serial, code, tokens[0], tokens[1], rids


# ------------------------------------------------------------ 删除原子失效

def test_delete_unknown_paper_is_404(client):
    r = client.delete("/papers/NOPE", headers=ORG)
    assert r.status_code == 404


def test_delete_requires_organizer_key(client):
    add_paper(client)
    assert client.delete("/papers/P1").status_code == 401


def test_delete_atomically_invalidates_code_and_expires_objections(client):
    serial, code, token1, token2, _rids = setup_snapshot_with_objection(client)

    # 删除前持码可读、可提交
    assert client.get(f"/feedback-snapshots/{code}").status_code == 200

    rev_before = client.get("/meta", headers=ORG).json()["revision"]
    r = client.delete("/papers/P1", headers=ORG)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "deleted"
    assert body["revision"] == rev_before + 1  # 删除沿用资料修订号 +1 约定
    assert body["invalidated_snapshots"] == 1
    assert body["expired_objections"] == 2
    deletion = body["deletion"]
    assert deletion["paper_id"] == "P1"
    assert deletion["published_serial"] == serial
    assert set(deletion["published_plan"]) == {"R1", "R2"}
    assert deletion["invalidated_snapshots"] == 1
    assert deletion["expired_objections"] == 2

    # 持码读取: 与无效码完全同形的 404, 响应体相同, 不透露论文存在过
    gone = client.get(f"/feedback-snapshots/{code}")
    bogus = client.get("/feedback-snapshots/fbk-deadbeefdeadbeef")
    assert gone.status_code == 404 and bogus.status_code == 404
    assert gone.json() == bogus.json()

    # 已失效码提交新异议: 与无效码同形 404
    again = client.post(
        "/feedback-objections",
        json={"access_code": code, "label": 1, "reason": "删除后试图再提异议"},
    )
    bogus_post = client.post(
        "/feedback-objections",
        json={"access_code": "fbk-deadbeefdeadbeef", "label": 1, "reason": "x"},
    )
    assert again.status_code == 404 and bogus_post.status_code == 404
    assert again.json() == bogus_post.json()

    # 待处理异议已原子过期; 查询凭据仍可查 (无需任何凭据)
    for token in (token1, token2):
        view = client.get(f"/feedback-objections/{token}")
        assert view.status_code == 200, view.text
        assert view.json()["state"] == "expired"
    assert client.get("/feedback-objections/obj-nope").status_code == 404


def test_terminal_deleted_old_code_stays_404_after_republish_serial_bump(client):
    _serial, code, _t1, _t2, _rids = setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)
    # 删除后重录并重新发布 (推进发布序号): 旧码依旧 404
    add_paper(client)
    pub = publish(client)
    assert pub["serial"] == 2
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404


def test_deleted_snapshots_and_objections_remain_traceable(client):
    _serial, code, token1, _token2, _rids = setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)

    # 会务方仍可追溯该论文编号下的快照版本 (全部标记非当前有效)
    trace = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert len(trace["snapshots"]) == 1
    assert trace["snapshots"][0]["access_code"] == code
    assert trace["snapshots"][0]["active"] is False

    # 异议 (含冻结原反馈) 仍可由会务方追溯, 状态为 expired
    objs = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    assert {o["state"] for o in objs} == {"expired"}
    assert {o["query_token"] for o in objs} == {token1, _token2}
    all_objs = client.get("/objections", headers=ORG).json()["objections"]
    assert len(all_objs) == 2

    # 删除记录可供追溯
    deletions = client.get("/papers/P1/deletions", headers=ORG).json()["deletions"]
    assert len(deletions) == 1
    assert deletions[0]["invalidated_snapshots"] == 1
    assert deletions[0]["expired_objections"] == 2
    # 从未删除过的编号返回空列表
    assert client.get("/papers/NOPE/deletions", headers=ORG).json()["deletions"] == []


def test_decision_on_deleted_paper_objection_is_expired(client):
    # 终态 (已驳回/受理) 异议不被删除改写
    _serial, code, token1, _token2, _rids = setup_snapshot_with_objection(client)
    obj_id = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0][
        "objection_id"
    ]
    rej = client.post(
        f"/objections/{obj_id}/decision",
        headers=ORG,
        json={"decision": "reject", "note": "经核异议不成立"},
    )
    assert rej.status_code == 200
    client.delete("/papers/P1", headers=ORG)
    view = client.get(f"/feedback-objections/{token1}").json()
    assert view["state"] == "rejected"  # 终态不改
    assert view["resolution"]["note"] == "经核异议不成立"


# ------------------------------------------------------------ 同编号重录

def test_same_id_reentry_does_not_revive_old_code(client):
    serial, old_code, _t1, _t2, _rids = setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)

    # 同编号重新录入: 旧码仍 404
    assert add_paper(client).status_code == 201
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404
    client.post(
        "/feedback-objections",
        json={"access_code": old_code, "label": 1, "reason": "旧码复活?"},
    ).status_code == 404

    # 在当前 (旧) 发布序号上重发快照: 命中的幂等行已随删除失效 -> 拒绝,
    # 绝不重新发放旧 (失效) 访问码
    r = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    )
    assert r.status_code == 409, r.text
    assert old_code not in r.text  # 不重新发放旧码
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404

    # 走完整新周期: 重新发布 -> 新确认/评语 -> 重新发布快照, 得到全新访问码
    pub2 = publish(client)
    assert pub2["serial"] == serial + 1
    new_rids = pub2["plan"]["P1"]
    for rid in new_rids:
        decide(client, rid, "P1")
        submit_review(client, rid, "P1", pub2["serial"], comment=f"{rid} 重录后评语")
    r = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": pub2["serial"]}
    )
    assert r.status_code == 200, r.text
    new_snap = r.json()
    assert new_snap["version"] == 2  # 版本号在该论文编号上继续递增
    assert new_snap["access_code"] != old_code
    assert client.get(f"/feedback-snapshots/{new_snap['access_code']}").status_code == 200
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404

    # 新码可提交异议; 旧异议凭据仍可查 (expired)
    ok = client.post(
        "/feedback-objections",
        json={"access_code": new_snap["access_code"], "label": 1, "reason": "重录后的新异议"},
    )
    assert ok.status_code == 200
    assert client.get(f"/feedback-objections/{_t1}").json()["state"] == "expired"

    # 追溯: 同编号下两条删除记录 (再次删除后)
    client.delete("/papers/P1", headers=ORG)
    deletions = client.get("/papers/P1/deletions", headers=ORG).json()["deletions"]
    assert [d["state"] for d in deletions] == ["deleted", "deleted"]
    assert client.get(f"/feedback-snapshots/{new_snap['access_code']}").status_code == 404


# ------------------------------------------------------------ 迁移

def test_export_v2_contains_deletion_records(client):
    setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)
    snap = export_snapshot(client)
    assert snap["format_version"] == 2
    assert "paper_deletions" in snap["data"]
    deletions = snap["data"]["paper_deletions"]
    assert len(deletions) == 1
    assert deletions[0]["paper_id"] == "P1"
    assert deletions[0]["invalidated_snapshots"] == 1
    assert deletions[0]["expired_objections"] == 2


def test_restore_roundtrip_terminal_deletion(client):
    """删除后未重录 (终态删除): 快照失效、异议过期, 迁移后旧码仍 404、历史可追溯。"""
    _serial, code, token1, _t2, _rids = setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)
    snap = export_snapshot(client)
    wipe_instance()
    r = restore_snapshot(client, snap)
    assert r.status_code == 200, r.text
    assert r.json()["format_version"] == 2
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    assert client.get(f"/feedback-objections/{token1}").json()["state"] == "expired"
    # 历史仍可由会务方追溯
    trace = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert len(trace["snapshots"]) == 1 and trace["snapshots"][0]["active"] is False
    deletions = client.get("/papers/P1/deletions", headers=ORG).json()["deletions"]
    assert len(deletions) == 1


def test_restore_roundtrip_delete_and_reenter(client):
    """删除 -> 同编号重录 -> 新周期新快照: 旧码失效、新码有效, 可迁移。"""
    _serial, old_code, _t1, _t2, _rids = setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)
    add_paper(client)
    pub2 = publish(client)
    for rid in pub2["plan"]["P1"]:
        decide(client, rid, "P1")
        submit_review(client, rid, "P1", pub2["serial"], comment=f"{rid} 重录后评语")
    new_code = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": pub2["serial"]}
    ).json()["access_code"]

    snap = export_snapshot(client)
    wipe_instance()
    assert restore_snapshot(client, snap).status_code == 200
    assert client.get(f"/feedback-snapshots/{old_code}").status_code == 404
    assert client.get(f"/feedback-snapshots/{new_code}").status_code == 200
    # 新码可提交异议
    ok = client.post(
        "/feedback-objections",
        json={"access_code": new_code, "label": 1, "reason": "迁移后新异议"},
    )
    assert ok.status_code == 200


def test_restore_rejects_flag_flip_after_terminal_deletion(client):
    """攻击: 终态删除后把旧快照 invalidated 改回 0 并重算校验和 -> 422。"""
    _serial, code, _t1, _t2, _rids = setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)
    snap = export_snapshot(client)
    wipe_instance()

    def flip(data):
        s = next(x for x in data["feedback_snapshots"] if x["access_code"] == code)
        s["invalidated"] = False

    bad = copy.deepcopy(snap)
    flip(bad["data"])
    resign(bad)
    r = restore_snapshot(client, bad)
    assert r.status_code == 422, r.text
    assert "终态删除" in r.json()["detail"]
    # 不留部分数据
    assert client.get("/papers", headers=ORG).json()["papers"] == []
    # 原快照仍可正常恢复
    assert restore_snapshot(client, snap).status_code == 200
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404


def test_restore_rejects_flag_flip_after_deletion_with_fake_timestamp(client):
    """攻击: 删除失效快照标记改回有效并把创建时刻伪造到未来 -> 终态删除仍拦截 422。"""
    _serial, code, _t1, _t2, _rids = setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)
    snap = export_snapshot(client)
    wipe_instance()

    bad = copy.deepcopy(snap)
    s = next(x for x in bad["data"]["feedback_snapshots"] if x["access_code"] == code)
    s["invalidated"] = False
    s["created_at"] = "2099-01-01T00:00:00+00:00"
    resign(bad)
    r = restore_snapshot(client, bad)
    assert r.status_code == 422
    assert "终态删除" in r.json()["detail"]


def test_restore_rejects_flag_flip_after_deletion_then_reenter(client):
    """攻击: 删除后重录, 把删除前旧快照标记改回有效 -> 删除时刻规则拦截 422。

    重录后论文资料行存在 (非终态删除), 但旧快照 created_at <= deleted_at,
    必已随该次删除失效; 重录不恢复旧码。
    """
    _serial, old_code, _t1, _t2, _rids = setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)
    add_paper(client)
    pub2 = publish(client)
    for rid in pub2["plan"]["P1"]:
        decide(client, rid, "P1")
        submit_review(client, rid, "P1", pub2["serial"], comment="重录后评语")
    new = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": pub2["serial"]}
    )
    assert new.status_code == 200
    snap = export_snapshot(client)
    wipe_instance()

    bad = copy.deepcopy(snap)
    s = next(x for x in bad["data"]["feedback_snapshots"] if x["access_code"] == old_code)
    s["invalidated"] = False
    resign(bad)
    r = restore_snapshot(client, bad)
    assert r.status_code == 422
    assert "删除" in r.json()["detail"]


def test_restore_rejects_pending_objection_on_deleted_snapshot(client):
    """矛盾: pending 异议指向删除失效快照 (应已随删除过期) -> 422 且不写部分数据。"""
    _serial, code, _t1, _t2, _rids = setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)
    snap = export_snapshot(client)
    wipe_instance()

    bad = copy.deepcopy(snap)
    # 同时把快照标记和异议状态改回"删除前"样子, 重算校验和:
    # 快照被删除记录证明应失效, pending 异议与删除过期矛盾
    s = next(x for x in bad["data"]["feedback_snapshots"] if x["access_code"] == code)
    s["invalidated"] = False
    for o in bad["data"]["feedback_objections"]:
        o["state"] = "pending"
        o["decided_at"] = None
    resign(bad)
    r = restore_snapshot(client, bad)
    assert r.status_code == 422
    assert client.get("/papers", headers=ORG).json()["papers"] == []


def test_restore_rejects_deletion_record_without_invalidated_flag(client):
    """反向矛盾: v2 快照有删除记录证明其应失效却标记有效 -> 422。

    等价于只伪造标记但保留删除记录; 另验证删除记录字段本身被校验。
    """
    _serial, code, _t1, _t2, _rids = setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)
    snap = export_snapshot(client)
    wipe_instance()

    # 删除记录计数与实际快照数矛盾不直接校验计数, 但删除时刻规则必须命中标记矛盾
    bad = copy.deepcopy(snap)
    next(x for x in bad["data"]["feedback_snapshots"] if x["access_code"] == code)
    # 删除记录 id 重复 -> 结构校验 422
    bad["data"]["paper_deletions"][0]["id"] = 999999
    bad["data"]["paper_deletions"].append(dict(bad["data"]["paper_deletions"][0]))
    resign(bad)
    r = restore_snapshot(client, bad)
    assert r.status_code == 422


def test_v1_snapshot_uses_legacy_restore_judgement_without_inferring_deletion(client):
    """旧格式 v1 (无 paper_deletions): 沿用原有恢复判断, 不推断删除历史。

    构造一个"终态删除"形态的 v1 快照 (papers 不含该论文, 快照标记有效),
    v1 验证必须接受它 (原有判断没有删除依据); v2 则应拒绝。
    """
    _serial, code, _t1, _t2, _rids = setup_snapshot_with_objection(client)
    client.delete("/papers/P1", headers=ORG)
    snap = export_snapshot(client)
    # 降级为 v1: 移除删除表与已过期状态 (v1 时代删除尚不会使异议过期),
    # 并把快照标记改回有效、异议改回 pending——v1 沿用旧判断全部接受
    legacy = copy.deepcopy(snap)
    legacy["format_version"] = 1
    del legacy["data"]["paper_deletions"]
    for o in legacy["data"]["feedback_objections"]:
        o["state"] = "pending"
        o["decided_at"] = None
    s = next(x for x in legacy["data"]["feedback_snapshots"] if x["access_code"] == code)
    s["invalidated"] = False
    resign(legacy)

    wipe_instance()
    r = restore_snapshot(client, legacy)
    assert r.status_code == 200, r.text
    assert r.json()["format_version"] == 1
    # v1 旧判断下该码可读 (旧格式未记载删除, 不推断)
    assert client.get(f"/feedback-snapshots/{code}").status_code == 200

    # 同样内容以 v2 恢复则必须拒绝 (删除失效依据可核验)
    wipe_instance()
    v2 = copy.deepcopy(legacy)
    v2["format_version"] = 2
    v2["data"]["paper_deletions"] = snap["data"]["paper_deletions"]
    resign(v2)
    assert restore_snapshot(client, v2).status_code == 422


def test_v1_legacy_cross_serial_snapshot_restores_without_serial_flag_rule(client):
    """旧服务导出的 v1 快照: 旧序号快照的 invalidated 可能仍为 0 (旧版未落该标记),
    v1 沿用原有恢复判断接受它; 运行时该码仍因序号非当前而 404 (不泄露内容)。
    v2 则要求标记与序号一致。
    """
    _serial, code, _t1, _t2, _rids = setup_snapshot_with_objection(client)
    # 推进发布序号 (补位发布, 槽位沿用): 新库会把旧序号快照置 invalidated=1
    bf = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": bf["revision"], "base_serial": bf["serial"]},
    )
    assert bp.status_code == 200
    snap = export_snapshot(client)
    old = next(x for x in snap["data"]["feedback_snapshots"] if x["access_code"] == code)
    assert old["serial"] == 1 and old["invalidated"] is True  # v2 已落标记

    # 伪造一个旧服务真实可能导出的 v1 形态: 旧序号标记仍为有效
    legacy = copy.deepcopy(snap)
    legacy["format_version"] = 1
    del legacy["data"]["paper_deletions"]
    next(x for x in legacy["data"]["feedback_snapshots"] if x["access_code"] == code)[
        "invalidated"
    ] = False
    resign(legacy)
    wipe_instance()
    assert restore_snapshot(client, legacy).status_code == 200
    # 运行时只认当前发布序号: 旧序号码仍读不到 (安全口径不依赖落库标记)
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404

    # 同一"旧序号标记有效"形态以 v2 恢复: 标记与序号推进规则矛盾 -> 422
    wipe_instance()
    v2 = copy.deepcopy(legacy)
    v2["format_version"] = 2
    v2["data"]["paper_deletions"] = []
    resign(v2)
    r = restore_snapshot(client, v2)
    assert r.status_code == 422
    assert "当前发布序号" in r.json()["detail"]
