"""领域异常。

所有面向用户的错误都带中文说明；需要“整份/整批拒绝”的导入流程
捕获 :class:`ValidationError` 后回滚事务，保证已有资料保持原样。
"""


class QReleaseError(Exception):
    """工具内所有自定义异常的基类。"""


class ValidationError(QReleaseError):
    """输入资料不合规，调用方应放弃当前整份/整批写入。"""


class NotFoundError(QReleaseError):
    """查询对象不存在。"""


class ConflictError(QReleaseError):
    """放行请求编号相同但内容不同等冲突场景。"""
