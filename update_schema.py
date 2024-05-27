import sqlite3

DATABASE_URL = "drafts.db"

def update_schema():
    # Connect to the database
    conn = sqlite3.connect(DATABASE_URL)
    cursor = conn.cursor()

    # Disable foreign keys
    cursor.execute("PRAGMA foreign_keys=off;")

    # Create a new table with the updated schema
    cursor.execute("""
    CREATE TABLE player_limits_new (
        player_id TEXT NOT NULL,
        display_name TEXT,
        drafts_participated INTEGER DEFAULT 0,
        WeekStartDate DATETIME NOT NULL,
        match_one_points INTEGER DEFAULT 0,
        match_two_points INTEGER DEFAULT 0,
        match_three_points INTEGER DEFAULT 0,
        match_four_points INTEGER DEFAULT 0,
        PRIMARY KEY (player_id, WeekStartDate)
    );
    """)

    # Copy data from the old table to the new table
    cursor.execute("""
    INSERT INTO player_limits_new (player_id, display_name, drafts_participated, WeekStartDate, match_one_points, match_two_points, match_three_points, match_four_points)
    SELECT player_id, display_name, drafts_participated, WeekStartDate, match_one_points, match_two_points, match_three_points, match_four_points FROM player_limits;
    """)

    # Drop the old table
    cursor.execute("DROP TABLE player_limits;")

    # Rename the new table to the old table name
    cursor.execute("ALTER TABLE player_limits_new RENAME TO player_limits;")

    # Enable foreign keys
    cursor.execute("PRAGMA foreign_keys=on;")

    # Commit the changes and close the connection
    conn.commit()
    conn.close()

    print("Schema update completed.")

if __name__ == "__main__":
    update_schema()
