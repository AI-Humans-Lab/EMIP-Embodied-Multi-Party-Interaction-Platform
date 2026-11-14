async function giveItemToOther(bot, othername, item, num) {
    maxDist = 3

    if (typeof othername !== "string") {
        throw new Error(`othername for giveItemToOther must be a string`);
    }
    if (typeof item !== "string") {
        throw new Error(`item for giveItemToOther must be a string`);
    }
    if (!Number.isInteger(num) || num <= 0) {
        throw new Error(`num for giveItemToOther must be a positive integer`);
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
        throw new Error(`Cannot give an item to myself`);
    }
    other = bot.players[otherMcName]
    if (bot.entity.position.distanceTo(other.entity.position) > maxDist){
        throw new Error(`Player ${otherAgentName} is too far away to receive items`);
    }

    function itemToDict(acc, cur) {
        if (cur.name && cur.count) {
            //if both name and count property are defined
            if (acc[cur.name]) {
                //if the item is already in the dict
                acc[cur.name] += cur.count;
            } else {
                //if the item is not in the dict
                acc[cur.name] = cur.count;
            }
        }
        return acc;
    }

    const itemdict = bot.inventory.items().reduce(itemToDict, {});
    num_in_inventory = itemdict[item]
    if(!num_in_inventory || num_in_inventory < num){
        throw new Error(`Not enough ${item} to give ${otherAgentName}: have ${num_in_inventory || 0}, need ${num}`);
    }
    
    bot.chat(`/clear ${bot.entity.username} ${item} ${num}`)
    bot.chat(`/give ${otherMcName} ${item} ${num}`)
    await think(bot, `I gave ${num} ${item} to ${otherAgentName}.`)

    return true
}
