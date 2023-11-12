import json
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO, StringIO

from . import utils


@dataclass
class CAIMessage:
    create_time: datetime
    author_name: str
    """The author's name on Character.AI"""
    author_is_human: bool
    content: str

    @classmethod
    def from_dict(cls, data: dict):
        # First candidate in the list is the latest
        # TODO: Use primary_candidate_id to determine the correct candidate
        #! In some rare cases, character.ai can stop generation so
        #! early that the message won't even have a raw_content.
        #! In that case, we set the content to an empty string.
        content = data["candidates"][0].get("raw_content", "")

        return cls(
            create_time=datetime.fromisoformat(data["create_time"]),
            author_name=data["author"]["name"],
            # Key is absent for bots
            author_is_human=data["author"].get("is_human", False),
            content=content,
        )

    def export_to_dict(self) -> dict:
        return {
            "create_time": utils.pretty_utc_str(self.create_time),
            "author_name": self.author_name,
            "author_is_human": self.author_is_human,
            "content": self.content,
        }


@dataclass
class ExportFile:
    file_extension: str
    mimetype: str
    data: bytes


def history_to_txt(
    history: list[CAIMessage], *, character_name: str, character_id: str, chat_id: str
) -> ExportFile:
    """
    Converts a list of CAIMessages to a txt file-like object.
    The messages should already be in chronological order.
    """
    f = StringIO()

    start_time_str = utils.pretty_utc_str(history[0].create_time)
    end_time_str = utils.pretty_utc_str(history[-1].create_time)

    # Write the header
    f.write(f"Character: {character_name} ({character_id})\n")
    f.write(f"Chat ID: {chat_id}\n")
    f.write(f"Messages: {len(history)}\n")
    f.write(f"{start_time_str} - {end_time_str}\n")
    f.write(f"{'='*60}\n\n")

    # Write the messages
    for msg in history:
        author = "You" if msg.author_is_human else f"{msg.author_name} [bot]"
        f.write(f"{author} - {utils.pretty_utc_str(msg.create_time)}\n")
        f.write(f"{msg.content}\n\n")
    f.seek(0)

    return ExportFile(
        file_extension="txt", mimetype="text/plain", data=f.read().encode()
    )


def history_to_json(
    history: list[CAIMessage], *, character_name: str, character_id: str, chat_id: str
) -> ExportFile:
    """
    Converts a list of CAIMessages to a json file-like object.
    """

    data = {}
    data["character_name"] = character_name
    data["character_id"] = character_id
    data["chat_id"] = chat_id
    data["start_time"] = utils.pretty_utc_str(history[0].create_time)
    data["end_time"] = utils.pretty_utc_str(history[-1].create_time)
    data["messages"] = [msg.export_to_dict() for msg in history]

    json_data = json.dumps(data, indent=4)

    return ExportFile(
        file_extension="json", mimetype="application/json", data=json_data.encode()
    )
