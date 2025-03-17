import json
import os
from pathlib import Path

# Your specific guild ID
SPECIAL_GUILD_ID = "336345350535118849"

class Config:
    def __init__(self):
        # Base configuration for all guilds
        self.default_config = {
            "channels": {
                "draft_results": "team-draft-results"
                # Basic channels only
            },
            "categories": {
                "draft": "Draft Channels"
                # Only basic category for regular guilds
            },
            "roles": {
                "admin": "Cube Overseer"
                # Basic roles only
            },
            "timezone": "US/Eastern",
            "external": {
                "cube_url": "https://cubecobra.com/cube/list/",
                "draft_url": "https://draftmancer.com/?session=DB"
            },
            "features": {
                "winston_draft": False,
                "voice_channels": False,
                "bot_detection": False
            },
            "matchmaking": {
                "trueskill_chance": 0  # Default to 0% (always random teams)
            },
              "stakes": {
                "use_optimized_algorithm": True,
                "use_bottoms_up_algorithm": True,  # Enable the new algorithm by default
                "stake_multiple": 10
            },
        }
        
        # Special configuration just for your guild
        self.special_guild_config = {
            "channels": {
                "draft_results": "team-draft-results",
                "winston_draft": "winston-draft",
                "open_play": "cube-draft-open-play",
                "role_request": "role-request"
            },
            "categories": {
                "draft": "Draft Channels",
                "voice": "Draft Voice"
            },
            "roles": {
                "admin": "Cube Overseer",
                "suspected_bot": "suspected bot"
            },
            "timezone": "US/Eastern",
            "external": {
                "cube_url": "https://cubecobra.com/cube/list/",
                "draft_url": "https://draftmancer.com/?session=DB"
            },
            "features": {
                "winston_draft": True,
                "voice_channels": True,
                "bot_detection": True
            },
            "matchmaking": {
                "trueskill_chance": 60  
            },
            "stakes": {
                "use_optimized_algorithm": False,
                "use_bottoms_up_algorithm": True,  # Enable the new algorithm by default
                "stake_multiple": 10
            },  
        }
        
        self.configs = {}
        self.load_configs()
    
    def load_configs(self):
        config_dir = Path("configs")
        if not config_dir.exists():
            config_dir.mkdir(exist_ok=True)
            
        # Load existing guild configs
        for config_file in config_dir.glob("*.json"):
            try:
                guild_id = config_file.stem
                with open(config_file, "r") as f:
                    self.configs[guild_id] = json.load(f)
            except Exception as e:
                print(f"Error loading config for guild {guild_id}: {e}")
    
    def get_guild_config(self, guild_id):
        guild_id = str(guild_id)
        if guild_id not in self.configs:
            # Use special config for your guild, default for others
            if guild_id == SPECIAL_GUILD_ID:
                self.configs[guild_id] = self.special_guild_config.copy()
            else:
                self.configs[guild_id] = self.default_config.copy()
            self.save_config(guild_id)
        return self.configs[guild_id]
    
    def save_config(self, guild_id):
        guild_id = str(guild_id)
        config_dir = Path("configs")
        if not config_dir.exists():
            config_dir.mkdir(exist_ok=True)
            
        config_path = Path(f"configs/{guild_id}.json")
        with open(config_path, "w") as f:
            json.dump(self.configs[guild_id], f, indent=2)
    
    def update_guild_setting(self, guild_id, path, value):
        guild_id = str(guild_id)
        config = self.get_guild_config(guild_id)
        
        # Prevent changing special features in non-special guilds
        if guild_id != SPECIAL_GUILD_ID and path.startswith("features.") and (
            path == "features.winston_draft" or 
            path == "features.voice_channels" or 
            path == "features.bot_detection"
        ):
            return False
        
        parts = path.split('.')
        current = config
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                current[part] = value
            else:
                if part not in current:
                    current[part] = {}
                current = current[part]
        
        self.save_config(guild_id)
        return True

# Initialize the config
bot_config = Config()

def get_config(guild_id):
    return bot_config.get_guild_config(guild_id)

def save_config(guild_id, config=None):
    if config:
        guild_id = str(guild_id)
        bot_config.configs[guild_id] = config
    bot_config.save_config(guild_id)

def is_special_guild(guild_id):
    """Helper function to check if this is your special guild"""
    return str(guild_id) == SPECIAL_GUILD_ID

def update_setting(guild_id, path, value):
    """Update a specific setting in a guild's config"""
    return bot_config.update_guild_setting(guild_id, path, value)

def migrate_configs():
    """Ensure all configs have the latest structure."""
    for guild_id, config in bot_config.configs.items():
        # Ensure stakes section exists
        if "stakes" not in config:
            config["stakes"] = {
                "use_optimized_algorithm": False, 
                "stake_multiple": 10
            }
            bot_config.save_config(guild_id)
            print(f"Added stakes settings to guild {guild_id}")