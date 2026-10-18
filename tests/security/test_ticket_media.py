"""Ticket media uses ticket permissions and tenant scope before any file I/O."""

import pytest

from paurubot.auth import rbac
from paurubot.db.models import StaffRole, Ticket, TicketMessage
from tests.security.test_admin_api import env as env
from tests.security.test_admin_api import http as http
from tests.security.test_admin_api import login

pytestmark = pytest.mark.security


async def test_media_permission_matrix_shop_and_internal_scope(session, http, env) -> None:  # type: ignore[no-untyped-def]
    ticket = Ticket(shop_id=env.shop.id, customer_id=env.reseller_customer_id, subject="Own")
    other = Ticket(shop_id=env.shop.id, customer_id=env.customer_id, subject="Not reseller's")
    session.add_all([ticket, other])
    await session.flush()
    public = TicketMessage(
        ticket_id=ticket.id,
        author_type="staff",
        attachment_data=b"log",
        attachment_size=3,
        attachment_mime="text/plain",
        attachment_name="attachment.txt",
    )
    note = TicketMessage(
        ticket_id=ticket.id,
        author_type="staff",
        is_internal=True,
        attachment_data=b"private",
        attachment_size=7,
        attachment_mime="text/plain",
        attachment_name="attachment.txt",
    )
    foreign = TicketMessage(
        ticket_id=other.id,
        author_type="staff",
        attachment_data=b"other",
        attachment_size=5,
        attachment_mime="text/plain",
        attachment_name="attachment.txt",
    )
    session.add_all([public, note, foreign])
    await session.commit()
    path = f"/api/admin/tickets/{ticket.id}/messages/{public.id}/attachment"
    note_path = f"/api/admin/tickets/{ticket.id}/messages/{note.id}/attachment"
    assert (await http.get(path)).status_code == 401
    for role in StaffRole:
        staff = await login(http, f"u_{role.value}")
        response = await staff.call("GET", path)
        allowed = rbac.allowed(role, "tickets.read")
        assert response.status_code == (200 if allowed else 403), (role, response.text)
        internal = await staff.call("GET", note_path)
        assert internal.status_code == (403 if not allowed else 404 if role == StaffRole.RESELLER else 200)
    reseller = await login(http, "u_reseller")
    assert (
        await reseller.call("GET", f"/api/admin/tickets/{other.id}/messages/{foreign.id}/attachment")
    ).status_code == 404
    owner = await login(http, "u_owner")
    assert (
        await owner.call("GET", f"/api/admin/tickets/{ticket.id}/messages/{foreign.id}/attachment")
    ).status_code == 404
    other_owner = await login(http, "other_owner")
    assert (await other_owner.call("GET", path)).status_code == 404
