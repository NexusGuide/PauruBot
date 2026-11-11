# Selling more: discounts, gifts, levels, the wheel and lotteries
<!-- screens: /discounts /broadcasts /lotteries -->

Tools for selling more: discount codes for purchases, gift codes for wallet credit, gifts of time and data for services, broadcasts and rewards for inviting friends.

## Discount codes

In [Discount codes](/discounts) tap **New code**:

- **Type**: percent (with an optional **Maximum discount**) or a fixed amount.
- **Limits**: **Total uses**, **Uses per customer**, **Minimum order**, **Starts** and **Ends** dates, **First purchase only** and **Only for these plans**. None selected means every plan.
- **Sticky**: also applies to later renewals of the same service.

Customers enter the code when buying in the bot. Codes accept only Latin letters, digits, - and _.

## Gift codes (wallet credit)

On the same page, under **Gift codes (wallet credit)**, use **New gift code** to make a code that adds an amount to the wallet. A code has at least 4 characters and customers enter it in the wallet. Each customer can use each code only once, and **Only for people who have not bought yet** keeps it for new customers. **Redemptions** shows how often it was used.

## Gifts of time and data

To make up for an outage or for a promotion, filter services in [Services](/services) (for example every service on one panel) and use **Gift time and data** to give them all extra days or GB. Nothing is taken from wallets.

## Broadcasts

1. In [Broadcasts](/broadcasts) tap **New broadcast**.
2. Write the text; `<b>`, `<i>` and links are allowed. Use **Add button** for link buttons under the message.
3. Choose the audience: language, **Only people who bought** or **Only resellers**. The approximate number of recipients is shown before sending.
4. Tap **Send to everyone**.

Messages go out as fast as Telegram allows, so large audiences take a few minutes. Once started, a broadcast can only be stopped, so read the text carefully before sending. People who blocked the bot are marked **Blocked the bot** on their customer page.

## Loyalty levels

In [Settings](/settings), **Referrals** group, turn on **Loyalty levels** and under **Levels** give each level a name, the total spend to reach it and a discount; for example "Silver" from 1 million Toman with 5%. Every completed order adds to the customer's total and they get a message when they reach a new level. The level discount applies to every purchase automatically, and the wallet shows the current level and how far the next one is. Resellers keep their own reseller discount.

## Lucky wheel

With **Lucky wheel** on, a **Lucky wheel** button appears in the bot. Under **Wheel prizes**, give each slice a label, a prize (wallet credit, discount code or nothing), an amount and a weight; each slice's chance is shown next to it. **Hours between spins** sets how often a customer can spin, and **Wheel only for buyers** keeps it closed to people who haven't bought yet. A discount prize is a one-time code valid for 7 days.

## Lotteries

In [Lotteries](/lotteries) tap **New lottery** and choose how people score:

- Purchase points: every so many toman paid between the start and the draw time is one point (for example 10,000 makes a 100,000 purchase worth 10 points).
- Number of purchases: every completed purchase is one point.
- **Join with a button**: customers sign up from the **Lottery** button in the bot. Turn on **Lottery button** in the settings to show it.

Then choose how winners are picked (highest score wins, or drawn at random weighted by score), what customers see in the bot (nothing, their own score and place, or also the masked top five) and the prizes by place: first place gets the first prize, second place the second, and so on. A prize is a new service for the winner (GB and days on a panel you choose, sent to them like any purchase), wallet credit, or a text prize you hand over yourself. **Standings** lists everyone with a score, best first, with their name, username, ID and phone.

At the draw time the winners get their prizes and a message saying what they won; you and the report group get every winner's details with a button to their card. If a prize service cannot be made (for example its panel was switched off), it is flagged for you to hand over. **Draw now** draws early and **Cancel** closes it with no winners. In the bot, the lottery card has the same standings, and prizes are added one by one after creating it.

## Trust channel

[Channels](/channels) has a **Trust channels** section: add as many public channels as you like and tick the report kinds each one receives (purchase, renewal, free trial, upgrade, extra volume, extra days and moving a service). Every such order is posted there as a masked report: only the first digits of the buyer's id are shown, with the plan, category, service and amount paid, plus a buy button. A free trial shows «Free» in place of the amount. Full usernames and ids are never shown. If Telegram refuses a check or a post, a red badge appears on that channel row until it works again. The bot's own growth screen still sets a single channel; the full list with report kinds lives in the panel.

## Referrals

In [Settings](/settings), **Referrals** group, with **Referrals on** every customer has a personal invite link. When someone comes through it and makes their first purchase, **Reward percent** of that purchase is added to the inviter's wallet. Each customer's referrals are listed on their page.

> Resellers earn through sub-resellers rather than invite links; see [the resellers guide](help:resellers).

## Referral limits and the welcome gift

"Smallest first order that earns a referral reward" ignores small first purchases (a later, larger first purchase still earns it). "Referral rewards per inviter per day" caps how many rewards one inviter can earn a day, which stops farming with fake accounts. Banned inviters earn nothing.

"Welcome gift for new customers" adds that much to a new customer's wallet once, on their first /start; with "Welcome gift only for invited customers" on, only those who came through an invite link get it. Keep it small: anyone can make a new Telegram account.

## Referral on every purchase and private codes

With "Referral reward on every purchase", the inviter earns on each of the invited customer's purchases, not only the first; "Referral percent on later purchases" can set a lower share for them. "Referral rewards per inviter per 30 days" caps it.

A discount code with "Only for this customer" set to a Telegram ID works for that customer alone. The customer is told about it and finds it under Wallet → My discounts in the bot, with a copy button.

## Invitation only, and unpinning

With "Invitation only" in growth settings, a newcomer gets in only through a customer's invite link or by sending their invite code (their Telegram id). On [Broadcasts](/broadcasts), the unpin button takes down every broadcast pinned in customers' chats.
