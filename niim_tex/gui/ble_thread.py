"""BLE communication thread — bridges asyncio (bleak) with the Qt event loop."""

import asyncio
import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from niim_tex.models import get_printer, MODELS


class BleThread(QThread):
    """Dedicated thread running an asyncio event loop for BLE operations.

    All printer communication goes through this thread.  Operations are
    serialised via an asyncio queue so only one BLE command runs at a time
    (prevents start_notify/stop_notify races).
    """

    # Signals
    connected = pyqtSignal(str)           # device name
    disconnected = pyqtSignal()
    error = pyqtSignal(str, str)          # operation_id, error message
    result = pyqtSignal(str, object)      # operation_id, result data
    status_updated = pyqtSignal(dict)     # heartbeat/battery/rfid data
    print_progress = pyqtSignal(int, int) # current row, total rows

    def __init__(self, parent=None):
        super().__init__(parent)
        self.loop = None
        self.printer = None
        self._queue = None
        self._heartbeat_task = None
        self._model_name = None

    # ── Thread lifecycle ─────────────────────────────────────────────

    def run(self):
        """Thread entry: create and run an asyncio event loop forever."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._queue = asyncio.Queue()
        self.loop.create_task(self._worker())
        self.loop.run_forever()

    def stop(self):
        """Cleanly stop the thread."""
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.wait()

    async def _worker(self):
        """Process operations from the queue one at a time."""
        while True:
            op_id, coro_func, args, kwargs = await self._queue.get()
            try:
                res = await coro_func(*args, **kwargs)
                self.result.emit(op_id, res)
            except Exception as e:
                self.error.emit(op_id, f"{e}")
                traceback.print_exc()
            finally:
                self._queue.task_done()

    # ── Submit work to the BLE thread ────────────────────────────────

    def submit(self, operation_id, coro_func, *args, **kwargs):
        """Schedule an async operation on the BLE thread (serialised)."""
        if not self.loop or not self.loop.is_running():
            self.error.emit(operation_id, "BLE thread not running")
            return
        self.loop.call_soon_threadsafe(
            self._queue.put_nowait,
            (operation_id, coro_func, args, kwargs),
        )

    # ── Connection management ────────────────────────────────────────

    def do_connect(self, model=None):
        self._model_name = model
        self.submit("connect", self._connect, model)

    async def _connect(self, model=None):
        if self.printer and hasattr(self.printer, 'is_connected') and self.printer.is_connected:
            await self.printer.disconnect()

        self.printer = get_printer(model)
        self.printer._notify_event = asyncio.Event()
        name = await self.printer.connect()
        self.connected.emit(name)
        self._start_heartbeat()
        return name

    def do_disconnect(self):
        self.submit("disconnect", self._disconnect)

    async def _disconnect(self):
        self._stop_heartbeat()
        if self.printer:
            try:
                await self.printer.disconnect()
            except Exception:
                pass
            self.printer = None
        self.disconnected.emit()

    @property
    def is_connected(self):
        return (self.printer is not None
                and hasattr(self.printer, 'is_connected')
                and self.printer.is_connected)

    # ── Printer operations ───────────────────────────────────────────

    def do_get_info(self):
        self.submit("info", self._get_info)

    async def _get_info(self):
        return await self.printer.get_info()

    def do_get_rfid(self):
        self.submit("rfid", self._get_rfid)

    async def _get_rfid(self):
        rfid = await self.printer.get_rfid()
        if rfid and rfid.get("uuid"):
            from niim_tex import lookup_rfid_barcode, correct_rfid_count
            info = lookup_rfid_barcode(rfid["barcode"]) if rfid.get("barcode") else None
            rfid["_lookup"] = info
            rfid["_remaining"] = correct_rfid_count(rfid["remaining_labels"])
            rfid["_total"] = correct_rfid_count(rfid["total_labels"])
        return rfid

    def do_heartbeat(self):
        self.submit("heartbeat", self._heartbeat)

    async def _heartbeat(self):
        hb = await self.printer.heartbeat()
        self.status_updated.emit(hb)
        return hb

    def do_print(self, image, density=3, quantity=1, label_type=1):
        self.submit("print", self._print, image, density, quantity, label_type)

    async def _print(self, image, density, quantity, label_type):
        self._stop_heartbeat()
        try:
            await self.printer.print_image(
                image, density=density, quantity=quantity, label_type=label_type)
        finally:
            self._start_heartbeat()
        return True

    def do_print_batch(self, images_and_settings):
        self.submit("print_batch", self._print_batch, images_and_settings)

    async def _print_batch(self, items):
        self._stop_heartbeat()
        try:
            for i, (image, density, quantity, label_type) in enumerate(items):
                self.print_progress.emit(i + 1, len(items))
                await self.printer.print_image(
                    image, density=density, quantity=quantity, label_type=label_type)
                if i < len(items) - 1:
                    await self.printer.wait_ready(delay=0.5)
        finally:
            self._start_heartbeat()
        return True

    # ── Settings ─────────────────────────────────────────────────────

    def do_set_density(self, density):
        self.submit("set_density", self._set_density, density)

    async def _set_density(self, density):
        return await self.printer.set_density(density)

    def do_set_sound(self, sound_type, enabled):
        self.submit("set_sound", self._set_sound, sound_type, enabled)

    async def _set_sound(self, sound_type, enabled):
        return await self.printer.set_sound(sound_type, enabled)

    def do_get_sound(self, sound_type):
        self.submit("get_sound", self._get_sound, sound_type)

    async def _get_sound(self, sound_type):
        return await self.printer.get_sound(sound_type)

    def do_set_shutdown(self, time_setting):
        self.submit("set_shutdown", self._set_shutdown, time_setting)

    async def _set_shutdown(self, time_setting):
        return await self.printer.set_auto_shutdown_time(time_setting)

    def do_calibrate(self):
        self.submit("calibrate", self._calibrate)

    async def _calibrate(self):
        return await self.printer.calibrate_label()

    def do_test_page(self):
        self.submit("test_page", self._test_page)

    async def _test_page(self):
        return await self.printer.print_test_page()

    def do_cancel(self):
        self.submit("cancel", self._cancel)

    async def _cancel(self):
        return await self.printer.cancel_print()

    # ── Heartbeat timer ──────────────────────────────────────────────

    def _start_heartbeat(self):
        self._stop_heartbeat()
        if self.loop:
            self._heartbeat_task = asyncio.run_coroutine_threadsafe(
                self._heartbeat_loop(), self.loop)

    def _stop_heartbeat(self):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _heartbeat_loop(self):
        while True:
            try:
                await asyncio.sleep(30)
                if self.printer and self.printer.is_connected:
                    # Queue heartbeat so it doesn't race with other operations
                    future = asyncio.get_event_loop().create_future()
                    async def _hb():
                        hb = await self.printer.heartbeat()
                        self.status_updated.emit(hb)
                        return hb
                    await self._queue.put(("heartbeat_auto", _hb, (), {}))
            except asyncio.CancelledError:
                break
            except Exception:
                pass
