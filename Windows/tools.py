import winsound
import threading
import time

TIMEOUT = 60*1*1000 # 1 minutes

class RingService:
    def __init__(self):
        self._ringing = False
        self._thread = None

    def _beep_loop(self):
        time_start = time.time()
        while self._ringing:
            # Play a beep at 2000 Hz for 500 milliseconds
            winsound.Beep(2000, 5000)
            time.sleep(0.05)
            if time.time() - time_start > TIMEOUT:
                self._ringing = False
                break

    def start_beep(self):
        if not self._ringing:
            self._ringing = True
            self._thread = threading.Thread(target=self._beep_loop, daemon=True)
            self._thread.start()
            print("[RingService] Started beeping.")

    def stop_beep(self):
        if self._ringing:
            self._ringing = False
            if self._thread is not None:
                self._thread.join(timeout=1.0)
                self._thread = None
            print("[RingService] Stopped beeping.")

# Global instance
ring_service = RingService()
