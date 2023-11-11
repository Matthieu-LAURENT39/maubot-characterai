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
        return cls(
            create_time=datetime.fromisoformat(data["create_time"]),
            author_name=data["author"]["name"],
            # Key is absent for bots
            author_is_human=data["author"].get("is_human", False),
            # First candidate in the list is the latest
            # TODO: Actually check the create time, to be more sure
            content=data["candidates"][0]["raw_content"],
        )


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
