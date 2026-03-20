import logging
import uuid

import inflect as _inflect
from litestar import Controller, delete, get, patch, post
from litestar.di import Provide
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.automap import AutomapBase
from sqlalchemy.orm import DeclarativeMeta

from .config import settings
from .persistence import get_async_session, set_role

_logger = logging.getLogger(settings.app_name)

inflect = _inflect.engine()
inflect.classical(names=0)
# tags_metadata = []


# http://api.example.com/v1/store/items/{id}✅
# http://api.example.com/v1/store/employees/{id}✅
# http://api.example.com/v1/store/employees/{id}/addresses
# /device-management/managed-devices/{id}/scripts/{id}/execute	//DON't DO THIS!
# /device-management/managed-devices/{id}/scripts/{id}/status		//POST request with action=execute
# _ protects keywords in pagination and advanced filtering
# /api/books?_offset=0&_limit=10&_orderBy=author desc,title asc
# basic FILTER on equality of fields
# http://api.example.com/v1/store/items?group=124
# http://api.example.com/v1/store/employees?department=IT&region=USA
# advanced FILTER on multiple fields using expressions
# /api/books?page=0&size=20&$filter=author eq 'Fitzgerald'
# /api/books?page=0&size=20&$filter=(author eq 'Fitzgerald' or name eq 'Redmond') and price lt 2.55
# /v1.0/people?$filter=name eq 'david'&$orderBy=hireDate
# https://docs.oasis-open.org/odata/odata/v4.01/odata-v4.01-part2-url-conventions.html#_Toc31361038


# Litestar conversion starts here


def create_controller(table_name: str, item: any) -> Controller:
    orm_class: DeclarativeMeta = Base.classes.get(table_name)
    table: Table = orm_class.__table__
    pkeys = table.primary_key.columns.keys()
    pk_input = models_registry[table_name].pk_input

    # router.openapi_tags.append({"name": table.name, "description": table.comment})
    class ItemController(Controller):
        path = table_name
        dependencies = {"session": Provide(get_async_session)}
        tags = [f"{table.name.capitalize()}: {table.comment if table.comment else ''}"]

        @post()
        async def create(self, session: AsyncSession, data: item.model) -> item.model:
            await set_role(session)
            new_item = orm_class(**data.model_dump(exclude_none=True))
            session.add(new_item)
            await session.commit()
            await session.refresh(new_item)
            return new_item

        @get(path=f"/{'/'.join([f'{{{pk}:uuid}}' for pk in pkeys])}")
        async def get(
            self,
            pk: pk_input,  # type: ignore
            session: AsyncSession,
        ) -> item.model:  # type: ignore
            await set_role(session)
            return await session.get(orm_class, pk.model_dump())

        @patch(path=f"/{'/'.join([f'{{{pk}:uuid}}' for pk in pkeys])}")
        async def update(self, session: AsyncSession, id: uuid.UUID, data: item.model) -> item.model:
            await set_role(session)
            record = await session.get(orm_class, id)
            for k, v in data.model_dump(exclude_unset=True, exclude_none=True).items():
                setattr(record, k, v)
            session.add(record)
            await session.commit()
            return record

        @delete(path=f"/{'/'.join([f'{{{pk}:uuid}}' for pk in pkeys])}")
        async def delete(self, session: AsyncSession, id: uuid.UUID) -> None:
            await set_role(session)
            record = await session.get(orm_class, id)
            await session.delete(record)
            await session.commit()

    return ItemController


def build_controllers(_base: AutomapBase, _registry):
    global Base, models_registry
    Base = _base
    models_registry = _registry
    controllers: list[Controller] = []
    for key, item in models_registry.items():
        controller = create_controller(key, item)
        controllers.append(controller)
    return controllers
