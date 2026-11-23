import ipaddress
import re

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from phonelet.constants import SIP_DOMAIN_PATTERN
from phonelet.sms_commands.envelope import parse_key


# Pinned by the driver patch (PHONELET_SMS_SPOOL) and asterisk/cdr_custom.conf.
SMS_INGRESS_DIR = "/var/spool/asterisk/phonelet-sms"
CDR_LOG_PATH = "/var/log/asterisk/cdr-custom/phonelet.csv"


def is_placeholder(value: str) -> bool:
    value = value.strip().lower()
    return not value or value == "changeme" or value.startswith(("generate_", "change_me", "replace_", "your_"))


class Settings(BaseSettings):
    # AMI
    ami_host: str = "127.0.0.1"
    ami_port: int = Field(default=5038, ge=1, le=65535)
    ami_username: str = "phonelet"
    ami_secret: str = "changeme"

    # Incoming SMS delivery to the SIP endpoint
    sip_domain: str = ""
    sip_message_destination: str = "pjsip:phone"

    # Database
    db_path: str = "/var/lib/phonelet/phonelet.db"

    # Recordings
    recordings_dir: str = "/var/lib/phonelet/recordings"
    # Recording files are deleted this many days after the call; 0 keeps them forever.
    recording_retention_days: int = Field(default=0, ge=0, le=36500)
    # Set by deployment from AST_RECORD_CALLS (not an .env key): only the call notification uses it.
    record_calls: bool = False

    # Durable ingress and disk reserve
    sms_ingress_dir: str = SMS_INGRESS_DIR
    cdr_log_path: str = CDR_LOG_PATH
    ingress_poll_interval: float = Field(default=5.0, gt=0)
    min_free_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    # Multipart partial-publication timeout (seconds)
    sms_reassembly_timeout: float = Field(default=600.0, gt=0)

    # Telegram
    telegram_token: str = ""
    telegram_chat_id: str = ""

    # Home Assistant
    ha_url: str = ""
    ha_token: str = ""

    # API
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_key: str = "changeme"

    # Modem monitoring
    modem_poll_interval: int = Field(default=30, gt=0)

    # AMI tuning
    ami_reconnect_timeout: int = Field(default=5, gt=0)

    # Commands by encrypted SMS (phonelet.sms_commands). An empty key turns them
    # off: new PC1 texts are ordinary SMS; queued commands are rejected as disabled.
    sms_command_key: str = ""
    # The Wi-Fi command (phonelet.sms_commands.commands.wifi, phonelet.netplan_wifi).
    wifi_netplan_file: str = "/etc/netplan/30-wifis-dhcp.yaml"
    wifi_interface: str = "wlan0"
    wifi_state_dir: str = "/var/lib/phonelet/wifi"
    wifi_connect_timeout: float = Field(default=120.0, gt=0)

    # Logging
    log_level: str = "INFO"

    model_config = {"env_prefix": "PHONELET_", "hide_input_in_errors": True, "allow_inf_nan": False}

    @model_validator(mode="after")
    def validate_settings(self):
        if bool(self.telegram_token) != bool(self.telegram_chat_id):
            raise ValueError("Telegram token and chat ID must be configured together")
        if bool(self.ha_url) != bool(self.ha_token):
            raise ValueError("Home Assistant URL and token must be configured together")
        if self.ha_url and not self.ha_url.startswith(("http://", "https://")):
            raise ValueError("Home Assistant URL must use http or https")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid log level")
        if self.sip_domain and not SIP_DOMAIN_PATTERN.fullmatch(self.sip_domain):
            raise ValueError("Invalid PHONELET_SIP_DOMAIN")
        if self.sip_message_destination and not re.fullmatch(r"pjsip:[A-Za-z0-9_:@.+/-]+", self.sip_message_destination):
            raise ValueError("Invalid SIP message destination")
        self.log_level = self.log_level.upper()
        try:
            ipaddress.ip_address(self.api_host)
        except ValueError:
            raise ValueError("Invalid PHONELET_API_HOST") from None
        if any(c in value for value in (self.ami_secret, self.api_key, self.ami_username) for c in "\r\n"):
            raise ValueError("Credentials must not contain newlines")
        if self.sms_command_key:
            try:
                parse_key(self.sms_command_key)
            except ValueError as exc:
                raise ValueError(f"PHONELET_SMS_COMMAND_KEY {exc}") from None
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.wifi_interface):
            raise ValueError("Invalid Wi-Fi interface")
        return self

    def check_secrets(self):
        """Raise ValueError for placeholder secrets.

        Run at app startup and in the deploy preflight.
        """
        problems = []
        if is_placeholder(self.ami_secret):
            problems.append("PHONELET_AMI_SECRET")
        if is_placeholder(self.api_key):
            problems.append("PHONELET_API_KEY")
        if problems:
            raise ValueError(
                f"Insecure default values for: {', '.join(problems)}. "
                "Set real values via environment variables or .env."
            )


settings = Settings()
