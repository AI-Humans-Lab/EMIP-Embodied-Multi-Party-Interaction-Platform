import unittest

import numpy as np

from modules.belief import Vec3Map
from modules.visibility import Vec3BoolMap, BlockSpatialIndex, build_block_arrays, build_visibility_data, get_block_visibility, get_player_visibility


class VisibilityTests(unittest.TestCase):
    def test_visibility_03_visibility_matches_after_cross_cell_movement_and_block_updates(self):
        env_box = [[-18, -1, -2], [18, 1, 2]]
        blocks = Vec3Map([
            {"position": [x, y, z], "name": "stone" if (x, y, z) in {(-16, 0, 0), (0, 0, 0), (16, 0, 0)} else "air"}
            for x in range(-18, 19) for y in range(-1, 2) for z in range(-2, 3)
        ])
        index = BlockSpatialIndex(blocks)

        def check(position):
            players = {"Sally": position, "Bob": [position[0] + 1, position[1], position[2]]}
            full_arrays = build_block_arrays(blocks, ())
            nearby_arrays = index.near(position, env_box)
            shared_data = build_visibility_data(env_box, position, nearby_arrays)
            self.assertEqual(
                get_player_visibility(env_box, players, blocks, "Sally", block_arrays=full_arrays),
                get_player_visibility(env_box, players, blocks, "Sally", block_arrays=nearby_arrays, visibility_data=shared_data),
            )
            full_visibility = get_block_visibility(env_box, players, blocks, "Sally", block_arrays=full_arrays)
            nearby_visibility = get_block_visibility(env_box, players, blocks, "Sally", block_arrays=nearby_arrays, visibility_data=shared_data)
            self.assertEqual(full_visibility.range[0].tolist(), nearby_visibility.range[0].tolist())
            self.assertEqual(full_visibility.range[1].tolist(), nearby_visibility.range[1].tolist())
            self.assertEqual(full_visibility.data.tolist(), nearby_visibility.data.tolist())

        for position in ([-17.5, 0, 0], [-0.5, 0, 0], [15.5, 0, 0]):
            check(position)

        changed = {"position": {"__Vec3__": [-16, 0, 0]}, "name": "air"}
        before = index.near([-17.5, 0, 0], env_box)
        self.assertEqual({(x, y, z): opaque for x, y, z, opaque in zip(*before)}[(-16, 0, 0)], 1)
        blocks.set(changed["position"]["__Vec3__"], {"name": "air"})
        index.update(changed)
        after = index.near([-17.5, 0, 0], env_box)
        self.assertEqual({(x, y, z): opaque for x, y, z, opaque in zip(*after)}[(-16, 0, 0)], 0)
        check([-17.5, 0, 0])


    def test_visibility_02_detects_newly_visible_positions_when_observation_bounds_move(self):
        previous = Vec3BoolMap(([0, 0, 0], [2, 0, 0]))
        previous.add([1, 0, 0])
        current = Vec3BoolMap(([1, 0, 0], [3, 0, 0]))
        current.add([1, 0, 0])
        current.add([3, 0, 0])

        self.assertEqual(current.new_indices(previous).tolist(), [2])

    def test_visibility_01_view_distance_changes_player_and_block_visibility(self):
        env_box = [[0, 0, 0], [20, 2, 0]]
        blocks = Vec3Map([{"position": [x, y, 0], "name": "air"} for x in range(21) for y in range(3)])
        players = {"Sally": [0.5, 0, 0.5], "Bob": [15.5, 0, 0.5]}
        index = BlockSpatialIndex(blocks)
        target_block = np.array([15, 0, 0])

        for distance, expected in ((10.0, False), (20.0, True)):
            arrays = index.near(players["Sally"], env_box, max_dist=distance)
            shared_data = build_visibility_data(env_box, players["Sally"], arrays, max_dist=distance)
            player_vis = get_player_visibility(env_box, players, blocks, "Sally", max_dist=distance, block_arrays=arrays, visibility_data=shared_data)
            block_vis = get_block_visibility(env_box, players, blocks, "Sally", max_dist=distance, block_arrays=arrays, visibility_data=shared_data)
            self.assertEqual(player_vis["Bob"], expected)
            self.assertEqual(block_vis.has(target_block), expected)

if __name__ == "__main__":
    unittest.main()
