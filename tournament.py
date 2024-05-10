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
            return self.pairings  # Return initial pairings for the first round
        
        self.round_number += 1
        # Group players by win points for subsequent rounds
        points_groups = {}
        for player_id, details in self.players.items():
            points = details['win_points']
            if points not in points_groups:
                points_groups[points] = []
            points_groups[points].append(player_id)

        # Generate pairings for the second and third rounds
        pairings = []
        for group in sorted(points_groups.values(), key=len, reverse=True):  # Start pairing from the group with the most players
            random.shuffle(group)
            temp_pairings = []
            while len(group) > 1:
                player1 = group.pop(0)
                for idx, player2 in enumerate(group):
                    if player2 not in self.players[player1]['opponents']:
                        temp_pairings.append((player1, player2))
                        group.pop(idx)
                        break
            pairings.extend(temp_pairings)

        # Handle any unpaired player (if an odd number exists, which shouldn't happen here)
        if len(group) == 1:
            pairings.append((group.pop(), None))  # None signifies a bye

        return pairings