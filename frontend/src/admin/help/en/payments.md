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

## Gateways and other methods

- **Zarinpal**: enter the merchant ID. Zarinpal sends the customer back to the bot's address, so the server's public address must be set correctly. Sandbox mode is for testing.
- **Telegram Stars**: customers pay with Stars; **Toman per Star** sets the rate.
- **NowPayments and Plisio**: crypto gateways; get the API keys from your own account on those sites.

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
