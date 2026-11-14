async function smeltItem(bot, itemName, fuelName, furnacePos, count = 1) {
    // return if itemName or fuelName is not string
    if (typeof itemName !== "string" || typeof fuelName !== "string") {
        throw new Error("itemName or fuelName for smeltItem must be a string");
    }
    // return if count is not a number
    if (!Number.isInteger(count) || count <= 0) {
        throw new Error("count for smeltItem must be a positive integer");
    }
    if (!(furnacePos instanceof Vec3)) {
        throw new Error("furnacePos for smeltItem must be a Vec3");
    }
    if (!checkValidPosition(furnacePos, bot)){
        throw new Error("furnacePos for smeltItem is out of environment.");
    }
    const item = mcData.itemsByName[itemName];
    const fuel = mcData.itemsByName[fuelName];
    if (!item) {
        throw new Error(`No item named ${itemName}`);
    }
    if (!fuel) {
        throw new Error(`No item named ${fuelName}`);
    }
    await goToPosition(bot, furnacePos, 2);
    const furnaceBlock = bot.blockAt(furnacePos);
    if (!furnaceBlock || !["furnace", "blast_furnace", "smoker"].includes(furnaceBlock.name)) {
        throw new Error(`No furnace was found at ${furnacePos}`);
    }
    await bot.pathfinder.goto(new GoalLookAtBlock(furnaceBlock.position, bot.world));
    const furnace = await bot.openFurnace(furnaceBlock);
    const producedItems = {};
    let completed = 0;
    let operationError = null;
    try {
        if (furnace.inputItem() || furnace.outputItem()) {
            throw new Error("Furnace input and output slots must be empty before smelting");
        }
        for (let i = 0; i < count; i++) {
            if (!bot.inventory.findInventoryItem(item.id, null)) {
                throw new Error(`Not enough ${itemName} in inventory`);
            }
            await furnace.putInput(item.id, null, 1);
            for (let ticks = 0; !furnace.outputItem() && ticks < 30 * 20; ticks += 5) {
                if (!furnace.fuelItem() && !(furnace.fuelSeconds > 0)) {
                    if (!bot.inventory.findInventoryItem(fuel.id, null)) {
                        throw new Error(`No ${fuelName} remains to refuel the furnace; input may still be in the furnace`);
                    }
                    await furnace.putFuel(fuel.id, null, 1);
                }
                await bot.waitForTicks(5);
            }
            if (!furnace.outputItem()) {
                const input = furnace.inputItem()?.name ?? "empty";
                const fuelSlot = furnace.fuelItem()?.name ?? "empty";
                throw new Error(`Smelting output was not observed within 600 ticks; input=${input}, fuel=${fuelSlot}, fuelSeconds=${furnace.fuelSeconds}`);
            }
            const output = await furnace.takeOutput();
            producedItems[output.name] = (producedItems[output.name] || 0) + output.count;
            completed++;
        }
    } catch (err) {
        operationError = new Error(`Failed to smelt ${count} ${itemName}; completed ${completed}: ${err?.message ?? String(err)}`);
    } finally {
        try {
            await furnace.close();
        } catch (err) {
            const closeMessage = `Failed to close furnace: ${err?.message ?? String(err)}`;
            operationError = new Error(operationError ? `${operationError.message}; ${closeMessage}` : closeMessage);
        }
    }
    if (operationError) throw operationError;
    const produced = Object.entries(producedItems).map(([name, amount]) => `${amount} ${name}`).join(", ");
    await think(bot, `Smelted ${itemName} into ${produced}.`);
}
