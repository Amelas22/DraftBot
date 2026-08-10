from datetime import datetime
import os
import urllib.parse
import logging
import aiohttp
from typing import Dict, Any, Optional, List

from .digital_ocean_helper import DigitalOceanHelper
from services.draft_log_store import split_decklist, build_mtgo_deck_text


class MagicProtoolsHelper:
    """Helper class for interacting with MagicProTools"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.do_helper = DigitalOceanHelper()
        self.api_key = os.getenv("MPT_API_KEY")
        
    def extract_deck_token(self, url: Optional[str]) -> Optional[str]:
        """Return the `deck` query-param token from an /api/draft/add result URL, or None."""
        if not url:
            return None
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        tokens = params.get("deck")
        return tokens[0] if tokens else None

    def convert_to_magicprotools_format(self, draft_log: Dict[str, Any], user_id: str, anonymize: bool = False) -> str:
        """Convert a draft log JSON to MagicProTools format for a specific user."""
        output = []

        # Basic draft info
        output.append(f"Event #: {draft_log['sessionID']}_{draft_log['time']}")
        output.append(f"Time: {datetime.fromtimestamp(draft_log['time']/1000).strftime('%a, %d %b %Y %H:%M:%S GMT')}")
        output.append(f"Players:")

        # Add player names
        opponent = 0
        for player_id, user_data in draft_log['users'].items():
            if player_id == user_id:
                name = "Drafter" if anonymize else user_data['userName']
                output.append(f"--> {name}")
            else:
                opponent += 1
                name = f"Player {opponent}" if anonymize else user_data['userName']
                output.append(f"    {name}")
        
        output.append("")
        
        # Determine booster header
        if (draft_log.get('setRestriction') and 
            len(draft_log['setRestriction']) == 1 and
            len([card for card in draft_log['carddata'].values() if card['set'] == draft_log['setRestriction'][0]]) >= 
            0.5 * len(draft_log['carddata'])):
            booster_header = f"------ {draft_log['setRestriction'][0].upper()} ------"
        else:
            booster_header = "------ Cube ------"
        
        # Group picks by pack
        picks = draft_log['users'][user_id]['picks']
        picks_by_pack = {}
        
        for pick in picks:
            pack_num = pick['packNum']
            if pack_num not in picks_by_pack:
                picks_by_pack[pack_num] = []
            picks_by_pack[pack_num].append(pick)
        
        # Sort packs and picks
        for pack_num in picks_by_pack:
            picks_by_pack[pack_num].sort(key=lambda x: x['pickNum'])
        
        # Process each pack
        for pack_num in sorted(picks_by_pack.keys()):
            output.append(booster_header)
            output.append("")
            
            for pick in picks_by_pack[pack_num]:
                output.append(f"Pack {pick['packNum'] + 1} pick {pick['pickNum'] + 1}:")
                
                # Get the picked card indices
                picked_indices = pick['pick']
                
                for idx, card_id in enumerate(pick['booster']):
                    # Get card name
                    card_name = draft_log['carddata'][card_id]['name']
                    
                    # Handle split/double-faced cards. Some card data already
                    # carries the combined "Front // Back" name, so only append
                    # the back face when the name isn't already combined.
                    if 'back' in draft_log['carddata'][card_id]:
                        back_name = draft_log['carddata'][card_id]['back']['name']
                        if back_name and '//' not in card_name:
                            card_name = f"{card_name} // {back_name}"
                    
                    # Check if this card was picked
                    if idx in picked_indices:
                        prefix = "--> "
                    else:
                        prefix = "    "
                    
                    output.append(f"{prefix}{card_name}")
                
                output.append("")
        
        return "\n".join(output)
    
    async def _submit_draft(
        self,
        user_id: str,
        draft_data: Dict[str, Any],
        deck_text: Optional[str] = None,
        anonymize: bool = False,
    ) -> Optional[str]:
        """Shared /api/draft/add POST core. Returns the raw MPT url (which carries
        `&deck=` when deck_text is given), or None on any failure."""
        session_id = draft_data.get("sessionID", "unknown")
        user_name = draft_data.get("users", {}).get(user_id, {}).get("userName", "unknown")
        if not self.api_key:
            self.logger.warning(
                f"[MPT] Missing API key, cannot submit for user {user_name} (session {session_id})"
            )
            return None
        try:
            draft = self.convert_to_magicprotools_format(draft_data, user_id, anonymize=anonymize)
            data = {"draft": draft, "apiKey": self.api_key, "platform": "mtgadraft"}
            if deck_text:
                data["deck"] = deck_text
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://draftmancer.com",
            }
            self.logger.info(
                f"[MPT] Submitting draft for user {user_name} (session {session_id}, "
                f"deck={'yes' if deck_text else 'no'}, anonymize={anonymize})"
            )
            async with aiohttp.ClientSession() as session:
                async with session.post("https://magicprotools.com/api/draft/add",
                                        headers=headers, data=data) as resp:
                    if resp.status != 200:
                        self.logger.warning(
                            f"[MPT] non-200 status {resp.status} for user {user_name} (session {session_id})"
                        )
                        try:
                            self.logger.debug(f"[MPT] Response body: {(await resp.text())[:200]}")
                        except Exception as text_err:
                            self.logger.debug(f"[MPT] Could not read response body: {text_err}")
                        return None
                    body = await resp.json()
            if body.get("error") or "url" not in body:
                self.logger.warning(
                    f"[MPT] bad body for user {user_name}: error={body.get('error')!r} "
                    f"url_present={'url' in body}"
                )
                return None
            result_url = body["url"]
            self.logger.info(f"[MPT] SUCCESS: got url for user {user_name}: {result_url}")
            return result_url
        except Exception as e:
            self.logger.error(f"[MPT] submit failed for user {user_name}: {e}")
            return None

    async def submit_to_api(self, user_id: str, draft_data: Dict[str, Any]) -> Optional[str]:
        """Submit draft data to the MagicProTools API with the player's built deck
        attached (best-effort), so the returned URL opens the draft with that deck.
        Returns the MPT URL if successful, None otherwise."""
        deck_text = None
        try:
            split = split_decklist(draft_data, user_id)
            deck_text = build_mtgo_deck_text(split, draft_data.get("carddata", {})) or None
        except Exception as e:
            self.logger.warning(f"[MPT] deck build failed for user {user_id}, submitting draft-only: {e}")
            deck_text = None
        return await self._submit_draft(user_id, draft_data, deck_text=deck_text, anonymize=False)
    
    async def submit_deck_view(self, user_id: str, draft_data: Dict[str, Any], deck_text: str) -> Optional[str]:
        """Upload the anonymized draft + deck to MPT; return the /deck/show URL or None."""
        url = await self._submit_draft(user_id, draft_data, deck_text=deck_text, anonymize=True)
        if not url:
            return None
        token = self.extract_deck_token(url)
        if not token:
            self.logger.warning(f"[MPT] deck view: no deck token in returned url (user_id: {user_id})")
            return None
        return f"https://magicprotools.com/deck/show?id={token}"

    def get_pack_first_picks(self, draft_data: Dict[str, Any], user_id: str) -> Dict[str, str]:
        """
        Extract the first pick card name for each pack for a specific user
        
        Args:
            draft_data: The draft log data
            user_id: The ID of the user
            
        Returns:
            Dictionary mapping pack numbers to first pick card names
        """
        pack_first_picks = {}
        try:
            # Get user's picks
            user_picks = draft_data['users'][user_id]['picks']
            
            # Find the first pick for each pack
            for pick in user_picks:
                pack_num = pick['packNum']
                pick_num = pick['pickNum']
                
                # Only consider the first pick (pick 0) for each pack
                if pick_num == 0:
                    # Get the picked card indices
                    picked_indices = pick['pick']
                    if not picked_indices:
                        pack_first_picks[str(pack_num)] = "Unknown"
                        continue
                    
                    # Get the card ID and name
                    first_picked_idx = picked_indices[0]
                    card_id = pick['booster'][first_picked_idx]
                    card_name = draft_data['carddata'][card_id]['name']
                    
                    # Handle split/double-faced cards. Skip appending when the
                    # name is already the combined "Front // Back" form.
                    if 'back' in draft_data['carddata'][card_id]:
                        back_name = draft_data['carddata'][card_id]['back']['name']
                        if back_name and '//' not in card_name:
                            card_name = f"{card_name} // {back_name}"
                    
                    pack_first_picks[str(pack_num)] = card_name
            
            return pack_first_picks
        except Exception as e:
            # In case of any error, return empty result
            self.logger.error(f"Error getting first picks: {e}")
            return {}