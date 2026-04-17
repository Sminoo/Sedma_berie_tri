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
CARD_WIDTH, CARD_HEIGHT = 80, 142
CARD_HIGHLIGHT_THICKNESS = 3
CLICK_DEBOUNCE_MS = 200
LEADERBOARD_DURATION = 10

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


class LayoutManager:
    """Manages card and UI element positions for different player seat arrangements."""

    def __init__(self, screen_width: int, screen_height: int):
        self.screen_w = screen_width
        self.screen_h = screen_height
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
            {"x": screen_width // 2, "y": 190, "align": "center", "angle": 180},
            {"x": 190, "y": screen_height // 2, "align": "center", "angle": -90}
        ]

    def get_player_position(self, pos_index: int, num_cards: int, card_index: int,
                            is_local: bool = False) -> Tuple[int, int, float]:
        """Calculate card position based on seat, hand size, and card index.

        Cards are spread so the full fan always fits within the screen.
        For the local player (bottom) the usable width is the screen minus
        side margins; for side players (left/right) the usable height is
        the screen minus top/bottom margins.
        """
        pos = self.positions[pos_index]

        if pos_index in (0, 2):
            # Horizontal fan (bottom / top)
            margin = 20  # pixels kept free on each side
            usable = self.screen_w - 2 * margin - CARD_WIDTH
            if num_cards <= 1:
                offset = 0
            else:
                max_offset = usable // max(num_cards - 1, 1)
                if is_local:
                    # Local player: prefer comfortable 84 px, shrink if needed
                    offset = min(84, max_offset)
                else:
                    # Opponent at top: smaller cards are fine, cap at 52 px
                    offset = min(52, max_offset)
            total_w = (num_cards - 1) * offset if num_cards > 1 else 0
            x = self.screen_w // 2 - total_w // 2 + card_index * offset
            y = pos["y"]
        else:
            # Vertical fan (left / right side)
            margin = 20
            usable = self.screen_h - 2 * margin - CARD_WIDTH  # rotated card width ≈ CARD_HEIGHT but use CARD_WIDTH as step
            if num_cards <= 1:
                offset = 0
            else:
                max_offset = usable // max(num_cards - 1, 1)
                offset = min(52, max_offset)
            total_h = (num_cards - 1) * offset if num_cards > 1 else 0
            x = pos["x"]
            y = self.screen_h // 2 - total_h // 2 + card_index * offset

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
        """Receive and decompress a length-prefixed JSON message.

        Returns a dict. If the server closed the connection it returns a
        message with t=="server_disconnected" so the rest of the client can
        handle it explicitly.
        """
        sock = self.client_socket
        if sock is None:
            return None

        buffer = b""
        for _ in range(retries):
            try:
                if len(buffer) < 4:
                    packet = sock.recv(4)
                    # empty packet indicates orderly shutdown by peer
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
                # treat unrecoverable socket errors as disconnection
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
        self.waiting_message: Optional[str] = None
        self.waiting_start: float = time.time()
        self.leaderboard_start: float = 0
        self.leaderboard_data: Optional[List[Dict]] = None
        self.target_private_room_id: Optional[str] = None


class LanServerManager:
    """Manages starting/stopping a local LAN server subprocess."""

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.running: bool = False
        self.local_ip: Optional[str] = None
        import atexit
        atexit.register(self.stop)

    def get_local_ip(self) -> str:
        """Detect the local network IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except socket.error:
            return "127.0.0.1"

    def start(self) -> bool:
        """Start server.py as a background subprocess."""
        if self.running:
            return False
        try:
            server_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
            creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self.process = subprocess.Popen(
                [sys.executable, server_script, "--port", str(PORT)],
                creationflags=creation_flags
            )
            time.sleep(0.5)
            if self.process.poll() is not None:
                self.process = None
                return False
            self.local_ip = self.get_local_ip()
            self.running = True
            return True
        except Exception as e:
            logger.error(f"Failed to start LAN server: {e}")
            return False

    def stop(self) -> None:
        """Terminate the server subprocess."""
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

        # Customization options
        self.background_options = [
            "background_green.png",
            "background_blue.png",
            "background_red.png",
        ]
        # Card backs are individual images inside assets/cards/card_backs
        backs_dir = os.path.join("assets", "cards", "card_backs")
        try:
            backs = [f for f in os.listdir(backs_dir) if f.lower().endswith((".png", ".jpg", ".bmp"))]
            backs.sort()
            self.card_back_themes = backs if backs else ["back.png"]
        except OSError:
            self.card_back_themes = ["back.png"]

        self.selected_background = self.background_options[0]
        # selected_card_theme stores the filename of the chosen back image
        self.selected_card_theme = self.card_back_themes[0]
        self.current_background_path = f"assets/backgrounds/{self.selected_background}"
        self.current_card_back_path = os.path.join("assets", "cards", "card_backs", self.selected_card_theme)

        # Load suit images for horník picker/indicator
        self.suit_images = {}
        suits = ["srdce", "zelen", "zalud", "gula"]
        for s in suits:
            path = os.path.join("assets", "znaky", f"{s}.png")
            try:
                img = pygame.image.load(path).convert_alpha()
                self.suit_images[s] = img
            except Exception:
                # fallback colored surface
                fallback = pygame.Surface((48, 48), pygame.SRCALPHA)
                colors = {"srdce": (200, 50, 50), "zelen": (50, 200, 50), "zalud": (150, 100, 50),
                          "gula": (200, 200, 50)}
                fallback.fill(colors.get(s, (120, 120, 120)))
                self.suit_images[s] = fallback

        # For lobby player-count display
        self.selected_room_max_players = 4

    def load_assets(self, background_path: str, card_back_path: str, size: Tuple[int, int]) -> None:
        """Load background and card back images, with fallbacks."""
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
        """Fill screen and draw background image."""
        self.screen.fill(BACKGROUND_COLOR)
        if self.background:
            self.screen.blit(self.background, (0, 0))

    def render_menu(self, ip_field: InputField, name_field: InputField, connect_btn: UIElement,
                    close_btn: UIElement, lan_btn: UIElement, lan_server: LanServerManager,
                    waiting_message: Optional[str], public_btn: UIElement = None) -> None:
        """Draw the main menu screen with connection fields and LAN server button."""
        self._draw_background()

        # Title
        title = self.title_font.render("Sedma Bere Tri", True, TEXT_COLOR)
        self.screen.blit(title, title.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 250)))

        # Input fields with labels
        ip_label = self.font.render("Server IP:", True, TEXT_COLOR)
        self.screen.blit(ip_label, (ip_field.rect.x, ip_field.rect.y - 26))
        ip_field.draw(self.screen)

        name_label = self.font.render("Your Name:", True, TEXT_COLOR)
        self.screen.blit(name_label, (name_field.rect.x, name_field.rect.y - 26))
        name_field.draw(self.screen)

        mouse_pos = pygame.mouse.get_pos()
        connect_btn.draw(self.screen, mouse_pos)
        close_btn.draw(self.screen, mouse_pos)

        # Public server button
        if public_btn:
            public_btn.draw(self.screen, mouse_pos)

        # LAN Server section (bottom-right)
        if lan_server.running and lan_server.local_ip:
            ip_text = self.font.render(f"LAN IP: {lan_server.local_ip}:{PORT}", True, SUCCESS_COLOR)
            ip_rect = ip_text.get_rect(right=SCREEN_WIDTH - 20, bottom=lan_btn.rect.top - 5)
            self.screen.blit(ip_text, ip_rect)

            status_text = self.small_font.render("● Server Running", True, SUCCESS_COLOR)
            status_rect = status_text.get_rect(right=SCREEN_WIDTH - 20, bottom=ip_rect.top - 3)
            self.screen.blit(status_text, status_rect)

        lan_btn.draw(self.screen, mouse_pos)

        # Status message
        if waiting_message:
            is_error = "error" in waiting_message.lower() or "please" in waiting_message.lower()
            msg_color = ERROR_COLOR if is_error else TEXT_COLOR
            msg_surface = self.font.render(waiting_message, True, msg_color)
            self.screen.blit(msg_surface, msg_surface.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 185)))

    def render_customize(self, mouse_pos: Tuple[int, int]) -> None:
        """Draw the customization screen for backgrounds and card backs."""
        self._draw_background()

        title = self.title_font.render("Customize", True, TEXT_COLOR)
        self.screen.blit(title, title.get_rect(center=(SCREEN_WIDTH // 2, 80)))

        # Background section
        bg_title = self.font.render("Background", True, TEXT_COLOR)
        self.screen.blit(bg_title, (120, 160))

        y = 210
        for bg in self.background_options:
            rect = pygame.Rect(100, y, 340, 45)
            color = (0, 180, 0) if bg == self.selected_background else (
                CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR)
            pygame.draw.rect(self.screen, color, rect, border_radius=8)
            label = self.font.render(bg.replace(".png", "").replace("_", " ").title(), True, TEXT_COLOR)
            self.screen.blit(label, (130, y + 10))
            y += 55

        # Deck Design section
        card_title = self.font.render("Deck Design", True, TEXT_COLOR)
        self.screen.blit(card_title, (SCREEN_WIDTH // 2 + 50, 160))

        y = 210
        for theme in self.card_back_themes:
            rect = pygame.Rect(SCREEN_WIDTH // 2 + 30, y, 340, 45)
            color = (0, 180, 0) if theme == self.selected_card_theme else (
                CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR)
            pygame.draw.rect(self.screen, color, rect, border_radius=8)
            label_text = os.path.splitext(theme)[0].replace("_", " ").title()
            label = self.font.render(label_text, True, TEXT_COLOR)
            self.screen.blit(label, (SCREEN_WIDTH // 2 + 60, y + 10))
            y += 55

        # Preview of selected card back (moved beside selection)
        preview_x = SCREEN_WIDTH // 2 + 400
        preview_y = 210
        preview_title = self.font.render("Back Preview", True, TEXT_COLOR)
        self.screen.blit(preview_title, (preview_x, preview_y - 30))
        preview_size = (int(CARD_WIDTH * 0.75), int(CARD_HEIGHT * 0.75))
        if self.card_back:
            preview_img = pygame.transform.scale(self.card_back, preview_size)
            preview_rect = preview_img.get_rect(topleft=(preview_x, preview_y))
            self.screen.blit(preview_img, preview_rect)
        else:
            placeholder_rect = pygame.Rect(preview_x, preview_y, preview_size[0], preview_size[1])
            pygame.draw.rect(self.screen, PLACEHOLDER_COLOR, placeholder_rect)

        # Buttons
        apply_rect = pygame.Rect(SCREEN_WIDTH // 2 - 220, SCREEN_HEIGHT - 100, 200, 60)
        cancel_rect = pygame.Rect(SCREEN_WIDTH // 2 + 40, SCREEN_HEIGHT - 100, 200, 60)

        for rect, text in [(apply_rect, "Apply & Return"), (cancel_rect, "Cancel")]:
            color = BUTTON_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR
            pygame.draw.rect(self.screen, color, rect, border_radius=10)
            txt = self.font.render(text, True, TEXT_COLOR)
            self.screen.blit(txt, txt.get_rect(center=rect.center))

    def render_lobby(self, background_path: str, player_name: str,
                     create_btn: UIElement, refresh_btn: UIElement, disconnect_btn: UIElement,
                     rooms_list: List[Dict], waiting_message: Optional[str]) -> None:
        """Draw the lobby screen with room creation and room list."""
        try:
            self.background = pygame.image.load(background_path)
        except pygame.error:
            pass
        self.screen.blit(self.background, (0, 0))

        title = self.title_font.render(f"Playing as: {player_name}", True, TEXT_COLOR)
        self.screen.blit(title, (50, 20))

        mouse_pos = pygame.mouse.get_pos()
        create_btn.draw(self.screen, mouse_pos)
        refresh_btn.draw(self.screen, mouse_pos)

        # Room list and disconnect
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
        """Draw the create-room dialog overlay."""
        self._draw_background()
        # Dim background
        overlay = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 170))
        self.screen.blit(overlay, (0, 0))

        # Dialog box – tall enough for all elements
        dlg_w, dlg_h = 620, 610
        dlg_x = SCREEN_WIDTH // 2 - dlg_w // 2
        dlg_y = SCREEN_HEIGHT // 2 - dlg_h // 2
        dlg_rect = pygame.Rect(dlg_x, dlg_y, dlg_w, dlg_h)
        pygame.draw.rect(self.screen, (30, 30, 30), dlg_rect, border_radius=10)
        pygame.draw.rect(self.screen, (70, 70, 70), dlg_rect, 1, border_radius=10)

        title = self.title_font.render("Create Room", True, TEXT_COLOR)
        self.screen.blit(title, title.get_rect(center=(dlg_x + dlg_w // 2, dlg_y + 28)))

        # Horizontal divider
        pygame.draw.line(self.screen, (70, 70, 70), (dlg_x + 20, dlg_y + 50), (dlg_x + dlg_w - 20, dlg_y + 50))

        # --- Room name field ---
        room_name_label = self.font.render("Názov miestnosti:", True, TEXT_COLOR)
        self.screen.blit(room_name_label, (dlg_x + 30, dlg_y + 65))
        rn_field = input_fields.get("room_name")
        old_rn_pos = rn_field.rect.topleft
        rn_field.rect = pygame.Rect(dlg_x + 30, dlg_y + 92, dlg_w - 60, 36)
        rn_field.draw(self.screen)

        # --- Player count ---
        pc_label = self.font.render("Počet hráčov:", True, TEXT_COLOR)
        self.screen.blit(pc_label, (dlg_x + 30, dlg_y + 148))
        for i, val in enumerate([2, 3, 4]):
            rect = pygame.Rect(dlg_x + 190 + i * 56, dlg_y + 145, 46, 30)
            is_selected = selected_max_players == val
            color = (0, 180, 0) if is_selected else (
                CUSTOMIZE_HOVER_COLOR if rect.collidepoint(mouse_pos) else BUTTON_COLOR)
            pygame.draw.rect(self.screen, color, rect, border_radius=6)
            label = self.small_font.render(str(val), True, TEXT_COLOR)
            self.screen.blit(label, label.get_rect(center=rect.center))

        # --- Rules ---
        pygame.draw.line(self.screen, (70, 70, 70), (dlg_x + 20, dlg_y + 192), (dlg_x + dlg_w - 20, dlg_y + 192))
        rules_title = self.font.render("Pravidlá / Úpravy:", True, TEXT_COLOR)
        self.screen.blit(rules_title, (dlg_x + 30, dlg_y + 200))
        rule_labels = {
            "stack_sevens": "Prebíjanie sedmy sedmou",
            "zeleny_niznik_prebija_sedmu": "Zelený dolník prebíja sedmu",
            "stack_aces": "Prebíjanie esa esom",
            "play_multiple_cards": "Hranie viacerých rovnakých kariet naraz",
            "hornik_changes_suit": "Horník mení farbu",
        }
        y = dlg_y + 228
        for key, text in rule_labels.items():
            chk_rect = pygame.Rect(dlg_x + 30, y + 1, 16, 16)
            color = (0, 180, 0) if rules.get(key, False) else (180, 0, 0)
            pygame.draw.rect(self.screen, color, chk_rect, border_radius=4)
            label = self.small_font.render(text, True, TEXT_COLOR)
            self.screen.blit(label, (dlg_x + 54, y))
            y += 30

        # --- Privacy + password ---
        pygame.draw.line(self.screen, (70, 70, 70), (dlg_x + 20, dlg_y + 392), (dlg_x + dlg_w - 20, dlg_y + 392))

        # Privacy row: checkbox on the LEFT, label to its right – nothing overlaps
        privacy_chk = pygame.Rect(dlg_x + 30, dlg_y + 404, 18, 18)
        pygame.draw.rect(self.screen, (0, 180, 0) if room_is_private else (120, 120, 120), privacy_chk, border_radius=4)
        privacy_label = self.font.render("Súkromná miestnosť (vyžaduje heslo)", True, TEXT_COLOR)
        self.screen.blit(privacy_label, (dlg_x + 56, dlg_y + 403))

        # Password field 36 px below the checkbox row
        pw_field = input_fields.get("room_password")
        old_pw_pos = pw_field.rect.topleft
        pw_row_y = dlg_y + 436
        if room_is_private:
            pw_label = self.small_font.render("Heslo:", True, TEXT_COLOR)
            self.screen.blit(pw_label, (dlg_x + 30, pw_row_y))
            pw_field.rect = pygame.Rect(dlg_x + 30, pw_row_y + 20, dlg_w - 60, 36)
            pw_field.draw(self.screen)
        else:
            disabled = self.small_font.render("(zaškrtnite políčko pre zadanie hesla)", True, PLACEHOLDER_COLOR)
            self.screen.blit(disabled, (dlg_x + 30, pw_row_y + 8))

        # --- Buttons ---
        btn_y = dlg_y + dlg_h - 58
        start_btn = ui_elements.get("start_room")
        cancel_btn = ui_elements.get("cancel_create")
        start_btn.rect = pygame.Rect(dlg_x + dlg_w - 260, btn_y, 110, 38)
        cancel_btn.rect = pygame.Rect(dlg_x + dlg_w - 135, btn_y, 110, 38)
        start_btn.draw(self.screen, mouse_pos)
        cancel_btn.draw(self.screen, mouse_pos)

        # Waiting / error message
        if waiting_message:
            msg_color = ERROR_COLOR if "error" in waiting_message.lower() else SUCCESS_COLOR
            msg = self.small_font.render(waiting_message, True, msg_color)
            self.screen.blit(msg, (dlg_x + 30, btn_y + 8))

        # restore field positions so lobby layout is unchanged
        try:
            rn_field.rect.topleft = old_rn_pos
            pw_field.rect.topleft = old_pw_pos
        except Exception:
            pass

    def _render_room_list(self, rooms_list: List[Dict]) -> None:
        """Draw the scrollable room list in the lobby."""
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

            # Room name (append lock icon if private)
            room_name = room.get("room_name", "Unknown")
            if room.get("is_private"):
                room_name = "🔒 " + room_name
            title_text = self.font.render(room_name, True, TEXT_COLOR)
            self.screen.blit(title_text, (room_rect.x + 10, room_rect.y + 8))

            # Creator
            creator_surface = self.small_font.render(f"by {room.get('creator', 'Unknown')}", True, details_color)
            self.screen.blit(creator_surface, (room_rect.x + 10, room_rect.y + 32))

            # Player count
            players_text = f"{room.get('players', 0)}/{room.get('max_players', 4)} players"
            if room.get("in_game", False):
                players_text += " (IN GAME)"
            players_surface = self.small_font.render(players_text, True, details_color)
            self.screen.blit(players_surface, (room_rect.x + 10, room_rect.y + 50))

    def render_game(self, state_manager: StateManager, card_sprites: Dict[int, pygame.sprite.Group],
                    current_room_name: str, mouse_pos: Tuple[int, int],
                    waiting_message: Optional[str], leave_btn: UIElement, end_turn_btn: UIElement) -> None:
        """Draw the active game screen with cards, piles, and player info."""
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

            players_title = self.font.render("Players in room:", True, TEXT_COLOR)
            self.screen.blit(players_title, players_title.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 20)))

            if state_manager.player_names:
                y = SCREEN_HEIGHT // 2 + 30
                for slot, name in sorted(state_manager.player_names.items()):
                    color = HIGHLIGHT_COLOR if slot == state_manager.local_player else TEXT_COLOR
                    player_text = self.font.render(f"Player {slot + 1}: {name}", True, color)
                    self.screen.blit(player_text, player_text.get_rect(center=(SCREEN_WIDTH // 2, y)))
                    y += 40
                # show room password if available
                if getattr(state_manager, 'current_room_password', None):
                    pw_surf = self.small_font.render(f"Password: {state_manager.current_room_password}", True,
                                                     TEXT_COLOR)
                    self.screen.blit(pw_surf, pw_surf.get_rect(center=(SCREEN_WIDTH // 2, y + 20)))

        elif state_manager.state == "playing" and state_manager.game_state and state_manager.local_player is not None:
            current_player = state_manager.game_state.get("current_player", 0)
            player_names = state_manager.game_state.get("player_names", {})

            for i in range(state_manager.num_players):
                if card_sprites.get(i):
                    card_sprites[i].draw(self.screen)
                    if i == state_manager.local_player and current_player == state_manager.local_player:
                        for sprite in card_sprites[i]:
                            if sprite.rect.collidepoint(mouse_pos):
                                pygame.draw.rect(self.screen, HIGHLIGHT_COLOR, sprite.rect, CARD_HIGHLIGHT_THICKNESS)

            # Draw pile
            draw_pile_rect = self.layout.draw_pile_rect
            if draw_pile_rect.collidepoint(mouse_pos):
                pygame.draw.rect(self.screen, HIGHLIGHT_COLOR, draw_pile_rect, CARD_HIGHLIGHT_THICKNESS)

            if state_manager.game_state.get("draw_pile_count", 0) > 0:
                self.screen.blit(self.card_back, (draw_pile_rect.x + 3, draw_pile_rect.y + 3))

            # Discard pile
            if state_manager.game_state.get("discard_pile"):
                top = state_manager.game_state["discard_pile"][-1]
                card = Card(top["name"], top["value"], top["suit"])
                card.draw(self.screen, *self.layout.discard_pile_pos)

                # Show chosen suit (if any) near discard pile so all players can see it
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

            # Player names
            for i in range(state_manager.num_players):
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
                    msg = f"Musíš ťahať {seven_penalty} kariet alebo prebiť!"
                    msg_surf = self.title_font.render(msg, True, ERROR_COLOR)
                    self.screen.blit(msg_surf, msg_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 100)))
                elif ace_penalty and cards_played == 0:
                    msg = "Stojíš! Prebi Eso alebo preskoč ťah."
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
        """Draw the post-game leaderboard screen."""
        self._draw_background()

        title = self.title_font.render("Game Over", True, TEXT_COLOR)
        self.screen.blit(title, title.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 4)))

        if state_manager.leaderboard_data:
            for i, entry in enumerate(state_manager.leaderboard_data):
                player_id = entry.get("pid", 0)
                rank = entry.get("rank", i + 1)
                player_name = state_manager.player_names.get(player_id, f"Unknown ({player_id + 1})")
                disconnected = entry.get("disconnected", False)

                text = f"{rank}. {player_name}" + (" (disconnected)" if disconnected else "")
                text_surface = self.font.render(text, True, TEXT_COLOR)
                self.screen.blit(text_surface,
                                 text_surface.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 3 + (i + 1) * 50)))

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
            "hornik_changes_suit": True
        }
        # Dialog state for create-room
        self.room_is_private: bool = False
        # Pending join dialog state
        self.pending_join_room_id = None
        self.current_room_password = None

    @staticmethod
    def validate_ip(ip: str) -> bool:
        """Validate IPv4 address or localhost."""
        if ip.lower() in ('localhost', '127.0.0.1'):
            return True
        return bool(re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip))

    def handle_click(self, pos: Tuple[int, int]) -> None:
        """Route click events to the appropriate screen handler."""
        current_time = pygame.time.get_ticks()
        if current_time - self.last_click_time < CLICK_DEBOUNCE_MS:
            return
        self.last_click_time = current_time

        state = self.state_manager.state
        handlers = {
            "menu": self._handle_menu_click,
            "customize": self._handle_customize_click,
            "lobby": self._handle_lobby_click,
            "create_room": self._handle_create_room_click,
            "join_password": self._handle_join_password_click,
            "room_waiting": self._handle_room_waiting_click,
            "playing": self._handle_game_click,
            "leaderboard": self._handle_leaderboard_click,
        }
        handler = handlers.get(state)
        if handler:
            handler(pos)

    def _handle_menu_click(self, pos: Tuple[int, int]) -> None:
        ip_field = self.input_fields["ip"]
        name_field = self.input_fields["name"]

        # Input field focus
        if ip_field.rect.collidepoint(pos):
            ip_field.active = True
            name_field.active = False
        elif name_field.rect.collidepoint(pos):
            name_field.active = True
            ip_field.active = False
        else:
            ip_field.active = False
            name_field.active = False

        # Button clicks
        if self.ui_elements["connect"].rect.collidepoint(pos):
            self._handle_connect()
        elif self.ui_elements["close"].rect.collidepoint(pos):
            raise SystemExit(0)
        elif self.ui_elements["customize"].rect.collidepoint(pos):
            self.state_manager.state = "customize"
        elif self.ui_elements["lan_server"].rect.collidepoint(pos):
            self._toggle_lan_server()
        elif self.ui_elements["public_server"].rect.collidepoint(pos):
            self._connect_public_server()

    def _toggle_lan_server(self) -> None:
        """Start or stop the LAN server and update button text."""
        btn = self.ui_elements["lan_server"]
        if self.lan_server.running:
            self.lan_server.stop()
            btn.update_text("Start LAN Server")
            btn.bg_color = LAN_INACTIVE_COLOR
            self.state_manager.waiting_message = "LAN server stopped"
        else:
            if self.lan_server.start():
                btn.update_text("Stop LAN Server")
                btn.bg_color = LAN_ACTIVE_COLOR
                ip = self.lan_server.local_ip
                self.state_manager.waiting_message = f"LAN server started on {ip}:{PORT}"
                # Auto-fill IP address field so the host can connect immediately
                self.input_fields["ip"].text = ip
            else:
                self.state_manager.waiting_message = "Failed to start LAN server"

    def _connect_public_server(self) -> None:
        """Connect to the public server at 158.101.177.217."""
        self.input_fields["ip"].text = "158.101.177.217"
        name = self.input_fields["name"].text.strip()
        if not name:
            self.state_manager.waiting_message = "Please enter your name first"
            return
        if not (3 <= len(name) <= 20):
            self.state_manager.waiting_message = "Name must be 3-20 characters"
            return
        self._handle_connect()

    def _handle_customize_click(self, pos: Tuple[int, int]) -> None:
        # Background selection
        y = 210
        for bg in self.renderer.background_options:
            rect = pygame.Rect(100, y, 340, 45)
            if rect.collidepoint(pos):
                self.renderer.selected_background = bg
                self.renderer.current_background_path = f"assets/backgrounds/{bg}"
                self.renderer.load_assets(
                    self.renderer.current_background_path,
                    self.renderer.current_card_back_path,
                    (CARD_WIDTH, CARD_HEIGHT)
                )
                return
            y += 55

        # Card back selection
        y = 210
        for theme in self.renderer.card_back_themes:
            rect = pygame.Rect(SCREEN_WIDTH // 2 + 30, y, 340, 45)
            if rect.collidepoint(pos):
                # 'theme' here is the filename of the selected back image
                self.renderer.selected_card_theme = theme
                self.renderer.current_card_back_path = os.path.join("assets", "cards", "card_backs", theme)
                self.renderer.load_assets(
                    self.renderer.current_background_path,
                    self.renderer.current_card_back_path,
                    (CARD_WIDTH, CARD_HEIGHT)
                )
                # Only the back image changes; do not modify front card art
                return
            y += 55

        # Apply / Cancel → both return to menu
        apply_rect = pygame.Rect(SCREEN_WIDTH // 2 - 220, SCREEN_HEIGHT - 100, 200, 60)
        cancel_rect = pygame.Rect(SCREEN_WIDTH // 2 + 40, SCREEN_HEIGHT - 100, 200, 60)
        if apply_rect.collidepoint(pos) or cancel_rect.collidepoint(pos):
            self.state_manager.state = "menu"

    def _handle_lobby_click(self, pos: Tuple[int, int]) -> None:
        room_name_field = self.input_fields["room_name"]

        if room_name_field.rect.collidepoint(pos):
            room_name_field.active = True
        elif self.ui_elements["create"].rect.collidepoint(pos):
            # Open the create-room dialog (single button) instead of sending immediately
            self.state_manager.state = "create_room"
            # Activate room name input by default
            self.input_fields["room_name"].active = True
            return
        elif self.ui_elements["refresh"].rect.collidepoint(pos):
            self.network.send_message({"t": "refresh_rooms"})
        elif self.ui_elements["disconnect"].rect.collidepoint(pos):
            self.network.disconnect()
            self.state_manager.state = "menu"
            self.state_manager.waiting_message = None
        else:
            room_name_field.active = False

        # Room list click
        rooms_area = pygame.Rect(300, 150, SCREEN_WIDTH - 350, SCREEN_HEIGHT - 200)
        if rooms_area.collidepoint(pos):
            self._handle_room_list_click(pos)

    def _render_join_password(self, mouse_pos: Tuple[int, int]) -> None:
        """Draw a small modal prompting for room password when joining private rooms."""
        # Use renderer.screen and fonts from renderer to draw modal
        screen = self.renderer.screen
        title_font = self.renderer.title_font
        small_font = self.renderer.small_font

        overlay = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 160))
        screen.blit(overlay, (0, 0))

        w, h = 420, 200
        x = SCREEN_WIDTH // 2 - w // 2
        y = SCREEN_HEIGHT // 2 - h // 2
        pygame.draw.rect(screen, (30, 30, 30), pygame.Rect(x, y, w, h), border_radius=8)
        title = title_font.render("Enter room password", True, TEXT_COLOR)
        screen.blit(title, title.get_rect(center=(x + w // 2, y + 30)))

        # Password field
        pw = self.input_fields["room_password"]
        old_pos = pw.rect.topleft
        pw.rect.topleft = (x + 30, y + 70)
        pw.draw(screen)

        # Buttons
        join_btn = self.ui_elements.get("join_room_btn")
        cancel_btn = self.ui_elements.get("cancel_join")
        join_btn.rect.topleft = (x + w - 220, y + h - 70)
        cancel_btn.rect.topleft = (x + w - 220, y + h - 35)
        join_btn.draw(screen, mouse_pos)
        cancel_btn.draw(screen, mouse_pos)

        # restore pw pos
        pw.rect.topleft = old_pos

    def _handle_create_room_click(self, pos: Tuple[int, int]) -> None:
        """Handle clicks inside the create-room dialog."""
        room_name_field = self.input_fields["room_name"]
        password_field = self.input_fields["room_password"]

        # dialog geometry (must match render_create_room)
        dlg_w, dlg_h = 620, 610
        dlg_x = SCREEN_WIDTH // 2 - dlg_w // 2
        dlg_y = SCREEN_HEIGHT // 2 - dlg_h // 2

        # Field rects inside dialog
        rn_rect = pygame.Rect(dlg_x + 30, dlg_y + 92, dlg_w - 60, 36)
        pw_row_y = dlg_y + 436
        pw_rect = pygame.Rect(dlg_x + 30, pw_row_y + 20, dlg_w - 60, 36)

        if rn_rect.collidepoint(pos):
            room_name_field.active = True
            password_field.active = False
            return
        if pw_rect.collidepoint(pos):
            password_field.active = True
            room_name_field.active = False
            return

        # Player count selector inside dialog
        for i, val in enumerate([2, 3, 4]):
            rect = pygame.Rect(dlg_x + 190 + i * 56, dlg_y + 145, 46, 30)
            if rect.collidepoint(pos):
                self.selected_max_players = val
                self.renderer.selected_room_max_players = val
                return

        # Rule checkboxes inside dialog
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

        # Privacy checkbox
        privacy_rect = pygame.Rect(dlg_x + 30, dlg_y + 404, 18, 18)
        if privacy_rect.collidepoint(pos):
            self.room_is_private = not getattr(self, 'room_is_private', False)
            return

        # Start / Cancel buttons
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

        # Clicking outside deactivates fields
        room_name_field.active = False
        password_field.active = False

    def _handle_room_list_click(self, pos: Tuple[int, int]) -> None:
        room_index = (pos[1] - 150) // 80
        if 0 <= room_index < len(self.rooms_list):
            room = self.rooms_list[room_index]
            if room.get("in_game", False) or room.get("players", 0) >= room.get("max_players", 4):
                return
            if room.get("is_private"):
                # open password prompt modal
                self.state_manager.state = "join_password"
                self.pending_join_room_id = room.get("room_id")
                # clear password field and focus it
                self.input_fields["room_password"].text = ""
                self.input_fields["room_password"].active = True
            else:
                self.network.send_message({"t": "join_room", "room_id": room["room_id"]})

    def _handle_room_waiting_click(self, pos: Tuple[int, int]) -> None:
        if self.ui_elements["leave_room"].rect.collidepoint(pos):
            self.network.send_message({"t": "leave_room"})

    def _handle_join_password_click(self, pos: Tuple[int, int]) -> None:
        # dialog geometry must match _render_join_password
        w, h = 420, 200
        x = SCREEN_WIDTH // 2 - w // 2
        y = SCREEN_HEIGHT // 2 - h // 2
        pw = self.input_fields["room_password"]
        pw_rect = pygame.Rect(x + 30, y + 70, pw.rect.width, pw.rect.height)
        join_rect = pygame.Rect(x + w - 220, y + h - 70, self.ui_elements["join_room_btn"].rect.width,
                                self.ui_elements["join_room_btn"].rect.height)
        cancel_rect = pygame.Rect(x + w - 220, y + h - 35, self.ui_elements["cancel_join"].rect.width,
                                  self.ui_elements["cancel_join"].rect.height)

        if pw_rect.collidepoint(pos):
            pw.active = True
            return
        if join_rect.collidepoint(pos):
            # attempt join with password
            password = pw.text
            room_id = getattr(self, 'pending_join_room_id', None)
            if not room_id:
                self.state_manager.waiting_message = "No room selected"
                self.state_manager.state = "lobby"
                return
            self.network.send_message({"t": "join_room", "room_id": room_id, "password": password})
            # save password locally for display in waiting room if join succeeds
            self.current_room_password = password
            # clear state and return to lobby/await server response
            pw.text = ""
            pw.active = False
            self.state_manager.state = "lobby"
            return
        if cancel_rect.collidepoint(pos):
            pw.text = ""
            pw.active = False
            self.state_manager.state = "lobby"
            return
        # clicking outside
        pw.active = False

    def _handle_game_click(self, pos: Tuple[int, int]) -> None:
        if getattr(self.state_manager, 'suit_picker_active', False):
            suits = ["srdce", "zelen", "zalud", "gula"]
            for i, suit in enumerate(suits):
                btn_rect = pygame.Rect(SCREEN_WIDTH // 2 - 120 + i * 60, SCREEN_HEIGHT // 2, 50, 40)
                if btn_rect.collidepoint(pos):
                    self.network.send_message(
                        {"t": "p", "ci": getattr(self.state_manager, 'suit_picker_card_index', -1), "cs": suit})
                    self.state_manager.suit_picker_active = False
                    return
            return  # Block other interactions while picker is active

        if self.ui_elements["leave_room"].rect.collidepoint(pos):
            self.network.send_message({"t": "leave_room"})
        elif (self.state_manager.game_state and
              self.state_manager.local_player == self.state_manager.game_state.get("current_player", -1)):

            if self.ui_elements["end_turn"].rect.collidepoint(pos):
                self.network.send_message({"t": "et"})
                return

            # Try playing a card
            for i, sprite in enumerate(self.card_sprites[self.state_manager.local_player].sprites()):
                if sprite.rect.collidepoint(pos):
                    if sprite.card.value == 12 and self.rules.get("hornik_changes_suit", True):
                        self.state_manager.suit_picker_active = True
                        self.state_manager.suit_picker_card_index = i
                    else:
                        self.network.send_message({"t": "p", "ci": i})
                    return

            # Map draw pile directly to End Turn
            if self.layout.draw_pile_rect.collidepoint(pos):
                self.network.send_message({"t": "et"})

    def _handle_leaderboard_click(self, pos: Tuple[int, int]) -> None:
        if self.ui_elements["leave_room"].rect.collidepoint(pos):
            self.network.send_message({"t": "leave_room"})

    def handle_key(self, event) -> None:
        """Route keyboard events to active input fields."""
        if self.state_manager.state == "menu":
            handled = self.input_fields["ip"].handle_key(event) or self.input_fields["name"].handle_key(event)
            if handled and event.key == pygame.K_RETURN:
                self._handle_connect()
        elif self.state_manager.state == "create_room":
            # Handle keys for room creation dialog
            if self.input_fields["room_name"].handle_key(event) and event.key == pygame.K_RETURN:
                # move focus to password field
                self.input_fields["room_name"].active = False
                self.input_fields["room_password"].active = True
                return
            if self.input_fields["room_password"].handle_key(event) and event.key == pygame.K_RETURN:
                # submit creation (same as clicking Start Room)
                # reuse code from create dialog
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
                    # remember password locally so creator sees it in waiting room once joined
                    self.current_room_password = self.input_fields["room_password"].text
                self.network.send_message(msg)
                # Clear and return to lobby
                self.input_fields["room_name"].text = ""
                self.input_fields["room_password"].text = ""
                self.input_fields["room_name"].active = False
                self.input_fields["room_password"].active = False
                self.room_is_private = False
                self.state_manager.state = "lobby"
                return
        elif self.state_manager.state == "join_password":
            # allow submitting password with Enter
            if self.input_fields["room_password"].handle_key(event) and event.key == pygame.K_RETURN:
                password = self.input_fields["room_password"].text
                room_id = getattr(self, 'pending_join_room_id', None)
                if not room_id:
                    self.state_manager.waiting_message = "No room selected"
                    self.state_manager.state = "lobby"
                    return
                self.network.send_message({"t": "join_room", "room_id": room_id, "password": password})
                # keep password for showing in waiting room if join is successful
                self.state_manager.current_room_password = password
                self.current_room_password = None
                self.input_fields["room_password"].text = ""
                self.input_fields["room_password"].active = False
                self.state_manager.state = "lobby"
                return
        elif self.state_manager.state == "lobby":
            if self.input_fields["room_name"].handle_key(event) and event.key == pygame.K_RETURN:
                # open the create dialog instead of creating immediately
                self.state_manager.state = "create_room"
                self.input_fields["room_name"].active = True
                return

    def _handle_connect(self) -> None:
        """Validate inputs and connect to the server."""
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
            self.state_manager.waiting_message = "Setting name..."
            running_flag = [True]
            self.network.start_listener(running_flag, self._on_network_message)
            self.network.send_message({"t": "set_name", "name": self.player_name})
        else:
            self.state_manager.waiting_message = f"Failed to connect to {ip}"

    def _create_room(self) -> None:
        """Send room creation request to the server."""
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
        """Process incoming server messages and update state accordingly."""
        self.state_manager.waiting_start = time.time()
        msg_type = message.get("t")

        if msg_type == "lobby_welcome":
            self.state_manager.waiting_message = None

        elif msg_type == "server_disconnected":
            # Server closed connection unexpectedly - return user to menu
            self.state_manager.waiting_message = "Server disconnected. Returned to menu."
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
            self.state_manager.player_names[self.state_manager.local_player] = self.player_name
            if "player_names" in message:
                self.state_manager.player_names = {int(k): v for k, v in message["player_names"].items()}
            self.update_card_sprites()
            self.state_manager.state = "room_waiting"
            self.state_manager.waiting_message = f"Joined room: {self.current_room_name}"
            # populate password into state for waiting-room display (if user entered it during join)
            self.state_manager.current_room_password = getattr(self, 'current_room_password', None)
            # clear local copy now that it's stored in state_manager
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
                players_needed = self.state_manager.num_players - len(self.state_manager.player_names)
                self.state_manager.waiting_message = f"Waiting for {players_needed} more player(s)..."

        elif msg_type == "waiting":
            if "player_names" in message:
                self.state_manager.player_names = {int(k): v for k, v in message["player_names"].items()}
            players_needed = message.get("players_needed", 0)
            self.state_manager.waiting_message = f"Waiting for {players_needed} more player(s)..."

        elif msg_type == "gs":
            if "player_names" in message:
                message["player_names"] = {int(k): v for k, v in message["player_names"].items()}
                self.state_manager.player_names = message["player_names"]
            self.state_manager.num_players = message.get("num_players", len(message.get("players", [])))
            self.state_manager.game_state = message
            self.state_manager.waiting_message = None
            self.state_manager.state = "playing"
            self.update_card_sprites()

        elif msg_type == "go":
            if "player_names" in message:
                self.state_manager.player_names = {int(k): v for k, v in message["player_names"].items()}
            self.state_manager.state = "leaderboard"
            self.state_manager.leaderboard_data = message.get("results", [])
            self.state_manager.leaderboard_start = time.time()
            self.state_manager.game_state = None
            self.card_sprites = {i: pygame.sprite.Group() for i in range(4)}
            self.current_room_id = None
            self.current_room_name = ""

        elif msg_type == "back_to_lobby":
            self.state_manager.state = "lobby"
            self.current_room_id = None
            self.current_room_name = ""
            self.state_manager.player_names = {}
            self.state_manager.waiting_message = "Returned to lobby"
            self.state_manager.current_room_password = None

        elif msg_type == "e":
            self.state_manager.waiting_message = f"Error: {message.get('msg', 'Unknown error')}"

    def update_card_sprites(self) -> None:
        """Rebuild card sprite groups from current game state.

        Use the currently selected card back image for non-local players so the chosen
        design appears on other players' cards as well.
        """
        if not self.state_manager.game_state or self.state_manager.local_player is None:
            return

        num_players = self.state_manager.num_players
        for i in range(num_players):
            if i not in self.card_sprites:
                self.card_sprites[i] = pygame.sprite.Group()
            self.card_sprites[i].empty()

        # Prepare a back-card object using the renderer's current card_back surface
        back_card = None
        if self.renderer.card_back:
            try:
                back_surface = pygame.transform.scale(self.renderer.card_back, (CARD_WIDTH, CARD_HEIGHT))
                back_card = Card("__back__", 0, "")
                back_card.image = back_surface
            except Exception:
                back_card = None

        for i in range(num_players):
            hand = self.state_manager.game_state.get("players", [])[i]
            if not hand:
                continue

            pos_index = (i - self.state_manager.local_player) % num_players
            is_local = (i == self.state_manager.local_player)

            for j, card_data in enumerate(hand):
                card_key = card_data["name"]
                if card_key not in self.card_cache:
                    self.card_cache[card_key] = Card(card_data["name"], card_data["value"], card_data["suit"])
                card = self.card_cache[card_key]

                x, y, angle = self.layout.get_player_position(pos_index, len(hand), j, is_local=is_local)
                if is_local:
                    display_card = card
                else:
                    # Prefer the rendered back_card surface; fall back to a generic cached 'back' Card
                    if back_card:
                        display_card = back_card
                    else:
                        display_card = self.card_cache.get("back", Card("back", 0, ""))
                self.card_sprites[i].add(CardSprite(display_card, x, y, angle))


class MultiRoomClient:
    """Main application class that ties together all game components."""

    def __init__(self):
        pygame.init()
        if not pygame.font.get_init():
            pygame.font.init()

        self.font = pygame.font.SysFont("Times New Roman", 24)
        self.title_font = pygame.font.SysFont("Times New Roman", 36, bold=True)
        self.small_font = pygame.font.SysFont("Times New Roman", 18)
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

        # Preload card images (fronts only)
        suits = ["srdce", "zelen", "zalud", "gula"]
        value_names = {7: "7", 8: "8", 9: "9", 10: "10", 11: "dolnik", 12: "hornik", 13: "kral", 14: "eso"}
        card_names = [f"{value_names[v]}_{s}" for s in suits for v in value_names]
        # Front card art remains the default set; back images are loaded separately from card_backs
        Card.preload_images(card_names)

    def _setup_ui(self) -> None:
        """Initialize all input fields and UI buttons."""
        cx = SCREEN_WIDTH // 2
        cy = SCREEN_HEIGHT // 2

        self.input_fields = {
            "ip": InputField(pygame.Rect(cx - 150, cy - 160, 300, 40), "Server IP", self.font, BUTTON_COLOR, 20),
            "name": InputField(pygame.Rect(cx - 150, cy - 80, 300, 40), "Username", self.font, BUTTON_COLOR, 20),
            "room_name": InputField(pygame.Rect(50, 260, 200, 40), "Room name", self.font, BUTTON_COLOR, 30),
            "room_password": InputField(pygame.Rect(50, 360, 200, 40), "Password (if private)", self.font, BUTTON_COLOR,
                                        30, is_password=True),
        }

        self.ui_elements = {
            "connect": UIElement(pygame.Rect(cx - 75, cy + 10, 150, 40), "Connect", self.font, BUTTON_COLOR),
            "close": UIElement(pygame.Rect(cx - 75, cy + 60, 150, 40), "Close", self.font, BUTTON_COLOR),
            "create": UIElement(pygame.Rect(50, 150, 200, 40), "Create Room", self.font, BUTTON_COLOR),
            "refresh": UIElement(pygame.Rect(50, 200, 200, 40), "Refresh Rooms", self.font, BUTTON_COLOR),
            "disconnect": UIElement(pygame.Rect(10, SCREEN_HEIGHT - 50, 160, 36), "Disconnect", self.font,
                                    BUTTON_COLOR),
            "leave_room": UIElement(pygame.Rect(10, SCREEN_HEIGHT - 50, 130, 36), "Leave Room", self.font,
                                    BUTTON_COLOR),
            "customize": UIElement(
                pygame.Rect(10, SCREEN_HEIGHT - 55, 150, 36),
                "Customize", self.font, CUSTOMIZE_BUTTON_COLOR
            ),
            "lan_server": UIElement(
                pygame.Rect(SCREEN_WIDTH - 210, SCREEN_HEIGHT - 55, 200, 40),
                "Start LAN Server", self.font, LAN_INACTIVE_COLOR,
                hover_color=(180, 80, 80)
            ),
            "public_server": UIElement(
                pygame.Rect(cx - 100, cy + 115, 200, 40),
                "Public Server", self.font, (40, 100, 160),
                hover_color=(60, 140, 220)
            ),
            "end_turn": UIElement(
                pygame.Rect(cx + 60, SCREEN_HEIGHT - 220, 160, 38),
                "Koniec Ťahu", self.font, (180, 80, 40), hover_color=(220, 100, 60)
            ),
            # Create-room dialog buttons
            "start_room": UIElement(pygame.Rect(50, 420, 200, 40), "Start Room", self.font, (0, 150, 0)),
            "cancel_create": UIElement(pygame.Rect(50, 470, 200, 40), "Cancel", self.font, (150, 0, 0)),
            # Join-password dialog buttons
            "join_room_btn": UIElement(pygame.Rect(0, 0, 180, 40), "Join", self.font, (0, 150, 0)),
            "cancel_join": UIElement(pygame.Rect(0, 0, 180, 40), "Cancel", self.font, (150, 0, 0)),
        }

    def run(self) -> None:
        """Main game loop."""
        clock = pygame.time.Clock()

        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    self.event_handler.handle_click(event.pos)
                elif event.type == pygame.KEYDOWN:
                    self.event_handler.handle_key(event)

            # Process network messages
            while not self.network.message_queue.empty():
                self.event_handler._on_network_message(self.network.message_queue.get())

            # Auto-return from leaderboard
            if (self.state_manager.state == "leaderboard" and
                    time.time() - self.state_manager.leaderboard_start > LEADERBOARD_DURATION):
                self.network.send_message({"t": "leave_room"})
                self.state_manager.state = "lobby"
                self.event_handler.current_room_id = None
                self.event_handler.current_room_name = ""
                self.state_manager.leaderboard_data = None
                self.state_manager.game_state = None

            mouse_pos = pygame.mouse.get_pos()
            self._render(mouse_pos)
            pygame.display.flip()
            clock.tick(60)

        self._cleanup()

    def _render(self, mouse_pos: Tuple[int, int]) -> None:
        """Render the current screen based on state."""
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

        elif state == "lobby":
            self.renderer.render_lobby(
                self.renderer.current_background_path,
                self.event_handler.player_name,
                self.input_fields["room_name"],
                self.ui_elements["create"], self.ui_elements["refresh"],
                self.ui_elements["disconnect"],
                self.event_handler.rooms_list,
                self.state_manager.waiting_message,
                self.event_handler.rules
            )
        elif state == "create_room":
            # Render the create-room dialog overlay
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
        elif state == "join_password":
            # render a simple join-password prompt overlay
            # render via event handler where the modal method actually lives
            self.event_handler._render_join_password(mouse_pos)

        elif state == "leaderboard":
            self.renderer.render_leaderboard(
                self.state_manager, mouse_pos,
                self.ui_elements["leave_room"]
            )

        elif state == "customize":
            self.renderer.render_customize(mouse_pos)

    def _cleanup(self) -> None:
        """Clean up resources on exit."""
        self.lan_server.stop()
        self.network.disconnect()
        pygame.quit()


if __name__ == "__main__":
    client = MultiRoomClient()
    client.run()