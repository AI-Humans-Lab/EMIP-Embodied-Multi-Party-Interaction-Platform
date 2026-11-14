class GoToPositionTimeoutError extends Error {}

function createGoToPositionTimeoutChecker(timeoutSec) {
    const deadline = Date.now() + Math.max(0, timeoutSec * 1000);

    return () => {
        if (Date.now() >= deadline) {
            throw new GoToPositionTimeoutError(`goToPosition timed out after ${timeoutSec} seconds`);
        }
    };
}

function hasReachedGoToPositionGoal(bot, goal) {
    const currentBlock = bot.entity.position.floored();
    return (
        goal.isEnd(currentBlock) ||
        goal.isEnd(currentBlock.offset(0, 1, 0))
    );
}

async function findPathToPosition(bot, goal, checkInterrupted) {
    const pathGenerator = bot.pathfinder.getPathFromTo(
        bot.pathfinder.movements,
        bot.entity.position,
        goal
    );
    let pathIteration = pathGenerator.next();

    while (
        !pathIteration.done &&
        pathIteration.value.result.status === "partial"
    ) {
        checkInterrupted();
        await bot.waitForTicks(1);
        pathIteration = pathGenerator.next();
    }

    if (pathIteration.done || pathIteration.value.result.status !== "success") {
        const status = pathIteration.done
            ? "noPath"
            : pathIteration.value.result.status;
        throw new Error(`Pathfinder failed to find a path: ${status}`);
    }

    return pathIteration.value.result.path;
}

function getRemainingTeleportPath(path, currentPosition) {
    if (path.length <= 1) {
        return path;
    }

    let nearestIndex = 0;
    let nearestDistance = Infinity;

    for (let index = 0; index < path.length; index++) {
        const node = path[index];
        const distance = currentPosition.distanceSquared(
            new Vec3(node.x, node.y, node.z)
        );
        if (distance < nearestDistance) {
            nearestDistance = distance;
            nearestIndex = index;
        }
    }

    const startIndex = Math.min(nearestIndex + 1, path.length - 1);
    return path.slice(startIndex);
}

function getTeleportWaypoints(current, target) {
    if (target.y > current.y) {
        return [
            new Vec3(current.x, target.y, current.z),
            target,
        ];
    }
    if (target.y < current.y) {
        return [
            new Vec3(target.x, current.y, target.z),
            target,
        ];
    }
    return [target];
}

async function teleportThroughPath({
    bot,
    path,
    goal,
    maxTeleportTicks,
    checkInterrupted,
}) {
    const walkSpeedPerTick = 4.317 / 20;
    const remainingPath = getRemainingTeleportPath(
        path,
        bot.entity.position
    );
    let current = bot.entity.position.clone();
    let teleportTicks = 0;

    for (const node of remainingPath) {
        checkInterrupted();
        if (node.toBreak.length > 0 || node.toPlace.length > 0) {
            throw new Error("Path requires breaking or placing blocks");
        }

        const target = new Vec3(node.x, node.y, node.z);
        if (!checkValidPosition(target, bot)) {
            throw new Error("Pathfinder returned a position outside the environment");
        }

        for (const waypoint of getTeleportWaypoints(current, target)) {
            const dx = waypoint.x - current.x;
            const dy = waypoint.y - current.y;
            const dz = waypoint.z - current.z;
            const horizontalDistance = Math.sqrt(dx * dx + dz * dz);
            const yaw = horizontalDistance > 0
                ? Math.atan2(-dx, dz) * 180 / Math.PI
                : (Number.isFinite(bot.entity.yaw)
                    ? (-bot.entity.yaw + Math.PI) * 180 / Math.PI
                    : 0);
            const steps = Math.max(
                1,
                Math.ceil(Math.max(horizontalDistance, Math.abs(dy)) / walkSpeedPerTick)
            );

            for (let step = 1; step <= steps; step++) {
                checkInterrupted();
                if (teleportTicks >= maxTeleportTicks) {
                    return false;
                }

                const ratio = step / steps;
                const x = current.x + dx * ratio;
                const y = current.y + dy * ratio;
                const z = current.z + dz * ratio;
                if (![x, y, z].every(Number.isFinite)) {
                    throw new Error("Pathfinder returned an invalid position");
                }

                bot.chat(`/tp @s ${x} ${y} ${z} ${yaw} 0`);
                teleportTicks++;
                await bot.waitForTicks(1);

                if (hasReachedGoToPositionGoal(bot, goal)) {
                    return true;
                }
            }

            current = waypoint;
        }
    }

    return hasReachedGoToPositionGoal(bot, goal);
}

async function teleportToPositionAlongPath({
    bot,
    path,
    goal,
    maxTeleportTicks,
    checkTimedOut,
}) {
    const previousGameMode = bot.game?.gameMode ?? "survival";
    const shouldRestoreGameMode = previousGameMode !== "creative";
    let cancelled = false;

    const cancelWhenGoalCleared = (updatedGoal) => {
        if (updatedGoal === null) {
            cancelled = true;
        }
    };
    const checkInterrupted = () => {
        if (cancelled) {
            throw new Error("goal was changed");
        }
        checkTimedOut();
    };

    bot.on("goal_updated", cancelWhenGoalCleared);
    try {
        if (shouldRestoreGameMode) {
            bot.chat("/gamemode creative @s");
        }

        return await teleportThroughPath({
            bot,
            path,
            goal,
            maxTeleportTicks,
            checkInterrupted,
        });
    } finally {
        bot.removeListener("goal_updated", cancelWhenGoalCleared);
        if (shouldRestoreGameMode) {
            bot.chat(`/gamemode ${previousGameMode} @s`);
        }
    }
}

async function moveToPositionWithPathfinder({
    bot,
    goal,
    movementConfig,
    checkTimedOut,
    adaptiveTeleportTicks,
}) {
    checkTimedOut();
    let pathfinderDone = hasReachedGoToPositionGoal(bot, goal);
    let pathfinderError = null;
    let teleportPath = null;
    const cacheTeleportPath = (result) => {
        if (result.status === "noPath") pathfinderError = new Error("No path to the goal");
        if (result.status === "timeout") pathfinderError = new Error("Pathfinder timed out");
        if (movementConfig.mode !== "adaptive") return;
        if (!result.path?.length) {
            teleportPath = null;
            return;
        }
        teleportPath = result.path.map((node) => ({
            x: node.x,
            y: node.y,
            z: node.z,
            toBreak: [...node.toBreak],
            toPlace: [...node.toPlace],
        }));
    };
    const cancelWhenGoalChanged = (updatedGoal) => {
        if (updatedGoal !== goal) pathfinderError = new Error("goal was changed");
    };
    bot.on("path_update", cacheTeleportPath);
    bot.on("goal_updated", cancelWhenGoalChanged);

    try {
        if (!pathfinderDone) {
            bot.pathfinder.goto(goal).then(
                () => { pathfinderDone = hasReachedGoToPositionGoal(bot, goal); },
                (error) => { pathfinderError = error; }
            );
        }

        const checkIntervalTicks = Math.max(
            1,
            movementConfig.mode === "adaptive" ? Math.round(movementConfig.stuckCheckIntervalSec * 20) : 1
        );
        let lastProgressPosition = bot.entity.position.clone();
        let lastProgressTime = Date.now();

        while (!pathfinderDone && !pathfinderError) {
            await bot.waitForTicks(checkIntervalTicks);
            pathfinderDone = hasReachedGoToPositionGoal(bot, goal);
            if (pathfinderDone || pathfinderError) break;
            checkTimedOut();
            if (movementConfig.mode !== "adaptive") continue;

            if (
                !bot.pathfinder.isMoving() ||
                bot.pathfinder.isMining() ||
                bot.pathfinder.isBuilding()
            ) {
                lastProgressPosition = bot.entity.position.clone();
                lastProgressTime = Date.now();
                continue;
            }

            if (lastProgressPosition.distanceTo(bot.entity.position) >= 0.1) {
                lastProgressPosition = bot.entity.position.clone();
                lastProgressTime = Date.now();
                continue;
            }

            if (
                Date.now() - lastProgressTime <
                movementConfig.stuckDetectionWindowSec * 1000
            ) {
                continue;
            }

            if (teleportPath) {
                await teleportToPositionAlongPath({
                    bot,
                    path: teleportPath,
                    goal,
                    maxTeleportTicks: adaptiveTeleportTicks,
                    checkTimedOut,
                });
            }
            lastProgressPosition = bot.entity.position.clone();
            lastProgressTime = Date.now();
        }

        if (pathfinderError) {
            throw pathfinderError;
        }
    } finally {
        bot.removeListener("path_update", cacheTeleportPath);
        bot.removeListener("goal_updated", cancelWhenGoalChanged);
        if (bot.pathfinder.goal === goal) bot.pathfinder.setGoal(null);
    }
}

async function goToPosition(bot, position, maxFallbackDistance=2, teleportAlongPath=false) {
    if (!(position instanceof Vec3)) {
        throw new Error("position for goToPosition must be a Vec3");
    }
    if (![position.x, position.y, position.z].every(Number.isFinite)) {
        throw new Error("position for goToPosition must contain only finite numbers");
    }

    if (!Number.isInteger(maxFallbackDistance) || maxFallbackDistance < 0) {
        throw new Error("maxFallbackDistance for goToPosition must be an integer greater than or equal to 0");
    }
    if (typeof teleportAlongPath !== "boolean") {
        throw new Error("teleportAlongPath for goToPosition must be a boolean");
    }

    const targetPosition = position.floored();
    if (position.x !== targetPosition.x || position.y !== targetPosition.y || position.z !== targetPosition.z) {
        await think(bot, `Warning: goToPosition floors fractional position ${position} to ${targetPosition}; using the floored position.`);
    }
    if (!checkValidPosition(targetPosition, bot)) {
        throw new Error(`position ${targetPosition} for goToPosition is out of environment.`);
    }

    const movementConfig = bot.easyLlmMovementConfig;
    if (!movementConfig) {
        throw new Error("Movement configuration is not initialized");
    }

    const checkTimedOut = createGoToPositionTimeoutChecker(
        movementConfig.moveTimeoutSec
    );
    const adaptiveTeleportTicks = 10;
    let error = null;
    const exclusionAreasBreak = bot.pathfinder.movements.exclusionAreasBreak;
    const preventBreakingTarget = block => block.position.equals(targetPosition) ? Infinity : 0;
    exclusionAreasBreak.push(preventBreakingTarget);

    for (let d = 0; d <= maxFallbackDistance; d++) {
        try {
            checkTimedOut();
            const goal = new GoalNear(targetPosition.x, targetPosition.y, targetPosition.z, d);
            const teleportPath = teleportAlongPath
                ? await findPathToPosition(bot, goal, checkTimedOut)
                : null;

            if (teleportAlongPath) {
                const reached = await teleportToPositionAlongPath({
                    bot,
                    path: teleportPath,
                    goal,
                    maxTeleportTicks: Infinity,
                    checkTimedOut,
                });
                if (reached) {
                    error = null;
                    break;
                }
                throw new Error("goToPosition did not reach the goal");
            }

            await moveToPositionWithPathfinder({
                bot,
                goal,
                movementConfig,
                checkTimedOut,
                adaptiveTeleportTicks,
            });
            error = null;
            break;
        } catch (err) {
            error = err;
            const errorMessage = String(err?.message ?? err);
            if (err instanceof GoToPositionTimeoutError || errorMessage.includes("goal was changed")) {
                break;
            }
        }
    }

    const exclusionIndex = exclusionAreasBreak.indexOf(preventBreakingTarget);
    if (exclusionIndex !== -1) exclusionAreasBreak.splice(exclusionIndex, 1);
    if (error) throw error;
}
