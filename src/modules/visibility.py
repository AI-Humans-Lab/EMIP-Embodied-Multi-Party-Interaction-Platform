from __future__ import annotations

import base64
import math
from typing import Any, Sequence

import numpy as np
from numba import njit, types


PLAYER_EYE_HEIGHT = 1.6
_INT32_VECTOR = types.int32[::1]
_UINT8_VECTOR = types.uint8[::1]
_FLOAT64_VECTOR = types.float64[::1]
#_FACE_SAMPLE_U = np.array([0.01, 0.01, 0.99, 0.99, 0.5], dtype=np.float64)
#_FACE_SAMPLE_V = np.array([0.01, 0.99, 0.01, 0.99, 0.5], dtype=np.float64)
_FACE_SAMPLE_U = np.array([0.5], dtype=np.float64)
_FACE_SAMPLE_V = np.array([0.5], dtype=np.float64)



_TRANSPARENT_BLOCK_NAMES = {
    "air",
    "cave_air",
    "void_air",
    "water",
    "lava",
    "grass",
    "tall_grass",
    "fern",
    "large_fern",
    "dead_bush",
    "vine",
    "ladder",
    "lever",
    "tripwire",
    "tripwire_hook",
    "redstone_wire",
    "torch",
    "wall_torch",
    "soul_torch",
    "soul_wall_torch",
    "rail",
    "powered_rail",
    "detector_rail",
    "activator_rail",
    "seagrass",
    "tall_seagrass",
    "kelp",
    "kelp_plant",
    "sugar_cane",
    "snow",
}


class Vec3BoolMap:
    def __init__(self, range_):
        """最小・最大座標を含む範囲に対して、座標ごとの真偽値を保存する配列を作り、すべてFalseで初期化する。"""
        self.range = (np.array(range_[0]), np.array(range_[1]))
        self.size = self.range[1] - self.range[0] + 1
        total_size = int(np.prod(self.size))
        self.data = np.zeros(total_size, dtype=bool)

    def _is_within_range(self, vec: np.ndarray) -> bool:
        """指定座標が、この真偽値マップの最小・最大座標の範囲内かを調べる。"""
        return np.all(vec >= self.range[0]) and np.all(vec <= self.range[1])

    def _to_index(self, vec: np.ndarray) -> int:
        """XYZ座標を、X方向を最も細かく並べた1次元配列の位置へ変換する。"""
        rel = vec - self.range[0]
        return int(rel[0] + self.size[0] * (rel[1] + self.size[1] * rel[2]))

    def _from_index(self, index: int) -> np.ndarray:
        """1次元配列の位置から、ワールド内のXYZ座標を復元する。"""
        sx, sy, sz = int(self.size[0]), int(self.size[1]), int(self.size[2])
        x = index % sx
        y = (index // sx) % sy
        z = index // (sx * sy)
        return np.array([x, y, z]) + self.range[0]

    def to_base64(self) -> str:
        """座標ごとの真偽値をビット列に詰め、通信や保存に使うBase64文字列へ変換する。"""
        bits = self.data.astype(np.uint8)
        pad = (-len(bits)) % 8
        if pad:
            bits = np.pad(bits, (0, pad), mode="constant", constant_values=0)
        byte_arr = np.packbits(bits, bitorder="big")
        return base64.b64encode(byte_arr.tobytes()).decode("ascii")

    def from_base64(self, base64_str: str) -> None:
        """Base64のビット列を読み込み、このマップの範囲に対応する真偽値配列へ復元する。"""
        byte_array = base64.b64decode(base64_str)
        binary_str = "".join(f"{byte:08b}" for byte in byte_array)
        total_size = int(np.prod(self.size))
        self.data = np.array([bit == "1" for bit in binary_str[:total_size]], dtype=bool)

    def add(self, vec) -> None:
        """指定座標をTrueに設定する。

        3要素の座標でない場合や範囲外の場合はエラーにする。
        """
        v = np.asarray(vec, dtype=int)
        if v.shape != (3,):
            raise ValueError("vec must be length-3 (x, y, z).")
        if not self._is_within_range(v):
            raise ValueError("Vec3 is out of the specified range.")
        idx = self._to_index(v)
        self.data[idx] = True

    def add_indices(self, idxs, *, safe: bool = False) -> None:
        """配列の位置で指定された複数の座標をまとめてTrueにする。

        safe指定時は範囲外の位置を検査する。
        """
        idxs = np.asarray(idxs)
        if idxs.ndim != 1:
            idxs = idxs.ravel()
        if idxs.dtype != np.int64:
            idxs = idxs.astype(np.int64, copy=False)
        if safe:
            n = self.data.size
            if idxs.size and (idxs.min() < 0 or idxs.max() >= n):
                raise ValueError("index out of range")
        self.data[idxs] = True

    def has(self, vec: np.ndarray) -> bool:
        """指定座標の真偽値を返す。

        マップの範囲外ならFalseを返す。
        """
        if not self._is_within_range(vec):
            return False
        return bool(self.data[self._to_index(vec)])

    def get_all(self) -> list[np.ndarray]:
        """Trueになっているすべての位置をXYZ座標の一覧として返す。"""
        true_indices = np.flatnonzero(self.data)
        return [self._from_index(int(i)) for i in true_indices]

    def new_indices(self, previous: Vec3BoolMap | None) -> np.ndarray:
        """今回Trueで、前回のマップではTrueでなかった位置を配列の添字で返す。

        前回と今回で範囲が違う場合も座標を合わせて比較する。
        """
        visible = self.data.reshape(tuple(int(value) for value in self.size[::-1])).copy()
        if previous is not None:
            lower = np.maximum(self.range[0], previous.range[0])
            upper = np.minimum(self.range[1], previous.range[1])
            if np.all(lower <= upper):
                current_slices = tuple(slice(int(lower[i] - self.range[0][i]), int(upper[i] - self.range[0][i]) + 1) for i in (2, 1, 0))
                previous_slices = tuple(slice(int(lower[i] - previous.range[0][i]), int(upper[i] - previous.range[0][i]) + 1) for i in (2, 1, 0))
                visible[current_slices] &= ~previous.data.reshape(tuple(int(value) for value in previous.size[::-1]))[previous_slices]
        return np.flatnonzero(visible)


def _as_v3(v) -> np.ndarray:
    """座標を浮動小数点数のNumPy配列へ変換し、XYZの3要素であることを確認する。"""
    arr = np.asarray(v, dtype=np.float64)
    if arr.shape != (3,):
        raise ValueError(f"Expected shape (3,), got {arr.shape}")
    return arr


def _as_v3_int(v) -> np.ndarray:
    """座標を32bit整数のNumPy配列へ変換し、XYZの3要素であることを確認する。"""
    arr = np.asarray(v, dtype=np.int32)
    if arr.shape != (3,):
        raise ValueError(f"Expected shape (3,), got {arr.shape}")
    return arr


def _is_transparent_block(block_name: str, extra_transparent_blocks: set[str]) -> bool:
    """ブロック名が視線を遮らない扱いの種類かを判定する。

    組み込みの一覧・名前の規則・追加指定を使う。
    """
    if block_name in _TRANSPARENT_BLOCK_NAMES or block_name in extra_transparent_blocks:
        return True
    if "glass" in block_name or "pane" in block_name:
        return True
    if block_name.endswith("_button"):
        return True
    if block_name.endswith("_pressure_plate"):
        return True
    if block_name.endswith("_carpet"):
        return True
    if block_name.endswith("_torch"):
        return True
    if block_name.endswith("_rail"):
        return True
    return False


def build_block_arrays(
    block_memory: Any,
    extra_transparent_blocks: Sequence[str],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """ブロック記憶を、XYZ座標と視線を遮るかどうかの配列に変換し、可視性計算へ渡せる形にする。"""
    extra_transparent = {str(name) for name in extra_transparent_blocks}
    data = block_memory.data
    count = len(data)

    if count == 0:
        empty = np.empty(0, dtype=np.int32)
        return empty, empty, empty, empty.astype(np.uint8)

    xs = np.empty(count, dtype=np.int32)
    ys = np.empty(count, dtype=np.int32)
    zs = np.empty(count, dtype=np.int32)
    opaque = np.empty(count, dtype=np.uint8)

    for i, (pos, block) in enumerate(data.items()):
        x = int(pos[0])
        y = int(pos[1])
        z = int(pos[2])
        xs[i] = x
        ys[i] = y
        zs[i] = z

        block_name = str(block.get("name", "air"))
        opaque[i] = 0 if _is_transparent_block(block_name, extra_transparent) else 1

    return xs, ys, zs, opaque


class BlockSpatialIndex:
    SECTION_SIZE = 16

    def __init__(self, block_memory: Any):
        """ブロックを16ブロック単位の領域に分け、近傍の遮蔽物を素早く取り出すための索引とキャッシュを作る。"""
        section_size = self.SECTION_SIZE
        sections: dict[tuple[int, int, int], dict[tuple[int, int, int], int]] = {}
        extra_transparent = set()
        for pos, block in block_memory.data.items():
            section_key = tuple(int(value) // section_size for value in pos)
            sections.setdefault(section_key, {})[pos] = 0 if _is_transparent_block(str(block.get("name", "air")), extra_transparent) else 1
        self.sections = sections
        self.section_arrays: dict[tuple[int, int, int], tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        self.near_cache: dict[tuple[int, ...], tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}

    def update(self, block: dict) -> None:
        """1件のブロック更新を索引へ反映し、古くなった領域配列と近傍検索キャッシュを無効にする。"""
        sections, section_arrays, near_cache, section_size = self.sections, self.section_arrays, self.near_cache, self.SECTION_SIZE
        pos = tuple(int(value) for value in block["position"]["__Vec3__"])
        section_key = tuple(value // section_size for value in pos)
        sections.setdefault(section_key, {})[pos] = 0 if _is_transparent_block(str(block["name"]), set()) else 1
        section_arrays.pop(section_key, None)
        near_cache.clear()

    def near(self, position: Any, env_box: Any, max_dist: float = 20.0) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """人物の目の位置と視認距離に重なる領域からブロック配列を取り出す。

        同じ領域範囲の検索結果はキャッシュから再利用する。
        """
        sections, section_arrays, near_cache, section_size = self.sections, self.section_arrays, self.near_cache, self.SECTION_SIZE
        eye_pos = _as_v3(position) + np.array([0.0, PLAYER_EYE_HEIGHT, 0.0])
        origin, upper, _ = _clip_box(env_box, eye_pos, max_dist)
        lower_sections = tuple(int(value) // section_size for value in origin)
        upper_sections = tuple(int(value) // section_size for value in upper)
        section_range = (*lower_sections, *upper_sections)
        if section_range in near_cache:
            return near_cache[section_range]

        selected = []
        for sx in range(lower_sections[0], upper_sections[0] + 1):
            for sy in range(lower_sections[1], upper_sections[1] + 1):
                for sz in range(lower_sections[2], upper_sections[2] + 1):
                    key = (sx, sy, sz)
                    section = sections.get(key)
                    if not section:
                        continue
                    arrays = section_arrays.get(key)
                    if arrays is None:
                        positions = np.asarray(list(section), dtype=np.int32)
                        arrays = (positions[:, 0], positions[:, 1], positions[:, 2], np.fromiter(section.values(), dtype=np.uint8))
                        section_arrays[key] = arrays
                    selected.append(arrays)

        if selected:
            result = tuple(np.concatenate([arrays[i] for arrays in selected]) for i in range(4))
        else:
            empty = np.empty(0, dtype=np.int32)
            result = (empty, empty, empty, empty.astype(np.uint8))
        if len(near_cache) == 4:
            del near_cache[next(iter(near_cache))]
        near_cache[section_range] = result
        return result


def _clip_box(
    env_box,
    eye_pos: np.ndarray,
    max_dist: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """目の位置を中心とする視認距離の範囲をワールド範囲内に収め、整数の原点・最大座標・各軸の長さを返す。"""
    env_box0 = _as_v3(env_box[0])
    env_box1 = _as_v3(env_box[1])

    min_x = int(math.floor(min(max(eye_pos[0] - max_dist, env_box0[0]), env_box1[0])))
    max_x = int(math.floor(max(min(eye_pos[0] + max_dist, env_box1[0]), env_box0[0])))
    min_y = int(math.floor(min(max(eye_pos[1] - max_dist, env_box0[1]), env_box1[1])))
    max_y = int(math.floor(max(min(eye_pos[1] + max_dist, env_box1[1]), env_box0[1])))
    min_z = int(math.floor(min(max(eye_pos[2] - max_dist, env_box0[2]), env_box1[2])))
    max_z = int(math.floor(max(min(eye_pos[2] + max_dist, env_box1[2]), env_box0[2])))

    origin = np.array([min_x, min_y, min_z], dtype=np.int32)
    max_corner = np.array([max_x, max_y, max_z], dtype=np.int32)
    size = max_corner - origin + 1
    return origin, max_corner, size.astype(np.int32, copy=False)


@njit(
    _UINT8_VECTOR(
        _INT32_VECTOR,
        _INT32_VECTOR,
        _INT32_VECTOR,
        _UINT8_VECTOR,
        _INT32_VECTOR,
        _INT32_VECTOR,
    ),
    fastmath=True,
)
def _build_occ_grid_jit(
    xs: np.ndarray,
    ys: np.ndarray,
    zs: np.ndarray,
    opaque: np.ndarray,
    origin: np.ndarray,
    size: np.ndarray,
) -> np.ndarray:
    """指定範囲の遮蔽物を1次元の格子配列へ配置する。

    不透明ブロックのある位置を1にし、範囲外と透明ブロックは除く。
    """
    sx = int(size[0])
    sy = int(size[1])
    sz = int(size[2])
    sxy = sx * sy
    occ = np.zeros(sx * sy * sz, dtype=np.uint8)

    for i in range(xs.shape[0]):
        if opaque[i] == 0:
            continue

        ix = int(xs[i] - origin[0])
        iy = int(ys[i] - origin[1])
        iz = int(zs[i] - origin[2])
        if ix < 0 or ix >= sx or iy < 0 or iy >= sy or iz < 0 or iz >= sz:
            continue
        occ[ix + sx * iy + sxy * iz] = 1

    return occ


def build_visibility_data(env_box, position, block_arrays, max_dist: float = 20.0):
    """人物の目の位置から可視性計算の対象範囲を決め、その範囲の遮蔽物配列と座標情報をまとめて返す。"""
    eye_pos = _as_v3(position) + np.array([0.0, PLAYER_EYE_HEIGHT, 0.0])
    origin, max_corner, size = _clip_box(env_box, eye_pos, max_dist)
    occ = _build_occlusion_data(*block_arrays, origin, size)
    return origin, max_corner, size, occ


def _build_occlusion_data(
    xs: np.ndarray,
    ys: np.ndarray,
    zs: np.ndarray,
    opaque: np.ndarray,
    origin: np.ndarray,
    size: np.ndarray,
) -> np.ndarray:
    """座標と不透明判定の配列から、高速化された処理で遮蔽物の格子配列を作る。"""
    return _build_occ_grid_jit(xs, ys, zs, opaque, origin, size)


@njit(
    types.boolean(
        types.float64,
        types.float64,
        types.float64,
        types.float64,
        types.float64,
        types.float64,
        _INT32_VECTOR,
        _UINT8_VECTOR,
        _INT32_VECTOR,
    ),
    fastmath=True,
)
def _ray_visible_occ(
    ox: float,
    oy: float,
    oz: float,
    tx: float,
    ty: float,
    tz: float,
    origin: np.ndarray,
    occ: np.ndarray,
    size: np.ndarray,
) -> bool:
    """始点から対象点まで格子をたどり、途中の不透明ブロックに視線が遮られるかを判定する。

    対象点がある格子自体は遮蔽物として扱わない。
    """
    dx = tx - ox
    dy = ty - oy
    dz = tz - oz

    if dx == 0.0 and dy == 0.0 and dz == 0.0:
        return True

    ix = int(math.floor(ox) - origin[0])
    iy = int(math.floor(oy) - origin[1])
    iz = int(math.floor(oz) - origin[2])

    txi = int(math.floor(tx) - origin[0])
    tyi = int(math.floor(ty) - origin[1])
    tzi = int(math.floor(tz) - origin[2])

    if ix == txi and iy == tyi and iz == tzi:
        return True

    step_x = 1 if dx > 0.0 else -1 if dx < 0.0 else 0
    step_y = 1 if dy > 0.0 else -1 if dy < 0.0 else 0
    step_z = 1 if dz > 0.0 else -1 if dz < 0.0 else 0

    voxel_x = math.floor(ox)
    voxel_y = math.floor(oy)
    voxel_z = math.floor(oz)

    next_boundary_x = voxel_x + (1 if step_x > 0 else 0)
    next_boundary_y = voxel_y + (1 if step_y > 0 else 0)
    next_boundary_z = voxel_z + (1 if step_z > 0 else 0)

    inv_dx = 1.0 / dx if dx != 0.0 else math.inf
    inv_dy = 1.0 / dy if dy != 0.0 else math.inf
    inv_dz = 1.0 / dz if dz != 0.0 else math.inf

    t_max_x = (next_boundary_x - ox) * inv_dx if dx != 0.0 else math.inf
    t_max_y = (next_boundary_y - oy) * inv_dy if dy != 0.0 else math.inf
    t_max_z = (next_boundary_z - oz) * inv_dz if dz != 0.0 else math.inf

    t_delta_x = abs(inv_dx) if dx != 0.0 else math.inf
    t_delta_y = abs(inv_dy) if dy != 0.0 else math.inf
    t_delta_z = abs(inv_dz) if dz != 0.0 else math.inf

    sx = int(size[0])
    sy = int(size[1])
    sz = int(size[2])
    sxy = sx * sy

    while True:
        if t_max_x < t_max_y:
            if t_max_x < t_max_z:
                ix += step_x
                t_max_x += t_delta_x
            else:
                iz += step_z
                t_max_z += t_delta_z
        else:
            if t_max_y < t_max_z:
                iy += step_y
                t_max_y += t_delta_y
            else:
                iz += step_z
                t_max_z += t_delta_z

        if ix < 0 or ix >= sx or iy < 0 or iy >= sy or iz < 0 or iz >= sz:
            return True

        if ix == txi and iy == tyi and iz == tzi:
            return True

        if occ[ix + sx * iy + sxy * iz] != 0:
            return False


@njit(fastmath=True)
def _face_points_visible(
    ex: float,
    ey: float,
    ez: float,
    axis: int,
    face_coord: float,
    bx: float,
    by: float,
    bz: float,
    origin: np.ndarray,
    occ: np.ndarray,
    size: np.ndarray,
) -> bool:
    """指定されたブロック面上の判定点へ視線を伸ばし、1点でも遮られずに届くかを返す。"""
    if axis == 0:
        for i in range(_FACE_SAMPLE_U.shape[0]):
            if _ray_visible_occ(
                ex, ey, ez,
                face_coord,
                by + _FACE_SAMPLE_U[i],
                bz + _FACE_SAMPLE_V[i],
                origin, occ, size,
            ):
                return True
        return False

    if axis == 1:
        for i in range(_FACE_SAMPLE_U.shape[0]):
            if _ray_visible_occ(
                ex, ey, ez,
                bx + _FACE_SAMPLE_U[i],
                face_coord,
                bz + _FACE_SAMPLE_V[i],
                origin, occ, size,
            ):
                return True
        return False

    if axis == 2:
        for i in range(_FACE_SAMPLE_U.shape[0]):
            if _ray_visible_occ(
                ex, ey, ez,
                bx + _FACE_SAMPLE_U[i],
                by + _FACE_SAMPLE_V[i],
                face_coord,
                origin, occ, size,
            ):
                return True
        return False

    raise ValueError("axis must be 0, 1, or 2")


@njit(fastmath=True, inline="always")
def _block_face_points_visible(
    ex: float,
    ey: float,
    ez: float,
    x: int,
    y: int,
    z: int,
    origin: np.ndarray,
    occ: np.ndarray,
    size: np.ndarray,
) -> bool:
    """目の位置から見える側のブロック面を調べ、少なくとも1つの面の判定点が見えるかを返す。"""
    bx = float(x)
    by = float(y)
    bz = float(z)

    if ex < bx:
        if _face_points_visible(ex, ey, ez, 0, bx, bx, by, bz, origin, occ, size):
            return True
    elif ex > bx + 1.0:
        if _face_points_visible(ex, ey, ez, 0, bx + 1.0, bx, by, bz, origin, occ, size):
            return True

    if ey < by:
        if _face_points_visible(ex, ey, ez, 1, by, bx, by, bz, origin, occ, size):
            return True
    elif ey > by + 1.0:
        if _face_points_visible(ex, ey, ez, 1, by + 1.0, bx, by, bz, origin, occ, size):
            return True

    if ez < bz:
        if _face_points_visible(ex, ey, ez, 2, bz, bx, by, bz, origin, occ, size):
            return True
    elif ez > bz + 1.0:
        if _face_points_visible(ex, ey, ez, 2, bz + 1.0, bx, by, bz, origin, occ, size):
            return True

    return False


@njit(
    _UINT8_VECTOR(
        _FLOAT64_VECTOR,
        _INT32_VECTOR,
        _UINT8_VECTOR,
        _INT32_VECTOR,
        _INT32_VECTOR,
        _INT32_VECTOR,
        _INT32_VECTOR,
        _INT32_VECTOR,
        _INT32_VECTOR,
        types.float64,
    ),
    fastmath=True,
)
def _mark_visible_blocks_jit(
    eye_pos: np.ndarray,
    origin: np.ndarray,
    occ: np.ndarray,
    size: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    zs: np.ndarray,
    clip_min: np.ndarray,
    clip_max: np.ndarray,
    max_dist_squared: float,
) -> np.ndarray:
    """距離と計算範囲内の各ブロックについて、中心または手前側の面が見えるかを判定し、可視フラグの配列を返す。"""
    visible = np.zeros(xs.shape[0], dtype=np.uint8)
    ex = float(eye_pos[0])
    ey = float(eye_pos[1])
    ez = float(eye_pos[2])

    for i in range(xs.shape[0]):
        x = int(xs[i])
        y = int(ys[i])
        z = int(zs[i])

        if x < clip_min[0] or x > clip_max[0]:
            continue
        if y < clip_min[1] or y > clip_max[1]:
            continue
        if z < clip_min[2] or z > clip_max[2]:
            continue

        tx = x + 0.5
        ty = y + 0.5
        tz = z + 0.5

        dx = tx - ex
        dy = ty - ey
        dz = tz - ez
        if dx * dx + dy * dy + dz * dz > max_dist_squared:
            continue

        if _ray_visible_occ(ex, ey, ez, tx, ty, tz, origin, occ, size):
            visible[i] = 1
            continue

        if _block_face_points_visible(ex, ey, ez, x, y, z, origin, occ, size):
            visible[i] = 1

    return visible


@njit(
    types.int64[::1](
        _INT32_VECTOR,
        _INT32_VECTOR,
        _INT32_VECTOR,
        _UINT8_VECTOR,
        _INT32_VECTOR,
        _INT32_VECTOR,
    ),
    fastmath=True,
)
def _collect_visible_indices_jit(
    xs: np.ndarray,
    ys: np.ndarray,
    zs: np.ndarray,
    visible: np.ndarray,
    origin: np.ndarray,
    size: np.ndarray,
) -> np.ndarray:
    """可視フラグが立ったブロック座標を、可視範囲マップの1次元配列の添字へ変換して返す。"""
    count = 0
    for i in range(visible.shape[0]):
        if visible[i] != 0:
            count += 1

    result = np.empty(count, dtype=np.int64)
    sx = int(size[0])
    sy = int(size[1])
    sxy = sx * sy

    j = 0
    for i in range(visible.shape[0]):
        if visible[i] == 0:
            continue
        lx = int(xs[i] - origin[0])
        ly = int(ys[i] - origin[1])
        lz = int(zs[i] - origin[2])
        result[j] = lx + sx * ly + sxy * lz
        j += 1

    return result


def get_player_visibility_all(
    env_box,
    player_positions: dict[str, Any],
    block_memory,
    non_existent_agent_names=(),
    max_dist: float = 20.0,
    extra_transparent_blocks=(),
) -> dict[str, dict[str, bool]]:
    """すべての人物を観測者として、誰から誰が見えるかをまとめた辞書を作る。"""
    visibility: dict[str, dict[str, bool]] = {}
    for see_agent_name in player_positions.keys():
        visibility[see_agent_name] = get_player_visibility(
            env_box,
            player_positions,
            block_memory,
            see_agent_name,
            non_existent_agent_names=non_existent_agent_names,
            max_dist=max_dist,
            extra_transparent_blocks=extra_transparent_blocks,
        )

    return visibility


def get_player_visibility(
        env_box,
        player_positions: dict[str, Any],
        block_memory,
        see_agent_name: str,
        non_existent_agent_names=(),
        max_dist: float = 20.0,
        extra_transparent_blocks=(),
        block_arrays=None,
        visibility_data=None,
):
    """指定人物から各人物が見えるかを、目の位置・距離・遮蔽物から判定する。

    自身は見える扱いにし、不在者や位置不明の他者は見えない扱いにする。
    """
    max_dist_squared = float(max_dist) * float(max_dist)
    missing_names = {str(name) for name in non_existent_agent_names}
    xs, ys, zs, opaque = block_arrays if block_arrays is not None else build_block_arrays(block_memory, extra_transparent_blocks)

    eye_positions: dict[str, np.ndarray | None] = {}
    for name, position in player_positions.items():
        if position is None:
            eye_positions[name] = None
            continue
        eye_positions[name] = _as_v3(position) + np.array([0.0, PLAYER_EYE_HEIGHT, 0.0])

    see_pos = eye_positions[see_agent_name]
    vis = {}
    if see_pos is not None and see_agent_name not in missing_names:
        if visibility_data is None:
            origin, _, size = _clip_box(env_box, see_pos, max_dist)
            occ = _build_occlusion_data(xs, ys, zs, opaque, origin, size)
        else:
            origin, _, size, occ = visibility_data
    else:
        origin = np.array([0, 0, 0], dtype=np.int32)
        size = np.array([1, 1, 1], dtype=np.int32)
        occ = np.zeros(1, dtype=np.uint8)

    for saw_agent_name, saw_pos in eye_positions.items():
        if see_agent_name == saw_agent_name:
            vis[saw_agent_name] = True
            continue

        if see_agent_name in missing_names or saw_agent_name in missing_names:
            vis[saw_agent_name] = False
            continue

        if see_pos is None or saw_pos is None:
            vis[saw_agent_name] = False
            continue

        dx = float(saw_pos[0] - see_pos[0])
        dy = float(saw_pos[1] - see_pos[1])
        dz = float(saw_pos[2] - see_pos[2])
        if dx * dx + dy * dy + dz * dz > max_dist_squared:
            vis[saw_agent_name] = False
            continue

        vis[saw_agent_name] = bool(
            _ray_visible_occ(
                float(see_pos[0]),
                float(see_pos[1]),
                float(see_pos[2]),
                float(saw_pos[0]),
                float(saw_pos[1]),
                float(saw_pos[2]),
                origin,
                occ,
                size,
            )
        )

    return vis


def get_block_visibility_all(
    env_box,
    player_positions: dict[str, Any],
    block_memory,
    non_existent_agent_names=(),
    max_dist: float = 20.0,
    extra_transparent_blocks=(),
) -> dict[str, Vec3BoolMap]:

    """すべての人物について、その人物から見えるブロック座標のマップを作る。"""
    visible_blocks: dict[str, Vec3BoolMap] = {}
    for agent_name in player_positions.keys():
        visible_blocks[agent_name] = get_block_visibility(
            env_box,
            player_positions,
            block_memory,
            agent_name,
            non_existent_agent_names=non_existent_agent_names,
            max_dist = max_dist,
            extra_transparent_blocks=extra_transparent_blocks,
        )

    return visible_blocks


def get_block_visibility(
    env_box,
    player_positions,
    block_memory,
    agent_name,
    non_existent_agent_names=(),
    max_dist: float = 20.0,
    extra_transparent_blocks=(),
    block_arrays=None,
    visibility_data=None,
    target_positions=None,
) -> Vec3BoolMap:
    """指定人物から距離内に見えるブロックを真偽値マップで返す。

    対象座標が指定されても遮蔽物には全既知ブロックを使い、判定対象だけを絞る。
    """
    max_dist_squared = float(max_dist) * float(max_dist)
    missing_names = {str(name) for name in non_existent_agent_names}
    xs, ys, zs, opaque = block_arrays if block_arrays is not None else build_block_arrays(block_memory, extra_transparent_blocks)

    position = player_positions.get(agent_name)

    if position is None or agent_name in missing_names or xs.size == 0:
        return Vec3BoolMap(
            (
                np.array([0, 0, 0], dtype=np.int32),
                np.array([0, 0, 0], dtype=np.int32),
            )
        )

    eye_pos = _as_v3(position) + np.array([0.0, PLAYER_EYE_HEIGHT, 0.0])
    if visibility_data is None:
        origin, max_corner, size = _clip_box(env_box, eye_pos, max_dist)
        occ = _build_occlusion_data(xs, ys, zs, opaque, origin, size)
    else:
        origin, max_corner, size, occ = visibility_data

    vec3boolmap = Vec3BoolMap((origin, max_corner))

    # All known blocks define occlusion; only the requested positions need ray tests.
    if target_positions is not None:
        targets = np.asarray(target_positions, dtype=np.int32).reshape(-1, 3)
        xs, ys, zs = (np.ascontiguousarray(targets[:, axis]) for axis in range(3))

    visible_mask = _mark_visible_blocks_jit(
        eye_pos,
        origin,
        occ,
        size,
        xs,
        ys,
        zs,
        origin,
        max_corner,
        max_dist_squared,
    )
    visible_indices = _collect_visible_indices_jit(xs, ys, zs, visible_mask, origin, size)
    if visible_indices.size:
        vec3boolmap.add_indices(visible_indices)

    return vec3boolmap

