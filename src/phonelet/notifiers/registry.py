"""Add a channel module and its class here; the runtime uses the shared protocol."""
from phonelet.notifiers.homeassistant import HomeAssistantNotifier
from phonelet.notifiers.protocol import Notifier
from phonelet.notifiers.telegram import TelegramNotifier
from phonelet.notifiers.sip import SipDelivery


CHANNELS: list[type[Notifier]] = [TelegramNotifier, HomeAssistantNotifier, SipDelivery]
