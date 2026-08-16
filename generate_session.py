import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

print("Telegram Personal Account Session Generator")
print("-------------------------------------------")
print("Get API_ID and API_HASH from https://my.telegram.org")

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

async def main():
    client = TelegramClient(StringSession(), api_id, api_hash)

    await client.start()

    session = client.session.save()

    print("\nSUCCESS")
    print("Copy this entire value into Railway as TELETHON_SESSION:\n")
    print(session)
    print("\nKeep this session private. Anyone with it may access your Telegram account.")

    await client.disconnect()

asyncio.run(main())
