"""Interactively create a Telethon StringSession for the service account.

Run this only in a trusted terminal. The resulting string grants access to the
Telegram account and must be stored as TELEGRAM_USER_SESSION in a secret store.
"""

import getpass

from telethon.sync import TelegramClient
from telethon.sessions import StringSession


def main() -> None:
    api_id = int(input("TELEGRAM_API_ID: ").strip())
    api_hash = getpass.getpass("TELEGRAM_API_HASH: ").strip()
    phone = input("Service account phone (+...): ").strip()

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        client.start(
            phone=phone,
            code_callback=lambda: input("Telegram login code: ").strip(),
            password=lambda: getpass.getpass("Telegram 2FA password: "),
        )
        session_string = client.session.save()
    finally:
        client.disconnect()

    print("\nStore this value as TELEGRAM_USER_SESSION (do not commit it):")
    print(session_string)


if __name__ == "__main__":
    main()
