"""Shared bounded keyset pagination for alphabetically ordered master data."""

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Query
from sqlalchemy import RowMapping

from spm_api.errors import ApiError, Model


class Page[T: Model](Model):
    items: list[T]
    next_cursor: str | None = None


class PageQuery:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        cursor: Annotated[str | None, Query(max_length=1500)] = None,
        q: Annotated[str, Query(max_length=100)] = "",
    ) -> None:
        self.limit = limit
        self.q = q.strip()
        self.cursor_value: str | None = None
        self.cursor_id: str | None = None
        if cursor:
            try:
                decoded = json.loads(
                    base64.b64decode(cursor.encode(), altchars=b"-_", validate=True)
                )
                if (
                    not isinstance(decoded, list)
                    or len(decoded) != 2
                    or not isinstance(decoded[0], str)
                    or not isinstance(decoded[1], str)
                ):
                    raise ValueError("Invalid cursor")
                self.cursor_value, self.cursor_id = decoded[0], str(UUID(decoded[1]))
            except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
                raise ApiError(422, "INVALID_CURSOR", "The pagination cursor is invalid") from exc

    def params(self) -> dict[str, Any]:
        escaped = self.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return {
            "cursor_value": self.cursor_value,
            "cursor_id": self.cursor_id,
            "search": f"%{escaped}%",
            "limit": self.limit + 1,
        }


PageDep = Annotated[PageQuery, Depends()]


def paginate[T: Model](
    rows: Sequence[RowMapping],
    query: PageQuery,
    key: str,
    convert: Callable[[RowMapping], T],
) -> Page[T]:
    cursor = None
    if len(rows) > query.limit:
        row = rows[query.limit - 1]
        cursor = base64.urlsafe_b64encode(json.dumps([row[key], row["id"]]).encode()).decode()
    return Page(items=[convert(row) for row in rows[: query.limit]], next_cursor=cursor)
