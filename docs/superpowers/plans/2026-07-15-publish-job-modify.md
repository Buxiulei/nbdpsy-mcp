# 待发定时任务原地修改 PATCH 端点 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。Steps 用 checkbox。

**Goal:** 加 `PATCH /api/publish-jobs/{job_id}`,让 pending 发布任务原地改时间与内容(不必取消重建)。

**Architecture:** 复用现有 publish_rest 的鉴权/校验/视图件;条件更新 `WHERE status='pending'` 防竞态;
Pydantic `model_fields_set` 做部分更新;清空 schedule_time → 转立即发并 submit。

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async(aiosqlite)+ Pydantic v2 + pytest-asyncio。

**Spec:** `docs/design/2026-07-15-publish-job-modify-design.md`

## Global Constraints

- 解释器 `/home/roots/nbdpsy-server/.venv/bin/python`;测试 `source .../activate && python -m pytest tests/ -q`(cwd=worktree 根,worktree 无独立 venv 借主解释器)。
- 全中文注释无 emoji;commit `type(scope): 描述`;显式列文件,禁 `git add -A`。
- 仅 `pending` 可改;非 pending 返回 `{"ok": false, "status": <当前态>}` 不报错。
- `account_id` 不可改;`_MAX_IMAGES=18`;schedule_time 复用 `_parse_schedule_time`(ISO8601 带时区 → naive UTC)。

---

### Task 1: PATCH 端点 + manifest + 测试

**Files:**
- Modify: `app/http/publish_rest.py`(加 `PublishJobPatchRequest` 模型、`patch_publish_job_endpoint`、`MANIFEST_ENTRIES` 补一条;`from sqlalchemy import select` 改为 `select, update`)
- Test: `tests/test_publish_rest.py`(加 PATCH 用例;复用 `rest_client` / `_install_fake_scheduler` / `_make_operator_with_access` / `_seed_job` / `bearer`)

**Interfaces:**
- Consumes:`_parse_schedule_time(raw) -> datetime|None`、`_job_view(job) -> dict`、`_MAX_IMAGES`、
  `assert_account_access(operator, account_id, session)`、`get_active_scheduler().submit(job_id)`、
  `current_operator()`、`get_session()`、`NotFoundError`、测试助手 `_seed_job(**PublishJob字段) -> PublishJob`。
- Produces:`PATCH /api/publish-jobs/{job_id}` → `{"ok": true, "job": {...}}` | `{"ok": false, "status": str}`。

- [ ] **Step 1: 写失败测试(先读 tests/test_publish_rest.py 顶部助手签名 `_seed_job`/`bearer`/`_make_operator_with_access` 再写)**

在 `tests/test_publish_rest.py` 末尾追加:
```python
async def test_patch_pending_updates_schedule_and_content(tmp_path, monkeypatch):
    """pending 任务改 schedule_time + title → 持久化,返回 ok:true + 更新后视图。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _make_account("号")  # 见文件内既有建账号助手;若名不同按实际
        op_key = "opkey-patch-1"
        await _make_operator_with_access(acc, key=op_key)
        job = await _seed_job(account_id=acc, status="pending", title="旧标题")
        r = await c.patch(
            f"/api/publish-jobs/{job.id}",
            json={"title": "新标题", "schedule_time": "2026-09-09T09:00:00+08:00"},
            headers=bearer(op_key),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["job"]["title"] == "新标题"
        assert body["job"]["schedule_time"] == "2026-09-09T01:00:00"  # +08:00 → naive UTC


async def test_patch_clear_schedule_enqueues(tmp_path, monkeypatch):
    """schedule_time 显式 null → 转立即发,submit 被调用。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        fake = _install_fake_scheduler()
        acc = await _make_account("号")
        op_key = "opkey-patch-2"
        await _make_operator_with_access(acc, key=op_key)
        from datetime import datetime
        job = await _seed_job(account_id=acc, status="pending",
                              schedule_time=datetime(2026, 9, 9, 1, 0, 0))
        r = await c.patch(f"/api/publish-jobs/{job.id}",
                          json={"schedule_time": None}, headers=bearer(op_key))
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert job.id in fake.submitted  # 假调度器记录 submit


async def test_patch_non_pending_no_change(tmp_path, monkeypatch):
    """非 pending(published)改 → ok:false + 当前态,DB 不变。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _make_account("号")
        op_key = "opkey-patch-3"
        await _make_operator_with_access(acc, key=op_key)
        job = await _seed_job(account_id=acc, status="published", title="定稿")
        r = await c.patch(f"/api/publish-jobs/{job.id}",
                          json={"title": "想改"}, headers=bearer(op_key))
        assert r.status_code == 200
        assert r.json() == {"ok": False, "status": "published"}


async def test_patch_rejects_empty_and_over_images(tmp_path, monkeypatch):
    """images 传空 → 400;传 19 张 → 400。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _make_account("号")
        op_key = "opkey-patch-4"
        await _make_operator_with_access(acc, key=op_key)
        job = await _seed_job(account_id=acc, status="pending")
        r0 = await c.patch(f"/api/publish-jobs/{job.id}",
                           json={"images": []}, headers=bearer(op_key))
        assert r0.status_code == 400, r0.text
        r1 = await c.patch(f"/api/publish-jobs/{job.id}",
                           json={"images": ["u"] * 19}, headers=bearer(op_key))
        assert r1.status_code == 400, r1.text


async def test_patch_404_and_403(tmp_path, monkeypatch):
    """job 不存在 → 404;无 access → 403。"""
    async with rest_client(tmp_path, monkeypatch) as c:
        _install_fake_scheduler()
        acc = await _make_account("号")
        owner_key, intruder_key = "opkey-owner", "opkey-intruder"
        await _make_operator_with_access(acc, key=owner_key)
        await make_operator(intruder_key)  # 未授权
        job = await _seed_job(account_id=acc, status="pending")
        r404 = await c.patch("/api/publish-jobs/999999",
                             json={"title": "x"}, headers=bearer(owner_key))
        assert r404.status_code == 404
        r403 = await c.patch(f"/api/publish-jobs/{job.id}",
                             json={"title": "x"}, headers=bearer(intruder_key))
        assert r403.status_code == 403
```
注:`_make_account` / `_seed_job` / `fake.submitted` 用文件内**实际**的助手名与假调度器记录字段——
Step 1 先读文件顶部(第 30-80 行)确认真实签名,按实际改;`_FakeScheduler` 若记录字段不叫
`submitted`(如 `submit` 收到的 id 列表)按实际断言。

- [ ] **Step 2: 确认失败**

Run: `source /home/roots/nbdpsy-server/.venv/bin/activate && python -m pytest tests/test_publish_rest.py -q -k patch`
Expected: FAIL(404 端点不存在 / method not allowed)。

- [ ] **Step 3: 实现端点**

`app/http/publish_rest.py`:import 改 `from sqlalchemy import select, update`;加模型 + 端点:
```python
class PublishJobPatchRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    images: list | None = None
    topics: list[str] | None = None
    schedule_time: str | None = None


@router.patch("/api/publish-jobs/{job_id}")
async def patch_publish_job_endpoint(job_id: int, payload: PublishJobPatchRequest) -> dict:
    """原地修改待发(pending)任务:改 schedule_time / title / content / images / topics。

    仅 pending 可改;非 pending 返回 {ok:false,status}。PATCH 部分更新:只改请求体里显式出现
    的字段(model_fields_set);schedule_time 显式 null=清空转立即发并 submit。条件更新
    WHERE status='pending' 防与 scan_once 抢占的竞态,rowcount=0 视为已被抢走。
    """
    operator = current_operator()
    async with get_session() as session:
        job = await session.get(PublishJob, job_id)
        if job is None:
            raise NotFoundError(f"发布任务 {job_id} 不存在")
        await assert_account_access(operator, job.account_id, session)
        if job.status != "pending":
            return {"ok": False, "status": job.status}

        fields = payload.model_fields_set
        changes: dict = {}
        if "title" in fields:
            changes["title"] = payload.title
        if "content" in fields:
            changes["content"] = payload.content
        if "images" in fields:
            imgs = payload.images or []
            if not imgs:
                raise ValueError("图文笔记至少需要 1 张图片")
            if len(imgs) > _MAX_IMAGES:
                raise ValueError(f"最多 {_MAX_IMAGES} 张图片")
            changes["images_json"] = json.dumps(imgs, ensure_ascii=False)
        if "topics" in fields:
            changes["topics_json"] = json.dumps(payload.topics or [], ensure_ascii=False)
        schedule_cleared = False
        if "schedule_time" in fields:
            parsed = _parse_schedule_time(payload.schedule_time)
            changes["schedule_time"] = parsed
            schedule_cleared = parsed is None

        if not changes:
            return {"ok": True, "job": _job_view(job)}

        # 条件更新:仅当仍为 pending 才落库,防与 scan_once 的 mark_publishing 抢占。
        result = await session.execute(
            update(PublishJob)
            .where(PublishJob.id == job_id, PublishJob.status == "pending")
            .values(**changes)
        )
        await session.commit()
        if result.rowcount == 0:
            fresh = await session.get(PublishJob, job_id)
            return {"ok": False, "status": fresh.status if fresh else "unknown"}
        if schedule_cleared:
            get_active_scheduler().submit(job_id)
        await session.refresh(job)
        return {"ok": True, "job": _job_view(job)}
```

- [ ] **Step 4: 补 manifest 条目**

在 `MANIFEST_ENTRIES` 列表(cancel 条目后)加:
```python
    {
        "method": "PATCH", "path": "/api/publish-jobs/{job_id}",
        "summary": "原地修改待发(pending)定时任务:改时间/标题/正文/图片/话题",
        "admin_only": False,
        "params": {
            "job_id": "path,int",
            "title": "body,str|None(省略=不改)",
            "content": "body,str|None(省略=不改)",
            "images": "body,list|None(省略=不改;传则 1-18 项,越界 400)",
            "topics": "body,list[str]|None(省略=不改)",
            "schedule_time": "body,str|None(省略=不改;显式 null=清空转立即发;"
                              "ISO8601 带时区如 2026-01-01T09:00:00+08:00)",
        },
        "returns": "{ok:true, job:<同 GET 单条视图>} 改成功;{ok:false, status:<当前态>} 非 pending 改不了",
        "errors": "400=images 越界;403=无该账号 access;404=job 不存在",
        "notes": "仅 pending 可改(定时未到期/失败等待重试均属 pending);publishing/published/failed/"
                 "canceled 一律 ok:false。已在发/已终态的任务改不动,需另建新任务。",
    },
```

- [ ] **Step 5: 跑测试通过 + 全量不回归**

Run: `python -m pytest tests/test_publish_rest.py -q` → 全绿(含新 PATCH 用例)。
Run: `python -m pytest tests/ -q` → 全绿(不回归 create/list/cancel/get + manifest 漂移测试)。
若 manifest 漂移测试(test_manifest.py)因新增端点失败,那是**预期**——它校验声明与注册一致,
新端点两侧都加了就应自洽通过;若仍红,核对 MANIFEST_ENTRIES 的 method/path 与 @router.patch 完全一致。

- [ ] **Step 6: 提交**

```bash
git add app/http/publish_rest.py tests/test_publish_rest.py
git commit -m "feat(publish): 待发任务原地修改 PATCH 端点——pending 可改时间/内容,条件更新防抢占"
```

## Self-Review 对照

- schedule_time 改/清空/不动 → Step 3 三分支 + 测试覆盖 ✓
- 仅 pending 可改 + 条件更新防竞态 → Step 3 fast-path + WHERE guard + rowcount ✓
- images 校验复用 → Step 3 ✓;404/403 → Step 3 ✓;manifest → Step 4 ✓
- account_id 不可改 → 请求体不含该字段 ✓
