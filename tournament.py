import random

class Tournament:
    def __init__(self, sign_ups):
        self.players = {}
        self.round_number = 0
        # Initialize players
        for user_id, display_name in sign_ups.items():
            self.players[user_id] = {
                'name': display_name,
                'win_points': 0,
                'opponents': []
            }
        self.initial_pairings(sign_ups)

    def initial_pairings(self, sign_ups):
        # Pair players according to the specific pattern for the first round
        self.pairings = []
        ids = list(sign_ups.keys())
        for i in range(4):  # 4 pairings for 8 players
            self.pairings.append((ids[i], ids[i+4]))

    def record_match(self, winner_id, loser_id):
        self.players[winner_id]['win_points'] += 1
        self.players[winner_id]['opponents'].append(loser_id)
        self.players[loser_id]['opponents'].append(winner_id)

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