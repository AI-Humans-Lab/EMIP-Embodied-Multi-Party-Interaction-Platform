import asyncio
import json
import logging
import os
from pathlib import Path
from tempfile import gettempdir
from textwrap import dedent
import unittest

from modules.mineflayerconnection import AdminMineflayerConnection, AgentMineflayerConnection, build_mineflayer_variables
from modules.minecraft_observation import MinecraftObservationProcessor
from modules.observation_source import MinecraftObservationConnection


WATCH_MODE = os.getenv("EASYLLM_WATCH_MINECRAFT_PRIMITIVES") == "1"
RUN_MINECRAFT_TESTS = WATCH_MODE or os.getenv("EASYLLM_RUN_MINECRAFT_PRIMITIVES") == "1"
WATCH_SECONDS = 5
START_WAIT_SECONDS = 30
PROJECT_DIR = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_DIR / "src"
LOG_DIR = Path(os.getenv("EASYLLM_PRIMITIVE_LOG_DIR", str(Path(gettempdir()) / "easyllm-minecraft-primitives")))
MINEFLAYER_HOST = os.getenv("EASYLLM_TEST_MINEFLAYER_HOST", "127.0.0.1")
MINEFLAYER_PORT = 3000
OBSERVATION_HOST = os.getenv("EASYLLM_TEST_OBSERVATION_HOST", "127.0.0.1")
OBSERVATION_PORT = 7891
ENV_BOX = [[-16, -61, -16], [16, -55, 16]]
AGENT_NAME = "PrimitiveTester"
PARTNER_NAME = "TransferPartner"


def primitive_sources(names: list[str]) -> list[str]:
    return [(SOURCE_DIR / "primitives" / f"{name}.js").read_text(encoding="utf-8") for name in names]


def test_display(primitive_name: str, description: str):
    def decorate(test_method):
        async def run_and_announce(self):
            try:
                await test_method(self)
                if WATCH_MODE:
                    message = f"[success] Verification conditions met: {description}"
                    await self._execute_success(
                        f"""
                        bot.chat({json.dumps(message, ensure_ascii=False)});
                        """,
                        [],
                    )
            except unittest.SkipTest:
                raise
            except Exception as error:
                if WATCH_MODE:
                    message = f"[failed] {description}: {error}".replace("\r", " ").replace("\n", " ")
                    try:
                        for index in range(0, len(message), 180):
                            await self._execute_success(
                                f"""
                                bot.chat({json.dumps(message[index:index + 180], ensure_ascii=False)});
                                """,
                                [],
                            )
                    except Exception:
                        pass
                raise
        run_and_announce.primitive_name = primitive_name
        run_and_announce.test_description = description
        return run_and_announce
    return decorate


@unittest.skipUnless(RUN_MINECRAFT_TESTS, "set EASYLLM_RUN_MINECRAFT_PRIMITIVES=1 to run Minecraft primitive tests")
class PrimitiveMinecraftTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.display_test_index = 0
        cls.display_test_count = len(unittest.defaultTestLoader.getTestCaseNames(cls))

    async def asyncSetUp(self):
        if not hasattr(self.__class__, "connection"):
            admin_connection = AdminMineflayerConnection(MINEFLAYER_HOST, MINEFLAYER_PORT, logging.getLogger(__name__))
            agent_connection = AgentMineflayerConnection(MINEFLAYER_HOST, MINEFLAYER_PORT, logging.getLogger(__name__))
            try:
                await admin_connection.connect()
                variables = build_mineflayer_variables(ENV_BOX)
                admin_mc_name = await admin_connection.acquire(variables)
                await agent_connection.connect()
                await agent_connection.join(AGENT_NAME, {
                    **variables,
                    "agentInfo": json.dumps({"Partner": {"mcName": PARTNER_NAME}}),
                })
                await agent_connection.join(PARTNER_NAME, {
                    **variables,
                    "agentInfo": json.dumps({"Tester": {"mcName": AGENT_NAME}}),
                })
            except BaseException:
                await agent_connection.close()
                await admin_connection.close()
                raise
            self.__class__.admin_connection = admin_connection
            self.__class__.connection = agent_connection
            self.__class__.admin_mc_name = admin_mc_name
            if WATCH_MODE:
                print("\nMinecraft server is ready for observation; the first scenario has not started yet.", flush=True)
                print("Connect Minecraft Java Edition 1.21 to localhost:25565 now.", flush=True)
                print(f"The first scenario will start in {START_WAIT_SECONDS:g} seconds.", flush=True)
                await asyncio.sleep(START_WAIT_SECONDS)

        await self._reset_scenario()
        await self._announce_scenario()

    @classmethod
    def tearDownClass(cls):
        agent_connection = getattr(cls, "connection", None)
        if agent_connection is None:
            return
        admin_connection = cls.admin_connection

        async def close():
            await agent_connection.close()
            await admin_connection.close()

        asyncio.run(close())
        delattr(cls, "connection")
        delattr(cls, "admin_connection")
        delattr(cls, "admin_mc_name")

    async def asyncTearDown(self):
        if WATCH_MODE:
            await asyncio.sleep(WATCH_SECONDS)

    async def _reset_scenario(self):
        connection = self.admin_connection
        await connection.exec_mc([
            f"/gamemode survival {AGENT_NAME}",
            f"/gamemode survival {PARTNER_NAME}",
            f"/effect clear {AGENT_NAME}",
            f"/effect clear {PARTNER_NAME}",
            "/kill @e[type=item]",
            "/fill -16 -60 -16 16 -55 16 air",
            "/fill -16 -61 -16 16 -61 16 stone",
            "/weather clear",
            "/time set day",
        ])
        await connection.set_inventory_and_equipment(AGENT_NAME, {}, [])
        await connection.set_inventory_and_equipment(PARTNER_NAME, {}, [])
        await connection.teleport(AGENT_NAME, [-9, -60, -9], 0, 0, teleport_offset=[0, 0, 0])
        await connection.teleport(PARTNER_NAME, [9, -60, 9], 0, 0, teleport_offset=[0, 0, 0])
        await self._execute("""await bot.waitForTicks(5);""", [])

    async def _announce_scenario(self):
        if not WATCH_MODE:
            return
        test_method = getattr(self, self._testMethodName)
        primitive_name = test_method.primitive_name
        description = test_method.test_description
        self.__class__.display_test_index += 1
        title = f"test({self.display_test_index}/{self.display_test_count}): {primitive_name}"
        print(f"{title} - {description}", flush=True)
        title_json = json.dumps({"text": title, "color": "gold"}, ensure_ascii=False)
        subtitle_json = json.dumps({"text": description, "color": "white"}, ensure_ascii=False)
        await self.admin_connection.exec_mc([
            f"/title @a title {title_json}",
            f"/title @a subtitle {subtitle_json}",
        ])
        await asyncio.sleep(WATCH_SECONDS)

    async def _execute(self, code: str, primitive_names: list[str]) -> dict:
        return await self._execute_for(AGENT_NAME, code, primitive_names)

    async def _execute_for(self, agent_name: str, code: str, primitive_names: list[str]) -> dict:
        result = await self.connection.exec_js(agent_name, dedent(code).strip(), primitive_sources(primitive_names))
        self.assertIsInstance(result, dict)
        return result

    async def _execute_success(self, code: str, primitive_names: list[str]):
        result = await self._execute(code, primitive_names)
        self.assertTrue(result.get("success"), result.get("errorMsg"))

    async def _execute_error(self, code: str, expected_message: str | None, primitive_names: list[str]):
        result = await self._execute(code, primitive_names)
        self.assertFalse(result.get("success"), "primitive unexpectedly succeeded")
        if expected_message is not None:
            self.assertIn(expected_message, result.get("errorMsg") or "")
        if WATCH_MODE and result.get("errorMsg"):
            message = str(result["errorMsg"]).replace("\r", " ").replace("\n", " ")
            for index in range(0, len(message), 180):
                prefix = "Expected error: " if index == 0 else "(continued)"
                await self._execute_success(
                    f"""
                    bot.chat({json.dumps(prefix + message[index:index + 180], ensure_ascii=False)});
                    """,
                    [],
                )

    async def _execute_success_for(self, agent_name: str, code: str, primitive_names: list[str]):
        result = await self._execute_for(agent_name, code, primitive_names)
        self.assertTrue(result.get("success"), result.get("errorMsg"))

    async def _set_inventory(self, inventory: dict[str, int], equipment: list[str | None] | None = None):
        await self.admin_connection.set_inventory_and_equipment(AGENT_NAME, inventory, [] if equipment is None else equipment)

    async def _set_partner_inventory(self, inventory: dict[str, int], equipment: list[str | None] | None = None):
        await self.admin_connection.set_inventory_and_equipment(PARTNER_NAME, inventory, [] if equipment is None else equipment)

    async def _set_blocks(self, *blocks: tuple[list[int], str]):
        await self.admin_connection.set_blocks([
            {"position": position, "name": name, "properties": {}}
            for position, name in blocks
        ])

    async def _set_container(self, position: list[int], items: dict[str, int]):
        await self.admin_connection.set_containers([{**items, "position": position}])
        await self._execute("""await bot.waitForTicks(5);""", [])

    async def _set_block_behind_fence(self, name: str):
        fence = [
            ([x, -60, z], "oak_fence")
            for x in range(2, 7) for z in range(-2, 3)
            if x in (2, 6) or z in (-2, 2)
        ]
        await self._set_blocks(*fence, ([4, -60, 0], "stone"), ([4, -59, 0], name))

    async def _assert_inventory_sync(self, expected: dict[str, int]):
        observation = MinecraftObservationConnection(
            log_dir=str(LOG_DIR), host=OBSERVATION_HOST, port=OBSERVATION_PORT, env_box=ENV_BOX,
        )
        try:
            initial_state = await asyncio.to_thread(observation.connect)
            processor = MinecraftObservationProcessor(initial_tick=initial_state["globalTick"], admin_mc_name=self.admin_mc_name)
            for _ in range(10):
                message = await asyncio.to_thread(observation.receive, 1.0)
                ticks = processor.process(message) if message is not None else []
                statuses = [tick["objective"]["status"] for tick in ticks if AGENT_NAME in tick["objective"]["status"]]
                if statuses:
                    server_inventory = statuses[-1][AGENT_NAME]["hidden"]["inventory"]
                    break
            else:
                self.fail("No server-side inventory observation was received")
        finally:
            await asyncio.to_thread(observation.close)
        expected_items = {name: count for name, count in expected.items() if count > 0}
        self.assertEqual({name: count for name, count in server_inventory.items() if count > 0}, expected_items)
        await self._execute_success(
            f"""
            const expected = {json.dumps(expected_items)};
            const actual = {{}};
            for (const item of bot.inventory.items()) actual[item.name] = (actual[item.name] || 0) + item.count;
            if (JSON.stringify(Object.entries(actual).sort()) !== JSON.stringify(Object.entries(expected).sort())) throw new Error(`Mineflayer inventory mismatch: ${{JSON.stringify(actual)}}`);
            """,
            [],
        )

    @test_display("craftItem", "Craft planks from logs without a crafting table")
    async def test_craftItem_01_crafts_without_crafting_table(self):
        await self._set_inventory({"oak_log": 1})
        await self._execute_success(
            """
            await craftItem(bot, "oak_planks", null);
            if (bot.inventory.count(mcData.itemsByName.oak_planks.id) !== 4) throw new Error("four oak_planks were not crafted");
            """,
            ["checkValidPosition", "think", "goToPosition", "craftHelper", "craftItem"],
        )

    @test_display("craftItem", "Craft two wooden pickaxes using different planks")
    async def test_craftItem_02_reselects_recipe_for_each_crafting_iteration(self):
        await self._set_blocks(([2, -60, 0], "crafting_table"))
        await self._set_inventory({"cherry_planks": 3, "oak_planks": 3, "stick": 4})
        await self._execute_success(
            """
            await craftItem(bot, "wooden_pickaxe", new Vec3(2, -60, 0), 2);
            if (bot.inventory.count(mcData.itemsByName.wooden_pickaxe.id) !== 2) throw new Error("two wooden_pickaxes were not crafted");
            """,
            ["checkValidPosition", "think", "goToPosition", "craftHelper", "craftItem"],
        )

    @test_display("craftItem", "Fail with an explicit report of missing materials")
    async def test_craftItem_03_reports_missing_materials(self):
        await self._set_blocks(([2, -60, 0], "crafting_table"))
        await self._set_inventory({"stick": 2})
        await self._execute_error(
            """await craftItem(bot, "wooden_pickaxe", new Vec3(2, -60, 0));""",
            " OR ",
            ["checkValidPosition", "think", "goToPosition", "craftHelper", "craftItem"],
        )

    @test_display("craftItem", "Report missing materials and completed count after one successful craft")
    async def test_craftItem_04_reports_completed_crafts_on_partial_failure(self):
        await self._set_blocks(([2, -60, 0], "crafting_table"))
        await self._set_inventory({"oak_planks": 3, "stick": 4})
        await self._execute_error(
            """await craftItem(bot, "wooden_pickaxe", new Vec3(2, -60, 0), 2);""",
            "completed 1",
            ["checkValidPosition", "think", "goToPosition", "craftHelper", "craftItem"],
        )
        await self._execute_success(
            """
            if (bot.inventory.count(mcData.itemsByName.wooden_pickaxe.id) !== 1) throw new Error("unexpected crafted count after partial completion");
            """,
            [],
        )

    @test_display("craftItem", "Fail when the specified position is not a crafting table")
    async def test_craftItem_05_rejects_non_crafting_table_blocks(self):
        await self._set_blocks(([2, -60, 0], "stone"))
        await self._set_inventory({"oak_planks": 3, "stick": 2})
        await self._execute_error(
            """await craftItem(bot, "wooden_pickaxe", new Vec3(2, -60, 0));""",
            "found stone",
            ["checkValidPosition", "think", "goToPosition", "craftHelper", "craftItem"],
        )

    @test_display("craftItem", "Fail with an explicit report that no recipe exists")
    async def test_craftItem_06_reports_missing_recipe(self):
        await self._execute_error(
            """await craftItem(bot, "stone", null);""",
            "No crafting recipe exists for stone",
            ["checkValidPosition", "think", "goToPosition", "craftHelper", "craftItem"],
        )

    @test_display("craftItem", "Use a crafting table across an unbreakable fence")
    async def test_craftItem_07_uses_crafting_table_beyond_distance_two(self):
        await self._set_block_behind_fence("crafting_table")
        await self._set_inventory({"oak_planks": 3, "stick": 2})
        await self._execute_success(
            """
            if (bot.pathfinder.movements.canDig !== false) throw new Error("movement must not dig blocks");
            await craftItem(bot, "wooden_pickaxe", new Vec3(4, -59, 0));
            if (bot.entity.position.distanceTo(new Vec3(4, -59, 0)) <= 2) throw new Error(`craft distance=${bot.entity.position.distanceTo(new Vec3(4, -59, 0))}`);
            if (bot.inventory.count(mcData.itemsByName.wooden_pickaxe.id) !== 1) throw new Error("wooden pickaxe was not crafted");
            """,
            ["checkValidPosition", "think", "goToPosition", "craftHelper", "craftItem"],
        )

    @test_display("craftItem", "Move to a crafting table behind a wall and craft")
    async def test_craftItem_08_crafts_at_table_behind_wall(self):
        await self._set_blocks(*[
            ([7, y, z], "oak_planks")
            for y in (-60, -59)
            for z in range(-2, 11)
        ], ([1, -60, 4], "crafting_table"))
        await self._set_inventory({"oak_planks": 3, "stick": 2})
        await self.admin_connection.teleport(AGENT_NAME, [15, -60, 4], 0, 0, teleport_offset=[0, 0, 0])
        await self._execute("""await bot.waitForTicks(30);""", [])
        await self._execute_success(
            """
            if (bot.pathfinder.movements.canDig !== false) throw new Error("movement must not dig blocks");
            await craftItem(bot, "wooden_pickaxe", new Vec3(1, -60, 4));
            if (bot.inventory.count(mcData.itemsByName.wooden_pickaxe.id) !== 1) throw new Error("wooden pickaxe was not crafted");
            """,
            ["checkValidPosition", "think", "goToPosition", "craftHelper", "craftItem"],
        )

    @test_display("depositItemIntoChest", "Deposit two apples into a chest")
    async def test_depositItemIntoChest_01_deposits_items_into_chest(self):
        position = [2, -60, 0]
        await self._set_blocks((position, "chest"))
        await self._set_container(position, {"diamond": 1})
        await self._set_inventory({"apple": 5})
        await self._execute_success(
            """
            const position = new Vec3(2, -60, 0);
            await depositItemIntoChest(bot, position, {apple: 2});
            if (bot.inventory.count(mcData.itemsByName.apple.id) !== 3) throw new Error("unexpected apple inventory after deposit");
            const chest = await bot.openContainer(bot.blockAt(position));
            const chestCount = chest.containerItems().filter(item => item.name === "apple").reduce((sum, item) => sum + item.count, 0);
            await chest.close();
            if (chestCount !== 2) throw new Error(`unexpected chest apple count after deposit: ${chestCount}`);
            """,
            ["checkValidPosition", "think", "goToPosition", "useChest"],
        )
        await self._assert_inventory_sync({"apple": 3})
        await self._execute_success(
            """
            const position = new Vec3(2, -60, 0);
            await getItemFromChest(bot, position, {apple: 1});
            const chest = await bot.openContainer(bot.blockAt(position));
            const items = chest.containerItems();
            await chest.close();
            if (items.filter(item => item.name === "apple").reduce((sum, item) => sum + item.count, 0) !== 1) throw new Error("follow-up withdrawal changed the wrong apple count");
            if (items.filter(item => item.name === "diamond").reduce((sum, item) => sum + item.count, 0) !== 1) throw new Error("existing diamond was changed");
            """,
            ["checkValidPosition", "think", "goToPosition", "useChest"],
        )
        await self._assert_inventory_sync({"apple": 4})

    @test_display("depositItemIntoChest", "Report deposited items even when a chest deposit fails")
    async def test_depositItemIntoChest_02_reports_deposited_items_on_partial_failure(self):
        position = [2, -60, 0]
        await self._set_blocks((position, "chest"))
        await self._set_inventory({"stick": 2})
        await self._execute_error(
            """await depositItemIntoChest(bot, new Vec3(2, -60, 0), {stick: 2, apple: 1});""",
            'completed deposit operations: {"stick":2}',
            ["checkValidPosition", "think", "goToPosition", "useChest"],
        )
        await self._execute_success(
            """
            const position = new Vec3(2, -60, 0);
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 0) throw new Error("unexpected inventory after partial deposit");
            const chest = await bot.openContainer(bot.blockAt(position));
            const chestCount = chest.containerItems().filter(item => item.name === "stick").reduce((sum, item) => sum + item.count, 0);
            await chest.close();
            if (chestCount !== 2) throw new Error(`unexpected chest count after partial deposit: ${chestCount}`);
            """,
            [],
        )
        await self._assert_inventory_sync({"stick": 0})
        await self._execute_success(
            """await getItemFromChest(bot, new Vec3(2, -60, 0), {stick: 1});""",
            ["checkValidPosition", "think", "goToPosition", "useChest"],
        )
        await self._assert_inventory_sync({"stick": 1})

    @test_display("depositItemIntoChest", "Use a chest across an unbreakable fence")
    async def test_depositItemIntoChest_03_uses_chest_beyond_distance_two(self):
        await self._set_block_behind_fence("chest")
        await self._set_inventory({"apple": 2})
        await self._execute_success(
            """
            if (bot.pathfinder.movements.canDig !== false) throw new Error("movement must not dig blocks");
            const position = new Vec3(4, -59, 0);
            await depositItemIntoChest(bot, position, {apple: 1});
            if (bot.entity.position.distanceTo(position) <= 2) throw new Error(`chest distance=${bot.entity.position.distanceTo(position)}`);
            const chest = await bot.openContainer(bot.blockAt(position));
            const count = chest.containerItems().filter(item => item.name === "apple").reduce((sum, item) => sum + item.count, 0);
            await chest.close();
            if (count !== 1) throw new Error("apple was not deposited");
            """,
            ["checkValidPosition", "think", "goToPosition", "useChest"],
        )

    @test_display("getItemFromChest", "Retrieve three sticks from a chest")
    async def test_getItemFromChest_01_retrieves_items_from_chest(self):
        position = [2, -60, 0]
        await self._set_blocks((position, "chest"))
        await self._set_container(position, {"stick": 5})
        await self._execute_success(
            """
            const position = new Vec3(2, -60, 0);
            await getItemFromChest(bot, position, {stick: 3});
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 3) throw new Error("unexpected stick inventory after withdrawal");
            const chest = await bot.openContainer(bot.blockAt(position));
            const chestCount = chest.containerItems().filter(item => item.name === "stick").reduce((sum, item) => sum + item.count, 0);
            await chest.close();
            if (chestCount !== 2) throw new Error(`unexpected chest stick count after withdrawal: ${chestCount}`);
            """,
            ["checkValidPosition", "think", "goToPosition", "useChest"],
        )
        await self._assert_inventory_sync({"stick": 3})
        await self._execute_success(
            """await depositItemIntoChest(bot, new Vec3(2, -60, 0), {stick: 1});""",
            ["checkValidPosition", "think", "goToPosition", "useChest"],
        )
        await self._assert_inventory_sync({"stick": 2})

    @test_display("getItemFromChest", "Reject a specified position that is not a chest")
    async def test_getItemFromChest_02_rejects_non_chest_blocks(self):
        await self._set_blocks(([2, -60, 0], "stone"))
        await self._execute_error(
            """await getItemFromChest(bot, new Vec3(2, -60, 0), {stick: 1});""",
            "No chest was found",
            ["checkValidPosition", "think", "goToPosition", "useChest"],
        )

    @test_display("getItemFromChest", "Report retrieved items even when a chest retrieval fails")
    async def test_getItemFromChest_03_reports_retrieved_items_on_partial_failure(self):
        position = [2, -60, 0]
        await self._set_blocks((position, "chest"))
        await self._set_container(position, {"stick": 5})
        await self._execute_error(
            """await getItemFromChest(bot, new Vec3(2, -60, 0), {stick: 2, apple: 1});""",
            'completed withdraw operations: {"stick":2}',
            ["checkValidPosition", "think", "goToPosition", "useChest"],
        )
        await self._execute_success(
            """
            const position = new Vec3(2, -60, 0);
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 2) throw new Error("unexpected inventory after partial withdrawal");
            const chest = await bot.openContainer(bot.blockAt(position));
            const chestCount = chest.containerItems().filter(item => item.name === "stick").reduce((sum, item) => sum + item.count, 0);
            await chest.close();
            if (chestCount !== 3) throw new Error(`unexpected chest count after partial withdrawal: ${chestCount}`);
            """,
            [],
        )
        await self._assert_inventory_sync({"stick": 2})
        await self._execute_success(
            """await depositItemIntoChest(bot, new Vec3(2, -60, 0), {stick: 1});""",
            ["checkValidPosition", "think", "goToPosition", "useChest"],
        )
        await self._assert_inventory_sync({"stick": 1})

    @test_display("giveItemToOther", "Do not give items when there are insufficient sticks")
    async def test_giveItemToOther_01_does_not_transfer_when_inventory_is_insufficient(self):
        await self._set_inventory({"stick": 1})
        await self._set_partner_inventory({})
        await self.admin_connection.teleport(AGENT_NAME, [0, -60, 0], 0, 0, teleport_offset=[0, 0, 0])
        await self.admin_connection.teleport(PARTNER_NAME, [1, -60, 0], 0, 0, teleport_offset=[0, 0, 0])
        await self._execute("""await bot.waitForTicks(5);""", [])
        await self._execute_error(
            """await giveItemToOther(bot, "Partner", "stick", 2);""",
            "Not enough stick",
            ["think", "giveItemToOther"],
        )
        await self._execute_success(
            """
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 1) throw new Error("giver inventory changed after rejected transfer");
            """,
            [],
        )
        await self._execute_success_for(
            PARTNER_NAME,
            """
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 0) throw new Error("recipient inventory changed after rejected transfer");
            """,
            [],
        )

    @test_display("giveItemToOther", "Give two sticks to a nearby partner")
    async def test_giveItemToOther_02_transfers_items(self):
        await self._set_inventory({"stick": 5})
        await self._set_partner_inventory({})
        await self.admin_connection.teleport(AGENT_NAME, [0, -60, 0], 0, 0, teleport_offset=[0, 0, 0])
        await self.admin_connection.teleport(PARTNER_NAME, [1, -60, 0], 0, 0, teleport_offset=[0, 0, 0])
        await self._execute("""await bot.waitForTicks(5);""", [])
        await self._execute_success(
            """
            await giveItemToOther(bot, "Partner", "stick", 2);
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 3) throw new Error("unexpected giver inventory after transfer");
            """,
            ["think", "giveItemToOther"],
        )
        await self._execute_success_for(
            PARTNER_NAME,
            """
            await bot.waitForTicks(2);
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 2) throw new Error("unexpected recipient inventory after transfer");
            """,
            [],
        )

    @test_display("giveItemToOther", "Reject an unknown partner before giving items")
    async def test_giveItemToOther_03_validates_recipient_and_item_name(self):
        await self._set_inventory({"stick": 2})
        await self._execute_error(
            """await giveItemToOther(bot, "MissingAgent", "stick", 1);""",
            "Agent MissingAgent is not registered",
            ["think", "giveItemToOther"],
        )
        await self._execute_error(
            """await giveItemToOther(bot, "Partner", "missing_item", 1);""",
            "No item named missing_item",
            ["think", "giveItemToOther"],
        )
        await self._execute_success(
            """
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 2) throw new Error("inventory changed after invalid give request");
            """,
            [],
        )

    @test_display("give/receiveItemFromOther", "Do not transfer items to a distant partner")
    async def test_giveItemToOther_04_does_not_transfer_to_distant_partner(self):
        await self._set_inventory({"stick": 2})
        await self._set_partner_inventory({"stick": 2})
        await self._execute("""await bot.waitForTicks(5);""", [])
        await self._execute_error(
            """await giveItemToOther(bot, "Partner", "stick", 1);""",
            "too far away",
            ["think", "giveItemToOther"],
        )
        await self._execute_error(
            """await receiveItemFromOther(bot, "Partner", "stick", 1);""",
            "too far away",
            ["think", "receiveItemFromOther"],
        )
        await self._execute_success(
            """
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 2) throw new Error("tester inventory changed after distant transfer");
            """,
            [],
        )
        await self._execute_success_for(
            PARTNER_NAME,
            """
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 2) throw new Error("partner inventory changed after distant transfer");
            """,
            [],
        )

    @test_display("goToPosition", "Move to the exact coordinates at distance zero")
    async def test_goToPosition_01_reaches_exact_position_at_distance_zero(self):
        await self._execute_success(
            """
            const target = new Vec3(5, -60, 0);
            await goToPosition(bot, target, 0);
            if (!new GoalNear(target.x, target.y, target.z, 0).isEnd(bot.entity.position.floored())) throw new Error(`exact goal was not reached: ${bot.entity.position}`);
            """,
            ["checkValidPosition", "think", "goToPosition"],
        )

    @test_display("goToPosition", "Floor fractional coordinates before moving")
    async def test_goToPosition_02_floors_fractional_coordinates(self):
        await self._execute_success(
            """
            const requested = new Vec3(4.8, -59.2, 3.7);
            const target = requested.floored();
            await goToPosition(bot, requested, 0);
            if (!new GoalNear(target.x, target.y, target.z, 0).isEnd(bot.entity.position.floored())) throw new Error(`floored goal was not reached: ${bot.entity.position}`);
            """,
            ["checkValidPosition", "think", "goToPosition"],
        )

    @test_display("goToPosition", "Teleport along a valid path")
    async def test_goToPosition_03_teleports_along_path(self):
        await self._execute_success(
            """
            const target = new Vec3(-5, -60, -5);
            await goToPosition(bot, target, 0, true);
            if (!new GoalNear(target.x, target.y, target.z, 0).isEnd(bot.entity.position.floored())) throw new Error(`teleport path goal was not reached: ${bot.entity.position}`);
            """,
            ["checkValidPosition", "think", "goToPosition"],
        )

    @test_display("goToPosition", "Move within distance one when the exact goal is unstandable")
    async def test_goToPosition_04_reaches_within_requested_distance_when_exact_goal_is_unstandable(self):
        await self._set_blocks(([5, -60, 0], "stone"))
        await self._execute_success(
            """
            const target = new Vec3(5, -60, 0);
            await goToPosition(bot, target, 1);
            if (!new GoalNear(target.x, target.y, target.z, 1).isEnd(bot.entity.position.floored())) throw new Error(`distance-one goal was not reached: ${bot.entity.position}`);
            if (bot.blockAt(target)?.name !== "stone") throw new Error("blocked target was changed");
            """,
            ["checkValidPosition", "think", "goToPosition"],
        )

    @test_display("goToPosition", "Do not break the goal block even at distance zero")
    async def test_goToPosition_05_does_not_break_goal_block_at_distance_zero(self):
        await self._set_blocks(([5, -60, 0], "stone"))
        await self._execute_error(
            """
            const target = new Vec3(5, -60, 0);
            const originalCanDig = bot.pathfinder.movements.canDig;
            bot.pathfinder.movements.canDig = true;
            try {
                await goToPosition(bot, target, 0);
            } finally {
                bot.pathfinder.movements.canDig = originalCanDig;
            }
            """,
            None,
            ["checkValidPosition", "think", "goToPosition"],
        )
        await self._execute_success(
            """
            if (bot.blockAt(new Vec3(5, -60, 0))?.name !== "stone") throw new Error("blocked target was changed");
            """,
            [],
        )

    @test_display("goToPosition", "Reach the goal without getting stuck on a wall")
    async def test_goToPosition_06_reaches_goal_without_getting_stuck_on_wall(self):
        await self._set_blocks(*[
            ([7, y, z], "oak_planks")
            for y in (-60, -59)
            for z in range(-2, 11)
        ])
        await self.admin_connection.teleport(AGENT_NAME, [15, -60, 4], 0, 0, teleport_offset=[0, 0, 0])
        await self._execute("""await bot.waitForTicks(30);""", [])
        await self._execute_success(
            """
            const target = new Vec3(1, -60, 4);
            await goToPosition(bot, target, 0);
            if (!new GoalNear(target.x, target.y, target.z, 0).isEnd(bot.entity.position.floored())) throw new Error(`movement goal was not reached: ${bot.entity.position}`);
            """,
            ["checkValidPosition", "think", "goToPosition"],
        )

    @test_display("goToPosition", "Do not report success when the goal is unreachable")
    async def test_goToPosition_07_does_not_report_success_for_unreachable_goal(self):
        await self._set_blocks(*[
            ([x, y, z], "stone")
            for x in range(5, 8) for z in range(-1, 2) for y in (-60, -59)
        ], ([2, -60, 0], "stone"), ([2, -59, 0], "stone"), ([6, -58, 0], "crafting_table"))
        await self.admin_connection.teleport(AGENT_NAME, [2, -58, 0], 0, 0, teleport_offset=[0, 0, 0])
        await self._execute("""await bot.waitForTicks(10);""", [])
        await self._execute_success(
            """
            if (bot.pathfinder.movements.canDig !== false) throw new Error("movement must not dig blocks");
            const target = new Vec3(6, -58, 0);
            const goal = new GoalNear(target.x, target.y, target.z, 2);
            let movementError = null;
            try {
                await goToPosition(bot, target, 2);
            } catch (error) {
                movementError = error;
            }
            if (!movementError && !goal.isEnd(bot.entity.position.floored())) throw new Error(`movement reported success outside the allowed distance: ${bot.entity.position}`);
            """,
            ["checkValidPosition", "think", "goToPosition"],
        )

    @test_display("mineBlock", "Mine and collect ore using a suitable tool")
    async def test_mineBlock_01_mines_and_collects_with_suitable_tool(self):
        position = [2, -60, 2]
        await self._set_blocks((position, "diamond_ore"))
        await self._set_inventory({"iron_pickaxe": 1}, [None, None, None, None, "dirt", None])
        await self._execute_success(
            """
            const position = new Vec3(2, -60, 2);
            await mineBlock(bot, position, "diamond_ore");
            if (bot.blockAt(position)?.name !== "air") throw new Error("diamond_ore remains after mining");
            if (bot.inventory.count(mcData.itemsByName.diamond.id) < 1) throw new Error("diamond drop was not collected");
            if (bot.heldItem?.name !== "iron_pickaxe") throw new Error(`best tool was not equipped: ${bot.heldItem?.name}`);
            """,
            ["checkValidPosition", "think", "goToPosition", "mineBlock"],
        )

    @test_display("mineBlock", "Do not mine an unexpected block at the target position")
    async def test_mineBlock_02_does_not_mine_unexpected_block(self):
        position = [2, -60, 2]
        await self._set_blocks((position, "stone"))
        await self._execute_error(
            """await mineBlock(bot, new Vec3(2, -60, 2), "dirt");""",
            "found stone instead",
            ["checkValidPosition", "think", "goToPosition", "mineBlock"],
        )
        await self._execute_success(
            """
            if (bot.blockAt(new Vec3(2, -60, 2))?.name !== "stone") throw new Error("mismatched block was changed");
            """,
            [],
        )

    @test_display("mineBlock", "Mine and collect without specifying a block name")
    async def test_mineBlock_03_mines_without_block_name(self):
        position = [2, -60, 2]
        await self._set_blocks((position, "dirt"))
        await self._execute_success(
            """
            const position = new Vec3(2, -60, 2);
            await mineBlock(bot, position, null);
            if (bot.blockAt(position)?.name !== "air") throw new Error("dirt remains after mining");
            if (bot.inventory.count(mcData.itemsByName.dirt.id) < 1) throw new Error("dirt drop was not collected");
            """,
            ["checkValidPosition", "think", "goToPosition", "mineBlock"],
        )

    @test_display("mineBlock", "Do not treat air as a mining target")
    async def test_mineBlock_04_does_not_mine_air(self):
        await self._execute_error(
            """await mineBlock(bot, new Vec3(2, -60, 2), null);""",
            "found air",
            ["checkValidPosition", "think", "goToPosition", "mineBlock"],
        )

    @test_display("mineBlock", "Report collection failure when creative mode produces no drops")
    async def test_mineBlock_05_reports_collection_failure_without_drops_in_creative_mode(self):
        position = [2, -60, 2]
        await self._set_blocks((position, "stone"))
        await self.admin_connection.exec_mc([f"/gamemode creative {AGENT_NAME}"])
        await self._execute("""await bot.waitForTicks(5);""", [])
        await self._execute_error(
            """await mineBlock(bot, new Vec3(2, -60, 2), "stone");""",
            "Mining completed, but item collection failed",
            ["checkValidPosition", "think", "goToPosition", "mineBlock"],
        )
        await self._execute_success(
            """
            if (bot.blockAt(new Vec3(2, -60, 2))?.name !== "air") throw new Error("stone remains after creative mining");
            """,
            [],
        )

    @test_display("mineBlock", "Reequip the mining tool after switching items during movement")
    async def test_mineBlock_06_reequips_mining_tool_after_movement(self):
        blocks = [
            ([x, y, z], "dirt")
            for x, z in ((-5, 0), (-7, 0), (-6, -1), (-6, 1))
            for y in (-60, -59)
        ]
        blocks.append(([6, -60, 0], "diamond_ore"))
        await self._set_blocks(*blocks)
        await self._set_inventory({"iron_pickaxe": 1, "diamond_shovel": 1}, [None, None, None, None, "iron_pickaxe", None])
        await self.admin_connection.teleport(AGENT_NAME, [-6, -60, 0], 0, 0, teleport_offset=[0, 0, 0])
        await self._execute("""await bot.waitForTicks(5);""", [])
        await self._execute_success(
            """
            const target = new Vec3(6, -60, 0);
            const originalCanDig = bot.pathfinder.movements.canDig;
            bot.pathfinder.movements.canDig = true;
            try {
                await mineBlock(bot, target, "diamond_ore");
            } finally {
                bot.pathfinder.movements.canDig = originalCanDig;
            }
            if (bot.heldItem?.name !== "iron_pickaxe") throw new Error(`mineBlock did not equip the pickaxe: ${bot.heldItem?.name}`);
            if (bot.blockAt(target)?.name !== "air") throw new Error("diamond_ore remains after mining");
            if (bot.inventory.count(mcData.itemsByName.diamond.id) < 1) throw new Error("diamond drop was not collected");
            """,
            ["checkValidPosition", "think", "goToPosition", "mineBlock"],
        )

    @test_display("mineBlock", "Distinguish mining from collection when inventory is full")
    async def test_mineBlock_07_does_not_report_success_when_drops_cannot_be_collected(self):
        position = [2, -60, 2]
        await self._set_blocks((position, "stone"))
        await self.admin_connection.exec_mc([
            f"/item replace entity {AGENT_NAME} container.{slot} with {'iron_pickaxe 1' if slot == 0 else 'dirt 64'}"
            for slot in range(36)
        ])
        await self._execute("""await bot.waitForTicks(5);""", [])
        await self._execute_error(
            """await mineBlock(bot, new Vec3(2, -60, 2), "stone");""",
            "Mining completed, but item collection failed",
            ["checkValidPosition", "think", "goToPosition", "mineBlock"],
        )
        await self._execute_success(
            """
            if (bot.blockAt(new Vec3(2, -60, 2))?.name !== "air") throw new Error("stone was not mined");
            if (bot.inventory.count(mcData.itemsByName.cobblestone.id) !== 0) throw new Error("drop was unexpectedly collected");
            """,
            [],
        )

    @test_display("placeItem", "Place cobblestone using the floor as support")
    async def test_placeItem_01_places_block_on_floor(self):
        await self._set_inventory({"cobblestone": 1})
        await self._execute_success(
            """
            const position = new Vec3(3, -60, 0);
            await goToPosition(bot, new Vec3(1, -60, 0), 0);
            await placeItem(bot, "cobblestone", position);
            if (bot.blockAt(position)?.name !== "cobblestone") throw new Error("cobblestone was not placed");
            for (let ticks = 0; bot.inventory.count(mcData.itemsByName.cobblestone.id) !== 0 && ticks < 20; ticks++) await bot.waitForTicks(1);
            if (bot.inventory.count(mcData.itemsByName.cobblestone.id) !== 0) throw new Error("inventory did not decrease after placement");
            """,
            ["checkValidPosition", "think", "goToPosition", "placeItem"],
        )

    @test_display("placeItem", "Place cobblestone using side support")
    async def test_placeItem_02_places_block_against_side_support(self):
        await self._set_blocks(([2, -59, 4], "stone"))
        await self._set_inventory({"cobblestone": 1})
        await self._execute_success(
            """
            const position = new Vec3(3, -59, 4);
            await goToPosition(bot, new Vec3(1, -60, 4), 0);
            await placeItem(bot, "cobblestone", position);
            if (bot.blockAt(position)?.name !== "cobblestone") throw new Error("side-supported block was not placed");
            """,
            ["checkValidPosition", "think", "goToPosition", "placeItem"],
        )

    @test_display("placeItem", "Reject placement using air as support")
    async def test_placeItem_03_does_not_use_air_as_support(self):
        await self._set_blocks(([0, -59, 8], "cave_air"))
        await self._set_inventory({"cobblestone": 1})
        await self._execute_error(
            """
            await goToPosition(bot, new Vec3(0, -60, 8), 0);
            await placeItem(bot, "cobblestone", new Vec3(0, -58, 8));
            """,
            "a floating block cannot be placed",
            ["checkValidPosition", "think", "goToPosition", "placeItem"],
        )
        await self._execute_success(
            """
            if (bot.inventory.count(mcData.itemsByName.cobblestone.id) !== 1) throw new Error("inventory changed after rejected placement");
            """,
            [],
        )

    @test_display("placeItem", "Do not place a block at an occupied position")
    async def test_placeItem_04_does_not_place_at_occupied_position(self):
        position = [3, -60, 0]
        await self._set_blocks((position, "stone"))
        await self._set_inventory({"cobblestone": 1})
        await self.admin_connection.teleport(AGENT_NAME, [1, -60, 0], 0, 0, teleport_offset=[0, 0, 0])
        await self._execute("""await bot.waitForTicks(5);""", [])
        await self._execute_error(
            """await placeItem(bot, "cobblestone", new Vec3(3, -60, 0));""",
            None,
            ["checkValidPosition", "think", "placeItem"],
        )
        await self._execute_success(
            """
            const position = new Vec3(3, -60, 0);
            if (bot.blockAt(position)?.name !== "stone") throw new Error("existing block was changed");
            if (bot.inventory.count(mcData.itemsByName.cobblestone.id) !== 1) throw new Error("inventory changed after occupied placement");
            """,
            [],
        )

    @test_display("placeItem", "Move from a distance, equip the item, and place it")
    async def test_placeItem_05_places_without_prior_movement_or_equipping(self):
        await self._set_inventory({"cobblestone": 1, "dirt": 1}, [None, None, None, None, "dirt", None])
        await self._execute_success(
            """
            const position = new Vec3(3, -60, 0);
            if (bot.entity.position.distanceTo(position) < 10) throw new Error("bot did not start far from the placement target");
            if (bot.heldItem?.name !== "dirt") throw new Error("bot was not holding the other item");
            await placeItem(bot, "cobblestone", position);
            if (bot.blockAt(position)?.name !== "cobblestone") throw new Error("cobblestone was not placed");
            for (let ticks = 0; bot.inventory.count(mcData.itemsByName.cobblestone.id) !== 0 && ticks < 20; ticks++) await bot.waitForTicks(1);
            if (bot.inventory.count(mcData.itemsByName.cobblestone.id) !== 0) throw new Error("placed cobblestone remains in inventory");
            if (bot.inventory.count(mcData.itemsByName.dirt.id) !== 1) throw new Error("other held item changed");
            """,
            ["checkValidPosition", "think", "goToPosition", "placeItem"],
        )

    @test_display("receiveItemFromOther", "Do not receive items when the partner has insufficient sticks")
    async def test_receiveItemFromOther_01_does_not_receive_when_partner_inventory_is_insufficient(self):
        await self.admin_connection.exec_mc(["/scoreboard objectives remove itemQuery"])
        await self._set_inventory({})
        await self._set_partner_inventory({"stick": 1})
        await self.admin_connection.teleport(AGENT_NAME, [0, -60, 0], 0, 0, teleport_offset=[0, 0, 0])
        await self.admin_connection.teleport(PARTNER_NAME, [1, -60, 0], 0, 0, teleport_offset=[0, 0, 0])
        await self._execute("""await bot.waitForTicks(5);""", [])
        await self._execute_error(
            """await receiveItemFromOther(bot, "Partner", "stick", 2);""",
            "does not have enough stick",
            ["think", "receiveItemFromOther"],
        )
        await self._execute_success_for(
            PARTNER_NAME,
            """
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 1) throw new Error("source inventory changed after rejected receipt");
            """,
            [],
        )

    @test_display("receiveItemFromOther", "Receive two sticks from a nearby partner")
    async def test_receiveItemFromOther_02_receives_items_from_registered_partner(self):
        await self.admin_connection.exec_mc(["/scoreboard objectives remove itemQuery"])
        await self._set_inventory({})
        await self._set_partner_inventory({"stick": 5})
        await self.admin_connection.teleport(AGENT_NAME, [0, -60, 0], 0, 0, teleport_offset=[0, 0, 0])
        await self.admin_connection.teleport(PARTNER_NAME, [1, -60, 0], 0, 0, teleport_offset=[0, 0, 0])
        await self._execute("""await bot.waitForTicks(5);""", [])
        await self._execute_success(
            """
            await receiveItemFromOther(bot, "Partner", "stick", 2);
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 2) throw new Error("unexpected receiver inventory after transfer");
            """,
            ["think", "receiveItemFromOther"],
        )
        await self._execute_success_for(
            PARTNER_NAME,
            """
            await bot.waitForTicks(2);
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 3) throw new Error("unexpected source inventory after transfer");
            """,
            [],
        )

    @test_display("receiveItemFromOther", "Reject an unknown sender or item before receiving")
    async def test_receiveItemFromOther_03_validates_sender_and_item_name(self):
        await self._set_inventory({})
        await self._set_partner_inventory({"stick": 2})
        await self._execute_error(
            """await receiveItemFromOther(bot, "MissingAgent", "stick", 1);""",
            "Agent MissingAgent is not registered",
            ["think", "receiveItemFromOther"],
        )
        await self._execute_error(
            """await receiveItemFromOther(bot, "Partner", "missing_item", 1);""",
            "No item named missing_item",
            ["think", "receiveItemFromOther"],
        )
        await self._execute_success_for(
            PARTNER_NAME,
            """
            if (bot.inventory.count(mcData.itemsByName.stick.id) !== 2) throw new Error("partner inventory changed after invalid receive request");
            """,
            [],
        )

    @test_display("smeltItem", "Smelt and collect two iron ores")
    async def test_smeltItem_01_collects_multiple_smelting_results(self):
        position = [2, -60, 0]
        await self._set_blocks((position, "furnace"))
        await self._set_inventory({"raw_iron": 2, "oak_planks": 2})
        await self._execute_success(
            """
            await smeltItem(bot, "raw_iron", "oak_planks", new Vec3(2, -60, 0), 2);
            if (bot.inventory.count(mcData.itemsByName.iron_ingot.id) !== 2) throw new Error("unexpected iron_ingot count after smelting");
            """,
            ["checkValidPosition", "think", "goToPosition", "smeltItem"],
        )

    @test_display("smeltItem", "Reject a block that is not a furnace")
    async def test_smeltItem_02_rejects_non_furnace_blocks(self):
        await self._set_blocks(([2, -60, 0], "stone"))
        await self._set_inventory({"raw_iron": 1, "coal": 1})
        await self._execute_error(
            """await smeltItem(bot, "raw_iron", "coal", new Vec3(2, -60, 0));""",
            "No furnace was found",
            ["checkValidPosition", "think", "goToPosition", "smeltItem"],
        )

    @test_display("smeltItem", "Do not process existing furnace contents")
    async def test_smeltItem_03_does_not_use_furnace_with_existing_contents(self):
        position = [2, -60, 0]
        await self._set_blocks((position, "furnace"))
        await self.admin_connection.exec_mc(["/item replace block 2 -60 0 container.0 with raw_iron 1"])
        await self._set_inventory({"raw_iron": 1, "coal": 1})
        await self._execute_error(
            """await smeltItem(bot, "raw_iron", "coal", new Vec3(2, -60, 0));""",
            "Furnace input and output slots must be empty",
            ["checkValidPosition", "think", "goToPosition", "smeltItem"],
        )
        await self._execute_success(
            """
            const furnace = await bot.openFurnace(bot.blockAt(new Vec3(2, -60, 0)));
            const input = furnace.inputItem();
            await furnace.close();
            if (input?.name !== "raw_iron" || input.count !== 1) throw new Error(`existing furnace input changed: ${input?.name} ${input?.count}`);
            """,
            [],
        )

    @test_display("smeltItem", "Report missing materials and completed count after one smelt")
    async def test_smeltItem_04_reports_completed_smelts_on_partial_failure(self):
        position = [2, -60, 0]
        await self._set_blocks((position, "furnace"))
        await self._set_inventory({"raw_iron": 1, "coal": 1})
        await self._execute_error(
            """await smeltItem(bot, "raw_iron", "coal", new Vec3(2, -60, 0), 2);""",
            "completed 1",
            ["checkValidPosition", "think", "goToPosition", "smeltItem"],
        )
        await self._execute_success(
            """
            if (bot.inventory.count(mcData.itemsByName.iron_ingot.id) !== 1) throw new Error("unexpected output after partial smelting");
            """,
            [],
        )

    @test_display("think", "Reject an empty message")
    async def test_think_01_rejects_empty_message(self):
        await self._execute_error(
            """await think(bot, "");""",
            "msg for think cannot be empty",
            ["think"],
        )

    @test_display("think", "Send a thought message to self")
    async def test_think_02_sends_thought_message(self):
        await self._execute_success(
            """
            let received = false;
            const handler = message => {
                if (message.toString().includes("primitive-test-thought")) received = true;
            };
            bot.on("message", handler);
            try {
                await think(bot, "primitive-test-thought");
                for (let ticks = 0; !received && ticks < 20; ticks++) await bot.waitForTicks(1);
                if (!received) throw new Error("self-directed thought message was not observed");
            } finally {
                bot.removeListener("message", handler);
            }
            """,
            ["think"],
        )
    @test_display("turnOn/OffLever", "Do not toggle a lever already in the target state")
    async def test_turnOffLever_01_preserves_lever_already_in_target_state(self):
        position = [2, -60, 0]
        await self.admin_connection.set_blocks([{
            "position": position,
            "name": "lever",
            "properties": {"face": "floor", "facing": "north", "powered": "false"},
        }])
        await self._execute_success(
            """
            const position = new Vec3(2, -60, 0);
            await turnOffLever(bot, position);
            if (![false, "false"].includes(bot.blockAt(position)?.getProperties().powered)) throw new Error("already-off lever changed state");
            await turnOnLever(bot, position);
            for (let ticks = 0; ![true, "true"].includes(bot.blockAt(position)?.getProperties().powered) && ticks < 20; ticks++) await bot.waitForTicks(1);
            if (![true, "true"].includes(bot.blockAt(position)?.getProperties().powered)) throw new Error("lever did not turn on before retry");
            await turnOnLever(bot, position);
            if (![true, "true"].includes(bot.blockAt(position)?.getProperties().powered)) throw new Error("already-on lever changed state");
            """,
            ["checkValidPosition", "think", "goToPosition", "useLever"],
        )

    @test_display("turnOnLever", "Report a missing lever after movement")
    async def test_turnOnLever_01_reports_missing_lever(self):
        await self._execute_error(
            """await turnOnLever(bot, new Vec3(2, -60, 0));""",
            "Lever not found",
            ["checkValidPosition", "think", "goToPosition", "useLever"],
        )



    @test_display("turnOn/OffLever", "Turn a lever on and then off")
    async def test_turnOnLever_02_toggles_lever_on_and_off(self):
        position = [2, -60, 0]
        await self.admin_connection.set_blocks([{
            "position": position,
            "name": "lever",
            "properties": {"face": "floor", "facing": "north", "powered": "false"},
        }])
        await self._execute_success(
            """
            const position = new Vec3(2, -60, 0);
            await turnOnLever(bot, position);
            for (let ticks = 0; ![true, "true"].includes(bot.blockAt(position)?.getProperties().powered) && ticks < 20; ticks++) await bot.waitForTicks(1);
            if (![true, "true"].includes(bot.blockAt(position)?.getProperties().powered)) throw new Error("lever did not turn on");
            await turnOffLever(bot, position);
            for (let ticks = 0; ![false, "false"].includes(bot.blockAt(position)?.getProperties().powered) && ticks < 20; ticks++) await bot.waitForTicks(1);
            if (![false, "false"].includes(bot.blockAt(position)?.getProperties().powered)) throw new Error("lever did not turn off");
            """,
            ["checkValidPosition", "think", "goToPosition", "useLever"],
        )

    @test_display("turnOnLever", "Do not turn on a lever blocked by a wall")
    async def test_turnOnLever_03_does_not_operate_lever_blocked_by_wall(self):
        walls = [
            ([x, y, z], "stone")
            for x in range(3, 6) for z in range(7, 10) for y in (-60, -59, -58)
            if x != 4 or z != 8
        ]
        await self._set_blocks(*walls)
        await self.admin_connection.set_blocks([{
            "position": [4, -60, 8],
            "name": "lever",
            "properties": {"face": "floor", "facing": "north", "powered": "false"},
        }])
        await self._execute_success(
            """if (bot.pathfinder.movements.canDig !== false) throw new Error("movement must not dig blocks");""",
            [],
        )
        result = await self._execute(
            """
            await turnOnLever(bot, new Vec3(4, -60, 8));
            """,
            ["checkValidPosition", "think", "goToPosition", "useLever"],
        )
        await self._execute_success(
            """
            const position = new Vec3(4, -60, 8);
            await bot.waitForTicks(5);
            for (const [x, z] of [[3, 8], [5, 8], [4, 7], [4, 9]]) {
                if (bot.blockAt(new Vec3(x, -60, z))?.name !== "stone") throw new Error(`wall at ${x},${z} was removed`);
            }
            if (![false, "false"].includes(bot.blockAt(position)?.getProperties().powered)) throw new Error(`lever was switched on; bot position=${bot.entity.position}`);
            """,
            [],
        )
        self.assertFalse(result.get("success"), "Operating a lever blocked by a wall was reported as successful")


if __name__ == "__main__":
    unittest.main()
