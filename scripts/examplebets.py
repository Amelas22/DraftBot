import pandas as pd

# Create sample data
data = {
    'Player Name': [
        'Alice', 
        'Bob', 
        'Charlie', 
        'Dave', 
        'Eve', 
        'Frank', 
        'Grace', 
        'Heidi'
    ],
    'Player Bet': [
        100, 
        75, 
        200, 
        50, 
        120, 
        60, 
        30, 
        80
    ],
    'Cap Player Settings': [
        True,    # Alice wants her bets capped
        False,   # Bob wants full action (uncapped)
        True,    # Charlie wants bets capped
        True,    # Dave wants bets capped
        False,   # Eve wants full action (uncapped)
        True,    # Frank wants bets capped
        True,    # Grace wants bets capped
        False    # Heidi wants full action (uncapped)
    ]
}

# Create DataFrame
df = pd.DataFrame(data)

# Save to Excel
df.to_excel('examplebets.xlsx', index=False)

print("Created examplebets.xlsx with sample data")
print("You can now modify this file to test different scenarios")