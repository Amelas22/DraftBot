# config.py
import json
import os
from pathlib import Path
import logging

# Your specific guild ID
SPECIAL_GUILD_ID = "336345350535118849"

# Base URL for Draftmancer service
# Change this to switch between environments (prod, beta, dev)
DRAFTMANCER_BASE_URL = "https://draftmancer.com"

# Global flag to enable test features
# Set to True during development to enable test buttons, False for production
TEST_MODE_ENABLED = False

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
                "admin": "Cube Overseer",
                "drafter": "Cube Drafter",  # Default drafter role
                "session_roles": {
                    "winston": "Winston Gamer",
                },
                "timeout": "the pit"
            },
            "timezone": "US/Eastern",
            "external": {
                "cube_url": "https://cubecobra.com/cube/list/"
            },
            "features": {
                "winston_draft": False,
                "voice_channels": False,
                "bot_detection": False,
                "money_server": False,
                "quiz_pack_images": {
                    "enabled": True,
                    "timeout_seconds": 10,
                    "card_width": 244,
                    "card_height": 340,
                    "border_pixels": 5
                }
            },
            "matchmaking": {
                "trueskill_chance": 0  # Default to 0% (always random teams)
            },
            "stakes": {
                "use_optimized_algorithm": True,
                "stake_multiple": 10
            },
            "activity_tracking": {
                "enabled": False,
                "active_role": "Active",
                "exempt_role": "degen",
                "mod_chat_channel": "mod-chat",
                "inactivity_months": 3 
            },
            "timeouts": {
                "queue_inactivity_minutes": 180,      # 3 hours default
                "session_deletion_hours": 4,          # 4 hours default
                "league_challenge_hours": 6,          # 6 hours default
                "premade_draft_days": 7,              # Always 7 days for leagues
                "cleanup_exempt": False,              # Skip cleanup entirely
                "reset_on_signup": True               # Reset timer when users sign up
            },
            "notifications": {
                "dm_notifications_default": True      # DM notifications enabled by default
            }
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
                "drafter": "Cube Drafter",
                "session_roles": {
                    "winston": "Winston Gamer",
                },
                "suspected_bot": "suspected bot"
            },
            "timezone": "US/Eastern",
            "external": {
                "cube_url": "https://cubecobra.com/cube/list/"
            },
            "features": {
                "winston_draft": True,
                "voice_channels": True,
                "bot_detection": True,
                "money_server": False,
                "quiz_pack_images": {
                    "enabled": True,
                    "timeout_seconds": 10,
                    "card_width": 244,
                    "card_height": 340,
                    "border_pixels": 5
                }
            },
            "matchmaking": {
                "trueskill_chance": 60  
            },
            "stakes": {
                "use_optimized_algorithm": True,
                "stake_multiple": 10
            },
            "activity_tracking": {
                "enabled": False,
                "active_role": "Active",
                "exempt_role": "degen",
                "mod_chat_channel": "mod-chat",
                "inactivity_months": 3
            },
            "notifications": {
                "dm_notifications_default": True      # DM notifications enabled by default
            }
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

def is_money_server(guild_id):
    """Helper function to check if this guild is configured for money drafts"""
    config = get_config(guild_id)
    return config.get("features", {}).get("money_server", False)

def get_draftmancer_base_url():
    """Return the draftmancer base URL"""
    return DRAFTMANCER_BASE_URL

def get_draftmancer_session_url(draft_id, guild_id=None):
    """Get full session URL for a draft ID"""
    return f"{DRAFTMANCER_BASE_URL}/?session=DB{draft_id}"

def get_draftmancer_draft_url(draft_id, guild_id=None):
    """Get full draft URL for a draft ID"""
    return f"{DRAFTMANCER_BASE_URL}/draft/DB{draft_id}"

def get_draftmancer_websocket_url(draft_id, guild_id=None, user_id="DraftBot", user_name="DraftBot"):
    """Get full websocket URL for a draft ID"""
    # Convert https:// to wss://
    websocket_url = DRAFTMANCER_BASE_URL.replace('https://', 'wss://')
    return f"{websocket_url}?userID={user_id}&sessionID=DB{draft_id}&userName={user_name}"

def get_timeout_config(guild_id):
    """Get timeout configuration for a guild"""
    config = get_config(guild_id)
    return config.get("timeouts", {
        "queue_inactivity_minutes": 180,      # 3 hours default
        "session_deletion_hours": 4,          # 4 hours default  
        "league_challenge_hours": 6,          # 6 hours default
        "premade_draft_days": 7,              # Always 7 days for leagues
        "cleanup_exempt": False,              # Skip cleanup entirely
        "reset_on_signup": True               # Reset timer when users sign up
    })

def is_cleanup_exempt(guild_id):
    """Check if guild is exempt from cleanup"""
    timeout_config = get_timeout_config(guild_id)
    return timeout_config.get("cleanup_exempt", False)

def should_reset_on_signup(guild_id):
    """Check if deletion timer should reset on signup"""
    timeout_config = get_timeout_config(guild_id)
    return timeout_config.get("reset_on_signup", True)

def get_queue_inactivity_minutes(guild_id):
    """Get queue inactivity timeout in minutes"""
    timeout_config = get_timeout_config(guild_id)
    return timeout_config.get("queue_inactivity_minutes", 180)

def get_session_deletion_hours(guild_id):
    """Get session deletion timeout in hours"""
    timeout_config = get_timeout_config(guild_id)
    return timeout_config.get("session_deletion_hours", 4)

def get_dm_notifications_default(guild_id):
    """Get the default DM notifications setting for a guild"""
    config = get_config(guild_id)
    return config.get("notifications", {}).get("dm_notifications_default", True)

def get_league_challenge_hours(guild_id):
    """Get league challenge timeout in hours"""
    timeout_config = get_timeout_config(guild_id)
    return timeout_config.get("league_challenge_hours", 6)

def get_premade_draft_days(guild_id):
    """Get premade draft timeout in days"""
    timeout_config = get_timeout_config(guild_id)
    return timeout_config.get("premade_draft_days", 7)

def migrate_configs():
    """Ensure all configs have the latest structure."""
    for guild_id, config in bot_config.configs.items():
        updated = False
                
        # Add timeout role if missing
        if "roles" in config and "timeout" not in config["roles"]:
            config["roles"]["timeout"] = "the pit"
            updated = True
            
        # Add timeout configuration if missing
        if "timeouts" not in config:
            # Special handling for test guild - give it longer timeouts and cleanup exemption
            if guild_id == "1229863996929216686":
                config["timeouts"] = {
                    "queue_inactivity_minutes": 10080,   # 7 days
                    "session_deletion_hours": 168,       # 7 days
                    "league_challenge_hours": 168,       # 7 days
                    "premade_draft_days": 7,             # Same as default
                    "cleanup_exempt": True,              # Skip cleanup entirely
                    "reset_on_signup": False             # Don't reset timers
                }
            else:
                # All other guilds get standard defaults
                config["timeouts"] = {
                    "queue_inactivity_minutes": 180,     # 3 hours default
                    "session_deletion_hours": 4,         # 4 hours default
                    "league_challenge_hours": 6,         # 6 hours default
                    "premade_draft_days": 7,             # Always 7 days for leagues
                    "cleanup_exempt": False,             # Skip cleanup entirely
                    "reset_on_signup": True              # Reset timer when users sign up
                }
            updated = True

        # Add notifications configuration if missing
        if "notifications" not in config:
            config["notifications"] = {
                "dm_notifications_default": True  # DM notifications enabled by default
            }
            updated = True

        # Add quiz_pack_images feature if missing
        if "features" in config and "quiz_pack_images" not in config["features"]:
            config["features"]["quiz_pack_images"] = {
                "enabled": True,
                "timeout_seconds": 10,
                "card_width": 244,
                "card_height": 340,
                "border_pixels": 5
            }
            updated = True

        # Save if any updates were made
        if updated:
            bot_config.save_config(guild_id)
            print(f"Updated configuration for guild {guild_id}")