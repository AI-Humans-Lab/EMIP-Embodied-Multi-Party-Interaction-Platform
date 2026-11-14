async function craftItem(bot, name, craftingTablePos, count = 1) {
    // return if name is not string
    if (typeof name !== "string") {
        throw new Error("name for craftItem must be a string");
    }
    // return if count is not number
    if (!Number.isInteger(count) || count <= 0) {
        throw new Error("count for craftItem must be a positive integer");
    }
    if (craftingTablePos !== null && !(craftingTablePos instanceof Vec3)) {
        throw new Error("craftingTablePos for craftItem must be a Vec3 or null");
    }
    if (craftingTablePos !== null && !checkValidPosition(craftingTablePos, bot)){
        throw new Error("craftingTablePos for craftItem is out of environment.");
    }
    const itemByName = mcData.itemsByName[name];
    if (!itemByName) {
        throw new Error(`No item named ${name}`);
    }
    
    let craftingTable = null;
    if (craftingTablePos !== null) {
        const block = bot.blockAt(craftingTablePos);
        if (!block) {
            throw new Error(`No crafting table was found at ${craftingTablePos}; no block was found`);
        }
        if (block.name !== "crafting_table") {
            throw new Error(`No crafting table was found at ${craftingTablePos}; found ${block.name}`);
        }
        await goToPosition(bot, craftingTablePos, 2);
        craftingTable = block;
    }
    if (craftingTable) {
        await bot.pathfinder.goto(new GoalLookAtBlock(craftingTable.position, bot.world));
    }

    let craftedCount = 0;
    try {
        for (let i = 0; i < count; i++) {
            const recipe = bot.recipesFor(itemByName.id, null, 1, craftingTable)[0];
            if (!recipe) await failedCraftFeedback(bot, name, itemByName, craftingTable);
            await bot.craft(recipe, 1, craftingTable);
            craftedCount++;
            await bot.waitForTicks(1);
        }
    } catch (err) {
        throw new Error(`Failed to craft ${name} ${count} time(s); completed ${craftedCount}: ${err?.message ?? String(err)}`);
    }
    await think(bot, `I did the recipe for ${name} ${craftedCount} times`);
}
