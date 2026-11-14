async function placeItem(bot, name, position) {
    // return if name is not string
    if (typeof name !== "string") {
        throw new Error(`name for placeItem must be a string`);
    }
    // return if position is not Vec3
    if (!(position instanceof Vec3)) {
        throw new Error(`position for placeItem must be a Vec3`);
    }
    if (!checkValidPosition(position, bot)){
        throw new Error("position for placeItem is out of environment.");
    }
    const itemByName = mcData.itemsByName[name];
    if (!itemByName) {
        throw new Error(`No item named ${name}`);
    }
    const item = bot.inventory.findInventoryItem(itemByName.id);
    if (!item) {
        throw new Error(`No ${name} in inventory`);
    }

    // find a reference block
    const faceVectors = [
        new Vec3(0, 1, 0),
        new Vec3(0, -1, 0),
        new Vec3(1, 0, 0),
        new Vec3(-1, 0, 0),
        new Vec3(0, 0, 1),
        new Vec3(0, 0, -1),
    ];
    let referenceBlock = null;
    let faceVector = null;
    for (const vector of faceVectors) {
        const block = bot.blockAt(position.minus(vector));
        if (block && !["air", "cave_air", "void_air"].includes(block.name)) {
            referenceBlock = block;
            faceVector = vector;
            await think(bot, `Placing ${name} on ${block.name} at ${block.position}`);
            break;
        }
    }
    if (!referenceBlock) {
        throw new Error(`No block to place ${name} on; a floating block cannot be placed`);
    }

    // You must first go to the block position you want to place
    await goToPosition(bot, position)
    await bot.pathfinder.goto(new GoalPlaceBlock(position, bot.world, {}))

    // You must use try catch to placeBlock
    try {
        // You must equip the item right before calling placeBlock
        await bot.equip(item, "hand");
        await bot.placeBlock(referenceBlock, faceVector);
        await think(bot, `Placed ${name}`);
    } catch (err) {
        throw new Error(`Failed to place ${name} at ${position}: ${err?.message}`);
    }
}
