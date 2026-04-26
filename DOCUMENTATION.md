Sedma Bere Tri — Detailná dokumentácia

Obsah
- Prehľad projektu
- Protokol a komunikácia
- Súbory a zodpovedné komponenty
- detailný popis tried a funkcií (client.py, card.py, game_logic.py, server.py, dedicated_server.py)
- Embedding servera v klienskom exe
- Ako zostaviť .exe (PyInstaller)
- Prevádzka dedicated_server.py
- Bezpečnosť, ladenie a odporúčania

Prehľad projektu
Sedma Bere Tri je sieťová kartová hra s grafickým klientom (pygame) a serverom, ktorý podporuje viac miestností (rooms) a režim turnaja. Architektúra je rozdelená na tri vrstvy:
- game_logic.py — čisté herné pravidlá a stav (nezávislé od siete/GUI);
- client.py — GUI, spracovanie vstupu, vykresľovanie a sieťová komunikácia; môže embedovať server pre LAN hru;
- server.py / dedicated_server.py — sieťový server (lokálny / produkčný) spravujúci lobby, miestnosti a distribúciu stavu.

Protokol a komunikácia
- Formát správ: JSON objekt komprimovaný zlib a odoslaný s 4-bajtovým prefixom (sieťový poriadok, !I) označujúcim dĺžku payloadu.
- Tento prístup znižuje veľkosť paketov a udržiava jednoduchú streaming-friendly implementáciu.
- Čítanie/odovzdávanie: prvé 4 bajty určujú veľkosť N, potom sa číta N bajtov, dekomprimuje zlib a parsuje JSON.
- Chybové stavy vracajú správy typu {"t":"e","msg":"..."} alebo internú "server_disconnected" správu.

Súbory a zodpovedné komponenty (stručne)
- client.py: GUI + sieťová logika, triedy Renderer, EventHandler, NetworkManager a LanServerManager.
- card.py: Card (model a cache obrázkov).
- game_logic.py: Game (stavy, pravidlá, deck, rozdávanie, hranie kariet, penalizácie).
- server.py: MultiRoomServer — lobby, GameRoom, RoomManager, MessageHandler; vhodné pre lokálny/embedded režim.
- dedicated_server.py: variant servera so súborovým logovaním, časovými limitmi a rotujúcimi logmi, určený pre verejný hosting.

Detailný popis tried a funkcií

card.py
- Card
  - atribúty: name (str), value (int), suit (str), image (pygame.Surface)
  - triedna premenná: _image_cache (dict) — cache načítaných obrázkov
  - metódy:
    - preload_images(card_names: List[str], theme: str = "default") -> None
      Načíta obrázky do cache. Pri chybe vytvorí náhradnú Surface.
    - draw(screen, x, y) -> None
      Vykreslí image na zadanú pozíciu.

game_logic.py
- Konštanty: SUITS (zoznam farieb), VALUE_NAMES (slovník pre pomenované hodnoty)
- card_asset_name(value, suit) -> str
  - Vráti názov assetu pre danú kartu (napr. "dolnik_zelen").

- Game
  - účel: úplná logika hry (balíčky, rozdávanie, hracie pravidlá, penalizácie, postup hráčov)
  - konštruktor: Game(num_players=4, rules=None)
    - num_players: počet slotov v miestnosti (min. 2)
    - rules: slovník pravidiel, napr. {"stack_sevens": True, "tournament_mode": False}
  - dôležité atribúty:
    - players: List[List[Card]] — ruky hráčov, indexované podľa slotov
    - draw_pile, discard_pile: deque pre ťahanie/odhadzovanie
    - current_player: index hráča, ktorý je na ťahu
    - seven_penalty_count, ace_penalty_active, cards_played_this_turn, last_played_value_in_turn
    - chosen_suit: ak horník (value 12) zmení farbu
    - active_slots: set aktívnych slotov (hráči, ktorí majú karty)
  - metódy:
    - create_deck() -> None
      Naplní _card_cache a zamieša draw_pile (karty hodnoty 7..14 pre každú farbu).
    - deal_cards(hand_sizes: dict = None) -> None
      Rozdá karty podľa hand_sizes (mapa slot -> počet kariet), nastaví active_slots, položí jednu kartu na discard.
    - play_card(player_index: int, card_index: int, new_suit: str = None) -> bool
      Hlavná logika hrania karty: kontrola ťahu, pravidiel stackovania sedmičiek/ás, horník (12) mení farbu, zelený dolník prebíja sedmu, efekty penalizácií. Po úspešnom hraní sa karta presunie na discard_pile a vracajú sa side-effecty (napr. zvýšenie seven_penalty_count).
    - end_turn(player_index: int) -> bool
      Ak hráč nehral kartu, aplikuje penalizácie (ťahanie kariet). Resetuje stav ťahu a posúva current_player na ďalšieho aktívneho hráča.
    - _draw_single(player_index)
      Ťahá jednu kartu z draw_pile; ak draw_pile prázdny, zavolá _refresh_draw_pile().
    - _refresh_draw_pile()
      Zamieša discard okrem vrchnej karty späť do draw_pile.
    - _get_next_active_player() -> int
      Nájde ďalší slot s kartami a aktívnym statusom.
    - check_game_over() -> bool
      Skontroluje, či zostáva ≤1 aktívny hráč.
    - serialize() -> dict
      Prevedie herný stav do JSON-serializovateľnej štruktúry (bez obrazkov), používané serverom/klientom.

client.py (prezentácia hlavných tried)
- CardSprite(pygame.sprite.Sprite)
  - Wrapper okolo Card pre vykreslenie s uhlom a pozíciou.

- CardAnimation
  - Reprezentuje animáciu jednej karty (start->end, trvanie, otočenie). Používa sa v AnimationManager.

- AnimationManager
  - Spravuje front a aktívne animácie (deal, play). Skrýva karty počas animácií (hidden_cards) a volá späť on_card_land / on_deal_complete.

- LayoutManager
  - Vypočítava pozície kariet a UI pre rôzne sedenia hráčov (4 seat layout). Vytvára draw_pile_rect, discard_pile_pos a name_positions.

- NetworkManager
  - Non-blocking TCP client wrapper
  - connect(host) -> bool: vytvorí socket, non-blocking connect, vráti True ak úspešné
  - send_message(message) -> bool: komprimuje a odošle s 4-bajtovým prefixom
  - receive_message(retries=3,...): pokúsi sa prečítať kompletný rámec (4B + payload), dekomprimuje a parsuje JSON
  - start_listener(running_flag, message_callback): spustí vláknový poslucháč (daemon) ktorý ukladá správy do queue

- UIElement
  - Jednoduché tlačidlo s hover efektom a vykreslením textu.

- InputField
  - Textové pole s placeholderom; podporuje password masking a obsluhu kláves.

- StateManager
  - Uchováva aktuálny stav obrazovky (menu, customize, lobby, playing, leaderboard atď.), lokálny slot hráča, posledné leaderboard údaje a ďalšie transientné dáta.

- LanServerManager
  - Zodpovedá za spustenie LAN servera priamo z klienta.
  - Pokusí sa najprv embedovať server importom modulu server a vytvorením MultiRoomServer(..., install_signal_handler=False) spusteného v daemon threade.
  - Ak embedding zlyhá, fallbackne na spustenie server.py ako subprocess (použije CREATE_NO_WINDOW na Windows ak dostupné).
  - stop() bezpečne vypne buď thread server (zatvorením socketu/selectoru), alebo ukončí proces.
  - Dôvod: keď klient zostane single-file exe, spúšťanie externého python procesu spôsobovalo nové okno; embedding tomu zabráni.

- Renderer
  - Vykresľuje všetky obrazovky: menu, public menu, lobby, create room, customize (bez voľby rozlíšenia po úprave), credits, hra, leaderboard.
  - Metódy: render_menu(...), render_public_menu(...), render_lobby(...), render_create_room(...), render_customize(...), render_game(...), render_leaderboard(...), render_credits(...)
  - load_assets(background_path, card_back_path, size): načíta a škáluje pozadie a obrázok zadnej strany kariet.

- EventHandler
  - Mapuje užívateľské kliky a klávesy do akcií. Každý stav má svoj handler (menu, customize, lobby, create_room, playing, atď.).
  - Hlavné metódy: handle_click(pos), handle_key(event), viac interných _handle_<state>_click na spracovanie tlačidiel a polí.
  - Pri zmene návrhového pozadia a motívu kariet volá renderer.load_assets(...).
  - Interaguje s NetworkManager pre odosielanie správ serveru a s AnimationManager pre animácie.

Server-side (server.py a dedicated_server.py)

Poznámka: obidva server súbory majú veľa spoločného; dedicated_server je rozšírená verzia s lepším logovaním a časovačmi.

Spoločné entity:
- Player (dataclass)
  - sock: socket
  - name: str
  - slot: int

- GameRoom
  - room_id, room_name, creator (Player), max_players, rules, is_private, password
  - players: list slotov (Player | None)
  - sockets: set aktívnych soketov v miestnosti
  - game: Optional[Game] — inštancia game_logic.Game počas hrania
  - tournament_mode/pol fieldy: penaltie, eliminácie, round counter
  - finish_order, disconnected, last_game_state, created_at
  - metódy:
    - _add_player(player): pridá hráča do prvého voľného slotu
    - remove_player(sock): odstráni hráča, ak bol v prebiehajúcej hre, odhodí jeho karty a bezpečne ukončí jeho ťah
    - is_empty(), is_full(), can_start_game(manual=False), start_game(manual=False)
    - get_room_info() -> dict: meta info pre lobby
    - is_stale() (dedicated_server): kontrola timeoutov pre cleanup

- RoomManager
  - Spravuje rooms dict a client_rooms mapovanie soket -> room_id
  - create_room(...), join_room(...), leave_room(...), broadcast_to_room(...), end_game(...)
  - end_game: zostaví výsledky, aplikuje turnajové penalizácie, vysiela "go" alebo "tournament_round_over" správy

- LobbyManager
  - Spravuje klientov pred vstupom do miestnosti, broadcast aktualizácií miestností

- MessageHandler
  - Parsuje prijaté správy z klientov v kontexte lobby alebo room a vykonáva akcie (set_name, create_room, join_room, start_game, p=play_card, et=end_turn, atď.)
  - Odosiela chybové správy klientom v prípade nevalidných akcií

- MultiRoomServer
  - Hlavná trieda server.py
  - Iniciuje listening socket, selector, LobbyManager, RoomManager a MessageHandler
  - Accept loop: používa selectors na neblokujúce prijímanie a obsluhu klientov
  - send_message(sock, message): komprimuje a odosiela s prefixom; retry logika pri chybe
  - receive_message(sock): číta rámce a dekomprimuje
  - _handle_client: ak klient odoslal None -> odpoj a cleanup; inak posielaj do MessageHandler
  - server loop tiež spracováva automatické akcie: auto-skip ťahu pri prázdnej ruke, štart ďalšieho turnaja, cleanup prázdnych miestností.
  - Parameter install_signal_handler: ak False (napr. pri embeddingu v klienskom threade), server neregistruje SIGINT handler.

- dedicated_server.py
  - Rozšírenia:
    - setup_logging(): RotatingFileHandler + console logger
    - Konfigurovateľné timeouts (ROOM_TIMEOUT, EMPTY_ROOM_TIMEOUT) a STATS_INTERVAL
    - is_stale() na miestnostiach a automatické odstraňovanie
    - Bezpečnejšie logovanie a robustnejšia správa klientskych chýb

Sieťové typy správ (vybrané)
- Lobby / connection
  - {"t":"lobby_welcome","msg":"..."}
  - {"t":"room_list","rooms":[{room_info},...], "max_rooms": N}
- From client to server (lobby):
  - {"t":"set_name","name": "Player"}
  - {"t":"create_room","room_name":..., "max_players":N, "rules":{...}, "is_private":bool, "password":str}
  - {"t":"join_room","room_id":..., "password":...}
- From client to server (in-room):
  - {"t":"p","ci":card_index, "cs": chosen_suit?} — play card
  - {"t":"et"} — end turn
- From server to clients:
  - {"t":"gs", ...game_state...} — game state (serialize() from Game)
  - {"t":"go", "w":winner, "results": [...]} — game over
  - {"t":"e","msg":"..."} — error message

Ako embedding servera funguje (detailnejšie)
- Klient má LanServerManager.start(), ktorý v prvom kroku skúsi:
  - import server as server_mod
  - vytvorí server_mod.MultiRoomServer(PORT, install_signal_handler=False)
  - spustí server_instance.start() v daemon threade
  - tento spôsob beží server v tom istom procesu, zdieľa adresár s assets, a neotvorí nové okno pri balení do single-file exe
- Ak import alebo spustenie v thread neprejde, použije sa fallback:
  - spustenie externého procesu: python server.py --port PORT (s CREATE_NO_WINDOW na Windows)
- Pri embedded serveri je dôležité install_signal_handler=False, pretože signály je možné registrovať iba v main threade; inak by došlo k chybe.
- Stop() sa pokúsi zatvoriť server.socket a selector, čím sa serverná slučka vyhodí a thread skončí.

Vytvorenie single-file .exe (PyInstaller) — pripomenutie
- PyInstaller príklad:
  pyinstaller --onefile --noconsole --hidden-import=server client.py
- Alternatívy:
  - pridať guarded import `try: import server except: pass` do client.py, aby PyInstaller automaticky zahrnul server.py.
- Testovanie exe:
  - Spustiť exe; v menu spustiť "Start LAN Server"; overiť, že sa nezobrazí nové okno a LAN IP sa zobrazí v menu.

Prevádzka dedicated_server.py
- Spustiť priamo: python dedicated_server.py --port 65432
- Odporúča sa použiť proces manager (systemd, supervisor) alebo Docker.
- Monitorovanie/Logovanie: loguje do rotating server.log + konzola.
- Upraviteľné nastavenia: MAX_ROOMS, MAX_PLAYERS_PER_ROOM, ROOM_TIMEOUT, EMPTY_ROOM_TIMEOUT, STATS_INTERVAL.

Bezpečnosť a vylepšenia (odporúčania)
- Autentifikácia: základné tokeny / registrovaní užívatelia; zabráni impersonácii.
- Šifrovanie: TLS (ssl.wrap_socket) medzi klientom a serverom.
- Dáta: overovanie vstupov (dlžka mien, povolené znaky) a ochrana proti DoS (rate limiting).
- Validácia správ na serveri: striktne kontrolovať typy a rozsahy polí pred použitím.

Ladenie a testovanie
- Unit testy na game_logic.Game: pokryť create_deck, deal_cards, play_card scenáre (sedma, zelený dolník, horník, eso), end_turn, refresh_draw_pile, check_game_over.
- Integračné testy: malý falošný socket server / klient pre testovanie protokolu (4B prefix + zlib).
- Lokálne testovanie client-server: spustiť server.py alebo embedovať server vo client exe a otestovať lobby/room flow.

Ďalšie poznámky
- Kód sa snaží byť robustný voči chybám klientov (odpojenie, nesprávne správy) a automaticky čistí miestnosti bez aktivity.
- Dokumentácia môže byť rozšírená o UML diagramy alebo sequence diagramy pre lifecycle hry -> ak to chceš, môžem ich pridať.

Ak chceš, vygenerujem:
- 1) Detailný API reference (každá verejná metóda + parametre + návratové hodnoty) v samostatnom súbore,
- 2) Testovacie prípady pre Game triedu (pytest),
- 3) PyInstaller .spec optimalizovaný pre bundlovanie assets a server.py.
