async function mineBlock(bot, blockPosition, blockName) {
    if (!(blockPosition instanceof Vec3)) {
        throw new Error(`blockPosition for mineBlock must be a Vec3`);
    }
    if (blockName !== null && typeof blockName !== "string") {
        throw new Error(`name for mineBlock must be a string or null`);
    }
    if (!checkValidPosition(blockPosition, bot)){
        throw new Error("blockPosition for mineBlock is out of environment.");
    }
    
    if (blockName !== null) {
        const blockByName = mcData.blocksByName[blockName];
        if (!blockByName) {
            throw new Error(`No block named ${blockName}`);
        }
    }

    await goToPosition(bot, blockPosition, 2);
    const block = bot.blockAt(blockPosition);
    if (!block) {
        throw new Error(`Cannot mine block at ${blockPosition}: no block was found there`);
    }
    if (block.name === "air") {
        throw new Error(`Cannot mine block at ${blockPosition}: found air`);
    }
    if (blockName !== null && block.name !== blockName){
        throw new Error(`Cannot mine ${blockName} at ${blockPosition}: found ${block.name} instead`)
    }

    const minedBlockName = block.name;

    await bot.pathfinder.goto(
        new GoalLookAtBlock(blockPosition, bot.world, {})
    );

    const tool = bot.pathfinder.bestHarvestTool(block);
    if (tool) {
        await bot.equip(tool, "hand");
    }

    // Nearby drops are candidates; proximity alone does not prove their source.
    let targetEntity = null;
    let collected = false;
    let mined = false;
    function onDrop(entity) {
        if (!targetEntity && entity.position.distanceTo(block.position.offset(0.5, 0.5, 0.5)) <= 3.0) {
            targetEntity = entity;
        }
    }
    function onCollect(collector, entity) {
        if (collector.id === bot.entity.id && entity.id === targetEntity?.id) collected = true;
    }
    bot.on("itemDrop", onDrop);
    bot.on("playerCollect", onCollect);
    try {
        await bot.dig(block);
        mined = true;
        for (let ticks = 0; !targetEntity && ticks < 100; ticks++) await bot.waitForTicks(1);
        if (!targetEntity) throw new Error("No nearby item drop was observed");
        if (!collected) await bot.pathfinder.goto(new GoalFollow(targetEntity, 0.5));
        for (let ticks = 0; !collected && ticks < 20; ticks++) await bot.waitForTicks(1);
        if (!collected) throw new Error("Collection of the nearby item drop was not confirmed");
    } catch (err) {
        const status = mined ? "Mining completed, but item collection failed" : "Mining failed";
        throw new Error(`${status} for ${minedBlockName} at ${blockPosition}: ${err?.message ?? String(err)}`);
    } finally {
        bot.removeListener("itemDrop", onDrop);
        bot.removeListener("playerCollect", onCollect);
    }
}
