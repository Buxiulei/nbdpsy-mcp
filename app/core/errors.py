"""对外 REST 错误契约的专用异常。"""


class NotFoundError(ValueError):
    """资源不存在(账号/任务/运营者/check_id)→ HTTP 404。

    继承 ValueError:未升级的旧调用方(按 ValueError 捕获/断言)行为不变;
    Starlette handler 查找按异常类精确优先,NotFoundError 走 404,裸 ValueError 走 400。
    """
