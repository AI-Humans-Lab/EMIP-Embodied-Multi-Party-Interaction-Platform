const path = require('path')
const log4js = require('log4js')
const WebSocket = require('ws');

const { getFormattedDateTime, PersistentWorker } = require("./lib/utils");

const BOTWORKER_FILE = __dirname + "/lib/botWorker.js"
const LOG_DIR = "/mf_logs"
const DEFAULT_PORT = 3000;
const PORT = process.argv[2] || DEFAULT_PORT;
const WS_PING_INTERVAL_MS = 2000;
const WS_MAX_MISSED_PONGS = 2;
const DEFAULT_TICK_RATE = 20;

function readBooleanEnvironment(name, defaultValue) {
    const value = process.env[name];
    if (value === undefined) return defaultValue;
    if (value === "true") return true;
    if (value === "false") return false;
    throw new Error(`${name} must be either "true" or "false", but received "${value}".`);
}

function readNumberEnvironment(name, defaultValue) {
    const value = process.env[name];
    if (value === undefined) return defaultValue;
    const parsedValue = Number(value);
    if (!Number.isFinite(parsedValue)) {
        throw new Error(`${name} must be a finite number, but received "${value}".`);
    }
    return parsedValue;
}

const config = Object.freeze({
    minecraftHost: process.env.MINECRAFT_HOST || "localhost",
    minecraftPort: readNumberEnvironment("MINECRAFT_PORT", 25565),
    adminMcName: process.env.MINEFLAYER_ADMIN_MC_NAME || "admin",
    canDigWhenMove: readBooleanEnvironment("MINEFLAYER_CAN_DIG_WHEN_MOVE", false),
    moveTimeoutSec: readNumberEnvironment("MINEFLAYER_MOVE_TIMEOUT_SEC", 120),
    stuckCheckIntervalSec: readNumberEnvironment("MINEFLAYER_STUCK_CHECK_INTERVAL_SEC", 0.1),
    stuckOffsetRange: readNumberEnvironment("MINEFLAYER_STUCK_OFFSET_RANGE", 1),
});

if (!Number.isInteger(config.minecraftPort) || config.minecraftPort <= 0 || config.minecraftPort > 65535) {
    throw new Error("MINECRAFT_PORT must be an integer between 1 and 65535.");
}

if (config.moveTimeoutSec <= 0 || config.stuckCheckIntervalSec <= 0 || config.stuckOffsetRange < 0) {
    throw new Error(
        "MINEFLAYER_MOVE_TIMEOUT_SEC and MINEFLAYER_STUCK_CHECK_INTERVAL_SEC must be positive, and MINEFLAYER_STUCK_OFFSET_RANGE must be non-negative."
    );
}

log4js.configure({
    appenders : {
        file : {type : 'file', filename : path.join(LOG_DIR, `mineflayer_${getFormattedDateTime()}.log`)},
        console: { type: 'console' },
        consoleFilter: {
            type: 'logLevelFilter',
            appender: 'console',
            level: 'info',
        },
    },
    categories : {
        default : {appenders : ['file', 'consoleFilter'], level : 'trace'},
    }
});

const logger = log4js.getLogger("mineflayer");
const wss = new WebSocket.Server({ port: PORT });
const workers = {};
let adminReadyPromise = null;
let adminController = null;
let currentSpeedMultiplier = 1;
let agentActionsPaused = false;
const activeAgentActions = new Set();

function buildCommandError(command, params, err) {
    const original = (err && (err.stack || err.message)) ? (err.stack || err.message) : String(err);

    if (command === "join") {
        const mcName = params?.mcName ?? "<unknown-name>";

        return [
            `Failed to join Minecraft server ${config.minecraftHost}:${config.minecraftPort} as \"${mcName}\".`,
            "Mineflayer could not connect or initialize the bot.",
            "Check that the Minecraft server/world is running and reachable, then retry.",
            `Original error: ${original}`,
        ].join(" ");
    }

    return `Command \"${command}\" failed. ${original}`;
}

function getErrorDetail(result) {
    if (!result) return "No response payload";
    if (typeof result === "string") return result;
    if (result.errorMsg) return result.errorMsg;
    if (result.message) return result.message;
    if (result.stack) return result.stack;
    return JSON.stringify(result);
}

async function postMessageToWorker(mcName, command, args={}, transferList=[]){
    if(!(mcName in workers)){
        throw new Error(`mcName "${mcName}" does not exist. List of mcName...${Object.keys(workers)}`);
    }

    const result = await workers[mcName].postMessage({
        "command": command,
        "args": args,
    }, transferList);
    if(result.errorMsg){
        throw new Error(result.errorMsg);
    }
    return result.data;
}

async function startWorker(mcName){
    const mcHost = config.minecraftHost;
    const mcPort = config.minecraftPort;
    if(mcName in workers){
        throw new Error(`mcName "${mcName}" already exists.`);
    }

    const workerData = {
        mcHost,
        mcPort,
        mcName,
        canDigWhenMove: config.canDigWhenMove,
        moveTimeoutSec: config.moveTimeoutSec,
        stuckCheckIntervalSec: config.stuckCheckIntervalSec,
        stuckOffsetRange: config.stuckOffsetRange,
        logDir: LOG_DIR,
    }

    const workerLogger = log4js.getLogger(`${mcName}`);

    const maxTrial = 5;
    for(let i = 0; i < maxTrial; i++){
        const worker = new PersistentWorker(BOTWORKER_FILE, { workerData: workerData }, mcName, workerLogger);
        workers[mcName] = worker;
        let result;
        try{
            result = await worker.waitForSignal("bot_status", 60);
        }catch(e){
            result = e;
        }
        if(result?.data?.success){
            if(currentSpeedMultiplier !== 1){
                await postMessageToWorker(mcName, "updateMineflayerTickRate", {tickRate: DEFAULT_TICK_RATE * currentSpeedMultiplier});
            }
            return { mcName };
        }
        const detail = getErrorDetail(result);
        await worker.terminate();
        delete workers[mcName];
        if(i === maxTrial - 1){
            throw new Error(`Failed to create bot \"${mcName}\" on ${mcHost}:${mcPort}. ${detail}`);
        }
        logger.warn(`${detail}. Trying again...`)
    }
}

async function join({mcName}){
    if(mcName === config.adminMcName){
        throw new Error(`mcName "${mcName}" is reserved for the server-owned admin. Use ensureAdmin.`);
    }
    return await startWorker(mcName);
}

async function joinAgent({mcName, variables}){
    if(agentActionsPaused) throw new Error("Agents cannot join while Agent actions are paused.");
    const requestedVariables = variables ?? {};
    const {mcName: adminMcName} = await ensureAdmin({variables: requestedVariables});
    await join({mcName});
    try {
        await updateAgentVariables({mcName, variables: requestedVariables});
        await execMc({commands: [`/op ${mcName}`]});
    } catch (error) {
        try {
            await leave({mcName});
        } catch (cleanupError) {
            logger.warn(`Failed to clean up ${mcName} after join failure: ${cleanupError.message}`);
        }
        throw error;
    }
    return {mcName, adminMcName};
}

async function ensureAdmin({variables}){
    const requestedVariables = variables ?? {};
    if(typeof requestedVariables !== "object" || Array.isArray(requestedVariables)){
        throw new Error("ensureAdmin variables must be an object.");
    }

    const reused = adminReadyPromise !== null;
    if(adminReadyPromise === null){
        adminReadyPromise = (async () => {
            await startWorker(config.adminMcName);
            await updateAgentVariables({mcName: config.adminMcName, variables: requestedVariables});
        })();
    }

    try {
        await adminReadyPromise;
    } catch(error) {
        logger.error(`Failed to initialize the server-owned admin: ${error.stack ?? error.message}`);
        process.exit(1);
    }
    return {mcName: config.adminMcName, reused};
}

async function leave({mcName}){
    if(mcName === config.adminMcName){
        throw new Error(`The server-owned admin "${mcName}" cannot be left by a client connection.`);
    }
    const worker = workers[mcName];
    if(!worker){
        throw new Error(`mcName "${mcName}" does not exist. List of mcName...${Object.keys(workers)}`);
    }
    delete workers[mcName];
    try {
        const result = await worker.postMessage({command: "close", args: {}});
        if(result.errorMsg) throw new Error(result.errorMsg);
    } finally {
        await worker.terminate();
    }
}
    
async function execJs({mcName, code, primitives}){
    if(agentActionsPaused) throw new Error("Agent actions are paused.");
    const action = postMessageToWorker(mcName, "execute", {code, primitives});
    activeAgentActions.add(action);
    try {
        return await action;
    } finally {
        activeAgentActions.delete(action);
    }
}

async function stopMoving({mcName}){
    await postMessageToWorker(mcName, "stopMoving", {});
}

async function execMc({mcName = config.adminMcName, commands}){
    await postMessageToWorker(mcName, "execMcCommands", {commands})
}

async function updateAgentVariables({mcName, variables}){
    await postMessageToWorker(mcName, "updateAgentVariables", {variables});
}

async function setBlocks({blockInfoList}){
    await postMessageToWorker(config.adminMcName, "setBlocks", {blockInfoList});
}

async function setContainers({containerInfoList}){
    await postMessageToWorker(config.adminMcName, "setContainers", {containerInfoList});
}

async function clearBox({}){
    await postMessageToWorker(config.adminMcName, "clearBox", {});
}

async function teleport({mcName, position, pitch, yaw, teleportOffset, waitForArrival}){
    await postMessageToWorker(config.adminMcName, "teleport", {mcName, position, pitch, yaw, teleportOffset, waitForArrival});
}

async function setInventoryAndEquipment({mcName, inventory, equipment}){
    await postMessageToWorker(config.adminMcName, "setInventoryAndEquipment", {mcName, inventory, equipment});
}

async function updateMineflayerTickRate({mcName = config.adminMcName, tickRate}){
    await postMessageToWorker(mcName, "updateMineflayerTickRate", {tickRate});
}

async function setSpeed({speedMultiplier}){
    if(!Number.isFinite(speedMultiplier) || speedMultiplier <= 0.05 || speedMultiplier >= 500){
        throw new Error("speedMultiplier must be greater than 0.05 and less than 500.");
    }

    const tickRate = DEFAULT_TICK_RATE * speedMultiplier;
    await execMc({commands: [`/tick rate ${tickRate}`]});
    for(const mcName of Object.keys(workers)){
        if(!(mcName in workers)) continue;
        await updateMineflayerTickRate({mcName, tickRate});
    }
    currentSpeedMultiplier = speedMultiplier;
    return {speedMultiplier};
}

async function pauseAgentActions(){
    agentActionsPaused = true;
    await Promise.allSettled([...activeAgentActions]);
    return {agentActionsPaused};
}

function resumeAgentActions(){
    agentActionsPaused = false;
    return {agentActionsPaused};
}

async function acquireAdmin(ws, params){
    if(adminController !== null) throw new Error("Another Admin connection already owns world control.");
    adminController = ws;
    const response = await ensureAdmin(params);
    resumeAgentActions();
    await setSpeed({speedMultiplier: 1});
    return response;
}


wss.on('connection', (ws) => {
  let pingCheckId = null;
  let missedPongs = 0;
  let closedHandled = false;
  const ownedWorkers = new Set();

  ws.isAlive = true;
  ws.on('pong', () => {
    ws.isAlive = true;
    missedPongs = 0;
  });

  const onSocketClosed = async () => {
    if (closedHandled) return;
    closedHandled = true;
    if (pingCheckId) {
      clearInterval(pingCheckId);
      pingCheckId = null;
    }
    try {
      if (adminController === ws) {
        adminController = null;
        try {
          resumeAgentActions();
          await setSpeed({speedMultiplier: 1});
        } catch (error) {
          logger.fatal(`Failed to restore normal world control after Admin disconnect: ${error.stack || error}`);
          setImmediate(() => process.exit(1));
        }
      }
      for (const mcName of [...ownedWorkers]) {
        try {
          await leave({ mcName });
        } catch (e) {
          if (logger) {
            logger.warn(`Failed to clean up worker ${mcName} on websocket close: ${e.message}`);
          } else {
            console.warn(`Failed to clean up worker ${mcName} on websocket close: ${e.message}`);
          }
        } finally {
          ownedWorkers.delete(mcName);
        }
      }
    } catch (e) {
      if (logger) {
        logger.warn(`Cleanup on websocket close failed: ${e.message}`);
      } else {
        console.warn(`Cleanup on websocket close failed: ${e.message}`);
      }
    }
  };

  ws.on('message', async (message) => {
    let payload;
    try {
        payload = JSON.parse(message);
    } catch (e) {
        const errorMsg = `Invalid JSON message: ${e.message}`;
        if (logger) logger.warn(errorMsg);
        ws.send(JSON.stringify({ type: "response", messageId: null, data: { errorMsg } }));
        return;
    }

    const {messageId, command, params} = payload;

    let response = null;
    try {
        switch(command){
            case "execJs": response = await execJs(params); break;
            case "stopMoving": await stopMoving(params); break;
            case "execMc": await execMc(params); break;
            case "acquireAdmin": response = await acquireAdmin(ws, params); break;

            case "join":
                response = await joinAgent(params);
                ownedWorkers.add(params.mcName);
                break;
            case "leave":
                await leave(params);
                ownedWorkers.delete(params.mcName);
                break;

            case "setBlocks": await setBlocks(params); response = {success: true}; break;
            case "setContainers": await setContainers(params); response = {success: true}; break;
            case "clearBox": await clearBox(params); response = {success: true}; break;
            case "teleport": await teleport(params); response = {success: true}; break;
            case "setInventoryAndEquipment": await setInventoryAndEquipment(params); response = {success: true}; break;
            case "setSpeed": response = await setSpeed(params); break;
            case "pauseAgentActions": response = await pauseAgentActions(); break;
            case "resumeAgentActions": response = resumeAgentActions(); break;
            default: throw new Error(`Unknown command "${command}"`);
        }

        if(response === undefined){
            throw new Error(`response undefined. command=${command}`);
        }

        const msg = {type: "response", messageId, data: response}
        ws.send(JSON.stringify(msg));
    } catch (e) {
        const errorMsg = buildCommandError(command, params, e);
        if (logger) {
            logger.error(errorMsg);
        } else {
            console.error(errorMsg);
        }
        const msg = {type: "response", messageId, data: {errorMsg}};
        try {
            ws.send(JSON.stringify(msg));
        } finally {
            if(adminController === ws){
                logger.fatal(`Stopping Mineflayer Server after Admin command failure: ${command}`);
                setImmediate(() => process.exit(1));
            }
        }
    }
  });

  ws.on('close', onSocketClosed);
  ws.on('error', onSocketClosed);

  pingCheckId = setInterval(() => {
    if (ws.readyState !== WebSocket.OPEN) return;
    if (!ws.isAlive) {
      missedPongs += 1;
      if (missedPongs >= WS_MAX_MISSED_PONGS) {
        ws.terminate();
      }
      return;
    }
    ws.isAlive = false;
    try {
      ws.ping();
    } catch (e) {
      ws.terminate();
    }
  }, WS_PING_INTERVAL_MS)
});

logger.info(`Server started on port ${PORT}`);
