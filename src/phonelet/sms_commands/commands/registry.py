"""Add a command module and its class here; each class decides whether it is available."""
from phonelet.sms_commands.commands.base import Command
from phonelet.sms_commands.commands.wifi import WifiCommand


COMMANDS: list[type[Command]] = [WifiCommand]
