import atexit
import logging
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.process_guard import (
    MAIN_MUTEX_NAME,
    SUPERVISOR_MUTEX_NAME,
    acquire_named_mutex,
    named_mutex_exists,
    release_named_mutex,
)


PROJECT_ROOT = Path(__file__).resolve().parent
MAIN_SCRIPT = PROJECT_ROOT / "main.py"
LOG_DIR = PROJECT_ROOT / "logs"
SUPERVISOR_LOG = LOG_DIR / "process_supervisor.log"
POLL_INTERVAL_SECONDS = 3
STARTUP_GRACE_SECONDS = 15
INITIAL_RESTART_DELAY_SECONDS = 2
MAX_RESTART_DELAY_SECONDS = 60
STABLE_RUNTIME_SECONDS = 60


class MainProcessSupervisor:
    def __init__(self):
        self.stop_requested = False
        self.child = None
        self.child_output_thread = None
        self.child_output_file = None
        self.restart_delay = INITIAL_RESTART_DELAY_SECONDS

    def _request_stop(self, signum, _frame):
        logging.info("Parada do supervisor solicitada (sinal %s).", signum)
        self.stop_requested = True

    def _install_signal_handlers(self):
        signal.signal(signal.SIGINT, self._request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self._request_stop)

    @staticmethod
    def _python_executable():
        project_python = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
        return str(project_python if project_python.is_file() else Path(sys.executable))

    def _start_main(self):
        if not MAIN_SCRIPT.is_file():
            raise FileNotFoundError(f"Arquivo principal não encontrado: {MAIN_SCRIPT}")

        self.child_output_file = SUPERVISOR_LOG.open(
            "a", encoding="utf-8", buffering=1
        )
        try:
            self.child = subprocess.Popen(
                [self._python_executable(), str(MAIN_SCRIPT)],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception:
            self.child_output_file.close()
            self.child_output_file = None
            raise
        self.child_output_thread = threading.Thread(
            target=self._relay_child_output,
            name="MainOutputRelay",
            daemon=True,
        )
        self.child_output_thread.start()
        self.child_started_at = time.monotonic()
        self.child_mutex_warning_logged = False
        logging.info(
            "main.py iniciado pelo supervisor (PID=%s).",
            self.child.pid,
        )

    def _relay_child_output(self):
        if self.child is None or self.child.stdout is None:
            return
        console_available = sys.stdout is not None
        try:
            for line in self.child.stdout:
                if self.child_output_file is not None:
                    self.child_output_file.write(line)
                    self.child_output_file.flush()
                try:
                    if console_available:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                except (BrokenPipeError, OSError):
                    console_available = False
                    logging.warning(
                        "A saída do main.py não está ligada a uma janela interativa; "
                        "continuando o registro em %s.",
                        SUPERVISOR_LOG,
                    )
        except (OSError, UnicodeError):
            logging.exception("Falha lendo/espelhando a saída do main.py.")

    def _finish_child_output(self):
        if self.child_output_thread is not None:
            self.child_output_thread.join(timeout=3)
            self.child_output_thread = None
        if self.child is not None and self.child.stdout is not None:
            self.child.stdout.close()
        if self.child_output_file is not None:
            self.child_output_file.close()
            self.child_output_file = None

    def _wait_for_main_mutex_release(self):
        while not self.stop_requested and named_mutex_exists(MAIN_MUTEX_NAME):
            time.sleep(POLL_INTERVAL_SECONDS)

    def _monitor_child(self):
        while not self.stop_requested:
            return_code = self.child.poll()
            if return_code is not None:
                runtime = time.monotonic() - self.child_started_at
                nivel = logging.WARNING if return_code == 0 else logging.ERROR
                logging.log(
                    nivel,
                    "main.py encerrou (PID=%s, código=%s, duração=%.1fs).",
                    self.child.pid,
                    return_code,
                    runtime,
                )
                self._finish_child_output()
                if runtime >= STABLE_RUNTIME_SECONDS:
                    self.restart_delay = INITIAL_RESTART_DELAY_SECONDS
                return

            if time.monotonic() - self.child_started_at >= STARTUP_GRACE_SECONDS:
                if (
                    not self.child_mutex_warning_logged
                    and not named_mutex_exists(MAIN_MUTEX_NAME)
                ):
                    logging.warning(
                        "Processo filho está vivo, mas main.py ainda não registrou "
                        "o mutex de instância única."
                    )
                    self.child_mutex_warning_logged = True

            time.sleep(POLL_INTERVAL_SECONDS)

    def _stop_child(self):
        if self.child is None or self.child.poll() is not None:
            return
        logging.warning(
            "Encerrando processo filho (PID=%s) por solicitação de parada.",
            self.child.pid,
        )
        self.child.terminate()
        try:
            self.child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logging.error(
                "Processo filho PID=%s não encerrou após terminate; forçando saída.",
                self.child.pid,
            )
            self.child.kill()
            self.child.wait()

    def run(self):
        self._install_signal_handlers()
        supervisor_mutex, acquired = acquire_named_mutex(SUPERVISOR_MUTEX_NAME)
        if not acquired:
            logging.warning("Já existe outro supervisor Deskubuddy em execução.")
            return 0
        atexit.register(release_named_mutex, supervisor_mutex)

        logging.info(
            "Supervisor iniciado. Projeto=%s | intervalo=%ss.",
            PROJECT_ROOT,
            POLL_INTERVAL_SECONDS,
        )
        try:
            while not self.stop_requested:
                if named_mutex_exists(MAIN_MUTEX_NAME):
                    logging.info(
                        "main.py já está ativo fora do supervisor; "
                        "aguardando encerramento para evitar duplicação."
                    )
                    self._wait_for_main_mutex_release()
                    continue

                try:
                    self._start_main()
                    self._monitor_child()
                except Exception:
                    logging.exception("Falha no ciclo de monitoramento do main.py.")

                if self.stop_requested:
                    break

                self._wait_for_main_mutex_release()
                if self.stop_requested:
                    break

                logging.warning(
                    "Reiniciando main.py em %ss.",
                    self.restart_delay,
                )
                if self._wait(self.restart_delay):
                    break
                self.restart_delay = min(
                    self.restart_delay * 2,
                    MAX_RESTART_DELAY_SECONDS,
                )
        finally:
            self._stop_child()
            self._finish_child_output()
            logging.info("Supervisor encerrado.")
        return 0

    def _wait(self, duration):
        deadline = time.monotonic() + duration
        while not self.stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
        return self.stop_requested


def configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        handlers=[
            TimedRotatingFileHandler(
                SUPERVISOR_LOG,
                when="midnight",
                backupCount=14,
                encoding="utf-8",
            ),
            logging.StreamHandler(sys.stdout),
        ],
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def main():
    configure_logging()
    return MainProcessSupervisor().run()


if __name__ == "__main__":
    raise SystemExit(main())
