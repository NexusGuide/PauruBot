"""Pure safety rules for reseller customer transfers."""

from paurubot.core.customer_transfer import allowed_target
from paurubot.db.models import Shop, ShopStatus


def shop(shop_id: int, *, parent_id: int | None = None, reseller_profile_id: int | None = None) -> Shop:
    return Shop(
        id=shop_id,
        parent_id=parent_id,
        reseller_profile_id=reseller_profile_id,
        title=str(shop_id),
        status=ShopStatus.ACTIVE,
    )


def test_main_shop_can_send_customer_only_to_reseller_shop() -> None:
    main = shop(1)
    reseller = shop(2, parent_id=1, reseller_profile_id=8)
    other = shop(3, parent_id=1)
    assert allowed_target(main, reseller)
    assert not allowed_target(main, other)


def test_reseller_shop_can_return_customer_to_parent_only() -> None:
    main = shop(1)
    reseller = shop(2, parent_id=1, reseller_profile_id=8)
    sibling = shop(3, parent_id=1, reseller_profile_id=9)
    assert allowed_target(reseller, main)
    assert not allowed_target(reseller, sibling)


def test_inactive_destination_is_rejected() -> None:
    main = shop(1)
    reseller = shop(2, parent_id=1, reseller_profile_id=8)
    reseller.status = ShopStatus.SUSPENDED
    assert not allowed_target(main, reseller)
