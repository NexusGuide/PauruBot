"""Subcategories, one level deep (e.g. VOLUME › PER-USER / PER-GB).

Plans sit in any category. A buyer first sees the top-level categories that have something on
sale beneath them, then a category's subcategories. A category that is switched off takes its
subcategories (and their plans) off sale with it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import ColumnElement, Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from paurubot.db.models import Category

SEP = " › "


def open_ids(shop_id: int | ColumnElement[int]) -> Select[Any]:
    """Ids of the shop's categories whose plans are on sale: switched on, and so is the one above."""
    parent = aliased(Category)
    return (
        select(Category.id)
        .outerjoin(parent, parent.id == Category.parent_id)
        .where(
            Category.shop_id == shop_id,
            Category.is_active.is_(True),
            or_(Category.parent_id.is_(None), parent.is_active.is_(True)),
        )
    )


def _order(rows: Iterable[Category]) -> list[Category]:
    return sorted(rows, key=lambda c: (c.sort, c.id))


async def _parents(session: AsyncSession, cats: Iterable[Category]) -> dict[int, Category]:
    ids = {c.parent_id for c in cats if c.parent_id is not None}
    if not ids:
        return {}
    rows = await session.scalars(select(Category).where(Category.id.in_(ids)))
    return {row.id: row for row in rows}


async def roots(session: AsyncSession, used: Iterable[Category]) -> list[Category]:
    """The first screen: each category with plans, or the category above it, once."""
    used = list(used)
    parents = await _parents(session, used)
    out: dict[int, Category] = {}
    for cat in used:
        top = parents.get(cat.parent_id) if cat.parent_id is not None else cat
        if top is not None and top.is_active:
            out[top.id] = top
    return _order(out.values())


def children(used: Iterable[Category], parent_id: int) -> list[Category]:
    """The subcategories of `parent_id` that have plans on sale."""
    return _order(c for c in used if c.parent_id == parent_id)


async def labels(session: AsyncSession, cats: Iterable[Category]) -> dict[int, str]:
    """Names to show in one flat list: a subcategory reads «VOLUME › PER-USER»."""
    cats = list(cats)
    parents = await _parents(session, cats)
    return {
        c.id: f"{parents[c.parent_id].title}{SEP}{c.title}" if c.parent_id in parents else c.title
        for c in cats
    }


class TreeError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


async def check_parent(session: AsyncSession, category: Category | None, parent: Category | None) -> None:
    """One level only: the parent is a top-level category, and a category with subcategories
    cannot go under another one. Raises TreeError("category_too_deep") otherwise."""
    if parent is None:
        return
    if category is not None and parent.id == category.id:
        raise TreeError("category_too_deep")
    if parent.parent_id is not None:
        raise TreeError("category_too_deep")
    if category is not None and category.id is not None:
        has_children = await session.scalar(
            select(Category.id).where(Category.parent_id == category.id).limit(1)
        )
        if has_children is not None:
            raise TreeError("category_too_deep")
