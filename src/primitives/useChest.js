async function getItemFromChest(bot, chestPosition, itemsToGet) {
    if (!(chestPosition instanceof Vec3)) {
        throw new Error("chestPosition for getItemFromChest must be a Vec3");
    }
    if (!checkValidPosition(chestPosition, bot)) {
        throw new Error(`chestPosition (${chestPosition}) for getItemFromChest is out of environment (${bot.envBox}).`);
    }
    if (!itemsToGet || typeof itemsToGet !== "object" || Array.isArray(itemsToGet)) {
        throw new Error("itemsToGet must be an object mapping item names to positive integer counts");
    }
    for (const [name, count] of Object.entries(itemsToGet)) {
        if (!mcData.itemsByName[name]) throw new Error(`No item named ${name}`);
        if (!Number.isInteger(count) || count <= 0) throw new Error(`The requested count for ${name} must be a positive integer`);
    }
    const chestBlock = await moveToChest(bot, chestPosition);
    const initialItems = getInventoryDict(bot);
    const chest = await bot.openContainer(chestBlock);
    const gotItems = {};
    let operationError = null;
    try {
        for (const [name, count] of Object.entries(itemsToGet)) {
            const itemByName = mcData.itemsByName[name];
            const item = chest.findContainerItem(itemByName.id);
            if (!item) throw new Error(`No ${name} was found in the chest`);
            try {
                await chest.withdraw(item.type, null, count);
                gotItems[name] = count;
            } catch (err) {
                throw new Error(`Failed to withdraw ${count} ${name}; this transfer may be partial: ${err?.message ?? String(err)}`);
            }
        }
        await bot.waitForTicks(10);
    } catch (err) {
        operationError = new Error(err?.message ?? String(err));
    } finally {
        try {
            await chest.close();
            await bot.waitForTicks(5);
        } catch (err) {
            const closeMessage = `Failed to close chest: ${err?.message ?? String(err)}`;
            operationError = new Error(operationError ? `${operationError.message}; ${closeMessage}` : closeMessage);
        }
    }
    try {
        await restoreInitialInventory(bot, initialItems);
        for (const [name, count] of Object.entries(gotItems)) {
            bot.chat(`/give @s ${name} ${count}`);
            await bot.waitForTicks(1);
        }
    } catch (err) {
        const restoreMessage = `Inventory restoration failed: ${err?.message ?? String(err)}`;
        operationError = new Error(operationError ? `${operationError.message}; ${restoreMessage}` : restoreMessage);
    }
    if (operationError) {
        throw new Error(`${operationError.message}; completed withdraw operations: ${JSON.stringify(gotItems)}`);
    }
}

async function depositItemIntoChest(bot, chestPosition, itemsToDeposit) {
    if (!(chestPosition instanceof Vec3)) {
        throw new Error("chestPosition for depositItemIntoChest must be a Vec3");
    }
    if (!checkValidPosition(chestPosition, bot)) {
        throw new Error(`chestPosition (${chestPosition}) for depositItemIntoChest is out of environment (${bot.envBox}).`);
    }
    if (!itemsToDeposit || typeof itemsToDeposit !== "object" || Array.isArray(itemsToDeposit)) {
        throw new Error("itemsToDeposit must be an object mapping item names to positive integer counts");
    }
    for (const [name, count] of Object.entries(itemsToDeposit)) {
        if (!mcData.itemsByName[name]) throw new Error(`No item named ${name}`);
        if (!Number.isInteger(count) || count <= 0) throw new Error(`The requested count for ${name} must be a positive integer`);
    }
    const chestBlock = await moveToChest(bot, chestPosition);
    const initialItems = getInventoryDict(bot);
    const chest = await bot.openContainer(chestBlock);
    const depositedItems = {};
    let operationError = null;
    try {
        for (const [name, count] of Object.entries(itemsToDeposit)) {
            const itemByName = mcData.itemsByName[name];
            const item = bot.inventory.findInventoryItem(itemByName.id);
            if (!item) throw new Error(`No ${name} was found in the inventory`);
            try {
                await chest.deposit(item.type, null, count);
                depositedItems[name] = count;
            } catch (err) {
                throw new Error(`Failed to deposit ${count} ${name}; this transfer may be partial: ${err?.message ?? String(err)}`);
            }
        }
        await bot.waitForTicks(10);
    } catch (err) {
        operationError = new Error(err?.message ?? String(err));
    } finally {
        try {
            await chest.close();
            await bot.waitForTicks(5);
        } catch (err) {
            const closeMessage = `Failed to close chest: ${err?.message ?? String(err)}`;
            operationError = new Error(operationError ? `${operationError.message}; ${closeMessage}` : closeMessage);
        }
    }
    try {
        await restoreInitialInventory(bot, initialItems);
        for (const [name, count] of Object.entries(depositedItems)) {
            bot.chat(`/clear @s ${name} ${count}`);
            await bot.waitForTicks(1);
        }
    } catch (err) {
        const restoreMessage = `Inventory restoration failed: ${err?.message ?? String(err)}`;
        operationError = new Error(operationError ? `${operationError.message}; ${restoreMessage}` : restoreMessage);
    }
    if (operationError) {
        throw new Error(`${operationError.message}; completed deposit operations: ${JSON.stringify(depositedItems)}`);
    }
}

async function moveToChest(bot, chestPosition) {
    if (!(chestPosition instanceof Vec3)) {
        throw new Error(
            "chestPosition for moveToChest must be a Vec3"
        );
    }
    if (!checkValidPosition(chestPosition, bot)){
        throw new Error("chestPosition for moveToChest is out of environment.");
    }
    await goToPosition(bot, chestPosition, 2);
    const chestBlock = bot.blockAt(chestPosition);
    if (!chestBlock || !["chest", "trapped_chest"].includes(chestBlock.name)) {
        throw new Error(`No chest was found at ${chestPosition}`);
    }
    await bot.pathfinder.goto(
        new GoalLookAtBlock(chestBlock.position, bot.world, {})
    );
    return chestBlock;
}

async function listItemsInChest(bot, chestBlock) {
    const chest = await bot.openContainer(chestBlock);
    const items = chest.containerItems();
    const itemDict = items.reduce((acc, obj) => {
        if (acc[obj.name]) {
            acc[obj.name] += obj.count;
        } else {
            acc[obj.name] = obj.count;
        }
        return acc;
    }, {});
    return itemDict;
}

function itemByName(items, name) {
    for (let i = 0; i < items.length; ++i) {
        const item = items[i];
        if (item && item.name === name) return item;
    }
    return null;
}

function getInventoryDict(bot){
    let inventoryDict = {}
    bot.inventory.items().forEach(item => {
        if(!inventoryDict[item.name]){
            inventoryDict[item.name] = 0
        }
        inventoryDict[item.name] += item.count
    });
    return inventoryDict
}

function itemToObs(item) {
    if (!item) return null;
    return item.name;
}

async function restoreInitialInventory(bot, initialItems) {
    for (let i = 0; i < 36; i++) {
        bot.chat(`/item replace entity @s container.${i} with minecraft:fishing_rod[minecraft:custom_name='{"text":"dummy item"}'] 36`);
    }
    await bot.waitForTicks(1);
    
    for (let i = 0; i < 36; i++) {
        bot.chat(`/item replace entity @s container.${i} with air`);
    }
    await bot.waitForTicks(1);

    for (const name in initialItems) {
        const count = initialItems[name];
        bot.chat(`/give @s ${name} ${count}`);
        await bot.waitForTicks(1);
    }
}
