from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from phonelet.config import Settings


class Command(ABC):
    """One kind of command SMS: subclasses set code, name and label and implement from_settings, parse and run.

    A new command: a module next to this one with an unused code, the codec of its
    arguments and a subclass; its class in commands/registry.py's COMMANDS list; a form and
    the same codec in tools/sms-command.html, a subcommand in src/phonelet/sms_commands/__main__.py,
    and its section in docs/sms-commands.md; docs/architecture.md describes the command flow.

    from_settings builds the command or returns None if unavailable on this host. Optional
    recover_at_start runs before the database opens, even with commands disabled; tick runs
    later with the database available and can report recovery or retry unfinished work.
    """
    code: int  # the plaintext byte after the stamp; unique among the worker's commands
    name: str  # ASCII identifier: the journal and the database
    label: str  # for people: notifications

    @classmethod
    @abstractmethod
    def from_settings(cls, config: Settings) -> Self | None:
        """Build from settings, or return None when this command is unavailable on the host."""

    async def recover_at_start(self):
        """Recover unfinished work before the database opens; needs no database."""

    @abstractmethod
    def parse(self, packed_args):
        """The arguments in `packed_args`; a ValueError (never quoting them) rejects the command as bad_format."""

    def describe(self, args) -> str:
        """What the journal may show of the arguments when the command is accepted."""
        return ""

    @abstractmethod
    async def run(self, args):
        """Carry the command out; return (result, message) for the database and the HEALTH notification.

        Blocking work goes to a thread. An exception ends the command as failed:error.
        """

    def interrupted_message(self):
        """The HEALTH message when a restart cut its run short; it is never repeated."""
        return f"{self.label}: command interrupted by a restart; not repeated"

    async def tick(self, db):
        """The command's own periodic work: runs before every pass, also while commands are off.

        An exception is logged and the tick runs again next pass.
        """
