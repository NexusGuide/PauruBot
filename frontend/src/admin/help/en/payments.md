# Payments and orders
<!-- screens: /payments /orders -->

Customers either buy a plan directly or top up their wallet first. Every payment method is switched on in [Settings](/settings), **Payments** and **Crypto** groups, and every payment shows up in [Payments](/payments).

## Card transfer

1. Turn on **Card transfer** and, under **Cards**, use **Add card** to enter the card number (or IBAN) and the account holder's name. Each customer is shown one of these cards.
2. The customer transfers the amount and sends a photo of the receipt in the bot.
3. The receipt appears in [Payments](/payments) and, in Telegram, for staff allowed to review receipts. **Approve** credits the wallet or delivers the order; **Reject** asks for a reason the customer sees.

The bot spots duplicate receipts by comparing images and flags them. With **Block duplicate receipts** on, a duplicate is not accepted at all.

### Auto-approve

With **Auto-approve receipts**, receipts below **Auto-approve up to** are approved without review, as long as the customer has at least **Approved receipts needed first** approved receipts and no rejected ones. Receipts from customers with medium or high risk always get a human review.

### Safer card payments

- "Which card customers see": all cards, one at random, or one after another (rotate). With one card shown, the receipt in Telegram says which card it should have landed on.
- "Add a random tail to card amounts" adds up to 999 Toman so every receipt matches one payment; the extra goes to the customer's wallet.
- "Customers register their card first": before paying card to card, the customer sends the card number and a photo of the card in the bot; staff approve or reject it there. A card already registered by someone else is flagged. "Card registration needed from" limits this to larger payments.
- On a customer's page, "Card to card" can be Trusted (receipts are approved at once) or Blocked (card to card isn't offered). Staff can change it from the bot's customer card too.
- "Approve unreviewed receipts after" approves receipts nobody looked at within that many minutes, up to the auto-approve amount. Duplicates and medium or high risk customers always wait for a person.
- Card numbers, amounts and crypto addresses come with one-tap copy buttons in the bot.

## Gateways and other methods

- **Zarinpal**: enter the merchant ID. Zarinpal sends the customer back to the bot's address, so the server's public address must be set correctly. Sandbox mode is for testing.
- **Telegram Stars**: customers pay with Stars; **Toman per Star** sets the rate.
- **NowPayments and Plisio**: crypto gateways; get the API keys from your own account on those sites.

## More gateways

The **More gateways** group of [Settings](/settings) has Aqayepardakht, Tetra98, Variza, AbanGateway, BluPal, Tronado, CubePay and AtlasPay. Turn one on and enter the keys from your account on that gateway; it then appears as a payment button in the bot and the Mini App.

- A payment is credited only after the bot checks it with the gateway itself, or, for Variza, Tronado and CubePay crypto, after a callback signed with your secret key. What the customer's browser sends back is never enough.
- If a different amount arrives than was asked, the payment waits in [Payments](/payments) instead of being credited. Opening it shows what arrived; **Approve** credits that amount, **Reject** closes it.
- Money that the gateway confirms after the payment expired is still credited, with the note "paid after expiry".
- **Variza** needs its webhook set to your server's public address followed by /api/pay/gw/variza. **BluPal** works without one, but setting /api/pay/gw/blupal in its dashboard credits faster. **AtlasPay** has no callback; open orders are checked every minute.
- **CubePay fee added to the price (%)** and AtlasPay's own markup are shown to the customer as the amount to pay; the wallet always gets the original amount.

## Direct crypto

With **Direct crypto payments**, customers send straight to your own wallet and the bot finds and confirms the transfer on the network.

1. Choose the networks (USDT and TRX on TRON, TON).
2. Enter the **TRON wallet address (TRC20)** and/or the **TON wallet address**.
3. Choose the **Exchange rate source**. The Nobitex rate refreshes every five minutes, and your manual rate is used when it's unavailable. **Markup on the rate (%)** covers price swings and fees.

The customer must send exactly the amount the bot shows; that amount is how the bot recognises the payment.

## Top-up bonus

Under **Top-up bonus**, use **Add rule** to set from which amount, and for which payment method, what percentage is added to the wallet as a gift (with an optional cap). When several rules match, the largest bonus applies.

## Orders and refunds

[Orders](/orders) lists every purchase, renewal and extra data or time purchase. **Refund to wallet** returns the order amount to the customer's wallet but does not change the service; if needed, disable or delete the service separately in [Services](/services).

> **Minimum top-up**, **Maximum top-up** and the order payment window are in the same **Payments** group of the settings.

## Sending balance between customers

With "Customers can send balance to each other" on, the wallet in the bot gets a "Send balance" button: the customer enters the other person's Telegram ID or @username (they must have started the bot), the amount, and confirms. The money moves at once, both sides are told, and it shows as "Transfer" in both histories. "Smallest transfer" and "Most a customer can send a day" set the limits.
