"""管理员引导命令行工具（开通租户、预置用户等）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.enums import AuditAction, ResourceType, TenantRole, TenantStatus
from app.core.config import Settings, get_settings
from app.core.context import tenant_context
from app.core.errors import AppError
from app.db.models import Tenant, User
from app.db.rls import install_rls_context
from app.db.session import create_engine, create_session_factory
from app.db.tenant_guard import install_tenant_guard
from app.schemas.tenancy import DisplayName, Slug
from app.schemas.users import UserProvisionRequest
from app.services.audit import record_event

_slug_adapter: TypeAdapter[str] = TypeAdapter(Slug)
_name_adapter: TypeAdapter[str] = TypeAdapter(DisplayName)


async def create_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    slug: str,
    name: str,
) -> Tenant:
    tenant = Tenant(id=uuid4(), slug=slug, name=name)
    with tenant_context(tenant.id):
        async with session_factory() as session, session.begin():
            session.add(tenant)
            await session.flush()
            record_event(
                session,
                tenant_id=tenant.id,
                actor_user_id=None,
                action=AuditAction.TENANT_CREATED,
                resource_type=ResourceType.TENANT,
                resource_id=tenant.id,
                details={"slug": tenant.slug},
            )
    return tenant


async def provision_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_slug: str,
    payload: UserProvisionRequest,
    issuer: str | None,
) -> User:
    if not issuer:
        raise ValueError("OIDC_ISSUER must be configured before provisioning users.")
    async with session_factory() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
    if tenant is None:
        raise LookupError(f"Tenant '{tenant_slug}' was not found.")
    if tenant.status != TenantStatus.ACTIVE:
        raise ValueError(f"Tenant '{tenant_slug}' is not active.")

    user = User(
        id=uuid4(),
        tenant_id=tenant.id,
        issuer=issuer,
        subject=payload.subject,
        email=str(payload.email),
        display_name=payload.display_name,
        tenant_role=payload.tenant_role.value,
    )
    with tenant_context(tenant.id):
        async with session_factory() as session, session.begin():
            session.add(user)
            await session.flush()
            record_event(
                session,
                tenant_id=tenant.id,
                actor_user_id=None,
                action=AuditAction.USER_PROVISIONED,
                resource_type=ResourceType.USER,
                resource_id=user.id,
                details={"tenant_role": user.tenant_role},
            )
    return user


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marketscope",
        description="Administrative bootstrap commands for the market intelligence backend.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    tenant_parser = subparsers.add_parser("create-tenant", help="Create a tenant registry entry.")
    tenant_parser.add_argument("--slug", required=True)
    tenant_parser.add_argument("--name", required=True)

    user_parser = subparsers.add_parser(
        "provision-user", help="Provision a user for the configured OIDC issuer."
    )
    user_parser.add_argument("--tenant-slug", required=True)
    user_parser.add_argument("--subject", required=True)
    user_parser.add_argument("--email", required=True)
    user_parser.add_argument("--display-name", required=True)
    user_parser.add_argument(
        "--tenant-role",
        choices=[role.value for role in TenantRole],
        default=TenantRole.MEMBER.value,
    )
    return parser


async def _run(args: argparse.Namespace, settings: Settings) -> dict[str, str]:
    engine = create_engine(settings)
    try:
        session_factory = create_session_factory(engine)
        if args.command == "create-tenant":
            slug = _slug_adapter.validate_python(args.slug)
            name = _name_adapter.validate_python(args.name)
            tenant = await create_tenant(session_factory, slug=slug, name=name)
            return {"id": str(tenant.id), "slug": tenant.slug, "name": tenant.name}

        payload = UserProvisionRequest(
            subject=args.subject,
            email=args.email,
            display_name=args.display_name,
            tenant_role=TenantRole(args.tenant_role),
        )
        user = await provision_user(
            session_factory,
            tenant_slug=args.tenant_slug,
            payload=payload,
            issuer=settings.oidc_issuer,
        )
        return {
            "id": str(user.id),
            "tenant_id": str(user.tenant_id),
            "email": user.email,
            "tenant_role": user.tenant_role,
        }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    install_tenant_guard()
    install_rls_context()
    try:
        settings = get_settings()
        result = asyncio.run(_run(args, settings))
    except ValidationError as exc:
        print(f"Invalid input: {exc}", file=sys.stderr)
        return 2
    except IntegrityError:
        print("A record with the same unique key already exists.", file=sys.stderr)
        return 1
    except AppError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    except (LookupError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
