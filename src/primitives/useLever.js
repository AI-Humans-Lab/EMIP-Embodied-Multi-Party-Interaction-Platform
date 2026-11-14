async function turnOnLever(bot, leverPosition) {
    if (!(leverPosition instanceof Vec3)) {
        throw new Error("leverPosition for turnOnLever must be a Vec3");
    }
    if (!checkValidPosition(leverPosition, bot)){
        throw new Error("leverPosition for turnOnLever is out of environment.");
    }
    await goToPosition(bot, leverPosition);
    const block = bot.blockAt(leverPosition);
    if (!block || block.name !== 'lever') {
        throw new Error(`Lever not found at ${leverPosition}`);
    }
  
    const powered = block.getProperties()?.powered;
    const isOn = powered === true || powered === 'true';
    if (isOn) {
        await think(bot, 'Lever is already on.');
        return;
    }
  
    try {
        await bot.activateBlock(block);
    } catch (err) {
        throw new Error(`Failed to turn on lever at ${leverPosition}: ${err.message}`);
    }
}
  
async function turnOffLever(bot, leverPosition) {
    if (!(leverPosition instanceof Vec3)) {
        throw new Error("leverPosition for turnOffLever must be a Vec3");
    }
    if (!checkValidPosition(leverPosition, bot)){
        throw new Error("leverPosition for turnOffLever is out of environment.");
    }
    await goToPosition(bot, leverPosition);
    const block = bot.blockAt(leverPosition);
    if (!block || block.name !== 'lever') {
        throw new Error(`Lever not found at ${leverPosition}`);
    }
  
    const powered = block.getProperties()?.powered;
    const isOff = powered === false || powered === 'false';
    if (isOff) {
        await think(bot, 'Lever is already off.');
        return;
    }
  
    try {
        await bot.activateBlock(block);
    } catch (err) {
        throw new Error(`Failed to turn off lever at ${leverPosition}: ${err.message}`);
    }
}
