import asyncio
import logging
import os
import signal
from contextlib import AsyncExitStack, asynccontextmanager

from phonelet.config import settings
from phonelet.db import Database
from phonelet.ami_client import AmiClient
from phonelet.cdr_events import CdrHandler
from phonelet.notification import backend_id, delivery_loop
from phonelet.notifiers import registry
from phonelet.constants import CALL_ANSWERED, NotificationMethod
from phonelet.disk_space import RECORDING_SKIPPED_NOTE, DiskSpaceMonitor
from phonelet.notifiers.protocol import ModemStatusReceiver, Notifier
from phonelet.modem_status import ModemStatusMonitor
from phonelet.ingress import IngressReplay
from phonelet.recording import recording_loop
from phonelet.api import create_app
from phonelet.logging_privacy import configure_logging
from phonelet.sms_commands.commands import registry as command_registry
from phonelet.sms_commands.envelope import key_fingerprint, parse_key
from phonelet.sms_commands.worker import by_code, command_loop


logger = logging.getLogger(__name__)


def end_process():
    """Graceful shutdown as on `systemctl stop`; the unit's Restart=always starts the service again."""
    os.kill(os.getpid(), signal.SIGTERM)


def supervise(task):
    """Done callback of a background task: one that dies with an exception ends the process.

    The loops never return; AmiClient.start returns once connected. Cancellation at shutdown is
    not a failure.
    """
    if task.cancelled() or task.exception() is None:
        return
    logger.critical("Background task %s died; stopping for a restart", task.get_name(),
                    exc_info=task.exception())
    end_process()


async def stop_tasks(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class Runtime:
    """One application's resources, constructed on its running lifespan loop."""
    def __init__(self, config):
        self.config = config
        self.ami = AmiClient(host=config.ami_host, port=config.ami_port,
                             username=config.ami_username, secret=config.ami_secret,
                             reconnect_timeout=config.ami_reconnect_timeout)
        self.notifiers: list[Notifier] = []
        for channel in registry.CHANNELS:
            notifier = channel.from_settings(config, ami=self.ami)
            if notifier is not None:
                self.notifiers.append(notifier)
        self.db = Database(config.db_path, backends=[backend_id(n) for n in self.notifiers],
                           backend_methods={backend_id(n): n.accepted_methods for n in self.notifiers},
                           sms_commands_enabled=bool(config.sms_command_key))
        data_dirs = (config.recordings_dir, os.path.dirname(os.path.abspath(config.db_path)), config.sms_ingress_dir)
        self.disk = DiskSpaceMonitor(self.db, data_dirs, config.min_free_bytes)
        self.handler = CdrHandler(on_call_end=self.on_call_end, on_sms_failed=self.on_sms_failed)
        commands = [command.from_settings(config) for command in command_registry.COMMANDS]
        self.commands = by_code([command for command in commands if command is not None])
        logger.info("Available SMS commands: %s", ", ".join(c.name for c in self.commands.values()) or "none")
        # Settings validated the key; None turns commands off.
        self.command_key = parse_key(config.sms_command_key) if config.sms_command_key else None
        if self.command_key:
            logger.info("SMS commands on: key fingerprint %s", key_fingerprint(self.command_key))
        else:
            logger.info("SMS commands off: PHONELET_SMS_COMMAND_KEY not set")
        self.tasks = []

    async def on_call_end(self, **call):
        # The dialplan skips a recording below the disk reserve without a trace in the CDR: an answered
        # call without one while low space is announced is taken as skipped for that reason.
        note = None
        if (self.config.record_calls and call["status"] == CALL_ANSWERED and not call["recording_path"]
                and self.disk.low):
            note = RECORDING_SKIPPED_NOTE
        return await self.db.insert_call(**call, note=note)

    async def on_sms_failed(self, event_key, recipient, reason, history=False):
        # The key is unique per backend and the outbox is kept: a replayed CDR line queues nothing.
        return await self.db.enqueue_notification(NotificationMethod.SMS_FAILED,
                                                  {"recipient": recipient, "reason": reason},
                                                  event_key=event_key, history=history)

    async def alert_sink(self, method, payload, state):
        await self.db.enqueue_monitor_alert(method, payload, state)

    @asynccontextmanager
    async def lifespan(self, application):
        config = self.config
        # Commands may recover access to the host (e.g. an unfinished Wi-Fi change).
        # Run before the database opens so a broken database cannot prevent recovery.
        # The command worker's first tick can report it with the database available.
        for command in self.commands.values():
            await command.recover_at_start()
        async with AsyncExitStack() as cleanup:
            await self.db.init()
            cleanup.push_async_callback(self.db.close)
            # Registered before notifier starts so even partial startup stops AMI.
            cleanup.callback(self.ami.stop)
            for notifier in self.notifiers:
                cleanup.push_async_callback(notifier.close)
                await notifier.start()
            # Stop producers/workers before AMI stops and before DB closes.
            cleanup.push_async_callback(stop_tasks, self.tasks)
            application.state.db = self.db
            application.state.ami_client = self.ami
            monitor = ModemStatusMonitor(
                ami_client=self.ami,
                status_receivers=[n for n in self.notifiers if isinstance(n, ModemStatusReceiver)],
                poll_interval=config.modem_poll_interval,
                alert_sink=self.alert_sink, load_alert_state=self.db.monitor_alert_state,
            )
            ingress = IngressReplay(self.db, self.handler, config.sms_ingress_dir, config.cdr_log_path,
                                   config.sms_reassembly_timeout, config.ingress_poll_interval)
            application.state.modem_monitor = monitor
            application.state.ingress = ingress
            # One delivery loop per backend: a slow or unreachable channel delays only its own
            # jobs; claim_delivery keeps the order of alerts within each backend.
            delivery = [delivery_loop(self.db, [backend])
                        for backend in self.notifiers]
            for coroutine in (self.ami.start(), monitor.run(), ingress.run(), *delivery,
                              recording_loop(self.db, config.recordings_dir, disk=self.disk,
                                             retention_days=config.recording_retention_days),
                              command_loop(self.db, self.command_key, self.commands,
                                           config.ingress_poll_interval)):
                task = asyncio.create_task(coroutine, name=coroutine.__qualname__)
                task.add_done_callback(supervise)
                self.tasks.append(task)
            yield


def create_application(config=None):
    config = config or settings
    channel_secrets = [getattr(config, field) for channel in registry.CHANNELS
                       for field in channel.secret_settings]
    configure_logging(config.log_level, secrets=(config.ami_secret, config.api_key, config.sms_command_key,
                                                *channel_secrets))
    application = create_app()
    application.state.settings = config

    @asynccontextmanager
    async def lifespan(app):
        config.check_secrets()
        runtime = Runtime(config)
        app.state.runtime = runtime
        async with runtime.lifespan(app):
            yield

    application.router.lifespan_context = lifespan
    return application


# Importing the ASGI application creates no DB, HTTP client, Manager or tasks.
app = create_application()
