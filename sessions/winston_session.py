from .base_session import BaseSession
from discord import Embed, Color

class WinstonSession(BaseSession):
    def _create_embed_content(self):
        title = f"Looking for Players - Winston Draft <t:{self.session_details.draft_start_time}:R>"
        description = (
            "**How to use bot**:\n"
            "1. Click sign up and join the draftmancer link (make sure you set up as a Winston Draft).\n"
            "2. You will be notified after someone joins.\n"
            f"{self.get_common_description()}"
        )
        embed = Embed(title=title, description=description, color=Color.brand_red())
        return embed

    def get_session_type(self):
        return "winston"
        
    def get_base_buttons(self, view):
        """Override base buttons for winston drafts."""
        view.add_item(view.create_button("Sign Up", "green", f"sign_up_{view.draft_session_id}", view.sign_up_callback))
        view.add_item(view.create_button("Cancel Sign Up", "red", f"cancel_sign_up_{view.draft_session_id}", view.cancel_sign_up_callback))
        view.add_item(view.create_button("Cancel Draft", "grey", f"cancel_draft_{view.draft_session_id}", view.cancel_draft_callback))
        view.add_item(view.create_button("Remove User", "grey", f"remove_user_{view.draft_session_id}", view.remove_user_button_callback))
