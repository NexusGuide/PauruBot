# Bot menu, texts and channels
<!-- screens: /bot-menu /texts /channels -->

Change how the bot looks without touching code: button layout, message texts, customer guide, app download links and channels.

## Main menu layout

[Bot menu](/bot-menu) shows a preview of the menu.

1. Drag buttons around, or tap one to change its place, colour or emoji.
2. To start a new row, drop a button on **Drop here for a new row**.
3. Buttons you don't want go to **Hidden buttons** with **Hide**. **Buy** and **Services** always stay.
4. Save. **Reset to default** brings back the original layout.

The **⚙️ Admin (staff only)** button appears only for staff.

### Colour and emoji

**Button colour** (blue, green or red) shows in recent Telegram apps; older ones show a plain button. A premium emoji works only when the bot's owner has Telegram Premium.

## Help topics

The topics you create with **Add topic** are listed under the **Guide** button in the bot. Each topic has a title and is either a text or a link, for example "Connecting on iPhone" pointing to a tutorial.

## App downloads

Under **App downloads**, use **Add app** to give each app its platform and GitHub repository so the bot always hands out the latest release. The **File name pattern** picks which file of the release to use, for example the one ending in `.apk`. For store apps, a **Store or website link** is enough.

If you set a **File storage channel** (a channel where the bot is an admin), files under 50 MB are uploaded there once and customers download them straight from Telegram, without needing GitHub.

## Bot texts

In [Bot texts](/texts) change any message the bot sends, separately for each language. Only the `<b>`, `<i>`, `<code>` and `<a>` tags are allowed. **Variables** under each text shows what is filled in automatically (such as the customer's name or an amount). **Restore default** brings back the original text.

## Channels

[Channels](/channels) sets three things:

- **Required channels**: people must join these channels to use the bot. The bot must be an admin there.
- **Report channels**: each kind of report (orders, payments, new users, errors, services, daily report, backups, resellers and panels) can go to its own group or topic. **Everything** is for when you want them all in one place.
- **Support contacts**: links customers see for reaching you.

> Enter a channel or group ID with its minus sign (for example -100...) and make the bot an admin there before saving.

You can also set a report group from inside it: add the bot to the group, then a staff member who may change settings sends `/setlog orders` (or any other topic) there, or inside one of the group's topics to use that topic. `/unsetlog orders` stops it and `/logs` shows what the group receives.

## Answering your own Telegram account

With Telegram Business (Telegram Premium), you can let this bot answer people who message your own account. Add the bot in Telegram under Settings → Telegram Business → Chatbots, allow it to reply, then turn on **Answer messages to my Telegram account** in the **Telegram Business** group of [Settings](/settings).

- **Keyword answers**: when a message contains one of a row's words, that answer goes out, for example prices or how to buy.
- **Standard reply**: sent at most once per person every few hours; write {bot} where the bot's link should go.
- After you write in a conversation yourself, the bot stays out of it for **Stay quiet after you write (minutes)**.

Only accounts of your staff are answered for; if someone else adds the bot to their account it does nothing. People who write to you are not added to your customers.
