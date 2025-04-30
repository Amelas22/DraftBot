from datetime import datetime
import os
import urllib.parse
import logging
import aiohttp
from typing import Dict, Any, Optional, List

from .digital_ocean_helper import DigitalOceanHelper


class MagicProtoolsHelper:
    """Helper class for interacting with MagicProTools"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.do_helper = DigitalOceanHelper()
        self.api_key = os.getenv("MPT_API_KEY")
        
    def convert_to_magicprotools_format(self, draft_log: Dict[str, Any], user_id: str) -> str:
        """Convert a draft log JSON to MagicProTools format for a specific user."""
        output = []
        
        # Basic draft info
        output.append(f"Event #: {draft_log['sessionID']}_{draft_log['time']}")
        output.append(f"Time: {datetime.fromtimestamp(draft_log['time']/1000).strftime('%a, %d %b %Y %H:%M:%S GMT')}")
        output.append(f"Players:")
        
        # Add player names
        for player_id, user_data in draft_log['users'].items():
            if player_id == user_id:
                output.append(f"--> {user_data['userName']}")
            else:
                output.append(f"    {user_data['userName']}")
        
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
                    
                    # Handle split/double-faced cards
                    if 'back' in draft_log['carddata'][card_id]:
                        back_name = draft_log['carddata'][card_id]['back']['name']
                        card_name = f"{card_name} // {back_name}"
                    
                    # Check if this card was picked
                    if idx in picked_indices:
                        prefix = "--> "
                    else:
                        prefix = "    "
                    
                    output.append(f"{prefix}{card_name}")
                
                output.append("")
        
        return "\n".join(output)
    
    async def submit_to_api(self, user_id: str, draft_data: Dict[str, Any]) -> Optional[str]:
        """
        Submit draft data directly to MagicProTools API
        
        Args:
            user_id: The ID of the user for the draft log
            draft_data: The draft log data
            
        Returns:
            The MagicProTools URL if successful, None otherwise
        """
        session_id = draft_data.get("sessionID", "unknown")
        user_name = draft_data.get("users", {}).get(user_id, {}).get("userName", "unknown")
        
        self.logger.info(f"[MPT] Submitting draft to MagicProTools API for user {user_name} (ID: {user_id}) in session {session_id}")
        
        if not self.api_key:
            self.logger.warning(f"[MPT] Missing MagicProTools API key, cannot submit directly for user {user_name}")
            return None
            
        try:
            # Convert to MagicProTools format
            self.logger.debug(f"[MPT] Converting draft to MagicProTools format for user {user_name}")
            mpt_format = self.convert_to_magicprotools_format(draft_data, user_id)
            
            # Create the API request
            url = "https://magicprotools.com/api/draft/add"
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://draftmancer.com"
            }
            
            # Use proper form data handling in aiohttp
            data = {
                "draft": mpt_format,
                "apiKey": self.api_key,
                "platform": "mtgadraft"
            }
            
            # Log key information
            self.logger.info(f"[MPT] Sending API request to {url} for user {user_name}")
            
            # Make the request using aiohttp's built-in form data handling
            async with aiohttp.ClientSession() as session:
                try:
                    self.logger.debug(f"[MPT] Sending POST request for user {user_name}")
                    async with session.post(url, headers=headers, data=data) as response:
                        self.logger.info(f"[MPT] API response status: {response.status} for user {user_name}")
                        
                        if response.status == 200:
                            json_response = await response.json()
                            
                            if "url" in json_response and not json_response.get("error"):
                                result_url = json_response["url"]
                                self.logger.info(f"[MPT] SUCCESS: Got direct URL for user {user_name}: {result_url}")
                                return result_url
                            else:
                                error = json_response.get("error", "Unknown error")
                                self.logger.warning(f"[MPT] API returned error for user {user_name}: {error}")
                                self.logger.debug(f"[MPT] Full API response: {json_response}")
                                return None
                        else:
                            self.logger.warning(f"[MPT] API returned non-200 status for user {user_name}: {response.status}")
                            try:
                                response_text = await response.text()
                                self.logger.debug(f"[MPT] Response body: {response_text[:200]}...")
                            except Exception as text_err:
                                self.logger.debug(f"[MPT] Could not get response text: {text_err}")
                            return None
                except aiohttp.ClientError as ce:
                    self.logger.error(f"[MPT] HTTP client error for user {user_name}: {ce}")
                    return None
            
            return None  # Return None if unsuccessful
        except Exception as e:
            self.logger.error(f"[MPT] Error submitting to MagicProTools API for user {user_name}: {e}")
            self.logger.debug(f"[MPT] Exception details: {repr(e)}")
            return None
    
    async def upload_draft_logs(
        self, 
        draft_data: Dict[str, Any], 
        session_id: str,
        session_type: str
    ) -> Dict[str, Dict[str, str]]:
        """
        Process the draft log and generate/upload formatted logs for each player
        
        Args:
            draft_data: The draft log data
            session_id: The session ID
            session_type: The session type (e.g., "swiss" or "team")
            
        Returns:
            Dictionary mapping user IDs to their MagicProTools data
        """
        result = {}
        folder = "swiss" if session_type == "swiss" else "team"
        base_path = f"draft_logs/{folder}/{session_id}"
        
        try:
            # Process each user
            for user_id, user_data in draft_data["users"].items():
                user_name = user_data["userName"]
                
                # Convert to MagicProTools format
                mpt_format = self.convert_to_magicprotools_format(draft_data, user_id)
                
                # Create file name for this user's log
                user_filename = f"DraftLog_{user_id}.txt"
                
                # Upload to DO Spaces
                success, _ = await self.do_helper.upload_text(
                    mpt_format,
                    base_path,
                    user_filename
                )
                
                if success:
                    # Get the public URL
                    txt_key = f"{base_path}/{user_filename}"
                    txt_url = self.do_helper.get_public_url(txt_key)
                    
                    # Try direct API submission first
                    mpt_url = None
                    if self.api_key:
                        mpt_url = await self.submit_to_api(user_id, draft_data)
                    
                    # Fallback to import URL if direct submission failed
                    if not mpt_url:
                        import_url = f"https://magicprotools.com/draft/import?url={urllib.parse.quote(txt_url)}"
                        mpt_url = import_url
                    
                    # Store the URLs
                    result[user_id] = {
                        "name": user_name,
                        "txt_url": txt_url,
                        "mpt_url": mpt_url
                    }
                    
                    self.logger.info(f"MagicProTools format log for {user_name} uploaded and processed")
            
            return result
        except Exception as e:
            self.logger.error(f"Error generating and uploading MagicProTools format logs: {e}")
            return result
    
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
                    
                    # Handle split/double-faced cards
                    if 'back' in draft_data['carddata'][card_id]:
                        back_name = draft_data['carddata'][card_id]['back']['name']
                        card_name = f"{card_name} // {back_name}"
                    
                    pack_first_picks[str(pack_num)] = card_name
            
            return pack_first_picks
        except Exception as e:
            # In case of any error, return empty result
            self.logger.error(f"Error getting first picks: {e}")
            return {}