# Telegram Personal Account Publisher

This project uses:

- **Aiogram / Bot API** for the private control panel with buttons.
- **Telethon / MTProto** for publishing messages from your personal Telegram account.

## Important

Your personal account must already be a member of the target group/channel and must have permission to send messages there.

Do not commit your `API_HASH`, `BOT_TOKEN`, or `TELETHON_SESSION` to GitHub.

A Telethon StringSession is highly sensitive. Anyone who obtains it may be able to access your Telegram account.

Telegram may restrict accounts that automate aggressive, repetitive, spammy, or unsolicited posting. Use conservative posting intervals and comply with Telegram rules and the rules of the groups you post in.

## Step 1 - Create Telegram API credentials

Go to:

https://my.telegram.org

Sign in using the same personal Telegram account that will publish the posts.

Open **API development tools** and create an application.

Copy:

- `api_id`
- `api_hash`

## Step 2 - Generate TELETHON_SESSION locally

Do **not** generate the login session inside Railway.

On your own computer:

```bash
pip install telethon
python generate_session.py
```

Enter:

- `API_ID`
- `API_HASH`
- Your Telegram phone number
- The Telegram login code
- Your 2FA password if enabled

The script prints a long string.

Copy it and store it in Railway as:

```text
TELETHON_SESSION=...
```

Never share this string publicly.

## Step 3 - Railway variables

Add:

```text
BOT_TOKEN=...
ADMIN_USER_ID=...
API_ID=...
API_HASH=...
TELETHON_SESSION=...
TARGET_CHAT=@yourgroupusername
TIMEZONE=Africa/Tunis
DB_PATH=/data/bot.db
```

`TARGET_CHAT` may be a public group username such as:

```text
@mygroup
```

or another identifier Telethon can resolve.

## Step 4 - Railway volume

For scheduled posts to survive redeployments, add a Railway Volume mounted at:

```text
/data
```

## Step 5 - Deploy

Upload the project to GitHub.

Connect the GitHub repo to Railway and deploy.

The service starts with:

```text
python bot.py
```

## Main menu

- 📝 New Post
- 📅 Scheduled Posts
- ⚡ Quick Publish
- 🗑 Delete Post
- 👁 Preview Formatter
- ℹ️ Help

The control panel is handled by the bot, but the actual message is sent by the logged-in personal Telegram account.
