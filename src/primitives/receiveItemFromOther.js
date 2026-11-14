async function receiveItemFromOther(bot, othername, item, num) {
    maxDist = 3

    if (typeof othername !== "string") {
        throw new Error(`othername for receiveItemFromOther must be a string`);
    }
    if (typeof item !== "string") {
        throw new Error(`item for receiveItemFromOther must be a string`);
    }
    if (!Number.isInteger(num) || num <= 0) {
        throw new Error(`num for receiveItemFromOther must be a positive integer`);
    }

    const otherAgentName = othername;
    if (bot.agentInfo && !Object.hasOwn(bot.agentInfo, otherAgentName)) {
        throw new Error(`Agent ${otherAgentName} is not registered`);
    }
    if (!mcData.itemsByName[item]) {
        throw new Error(`No item named ${item}`);
    }
    const otherMcName = bot.agentInfo ? bot.agentInfo[otherAgentName].mcName : otherAgentName;

    if (!bot.players[otherMcName]?.entity){
        throw new Error(`Player ${otherAgentName} was not found`);
    }
    if (otherMcName === bot.entity.username){
        throw new Error(`Cannot receive an item from myself`);
    }
    other = bot.players[otherMcName]
    if (bot.entity.position.distanceTo(other.entity.position) > maxDist){
        throw new Error(`Player ${otherAgentName} is too far away to give items`);
    }

    const OBJ = `itemQuery`;
    bot.chat(`/scoreboard objectives add ${OBJ} dummy`);
    await bot.waitForTicks(1);

    let hasReset = false;

    let count;
    try {
        count = await new Promise((resolve, reject) => {
            const scoreClient = bot._client;
            const cleanup = () => {
                clearTimeout(to);
                scoreClient.off('scoreboard_score', handler);
            };
            const to = setTimeout(() => {
                cleanup();
                reject(new Error(`Timed out querying ${otherAgentName} inventory for ${item}; no transfer was attempted`));
            }, 10000);

            const handler = (packet) => {
                if (packet.scoreName !== OBJ || packet.itemName !== bot.username || packet.action === 1) return;
                if(!hasReset){
                    if(packet.value === -1){
                        hasReset = true
                    }
                    return;
                }

                cleanup()
                if (!Number.isInteger(packet.value) || packet.value < 0) {
                    reject(new Error(`Invalid inventory count received: ${packet.value}`));
                } else {
                    resolve(packet.value);
                }
            }
            scoreClient.on('scoreboard_score', handler)

            async function queryItemCount() {
                bot.chat(`/scoreboard objectives setdisplay sidebar ${OBJ}`)
                bot.chat(`/scoreboard players set ${bot.username} ${OBJ} -2`) // dummy
                await bot.waitForTicks(1);
                bot.chat(`/scoreboard players set ${bot.username} ${OBJ} -1`) // to reset the board
                await bot.waitForTicks(5);
                bot.chat(`/execute store result score ${bot.username} ${OBJ} run clear ${otherMcName} ${item} 0`) // number of items
            }

            queryItemCount().catch((err) => {
                cleanup();
                reject(err);
            });
        })
    } finally {
        await bot.waitForTicks(1);
        bot.chat(`/scoreboard objectives setdisplay sidebar`)
        bot.chat(`/scoreboard players reset ${bot.username} ${OBJ}`)
    }

    if(count < num){
        throw new Error(`${otherAgentName} does not have enough ${item}: has ${count}, need ${num}`);
    }
    
    await bot.waitForTicks(1);
    bot.chat(`/give ${bot.entity.username} ${item} ${num}`)
    await bot.waitForTicks(1);
    bot.chat(`/clear ${otherMcName} ${item} ${num}`)
    await think(bot, `I received ${num} ${item} from ${otherAgentName}.`)

    await bot.waitForTicks(1);

    return true
}
