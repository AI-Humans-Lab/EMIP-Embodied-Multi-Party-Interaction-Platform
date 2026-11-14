import json
import queue
import unittest

from modules.minecraft_observation import MinecraftObservationProcessor
from modules.observation_source import MinecraftObservationConnection, ObservationsProcessedMarker, _ObservationConnectionState


class ObservationProcessingTests(unittest.TestCase):
    def test_observation_01_receives_events_before_ticks(self):
        event = {"type": "chat", "data": {"player": {"name": "Sally"}, "message": "hello"}}
        connection = object.__new__(MinecraftObservationConnection)
        connection.incoming_queue = queue.Queue()
        connection.state = _ObservationConnectionState(websocket=object())
        processor = MinecraftObservationProcessor(initial_tick=10)
        connection.incoming_queue.put(json.dumps(event))

        self.assertEqual(connection.receive(timeout=0), event)
        self.assertEqual(processor.process(event), [])
        self.assertEqual(len(processor.pending_events), 1)

    def test_observation_02_processes_batched_ticks_and_interleaved_events_in_order(self):
        batch = {"type": "event_batch", "items": [
            {"type": "players_tick", "tick": 11, "data": {"players": {}}},
            {"type": "chat", "data": {"player": {"name": "Sally"}, "message": "hello"}},
            {"type": "players_tick", "tick": 12, "data": {"players": {}}},
            {"type": "players_tick", "tick": 13, "data": {"players": {}}},
        ]}
        connection = object.__new__(MinecraftObservationConnection)
        connection.incoming_queue = queue.Queue()
        connection.state = _ObservationConnectionState(websocket=object())
        processor = MinecraftObservationProcessor(initial_tick=10)
        connection.incoming_queue.put(json.dumps(batch))

        tick_observations = processor.process(connection.receive(timeout=0))
        self.assertEqual(connection.pending_message_count, 0)

        self.assertEqual([tick_observation["globalTick"] for tick_observation in tick_observations], [11, 12, 13])
        self.assertEqual([event["visible"]["msg"] for event in tick_observations[1]["objective"]["events"]], ["hello"])
        self.assertEqual(connection.pending_message_count, 0)

    def test_observation_03_notifies_completion_after_batched_ticks(self):
        connection = object.__new__(MinecraftObservationConnection)
        connection.incoming_queue = queue.Queue()
        connection.state = _ObservationConnectionState(websocket=object())
        processor = MinecraftObservationProcessor(initial_tick=10)
        connection.incoming_queue.put(json.dumps({"type": "event_batch", "items": [
            {"type": "players_tick", "tick": tick, "data": {"players": {}}} for tick in (11, 12, 13)
        ] + [{"type": "chat", "data": {"player": {"name": "Sally"}, "message": "before barrier"}}]}))
        observations_processed_marker = connection.register_observations_processed_marker()
        connection.incoming_queue.put(json.dumps({"type": "players_tick", "tick": 14, "data": {"players": {}}}))

        queued_tick_observations = queue.Queue()
        tick_observations = processor.process(connection.receive(timeout=0))
        for tick_observation in tick_observations[1:]:
            queued_tick_observations.put(tick_observation)
        self.assertEqual([tick_observations[0]["globalTick"]] + [queued_tick_observations.get_nowait()["globalTick"] for _ in range(2)], [11, 12, 13])
        self.assertIs(connection.receive(timeout=0), observations_processed_marker)
        self.assertIsInstance(observations_processed_marker, ObservationsProcessedMarker)
        self.assertFalse(observations_processed_marker.done())
        self.assertEqual(len(processor.pending_events), 1)
        observations_processed_marker.set_result(None)
        self.assertIsNone(observations_processed_marker.result())
        next_tick_observation = processor.process(connection.receive(timeout=0))[0]
        self.assertEqual([event["visible"]["msg"] for event in next_tick_observation["objective"]["events"]], ["before barrier"])

    def test_observation_04_reports_failure_when_observations_are_discarded(self):
        connection = object.__new__(MinecraftObservationConnection)
        connection.incoming_queue = queue.Queue()
        connection.state = _ObservationConnectionState(websocket=object())
        observations_processed_marker = connection.register_observations_processed_marker()
        connection.discard(timeout=0)
        with self.assertRaisesRegex(RuntimeError, "Observation processing is disabled"):
            observations_processed_marker.result()

    def test_observation_05_notifies_completion_on_stop_marker_after_discard(self):
        connection = object.__new__(MinecraftObservationConnection)
        connection.incoming_queue = queue.Queue()
        connection.state = _ObservationConnectionState(websocket=object())
        connection.incoming_queue.put("before")
        processing_stop_marker = connection.register_processing_stop_marker()
        connection.incoming_queue.put("after")

        connection.discard(timeout=0)
        self.assertFalse(processing_stop_marker.done())
        connection.discard(timeout=0)
        self.assertIsNone(processing_stop_marker.result())
        self.assertEqual(connection.pending_message_count, 1)


if __name__ == "__main__":
    unittest.main()
