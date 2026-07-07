# pytest 会话级公共 fixture。
#
# 关键背景:app.core.db 的 engine 在 import 时依 settings.DATABASE_URL 建好,
# 测试仅 monkeypatch 环境变量并不会重建它。因此这里提供独立的 `db` fixture,
# 为每个测试单建临时 sqlite async engine + sessionmaker,建表 → yield 会话 →
# 结束 drop/dispose 清理,彻底与生产库文件隔离。后续 Task 0.4 的模型测试复用它。

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest.fixture(scope="session", autouse=True)
def _debug_mode_for_tests():
    """全测试会话置 DEBUG=True,放行 create_app 的 SECRET_KEY 生产闸(N2)。

    单测用默认占位 SECRET_KEY;生产闸仅在 DEBUG=False 才强制非默认 key。DEBUG 在 app 内除该闸
    外无其它读取,置 True 无副作用。N2 的闸测试用函数级 monkeypatch 覆写 DEBUG=False 独立验证。
    """
    from app.core.config import settings

    original = settings.DEBUG
    settings.DEBUG = True
    yield
    settings.DEBUG = original


@pytest_asyncio.fixture
async def db(tmp_path):
    """每个测试独立的临时 sqlite AsyncSession,自动建表并在结束时清理。"""
    from app.core.db import Base

    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = create_async_engine(url, future=True)

    # 导入模型完成注册后建表(当前模型为空亦不报错,Task 0.4 起填充)
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    session = session_factory()
    try:
        yield session
    finally:
        await session.close()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def db_factory(tmp_path):
    """隔离的 async_sessionmaker(会话工厂),供需要多会话的组件用(如发布调度器)。

    与 db fixture 同源(每测试独立临时 sqlite + 建表 + 结束清理),但暴露的是会话工厂
    而非单一会话——调度器/队列每次操作各开一个短事务会话,须共享同一底层引擎。
    """
    from app.core.db import Base

    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = create_async_engine(url, future=True)

    import app.models  # noqa: F401  触发模型注册到 Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield session_factory
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
