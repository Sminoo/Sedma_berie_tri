import random
import logging
from collections import deque
from typing import List, Dict, Optional
from card import Card

logger = logging.getLogger(__name__)

# Card suits (used as asset suffixes)
SUITS = ["srdce", "zelen", "zalud", "gula"]

# Name mapping for face values
VALUE_NAMES = {
    11: "dolnik",
    12: "hornik",
    13: "kral",
    14: "eso",
}


def card_asset_name(value: int, suit: str) -> str:
    """Return asset name for a value and suit."""
    value_str = VALUE_NAMES.get(value, str(value))
    return f"{value_str}_{suit}"


class Game:
    """Manage game state and rules for Sedma Bere Tri."""

    def __init__(self, num_players: int = 4, rules: dict = None):
        self.num_players = max(2, int(num_players))
        self.rules = rules or {}
        self.players: List[List[Card]] = [[] for _ in range(self.num_players)]
        self.draw_pile: deque[Card] = deque()
        self.discard_pile: deque[Card] = deque()
        self.current_player: int = 0
        self._card_cache: Dict[str, Card] = {}
        # Active player slots (players receiving cards)
        self.active_slots: set = set(range(self.num_players))

        # Turn/penalty state
        self.seven_penalty_count: int = 0
        self.ace_penalty_active: bool = False
        self.cards_played_this_turn: int = 0
        self.last_played_value_in_turn: Optional[int] = None
        self.chosen_suit: Optional[str] = None
        # Green jack played in this turn
        self.green_jack_played: bool = False
        # Skip next player flag
        self.skip_next_player: bool = False

    def create_deck(self) -> None:
        """Create and shuffle a 32-card deck (values 7-14 for each suit)."""
        for suit in SUITS:
            for value in range(7, 15):
                card_key = card_asset_name(value, suit)
                if card_key not in self._card_cache:
                    self._card_cache[card_key] = Card(card_key, value, suit)
        self.draw_pile = deque(list(self._card_cache.values()))
        random.shuffle(self.draw_pile)

    def deal_cards(self, hand_sizes: dict = None) -> None:
        """Deal cards to players and place one card onto discard pile.

        hand_sizes: optional dict {player_index: card_count}. Default is 5.
        Slots with 0 cards are spectators/absent.
        """
        default = 5
        sizes = hand_sizes or {}
        self.active_slots = {i for i in range(self.num_players)
                             if sizes.get(i, default) > 0}
        max_cards = max((sizes.get(i, default) for i in range(self.num_players)), default=default)
        for round_card in range(max_cards):
            for i, player in enumerate(self.players):
                limit = sizes.get(i, default)
                if round_card < limit and self.draw_pile:
                    player.append(self.draw_pile.popleft())
        if self.draw_pile:
            self.discard_pile.append(self.draw_pile.popleft())

    def play_card(self, player_index: int, card_index: int, new_suit: str = None) -> bool:
        """Attempt to play a card from a player's hand. Return True on success."""
        if player_index != self.current_player:
            return False
        if not (0 <= player_index < self.num_players and 0 <= card_index < len(self.players[player_index])):
            return False

        card = self.players[player_index][card_index]
        top_discard = self.discard_pile[-1] if self.discard_pile else None

        is_incoming_seven = (self.seven_penalty_count > 0 and self.cards_played_this_turn == 0)
        is_incoming_ace = (self.ace_penalty_active and self.cards_played_this_turn == 0)

        # If green jack was played earlier in this turn, no more plays allowed
        if self.green_jack_played:
            return False

        # Multiple cards in one turn checks
        if self.cards_played_this_turn > 0:
            if not self.rules.get("play_multiple_cards", True):
                return False
            if card.value != self.last_played_value_in_turn:
                return False
            if self.last_played_value_in_turn == 7 and card.value != 7:
                return False
        else:
            # First card of the turn — rule checks
            if is_incoming_seven:
                valid = False
                if card.value == 7 and self.rules.get("stack_sevens", True):
                    valid = True
                elif card.value == 11 and card.suit == 'zelen' and self.rules.get("zeleny_niznik_prebija_sedmu", True):
                    valid = True
                if not valid:
                    return False
            elif is_incoming_ace:
                if not (card.value == 14 and self.rules.get("stack_aces", True)):
                    return False
            else:
                # Normal rules: match suit, value, or hornik (12)
                effective_suit = self.chosen_suit if self.chosen_suit else (top_discard.suit if top_discard else None)
                if top_discard is not None:
                    if not ((card.suit == effective_suit) or (card.value == top_discard.value) or (card.value == 12)):
                        return False

        # Play the card
        self.discard_pile.append(self.players[player_index].pop(card_index))
        self.cards_played_this_turn += 1
        self.last_played_value_in_turn = card.value
        self.chosen_suit = None

        # Card special effects
        if card.value == 7:
            self.seven_penalty_count += 3
        elif card.value == 11 and card.suit == 'zelen' and self.rules.get("zeleny_niznik_prebija_sedmu", True):
            if is_incoming_seven:
                self.seven_penalty_count = 0
                self.green_jack_played = True
        elif card.value == 14:
            self.ace_penalty_active = True
        elif card.value == 12:
            if self.rules.get("hornik_changes_suit", True) and new_suit in SUITS:
                self.chosen_suit = new_suit

        return True

    def end_turn(self, player_index: int) -> bool:
        """End the player's turn — apply penalties or draw a card."""
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

        # Advance to next active player
        self.current_player = self._get_next_active_player()

        # Skip next player if flag set
        if self.skip_next_player:
            self.skip_next_player = False
            self.current_player = self._get_next_active_player()

        return True

    def _draw_single(self, player_index: int) -> None:
        """Draw a single card from the deck."""
        if not self.draw_pile:
            self._refresh_draw_pile()
        if self.draw_pile:
            self.players[player_index].append(self.draw_pile.popleft())

    def _refresh_draw_pile(self) -> None:
        """Shuffle discard pile (except top) back into draw pile."""
        if len(self.discard_pile) <= 1:
            return
        top_card = self.discard_pile[-1]
        cards_to_shuffle = list(self.discard_pile)[:-1]
        random.shuffle(cards_to_shuffle)
        self.draw_pile = deque(cards_to_shuffle)
        self.discard_pile = deque([top_card])

    def _get_next_active_player(self) -> int:
        """Find the next active player who has cards."""
        next_player = (self.current_player + 1) % self.num_players
        for _ in range(self.num_players):
            if self.players[next_player] and next_player in self.active_slots:
                return next_player
            next_player = (next_player + 1) % self.num_players
        return next_player

    def check_game_over(self) -> bool:
        """Check if the game is over (1 or fewer active players)."""
        if not self.active_slots:
            return True
        active_with_cards = sum(
            1 for i, player in enumerate(self.players)
            if player and i in self.active_slots
        )
        return active_with_cards <= 1

    def serialize(self) -> dict:
        """Serialize game state for network transport."""
        return {
            "num_players": self.num_players,
            "players": [
                [{"name": c.name, "value": c.value, "suit": c.suit} for c in hand]
                for hand in self.players
            ],
            "draw_pile_count": len(self.draw_pile),
            "discard_pile": [
                {"name": c.name, "value": c.value, "suit": c.suit}
                for c in self.discard_pile
            ] if self.discard_pile else [],
            "current_player": self.current_player,
            "seven_penalty_count": self.seven_penalty_count,
            "ace_penalty_active": self.ace_penalty_active,
            "cards_played_this_turn": self.cards_played_this_turn,
            "chosen_suit": self.chosen_suit,
            "green_jack_played": self.green_jack_played,
            "skip_next_player": self.skip_next_player,
            "active_slots": list(self.active_slots),
        }