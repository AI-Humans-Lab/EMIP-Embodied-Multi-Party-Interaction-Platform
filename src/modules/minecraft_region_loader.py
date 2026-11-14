"""Java Edition 1.18+ overworld blocks from region files."""

import gzip
import io
import zlib
from itertools import product
from pathlib import Path
from typing import TypedDict

from nbt import nbt, region
from nbt.world import AnvilWorldFolder
from mca import Chunk as McapyChunk

JAVA_1_18_DATA_VERSION = 2860
CHUNK_SIZE = 16
CHUNKS_PER_REGION = 32
EXTERNAL_CHUNK_FLAG = 0x80
MIN_Y, MAX_Y = -64, 319


class BlockPlacement(TypedDict):
    position: list[int]  # XYZ
    name: str
    properties: dict[str, str]


def load_block_region(world_directory: str | Path, bounds=None, *, allow_mcc: bool = True) -> list[BlockPlacement]:
    """保存済みのJava版Minecraftワールドから、指定範囲または保存済み範囲のブロックを読み込む。

    未生成チャンクは生成せず、空気を含む元座標の一覧を返す。
    """
    region_directory = Path(world_directory) / "region"
    if not region_directory.is_dir():
        raise FileNotFoundError(f"Overworld region directory not found: {region_directory}")
    _validate_bounds(bounds)
    chunk_positions = _get_chunk_positions(bounds)
    chunk_nbt_iter = _load_chunk_data(world_directory, chunk_positions, allow_mcc)
    
    blocks = []
    for chunk_nbt in chunk_nbt_iter:
        cx, cz = chunk_nbt["xPos"].value, chunk_nbt["zPos"].value
        block_positions = product(range(MIN_Y, MAX_Y + 1), range(cz * CHUNK_SIZE, (cz + 1) * CHUNK_SIZE), range(cx * CHUNK_SIZE, (cx + 1) * CHUNK_SIZE))

        mcapy_chunk = _convert_chunk(chunk_nbt)
        chunk_blocks = ({"position": [x, y, z], "name": block.name().removeprefix("minecraft:"),
                       "properties": {key: str(value) for key, value in block.properties.items()}}
                      for (y, z, x), block in zip(block_positions, mcapy_chunk.stream_chunk()))
        blocks.extend(_select_blocks_in_bounds(chunk_blocks, bounds))
    if not blocks:
        raise ValueError(f"No saved overworld chunks found: {region_directory}")
    # Ordinary supported blocks should be placed after their support.
    return sorted(blocks, key=lambda block: (block["position"][1], block["position"][2], block["position"][0]))


def _validate_bounds(bounds):
    """読込範囲が整数のXYZ最小・最大座標で、順序と高さの範囲が有効かを検証する。"""
    if bounds is None:
        return None
    if len(bounds) != 2 or any(len(position) != 3 or any(type(value) is not int for value in position) for position in bounds):
        raise ValueError("Bounds must contain two integer XYZ positions")
    minimum, maximum = bounds
    if any(low > high for low, high in zip(minimum, maximum)):
        raise ValueError("Bounds minimum must not exceed maximum")
    if not MIN_Y <= minimum[1] <= maximum[1] <= MAX_Y:
        raise ValueError(f"Source Y bounds must be inside {MIN_Y}..{MAX_Y}")



def _get_chunk_positions(bounds) -> set[tuple[int, int]] | None:
    """指定したブロック範囲と重なるチャンク座標を列挙する。

    範囲未指定なら全チャンク読込を示すNoneを返す。
    """
    if bounds is None:
        return None
    minimum, maximum = bounds
    return set(product(range(minimum[0] // CHUNK_SIZE, maximum[0] // CHUNK_SIZE + 1), range(minimum[2] // CHUNK_SIZE, maximum[2] // CHUNK_SIZE + 1)))


def _convert_chunk(chunk_nbt: nbt.NBTFile) -> McapyChunk:
    """保存チャンクの対応バージョンと生成状態を確認し、単一パレットの互換処理を施してブロック読込用のチャンクへ変換する。"""
    _validate_saved_chunk(chunk_nbt)
    # mcapy requires data even for single palettes, where Minecraft allows it to be omitted.
    for section in chunk_nbt["sections"]:
        if "block_states" in section and len(section["block_states"]["palette"]) == 1 and "data" not in section["block_states"]:
            section["block_states"]["data"] = nbt.TAG_Long_Array()
            section["block_states"]["data"].value = [0] * (CHUNK_SIZE ** 3 // (64 // 4))  # Four-bit indexes packed into 64-bit words.
    return McapyChunk(chunk_nbt)


def _validate_saved_chunk(chunk_nbt: nbt.NBTFile):
    """保存チャンクが対応するMinecraftバージョンで、生成完了済みであることを確認する。"""
    if chunk_nbt["DataVersion"].value < JAVA_1_18_DATA_VERSION:
        raise ValueError("Only Java Edition 1.18+ chunk format is supported")
    if chunk_nbt["Status"].value not in ("full", "minecraft:full"):
        raise ValueError(f"Saved chunk is not fully generated: {(chunk_nbt['xPos'].value, chunk_nbt['zPos'].value)}")


def _select_blocks_in_bounds(blocks, bounds):
    """読み込んだブロック一覧から、最小・最大座標を含む指定範囲内のブロックだけを選ぶ。"""
    return [block for block in blocks if bounds is None or all(lo <= value <= hi for lo, value, hi in zip(bounds[0], block["position"], bounds[1]))]


def _load_chunk_data(world_directory, chunk_positions, allow_mcc):
    """ワールド保存先から対象チャンクのNBTデータを読む。

    許可された場合は外部.mccファイルへの読み替えも行う。
    """
    world = AnvilWorldFolder(str(world_directory))
    if chunk_positions is None:
        chunk_positions = ((reader.loc.x * CHUNKS_PER_REGION + metadata.x, reader.loc.z * CHUNKS_PER_REGION + metadata.z)
                           for reader in world.iter_regions() for metadata in reader.get_metadata())
    for cx, cz in sorted(chunk_positions):
        try:
            yield world.get_nbt(cx, cz)
        except region.ChunkHeaderError:
            if not allow_mcc:
                raise
            yield _read_mcc_chunk(world, cx, cz)


def _read_mcc_chunk(world: AnvilWorldFolder, cx: int, cz: int) -> nbt.NBTFile:
    """リージョン情報から外部保存された.mccチャンクを探し、圧縮を解いてNBTデータとして読み込む。"""
    # NBT does not support external .mcc files, so read their payload separately.
    reader = world.get_region(cx // CHUNKS_PER_REGION, cz // CHUNKS_PER_REGION)
    compression = reader.metadata[cx % CHUNKS_PER_REGION, cz % CHUNKS_PER_REGION].compression
    decompress = {EXTERNAL_CHUNK_FLAG | region.COMPRESSION_GZIP: gzip.decompress, EXTERNAL_CHUNK_FLAG | region.COMPRESSION_ZLIB: zlib.decompress}[compression]
    compressed_data = Path(reader.filename).with_name(f"c.{cx}.{cz}.mcc").read_bytes()
    return nbt.NBTFile(buffer=io.BytesIO(decompress(compressed_data)))
