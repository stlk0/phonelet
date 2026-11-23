"""Low disk space alerts for the data filesystems of the app (recordings, database, SMS journal)."""
import logging
import os
import shutil

logger = logging.getLogger(__name__)

MIB = 1024 * 1024
# Restored only above the minimum plus this share of it, so a disk near the limit does not flap.
RESTORE_MARGIN = 0.10
RECORDING_SKIPPED_NOTE = "not recorded: low disk space"


class DiskSpaceMonitor:
    """Announce low and restored free space once each through the durable HEALTH alert path.

    ``db`` provides ``disk_low()`` and ``enqueue_disk_alert(message, low)``: the announced state
    is stored with the alert, so a restart neither repeats "low" nor loses "restored".
    """

    def __init__(self, db, directories, min_free_bytes, *, disk_usage=shutil.disk_usage):
        self._db = db
        self._directories = tuple(str(d) for d in directories)
        self._min_free = min_free_bytes
        self._disk_usage = disk_usage
        self.low = None  # the last announced state; None until loaded

    def _free_space(self):
        """(free bytes, directory) per filesystem, one directory per st_dev; missing ones are skipped."""
        seen, result = set(), []
        for directory in self._directories:
            try:
                device = os.stat(directory).st_dev
                if device in seen:
                    continue
                free = self._disk_usage(directory).free
            except OSError:
                continue
            seen.add(device)
            result.append((free, directory))
        return result

    async def check(self):
        if self.low is None:
            self.low = await self._db.disk_low()
        space = self._free_space()
        if not space:
            return
        free, directory = min(space)
        if not self.low and free < self._min_free:
            message = (f"Low disk space: {free // MIB} MiB free on {directory}, "
                       f"minimum {self._min_free // MIB} MiB")
            low = True
        elif self.low and free >= self._min_free * (1 + RESTORE_MARGIN):
            message = f"Disk space restored: {free // MIB} MiB free on {directory}"
            low = False
        else:
            return
        await self._db.enqueue_disk_alert(message, low)
        self.low = low
        logger.warning("Disk space %s: %s", "low" if low else "restored", directory)
