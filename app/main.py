"""ASGI 应用入口。

由 uvicorn 直接加载：``uvicorn app.main:app``。
本模块只负责调用应用工厂创建全局 ``app`` 实例，不写任何业务逻辑。
"""

from app.factory import create_app

# 全局 ASGI 应用实例，uvicorn / 测试套件都从这里挂载
app = create_app()
