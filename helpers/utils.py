
# Define a mapping of cube names to thumbnail URLs
# This is used for consistent cube thumbnails across the app
CUBE_THUMBNAILS = {
    "LSVCube": "https://cdn.discordapp.com/attachments/1239255966818635796/1348496824956354641/LSVCube.png?ex=67cfad08&is=67ce5b88&hm=16d3350410a3a4c87404c5e6fa1c8ce0408db78a6e841a9133fd69886a1a0db8&",
    "LSVRetro": "https://cdn.discordapp.com/attachments/1239255966818635796/1348496825417470012/LSVRetro.png?ex=67cfad09&is=67ce5b89&hm=8d4d755e1e47993910f06f886f131b2f7930a8fff022db7651ca3e976d1582ce&",
    "AlphaFrog": "https://cdn.discordapp.com/attachments/1097030242507444226/1348723563481530378/585x620-Gavin-Thompson-Exner-2022-Profile-removebg-preview.png?ex=67d08033&is=67cf2eb3&hm=2962b1159ffafce373de1a69e527ffceec86f085453695f3348ee518e3954674&",
    "PowerMack": "https://cdn.discordapp.com/attachments/1097030242507444226/1348717924978004102/mac.png?ex=67d07af3&is=67cf2973&hm=c750d1ce62a06cc0aa0b224119b4d8a04e3c35e2933cb834f819a8a11061e4f8&",
    "Powerslax": "https://media.discordapp.net/attachments/1239255966818635796/1376970727819051028/rof.jpg?ex=6837436c&is=6835f1ec&hm=ef052c0113250a9e6179736a4e257fc81002a0c3824b48054d95555d1b57ef08&=&format=webp"
}

# Default thumbnail for cubes that don't have a specific image
DEFAULT_THUMBNAIL = "https://cdn.discordapp.com/attachments/1186757246936424558/1217295353972527176/131.png"

def get_cube_thumbnail_url(cube_name):
    """Get the thumbnail URL for a given cube name."""
    return CUBE_THUMBNAILS.get(cube_name, DEFAULT_THUMBNAIL)