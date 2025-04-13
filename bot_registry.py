_BOT_INSTANCE = None

def register_bot(bot):
    """Register the bot instance in the global registry"""
    global _BOT_INSTANCE
    _BOT_INSTANCE = bot

def get_bot():
    """Get the registered bot instance"""
    return _BOT_INSTANCE