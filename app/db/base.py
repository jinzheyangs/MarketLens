"""声明式 ORM 基类，为 Alembic 迁移提供稳定的约束命名约定。"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 索引/唯一约束/检查约束/外键/主键的统一命名模板，保证迁移脚本可预测
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类，挂载统一命名约定的元数据。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
