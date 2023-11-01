from __future__ import annotations

from contextlib import asynccontextmanager
from asyncio import Lock
from typing import TYPE_CHECKING, Type
from uuid import uuid4

from characterai import PyAsyncCAI
from maubot import MessageEvent, Plugin
from maubot.handlers import command, event
from mautrix.types import (
    EventType,
    Format,
    MessageType,
    RelationType,
    TextMessageEventContent,
    UserID,
)
from mautrix.util import markdown
from mautrix.util.async_db import Connection, UpgradeTable
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

if TYPE_CHECKING:
    from mautrix.client import Client


@asynccontextmanager
async def client_typing(
    client: Client, event: MessageEvent, *, timeout: int = 60_000
) -> None:
    try:
        await client.set_typing(event.room_id, timeout=timeout)
        yield
    finally:
        await client.set_typing(event.room_id, timeout=0)


upgrade_table = UpgradeTable()


@upgrade_table.register(description="Initial revision")
async def upgrade_v1(conn: Connection) -> None:
    await conn.execute(
        """CREATE TABLE `rooms` (
	    `matrix_room_id` TEXT NOT NULL,
	    `cai_character_id` TEXT NOT NULL,
	    `cai_chat_id` TEXT NOT NULL,
	    PRIMARY KEY (`matrix_room_id`)
        )"""
    )


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("token")
        helper.copy("default_character_id")
        helper.copy("allowed_users")
        helper.copy("trigger")
        helper.copy("reply_is_trigger")
        helper.copy("reply_to_message")
        helper.copy("always_reply_in_dm")
        helper.copy("group_mode")
        helper.copy("group_mode_template")


class CAIBot(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()

        self._lock = Lock()

        # Setup the CAI api
        self.cai_client = PyAsyncCAI(self.config["token"])
        self.default_character_id: str = self.config["default_character_id"]
        user_info = await self.cai_client.user.info()
        self.user_id = str(user_info["user"]["user"]["id"])
        self.trigger: str = self.config["trigger"].strip().casefold()
        self.allowed_users = set(self.config["allowed_users"])
        self.reply_is_trigger: bool = self.config["reply_is_trigger"]
        self.reply_to_message: bool = self.config["reply_to_message"]
        self.always_reply_in_dm: bool = self.config["always_reply_in_dm"]
        self.group_mode: bool | None = self.config["group_mode"]
        self.group_mode_template: str = self.config["group_mode_template"]

    async def _insert_room_chat(
        self, *, room_id: str, character_id: str, chat_id: str
    ) -> None:
        async with self.database.acquire() as conn:
            await conn.execute(
                "REPLACE INTO rooms (matrix_room_id, cai_character_id, cai_chat_id) VALUES (?, ?, ?)",
                room_id,
                character_id,
                chat_id,
            )

    async def _get_chat_by_room(self, room_id: str) -> tuple[str, str] | None:
        """Gets the character_id and chat_id for a room, if it exists in the db."""
        async with self.database.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT cai_chat_id, cai_character_id FROM rooms WHERE matrix_room_id = ?",
                room_id,
            )
            return (
                (result["cai_character_id"], result["cai_chat_id"])
                if result is not None
                else None
            )

    async def _handle_group_mode(self, event: MessageEvent, text: str) -> str:
        if self.group_mode == True or (
            self.group_mode is None and not await self._is_room_dm(event.room_id)
        ):
            text = self.group_mode_template.format(
                username=self.client.parse_user_id(event.sender)[0],
                text=text,
            )
        return text

    async def send_message_to_ai(
        self, text: str, *, character_id: str, chat_id: str
    ) -> str:
        """Sends a message to the AI, and returns the response."""

        async with self._lock:
            async with self.cai_client.connect() as chat2:
                data = await chat2.send_message(
                    character_id,
                    chat_id,
                    text,
                    {"author_id": self.user_id},
                )
            return data["turn"]["candidates"][0]["raw_content"]

    async def create_ai_chat(self, character_id: str) -> tuple[str, str]:
        """Returns the chat_id and the first message from the AI."""
        print("Creating new chat", {"c": character_id, "u": self.user_id})
        async with self._lock:
            async with self.cai_client.connect() as chat2:
                char_chat = await chat2.new_chat(
                    character_id,
                    str(uuid4()),  # Why is this client side???
                    self.user_id,
                )
            return (
                char_chat[0]["chat"]["chat_id"],
                char_chat[1]["turn"]["candidates"][0]["raw_content"],
            )

    async def _is_room_dm(self, room_id: str) -> bool:
        """
        Returns True if the room is a DM, else False.
        A room is considered a DM it has 2 members
        """
        return len(await self.client.get_joined_members(room_id)) == 2

    def is_user_allowed(self, user_id: UserID) -> bool:
        """True if the user is allowed to use the bot, else False."""

        # If the whitelist is empty, allow everyone
        if not self.allowed_users:
            return True

        return user_id in self.allowed_users

    async def is_bot_triggered(self, event: MessageEvent) -> bool:
        """True if we should respond to this message, else False."""

        if (
            event.sender == self.client.mxid  # Ignore our own messages
            or event.content.relates_to["rel_type"]
            == RelationType.REPLACE  # Ignore message edits
            or event.content["msgtype"]
            != MessageType.TEXT  # Ignore non-text messages (like images)
            or not self.is_user_allowed(event.sender)  # Ignore non-whitelisted users
            or event.content.body.startswith(
                "!"
            )  # Ignore command (prefix is always ! it seems)
        ):
            return False

        if self.always_reply_in_dm and await self._is_room_dm(event.room_id):
            return True

        if (self.trigger == "{name}") and (
            self.client.parse_user_id(self.client.mxid)[0]
            in event.content.body.casefold()
        ):
            return True

        # Always returns True if the trigger is empty
        if self.trigger in event.content.body.casefold():
            return True

        reply_to = event.content.get_reply_to()
        if reply_to:
            reply_to = await self.client.get_event(event.room_id, reply_to)
        if self.reply_is_trigger and reply_to and reply_to.sender == self.client.mxid:
            return True

        return False

    async def _reply(self, *, event: MessageEvent, body: str):
        content = TextMessageEventContent(
            format=Format.HTML,
            body=body,
            formatted_body=markdown.render(body),
            msgtype=MessageType.NOTICE,  # Looks distinct from normal messages
        )
        return await event.respond(content, reply=self.reply_to_message)

    # Base command so we can create subcommands
    @command.new(name="cai", require_subcommand=True)
    async def cai(self, event: MessageEvent) -> None:
        pass

    @cai.subcommand(name="new_chat")
    @command.argument("character_id", required=False)
    async def new_chat(self, event: MessageEvent, character_id: str) -> None:
        if not self.is_user_allowed(event.sender):
            return

        # For some reason, missing arguments are empty strings instead of None
        if not character_id:
            if self.default_character_id:
                character_id = self.default_character_id
            else:
                await event.respond(
                    "No character id was provided and no default character is set."
                )
                return

        async with client_typing(self.client, event):
            chat_id, ai_reply = await self.create_ai_chat(character_id)
            await self._insert_room_chat(
                room_id=event.room_id, character_id=character_id, chat_id=chat_id
            )
        await self._reply(event=event, body=ai_reply)

    @event.on(EventType.ROOM_MESSAGE)
    async def on_message(self, event: MessageEvent) -> None:
        # Mark message as read, so the user can see the bot is alive
        await event.mark_read()

        if not await self.is_bot_triggered(event):
            return

        try:
            # I really with you could use a context manager for this
            async with client_typing(self.client, event):
                character_id, chat_id = await self._get_chat_by_room(event.room_id)
                if chat_id is None:
                    await event.respond(
                        "This room doesn't have an AI chat yet. Create one with `!cai new_chat`"
                    )
                    return

                ai_reply = await self.send_message_to_ai(
                    await self._handle_group_mode(event, str(event.content.body)),
                    character_id=character_id,
                    chat_id=chat_id,
                )

            # Send the response back to the chat room
            await self._reply(event=event, body=ai_reply)

        except Exception as e:
            self.log.exception(f"Error while handing message: {e}")
            await event.respond(f"Error while handing message... {e}")

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls) -> UpgradeTable | None:
        return upgrade_table
