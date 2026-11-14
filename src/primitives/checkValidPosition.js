function checkValidPosition(position, bot){
    const envBox = bot.envBox;
    return (envBox[0].x <= position.x && position.x <= envBox[1].x) && (envBox[0].y <= position.y && position.y <= envBox[1].y) && (envBox[0].z <= position.z && position.z <= envBox[1].z)
}