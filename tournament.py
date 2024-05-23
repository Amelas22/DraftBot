import random

class Tournament:
    def __init__(self, sign_ups=None, from_state=None):
        if from_state:
            self.__dict__.update(from_state)
        else:
            self.players = {}
            self.round_number = 0
            self.matches = []
            self.config = {
                "total_rounds": 3,
                "points_for_win": 1,
                "points_for_loss": 0,
            }
            for user_id, display_name in sign_ups.items():
                self.add_player(user_id, display_name)
            self.initial_pairings(sign_ups)

    def add_player(self, user_id, display_name):
        self.players[user_id] = {
            "display_name": display_name,
            "win_points": 0,
            "opponents": []
        }

    def record_match(self, player1_id, player2_id, winner_id):
        self.players[winner_id]['win_points'] += self.config["points_for_win"]
        self.players[player1_id]['opponents'].append(player2_id)
        self.players[player2_id]['opponents'].append(player1_id)
        self.matches.append({
            "player1_id": player1_id,
            "player2_id": player2_id,
            "winner_id": winner_id
        })

    def get_state(self):
        return {
            "players": self.players,
            "round_number": self.round_number,
            "matches": self.matches,
            "config": self.config
        }
    def initial_pairings(self, sign_ups):
        # Pair players according to the specific pattern for the first round
        self.pairings = []
        ids = list(sign_ups.keys())
        for i in range(4):  # 4 pairings for 8 players
            self.pairings.append((ids[i], ids[i+4]))

    def pair_round(self):
        if self.round_number == 0:
            self.round_number += 1
            return self.pairings  # Initial pairings

        self.round_number += 1
        points_groups = {}
        for player_id, details in self.players.items():
            points = details['win_points']
            if points not in points_groups:
                points_groups[points] = []
            points_groups[points].append(player_id)

        pairings = []
        print(f"Pairings for Round {self.round_number}")
        for points, group in sorted(points_groups.items(), key=lambda item: item[0], reverse=True):
            random.shuffle(group)
            group_pairings = []
            if not self.find_pairings(group, group_pairings, set()):
                print(f"Failed to find pairings for group with {points} points: {group}")
            pairings.extend(group_pairings)

        return pairings

    def find_pairings(self, group, group_pairings, used):
        if not group:
            print("All players paired successfully.")
            return True  # All players are paired, valid configuration found

        player1 = group[0]
        print(f"Trying to find pair for {self.players[player1]['display_name']} ({player1})")
        
        for i in range(1, len(group)):
            player2 = group[i]
            if player2 not in self.players[player1]['opponents'] and player2 not in used:
                print(f"Pairing {self.players[player1]['display_name']} ({player1}) with {self.players[player2]['display_name']} ({player2})")
                # Tentatively pair them
                group_pairings.append((player1, player2))
                used.add(player1)
                used.add(player2)
                # Recursively try to pair the rest
                if self.find_pairings([p for p in group if p not in used], group_pairings, used):
                    return True  # Successful pairing configuration
                # Backtrack if not successful
                print(f"Backtracking from pairing {self.players[player1]['display_name']} ({player1}) and {self.players[player2]['display_name']} ({player2})")
                group_pairings.pop()
                used.remove(player1)
                used.remove(player2)

        print(f"No valid pairings found for {self.players[player1]['display_name']} ({player1}) at this path.")
        return False  # No valid pairing configuration found for this entry point