# main.py
import ctypes
import math
import sys
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import threading
from datetime import datetime
from pathlib import Path

PASTA_LOGS = Path(__file__).resolve().parent / "logs"
PASTA_LOGS.mkdir(parents=True, exist_ok=True)
arquivo_log = PASTA_LOGS / "deskubuddy_debug.log"
formato_log = "%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s"

logging.basicConfig(
    handlers=[
        TimedRotatingFileHandler(
            arquivo_log,
            when="midnight",
            backupCount=14,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stderr),
    ],
    level=logging.INFO,
    format=formato_log,
)


def _registrar_excecao_nao_tratada(tipo, valor, traceback):
    if issubclass(tipo, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(tipo, valor, traceback)
        return
    logging.critical(
        "Exceção não tratada na thread principal.",
        exc_info=(tipo, valor, traceback),
    )


def _registrar_excecao_de_thread(args):
    logging.critical(
        "Exceção não tratada na thread %s.",
        args.thread.name if args.thread else "<desconhecida>",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


sys.excepthook = _registrar_excecao_nao_tratada
if hasattr(threading, "excepthook"):
    threading.excepthook = _registrar_excecao_de_thread

from PySide6.QtCore import Qt, QPoint, QRect, QTimer, Slot
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QWidget

from config import Config
from database import iniciar_banco
from trading_worker import DeskubuddyTradingEngine

logging.info("--- Inicializando Aplicação Deskubuddy ---")
logging.info(
    "PID=%s | executável=%s | diretório de trabalho=%s | log=%s",
    os.getpid(),
    sys.executable,
    os.getcwd(),
    arquivo_log,
)

class DeskubuddyPetWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.drag_position = QPoint()
        self.engine_thread = None
        self._is_paused = False
        self._has_started = False
        self._restart_when_stopped = False
        self._closing = False
        self.rows = 4
        self.cols = 6
        self.current_row = 0
        self.current_frame = 0
        self.movement_speed = 16
        self.cat_anchor = QPoint(220, 174)

        script_dir = os.path.dirname(os.path.abspath(__file__))
        image_path = os.path.join(script_dir, "walk.png")
        if not os.path.isfile(image_path):
            image_path = os.path.join(script_dir, "assets", "walk.png")
        self.sprite_sheet = QPixmap(image_path)
        if self.sprite_sheet.isNull():
            logging.error("Não foi possível carregar a sprite sheet: %s", image_path)
            self.frame_width = 0
            self.frame_height = 0
        else:
            self.frame_width = int(self.sprite_sheet.width() / self.cols)
            self.frame_height = int(self.sprite_sheet.height() / self.rows)

        try:
            iniciar_banco()
            logging.info("Banco SQLite verificado/inicializado com sucesso.")
        except Exception as e:
            logging.critical(f"Falha ao iniciar o banco de dados: {e}", exc_info=True)

        self.init_ui()
        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self.animate_cat)
        self.animation_timer.start(100)
        
        # Dispara o motor automaticamente ao iniciar a interface
        QTimer.singleShot(0, self.start_trading_engine)

    def init_ui(self):
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(330, 245)
        self.setToolTip(
            "Duplo clique: pausar ou reiniciar o bot\n"
            "Clique com o botão direito: opções\n"
            "Segure Ctrl para o gato seguir o mouse"
        )

        self.balloon_text = QLabel(
            "Olá! O bot vai iniciar automaticamente. Duplo clique para pausar.",
            self,
        )
        self.balloon_text.setGeometry(8, 8, 310, 88)
        self.balloon_text.setWordWrap(True)
        self.balloon_text.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.balloon_text.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.balloon_text.setStyleSheet(
            "QLabel { background: #fffdf5; color: #202820; "
            "border: 2px solid #355b42; border-radius: 18px; "
            "padding: 10px 13px; font: 11pt 'Segoe UI'; }"
        )

        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - 24, area.bottom() - self.height() - 32)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.engine_thread is not None and self.engine_thread.isRunning():
                if self._is_paused:
                    self._restart_when_stopped = True
                    self.show_message("Reinício solicitado. Aguarde eu encerrar a análise atual...")
                else:
                    self.stop_trading_engine()
            else:
                self.start_trading_engine()
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        if self._is_paused:
            menu.addAction("Reiniciar bot", self.start_trading_engine)
        else:
            menu.addAction("Pausar bot", self.stop_trading_engine)
        menu.addSeparator()
        menu.addAction("Fechar", self.close)
        menu.exec(event.globalPos())

    def animate_cat(self):
        self.current_frame = (self.current_frame + 1) % self.cols
        if self._control_pressionado():
            self._seguir_cursor()
        self.update()

    @staticmethod
    def _control_pressionado():
        if sys.platform == "win32":
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)
        return bool(QApplication.keyboardModifiers() & Qt.ControlModifier)

    def _seguir_cursor(self):
        cursor = QCursor.pos()
        cat_position = self.mapToGlobal(self.cat_anchor)
        dx = cursor.x() - cat_position.x()
        dy = cursor.y() - cat_position.y()
        distance = math.hypot(dx, dy)
        if distance <= 1:
            return

        step = min(self.movement_speed, distance)
        next_cat_position = QPoint(
            round(cat_position.x() + dx * step / distance),
            round(cat_position.y() + dy * step / distance),
        )
        self.move(next_cat_position - self.cat_anchor)

    def paintEvent(self, _event):
        del _event
        painter = QPainter()
        try:
            if not painter.begin(self):
                logging.error("Não foi possível iniciar o desenho da janela.")
                return

            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(
                QPen(
                    QColor("#355b42"),
                    2,
                    Qt.SolidLine,
                    Qt.RoundCap,
                    Qt.RoundJoin,
                )
            )
            painter.setBrush(QColor("#fffdf5"))

            speech_tail = QPainterPath()
            speech_tail.moveTo(222, 87)
            speech_tail.lineTo(239, 106)
            speech_tail.lineTo(251, 91)
            speech_tail.closeSubpath()
            painter.drawPath(speech_tail)

            if self.frame_width <= 0 or self.frame_height <= 0:
                return

            source_rect = QRect(
                self.current_frame * self.frame_width
                + int(self.frame_width * 0.30),
                self.current_row * self.frame_height
                + int(self.frame_height * 0.10),
                int(self.frame_width * 0.40),
                int(self.frame_height * 0.30),
            )
            scale = 1.8
            draw_width = int(source_rect.width() * scale)
            draw_height = int(source_rect.height() * scale)
            dest_rect = QRect(
                220 - draw_width // 2,
                174 - draw_height // 2,
                draw_width,
                draw_height,
            )
            painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
            painter.drawPixmap(dest_rect, self.sprite_sheet, source_rect)
        finally:
            if painter.isActive():
                painter.end()

    # ----------------------------------------------------------------------
    # Métodos de controle do Motor de Trading
    # ----------------------------------------------------------------------

    def start_trading_engine(self):
        if self.engine_thread is not None and self.engine_thread.isRunning():
            return

        try:
            Config.validate()
        except ValueError as erro:
            logging.error("Configuração de trading recusada: %s", erro)
            self.show_message(f"Configuração inválida: {erro}")
            self._is_paused = True
            return

        self._restart_when_stopped = False
        self._is_paused = False
        logging.info("Criando e iniciando a thread DeskubuddyTradingEngine...")
        self.engine_thread = DeskubuddyTradingEngine(
            email=Config.IQ_EMAIL,
            password=Config.IQ_PASSWORD,
            account_type=Config.IQ_ACCOUNT_TYPE,
            resume_manual_pause=self._has_started,
        )
        self._has_started = True
        self.engine_thread.status_changed.connect(self.update_pet_status)
        self.engine_thread.erro_disparado.connect(self.handle_engine_error)
        self.engine_thread.log_message.connect(self.show_message)
        self.engine_thread.finished.connect(self.engine_finished)

        self.engine_thread.start()
        self.show_message("Iniciando o bot e conectando à corretora...")

    def stop_trading_engine(self):
        self._is_paused = True
        self._restart_when_stopped = False
        logging.info("Solicitação de parada do motor pelo usuário.")
        if self.engine_thread is not None and self.engine_thread.isRunning():
            self.engine_thread.stop()
        self.show_message("Pausado. Duplo clique para reiniciar o bot.")

    def closeEvent(self, event):
        self._closing = True
        logging.info("Encerrando aplicação por solicitação de fechar.")
        if self.engine_thread is not None and self.engine_thread.isRunning():
            self.stop_trading_engine()
            self.show_message("Encerrando as análises antes de fechar...")
            event.ignore()
            return
        event.accept()

    @Slot(str, str)
    def update_pet_status(self, estado, mensagem):
        logging.info(f"STATUS [{estado}]: {mensagem}")
        self.show_message(f"{estado}: {mensagem}")

    @Slot(str)
    def handle_engine_error(self, erro):
        self._is_paused = True
        logging.error(f"Erro operacional disparado: {erro}")
        self.show_message(
            f"⚠️ Erro operacional. Detalhes em {arquivo_log.name}; "
            "duplo clique para reiniciar."
        )

    @Slot(str)
    def show_message(self, mensagem):
        texto = " ".join(mensagem.split())
        if len(texto) > 180:
            texto = texto[:177].rstrip() + "..."
        self.balloon_text.setText(texto)

    @Slot()
    def engine_finished(self):
        thread = self.sender()
        if thread is not self.engine_thread:
            return

        logging.warning("Sinal 'finished' da thread do motor recebido.")
        self.engine_thread = None
        thread.deleteLater()

        if self._closing:
            self.close()
        elif self._restart_when_stopped:
            logging.info("Reiniciando motor automaticamente após parada...")
            self._restart_when_stopped = False
            QTimer.singleShot(0, self.start_trading_engine)
        elif not self._is_paused:
            # Caso a thread tenha morrido sem o usuário apertar Pause ou Fechar
            logging.error("ATENÇÃO: A thread encerrou de forma inesperada sem solicitação de pausa.")
            self.show_message("⚠️ O motor parou inesperadamente. Verifique o arquivo de log em /logs.")


if __name__ == "__main__":
    try:
        from app.process_guard import (
            MAIN_MUTEX_NAME,
            acquire_named_mutex,
            release_named_mutex,
        )

        _main_mutex, _main_mutex_acquired = acquire_named_mutex(MAIN_MUTEX_NAME)
        if not _main_mutex_acquired:
            logging.warning(
                "Outra instância do Deskubuddy já está em execução; "
                "esta inicialização será encerrada."
            )
            sys.exit(0)
        import atexit

        atexit.register(release_named_mutex, _main_mutex)

        app = QApplication(sys.argv)
        app.aboutToQuit.connect(
            lambda: logging.info("Loop de eventos Qt encerrado normalmente.")
        )
        window = DeskubuddyPetWindow()
        window.show()
        codigo_saida = app.exec()
        logging.info("Aplicação encerrada com código %s.", codigo_saida)
        sys.exit(codigo_saida)
    except Exception:
        logging.critical("Falha fatal durante inicialização/execução da interface.", exc_info=True)
        raise