"""
Sedma Bere Tri - Game Logic
Core card game rules engine: deck management, card playing, drawing, and turn progression.
"""

import random
import logging
from collections import deque
from typing import List, Dict
from card import Card

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)


class Game:
    """Manages the state and rules of a single Sedma Bere Tri game."""

    def __init__(self, num_players: int = 4):
        self.num_players = max(2, int(num_players))
        self.players: List[List[Card]] = [[] for _ in range(self.num_players)]
        self.draw_pile: deque[Card] = deque()
        self.discard_pile: deque[Card] = deque()
        self.current_player: int = 0
        self._card_cache: Dict[str, Card] = {}

    def create_deck(self) -> None:
        """Create and shuffle a standard 32-card deck (7-14 of each suit)."""
        suits = ["♥", "♦", "♣", "♠"]
        values = list(range(7, 15))
        for suit in suits:
            for value in values:
                card_key = f"{value}{suit}"
                if card_key not in self._card_cache:
                    self._card_cache[card_key] = Card(card_key, value, suit)
        self.draw_pile = deque(list(self._card_cache.values()))
        random.shuffle(self.draw_pile)

    def deal_cards(self) -> None:
        """Deal 5 cards to each player and place one card on the discard pile."""
        for _ in range(5):
            for player in self.players:
                if self.draw_pile:
                    player.append(self.draw_pile.popleft())
        if self.draw_pile:
            self.discard_pile.append(self.draw_pile.popleft())

    def play_card(self, player_index: int, card_index: int) -> bool:
        """
        Attempt to play a card from a player's hand.

        Rules:
        - Card must match suit or value of the top discard card, or be a Queen (value 12).
        - Playing a 7 forces the next player to draw 3 cards.
        - Playing an Ace (14) gives the current player another turn.
        """
        if not (0 <= player_index < self.num_players and 0 <= card_index < len(self.players[player_index])):
            logger.error(f"Invalid player {player_index} or card index {card_index}")
            return False

        card = self.players[player_index][card_index]
        top_discard = self.discard_pile[-1] if self.discard_pile else None

        if top_discard and not (card.suit == top_discard.suit or card.value == top_discard.value or card.value == 12):
            logger.error(f"Cannot play {card}: must match {top_discard.suit} or {top_discard.value}")
            return False

        self.discard_pile.append(self.players[player_index].pop(card_index))

        if card.value == 7:
            next_player = self._get_next_active_player()
            for i in range(3):
                if not self.draw_pile:
                    self._refresh_draw_pile()
                if self.draw_pile:
                    self.players[next_player].append(self.draw_pile.popleft())
                else:
                    logger.error(f"Cannot draw card {i + 1} for Player {next_player + 1}: draw pile empty")
                    break
            self.current_player = next_player
        elif card.value == 14:
            self.current_player = self._get_next_active_player()

        if not self.draw_pile and card.value != 7:
            self._refresh_draw_pile()

        self.next_turn()
        return True

    def draw_card(self, player_index: int) -> bool:
        """Draw a card from the draw pile for the specified player."""
        if not (0 <= player_index < self.num_players):
            logger.error(f"Invalid player index {player_index}")
            return False
        if not self.draw_pile:
            self._refresh_draw_pile()
        if self.draw_pile:
            self.players[player_index].append(self.draw_pile.popleft())
            return True
        logger.error("Draw pile is empty")
        return False

    def _refresh_draw_pile(self) -> None:
        """Shuffle the discard pile (except top card) back into the draw pile."""
        if len(self.discard_pile) <= 1:
            logger.error("Not enough cards to refresh draw pile")
            return
        top_card = self.discard_pile[-1]
        cards_to_shuffle = list(self.discard_pile)[:-1]
        random.shuffle(cards_to_shuffle)
        self.draw_pile = deque(cards_to_shuffle)
        self.discard_pile = deque([top_card])

    def _get_next_active_player(self) -> int:
        """Find the next player who still has cards."""
        next_player = (self.current_player + 1) % self.num_players
        for _ in range(self.num_players):
            if self.players[next_player]:
                return next_player
            next_player = (next_player + 1) % self.num_players
        return next_player

    def next_turn(self) -> None:
        """Advance to the next active player's turn."""
        self.current_player = self._get_next_active_player()

    def check_game_over(self) -> bool:
        """Check if the game is over (1 or fewer players have cards)."""
        return sum(1 for player in self.players if player) <= 1

    def serialize(self) -> dict:
        """Serialize the game state for network transmission."""
        return {
            "num_players": self.num_players,
            "players": [
                [{"name": card.name, "value": card.value, "suit": card.suit} for card in hand]
                for hand in self.players
            ],
            "draw_pile_count": len(self.draw_pile),
            "discard_pile": [
                {"name": card.name, "value": card.value, "suit": card.suit}
                for card in self.discard_pile
            ],
            "current_player": self.current_player
        }