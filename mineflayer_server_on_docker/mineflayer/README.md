# Mineflayer server

A Docker-based WebSocket server for Minecraft connections and bot actions.

## Bundled Mineflayer

`mineflayer-4.33.0/` differs from the official release in two internal modules:

- `physics.js`: adds `bot.updateTickRate()` to match physics updates to the server tick rate.
- `physics.js`: recovers from non-finite positions and velocities, suppresses invalid movement packets, and pauses physics on death.
- `entities.js`: handles both nested and legacy entity-velocity packets.

## Dependencies

The Docker build runs:

```sh
npm ci --omit=dev --install-links
```

- `ci`: installs the dependency versions recorded in `package-lock.json`.
- `--omit=dev`: excludes development tools such as Prettier.
- `--install-links`: installs bundled Mineflayer as a package rather than a symlink.

When updating dependencies, update the lockfile using `--install-links`, then verify the build and Minecraft behavior.

## UUID override

UUID dependencies of `@azure/msal-node` and `yggdrasil` are overridden to remove deprecation warnings, outside their declared version ranges.
UUID generation, limited authentication checks, and offline Minecraft operations have been tested; full authentication with a real Microsoft account has not.
