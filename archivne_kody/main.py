import pygame
from game_logic import Game

pygame.init()

SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 720

screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
pygame.display.set_caption("Sedma berie tri")

background_image = pygame.image.load("../assets/backgrounds/background_green.png")
card_back_image = pygame.image.load("../assets/cards/default/back.png")

HIGHLIGHT_COLOR = (255, 0, 0)

game = Game()
game.create_deck()
game.deal_cards()

def is_card_clicked(card_rect, mouse_pos):
    return card_rect.collidepoint(mouse_pos)

def draw_player_cards(screen, player_hand, player_index, current_player):
    num_cards = len(player_hand)
    for j, card in enumerate(player_hand):
        if player_index == 0:  # Hráč 1 (hore)
            x = SCREEN_WIDTH // 2 - (num_cards * 40) + (j * 84)
            y = 30
            angle = 0
        elif player_index == 1:  # Hráč 2 (vpravo)
            x = SCREEN_WIDTH - 172
            y = SCREEN_HEIGHT // 2 - (num_cards * 40) + (j * 84)
            angle = -90
        elif player_index == 2:  # Hráč 3 (dole)
            x = SCREEN_WIDTH // 2 - (num_cards * 40) + (j * 84)
            y = SCREEN_HEIGHT - 172
            angle = 180
        elif player_index == 3:  # Hráč 4 (vľavo)
            x = 30
            y = SCREEN_HEIGHT // 2 - (num_cards * 40) + (j * 84)
            angle = 90

        if player_index == 1 or player_index == 3:
            card_rect = pygame.Rect(x - 3, y - 3, 148, 86)
        else:
            card_rect = pygame.Rect(x - 3, y - 3, 86, 148)

        mouse_pos = pygame.mouse.get_pos()
        if player_index == current_player and card_rect.collidepoint(mouse_pos):
            pygame.draw.rect(screen, (0, 0, 0), card_rect, 3)

        rotated_card = pygame.transform.rotate(card.image, angle)
        rotated_rect = rotated_card.get_rect(center=(x + rotated_card.get_width() // 2, y + rotated_card.get_height() // 2))

        screen.blit(rotated_card, rotated_rect.topleft)
        yield card_rect


def draw_player_indicator(screen, player_index):
    if player_index == 0:  # Hráč 1 (hore)
        pygame.draw.polygon(screen, HIGHLIGHT_COLOR, [
            (SCREEN_WIDTH // 2, 10),
            (SCREEN_WIDTH // 2 - 15, 25),
            (SCREEN_WIDTH // 2 + 15, 25)
        ])
    elif player_index == 1:  # Hráč 2 (vpravo)
        pygame.draw.polygon(screen, HIGHLIGHT_COLOR, [
            (SCREEN_WIDTH - 10, SCREEN_HEIGHT // 2),
            (SCREEN_WIDTH - 25, SCREEN_HEIGHT // 2 - 15),
            (SCREEN_WIDTH - 25, SCREEN_HEIGHT // 2 + 15)
        ])
    elif player_index == 2:  # Hráč 3 (dole)
        pygame.draw.polygon(screen, HIGHLIGHT_COLOR, [
            (SCREEN_WIDTH // 2, SCREEN_HEIGHT - 10),
            (SCREEN_WIDTH // 2 - 15, SCREEN_HEIGHT - 25),
            (SCREEN_WIDTH // 2 + 15, SCREEN_HEIGHT - 25)
        ])
    elif player_index == 3:  # Hráč 4 (vľavo)
        pygame.draw.polygon(screen, HIGHLIGHT_COLOR, [
            (10, SCREEN_HEIGHT // 2),
            (25, SCREEN_HEIGHT // 2 - 15),
            (25, SCREEN_HEIGHT // 2 + 15)
        ])

running = True
while running:
    screen.blit(background_image, (0, 0))

    draw_player_indicator(screen, game.current_player)

    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

        if event.type == pygame.MOUSEBUTTONDOWN:
            mouse_pos = event.pos
            current_player = game.players[game.current_player]

            for i, card_rect in enumerate(draw_player_cards(screen, current_player, game.current_player, game.current_player)):
                if card_rect.collidepoint(mouse_pos):
                    if not game.play_card(game.current_player, i):
                        pass
                    if not current_player:
                        print(f"Hráč {game.current_player + 1} vypadol z hry!")
                    break

            draw_pile_rect = pygame.Rect(SCREEN_WIDTH // 2 - 50, SCREEN_HEIGHT // 2 - 50, 80, 142)
            if draw_pile_rect.collidepoint(mouse_pos):
                print("Hráč si vzal kartu z ťahacieho balíka")
                game.draw_card(game.current_player)
                game.next_turn()
                break

    draw_pile_rect = pygame.Rect(SCREEN_WIDTH // 2 - 53, SCREEN_HEIGHT // 2 - 53, 86, 148)
    mouse_pos = pygame.mouse.get_pos()
    if draw_pile_rect.collidepoint(mouse_pos):
        pygame.draw.rect(screen, (0, 0, 0), draw_pile_rect, 3)
    if game.draw_pile:
        screen.blit(card_back_image, (SCREEN_WIDTH // 2 - 50, SCREEN_HEIGHT // 2 - 50))

    if game.discard_pile:
        game.discard_pile[-1].draw(screen, SCREEN_WIDTH // 2 + 50, SCREEN_HEIGHT // 2 - 50)

    for i, player_hand in enumerate(game.players):
        list(draw_player_cards(screen, player_hand, i, game.current_player))

    if game.check_game_over():
        print("Hra skončila!")
        running = False

    pygame.display.flip()

pygame.quit()
