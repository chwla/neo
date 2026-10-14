"""Fill a disposable Neo account-profile with chats for the interaction benchmark.

The sidebar lists only chats that have messages, and the API only creates a
message by running a model -- which would make every benchmark run depend on a
local model being loaded, and make the transcript text different each time. So
this writes through the application's own store instead: the same database URL
the server resolves for the profile, the same schema initialisation, the same
``AppStore`` methods the routes use. Nothing here touches the profile registry;
the profile itself is created and deleted through the HTTP API by the driver.

Usage, from the repository root:

    .venv/bin/python frontend/bench/seed_profile.py <profile_id> --chats 40 --messages 12

Prints a JSON summary with the created chat ids, oldest first.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sqlalchemy.orm import Session  # noqa: E402

from app.db.session import build_engine, initialize_database  # noqa: E402
from app.repositories.app_store import AppStore  # noqa: E402
from app.services.profile_accounts import database_url_for, list_profiles  # noqa: E402

#: Every block type the renderer handles, so a transcript costs what a real one does.
REPLY_PARTS = [
    "## Summary\n\nHere is what changed and why it matters for the **next step**.",
    "- First point with `inline code`\n- Second point with a [link](https://example.com)\n- Third point",
    "1. Prepare the data\n2. Run the check\n3. Compare the output",
    "```python\ndef total(items):\n    return sum(item.price for item in items)\n```",
    "| Option | Cost | Notes |\n| --- | ---: | --- |\n| A | 3 | fastest |\n| B | 5 | safest |",
    "> A quoted line that carries a caveat worth keeping.",
    "Plain paragraph text that wraps across a couple of lines so the bubble has real height "
    "and the layout engine has something to measure.",
]


def reply(rng: random.Random, index: int) -> str:
    parts = rng.sample(REPLY_PARTS, k=3)
    return f"Reply {index}.\n\n" + "\n\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("profile_id")
    parser.add_argument("--chats", type=int, default=40)
    parser.add_argument("--messages", type=int, default=12, help="messages per chat")
    parser.add_argument("--long-chat-messages", type=int, default=120,
                        help="messages in the newest chat, for chat-open cost")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    # `database_url_for` creates the directory it names, so an id that is not a
    # registered profile is refused before it can leave a stray folder behind.
    if not any(profile["id"] == args.profile_id for profile in list_profiles()):
        print(f"No account-profile with id {args.profile_id}", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    url = database_url_for(args.profile_id)
    initialize_database(url)
    engine = build_engine(url)
    created: list[int] = []
    try:
        with Session(engine) as db:
            store = AppStore(db)
            for number in range(1, args.chats + 1):
                chat = store.create_chat(title=f"Bench chat {number:02d}")
                count = args.long_chat_messages if number == args.chats else args.messages
                for index in range(count):
                    if index % 2 == 0:
                        store.add_chat_message(
                            chat.id, "user", f"Question {index // 2 + 1} in chat {number}?"
                        )
                    else:
                        store.add_chat_message(chat.id, "assistant", reply(rng, index // 2 + 1))
                created.append(chat.id)
            db.commit()
    finally:
        engine.dispose()
    print(json.dumps({"profile_id": args.profile_id, "chat_ids": created}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
