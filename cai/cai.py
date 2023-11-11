from __future__ import annotations

import zipfile
from asyncio import Lock
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from textwrap import indent
from typing import TYPE_CHECKING, Type
from urllib.parse import urljoin
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

from . import utils
from .caimessage import CAIMessage, ExportFile, history_to_json, history_to_txt

if TYPE_CHECKING:
    from mautrix.client import Client


BASE_AVATAR_URL = "https://characterai.io/i/400/static/avatars/"


@asynccontextmanager
async def client_typing(
    client: Client, event: MessageEvent, *, timeout: int = 60_000
) -> None:
    """Context manager to set typing status for the duration of the block."""
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
        helper.copy("strip_trigger_prefix")
        helper.copy("reply_is_trigger")
        helper.copy("always_reply_in_dm")
        helper.copy("reply_to_message")
        helper.copy("show_prompt_in_reply")
        helper.copy("use_char_name")
        helper.copy("use_char_avatar")
        helper.copy("group_mode")
        helper.copy("group_mode_template")
        helper.copy("export_txt")
        helper.copy("export_json")


class CAIBot(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()

        self._lock = Lock()

        # Setup the CAI api
        self.cai_client = PyAsyncCAI(self.config["token"])
        user_info = await self.cai_client.user.info()
        self.user_id = str(user_info["user"]["user"]["id"])

    async def _insert_room_chat(
        self, *, room_id: str, character_id: str, chat_id: str
    ) -> None:
        """Associates a room with a CAI chat in the database."""
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
        """Applies the group mode template if needed"""
        if self.config["group_mode"] == True or (
            self.config["group_mode"] is None
            and not await self._is_room_dm(event.room_id)
        ):
            text = self.config["group_mode_template"].format(
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

    async def get_chat_history(self, chat_id: str) -> list[CAIMessage]:
        """Returns all messages in a chat, from oldest to newest."""
        async with self._lock:
            async with self.cai_client.connect() as chat2:
                data = await chat2.get_history(chat_id)
            return sorted(
                [CAIMessage.from_dict(msg) for msg in data["turns"]],
                key=lambda m: m.create_time,
            )

    async def get_char_info(self, character_id: str) -> tuple[str, str]:
        """Returns a tuple with the character's name and avatar url."""

        # Get the character's info
        # We can't use character.info, as it doesn't work for private characters
        # info = await self.cai_client.character.info(character_id)

        # We use the chat info instead, but it requires a chat to exist already
        async with self._lock:
            async with self.cai_client.connect() as chat2:
                chats_info = await chat2.get_chat(character_id)
        info = chats_info["chats"][0]

        return (info["character_name"], info["character_avatar_uri"])

    async def set_display_to_char_info(
        self, room_id: str, character_id: str, *, copy_name: bool, copy_avatar: bool
    ) -> None:
        """
        Sets the bot's nickname and room pfp to the CAI character's
        Only call this AFTER a chat has been created with that character
        """
        # Avoid useless requests
        if not copy_name and not copy_avatar:
            return

        character_name, character_avatar_uri = await self.get_char_info(character_id)

        content = {"membership": "join"}
        if copy_name:
            content["displayname"] = character_name
        if copy_avatar:
            # download the avatar
            avatar_url = urljoin(BASE_AVATAR_URL, character_avatar_uri)
            async with self.http.get(avatar_url) as resp:
                resp.raise_for_status()
                avatar_content = await resp.read()
                avarat_mimetype = resp.content_type
            avatar_mxc = await self.client.upload_media(
                avatar_content, mime_type=avarat_mimetype
            )
            content["avatar_url"] = avatar_mxc

        await self.client.send_state_event(
            room_id=room_id,
            event_type="m.room.member",
            content=content,
            state_key=self.client.mxid,
        )

    async def _is_room_dm(self, room_id: str) -> bool:
        """
        Returns True if the room is a DM, else False.
        A room is considered a DM it has 2 members
        """
        return len(await self.client.get_joined_members(room_id)) == 2

    def is_user_allowed(self, user_id: UserID) -> bool:
        """True if the user is allowed to use the bot, else False."""

        allowed_users = set(self.config["allowed_users"])

        # If the whitelist is empty, allow everyone
        if not allowed_users:
            return True

        return user_id in allowed_users

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

        if self.config["always_reply_in_dm"] and await self._is_room_dm(event.room_id):
            return True

        # Always returns True if the trigger is empty
        if self.trigger in event.content.body.casefold():
            return True

        reply_to = event.content.get_reply_to()
        if reply_to:
            reply_to = await self.client.get_event(event.room_id, reply_to)
        if (
            self.config["reply_is_trigger"]
            and reply_to
            and reply_to.sender == self.client.mxid
        ):
            return True

        return False

    async def _reply(self, *, event: MessageEvent, body: str):
        """Helper function to reply to a MessageEvent"""
        content = TextMessageEventContent(
            format=Format.HTML,
            body=body,
            formatted_body=markdown.render(body),
            msgtype=MessageType.NOTICE,  # Looks distinct from normal messages
        )
        return await event.respond(content, reply=self.config["reply_to_message"])

    async def _handle_exports(self, room_id: str):
        character_id, chat_id = await self._get_chat_by_room(room_id)
        history = await self.get_chat_history(chat_id)
        character_name, _ = await self.get_char_info(character_id)
        safe_character_name = "".join(
            c for c in character_name.replace(" ", "_") if c.isalnum() or c == "_"
        )
        export_time_str = utils.pretty_utc_str(datetime.now(tz=timezone.utc))

        files: list[ExportFile] = []
        if self.config["export_txt"]:
            text_file = history_to_txt(
                history,
                character_name=character_name,
                character_id=character_id,
                chat_id=chat_id,
            )
            files.append(text_file)

        if self.config["export_json"]:
            json_file = history_to_json(
                history,
                character_name=character_name,
                character_id=character_id,
                chat_id=chat_id,
            )
            files.append(json_file)

        # No output format were enabled, just do nothing
        if not files:
            return

        # More than one file, zip them together
        if 1 < len(files):
            zip_file = BytesIO()
            with zipfile.ZipFile(zip_file, mode="w") as zf:
                for file in files:
                    zf.writestr(
                        f"cai-{safe_character_name}-{export_time_str}.{file.file_extension}",
                        file.data,
                    )
            zip_file.seek(0)
            file = ExportFile(".zip", "application/zip", zip_file.read())
        # Only one file, just send it directly
        else:
            file = files[0]

        # Upload and send the file
        file_url = await self.client.upload_media(
            data=file.data,
            mime_type=file.mimetype,
        )
        await self.client.send_file(
            room_id,
            file_url,
            file_name=f"cai-{safe_character_name}-{export_time_str}.{file.file_extension}",
        )

    # Base command so we can create subcommands
    @command.new(name="cai", require_subcommand=True)
    async def cai(self, event: MessageEvent) -> None:
        pass

    @cai.subcommand(name="new", aliases=["new_chat"])
    @command.argument("character_id", required=False)
    async def new_chat(self, event: MessageEvent, character_id: str) -> None:
        if not self.is_user_allowed(event.sender):
            return

        # For some reason, missing arguments are empty strings instead of None
        if not character_id:
            if self.config["default_character_id"]:
                character_id = self.config["default_character_id"]
            else:
                await event.respond(
                    "No character id was provided and no default character is set."
                )
                return

        async with client_typing(self.client, event):
            # If a chat already exists
            if await self._get_chat_by_room(event.room_id) is not None:
                await self._handle_exports(room_id=event.room_id)

            chat_id, ai_reply = await self.create_ai_chat(character_id)
            await self._insert_room_chat(
                room_id=event.room_id, character_id=character_id, chat_id=chat_id
            )

            await self.set_display_to_char_info(
                room_id=event.room_id,
                character_id=character_id,
                copy_name=self.config["use_char_name"],
                copy_avatar=self.config["use_char_avatar"],
            )

        await self._reply(event=event, body=ai_reply)

    @cai.subcommand(name="sync_info")
    async def sync_info(self, event: MessageEvent) -> None:
        if not self.is_user_allowed(event.sender):
            return

        if not self.config["use_char_name"] and not self.config["use_char_avatar"]:
            await self._reply(
                event=event,
                body="Both `use_char_name` and `use_char_avatar` are disabled, nothing to do.",
            )
            return

        # TODO: this is duplicated code, should be factored out
        query = await self._get_chat_by_room(event.room_id)
        if query is None:
            await event.respond(
                "This room doesn't have an AI chat yet. Create one with `!cai new_chat`"
            )
            return
        character_id, _ = query

        await self.set_display_to_char_info(
            room_id=event.room_id,
            character_id=character_id,
            copy_name=self.config["use_char_name"],
            copy_avatar=self.config["use_char_avatar"],
        )

        await event.react("✅")

    @event.on(EventType.ROOM_MESSAGE)
    async def on_message(self, event: MessageEvent) -> None:
        # Mark message as read, so the user can see the bot is alive
        await event.mark_read()

        if not await self.is_bot_triggered(event):
            return

        try:
            # I really with you could use a context manager for this
            async with client_typing(self.client, event):
                query = await self._get_chat_by_room(event.room_id)
                if query is None:
                    await event.respond(
                        "This room doesn't have an AI chat yet. Create one with `!cai new_chat`"
                    )
                    return
                character_id, chat_id = query

                text = str(event.content.body)
                if self.config["strip_trigger_prefix"]:
                    text = text.lstrip()
                    if text.casefold().startswith(self.trigger):
                        text = text[len(self.trigger) :]
                text = await self._handle_group_mode(event, text)

                ai_reply = await self.send_message_to_ai(
                    text,
                    character_id=character_id,
                    chat_id=chat_id,
                )

                if (self.config["show_prompt_in_reply"] == True) or (
                    self.config["show_prompt_in_reply"] is None
                    and not await self._is_room_dm(event.room_id)
                ):
                    prompt = indent(text, "> ", predicate=lambda _: True)
                    ai_reply = f"{prompt}\n\n{ai_reply}"

            # Send the response back to the chat room
            await self._reply(event=event, body=ai_reply)

        except Exception as e:
            self.log.exception(f"Error while handing message: {e}")
            await event.respond(f"Error while handing message... {e}")

    @property
    def trigger(self) -> str:
        """The casefolded trigger for the bot to respond to. Handles the {name} placeholder."""
        t = self.config["trigger"]

        # Ok so, for some reason, maubot calls EVERY properties at
        # plugin registration time, which is before the config is loaded.
        # So t will be None, and we need to handle that.
        if t is None:
            return ""

        if t == "{name}":
            t = self.client.parse_user_id(self.client.mxid)[0]

        return t.strip().casefold()

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls) -> UpgradeTable | None:
        return upgrade_table
