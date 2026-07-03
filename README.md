# Skhron

A self-hosted Telegram bot for archiving short videos, photos, and memes within a private circle of friends. Media files are stored in Telegram's cloud via `file_id` references; the server keeps only a small SQLite database with metadata. No media traffic or disk space is required on the host.

## Features

- **Categories** — media is organized into named categories, one per friend group or topic
- **Per-user access control** — the admin grants each user view and/or upload rights per category
- **Invite links** — deep-link invites (`t.me/<bot>?start=inv_<code>`) grant preconfigured permissions on first use; supports usage limits and view-only or view+upload modes
- **Random** — fetch a random item from all accessible categories or from a specific one
- **Feed** — browse a category item by item, sorted by upload date, with inline navigation
- **Favorites** — per-user starred collection
- **Upload** — send media directly to the bot (albums supported); the bot asks which category to store it in. Supported types: photo, video, animation (GIF), video note, voice, audio
- **Deduplication** — exact duplicates are rejected per category by `file_unique_id`; near-duplicates (recompressed or re-uploaded copies) are detected via a perceptual hash (dHash) of the image or video thumbnail, and the bot shows the existing item and asks whether to save anyway
- **Inline mode** — type `@<bot>` in any chat to search and post items from accessible categories (enable via BotFather `/setinline`)
- **Group mode** — add the bot to a group chat and grant categories to the group as a whole; members use `/random` and `/categories` there. Group permissions are independent of personal ones
- **Admin panel** — in-bot management of categories, users, permissions, groups, invites, and statistics
- **Archive channel** — optional private channel that receives a copy of every upload; doubles as an automatic fallback source if a `file_id` send ever fails
- **Database backup** — one-click export of the SQLite file to the admin's chat

## Storage model

When a user uploads a file, Telegram stores it on its own servers and the bot records the `file_id`. Deleting the originating chat or the uploader's account does not invalidate stored `file_id`s. The only scenario that invalidates them is deleting the bot itself in BotFather, which is what the archive channel mitigates: every upload is copied there as a regular channel message, and the database keeps the reference (`archive_chat_id`, `archive_message_id`). If a direct send by `file_id` fails, the bot transparently serves the archived copy.

Media deletion is soft (a flag in the database). Deleting a category removes its metadata rows via FK cascades; files remain in Telegram and in the archive channel.

## Requirements

- Python 3.11+ (or Docker)
- A bot token from [@BotFather](https://t.me/BotFather)

Dependencies: [aiogram 3](https://docs.aiogram.dev/), SQLAlchemy 2 (async), aiosqlite, pydantic-settings.

## Deployment

```bash
git clone git@github.com:aga134/shron.git && cd shron
cp .env.example .env   # set BOT_TOKEN and ADMIN_IDS
```

Docker:

```bash
docker compose up -d --build
```

Bare Python:

```bash
pip install -r requirements.txt
python main.py
```

The bot runs on long polling; no inbound ports or webhooks are required. The SQLite database is created at `DATABASE_PATH` (default `data/skhron.db`, mounted as a volume in `docker-compose.yml`).

## Configuration

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | Bot token from BotFather |
| `ADMIN_IDS` | yes | Comma-separated Telegram user IDs with admin rights |
| `ARCHIVE_CHANNEL_ID` | no | ID of a private archive channel (`-100...`); the bot must be an admin there |
| `DATABASE_PATH` | no | SQLite file path, default `data/skhron.db` |

Admins listed in `ADMIN_IDS` are permanent; additional admins can be promoted from the admin panel (stored in the database).

### Archive channel setup

1. Create a private channel and add the bot as an administrator with posting rights.
2. Obtain the channel ID (forward a post from it to a bot such as @userinfobot; the ID has the form `-100...`).
3. Set `ARCHIVE_CHANNEL_ID` in `.env` and restart.

### Group mode setup

Add the bot to a group. It registers the chat and appears in the admin panel under Groups, where categories can be toggled per group. Group-to-supergroup migration is handled automatically, including forum topics.

## Project layout

```
main.py                  # entry point: config, DB, middlewares, routers, polling
skhron/
  config.py              # pydantic-settings, .env parsing
  db/models.py           # User, Category, Media, Permission, Favorite, Invite, Chat, GroupPermission
  db/repo.py             # all database operations
  services/access.py     # single point of permission checks (personal and group)
  services/archive.py    # archive-channel copying
  keyboards/callbacks.py # CallbackData factory registry
  keyboards/common.py    # shared keyboards
  middlewares/           # per-update DB session and user registration
  utils/media.py         # media extraction and file_id sending with archive fallback
  handlers/              # start, menu, random, feed, favorites, upload, inline, group
  handlers/admin/        # panel, categories, users, groups, invites, stats, backup
tests/                   # pytest suite (repo, access, config, groups)
```

Notable implementation details:

- `SimpleEventIsolation` serializes update processing per user, which makes FSM-based album collection race-free.
- Category IDs use `sqlite_autoincrement` to prevent rowid reuse from resurrecting stale invite grants.
- Invite redemption is idempotent and uses an atomic conditional `UPDATE` for the usage counter.
- All callback handlers guard against `InaccessibleMessage` (buttons older than 48 hours).
- Near-duplicate detection uses a pure-Pillow 64-bit dHash (Hamming distance threshold 8) computed from the photo itself or the Telegram-generated video thumbnail; audio and voice are matched by `file_unique_id` only. The admin command `/rehash` backfills hashes for photos uploaded before the feature was enabled.

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q
```

## License

MIT
