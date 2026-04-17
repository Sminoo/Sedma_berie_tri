"""
Sedma Bere Tri - Game Logic
Core card game rules engine: deck management, card playing, drawing, and turn progression.
"""

import random
import logging
from collections import deque
from typing import List, Dict, Optional
from card import Card

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

SUITS = ["srdce", "zelen", "zalud", "gula"]

VALUE_NAMES = {
    11: "dolnik",
    12: "hornik",
    13: "kral",
    14: "eso",
}


def card_asset_name(value: int, suit: str) -> str:
    """Return the asset filename stem for a given value and suit."""
    value_str = VALUE_NAMES.get(value, str(value))
    return f"{value_str}_{suit}"


class Game:
    """Manages the state and rules of a single Sedma Bere Tri game."""

    def __init__(self, num_players: int = 4, rules: dict = None):
        self.num_players = max(2, int(num_players))
        self.rules = rules or {}
        self.players: List[List[Card]] = [[] for _ in range(self.num_players)]
        self.draw_pile: deque[Card] = deque()
        self.discard_pile: deque[Card] = deque()
        self.current_player: int = 0
        self._card_cache: Dict[str, Card] = {}
        
        # Turn and penalty states
        self.seven_penalty_count: int = 0
        self.ace_penalty_active: bool = False
        self.cards_played_this_turn: int = 0
        self.last_played_value_in_turn: Optional[int] = None
        self.chosen_suit: Optional[str] = None
        # Tracks if green jack was played this turn (blocks further cards)
        self.green_jack_played: bool = False
        # Tracks if the next player should be skipped (green jack counter)
        self.skip_next_player: bool = False

    def create_deck(self) -> None:
        """Create and shuffle a standard 32-card deck (7-14 of each suit)."""
        values = list(range(7, 15))
        for suit in SUITS:
            for value in values:
                card_key = card_asset_name(value, suit)
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

    def play_card(self, player_index: int, card_index: int, new_suit: str = None) -> bool:
        """Attempt to play a card from a player's hand."""
        if player_index != self.current_player:
            logger.error("Not your turn")
            return False

        if not (0 <= player_index < self.num_players and 0 <= card_index < len(self.players[player_index])):
            return False

        card = self.players[player_index][card_index]
        top_discard = self.discard_pile[-1] if self.discard_pile else None

        is_incoming_seven = (self.seven_penalty_count > 0 and self.cards_played_this_turn == 0)
        is_incoming_ace = (self.ace_penalty_active and self.cards_played_this_turn == 0)

        # Block any card after green jack was played this turn
        if self.green_jack_played:
            return False

        if self.cards_played_this_turn > 0:
            if not self.rules.get("play_multiple_cards", True):
                return False
            if card.value != self.last_played_value_in_turn:
                return False
            # After playing a seven, only more sevens are allowed (stacking)
            if self.last_played_value_in_turn == 7 and card.value != 7:
                return False
        else:
            if is_incoming_seven:
                valid_prebitie = False
                if card.value == 7 and self.rules.get("stack_sevens", True):
                    valid_prebitie = True
                elif card.value == 11 and card.suit == 'zelen' and self.rules.get("zeleny_niznik_prebija_sedmu", True):
                    valid_prebitie = True

                if not valid_prebitie:
                    return False
            elif is_incoming_ace:
                if not (card.value == 14 and self.rules.get("stack_aces", True)):
                    return False
            else:
                effective_suit = self.chosen_suit if self.chosen_suit else (top_discard.suit if top_discard else None)
                matches_top = False
                if top_discard is None:
                    matches_top = True
                elif (card.suit == effective_suit) or (card.value == top_discard.value) or (card.value == 12):
                    matches_top = True

                if not matches_top:
                    return False

        # Apply play
        self.discard_pile.append(self.players[player_index].pop(card_index))
        self.cards_played_this_turn += 1
        self.last_played_value_in_turn = card.value
        self.chosen_suit = None

        if card.value == 7:
            # Each 7 played adds 3 to the penalty (whether stacking or starting fresh)
            self.seven_penalty_count += 3
        elif card.value == 11 and card.suit == 'zelen' and self.rules.get("zeleny_niznik_prebija_sedmu", True):
            self.seven_penalty_count = 0
            # Green jack ends the turn immediately – no further cards allowed
            self.green_jack_played = True
    #        self.current_player = self._get_next_active_player()
        elif card.value == 14:
            self.ace_penalty_active = True
        elif card.value == 12:
            if self.rules.get("hornik_changes_suit", True) and new_suit in SUITS:
                self.chosen_suit = new_suit

        return True

    def end_turn(self, player_index: int) -> bool:
        """Ends the turn for the specified player, applying penalties or drawing if necessary."""
        if player_index != self.current_player:
            return False

        if self.cards_played_this_turn == 0:
            if self.seven_penalty_count > 0:
                for _ in range(self.seven_penalty_count):
                    self._draw_single(player_index)
                self.seven_penalty_count = 0
            elif self.ace_penalty_active:
                self.ace_penalty_active = False
            else:
                self._draw_single(player_index)

        self.cards_played_this_turn = 0
        self.last_played_value_in_turn = None
        self.green_jack_played = False

        # Advance to the next active player
        self.current_player = self._get_next_active_player()

        # If green jack was played, skip that player too
        if self.skip_next_player:
            self.skip_next_player = False
            self.current_player = self._get_next_active_player()

        return True

    def _draw_single(self, player_index: int) -> None:
        """Draw a single card."""
        if not self.draw_pile:
            self._refresh_draw_pile()
        if self.draw_pile:
            self.players[player_index].append(self.draw_pile.popleft())

    def _refresh_draw_pile(self) -> None:
        """Shuffle the discard pile (except top card) back into the draw pile."""
        if len(self.discard_pile) <= 1:
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
            ] if self.discard_pile else [],
            "current_player": self.current_player,
            "seven_penalty_count": self.seven_penalty_count,
            "ace_penalty_active": self.ace_penalty_active,
            "cards_played_this_turn": self.cards_played_this_turn,
            "chosen_suit": self.chosen_suit,
            "green_jack_played": self.green_jack_played,
            "skip_next_player": self.skip_next_player,
        }
