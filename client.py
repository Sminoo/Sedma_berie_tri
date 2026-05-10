import pygame
import socket
import json
import select
import struct
import threading
import subprocess
import sys
import os
import errno
import time
import zlib
import re
import logging
from typing import List, Optional, Tuple, Dict
from queue import Queue
from card import Card

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

# --- Constants ---
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 720
PORT = 65432

# Support running the bundled executable in headless server mode.
# When the application executable is invoked with --run-server, start the
# server loop and exit. This allows launching the same .exe as a background
# server subprocess (used by LanServerManager) without creating a GUI.
if "--run-server" in sys.argv:
    try:
        # parse optional --port after --run-server
        port = PORT
        try:
            idx = sys.argv.index("--run-server")
            if "--port" in sys.argv[idx+1:]:
                pidx = sys.argv.index("--port", idx+1)
                port = int(sys.argv[pidx+1])
        except Exception:
            port = PORT
        import server as _embedded_server
        _srv = _embedded_server.MultiRoomServer(port)
        _srv.start()
    except Exception as _e:
        # If server startup fails in this mode, print and exit with error
        print(f"Server (headless) failed to start: {_e}")
    finally:
        sys.exit(0)

CARD_WIDTH, CARD_HEIGHT = 80, 142
CARD_HIGHLIGHT_THICKNESS = 3
CLICK_DEBOUNCE_MS = 200
LEADERBOARD_DURATION = 5

# Colors
BACKGROUND_COLOR = (0, 0, 0)
TEXT_COLOR = (255, 255, 255)
HIGHLIGHT_COLOR = (255, 0, 0)
PLACEHOLDER_COLOR = (150, 150, 150)
BUTTON_COLOR = (100, 100, 100)
BUTTON_HOVER_COLOR = (150, 150, 150)
CUSTOMIZE_BUTTON_COLOR = (80, 80, 140)
CUSTOMIZE_HOVER_COLOR = (120, 120, 200)
ROOM_ITEM_COLOR = (60, 60, 80, 15)
ROOM_ITEM_HOVER_COLOR = (80, 80, 100)
SUCCESS_COLOR = (0, 200, 0)
ERROR_COLOR = (255, 100, 100)
LAN_ACTIVE_COLOR = (0, 160, 0)
LAN_INACTIVE_COLOR = (140, 60, 60)


class CardSprite(pygame.sprite.Sprite):
    """Sprite wrapper for rendering a card at a position with rotation."""

    def __init__(self, card: Card, x: int, y: int, angle: float):
        super().__init__()
        self.card = card
        self.image = pygame.transform.rotate(card.image, angle)
        self.rect = self.image.get_rect(topleft=(x, y))
        self.angle = angle


class CardAnimation:
    """A single card flying from a start position to a target position."""

    def __init__(self, image: pygame.Surface, start: Tuple[float, float],
                 end: Tuple[float, float], duration_ms: int, angle: float = 0):
        self.image = image
        self.start = start
        self.end = end
        self.duration_ms = duration_ms
        self.elapsed_ms: float = 0
        self.angle = angle
        self.done = False

    def update(self, dt_ms: float) -> None:
        self.elapsed_ms = min(self.elapsed_ms + dt_ms, self.duration_ms)
        if self.elapsed_ms >= self.duration_ms:
            self.done = True

    @property
    def current_pos(self) -> Tuple[float, float]:
        t = self.elapsed_ms / self.duration_ms if self.duration_ms > 0 else 1.0
        # Smooth ease-out
        t = 1 - (1 - t) ** 3
        x = self.start[0] + (self.end[0] - self.start[0]) * t
        y = self.start[1] + (self.end[1] - self.start[1]) * t
        return x, y

    def draw(self, screen: pygame.Surface) -> None:
        if self.done:
            return
        rotated = pygame.transform.rotate(self.image, self.angle)
        screen.blit(rotated, (int(self.current_pos[0]), int(self.current_pos[1])))


class AnimationManager:
    """Manages card animations: dealing, playing, and drawing."""

    DEAL_INTERVAL = 100
    DEAL_DURATION = 250
    PLAY_DURATION = 200

    def __init__(self):
        self._active: List[CardAnimation] = []
        self._pending: List[Tuple] = []
        self._queue_timer: float = 0.0
        # (player_index, card_index) pairs that are hidden until their animation lands
        self.hidden_cards: set = set()
        self.on_deal_complete = None
        # Called with (player_index, card_index) each time a deal animation lands
        self.on_card_land = None

    @property
    def is_dealing(self) -> bool:
        return bool(self._active or self._pending)

    def queue_deal(self, image: pygame.Surface, start: Tuple[float, float],
                   end: Tuple[float, float], angle: float,
                   player_index: int, card_index: int,
                   pre_hidden: bool = False) -> None:
        """Queue a deal animation. If pre_hidden=True the caller already added to hidden_cards."""
        if not pre_hidden:
            self.hidden_cards.add((player_index, card_index))
        self._pending.append((image, start, end, angle, player_index, card_index))

    def play_card(self, image: pygame.Surface, start: Tuple[float, float],
                  end: Tuple[float, float], angle: float = 0) -> None:
        self._active.append(CardAnimation(image, start, end, self.PLAY_DURATION, angle))

    def update(self, dt_ms: float) -> None:
        if self._pending:
            self._queue_timer -= dt_ms
            if self._queue_timer <= 0:
                img, s, e, ang, pi, ci = self._pending.pop(0)
                anim = CardAnimation(img, s, e, self.DEAL_DURATION, ang)
                anim.player_index = pi
                anim.card_index = ci
                self._active.append(anim)
                self._queue_timer = self.DEAL_INTERVAL

        for anim in self._active:
            anim.update(dt_ms)

        for a in self._active:
            if a.done:
                pi = getattr(a, "player_index", None)
                ci = getattr(a, "card_index", None)
                if pi is not None and ci is not None:
                    self.hidden_cards.discard((pi, ci))
                    if self.on_card_land:
                        self.on_card_land(pi, ci)

        self._active = [a for a in self._active if not a.done]

        if self.on_deal_complete and not self._active and not self._pending:
            cb = self.on_deal_complete
            self.on_deal_complete = None
            cb()

    def draw(self, screen: pygame.Surface) -> None:
        for anim in self._active:
            anim.draw(screen)

    def clear(self) -> None:
        self._active.clear()
        self._pending.clear()
        self._queue_timer = 0.0
        self.hidden_cards.clear()
        self.on_deal_complete = None
        # Note: on_card_land is NOT cleared — it is wired up once in EventHandler


class LayoutManager:
    """Manages card and UI element positions for different player seat arrangements."""

    def __init__(self, screen_width: int, screen_height: int):
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.positions = [
            {"x": screen_width // 2, "y": screen_height - 172, "angle": 0, "offset": 84},
            {"x": screen_width - 172, "y": screen_height // 2, "angle": -90, "offset": 84},
            {"x": screen_width // 2, "y": 30, "angle": 180, "offset": 84},
            {"x": 30, "y": screen_height // 2, "angle": 90, "offset": 84}
        ]
        self.draw_pile_rect = pygame.Rect(
            screen_width // 2 - 53, screen_height // 2 - 53,
            CARD_WIDTH + 6, CARD_HEIGHT + 6
        )
        self.discard_pile_pos = (screen_width // 2 + 50, screen_height // 2 - 50)
        self.name_positions = [
            {"x": screen_width // 2, "y": screen_height - 190, "align": "center", "angle": 0},
            {"x": screen_width - 190, "y": screen_height // 2, "align": "center", "angle": 90},
            {"x": screen_width // 2, "y": 190, "align": "center", "angle": 0},
            {"x": 190, "y": screen_height // 2, "align": "center", "angle": -90}
        ]

    def get_player_position(self, pos_index: int, num_cards: int, card_index: int,
                            is_local: bool = False) -> Tuple[int, int, float]:
        """Calculate card position based on seat, hand size, and card index."""
        pos = self.positions[pos_index]
        base_offset = 84

        if is_local:
            offset = base_offset if num_cards <= 8 else max(35, base_offset - (num_cards - 8) * 3)
        else:
            offset = max(14, base_offset - max(0, (num_cards - 2) * 8)) - 6

        if pos_index in (0, 2):
            x = pos["x"] - (num_cards * offset // 2) + (card_index * offset)
            y = pos["y"]
        else:
            x = pos["x"]
            y = pos["y"] - (num_cards * offset // 2) + (card_index * offset)

        return x, y, pos["angle"]


class NetworkManager:
    """Handles TCP socket communication with the game server."""

    def __init__(self, port: int = PORT):
        self.client_socket: Optional[socket.socket] = None
        self.message_queue: Queue = Queue()
        self.port = port

    def connect(self, host: str) -> bool:
        """Establish non-blocking connection to the server."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.connect((host, self.port))
        except socket.error as e:
            if e.errno not in (errno.EWOULDBLOCK, 10035):
                logger.error(f"Connection failed: {e}")
                sock.close()
                return False
        try:
            _, writable, _ = select.select([], [sock], [], 5.0)
            if writable and sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                self.client_socket = sock
                return True
            sock.close()
            return False
        except socket.error as e:
            logger.error(f"Connection failed: {e}")
            sock.close()
            return False

    def disconnect(self) -> None:
        """Close connection and clear message queue."""
        if self.client_socket:
            try:
                self.client_socket.close()
            except socket.error as e:
                logger.error(f"Error closing socket: {e}")
            self.client_socket = None
        self.message_queue = Queue()

    def send_message(self, message: dict) -> bool:
        """Send a compressed JSON message with length prefix."""
        if not self.client_socket:
            return False
        try:
            data = zlib.compress(json.dumps(message, separators=(',', ':')).encode())
            self.client_socket.sendall(struct.pack('!I', len(data)) + data)
            return True
        except socket.error as e:
            logger.error(f"Send error: {e}")
            return False

    def receive_message(self, retries: int = 3, delay: float = 0.1) -> Optional[dict]:
        """Receive and decompress a length-prefixed JSON message."""
        sock = self.client_socket
        if sock is None:
            return None

        buffer = b""
        for _ in range(retries):
            try:
                if len(buffer) < 4:
                    packet = sock.recv(4)
                    if packet == b"":
                        return {"t": "server_disconnected"}
                    if not packet:
                        return None
                    buffer += packet
                length = struct.unpack('!I', buffer[:4])[0]
                while len(buffer) - 4 < length:
                    packet = sock.recv(length - (len(buffer) - 4))
                    if packet == b"":
                        return {"t": "server_disconnected"}
                    if not packet:
                        return None
                    buffer += packet
                return json.loads(zlib.decompress(buffer[4:]).decode())
            except socket.error as e:
                if hasattr(e, 'winerror') and e.winerror == 10038:
                    return None
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(delay)
                    continue
                logger.error(f"Receive error: {e}")
                return {"t": "server_disconnected"}
            except (json.JSONDecodeError, struct.error, zlib.error) as e:
                logger.error(f"Decode error: {e}")
                return None
        return None

    def start_listener(self, running_flag, message_callback):
        """Start background thread to listen for incoming messages."""

        def listen():
            while running_flag[0]:
                if self.client_socket is None:
                    break
                message = self.receive_message()
                if message:
                    self.message_queue.put(message)
                time.sleep(0.01)

        threading.Thread(target=listen, daemon=True).start()


class UIElement:
    """Clickable button with hover effect."""

    def __init__(self, rect: pygame.Rect, text: str, font: pygame.font.Font,
                 bg_color: Tuple[int, int, int], hover_color: Tuple[int, int, int] = None):
        self.rect = rect
        self.text = text
        self.font = font
        self.bg_color = bg_color
        self.hover_color = hover_color or BUTTON_HOVER_COLOR
        self.surface = self.font.render(text, True, TEXT_COLOR)

    def draw(self, screen: pygame.Surface, mouse_pos: Tuple[int, int]) -> None:
        color = self.hover_color if self.rect.collidepoint(mouse_pos) else self.bg_color
        pygame.draw.rect(screen, color, self.rect, border_radius=6)
        screen.blit(self.surface, self.surface.get_rect(center=self.rect.center))

    def update_text(self, new_text: str) -> None:
        self.text = new_text
        self.surface = self.font.render(new_text, True, TEXT_COLOR)


class InputField:
    """Text input field with placeholder and activation state."""

    def __init__(self, rect: pygame.Rect, placeholder: str, font: pygame.font.Font,
                 bg_color: Tuple[int, int, int], max_len: int = 20, is_password: bool = False):
        self.rect = rect
        self.placeholder = placeholder
        self.font = font
        self.bg_color = bg_color
        self.text = ""
        self.active = False
        self.max_len = max_len
        self.is_password = is_password

    def draw(self, screen: pygame.Surface) -> None:
        bg_color = BUTTON_HOVER_COLOR if self.active else self.bg_color
        pygame.draw.rect(screen, bg_color, self.rect, border_radius=4)
        display_text = self.text or self.placeholder
        if self.is_password and self.text:
            display_text = "*" * len(self.text)
        text_color = TEXT_COLOR if self.text else PLACEHOLDER_COLOR
        surface = self.font.render(display_text, True, text_color)
        screen.blit(surface, surface.get_rect(center=self.rect.center))

    def handle_key(self, event) -> bool:
        if not self.active:
            return False
        if event.key == pygame.K_RETURN:
            return True
        elif event.key == pygame.K_BACKSPACE:
            self.text = self.text[:-1]
            return True
        elif event.unicode.isprintable() and len(self.text) < self.max_len:
            self.text += event.unicode
            return True
        return False


class StateManager:
    """Tracks the current game state, screen, and player information."""

    def __init__(self):
        self.state = "menu"
        self.local_player: Optional[int] = None
        self.game_state: Optional[Dict] = None
        self.player_names: Dict[int, str] = {}
        self.num_players: int = 4
        self.room_max_players: int = 4
        self.waiting_message: Optional[str] = None
        self.waiting_start: float = time.time()
        self.leaderboard_start: float = 0
        self.leaderboard_data: Optional[List[Dict]] = None
        self.target_private_room_id: Optional[str] = None
        self.dealing_animation: bool = False
        self.tournament_round_over: bool = False
        self.room_rules: dict = {}
        self.show_credits: bool = False
        self.tournament_results: Optional[List[Dict]] = None
        self.tournament_round: int = 0
        self.tournament_penalties: dict = {}
        # Holds "go" data while waiting 2s before showing leaderboard
        self.leaderboard_pending: bool = False
        self.leaderboard_pending_start: float = 0
        self.previous_state: Optional[str] = None


class LanServerManager:
    """Manages starting/stopping a local LAN server subprocess."""

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.running: bool = False
        self.local_ip: Optional[str] = None
        import atexit
        atexit.register(self.stop)

    def get_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except socket.error:
            return "127.0.0.1"

    def start(self) -> bool:
        if self.running:
            return False

        # Prefer embedding the server in-process (thread). If that fails
        # (e.g. module import issues in bundled exe), fall back to launching
        # the same executable in headless server mode as a subprocess inside
        # a background thread.
        try:
            import server as server_mod
            # Create server without installing signal handlers when embedding
            self.server_instance = server_mod.MultiRoomServer(PORT, install_signal_handler=False)
            t = threading.Thread(target=self.server_instance.start, daemon=True)
            t.start()
            self.server_thread = t
            self.local_ip = self.get_local_ip()
            self.running = True
            return True
        except Exception:
            # Fall back to subprocess-in-thread approach
            def _run_subprocess():
                try:
                    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                    # Launch the same executable with --run-server so it starts the server loop
                    cmd = [sys.executable, sys.argv[0], "--run-server", "--port", str(PORT)]
                    proc = subprocess.Popen(cmd, creationflags=creation_flags)
                    self.process = proc
                    proc.wait()
                except Exception as e:
                    logger.error(f"LAN server subprocess failed: {e}")
                finally:
                    self.running = False
                    self.local_ip = None

            thread = threading.Thread(target=_run_subprocess, daemon=True)
            thread.start()
            # give the subprocess a moment to start
            time.sleep(0.5)
            if self.process is None or self.process.poll() is not None:
                return False
            self.running = True
            self.local_ip = self.get_local_ip()
            return True

    def stop(self) -> None:
        # Stop embedded server if running
        if getattr(self, 'server_instance', None):
            try:
                # Prefer graceful shutdown if available
                if hasattr(self.server_instance, 'shutdown'):
                    try:
                        self.server_instance.shutdown()
                    except Exception:
                        pass
                else:
                    try:
                        self.server_instance.server_socket.close()
                    except Exception:
                        pass
                    try:
                        self.server_instance.sel.close()
                    except Exception:
                        pass
            finally:
                self.server_instance = None
                self.server_thread = None

        # Stop subprocess if used
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

        self.running = False
        self.local_ip = None


class Renderer:
    """Handles all drawing operations for different game screens."""

    def __init__(self, screen: pygame.Surface, layout: LayoutManager, font: pygame.font.Font,
                 title_font: pygame.font.Font, small_font: pygame.font.Font):
        self.screen = screen
        self.layout = layout
        self.font = font
        self.title_font = title_font
        self.small_font = small_font
        self.background: Optional[pygame.Surface] = None
        self.card_back: Optional[pygame.Surface] = None

        bg_dir = os.path.join("assets", "backgrounds")
        try:
            bgs = [f for f in os.listdir(bg_dir) if f.lower().endswith((".png", ".jpg", ".bmp"))]
            bgs.sort()
            self.background_options = bgs if bgs else ["background_green.png"]
        except OSError:
            self.background_options = ["background_green.png"]
        backs_dir = os.path.join("assets", "cards", "card_backs")
        try:
            backs = [f for f in os.listdir(backs_dir) if f.lower().endswith((".png", ".jpg", ".bmp"))]
            backs.sort()
            self.card_back_themes = backs if backs else ["back.png"]
        except OSError:
            self.card_back_themes = ["back.png"]

        self.selected_background = self.background_options[0]
        self.selected_card_theme = self.card_back_themes[0]
        self.current_background_path = f"assets/backgrounds/{self.selected_background}"
        self.current_card_back_path = os.path.join("assets", "cards", "card_backs", self.selected_card_theme)

        self.suit_images = {}
        suits = ["srdce", "zelen", "zalud", "gula"]
        for s in suits:
            path = os.path.join("assets", "znaky", f"{s}.png")
            try:
                img = pygame.image.load(path).convert_alpha()
                self.suit_images[s] = img
            except Exception:
                fallback = pygame.Surface((48, 48), pygame.SRCALPHA)
                colors = {"srdce": (200, 50, 50), "zelen": (50, 200, 50), "zalud": (150, 100, 50),
                          "gula": (200, 200, 50)}
                fallback.fill(colors.get(s, (120, 120, 120)))
                self.suit_images[s] = fallback

        self.selected_room_max_players = 4

        # Window size customization removed — client will keep default screen dimensions.
        # (Resolution options were intentionally removed to simplify the Customize screen.)

    def load_assets(self, background_path: str, card_back_path: str, size: Tuple[int, int]) -> None:
        try:
            self.background = pygame.image.load(background_path)
            if self.background.get_size() != (SCREEN_WIDTH, SCREEN_HEIGHT):
                self.background = pygame.transform.scale(self.background, (SCREEN_WIDTH, SCREEN_HEIGHT))
        except pygame.error as e:
            logger.warning(f"Failed to load background {background_path}: {e}")
            self.background = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT))
            self.background.fill((20, 60, 20))

        try:
            self.card_back = pygame.transform.scale(pygame.image.load(card_back_path), size)
        except pygame.error as e:
            logger.warning(f"Failed to load card back {card_back_path}: {e}")
            self.card_back = pygame.Surface(size)
            self.card_back.fill((180, 30, 30))

    def _draw_background(self) -> None:
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

    def render_menu(self, ip_field: InputField, name_field: InputField, connect_btn: UIElement,
                    close_btn: UIElement, lan_btn: UIElement, lan_server: LanServerManager,
                    waiting_message: Optional[str], public_btn: UIElement = None) -> None:
        self._draw_background()

        title = self.title_font.render("Sedma Bere Tri", True, TEXT_COLOR)
        self.screen.blit(title, title.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 250)))

        ip_label = self.font.render("Server IP:", True, TEXT_COLOR)
        self.screen.blit(ip_label, (ip_field.rect.x, ip_field.rect.y - 30))
        ip_field.draw(self.screen)

        name_label = self.font.render("Your Name:", True, TEXT_COLOR)
        self.screen.blit(name_label, (name_field.rect.x, name_field.rect.y - 30))
        name_field.draw(self.screen)

        mouse_pos = pygame.mouse.get_pos()
        connect_btn.draw(self.screen, mouse_pos)
        close_btn.draw(self.screen, mouse_pos)

        if public_btn:
            public_btn.draw(self.screen, mouse_pos)

        if lan_server.running and lan_server.local_ip:
            ip_text = self.font.render(f"LAN IP: {lan_server.local_ip}:{PORT}", True, SUCCESS_COLOR)
            ip_rect = ip_text.get_rect(right=SCREEN_WIDTH - 20, bottom=lan_btn.rect.top - 5)
            self.screen.blit(ip_text, ip_rect)

            status_text = self.small_font.render("● Server Running", True, SUCCESS_COLOR)
            status_rect = status_text.get_rect(right=SCREEN_WIDTH - 20, bottom=ip_rect.top - 3)
            self.screen.blit(status_text, status_rect)

        lan_btn.draw(self.screen, mouse_pos)

        if waiting_message:
            is_error = "error" in waiting_message.lower() or "please" in waiting_message.lower()
            msg_color = ERROR_COLOR if is_error else TEXT_COLOR
            msg_surface = self.font.render(waiting_message, True, msg_color)
            self.screen.blit(msg_surface, msg_surface.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 185)))

    def render_public_menu(self, name_field: InputField, connect_btn: UIElement, close_btn: UIElement,
                           customize_btn: UIElement, back_to_lan_btn: UIElement, waiting_message: Optional[str]) -> None:
        self._draw_background()

        # Main title and smaller 'Public server' subtitle (position matches main menu)
        main_title = self.title_font.render("Sedma Bere Tri", True, TEXT_COLOR)
        self.screen.blit(main_title, main_title.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 250)))
        subtitle = self.small_font.render("Public server", True, PLACEHOLDER_COLOR)
        self.screen.blit(subtitle, subtitle.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 210)))

        # Name field (centered similar to main menu)
        name_label = self.font.render("Your Name:", True, TEXT_COLOR)
        self.screen.blit(name_label, (name_field.rect.x, name_field.rect.y - 30))
        name_field.draw(self.screen)

        mouse_pos = pygame.mouse.get_pos()
        connect_btn.draw(self.screen, mouse_pos)
        close_btn.draw(self.screen, mouse_pos)
        customize_btn.draw(self.screen, mouse_pos)

        # Back to LAN button (top-right)
        if back_to_lan_btn:
            back_to_lan_btn.draw(self.screen, mouse_pos)

        if waiting_message:
            is_error = "error" in waiting_message.lower() or "please" in waiting_message.lower()
            msg_color = ERROR_COLOR if is_error else TEXT_COLOR
            msg_surface = self.font.render(waiting_message, True, msg_color)
            # Keep message placement consistent with LAN menu to avoid overlap with Close button.
            self.screen.blit(msg_surface, msg_surface.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 185)))

    def render_customize(self, mouse_pos: Tuple[int, int]) -> None:
        self._draw_background()

        title = self.title_font.render("Customize", True, TEXT_COLOR)
        self.screen.blit(title, title.get_rect(center=(SCREEN_WIDTH // 2, 80)))

        bg_title = self.font.render("Background", True, TEXT_COLOR)
        self.screen.blit(bg_title, bg_title.get_rect(center=(SCREEN_WIDTH // 4, 160)))

        # Arrow selector for background — no preview, background is visible behind
        bcx = SCREEN_WIDTH // 4
        bsel_y = 290
        barr_w, barr_h = 44, 44
        bleft_rect  = pygame.Rect(bcx - 80, bsel_y - barr_h // 2, barr_w, barr_h)
        bright_rect = pygame.Rect(bcx + 36, bsel_y - barr_h // 2, barr_w, barr_h)
        for rect, symbol in [(bleft_rect, "<"), (bright_rect, ">")]:
            color = CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR
            pygame.draw.rect(self.screen, color, rect, border_radius=8)
            sym = self.title_font.render(symbol, True, TEXT_COLOR)
            self.screen.blit(sym, sym.get_rect(center=rect.center))
        lbl = self.font.render("Change background", True, TEXT_COLOR)
        self.screen.blit(lbl, lbl.get_rect(center=(bcx, bsel_y - 40)))

        if self.background_options:
            bidx = self.background_options.index(self.selected_background) if self.selected_background in self.background_options else 0
            bcounter = self.small_font.render(f"{bidx + 1} / {len(self.background_options)}", True, PLACEHOLDER_COLOR)
            self.screen.blit(bcounter, bcounter.get_rect(center=(bcx, bsel_y + barr_h // 2 + 20)))

        card_title = self.font.render("Deck Design", True, TEXT_COLOR)
        self.screen.blit(card_title, card_title.get_rect(center=(SCREEN_WIDTH * 3 // 4, 160)))

        # Arrow selector for card back
        cx = SCREEN_WIDTH * 3 // 4
        sel_y = 290

        # Large card preview
        preview_size = (int(CARD_WIDTH * 1.4), int(CARD_HEIGHT * 1.4))
        if self.card_back:
            preview_img = pygame.transform.scale(self.card_back, preview_size)
            preview_rect = preview_img.get_rect(center=(cx, sel_y))
            self.screen.blit(preview_img, preview_rect)
        else:
            placeholder_rect = pygame.Rect(0, 0, *preview_size)
            placeholder_rect.center = (cx, sel_y)
            pygame.draw.rect(self.screen, PLACEHOLDER_COLOR, placeholder_rect, border_radius=6)

        # Left arrow
        arr_w, arr_h = 44, 44
        left_rect = pygame.Rect(cx - preview_size[0] // 2 - 60, sel_y - arr_h // 2, arr_w, arr_h)
        right_rect = pygame.Rect(cx + preview_size[0] // 2 + 16, sel_y - arr_h // 2, arr_w, arr_h)
        for rect, symbol in [(left_rect, "<"), (right_rect, ">")]:
            color = CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR
            pygame.draw.rect(self.screen, color, rect, border_radius=8)
            sym = self.title_font.render(symbol, True, TEXT_COLOR)
            self.screen.blit(sym, sym.get_rect(center=rect.center))

        # Theme counter e.g. "2 / 5"
        if self.card_back_themes:
            idx = self.card_back_themes.index(self.selected_card_theme) if self.selected_card_theme in self.card_back_themes else 0
            counter = self.small_font.render(f"{idx + 1} / {len(self.card_back_themes)}", True, PLACEHOLDER_COLOR)
            self.screen.blit(counter, counter.get_rect(center=(cx, sel_y + preview_size[1] // 2 + 50)))


        back_rect = pygame.Rect(10, SCREEN_HEIGHT - 50, 150, 36)
        color = BUTTON_HOVER_COLOR if back_rect.collidepoint(mouse_pos) else BUTTON_COLOR
        pygame.draw.rect(self.screen, color, back_rect, border_radius=8)
        txt = self.small_font.render("Back to Menu", True, TEXT_COLOR)
        self.screen.blit(txt, txt.get_rect(center=back_rect.center))
        credits_rect = pygame.Rect(SCREEN_WIDTH - 160, SCREEN_HEIGHT - 50, 150, 36)
        color = BUTTON_HOVER_COLOR if credits_rect.collidepoint(mouse_pos) else BUTTON_COLOR
        pygame.draw.rect(self.screen, color, credits_rect, border_radius=8)
        self.screen.blit(self.small_font.render("Credits", True, TEXT_COLOR),
                         self.small_font.render("Credits", True, TEXT_COLOR).get_rect(center=credits_rect.center))

    def render_lobby(self, background_path: str, player_name: str, room_name_field: InputField,
                     create_btn: UIElement, refresh_btn: UIElement, disconnect_btn: UIElement,
                     rooms_list: List[Dict], waiting_message: Optional[str], rules_state: Dict[str, bool], server_ip: str = None) -> None:
        try:
            bg = pygame.image.load(background_path)
            if bg.get_size() != (SCREEN_WIDTH, SCREEN_HEIGHT):
                bg = pygame.transform.scale(bg, (SCREEN_WIDTH, SCREEN_HEIGHT))
            self.background = bg
        except Exception:
            # keep existing background if loading/scaling fails
            pass
        # clear screen and draw current background (ensures no uncovered areas)
        self._draw_background()

        title = self.title_font.render(f"Playing as: {player_name}", True, TEXT_COLOR)
        self.screen.blit(title, (50, 20))
        # Show server info next to player info; show friendly name for public server
        if server_ip:
            if server_ip == "158.101.177.217":
                server_text = self.title_font.render("on a Public server", True, TEXT_COLOR)
            else:
                server_text = self.title_font.render(f"on server: {server_ip}", True, TEXT_COLOR)
            self.screen.blit(server_text, (50 + title.get_width() + 12, 20))

        mouse_pos = pygame.mouse.get_pos()
        create_btn.draw(self.screen, mouse_pos)
        refresh_btn.draw(self.screen, mouse_pos)

        self._render_room_list(rooms_list)
        disconnect_btn.draw(self.screen, mouse_pos)

        if waiting_message:
            if "error" in waiting_message.lower():
                msg_color = ERROR_COLOR
            elif "joined" in waiting_message.lower():
                msg_color = SUCCESS_COLOR
            else:
                msg_color = TEXT_COLOR
            msg_surface = self.font.render(waiting_message, True, msg_color)
            self.screen.blit(msg_surface, msg_surface.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT - 100)))

    def render_create_room(self, input_fields: Dict[str, 'InputField'], ui_elements: Dict[str, UIElement],
                           selected_max_players: int, rules: Dict[str, bool], room_is_private: bool,
                           mouse_pos: Tuple[int, int], waiting_message: Optional[str]) -> None:
        self._draw_background()
        overlay = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 170))
        self.screen.blit(overlay, (0, 0))

        dlg_w, dlg_h = 620, 610
        dlg_x = SCREEN_WIDTH // 2 - dlg_w // 2
        dlg_y = SCREEN_HEIGHT // 2 - dlg_h // 2
        dlg_rect = pygame.Rect(dlg_x, dlg_y, dlg_w, dlg_h)
        pygame.draw.rect(self.screen, (30, 30, 30), dlg_rect, border_radius=10)
        pygame.draw.rect(self.screen, (70, 70, 70), dlg_rect, 1, border_radius=10)

        title = self.title_font.render("Create Room", True, TEXT_COLOR)
        self.screen.blit(title, title.get_rect(center=(dlg_x + dlg_w // 2, dlg_y + 28)))

        pygame.draw.line(self.screen, (70, 70, 70), (dlg_x + 20, dlg_y + 50), (dlg_x + dlg_w - 20, dlg_y + 50))

        room_name_label = self.font.render("Názov miestnosti:", True, TEXT_COLOR)
        self.screen.blit(room_name_label, (dlg_x + 30, dlg_y + 65))
        rn_field = input_fields.get("room_name")
        old_rn_pos = rn_field.rect.topleft
        rn_field.rect = pygame.Rect(dlg_x + 30, dlg_y + 92, dlg_w - 60, 36)
        rn_field.draw(self.screen)

        pc_label = self.font.render("Počet hráčov:", True, TEXT_COLOR)
        self.screen.blit(pc_label, (dlg_x + 30, dlg_y + 148))
        for i, val in enumerate([2, 3, 4]):
            rect = pygame.Rect(dlg_x + 240 + i * 56, dlg_y + 145, 46, 30)
            is_selected = selected_max_players == val
            color = (0, 180, 0) if is_selected else (
                CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR)
            pygame.draw.rect(self.screen, color, rect, border_radius=6)
            label = self.small_font.render(str(val), True, TEXT_COLOR)
            self.screen.blit(label, label.get_rect(center=rect.center))

        pygame.draw.line(self.screen, (70, 70, 70), (dlg_x + 20, dlg_y + 192), (dlg_x + dlg_w - 20, dlg_y + 192))
        rules_title = self.font.render("Pravidlá / Nastavenia:", True, TEXT_COLOR)
        self.screen.blit(rules_title, (dlg_x + 30, dlg_y + 200))
        rule_labels = {
            "stack_sevens": "Prebíjanie sedmy sedmou",
            "zeleny_niznik_prebija_sedmu": "Zelený dolník prebíja sedmu",
            "stack_aces": "Prebíjanie esa esom",
            "play_multiple_cards": "Hranie viacerých rovnakých kariet naraz",
            "hornik_changes_suit": "Horník mení farbu",
        }
        # Tournament mode is rendered separately below the rules
        y = dlg_y + 228
        for key, text in rule_labels.items():
            chk_rect = pygame.Rect(dlg_x + 30, y + 1, 16, 16)
            color = (0, 180, 0) if rules.get(key, False) else (180, 0, 0)
            pygame.draw.rect(self.screen, color, chk_rect, border_radius=4)
            label = self.small_font.render(text, True, TEXT_COLOR)
            self.screen.blit(label, (dlg_x + 54, y))
            y += 30

        pygame.draw.line(self.screen, (70, 70, 70), (dlg_x + 20, dlg_y + 392), (dlg_x + dlg_w - 20, dlg_y + 392))

        # Tournament mode checkbox
        tourn_chk = pygame.Rect(dlg_x + 30, dlg_y + 398, 16, 16)
        tourn_on = rules.get("tournament_mode", False)
        pygame.draw.rect(self.screen, (0, 180, 0) if tourn_on else (180, 0, 0), tourn_chk, border_radius=4)
        tourn_label = self.small_font.render("Turnajový mód (každé kolo -1 karta pre posledného)", True, TEXT_COLOR)
        self.screen.blit(tourn_label, (dlg_x + 54, dlg_y + 397))

        pygame.draw.line(self.screen, (70, 70, 70), (dlg_x + 20, dlg_y + 422), (dlg_x + dlg_w - 20, dlg_y + 422))

        privacy_chk = pygame.Rect(dlg_x + 30, dlg_y + 434, 18, 18)
        pygame.draw.rect(self.screen, (0, 180, 0) if room_is_private else (120, 120, 120), privacy_chk, border_radius=4)
        privacy_label = self.font.render("Súkromná miestnosť (vyžaduje heslo)", True, TEXT_COLOR)
        self.screen.blit(privacy_label, (dlg_x + 56, dlg_y + 433))

        pw_field = input_fields.get("room_password")
        old_pw_pos = pw_field.rect.topleft
        pw_row_y = dlg_y + 466
        if room_is_private:
            pw_label = self.small_font.render("Heslo:", True, TEXT_COLOR)
            self.screen.blit(pw_label, (dlg_x + 30, pw_row_y))
            pw_field.rect = pygame.Rect(dlg_x + 30, pw_row_y + 20, dlg_w - 60, 36)
            pw_field.draw(self.screen)
        else:
            disabled = self.small_font.render("(zaškrtnite políčko pre zadanie hesla)", True, PLACEHOLDER_COLOR)
            self.screen.blit(disabled, (dlg_x + 30, pw_row_y + 8))

        btn_y = dlg_y + dlg_h - 58
        start_btn = ui_elements.get("start_room")
        cancel_btn = ui_elements.get("cancel_create")
        start_btn.rect = pygame.Rect(dlg_x + dlg_w - 260, btn_y, 110, 38)
        cancel_btn.rect = pygame.Rect(dlg_x + dlg_w - 135, btn_y, 110, 38)
        start_btn.draw(self.screen, mouse_pos)
        cancel_btn.draw(self.screen, mouse_pos)

        if waiting_message:
            msg_color = ERROR_COLOR if "error" in waiting_message.lower() else SUCCESS_COLOR
            msg = self.small_font.render(waiting_message, True, msg_color)
            self.screen.blit(msg, (dlg_x + 30, btn_y + 8))

        try:
            rn_field.rect.topleft = old_rn_pos
            pw_field.rect.topleft = old_pw_pos
        except Exception:
            pass

    def _render_room_list(self, rooms_list: List[Dict]) -> None:
        list_title = self.font.render("Available Rooms (click to join):", True, TEXT_COLOR)
        self.screen.blit(list_title, (300, 120))

        rooms_list_rect = pygame.Rect(300, 150, SCREEN_WIDTH - 350, SCREEN_HEIGHT - 200)
        room_item_height = 80
        pygame.draw.rect(self.screen, ROOM_ITEM_COLOR, rooms_list_rect)

        if not rooms_list:
            no_rooms_text = self.font.render("No rooms available. Create one!", True, PLACEHOLDER_COLOR)
            self.screen.blit(no_rooms_text, no_rooms_text.get_rect(center=rooms_list_rect.center))
            return

        mouse_pos = pygame.mouse.get_pos()
        details_color = (200, 200, 200)

        for i, room in enumerate(rooms_list):
            room_rect = pygame.Rect(
                rooms_list_rect.x + 10, rooms_list_rect.y + i * room_item_height + 5,
                rooms_list_rect.width - 20, room_item_height - 5
            )

            can_join = not room.get("in_game", False) and room.get("players", 0) < room.get("max_players", 4)
            if can_join:
                color = ROOM_ITEM_HOVER_COLOR if room_rect.collidepoint(mouse_pos) else ROOM_ITEM_COLOR
            else:
                color = (60, 60, 60)
            pygame.draw.rect(self.screen, color, room_rect)

            room_name = room.get("room_name", "Unknown")
            if room.get("is_private"):
                room_name = "🔒 " + room_name
            title_text = self.font.render(room_name, True, TEXT_COLOR)
            self.screen.blit(title_text, (room_rect.x + 10, room_rect.y + 8))

            creator_surface = self.small_font.render(f"by {room.get('creator', 'Unknown')}", True, details_color)
            self.screen.blit(creator_surface, (room_rect.x + 10, room_rect.y + 32))

            players_text = f"{room.get('players', 0)}/{room.get('max_players', 4)} players"
            if room.get("in_game", False):
                players_text += " (IN GAME)"
            players_surface = self.small_font.render(players_text, True, details_color)
            self.screen.blit(players_surface, (room_rect.x + 10, room_rect.y + 50))

    def render_join_password(self, input_fields: Dict[str, 'InputField'],
                             ui_elements: Dict[str, UIElement],
                             mouse_pos: Tuple[int, int]) -> None:
        """
        Draw the join-password modal with a noticeably dark overlay and
        well-spaced buttons so they do not overlap.

        Layout (modal 440 x 240):
          - Title at top
          - Password field below title
          - [Join] and [Cancel] on separate rows with 14 px gap
        """
        # --- Darker overlay (alpha 210 instead of 160) ---
        overlay = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 210))
        self.screen.blit(overlay, (0, 0))

        # --- Dialog box ---
        w, h = 440, 240
        x = SCREEN_WIDTH // 2 - w // 2
        y = SCREEN_HEIGHT // 2 - h // 2
        dlg_rect = pygame.Rect(x, y, w, h)
        pygame.draw.rect(self.screen, (35, 35, 45), dlg_rect, border_radius=10)
        pygame.draw.rect(self.screen, (80, 80, 100), dlg_rect, 2, border_radius=10)

        # Title
        title = self.title_font.render("Enter room password", True, TEXT_COLOR)
        self.screen.blit(title, title.get_rect(center=(x + w // 2, y + 30)))

        # Password input field
        pw = input_fields["room_password"]
        old_rect = pygame.Rect(pw.rect)
        pw.rect = pygame.Rect(x + 30, y + 65, w - 60, 36)
        pw.draw(self.screen)
        pw.rect = old_rect  # restore immediately

        # Buttons — stacked vertically with a gap so they never overlap
        btn_w, btn_h = w - 60, 38
        btn_x = x + 30
        join_y  = y + h - 100   # upper button
        cancel_y = y + h - 52   # lower button (38 px height + 10 px gap)

        join_rect   = pygame.Rect(btn_x, join_y,   btn_w, btn_h)
        cancel_rect = pygame.Rect(btn_x, cancel_y, btn_w, btn_h)

        join_color   = (0, 180, 0)   if join_rect.collidepoint(mouse_pos)   else (0, 130, 0)
        cancel_color = (180, 50, 50) if cancel_rect.collidepoint(mouse_pos) else (130, 30, 30)

        pygame.draw.rect(self.screen, join_color,   join_rect,   border_radius=6)
        pygame.draw.rect(self.screen, cancel_color, cancel_rect, border_radius=6)

        join_lbl   = self.font.render("Join", True, TEXT_COLOR)
        cancel_lbl = self.font.render("Cancel",      True, TEXT_COLOR)
        self.screen.blit(join_lbl,   join_lbl.get_rect(center=join_rect.center))
        self.screen.blit(cancel_lbl, cancel_lbl.get_rect(center=cancel_rect.center))

        # Store button rects on ui_elements so the click handler can use them
        ui_elements["join_room_btn"].rect  = join_rect
        ui_elements["cancel_join"].rect    = cancel_rect

    def render_credits(self, mouse_pos: Tuple[int, int]) -> None:
        self._draw_background()
        title = self.title_font.render("Credits & Asset Sources", True, TEXT_COLOR)
        self.screen.blit(title, title.get_rect(center=(SCREEN_WIDTH // 2, 80)))

        entries = [
            ("Cards",        "Saxonian pattern — Wikimedia Commons",
             "https://commons.wikimedia.org/wiki/Category:Saxon_pattern"),
            ("Suit symbols", "German-suited playing cards — Wikipedia",
             "https://en.wikipedia.org/wiki/German-suited_playing_cards"),
            ("Backgrounds",  "Generated with AI", ""),
            ("Card backs",   "Generated with AI, Freepik",
             ["https://www.freepik.com"]),
        ]
        y = 200
        for category, description, url in entries:
            cat_surf = self.font.render(category + ":", True, HIGHLIGHT_COLOR)
            self.screen.blit(cat_surf, cat_surf.get_rect(midright=(SCREEN_WIDTH // 2 - 20, y)))
            desc_surf = self.font.render(description, True, TEXT_COLOR)
            self.screen.blit(desc_surf, desc_surf.get_rect(midleft=(SCREEN_WIDTH // 2 - 10, y)))
            urls = url if isinstance(url, list) else ([url] if url else [])
            for ui, u in enumerate(urls):
                url_rect = pygame.Rect(SCREEN_WIDTH // 2 - 10, y + 28 + ui * 24, 700, 22)
                url_color = HIGHLIGHT_COLOR if url_rect.collidepoint(mouse_pos) else PLACEHOLDER_COLOR
                url_surf = self.small_font.render(u, True, url_color)
                self.screen.blit(url_surf, (SCREEN_WIDTH // 2 - 10, y + 28 + ui * 24))
                if url_rect.collidepoint(mouse_pos):
                    pygame.draw.line(self.screen, url_color,
                                     (url_rect.x, url_rect.bottom),
                                     (url_rect.x + url_surf.get_width(), url_rect.bottom), 1)
            y += 100

        back_rect = pygame.Rect(10, SCREEN_HEIGHT - 50, 150, 36)
        color = BUTTON_HOVER_COLOR if back_rect.collidepoint(mouse_pos) else BUTTON_COLOR
        pygame.draw.rect(self.screen, color, back_rect, border_radius=8)
        self.screen.blit(self.small_font.render("Back", True, TEXT_COLOR),
                         self.small_font.render("Back", True, TEXT_COLOR).get_rect(center=back_rect.center))
        hint = self.small_font.render("Click a link to copy to clipboard", True, PLACEHOLDER_COLOR)
        self.screen.blit(hint, hint.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT - 40)))

        # Custom assets info
        info_lines = [
            "Custom backgrounds (1280x720 px) go in:  assets/backgrounds/",
            "Custom card backs (80x142 px) go in:  assets/cards/card_backs/",
        ]
        iy = SCREEN_HEIGHT - 130
        sep = self.small_font.render("─" * 80, True, PLACEHOLDER_COLOR)
        self.screen.blit(sep, sep.get_rect(center=(SCREEN_WIDTH // 2, iy - 14)))
        for line in info_lines:
            s = self.small_font.render(line, True, TEXT_COLOR)
            self.screen.blit(s, s.get_rect(center=(SCREEN_WIDTH // 2, iy)))
            iy += 26

    def render_game(self, state_manager: StateManager, card_sprites: Dict[int, pygame.sprite.Group],
                    current_room_name: str, mouse_pos: Tuple[int, int],
                    waiting_message: Optional[str], leave_btn: UIElement, end_turn_btn: UIElement) -> None:
        self._draw_background()

        if current_room_name:
            room_info = self.title_font.render(f"Room: {current_room_name}", True, TEXT_COLOR)
            self.screen.blit(room_info, (10, 10))

        if waiting_message:
            msg_surface = self.font.render(waiting_message, True, TEXT_COLOR)
            self.screen.blit(msg_surface, (10, 50))

        leave_btn.draw(self.screen, mouse_pos)

        if state_manager.state == "room_waiting":
            if state_manager.waiting_message:
                wait_text = self.title_font.render(state_manager.waiting_message, True, TEXT_COLOR)
                self.screen.blit(wait_text, wait_text.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 100)))

            # (Players in room title rendered inside the split-column block below)

            # Split screen: players left, rules right
            col_left = SCREEN_WIDTH // 3
            col_right = SCREEN_WIDTH * 2 // 3

            if state_manager.player_names:
                y = SCREEN_HEIGHT // 2 + 30
                for slot, name in sorted(state_manager.player_names.items()):
                    color = HIGHLIGHT_COLOR if slot == state_manager.local_player else TEXT_COLOR
                    player_text = self.font.render(f"{name}", True, color)
                    self.screen.blit(player_text, player_text.get_rect(center=(col_left, y)))
                    y += 40
                if getattr(state_manager, 'current_room_password', None):
                    pw_surf = self.small_font.render(f"Password: {state_manager.current_room_password}", True,
                                                     TEXT_COLOR)
                    self.screen.blit(pw_surf, pw_surf.get_rect(center=(col_left, y + 20)))

            # Rules panel on the right
            rules = getattr(state_manager, "room_rules", {})
            if rules:
                rule_label_map = [
                    ("stack_sevens",              "Prebíjanie sedmy sedmou"),
                    ("zeleny_niznik_prebija_sedmu","Zelený dolník prebíja sedmu"),
                    ("stack_aces",                "Prebíjanie esa esom"),
                    ("play_multiple_cards",        "Hranie viacerých kariet naraz"),
                    ("hornik_changes_suit",        "Horník mení farbu"),
                    ("tournament_mode",            "Turnajový mód"),
                ]
                rules_title = self.font.render("Pravidlá:", True, TEXT_COLOR)
                self.screen.blit(rules_title, rules_title.get_rect(center=(col_right, SCREEN_HEIGHT // 2 - 20)))
                ry = SCREEN_HEIGHT // 2 + 30
                for key, label in rule_label_map:
                    val = rules.get(key, False)
                    box_color = (60, 180, 60) if val else (180, 50, 50)
                    box_rect = pygame.Rect(0, 0, 18, 18)
                    box_rect.centery = ry
                    box_rect.right = col_right - 160
                    pygame.draw.rect(self.screen, box_color, box_rect, border_radius=3)
                    rule_surf = self.font.render(label, True, TEXT_COLOR)
                    self.screen.blit(rule_surf, rule_surf.get_rect(midleft=(box_rect.right + 10, ry)))
                    ry += 38

            # Move players title to match left column
            players_title2 = self.font.render("Players in room:", True, TEXT_COLOR)
            self.screen.blit(players_title2, players_title2.get_rect(center=(col_left, SCREEN_HEIGHT // 2 - 20)))

        elif state_manager.state == "playing" and state_manager.game_state and state_manager.local_player is not None:
            current_player = state_manager.game_state.get("current_player", 0)
            player_names = state_manager.game_state.get("player_names", {})

            for i in range(state_manager.num_players):
                if card_sprites.get(i):
                    card_sprites[i].draw(self.screen)
                    if i == state_manager.local_player and current_player == state_manager.local_player:
                        hovered = None
                        for sprite in card_sprites[i]:
                            if sprite.rect.collidepoint(mouse_pos):
                                hovered = sprite  # last match = topmost visible card
                        if hovered:
                            pygame.draw.rect(self.screen, HIGHLIGHT_COLOR, hovered.rect, CARD_HIGHLIGHT_THICKNESS)

            draw_pile_rect = self.layout.draw_pile_rect
            if draw_pile_rect.collidepoint(mouse_pos):
                pygame.draw.rect(self.screen, HIGHLIGHT_COLOR, draw_pile_rect, CARD_HIGHLIGHT_THICKNESS)

            if state_manager.game_state.get("draw_pile_count", 0) > 0:
                self.screen.blit(self.card_back, (draw_pile_rect.x + 3, draw_pile_rect.y + 3))

            if state_manager.game_state.get("discard_pile") and not getattr(state_manager, 'dealing_animation', False):
                top = state_manager.game_state["discard_pile"][-1]
                card = Card(top["name"], top["value"], top["suit"])
                card.draw(self.screen, *self.layout.discard_pile_pos)

                chosen = state_manager.game_state.get("chosen_suit")
                if chosen:
                    img = getattr(self, 'suit_images', {}).get(chosen)
                    dx, dy = self.layout.discard_pile_pos
                    if img:
                        small = pygame.transform.smoothscale(img, (36, 36))
                        self.screen.blit(small, (dx + CARD_WIDTH + 8, dy))
                    else:
                        colors_map = {"srdce": (200, 50, 50), "zelen": (50, 200, 50), "zalud": (150, 100, 50),
                                      "gula": (200, 200, 50)}
                        pygame.draw.circle(self.screen, colors_map.get(chosen, (120, 120, 120)),
                                           (dx + CARD_WIDTH + 26, dy + CARD_HEIGHT // 2), 18)

            for i in range(state_manager.num_players):
                # Skip slots with no player (empty seats)
                if i not in player_names:
                    continue
                pos_index = (i - state_manager.local_player) % state_manager.num_players
                name_pos = self.layout.name_positions[pos_index]
                name_color = HIGHLIGHT_COLOR if i == current_player else TEXT_COLOR
                player_name = player_names.get(i, f"Unknown ({i + 1})")
                name_text = self.font.render(player_name, True, name_color)
                rotated_name = pygame.transform.rotate(name_text, name_pos["angle"])
                self.screen.blit(rotated_name, rotated_name.get_rect(center=(name_pos["x"], name_pos["y"])))

            if current_player == state_manager.local_player:
                end_turn_btn.draw(self.screen, mouse_pos)

                seven_penalty = state_manager.game_state.get("seven_penalty_count", 0)
                ace_penalty = state_manager.game_state.get("ace_penalty_active", False)
                cards_played = state_manager.game_state.get("cards_played_this_turn", 0)

                if seven_penalty > 0 and cards_played == 0:
                    msg = f"Ťahaj {seven_penalty} kariet alebo prebi!"
                    msg_surf = self.title_font.render(msg, True, ERROR_COLOR)
                    self.screen.blit(msg_surf, msg_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 100)))
                elif ace_penalty and cards_played == 0:
                    msg = "Stojíš! Prebi eso alebo preskoč ťah."
                    msg_surf = self.title_font.render(msg, True, ERROR_COLOR)
                    self.screen.blit(msg_surf, msg_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 100)))

            if getattr(state_manager, 'suit_picker_active', False):
                picker_rect = pygame.Rect(SCREEN_WIDTH // 2 - 150, SCREEN_HEIGHT // 2 - 60, 300, 120)
                pygame.draw.rect(self.screen, (50, 50, 50), picker_rect, border_radius=10)
                title = self.small_font.render("Vyber farbu:", True, TEXT_COLOR)
                self.screen.blit(title, title.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 40)))
                suits = ["srdce", "zelen", "zalud", "gula"]
                colors = [(200, 50, 50), (50, 200, 50), (150, 100, 50), (200, 200, 50)]
                for i, suit in enumerate(suits):
                    btn_rect = pygame.Rect(SCREEN_WIDTH // 2 - 120 + i * 60, SCREEN_HEIGHT // 2, 50, 40)
                    img = getattr(self, 'suit_images', {}).get(suit)
                    if img:
                        try:
                            scaled = pygame.transform.smoothscale(img, (btn_rect.width, btn_rect.height))
                            self.screen.blit(scaled, btn_rect.topleft)
                        except Exception:
                            pygame.draw.rect(self.screen, colors[i], btn_rect, border_radius=5)
                    else:
                        pygame.draw.rect(self.screen, colors[i], btn_rect, border_radius=5)

    def render_leaderboard(self, state_manager: StateManager, mouse_pos: Tuple[int, int],
                           leave_btn: UIElement) -> None:
        self._draw_background()

        is_tournament_round = getattr(state_manager, 'tournament_round_over', False)

        if is_tournament_round:
            title_text = f"Round {getattr(state_manager, 'tournament_round', 1)} Over"
            sub_text = "Next round starting soon..."
        else:
            title_text = "Game Over"
            sub_text = None

        title = self.title_font.render(title_text, True, TEXT_COLOR)
        self.screen.blit(title, title.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 4)))

        if sub_text:
            sub = self.small_font.render(sub_text, True, PLACEHOLDER_COLOR)
            self.screen.blit(sub, sub.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 4 + 40)))

        data = (getattr(state_manager, 'tournament_results', None)
                if is_tournament_round else state_manager.leaderboard_data)

        if data:
            penalties = getattr(state_manager, 'tournament_penalties', {})
            for i, entry in enumerate(data):
                player_id = entry.get("pid", 0)
                rank = entry.get("rank", i + 1)
                player_name = state_manager.player_names.get(player_id, f"Unknown ({player_id + 1})")
                disconnected = entry.get("disconnected", False)
                is_local = (player_id == state_manager.local_player)
                eliminated = entry.get("eliminated", False)
                cards_next = entry.get("cards_next", None)

                if eliminated:
                    color = (150, 150, 150)
                    suffix = " — eliminated"
                elif disconnected:
                    color = (150, 150, 150)
                    suffix = " (disconnected)"
                elif is_local:
                    color = ERROR_COLOR
                    suffix = ""
                else:
                    color = TEXT_COLOR
                    suffix = ""

                if is_tournament_round and cards_next is not None and not eliminated:
                    suffix += f"  →  {cards_next} card(s) next round"

                text = f"{rank}. {player_name}{suffix}"
                text_surface = self.font.render(text, True, color)
                self.screen.blit(text_surface,
                                 text_surface.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 3 + (i + 1) * 50)))

        if not is_tournament_round:
            leave_btn.draw(self.screen, mouse_pos)


class EventHandler:
    """Handles all user input events and game logic responses."""

    def __init__(self, network: NetworkManager, state_manager: StateManager, renderer: Renderer,
                 layout: LayoutManager, input_fields: Dict[str, InputField],
                 ui_elements: Dict[str, UIElement], lan_server: LanServerManager):
        self.network = network
        self.state_manager = state_manager
        self.renderer = renderer
        self.layout = layout
        self.input_fields = input_fields
        self.ui_elements = ui_elements
        self.lan_server = lan_server
        self.last_click_time: int = 0
        self.card_sprites: Dict[int, pygame.sprite.Group] = {i: pygame.sprite.Group() for i in range(4)}
        self.card_cache: Dict[str, Card] = {}
        self.player_name = ""
        self.name_set = False
        self.current_room_id = None
        self.current_room_name = ""
        self.rooms_list: List[Dict] = []
        self.selected_max_players: int = 4
        self.renderer.selected_room_max_players = self.selected_max_players

        self.rules: Dict[str, bool] = {
            "stack_sevens": True,
            "zeleny_niznik_prebija_sedmu": True,
            "stack_aces": True,
            "play_multiple_cards": True,
            "hornik_changes_suit": True,
            "tournament_mode": False
        }
        self.room_is_private: bool = False
        self.pending_join_room_id = None
        self.current_room_password = None
        self.is_leader: bool = False
        self.animation_manager: AnimationManager = AnimationManager()
        self._prev_hand_sizes: Dict[int, int] = {}
        # Stores (card, rect_x, rect_y, angle) of card waiting for server confirmation
        self._pending_play: Optional[Tuple] = None
        # Re-render sprites each time a deal animation lands (reveals the card)
        self.animation_manager.on_card_land = lambda pi, ci: self.update_card_sprites()

    @staticmethod
    def validate_ip(ip: str) -> bool:
        if ip.lower() in ('localhost', '127.0.0.1'):
            return True
        return bool(re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip))

    def handle_click(self, pos: Tuple[int, int]) -> None:
        current_time = pygame.time.get_ticks()
        if current_time - self.last_click_time < CLICK_DEBOUNCE_MS:
            return
        self.last_click_time = current_time

        state = self.state_manager.state
        handlers = {
            "menu": self._handle_menu_click,
            "customize": self._handle_customize_click,
            "credits": self._handle_credits_click,
            "lobby": self._handle_lobby_click,
            "create_room": self._handle_create_room_click,
            "join_password": self._handle_join_password_click,
            "room_waiting": self._handle_room_waiting_click,
            "playing": self._handle_game_click,
            "leaderboard": self._handle_leaderboard_click,
            "public_menu": self._handle_public_menu_click,
        }
        handler = handlers.get(state)
        if handler:
            handler(pos)

    def _handle_menu_click(self, pos: Tuple[int, int]) -> None:
        ip_field = self.input_fields["ip"]
        name_field = self.input_fields["name"]

        if ip_field.rect.collidepoint(pos):
            ip_field.active = True
            name_field.active = False
        elif name_field.rect.collidepoint(pos):
            name_field.active = True
            ip_field.active = False
        else:
            ip_field.active = False
            name_field.active = False

        if self.ui_elements["connect"].rect.collidepoint(pos):
            self._handle_connect()
        elif self.ui_elements["close"].rect.collidepoint(pos):
            raise SystemExit(0)
        elif self.ui_elements["customize"].rect.collidepoint(pos):
            self.state_manager.previous_state = self.state_manager.state
            self.state_manager.state = "customize"
        elif self.ui_elements["lan_server"].rect.collidepoint(pos):
            self._toggle_lan_server()
        elif self.ui_elements["public_server"].rect.collidepoint(pos):
            # Open Public Server screen instead of immediate connect
            # If LAN server is running, stop it so public lobby doesn't conflict
            if getattr(self, 'lan_server', None) and self.lan_server.running:
                self._toggle_lan_server()
            self.state_manager.state = "public_menu"
            # Clear any leftover waiting message (e.g., "Server disconnected.") when
            # switching to the Public server screen so it doesn't persist there.
            self.state_manager.waiting_message = None

    def _handle_public_menu_click(self, pos: Tuple[int, int]) -> None:
        name_field = self.input_fields["name"]

        # Name field activation
        if name_field.rect.collidepoint(pos):
            name_field.active = True
        else:
            name_field.active = False

        # Buttons
        if self.ui_elements["connect"].rect.collidepoint(pos):
            # Attempt to connect to the public server
            self._connect_public_server()
            return
        if self.ui_elements["close"].rect.collidepoint(pos):
            raise SystemExit(0)
        if self.ui_elements["customize"].rect.collidepoint(pos):
            self.state_manager.previous_state = self.state_manager.state
            self.state_manager.state = "customize"
            return
        if self.ui_elements.get("back_to_lan") and self.ui_elements["back_to_lan"].rect.collidepoint(pos):
            # Return to normal menu where LAN controls are shown
            # Clear IP input so LAN menu starts with empty IP field
            self.input_fields["ip"].text = ""
            self.state_manager.server_ip = None
            self.state_manager.state = "menu"
            return

    def _toggle_lan_server(self) -> None:
        btn = self.ui_elements["lan_server"]
        if self.lan_server.running:
            self.lan_server.stop()
            btn.update_text("Start LAN Server")
            btn.bg_color = LAN_INACTIVE_COLOR
            self.state_manager.waiting_message = None
        else:
            if self.lan_server.start():
                btn.update_text("Stop LAN Server")
                btn.bg_color = LAN_ACTIVE_COLOR
                self.state_manager.waiting_message = None
                self.input_fields["ip"].text = self.lan_server.local_ip
            else:
                self.state_manager.waiting_message = None

    def _connect_public_server(self) -> None:
        self.input_fields["ip"].text = "158.101.177.217"
        name = self.input_fields["name"].text.strip()
        if not name:
            self.state_manager.waiting_message = "Please enter your name first"
            return
        if not (3 <= len(name) <= 20):
            self.state_manager.waiting_message = "Name must be 3-20 characters"
            return
        self._handle_connect()

    def _handle_credits_click(self, pos: Tuple[int, int]) -> None:
        back_rect = pygame.Rect(10, SCREEN_HEIGHT - 50, 150, 36)
        if back_rect.collidepoint(pos):
            self.state_manager.state = "customize"
            return
        # (category, urls_list, base_y)
        clickable = [
            (200, ["https://commons.wikimedia.org/wiki/Category:Saxon_pattern"]),
            (300, ["https://en.wikipedia.org/wiki/German-suited_playing_cards"]),
            (400, []),
            (500, ["https://www.freepik.com"]),
        ]
        for base_y, urls in clickable:
            for ui, url in enumerate(urls):
                url_rect = pygame.Rect(SCREEN_WIDTH // 2 - 10, base_y + 28 + ui * 24, 700, 22)
                if url_rect.collidepoint(pos):
                    try:
                        import subprocess
                        if sys.platform == "win32":
                            subprocess.run("clip", input=url.encode(), check=True, shell=True)
                        elif sys.platform == "darwin":
                            subprocess.run("pbcopy", input=url.encode(), check=True)
                        else:
                            subprocess.run(["xclip", "-selection", "clipboard"],
                                           input=url.encode(), check=True)
                        self.state_manager.waiting_message = None
                    except Exception:
                        self.state_manager.waiting_message = None
                    return

    def _handle_customize_click(self, pos: Tuple[int, int]) -> None:
        global SCREEN_WIDTH, SCREEN_HEIGHT
        # Arrow selector for background
        bgs = self.renderer.background_options
        if bgs:
            bcx = SCREEN_WIDTH // 4
            bsel_y = 290
            bleft_rect  = pygame.Rect(bcx - 80, bsel_y - 22, 44, 44)
            bright_rect = pygame.Rect(bcx + 36, bsel_y - 22, 44, 44)
            cur_bg = self.renderer.selected_background
            bidx = bgs.index(cur_bg) if cur_bg in bgs else 0
            if bleft_rect.collidepoint(pos):
                bidx = (bidx - 1) % len(bgs)
            elif bright_rect.collidepoint(pos):
                bidx = (bidx + 1) % len(bgs)
            else:
                bidx = None
            if bidx is not None:
                self.renderer.selected_background = bgs[bidx]
                self.renderer.current_background_path = f"assets/backgrounds/{bgs[bidx]}"
                self.renderer.load_assets(
                    self.renderer.current_background_path,
                    self.renderer.current_card_back_path,
                    (CARD_WIDTH, CARD_HEIGHT)
                )
                return

        # Arrow selector for card back
        themes = self.renderer.card_back_themes
        if themes:
            cx = SCREEN_WIDTH * 3 // 4
            sel_y = 290
            preview_size = (int(CARD_WIDTH * 1.4), int(CARD_HEIGHT * 1.4))
            left_rect = pygame.Rect(cx - preview_size[0] // 2 - 60, sel_y - 22, 44, 44)
            right_rect = pygame.Rect(cx + preview_size[0] // 2 + 16, sel_y - 22, 44, 44)
            cur_idx = themes.index(self.renderer.selected_card_theme) if self.renderer.selected_card_theme in themes else 0
            if left_rect.collidepoint(pos):
                cur_idx = (cur_idx - 1) % len(themes)
            elif right_rect.collidepoint(pos):
                cur_idx = (cur_idx + 1) % len(themes)
            else:
                cur_idx = None
            if cur_idx is not None:
                self.renderer.selected_card_theme = themes[cur_idx]
                self.renderer.current_card_back_path = os.path.join("assets", "cards", "card_backs", themes[cur_idx])
                self.renderer.load_assets(
                    self.renderer.current_background_path,
                    self.renderer.current_card_back_path,
                    (CARD_WIDTH, CARD_HEIGHT)
                )
                return


        back_rect = pygame.Rect(10, SCREEN_HEIGHT - 50, 150, 36)
        if back_rect.collidepoint(pos):
            # Return to the menu we came from (e.g. public_menu) if recorded,
            # otherwise fall back to the main menu.
            prev = getattr(self.state_manager, 'previous_state', None)
            if prev:
                self.state_manager.state = prev
                self.state_manager.previous_state = None
            else:
                self.state_manager.state = "menu"
        credits_rect = pygame.Rect(SCREEN_WIDTH - 160, SCREEN_HEIGHT - 50, 150, 36)
        if credits_rect.collidepoint(pos):
            self.state_manager.state = "credits"

    def _handle_lobby_click(self, pos: Tuple[int, int]) -> None:
        room_name_field = self.input_fields["room_name"]

        if room_name_field.rect.collidepoint(pos):
            room_name_field.active = True
        elif self.ui_elements["create"].rect.collidepoint(pos):
            self.state_manager.state = "create_room"
            self.input_fields["room_name"].active = True
            return
        elif self.ui_elements["refresh"].rect.collidepoint(pos):
            self.network.send_message({"t": "refresh_rooms"})
        elif self.ui_elements["disconnect"].rect.collidepoint(pos):
            self.network.disconnect()
            # If connected to the public server, return to the Public Server screen;
            # otherwise return to the normal menu.
            if getattr(self.state_manager, 'server_ip', None) == "158.101.177.217":
                self.state_manager.state = "public_menu"
            else:
                self.state_manager.state = "menu"
            self.state_manager.waiting_message = None
        else:
            room_name_field.active = False

        rooms_area = pygame.Rect(300, 150, SCREEN_WIDTH - 350, SCREEN_HEIGHT - 200)
        if rooms_area.collidepoint(pos):
            self._handle_room_list_click(pos)

    def _handle_create_room_click(self, pos: Tuple[int, int]) -> None:
        room_name_field = self.input_fields["room_name"]
        password_field = self.input_fields["room_password"]

        dlg_w, dlg_h = 620, 610
        dlg_x = SCREEN_WIDTH // 2 - dlg_w // 2
        dlg_y = SCREEN_HEIGHT // 2 - dlg_h // 2

        rn_rect = pygame.Rect(dlg_x + 30, dlg_y + 92, dlg_w - 60, 36)
        pw_row_y = dlg_y + 466
        pw_rect = pygame.Rect(dlg_x + 30, pw_row_y + 20, dlg_w - 60, 36)

        if rn_rect.collidepoint(pos):
            room_name_field.active = True
            password_field.active = False
            return
        if pw_rect.collidepoint(pos):
            password_field.active = True
            room_name_field.active = False
            return

        for i, val in enumerate([2, 3, 4]):
            rect = pygame.Rect(dlg_x + 240 + i * 56, dlg_y + 145, 46, 30)
            if rect.collidepoint(pos):
                self.selected_max_players = val
                self.renderer.selected_room_max_players = val
                return

        y = dlg_y + 228
        rule_labels = [
            "stack_sevens",
            "zeleny_niznik_prebija_sedmu",
            "stack_aces",
            "play_multiple_cards",
            "hornik_changes_suit"
        ]
        for key in rule_labels:
            rect = pygame.Rect(dlg_x + 30, y + 1, 16, 16)
            if rect.collidepoint(pos):
                self.rules[key] = not self.rules.get(key, False)
                return
            y += 30

        tourn_rect = pygame.Rect(dlg_x + 30, dlg_y + 398, 16, 16)
        if tourn_rect.collidepoint(pos):
            self.rules["tournament_mode"] = not self.rules.get("tournament_mode", False)
            return

        privacy_rect = pygame.Rect(dlg_x + 30, dlg_y + 434, 18, 18)
        if privacy_rect.collidepoint(pos):
            self.room_is_private = not getattr(self, 'room_is_private', False)
            return

        btn_y = dlg_y + dlg_h - 58
        start_rect = pygame.Rect(dlg_x + dlg_w - 260, btn_y, 110, 38)
        cancel_rect = pygame.Rect(dlg_x + dlg_w - 135, btn_y, 110, 38)

        if start_rect.collidepoint(pos):
            room_name = room_name_field.text.strip()
            if len(room_name) < 3:
                self.state_manager.waiting_message = "Room name must be at least 3 characters"
                return
            if getattr(self, 'room_is_private', False) and not password_field.text:
                self.state_manager.waiting_message = "Password required for private room"
                return

            msg = {
                "t": "create_room",
                "room_name": room_name,
                "max_players": self.selected_max_players,
                "rules": self.rules
            }
            if getattr(self, 'room_is_private', False):
                msg["is_private"] = True
                msg["password"] = password_field.text
                self.current_room_password = password_field.text

            self.network.send_message(msg)
            room_name_field.text = ""
            password_field.text = ""
            room_name_field.active = False
            password_field.active = False
            self.room_is_private = False
            self.state_manager.state = "lobby"
            return

        if cancel_rect.collidepoint(pos):
            room_name_field.active = False
            password_field.active = False
            self.room_is_private = False
            self.state_manager.state = "lobby"
            return

        room_name_field.active = False
        password_field.active = False

    def _handle_room_list_click(self, pos: Tuple[int, int]) -> None:
        room_index = (pos[1] - 150) // 80
        if 0 <= room_index < len(self.rooms_list):
            room = self.rooms_list[room_index]
            if room.get("in_game", False) or room.get("players", 0) >= room.get("max_players", 4):
                return
            if room.get("is_private"):
                self.state_manager.state = "join_password"
                self.pending_join_room_id = room.get("room_id")
                self.input_fields["room_password"].text = ""
                self.input_fields["room_password"].active = True
            else:
                self.network.send_message({"t": "join_room", "room_id": room["room_id"]})

    def _handle_room_waiting_click(self, pos: Tuple[int, int]) -> None:
        if self.ui_elements["leave_room"].rect.collidepoint(pos):
            self.network.send_message({"t": "leave_room"})
        elif self.is_leader and self.ui_elements["start_game"].rect.collidepoint(pos):
            if len(self.state_manager.player_names) >= 2:
                self.network.send_message({"t": "start_game"})

    def _handle_join_password_click(self, pos: Tuple[int, int]) -> None:
        """
        Delegate to the button rects that render_join_password() has already
        written onto the UIElements so coordinates are always in sync.
        """
        pw = self.input_fields["room_password"]
        join_rect   = self.ui_elements["join_room_btn"].rect
        cancel_rect = self.ui_elements["cancel_join"].rect

        # Approximate password field rect (same formula as in render_join_password)
        w, h = 440, 240
        x = SCREEN_WIDTH // 2 - w // 2
        y = SCREEN_HEIGHT // 2 - h // 2
        pw_rect = pygame.Rect(x + 30, y + 65, w - 60, 36)

        if pw_rect.collidepoint(pos):
            pw.active = True
            return

        if join_rect.collidepoint(pos):
            password = pw.text
            room_id = getattr(self, 'pending_join_room_id', None)
            if not room_id:
                self.state_manager.waiting_message = "No room selected"
                self.state_manager.state = "lobby"
                return
            self.network.send_message({"t": "join_room", "room_id": room_id, "password": password})
            self.current_room_password = password
            pw.text = ""
            pw.active = False
            self.state_manager.state = "lobby"
            return

        if cancel_rect.collidepoint(pos):
            pw.text = ""
            pw.active = False
            self.state_manager.state = "lobby"
            return

        pw.active = False

    def _handle_game_click(self, pos: Tuple[int, int]) -> None:
        if getattr(self.state_manager, 'suit_picker_active', False):
            suits = ["srdce", "zelen", "zalud", "gula"]
            picker_rect = pygame.Rect(SCREEN_WIDTH // 2 - 150, SCREEN_HEIGHT // 2 - 60, 300, 120)
            for i, suit in enumerate(suits):
                btn_rect = pygame.Rect(SCREEN_WIDTH // 2 - 120 + i * 60, SCREEN_HEIGHT // 2, 50, 40)
                if btn_rect.collidepoint(pos):
                    self.network.send_message(
                        {"t": "p", "ci": getattr(self.state_manager, 'suit_picker_card_index', -1), "cs": suit})
                    self.state_manager.suit_picker_active = False
                    # _pending_play stays — gs handler fires the animation on confirmation
                    return
            # Click outside picker — cancel the hornik play
            if not picker_rect.collidepoint(pos):
                self.state_manager.suit_picker_active = False
                self._pending_play = None
            return

        if self.ui_elements["leave_room"].rect.collidepoint(pos):
            self.network.send_message({"t": "leave_room"})
        elif (self.state_manager.game_state and
              self.state_manager.local_player == self.state_manager.game_state.get("current_player", -1)):

            if self.ui_elements["end_turn"].rect.collidepoint(pos):
                self.network.send_message({"t": "et"})
                return

            sprites = list(self.card_sprites[self.state_manager.local_player].sprites())
            for i, sprite in reversed(list(enumerate(sprites))):
                if sprite.rect.collidepoint(pos):
                    if sprite.card.value == 12 and self.rules.get("hornik_changes_suit", True):
                        self.state_manager.suit_picker_active = True
                        self.state_manager.suit_picker_card_index = i
                        # Store pending now; animation fires after server confirms
                        self._pending_play = (
                            sprite.card,
                            float(sprite.rect.x), float(sprite.rect.y),
                            sprite.angle
                        )
                    else:
                        # Store the card info; play animation only after server confirms
                        self._pending_play = (
                            sprite.card,
                            float(sprite.rect.x), float(sprite.rect.y),
                            sprite.angle
                        )
                        self.network.send_message({"t": "p", "ci": i})
                    return

            if self.layout.draw_pile_rect.collidepoint(pos):
                self.network.send_message({"t": "et"})

    def _handle_leaderboard_click(self, pos: Tuple[int, int]) -> None:
        if self.ui_elements["leave_room"].rect.collidepoint(pos):
            self.network.send_message({"t": "leave_room"})

    def handle_key(self, event) -> None:
        if self.state_manager.state in ("menu", "public_menu"):
            handled = self.input_fields["ip"].handle_key(event) or self.input_fields["name"].handle_key(event)
            if handled and event.key == pygame.K_RETURN:
                if self.state_manager.state == "menu":
                    self._handle_connect()
                elif self.state_manager.state == "public_menu":
                    self._connect_public_server()
        elif self.state_manager.state == "create_room":
            if self.input_fields["room_name"].handle_key(event) and event.key == pygame.K_RETURN:
                self.input_fields["room_name"].active = False
                self.input_fields["room_password"].active = True
                return
            if self.input_fields["room_password"].handle_key(event) and event.key == pygame.K_RETURN:
                room_name = self.input_fields["room_name"].text.strip()
                if len(room_name) < 3:
                    self.state_manager.waiting_message = "Room name must be at least 3 characters"
                    return
                if getattr(self, 'room_is_private', False) and not self.input_fields["room_password"].text:
                    self.state_manager.waiting_message = "Password required for private room"
                    return
                msg = {
                    "t": "create_room",
                    "room_name": room_name,
                    "max_players": self.selected_max_players,
                    "rules": self.rules
                }
                if getattr(self, 'room_is_private', False):
                    msg["private"] = True
                    msg["password"] = self.input_fields["room_password"].text
                    self.current_room_password = self.input_fields["room_password"].text
                self.network.send_message(msg)
                self.input_fields["room_name"].text = ""
                self.input_fields["room_password"].text = ""
                self.input_fields["room_name"].active = False
                self.input_fields["room_password"].active = False
                self.room_is_private = False
                self.state_manager.state = "lobby"
                return
        elif self.state_manager.state == "join_password":
            if self.input_fields["room_password"].handle_key(event) and event.key == pygame.K_RETURN:
                password = self.input_fields["room_password"].text
                room_id = getattr(self, 'pending_join_room_id', None)
                if not room_id:
                    self.state_manager.waiting_message = None
                    self.state_manager.state = "lobby"
                    return
                self.network.send_message({"t": "join_room", "room_id": room_id, "password": password})
                self.state_manager.current_room_password = password
                self.current_room_password = None
                self.input_fields["room_password"].text = ""
                self.input_fields["room_password"].active = False
                self.state_manager.state = "lobby"
                return
        elif self.state_manager.state == "lobby":
            if self.input_fields["room_name"].handle_key(event) and event.key == pygame.K_RETURN:
                self.state_manager.state = "create_room"
                self.input_fields["room_name"].active = True
                return

    def _handle_connect(self) -> None:
        ip = self.input_fields["ip"].text or "localhost"
        name = self.input_fields["name"].text.strip()

        if not self.validate_ip(ip):
            self.state_manager.waiting_message = "Invalid IP address"
            return
        if not name:
            self.state_manager.waiting_message = "Username is required"
            return
        if not (3 <= len(name) <= 20):
            self.state_manager.waiting_message = "Name must be 3-20 characters"
            return

        self.player_name = name
        if self.network.connect(ip):
            self.state_manager.waiting_message = None
            running_flag = [True]
            self.network.start_listener(running_flag, self._on_network_message)
            # remember connected server IP for lobby display
            self.state_manager.server_ip = ip
            self.network.send_message({"t": "set_name", "name": self.player_name})
        else:
            self.state_manager.waiting_message = f"Failed to connect to {ip}"

    def _create_room(self) -> None:
        room_name = self.input_fields["room_name"].text.strip()
        if len(room_name) >= 3:
            self.network.send_message({
                "t": "create_room",
                "room_name": room_name,
                "max_players": self.selected_max_players,
                "rules": self.rules
            })
            self.input_fields["room_name"].text = ""

    def _on_network_message(self, message: dict) -> None:
        self.state_manager.waiting_start = time.time()
        msg_type = message.get("t")

        if msg_type == "lobby_welcome":
            self.state_manager.waiting_message = None

        elif msg_type == "server_disconnected":
            self.is_leader = False
            self.state_manager.waiting_message = "Server disconnected."
            try:
                self.network.disconnect()
            except Exception:
                pass
            self.state_manager.state = "menu"
            self.state_manager.game_state = None
            self.current_room_id = None
            self.current_room_name = ""
            self.state_manager.player_names = {}
            self.state_manager.local_player = None
            self.card_sprites = {i: pygame.sprite.Group() for i in range(4)}
            self.state_manager.current_room_password = None
            self.state_manager.server_ip = None

        elif msg_type == "name_set":
            self.player_name = message.get("name", self.player_name)
            self.name_set = True
            self.state_manager.state = "lobby"
            self.state_manager.waiting_message = None

        elif msg_type in ("room_list", "room_list_update"):
            self.rooms_list = message.get("rooms", [])

        elif msg_type == "room_joined":
            self.current_room_id = message.get("room_id")
            self.current_room_name = message.get("room_name", "")
            self.state_manager.local_player = message.get("player_slot", 0)
            self.state_manager.room_max_players = message.get("max_players", self.state_manager.room_max_players)
            self.state_manager.player_names[self.state_manager.local_player] = self.player_name
            if "player_names" in message:
                self.state_manager.player_names = {int(k): v for k, v in message["player_names"].items()}
            self.update_card_sprites()
            self.state_manager.state = "room_waiting"
            self.is_leader = message.get("is_leader", False)
            self.state_manager.room_rules = {k: v for k, v in message.get("rules", self.rules).items()}
            players_needed = self.state_manager.room_max_players - len(self.state_manager.player_names)
            if players_needed > 0:
                self.state_manager.waiting_message = f"Waiting for {players_needed} more player(s)..."
            else:
                self.state_manager.waiting_message = None
            self.state_manager.current_room_password = getattr(self, 'current_room_password', None)
            self.current_room_password = None

        elif msg_type == "player_joined":
            if "player_names" in message:
                self.state_manager.player_names = {int(k): v for k, v in message["player_names"].items()}
            else:
                player_slot = message.get("player_slot", -1)
                if player_slot >= 0:
                    self.state_manager.player_names[player_slot] = message.get("player_name", "Unknown")

        elif msg_type == "player_left":
            if "player_names" in message:
                self.state_manager.player_names = {int(k): v for k, v in message["player_names"].items()}
            else:
                left_name = message.get("player_name", "")
                slots_to_remove = [k for k, v in self.state_manager.player_names.items() if v == left_name]
                for k in slots_to_remove:
                    self.state_manager.player_names.pop(k, None)

            if self.state_manager.state == "room_waiting":
                players_needed = self.state_manager.room_max_players - len(self.state_manager.player_names)
                if players_needed > 0:
                    self.state_manager.waiting_message = f"Waiting for {players_needed} more player(s)..."
                else:
                    self.state_manager.waiting_message = None

        elif msg_type == "waiting":
            if "player_names" in message:
                self.state_manager.player_names = {int(k): v for k, v in message["player_names"].items()}
            players_needed = message.get("players_needed", 0)
            if players_needed > 0:
                self.state_manager.waiting_message = f"Waiting for {players_needed} more player(s)..."
            else:
                self.state_manager.waiting_message = None

        elif msg_type == "gs":
            if "player_names" in message:
                message["player_names"] = {int(k): v for k, v in message["player_names"].items()}
                self.state_manager.player_names = message["player_names"]
            # Use actual player count (from player_names), not max slot count
            self.state_manager.num_players = len(message.get("player_names", {})) or message.get("num_players", len(message.get("players", [])))
            prev_state = self.state_manager.state
            # Snapshot old hand sizes BEFORE updating state
            old_hand_sizes: Dict[int, int] = {}
            if self.state_manager.game_state:
                for pi, ph in enumerate(self.state_manager.game_state.get("players", [])):
                    old_hand_sizes[pi] = len(ph)
            self.state_manager.game_state = message
            self.state_manager.waiting_message = None
            self.state_manager.state = "playing"
            if prev_state != "playing":
                # ── Game start: pre-hide ALL cards, then queue round-robin deal ──
                self.state_manager.dealing_animation = True
                self._trigger_deal_animation()   # fills hidden_cards first, then queues
                self.update_card_sprites()        # renders nothing (all hidden)
                def _on_deal_done():
                    self.state_manager.dealing_animation = False
                self.animation_manager.on_deal_complete = _on_deal_done
            else:
                # ── Mid-game: fire confirmed play animation if pending ────────
                local = self.state_manager.local_player
                if self._pending_play and local is not None:
                    new_local_size = len(message.get("players", [[]]*(local+1))[local]) if local < len(message.get("players", [])) else 0
                    old_local_size = old_hand_sizes.get(local, new_local_size)
                    if new_local_size < old_local_size:
                        # Server confirmed the card was played — animate now
                        card, sx, sy, angle = self._pending_play
                        self._trigger_play_animation(card, sx, sy, angle)
                    self._pending_play = None

                # ── Detect plays by any player (animate cards played to discard)
                new_players = message.get("players", [])
                discard_pile = message.get("discard_pile", [])
                for pi, hand in enumerate(new_players):
                    old_size = old_hand_sizes.get(pi, len(hand))
                    new_size = len(hand)
                    if new_size < old_size:
                        # local already handled via _pending_play
                        if pi == self.state_manager.local_player:
                            continue
                        # Use top of discard pile as played card (best-effort)
                        played_card_data = discard_pile[-1] if discard_pile else None
                        pos_index = (pi - self.state_manager.local_player) % self.state_manager.num_players
                        start_x, start_y, start_angle = self.layout.get_player_position(pos_index, old_size, old_size - 1, is_local=(pi == self.state_manager.local_player))
                        if played_card_data:
                            key = played_card_data.get("name")
                            if key not in self.card_cache:
                                try:
                                    self.card_cache[key] = Card(key, played_card_data.get("value", 0), played_card_data.get("suit", ""))
                                except Exception:
                                    self.card_cache[key] = Card(key, 0, "")
                            card_obj = self.card_cache.get(key)
                        else:
                            # Fallback to back image
                            card_obj = Card("__back__", 0, "")
                            if self.renderer.card_back:
                                try:
                                    card_obj.image = pygame.transform.scale(self.renderer.card_back, (CARD_WIDTH, CARD_HEIGHT))
                                except Exception:
                                    pass
                        if card_obj:
                            self._trigger_play_animation(card_obj, float(start_x), float(start_y), start_angle)

                # ── Detect newly drawn cards ─────────────────────────────────
                new_players = message.get("players", [])
                drew_cards: List[Tuple[int, dict, int, int]] = []
                for pi, hand in enumerate(new_players):
                    old_size = old_hand_sizes.get(pi, len(hand))
                    new_size = len(hand)
                    if new_size > old_size:
                        for ci in range(old_size, new_size):
                            # Pre-hide BEFORE update_card_sprites so it never flashes
                            self.animation_manager.hidden_cards.add((pi, ci))
                            drew_cards.append((pi, hand[ci], ci, new_size))
                self.update_card_sprites()        # renders without the newly hidden cards
                for pi, card_data, card_index, final_hand_size in drew_cards:
                    self._trigger_draw_animation(pi, card_data, card_index, final_hand_size)

        elif msg_type == "go":
            if "player_names" in message:
                self.state_manager.player_names = {int(k): v for k, v in message["player_names"].items()}
            # Sort: connected players first (by rank), disconnected last
            results = message.get("results", [])
            # Strip tournament-round fields — not relevant for final/normal leaderboard
            for r in results:
                r.pop("cards_next", None)
            connected = [e for e in results if not e.get("disconnected", False)]
            disconnected = [e for e in results if e.get("disconnected", False)]
            self.state_manager.leaderboard_data = connected + disconnected
            # Clear leftover tournament round state so leaderboard renders cleanly
            self.state_manager.tournament_round_over = False
            self.state_manager.tournament_results = None
            self.animation_manager.clear()
            # Wait 2s before showing leaderboard — keep cards visible until then
            self.state_manager.leaderboard_pending = True
            self.state_manager.leaderboard_pending_start = time.time()

        elif msg_type == "back_to_lobby":
            self.is_leader = False
            self.state_manager.state = "lobby"
            self.current_room_id = None
            self.current_room_name = ""
            self.state_manager.player_names = {}
            self.state_manager.waiting_message = None
            self.state_manager.current_room_password = None
            # keep server_ip so lobby can show which server we're on (optional)
            # If want cleared on back_to_lobby, uncomment next line
            # self.state_manager.server_ip = None

        elif msg_type == "you_are_leader":
            self.is_leader = True

        elif msg_type == "tournament_round_over":
            self.state_manager.tournament_round_over = True
            self.state_manager.tournament_results = message.get("results", [])
            self.state_manager.tournament_round = message.get("round", 0)
            self.state_manager.tournament_penalties = message.get("penalties", {})
            self.state_manager.player_names = {int(k): v for k, v in message.get("player_names", {}).items()}
            self.state_manager.leaderboard_pending = True
            self.state_manager.leaderboard_pending_start = time.time()

        elif msg_type == "e":
            self._pending_play = None
            err_msg = message.get('msg', '')
            if "name" in err_msg.lower() or "connect" in err_msg.lower():
                self.state_manager.waiting_message = err_msg
            else:
                self.state_manager.waiting_message = None

    def _trigger_deal_animation(self) -> None:
        """Queue deal animations in round-robin: 1 card per player per round."""
        if not self.state_manager.game_state or self.state_manager.local_player is None:
            return
        am = self.animation_manager
        am.clear()
        draw_pile_pos = (float(self.layout.draw_pile_rect.x + 3), float(self.layout.draw_pile_rect.y + 3))
        back_surface = None
        if self.renderer.card_back:
            try:
                back_surface = pygame.transform.scale(self.renderer.card_back, (CARD_WIDTH, CARD_HEIGHT))
            except Exception:
                pass
        num_players = self.state_manager.num_players
        players_data = self.state_manager.game_state.get("players", [])
        hand_size = max((len(players_data[i]) for i in range(num_players) if i < len(players_data)), default=0)
        # Round-robin: card round 0..hand_size-1, inner loop player 0..num_players-1
        for card_round in range(hand_size):
            for i in range(num_players):
                hand = players_data[i] if i < len(players_data) else []
                if card_round >= len(hand):
                    continue
                card_data = hand[card_round]
                pos_index = (i - self.state_manager.local_player) % num_players
                is_local = (i == self.state_manager.local_player)
                x, y, angle = self.layout.get_player_position(pos_index, len(hand), card_round, is_local=is_local)
                if is_local:
                    card_key = card_data["name"]
                    if card_key not in self.card_cache:
                        self.card_cache[card_key] = Card(card_data["name"], card_data["value"], card_data["suit"])
                    img = self.card_cache[card_key].image
                else:
                    img = back_surface or pygame.Surface((CARD_WIDTH, CARD_HEIGHT))
                rotated_img = pygame.transform.rotate(img, angle)
                am.queue_deal(rotated_img, draw_pile_pos, (float(x), float(y)), angle=0,
                              player_index=i, card_index=card_round)

    def _trigger_draw_animation(self, player_index: int, card_data: dict,
                                   card_index: int, final_hand_size: int) -> None:
        """Animate a drawn card flying to its correct slot in the hand."""
        if self.state_manager.local_player is None:
            return
        am = self.animation_manager
        pos_index = (player_index - self.state_manager.local_player) % self.state_manager.num_players
        is_local = (player_index == self.state_manager.local_player)
        # final_hand_size drives spacing so all cards land at correct positions
        x, y, angle = self.layout.get_player_position(pos_index, final_hand_size, card_index, is_local=is_local)
        draw_pile_pos = (float(self.layout.draw_pile_rect.x + 3), float(self.layout.draw_pile_rect.y + 3))
        if is_local:
            card_key = card_data["name"]
            if card_key not in self.card_cache:
                self.card_cache[card_key] = Card(card_data["name"], card_data["value"], card_data["suit"])
            img = self.card_cache[card_key].image
        else:
            if self.renderer.card_back:
                try:
                    img = pygame.transform.scale(self.renderer.card_back, (CARD_WIDTH, CARD_HEIGHT))
                except Exception:
                    img = pygame.Surface((CARD_WIDTH, CARD_HEIGHT))
            else:
                img = pygame.Surface((CARD_WIDTH, CARD_HEIGHT))
        rotated_img = pygame.transform.rotate(img, angle)
        am.queue_deal(rotated_img, draw_pile_pos, (float(x), float(y)), angle=0,
                      player_index=player_index, card_index=card_index, pre_hidden=True)

    def _trigger_play_animation(self, card: Card, start_x: float, start_y: float, angle: float) -> None:
        """Start an animation of card flying from hand to discard pile."""
        dx, dy = self.layout.discard_pile_pos
        img = pygame.transform.rotate(card.image, angle)
        self.animation_manager.play_card(img, (start_x, start_y), (dx, dy), angle=0)

    def update_card_sprites(self) -> None:
        if not self.state_manager.game_state or self.state_manager.local_player is None:
            return

        num_players = self.state_manager.num_players
        for i in range(num_players):
            if i not in self.card_sprites:
                self.card_sprites[i] = pygame.sprite.Group()
            self.card_sprites[i].empty()

        back_card = None
        if self.renderer.card_back:
            try:
                back_surface = pygame.transform.scale(self.renderer.card_back, (CARD_WIDTH, CARD_HEIGHT))
                back_card = Card("__back__", 0, "")
                back_card.image = back_surface
            except Exception:
                back_card = None

        hidden = self.animation_manager.hidden_cards

        for i in range(num_players):
            hand = self.state_manager.game_state.get("players", [])[i]
            if not hand:
                continue

            pos_index = (i - self.state_manager.local_player) % num_players
            is_local = (i == self.state_manager.local_player)

            for j, card_data in enumerate(hand):
                # Skip cards that are still mid-animation (will appear when they land)
                if (i, j) in hidden:
                    continue

                card_key = card_data["name"]
                if card_key not in self.card_cache:
                    self.card_cache[card_key] = Card(card_data["name"], card_data["value"], card_data["suit"])
                card = self.card_cache[card_key]

                x, y, angle = self.layout.get_player_position(pos_index, len(hand), j, is_local=is_local)
                if is_local:
                    display_card = card
                else:
                    if back_card:
                        display_card = back_card
                    else:
                        display_card = self.card_cache.get("back", Card("back", 0, ""))
                self.card_sprites[i].add(CardSprite(display_card, x, y, angle))


class MultiRoomClient:
    """Main application class."""

    def __init__(self):
        pygame.init()
        if not pygame.font.get_init():
            pygame.font.init()

        self.font = pygame.font.SysFont("Times New Roman", 24)
        self.title_font = pygame.font.SysFont("Times New Roman", 36, bold=True)
        self.small_font = pygame.font.SysFont("Times New Roman", 18)
        info = pygame.display.Info()
        taskbar_margin = 85
        global SCREEN_WIDTH, SCREEN_HEIGHT
        SCREEN_WIDTH = info.current_w - 20
        SCREEN_HEIGHT = info.current_h - taskbar_margin
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption("Sedma bere tri")

        self.layout = LayoutManager(SCREEN_WIDTH, SCREEN_HEIGHT)
        self.renderer = Renderer(self.screen, self.layout, self.font, self.title_font, self.small_font)
        self.state_manager = StateManager()
        self.network = NetworkManager()
        self.lan_server = LanServerManager()

        self._setup_ui()
        self.event_handler = EventHandler(
            self.network, self.state_manager, self.renderer, self.layout,
            self.input_fields, self.ui_elements, self.lan_server
        )
        self.running: bool = True

        self.renderer.load_assets(
            self.renderer.current_background_path,
            self.renderer.current_card_back_path,
            (CARD_WIDTH, CARD_HEIGHT)
        )

        suits = ["srdce", "zelen", "zalud", "gula"]
        value_names = {7: "7", 8: "8", 9: "9", 10: "10", 11: "dolnik", 12: "hornik", 13: "kral", 14: "eso"}
        card_names = [f"{value_names[v]}_{s}" for s in suits for v in value_names]
        Card.preload_images(card_names)

    def _setup_ui(self) -> None:
        cx = SCREEN_WIDTH // 2
        cy = SCREEN_HEIGHT // 2

        self.input_fields = {
            "ip": InputField(pygame.Rect(cx - 150, cy - 150, 300, 40), "Server IP", self.font, BUTTON_COLOR, 20),
            "name": InputField(pygame.Rect(cx - 150, cy - 70, 300, 40), "Username", self.font, BUTTON_COLOR, 20),
            "room_name": InputField(pygame.Rect(50, 260, 200, 40), "Room name", self.font, BUTTON_COLOR, 30),
            "room_password": InputField(pygame.Rect(50, 360, 200, 40), "Password (if private)", self.font,
                                        BUTTON_COLOR, 30, is_password=True),
        }

        self.ui_elements = {
            "connect": UIElement(pygame.Rect(cx - 75, cy - 10, 150, 40), "Connect", self.font, BUTTON_COLOR),
            "close": UIElement(pygame.Rect(cx - 75, cy + 50, 150, 40), "Close", self.font, BUTTON_COLOR),
            "create": UIElement(pygame.Rect(50, 150, 200, 40), "Create Room", self.font, BUTTON_COLOR),
            "refresh": UIElement(pygame.Rect(50, 200, 200, 40), "Refresh Rooms", self.font, BUTTON_COLOR),
            "disconnect": UIElement(pygame.Rect(50, SCREEN_HEIGHT - 60, 200, 40), "Disconnect", self.font,
                                    BUTTON_COLOR),
            "leave_room": UIElement(pygame.Rect(10, SCREEN_HEIGHT - 44, 90, 30), "Leave", self.font,
                                    BUTTON_COLOR),
            "customize": UIElement(
                pygame.Rect(20, SCREEN_HEIGHT - 80, 180, 60),
                "Customize", self.font, CUSTOMIZE_BUTTON_COLOR
            ),
            "lan_server": UIElement(
                pygame.Rect(SCREEN_WIDTH - 230, SCREEN_HEIGHT - 65, 210, 45),
                "Start LAN Server", self.font, LAN_INACTIVE_COLOR,
                hover_color=(180, 80, 80)
            ),
            # Public server button moved to top-right
            "public_server": UIElement(
                pygame.Rect(SCREEN_WIDTH - 230, 20, 210, 45),
                "Public Server", self.font, (40, 100, 160),
                hover_color=(60, 140, 220)
            ),
            # Back to LAN button (used on Public Server screen)
            "back_to_lan": UIElement(
                pygame.Rect(SCREEN_WIDTH - 230, 20, 210, 45),
                "Back to LAN", self.font, (40, 100, 160),
                hover_color=(60, 140, 220)
            ),
            "end_turn": UIElement(
                pygame.Rect(SCREEN_WIDTH // 2 + 130, SCREEN_HEIGHT - 202, 110, 28),
                "End Turn", self.small_font, BUTTON_COLOR
            ),
            "start_room": UIElement(pygame.Rect(50, 420, 200, 40), "Start Room", self.font, (0, 150, 0)),
            "start_game": UIElement(
                pygame.Rect(SCREEN_WIDTH // 2 - 75, SCREEN_HEIGHT // 2 + 280, 150, 44),
                "Start Game", self.font, (0, 150, 0), hover_color=(0, 200, 0)
            ),
            "cancel_create": UIElement(pygame.Rect(50, 470, 200, 40), "Cancel", self.font, (150, 0, 0)),
            # Join-password buttons — rects are set at render time by render_join_password()
            "join_room_btn": UIElement(pygame.Rect(0, 0, 380, 38), "Join", self.font, (0, 130, 0)),
            "cancel_join": UIElement(pygame.Rect(0, 0, 380, 38), "Cancel", self.font, (130, 30, 30)),
        }

    def run(self) -> None:
        clock = pygame.time.Clock()

        while self.running:
            dt = clock.tick(60)

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    self.event_handler.handle_click(event.pos)
                elif event.type == pygame.KEYDOWN:
                    self.event_handler.handle_key(event)

            while not self.network.message_queue.empty():
                self.event_handler._on_network_message(self.network.message_queue.get())

            if (getattr(self.state_manager, 'leaderboard_pending', False) and
                    time.time() - self.state_manager.leaderboard_pending_start > 2.0):
                self.state_manager.leaderboard_pending = False
                self.state_manager.state = "leaderboard"
                self.state_manager.leaderboard_start = time.time()
                if not getattr(self.state_manager, 'tournament_round_over', False):
                    self.state_manager.game_state = None
                    self.event_handler.card_sprites = {i: pygame.sprite.Group() for i in range(4)}
                    self.event_handler.current_room_id = None
                    self.event_handler.current_room_name = ""

            tourn_round = getattr(self.state_manager, 'tournament_round_over', False)
            lb_duration = 4.0 if tourn_round else LEADERBOARD_DURATION
            if (self.state_manager.state == "leaderboard" and
                    time.time() - self.state_manager.leaderboard_start > lb_duration):
                if getattr(self.state_manager, 'tournament_round_over', False):
                    # Tournament round: hide leaderboard, stay in playing — server sends next gs
                    self.state_manager.tournament_round_over = False
                    self.state_manager.tournament_results = None
                    self.state_manager.state = "playing"
                else:
                    self.network.send_message({"t": "leave_room"})
                    self.state_manager.state = "lobby"
                    self.event_handler.current_room_id = None
                    self.event_handler.current_room_name = ""
                    self.state_manager.leaderboard_data = None
                    self.state_manager.game_state = None

            # Update animations
            self.event_handler.animation_manager.update(dt)

            mouse_pos = pygame.mouse.get_pos()
            self._render(mouse_pos)

            # Draw card animations on top of everything
            self.event_handler.animation_manager.draw(self.screen)

            pygame.display.flip()

        self._cleanup()

    def _render(self, mouse_pos: Tuple[int, int]) -> None:
        state = self.state_manager.state

        if state == "menu":
            self.renderer.render_menu(
                self.input_fields["ip"], self.input_fields["name"],
                self.ui_elements["connect"], self.ui_elements["close"],
                self.ui_elements["lan_server"], self.lan_server,
                self.state_manager.waiting_message,
                self.ui_elements["public_server"]
            )
            self.ui_elements["customize"].draw(self.screen, mouse_pos)

        elif state == "public_menu":
            # Render Public Server screen (no IP field, shows name/connect/close/customize/back)
            self.renderer.render_public_menu(
                self.input_fields["name"],
                self.ui_elements["connect"],
                self.ui_elements["close"],
                self.ui_elements["customize"],
                self.ui_elements.get("back_to_lan"),
                self.state_manager.waiting_message
            )

        elif state == "lobby":
            self.renderer.render_lobby(
                self.renderer.current_background_path,
                self.event_handler.player_name,
                self.input_fields["room_name"],
                self.ui_elements["create"], self.ui_elements["refresh"],
                self.ui_elements["disconnect"],
                self.event_handler.rooms_list,
                self.state_manager.waiting_message,
                self.event_handler.rules,
                getattr(self.state_manager, 'server_ip', None)
            )
        elif state == "create_room":
            self.renderer.render_create_room(
                self.input_fields,
                self.ui_elements,
                self.event_handler.selected_max_players,
                self.event_handler.rules,
                getattr(self.event_handler, 'room_is_private', False),
                mouse_pos,
                self.state_manager.waiting_message
            )

        elif state in ("room_waiting", "playing"):
            self.renderer.render_game(
                self.state_manager, self.event_handler.card_sprites,
                self.event_handler.current_room_name, mouse_pos,
                self.state_manager.waiting_message,
                self.ui_elements["leave_room"],
                self.ui_elements["end_turn"]
            )
            if state == "room_waiting" and self.event_handler.is_leader:
                enough_players = len(self.state_manager.player_names) >= 2
                btn = self.ui_elements["start_game"]
                if enough_players:
                    btn.bg_color = (0, 150, 0)
                    btn.hover_color = (0, 200, 0)
                else:
                    btn.bg_color = (80, 80, 80)
                    btn.hover_color = (80, 80, 80)
                btn.draw(self.screen, mouse_pos)

        elif state == "join_password":
            # Render lobby in background, then draw the password modal on top
            self.renderer.render_lobby(
                self.renderer.current_background_path,
                self.event_handler.player_name,
                self.input_fields["room_name"],
                self.ui_elements["create"], self.ui_elements["refresh"],
                self.ui_elements["disconnect"],
                self.event_handler.rooms_list,
                None,
                self.event_handler.rules
            )
            self.renderer.render_join_password(
                self.input_fields, self.ui_elements, mouse_pos
            )

        elif state == "leaderboard":
            self.renderer.render_leaderboard(
                self.state_manager, mouse_pos,
                self.ui_elements["leave_room"]
            )

        elif state == "customize":
            self.renderer.render_customize(mouse_pos)
        elif state == "credits":
            self.renderer.render_credits(mouse_pos)

    def _cleanup(self) -> None:
        self.lan_server.stop()
        self.network.disconnect()
        pygame.quit()


if __name__ == "__main__":
    client = MultiRoomClient()
    client.run()