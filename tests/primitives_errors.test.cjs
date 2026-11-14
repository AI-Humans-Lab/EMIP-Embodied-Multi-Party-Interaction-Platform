const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { test } = require('node:test');

const root = path.resolve(__dirname, '..');
class Vec3 {
    constructor(x = 0, y = 0, z = 0) { Object.assign(this, { x, y, z }); }
    floored() { return new Vec3(Math.floor(this.x), Math.floor(this.y), Math.floor(this.z)); }
    clone() { return new Vec3(this.x, this.y, this.z); }
    toString() { return `(${this.x}, ${this.y}, ${this.z})`; }
    minus() { return this; }
    offset(dx = 0, dy = 0, dz = 0) { return new Vec3(this.x + dx, this.y + dy, this.z + dz); }
    distanceTo() { return 0; }
}
const mcData = {
    itemsByName: { wooden_pickaxe: { id: 1 }, cherry_planks: { id: 2 }, oak_planks: { id: 3 }, stick: { id: 4 }, raw_iron: { id: 5 } },
    items: { 2: { name: 'cherry_planks' }, 3: { name: 'oak_planks' }, 4: { name: 'stick' } },
    blocksByName: { crafting_table: { id: 10 } },
};
function context(files, extra = {}) {
    const c = vm.createContext({
        Vec3, mcData, setTimeout, clearTimeout, setInterval, clearInterval, console: { log() {} },
        checkValidPosition: () => true, think: async () => {}, goToPosition: async bot => { bot.testMoved = true; },
        GoalLookAtBlock: class {}, GoalPlaceBlock: class {}, GoalFollow: class {}, GoalNear: class {}, GoalNearXZ: class {}, ...extra,
    });
    for (const file of files) vm.runInContext(fs.readFileSync(path.join(root, 'src/primitives', file + '.js'), 'utf8'), c);
    return c;
}
function basicBot() {
    return Object.assign(new EventEmitter(), {
        entity: { id: 1, position: new Vec3(), username: 'self' },
        waitForTicks: async () => {}, pathfinder: { goto: async () => {}, setGoal() {}, bestHarvestTool: () => null, movements: { exclusionAreasBreak: [] } }, chat() {},
    });
}
const recipe = id => ({ delta: [{ id, count: -3 }, { id: 4, count: -2 }] });

test('Crafting shows alternative wood recipes and sums inventory across stacks', async () => {
    const c = context(['craftHelper']);
    const stacks = [{ id: 2, count: 1 }, { id: 2, count: 2 }];
    const bot = { recipesAll: () => [recipe(2), recipe(3)], inventory: {
        count: id => stacks.filter(item => item.id === id).reduce((sum, item) => sum + item.count, 0),
    } };
    await assert.rejects(c.failedCraftFeedback(bot, 'wooden_pickaxe', { id: 1 }, {}), error => {
        assert.match(error.message, /recipe alternatives/);
        assert.match(error.message, /\(2 stick\) OR \(3 oak_planks, 2 stick\)/);
        assert.doesNotMatch(error.message, /cherry_planks/);
        return true;
    });
});
test('Distinguishes missing recipes from recipes requiring a crafting table', async () => {
    const c = context(['craftHelper']);
    await assert.rejects(c.failedCraftFeedback({ recipesAll: () => [] }, 'stone', { id: 1 }, {}), /No crafting recipe/);
    await assert.rejects(c.failedCraftFeedback({ recipesAll: (id, metadata, table) => table ? [recipe(2)] : [] }, 'pickaxe', { id: 1 }, null), /table is required/);
});
test('Does not imply multiple choices when only one recipe is available', async () => {
    const c = context(['craftHelper']);
    const bot = { recipesAll: () => [recipe(2)], inventory: { count: () => 0 } };
    await assert.rejects(c.failedCraftFeedback(bot, 'wooden_pickaxe', { id: 1 }, {}), error => {
        assert.match(error.message, /missing ingredients: \(3 cherry_planks, 2 stick\)/);
        assert.doesNotMatch(error.message, /choose|alternative/);
        return true;
    });
});
test('Shows three candidate recipes ordered by number of missing material types', async () => {
    const c = context(['craftHelper']);
    const recipes = [
        { delta: [{ id: 2, count: -1 }, { id: 4, count: -1 }] },
        { delta: [{ id: 3, count: -3 }] },
        { delta: [{ id: 2, count: -2 }, { id: 4, count: -2 }] },
        { delta: [{ id: 2, count: -4 }] },
        { delta: [{ id: 3, count: -5 }, { id: 4, count: -1 }] },
    ];
    const bot = { recipesAll: () => recipes, inventory: { count: () => 0 } };
    await assert.rejects(c.failedCraftFeedback(bot, 'wooden_pickaxe', { id: 1 }, {}), error => {
        assert.match(error.message, /\(3 oak_planks\) OR \(4 cherry_planks\) OR \(1 cherry_planks, 1 stick\)/);
        assert.match(error.message, /2 other recipe alternative\(s\) omitted/);
        return true;
    });
});
test('Reselects the recipe for each craft without a crafting table', async () => {
    const c = context(['craftItem']);
    const bot = basicBot();
    const crafted = [];
    let tableArgument;
    bot.recipesFor = (id, metadata, count, table) => { tableArgument = table; return [recipe(crafted.length === 0 ? 2 : 3)]; };
    bot.craft = async recipe => crafted.push(recipe.delta[0].id);
    await c.craftItem(bot, 'wooden_pickaxe', null, 2);
    assert.deepEqual(crafted, [2, 3]);
    assert.equal(tableArgument, null);
});
test('Rejects crafting without moving when the specified position is not a crafting table', async () => {
    const c = context(['craftItem']);
    const bot = basicBot();
    bot.blockAt = () => { assert.equal(bot.testMoved, undefined); return { name: 'stone' }; };
    await assert.rejects(c.craftItem(bot, 'wooden_pickaxe', new Vec3(10, 64, 20)), /No crafting table was found at \(10, 64, 20\); found stone/);
    assert.equal(bot.testMoved, undefined);
});
test('Reports a missing block at the specified crafting table position', async () => {
    const c = context(['craftItem']);
    const bot = basicBot();
    bot.blockAt = () => null;
    await assert.rejects(c.craftItem(bot, 'wooden_pickaxe', new Vec3(10, 64, 20)), /No crafting table was found at \(10, 64, 20\); no block was found/);
    assert.equal(bot.testMoved, undefined);
});
test('Reports that a crafting table is required for recipes that need one', async () => {
    const c = context(['craftHelper', 'craftItem']);
    const bot = basicBot();
    bot.recipesFor = () => [];
    bot.recipesAll = (id, metadata, table) => table ? [recipe(2)] : [];
    await assert.rejects(c.craftItem(bot, 'wooden_pickaxe', null), /Failed to craft wooden_pickaxe 1 time\(s\); completed 0: Cannot craft wooden_pickaxe: a crafting table is required/);
});
test('Reports completed count when repeated crafting fails partway through', async () => {
    const c = context(['craftItem']);
    const bot = basicBot();
    let calls = 0;
    bot.recipesFor = () => [recipe(2)];
    bot.craft = async () => { if (++calls === 2) throw new Error('missing ingredient'); };
    await assert.rejects(c.craftItem(bot, 'wooden_pickaxe', null, 2), /completed 1: missing ingredient/);
});

test('Does not report placement success merely because an item disappeared from inventory', async () => {
    const c = context(['placeItem']);
    const bot = basicBot();
    let reads = 0;
    bot.inventory = { findInventoryItem: () => ++reads === 1 ? { count: 1 } : null };
    bot.blockAt = () => ({ name: 'stone', position: new Vec3() });
    bot.equip = async () => {};
    bot.placeBlock = async () => { throw new Error('server rejected'); };
    await assert.rejects(c.placeItem(bot, 'oak_planks', new Vec3()), /Failed to place.*server rejected/);
});
test('Does not move before checking the support block for placement', async () => {
    const c = context(['placeItem']);
    const bot = basicBot();
    bot.inventory = { findInventoryItem: () => ({ count: 1 }) };
    bot.pathfinder.goto = async () => { throw new Error('placeItem moved unexpectedly'); };
    bot.blockAt = () => null;
    await assert.rejects(c.placeItem(bot, 'oak_planks', new Vec3()), /No block to place/);
});
test('Moves before equipping the item and places it immediately afterward', async () => {
    const calls = [];
    const c = context(['placeItem'], { goToPosition: async () => calls.push('move') });
    const bot = basicBot();
    const item = { name: 'oak_planks' };
    bot.inventory = { findInventoryItem: () => item };
    bot.blockAt = () => ({ name: 'stone', position: new Vec3() });
    bot.pathfinder.goto = async () => calls.push('placementGoal');
    bot.equip = async (selectedItem, destination) => { assert.equal(selectedItem, item); assert.equal(destination, 'hand'); calls.push('equip'); };
    bot.placeBlock = async () => calls.push('placeBlock');
    await c.placeItem(bot, 'oak_planks', new Vec3());
    assert.deepEqual(calls, ['move', 'placementGoal', 'equip', 'placeBlock']);
});
test('Does not equip or place when movement to the placement target fails', async () => {
    const calls = [];
    const c = context(['placeItem'], { goToPosition: async () => { throw new Error('no path'); } });
    const bot = basicBot();
    bot.inventory = { findInventoryItem: () => ({ name: 'oak_planks' }) };
    bot.blockAt = () => ({ name: 'stone', position: new Vec3() });
    bot.equip = async () => calls.push('equip');
    bot.placeBlock = async () => calls.push('placeBlock');
    await assert.rejects(c.placeItem(bot, 'oak_planks', new Vec3()), /no path/);
    assert.deepEqual(calls, []);
});

function furnaceBot() {
    const bot = basicBot();
    let progress = 0;
    let input = null;
    let output = null;
    const furnace = {
        fuelSeconds: 0, fuelAdded: 0, closed: false,
        inputItem: () => input, outputItem: () => output, fuelItem: () => null,
        putInput: async () => { input = { name: 'raw_iron' }; progress = 0; },
        putFuel: async () => { furnace.fuelAdded++; furnace.fuelSeconds = 1.5; },
        takeOutput: async () => { const item = output; output = null; return item; },
        close: async () => { furnace.closed = true; },
    };
    bot.blockAt = () => { assert.equal(bot.testMoved, true); return { name: 'furnace', position: new Vec3() }; };
    bot.inventory = { findInventoryItem: () => ({ count: 64 }) };
    bot.openFurnace = async () => furnace;
    bot.waitForTicks = async ticks => {
        if (furnace.fuelSeconds > 0 && input) {
            progress += ticks;
            furnace.fuelSeconds -= ticks / 20;
            if (progress >= 200) { output = { name: 'iron_ingot', count: 1 }; input = null; }
        }
    };
    return { bot, furnace };
}
test('Refuels during smelting and records actual outputs rather than inventory changes', async () => {
    let message;
    const c = context(['smeltItem'], { think: async (bot, text) => { message = text; } });
    const { bot, furnace } = furnaceBot();
    await c.smeltItem(bot, 'raw_iron', 'oak_planks', new Vec3(), 2);
    assert.ok(furnace.fuelAdded > 2);
    assert.equal(furnace.closed, true);
    assert.match(message, /2 iron_ingot/);
});
test('Reports observed state on smelting timeout without assuming a material problem', async () => {
    const c = context(['smeltItem']);
    const { bot, furnace } = furnaceBot();
    furnace.fuelItem = () => ({ name: 'oak_planks' });
    bot.waitForTicks = async () => {};
    await assert.rejects(c.smeltItem(bot, 'raw_iron', 'oak_planks', new Vec3()), error => {
        assert.match(error.message, /completed 0.*not observed within 600 ticks.*input=raw_iron/);
        assert.doesNotMatch(error.message, /not a valid input/);
        return true;
    });
    assert.equal(furnace.closed, true);
});
test('Preserves the original smelting failure when closing the furnace also fails', async () => {
    const c = context(['smeltItem']);
    const { bot, furnace } = furnaceBot();
    furnace.putInput = async () => { throw new Error('input transfer failed'); };
    furnace.close = async () => { throw new Error('close failed'); };
    await assert.rejects(c.smeltItem(bot, 'raw_iron', 'oak_planks', new Vec3()), /input transfer failed.*close failed/);
});
test('Does not count existing furnace items as results of this smelting operation', async () => {
    const c = context(['smeltItem']);
    const { bot, furnace } = furnaceBot();
    furnace.outputItem = () => ({ name: 'gold_ingot', count: 2 });
    await assert.rejects(c.smeltItem(bot, 'raw_iron', 'oak_planks', new Vec3()), /slots must be empty/);
    assert.equal(furnace.closed, true);
});

test('Removes both event listeners when mining raises an exception', async () => {
    const c = context(['mineBlock']);
    const bot = basicBot();
    bot.blockAt = () => ({ name: 'stone', position: new Vec3() });
    bot.dig = async () => { throw new Error('dig failed'); };
    await assert.rejects(c.mineBlock(bot, new Vec3(), null), /Mining failed.*dig failed/);
    assert.equal(bot.listenerCount('itemDrop'), 0);
    assert.equal(bot.listenerCount('playerCollect'), 0);
});
test('Distinguishes completed mining from failed collection when there are no drops', async () => {
    const c = context(['mineBlock']);
    const bot = basicBot();
    bot.blockAt = () => ({ name: 'stone', position: new Vec3() });
    bot.dig = async () => {};
    await assert.rejects(c.mineBlock(bot, new Vec3(), null), /Mining completed, but item collection failed.*No nearby item drop was observed/);
    assert.equal(bot.listenerCount('itemDrop'), 0);
    assert.equal(bot.listenerCount('playerCollect'), 0);
});
test('Reports only mining completion when collection cannot be confirmed', async () => {
    const c = context(['mineBlock']);
    const bot = basicBot();
    bot.blockAt = () => ({ name: 'stone', position: new Vec3() });
    bot.dig = async () => bot.emit('itemDrop', { id: 2, position: new Vec3() });
    await assert.rejects(c.mineBlock(bot, new Vec3(), null), /Mining completed, but item collection failed/);
});
test('Reports collection success only when the bot picks up the item', async () => {
    const c = context(['mineBlock']);
    const bot = basicBot();
    bot.blockAt = () => ({ name: 'stone', position: new Vec3() });
    bot.dig = async () => {
        const drop = { id: 2, position: new Vec3() };
        bot.emit('itemDrop', drop);
        bot.emit('playerCollect', bot.entity, drop);
    };
    await c.mineBlock(bot, new Vec3(), null);
});
test('Equips the suitable tool after movement and before mining', async () => {
    const calls = [];
    const c = context(['mineBlock'], { goToPosition: async () => calls.push('goToPosition') });
    const bot = basicBot();
    const block = { name: 'stone', position: new Vec3() };
    const tool = { name: 'diamond_pickaxe' };
    bot.blockAt = () => { calls.push('blockAt'); return block; };
    bot.pathfinder.goto = async () => calls.push('goto');
    bot.pathfinder.bestHarvestTool = target => { assert.equal(target, block); calls.push('bestHarvestTool'); return tool; };
    bot.equip = async (selectedTool, destination) => { assert.equal(selectedTool, tool); assert.equal(destination, 'hand'); calls.push('equip'); };
    bot.dig = async () => {
        calls.push('dig');
        const drop = { id: 2, position: new Vec3() };
        bot.emit('itemDrop', drop);
        bot.emit('playerCollect', bot.entity, drop);
    };
    await c.mineBlock(bot, new Vec3(), null);
    assert.deepEqual(calls, ['goToPosition', 'blockAt', 'goto', 'bestHarvestTool', 'equip', 'dig']);
});

for (const [fn, action] of [['getItemFromChest', 'withdraw'], ['depositItemIntoChest', 'deposit']]) {
    test(`${fn} reports completed transfers and the original failure when closing the chest also fails`, async () => {
        const c = context(['useChest']);
        const bot = basicBot();
        bot.inventory = { items: () => [], findInventoryItem: () => ({ type: 4 }) };
        bot.blockAt = () => { assert.equal(bot.testMoved, true); return { name: 'chest', position: new Vec3() }; };
        let calls = 0;
        let opens = 0;
        const chest = {
            findContainerItem: () => ({ type: 4 }),
            [action]: async () => { if (++calls === 2) throw new Error('transfer failed'); },
            close: async () => { throw new Error('close failed'); },
        };
        bot.openContainer = async () => { opens++; return chest; };
        await assert.rejects(c[fn](bot, new Vec3(), { stick: 1, oak_planks: 2 }), error => {
            assert.match(error.message, /transfer failed.*close failed/);
            assert.match(error.message, /"stick":1/);
            return true;
        });
        assert.equal(opens, 1);
    });
}
test('Does not treat a later external state change as a lever operation failure', async () => {
    const c = context(['useLever']);
    for (const [fn, initial, target] of [['turnOnLever', false, true], ['turnOffLever', true, false]]) {
        const bot = basicBot();
        let powered = initial;
        bot.blockAt = () => ({ name: 'lever', getProperties: () => ({ powered }) });
        bot.activateBlock = async () => { powered = target; powered = initial; };
        await c[fn](bot, new Vec3());
    }
});
test('Reports errors for missing levers and failed lever operations', async () => {
    const c = context(['useLever']);
    for (const [fn, initial] of [['turnOnLever', false], ['turnOffLever', true]]) {
        const bot = basicBot();
        bot.blockAt = () => null;
        bot.activateBlock = async () => { throw new Error('activation should not be attempted'); };
        await assert.rejects(c[fn](bot, new Vec3()), /Lever not found/);
        bot.blockAt = () => ({ name: 'lever', getProperties: () => ({ powered: initial }) });
        bot.activateBlock = async () => { throw new Error('server rejected activation'); };
        await assert.rejects(c[fn](bot, new Vec3()), /Failed to turn .*server rejected activation/);
    }
});
test('Reports an error without attempting the operation when the target is unreachable', async () => {
    for (const [file, blockName, run] of [
        ['craftItem', 'crafting_table', (context, bot) => context.craftItem(bot, 'wooden_pickaxe', new Vec3())],
        ['smeltItem', 'furnace', (context, bot) => context.smeltItem(bot, 'raw_iron', 'oak_planks', new Vec3())],
        ['mineBlock', 'stone', (context, bot) => context.mineBlock(bot, new Vec3(), null)],
        ['useChest', 'chest', (context, bot) => context.depositItemIntoChest(bot, new Vec3(), { stick: 1 })],
        ['useChest', 'chest', (context, bot) => context.getItemFromChest(bot, new Vec3(), { stick: 1 })],
    ]) {
        const primitive = context([file]);
        const bot = basicBot();
        bot.blockAt = () => { assert.equal(bot.testMoved, file === 'craftItem' ? undefined : true); return { name: blockName, position: new Vec3() }; };
        bot.pathfinder.goto = async () => { throw new Error('Cannot reach the block'); };
        bot.craft = bot.openFurnace = bot.dig = bot.openContainer = async () => { throw new Error('interaction should not occur'); };
        await assert.rejects(run(primitive, bot), /Cannot reach the block/);
    }
});
test('Reports an explicit error for an invalid movement distance', async () => {
    const c = context(['goToPosition']);
    for (const maxFallbackDistance of [-1, 0.5, 1.5, NaN, Infinity, "1"]) await assert.rejects(c.goToPosition({}, new Vec3(), maxFallbackDistance), /maxFallbackDistance for goToPosition.*integer/);
});
test('Reports an explicit error for an unregistered item transfer partner', async () => {
    const c = context(['giveItemToOther', 'receiveItemFromOther']);
    for (const fn of ['giveItemToOther', 'receiveItemFromOther']) {
        await assert.rejects(c[fn]({ agentInfo: {} }, 'missing', 'stick', 1), /Agent missing is not registered/);
    }
});
test('Reads partner inventory only from the specified scoreboard response', async () => {
    const c = context(['receiveItemFromOther']);
    const bot = basicBot();
    bot.username = 'self';
    bot._client = new EventEmitter();
    bot.agentInfo = { Partner: { mcName: 'partner' } };
    bot.players = { partner: { entity: { position: new Vec3() } } };
    bot.chat = command => {
        if (!command.startsWith('/execute store result')) return;
        bot._client.emit('scoreboard_score', { scoreName: 'otherObjective', itemName: 'self', value: -1 });
        bot._client.emit('scoreboard_score', { scoreName: 'itemQuery', itemName: 'otherPlayer', value: -1 });
        bot._client.emit('scoreboard_score', { scoreName: 'itemQuery', itemName: 'self', value: -1 });
        bot._client.emit('scoreboard_score', { scoreName: 'itemQuery', itemName: 'self', value: 2 });
    };
    await c.receiveItemFromOther(bot, 'Partner', 'stick', 2);
    assert.equal(bot._client.listenerCount('scoreboard_score'), 0);
});
test('Rejects receiving items when the partner inventory count is invalid', async () => {
    const c = context(['receiveItemFromOther']);
    const bot = basicBot();
    bot.username = 'self';
    bot._client = new EventEmitter();
    bot.agentInfo = { Partner: { mcName: 'partner' } };
    bot.players = { partner: { entity: { position: new Vec3() } } };
    const commands = [];
    bot.chat = command => {
        commands.push(command);
        if (!command.startsWith('/execute store result')) return;
        bot._client.emit('scoreboard_score', { scoreName: 'itemQuery', itemName: 'self', value: -1 });
        bot._client.emit('scoreboard_score', { scoreName: 'itemQuery', itemName: 'self', value: -2 });
    };
    await assert.rejects(c.receiveItemFromOther(bot, 'Partner', 'stick', 1), /Invalid inventory count received: -2/);
    assert.equal(commands.some(command => command.startsWith('/give ')), false);
    assert.equal(commands.some(command => command.startsWith('/clear partner stick 1')), false);
    assert.equal(bot._client.listenerCount('scoreboard_score'), 0);
});

test('Awaits asynchronous search predicates and clears the movement goal on pathfinding failure', async () => {
    const c = context(['exploreUntil']);
    const bot = basicBot();
    let cleaned = false;
    bot.pathfinder.setGoal = goal => { if (goal === null) cleaned = true; else throw new Error('path failure'); };
    await assert.rejects(c.exploreUntil(bot, new Vec3(1), 1, async () => false), /path failure/);
    assert.equal(cleaned, true);
});
test('Ends the search at the time limit even when the predicate does not respond', async () => {
    const c = context(['exploreUntil']);
    await assert.rejects(c.exploreUntil(basicBot(), new Vec3(1), 0.01, () => new Promise(() => {})), /Explore timed out/);
});
for (const [fn, plugin] of [['waitForMobRemoved', 'pvp'], ['waitForMobShot', 'hawkEye']]) {
    test(`${fn} times out and removes event listeners even without a completion event`, async () => {
        const c = context(['waitForMobRemoved']);
        const bot = basicBot();
        bot[plugin] = { stop() {} };
        await assert.rejects(c[fn](bot, { id: 2, name: 'zombie' }, 0.01), /Timed out/);
        assert.deepEqual(bot.eventNames(), []);
    });
    test(`${fn} distinguishes target disappearance from confirmed death`, async () => {
        const c = context(['waitForMobRemoved']);
        const bot = basicBot();
        bot[plugin] = { stop() {} };
        const entity = { id: 2, name: 'zombie' };
        const vanished = c[fn](bot, entity);
        bot.emit('entityGone', entity);
        await assert.rejects(vanished, /death was not confirmed/);
        const killed = c[fn](bot, entity);
        bot.emit('entityDead', entity);
        bot.emit('entityGone', entity);
        await killed;
        assert.deepEqual(bot.eventNames(), []);
    });
}
function movementContext(extra = {}) {
    return context(['goToPosition'], {
        GoalNear: class {
            constructor(x, y, z, range) { Object.assign(this, { x, y, z, range }); }
            isEnd(node) { return (node.x - this.x) ** 2 + (node.y - this.y) ** 2 + (node.z - this.z) ** 2 <= this.range ** 2; }
        },
        ...extra,
    });
}
function movementBot() {
    return Object.assign(basicBot(), { easyLlmMovementConfig: { mode: 'normal', moveTimeoutSec: 10 } });
}
test('Floors fractional and negative coordinates without mutating the input and warns once', async () => {
    const messages = [];
    let checked;
    let goal;
    const c = movementContext({ think: async (bot, msg) => messages.push(msg), checkValidPosition: pos => { checked = pos; return true; } });
    const bot = movementBot();
    bot.pathfinder.goto = async value => { goal = value; bot.entity.position = new Vec3(value.x, value.y, value.z); };
    const position = new Vec3(1.8, 64.2, -2.3);
    await c.goToPosition(bot, position, 0);
    assert.deepEqual([position.x, position.y, position.z], [1.8, 64.2, -2.3]);
    assert.deepEqual([goal.x, goal.y, goal.z, goal.range], [1, 64, -3, 0]);
    assert.deepEqual([checked.x, checked.y, checked.z], [1, 64, -3]);
    assert.equal(messages.length, 1);
    assert.match(messages[0], /Warning:.*\(1.8, 64.2, -2.3\).*\(1, 64, -3\)/);
});
test('Does not warn for integer coordinates and widens goal distance starting at zero', async () => {
    const c = movementContext({ think: async () => { throw new Error('unexpected warning'); } });
    const bot = movementBot();
    const radii = [];
    bot.pathfinder.goto = async goal => { radii.push(goal.range); if (goal.range < 2) throw new Error('No path'); bot.entity.position = new Vec3(goal.x, goal.y, goal.z); };
    await c.goToPosition(bot, new Vec3(1, 64, 2), 2);
    assert.deepEqual(radii, [0, 1, 2]);
});
test('Excludes only the goal block from blocks that may be broken during movement', async () => {
    const c = movementContext();
    const bot = movementBot();
    bot.pathfinder.goto = async () => {
        const exclude = bot.pathfinder.movements.exclusionAreasBreak[0];
        assert.equal(exclude({ position: { equals: position => position.x === 1 && position.y === 64 && position.z === 2 } }), Infinity);
        assert.equal(exclude({ position: { equals: () => false } }), 0);
        bot.entity.position = new Vec3(1, 64, 2);
    };
    await c.goToPosition(bot, new Vec3(1, 64, 2), 0);
    assert.deepEqual(bot.pathfinder.movements.exclusionAreasBreak, []);
});
test('Does not fall back to a nearby position when allowed distance is zero', async () => {
    const c = movementContext();
    const bot = movementBot();
    const radii = [];
    bot.pathfinder.goto = async goal => { radii.push(goal.range); throw new Error('No path'); };
    await assert.rejects(c.goToPosition(bot, new Vec3(1, 0, 0), 0), /No path/);
    assert.deepEqual(radii, [0]);
    assert.deepEqual(bot.pathfinder.movements.exclusionAreasBreak, []);
});
test('Rejects invalid coordinates and teleport options before moving', async () => {
    const c = movementContext();
    for (const value of [NaN, Infinity, -Infinity, '2']) {
        for (const position of [new Vec3(value, 0, 0), new Vec3(0, value, 0), new Vec3(0, 0, value)]) {
            await assert.rejects(c.goToPosition({}, position), /position.*finite numbers/);
        }
    }
    await assert.rejects(c.goToPosition({}, new Vec3(), 0, 'false'), /teleportAlongPath.*boolean/);
});
test('Shows the floored goal coordinates in errors for targets outside environment bounds', async () => {
    const c = movementContext({ checkValidPosition: pos => pos.x >= -2 });
    await assert.rejects(c.goToPosition({}, new Vec3(-2.3, 64, 0)), /position \(-3, 64, 0\).*out of environment/);
});
test('Recognizes teleport arrival on blocks with fractional height', () => {
    const c = movementContext();
    const goal = new c.GoalNear(1, 64, 2, 0);
    const bot = basicBot();
    bot.entity.position = new Vec3(1.5, 63, 2.5);
    assert.equal(c.hasReachedGoToPositionGoal(bot, goal), true);
    bot.entity.position = new Vec3(1.5, 64, 2.5);
    assert.equal(c.hasReachedGoToPositionGoal(bot, goal), true);
});
test('Ends on overall movement timeout without increasing allowed distance', async () => {
    const c = movementContext();
    const bot = movementBot();
    const radii = [];
    bot.easyLlmMovementConfig.moveTimeoutSec = 0.01;
    bot.pathfinder.goto = async goal => { radii.push(goal.range); await new Promise(resolve => setTimeout(resolve, 20)); };
    await assert.rejects(c.goToPosition(bot, new Vec3(1, 0, 0), 2), /timed out/);
    assert.deepEqual(radii, [0]);
});
test('Tries the next allowed distance when only pathfinding times out', async () => {
    const c = movementContext();
    const bot = movementBot();
    const radii = [];
    bot.pathfinder.goto = async goal => {
        radii.push(goal.range);
        if (goal.range === 0) bot.emit('path_update', { status: 'timeout', path: [] });
        else bot.entity.position = new Vec3(goal.x, goal.y, goal.z);
    };
    await c.goToPosition(bot, new Vec3(2, 0, 0), 1);
    assert.deepEqual(radii, [0, 1]);
    assert.deepEqual(bot.eventNames(), []);
});
test('Waits for actual arrival even when movement completes with an empty path', async () => {
    const c = movementContext();
    const bot = movementBot();
    let ticks = 0;
    let calls = 0;
    bot.pathfinder.goto = async () => { calls++; bot.emit('path_update', { status: 'partial', path: [] }); };
    bot.waitForTicks = async () => { if (++ticks === 3) bot.entity.position = new Vec3(2, 0, 0); };
    await c.goToPosition(bot, new Vec3(2, 0, 0), 0);
    assert.equal(calls, 1);
    assert.ok(ticks >= 3);
    assert.deepEqual(bot.eventNames(), []);
});
test('Does not report success when movement completes without reaching the goal', async () => {
    const c = movementContext();
    const bot = movementBot();
    bot.easyLlmMovementConfig.moveTimeoutSec = 0.01;
    bot.pathfinder.goto = async goal => { bot.pathfinder.goal = goal; bot.emit('path_update', { status: 'partial', path: [] }); };
    bot.pathfinder.setGoal = goal => { bot.pathfinder.goal = goal; };
    await assert.rejects(c.goToPosition(bot, new Vec3(2, 0, 0), 0), /timed out/);
    assert.equal(bot.pathfinder.goal, null);
    assert.deepEqual(bot.pathfinder.movements.exclusionAreasBreak, []);
    assert.deepEqual(bot.eventNames(), []);
});
test('Tries the next allowed distance when movement completes with no path', async () => {
    const c = movementContext();
    const bot = movementBot();
    const radii = [];
    bot.pathfinder.goto = async goal => {
        radii.push(goal.range);
        if (goal.range === 0) bot.emit('path_update', { status: 'noPath', path: [] });
        else bot.entity.position = new Vec3(goal.x, goal.y, goal.z);
    };
    await c.goToPosition(bot, new Vec3(2, 0, 0), 1);
    assert.deepEqual(radii, [0, 1]);
    assert.deepEqual(bot.eventNames(), []);
});
