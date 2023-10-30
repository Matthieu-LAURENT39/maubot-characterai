from typing import Type
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from maubot import Plugin, MessageEvent
from maubot.handlers import event
from characterai import PyAsyncCAI
from mautrix.types import (
    Format,
    TextMessageEventContent,
    EventType,
    UserID,
    MessageType,
    RelationType,
)
from mautrix.util import markdown


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("token")
        helper.copy("character_id")
        helper.copy("chat_id")
        helper.copy("allowed_users")
        helper.copy("trigger")
        helper.copy("reply_is_trigger")


class CAIBot(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()

        # Setup the CAI api
        self.cai_client = PyAsyncCAI(self.config["token"])
        self.character_id = self.config["character_id"]
        char_chat = await self.cai_client.chat2.get_chat(self.character_id)
        self.author = {"author_id": char_chat["chats"][0]["creator_id"]}
        self.chat_id = self.config["chat_id"] or char_chat["chats"][0]["chat_id"]
        self.trigger: str = self.config["trigger"].strip().casefold()
        self.allowed_users = set(self.config["allowed_users"])
        self.reply_is_trigger: bool = self.config["reply_is_trigger"]

    async def send_message_to_ai(self, text: str, /) -> str:
        """Sends a message to the AI, and returns the response."""

        async with self.cai_client.connect() as chat2:
            data = await chat2.send_message(
                self.character_id,
                self.chat_id,
                text,
                self.author,
            )
        return data["turn"]["candidates"][0]["raw_content"]

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
        ):
            return False

        if (self.trigger == "{name}") and (
            self.client.mxid in event.content.body.casefold()
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

    @event.on(EventType.ROOM_MESSAGE)
    async def on_message(self, event: MessageEvent) -> None:
        # Mark message as read, so the user can see the bot is alive
        await event.mark_read()

        if not await self.is_bot_triggered(event):
            return

        try:
            # I really with you could use a context manager for this
            await self.client.set_typing(event.room_id, timeout=60_000)

            ai_reply = await self.send_message_to_ai(str(event.content.body))

            # Send the response back to the chat room
            await self.client.set_typing(event.room_id, timeout=0)

            content = TextMessageEventContent(
                format=Format.HTML,
                body=ai_reply,
                formatted_body=markdown.render(ai_reply),
                msgtype=MessageType.NOTICE,  # Looks distinct from normal messages
            )
            await event.reply(content)

        except Exception as e:
            self.log.exception(f"Error while handing message: {e}")
            await event.respond(f"Error while handing message... {e}")

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config
