"""Tests for the canonical map identity and its YAML wrapper.

tests/fixtures/map_id/golden.json was computed by a separate script that builds
each hash preimage by hand and does not import walkie_map_id. Its values are the
public contract of walkie-map-v1: a change to one needs a new algorithm version
and new map ids (ADR-0020).
"""

import ast
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import walkie_map_id
from walkie_map_id import ALGORITHM, decode_pgm, make_map_id, map_digest

from walkie_database.map_config import digest_map_files, parse_map_yaml

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "walkie_map_id.py"
GOLDEN = json.loads((ROOT / "tests" / "fixtures" / "map_id" / "golden.json").read_text())
VECTORS = {vector["name"]: vector for vector in GOLDEN["vectors"]}

BASE_DIGEST = VECTORS["p5_base"]["digest"]
METADATA = {
    "resolution": 0.05,
    "origin": [-4.698, -7.82, 0],
    "negate": 0,
    "occupied_thresh": 0.65,
    "free_thresh": 0.196,
    "mode": "trinary",
}
PGM = bytes.fromhex(VECTORS["p5_base"]["pgm_hex"])
GOOD_DIGEST = "a" * 64

# The same shape as the map YAMLs in walkie-ros: an integer in origin, and an
# integer negate.
MAP_YAML = """\
image: map.pgm
mode: trinary
resolution: 0.05
origin: [-4.698, -7.82, 0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
"""


def by_name(items):
    return pytest.mark.parametrize("item", items, ids=[item["name"] for item in items])


@by_name(GOLDEN["vectors"])
def test_decode_matches_the_golden_samples(item):
    assert decode_pgm(bytes.fromhex(item["pgm_hex"])) == (
        item["width"],
        item["height"],
        item["maxval"],
        tuple(item["samples"]),
    )


@by_name(GOLDEN["vectors"])
def test_digest_matches_the_golden_digest(item):
    assert map_digest(bytes.fromhex(item["pgm_hex"]), item["metadata"]) == item["digest"]


@by_name(GOLDEN["vectors"])
def test_map_id_matches_the_golden_id(item):
    map_id = make_map_id(GOLDEN["map_id_arena"], GOLDEN["map_id_date"], item["digest"])
    assert map_id == item["map_id"]


def test_the_golden_file_names_this_algorithm():
    assert GOLDEN["algorithm"] == ALGORITHM == "walkie-map-v1"


@pytest.mark.parametrize(
    "name",
    [
        "p2_same_pixels",
        "p2_other_whitespace",
        "p5_header_comments",
        "p5_comment_ends_at_cr",
        "p2_header_comment",
        "renamed_image",
        "no_image_key",
        "reversed_key_order",
        "unrelated_metadata",
        "missing_mode_is_trinary",
        "integer_and_float_origin",
        "negative_zero_origin",
    ],
)
def test_a_harmless_difference_keeps_the_digest(name):
    assert VECTORS[name]["digest"] == BASE_DIGEST


@pytest.mark.parametrize(
    "name",
    [
        "changed_pixel",
        "changed_origin",
        "changed_resolution",
        "changed_negate",
        "changed_occupied_thresh",
        "changed_free_thresh",
        "changed_mode",
        "changed_shape",
    ],
)
def test_a_real_difference_changes_the_digest(name):
    assert VECTORS[name]["digest"] != BASE_DIGEST


@pytest.mark.parametrize(
    "names",
    [
        ("free_thresh_zero", "free_thresh_negative_zero"),
        ("p5_whitespace_raster_bytes", "p2_whitespace_raster_bytes", "p5_header_ends_at_space"),
        ("p5_16_bit", "p2_16_bit"),
        (
            "black_white_maxval_1",
            "black_white_maxval_255",
            "black_white_maxval_65535",
            "black_white_p2_maxval_1",
        ),
    ],
    ids=["negative_zero", "whitespace_bytes", "16_bit", "black_white"],
)
def test_equal_maps_in_other_encodings_share_a_digest(names):
    assert len({VECTORS[name]["digest"] for name in names}) == 1


def test_the_reordered_vector_really_has_another_key_order():
    assert list(VECTORS["reversed_key_order"]["metadata"]) != list(VECTORS["p5_base"]["metadata"])


def test_digest_is_the_sha256_of_the_documented_preimage():
    pgm = b"P5\n2 1\n255\n\x00\xff"
    metadata = {
        "resolution": 0.05,
        "origin": [1.5, -2.0, 0.0],
        "negate": 1,
        "occupied_thresh": 0.75,
        "free_thresh": 0.25,
        "mode": "raw",
    }
    preimage = (
        b"walkie-map-v1\x00"
        b"\x00\x00\x00\x02\x00\x00\x00\x01"
        b"\x00\x00\xff\xff"
        b'{"free_thresh":"0x1.0000000000000p-2","mode":"raw","negate":1,'
        b'"occupied_thresh":"0x1.8000000000000p-1",'
        b'"origin":["0x1.8000000000000p+0","-0x1.0000000000000p+1","0x0.0p+0"],'
        b'"resolution":"0x1.999999999999ap-5"}'
    )
    assert map_digest(pgm, metadata) == hashlib.sha256(preimage).hexdigest()


def test_digest_covers_a_map_larger_than_one_update():
    width, height = 400, 300
    samples = [index % 256 for index in range(width * height)]
    p5 = b"P5\n400 300\n255\n" + bytes(samples)
    p2 = b"P2\n400 300\n255\n" + " ".join(map(str, samples)).encode()

    preimage = b"walkie-map-v1\x00" + struct.pack(">II", width, height)
    preimage += b"".join(struct.pack(">H", sample * 257) for sample in samples)
    preimage += json.dumps(
        {
            "free_thresh": (0.196).hex(),
            "mode": "trinary",
            "negate": 0,
            "occupied_thresh": (0.65).hex(),
            "origin": [(-4.698).hex(), (-7.82).hex(), (0.0).hex()],
            "resolution": (0.05).hex(),
        },
        separators=(",", ":"),
    ).encode()

    expected = hashlib.sha256(preimage).hexdigest()
    assert map_digest(p5, METADATA) == expected
    assert map_digest(p2, METADATA) == expected


def test_a_tuple_origin_equals_a_list_origin():
    assert map_digest(PGM, {**METADATA, "origin": (-4.698, -7.82, 0)}) == BASE_DIGEST


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 10**400])
@pytest.mark.parametrize(
    "key",
    ["resolution", "occupied_thresh", "free_thresh", "origin_x", "origin_y", "origin_yaw"],
)
def test_a_value_that_is_not_finite_is_rejected(key, value):
    metadata = dict(METADATA)
    if key.startswith("origin_"):
        origin = [0.0, 0.0, 0.0]
        origin[("origin_x", "origin_y", "origin_yaw").index(key)] = value
        metadata["origin"] = origin
    else:
        metadata[key] = value
    with pytest.raises(ValueError):
        map_digest(PGM, metadata)


@by_name(GOLDEN["rejected_metadata"])
def test_rejected_metadata(item):
    with pytest.raises(ValueError):
        map_digest(PGM, item["metadata"])


@by_name(GOLDEN["rejected_pgm"])
def test_rejected_pgm_fails_to_decode(item):
    with pytest.raises(ValueError):
        decode_pgm(bytes.fromhex(item["pgm_hex"]))


@by_name(GOLDEN["rejected_pgm"])
def test_rejected_pgm_gets_no_digest(item):
    with pytest.raises(ValueError):
        map_digest(bytes.fromhex(item["pgm_hex"]), METADATA)


@pytest.mark.parametrize(
    "data",
    ["P5\n1 1\n255\n\x00", bytearray(PGM), memoryview(PGM), None, 5, Path("map.pgm")],
    ids=["str", "bytearray", "memoryview", "none", "int", "path"],
)
def test_decode_accepts_only_bytes(data):
    with pytest.raises(ValueError):
        decode_pgm(data)
    with pytest.raises(ValueError):
        map_digest(data, METADATA)


@pytest.mark.parametrize("metadata", [None, [], "resolution: 0.05", 5], ids=repr)
def test_metadata_must_be_a_dict(metadata):
    with pytest.raises(ValueError):
        map_digest(PGM, metadata)


def test_digest_never_reads_a_path(tmp_path):
    image = tmp_path / "map.pgm"
    image.write_bytes(PGM)
    for path in (image, str(image), str(image).encode()):
        with pytest.raises(ValueError):
            map_digest(path, METADATA)


@pytest.mark.parametrize(
    ("arena", "date"),
    [
        ("arena", "2026-11-05"),
        ("a", "2026-11-05"),
        ("chula_pat14-6", "2028-02-29"),
        ("a0_-", "0001-01-01"),
    ],
)
def test_make_map_id_accepts_a_valid_name_and_date(arena, date):
    digest = "0123456789abcdef" * 4
    assert make_map_id(arena, date, digest) == f"{arena}-{date}-01234567"


@pytest.mark.parametrize(
    "arena",
    ["", "Arena", "1arena", "-arena", "_arena", "a b", "a/b", "arena\n", "aréna", None, 5, b"arena"],
    ids=repr,
)
def test_make_map_id_rejects_a_bad_arena(arena):
    with pytest.raises(ValueError):
        make_map_id(arena, "2026-11-05", GOOD_DIGEST)


@pytest.mark.parametrize(
    "date",
    [
        "",
        "2026-13-01",
        "2026-02-29",
        "2026-11-31",
        "0000-01-01",
        "2026-1-05",
        "20261105",
        "2026-W45-4",
        "2026-11-05T00:00:00",
        "2026-11-05\n",
        "٢٠٢٦-11-05",
        None,
        20261105,
    ],
    ids=repr,
)
def test_make_map_id_rejects_a_bad_date(date):
    with pytest.raises(ValueError):
        make_map_id("arena", date, GOOD_DIGEST)


@pytest.mark.parametrize(
    "digest",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n", None, 5, b"a" * 64],
    ids=repr,
)
def test_make_map_id_rejects_a_bad_digest(digest):
    with pytest.raises(ValueError):
        make_map_id("arena", "2026-11-05", digest)


def _imported_top_level_modules(tree):
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            modules.add(node.module.partition(".")[0])
    return modules


def test_the_module_imports_only_the_standard_library():
    modules = _imported_top_level_modules(ast.parse(MODULE_PATH.read_text()))
    assert modules
    assert modules <= set(sys.stdlib_module_names)


def test_the_module_opens_no_file():
    tree = ast.parse(MODULE_PATH.read_text())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "open" not in names
    assert not _imported_top_level_modules(tree) & {"io", "os", "pathlib", "yaml"}


def test_a_verbatim_copy_works_without_any_installed_package(tmp_path):
    """Load one copied file with no site-packages, as a consumer would."""
    copy = tmp_path / "vendored_map_id.py"
    shutil.copyfile(MODULE_PATH, copy)
    code = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('vendored_map_id', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "vector = json.loads(sys.argv[2])\n"
        "print(module.map_digest(bytes.fromhex(vector['pgm_hex']), vector['metadata']))\n"
        "print(any('site-packages' in path for path in sys.path))\n"
    )
    # -I ignores PYTHONPATH and the script's directory. -S skips site-packages.
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(copy), json.dumps(VECTORS["p5_base"])],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [BASE_DIGEST, "False"]


def test_the_module_is_imported_from_this_checkout():
    assert Path(walkie_map_id.__file__).resolve() == MODULE_PATH


def test_a_wheel_holds_the_module_beside_the_package(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(ROOT / "pyproject.toml", project)
    shutil.copytree(
        ROOT / "src",
        project / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
    )
    output = tmp_path / "wheel"
    output.mkdir()
    code = "import setuptools.build_meta as backend, sys; backend.build_wheel(sys.argv[1])"
    result = subprocess.run(
        [sys.executable, "-c", code, str(output)],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    with zipfile.ZipFile(next(output.glob("*.whl"))) as wheel:
        names = set(wheel.namelist())
    assert "walkie_map_id.py" in names
    assert "walkie_database/map_config.py" in names
    assert "walkie_database/node.py" in names
    assert any(name.startswith("walkie_database/migrations/") for name in names)


def _write_map(directory, text=MAP_YAML, image="map.pgm", pgm=PGM):
    (directory / image).parent.mkdir(parents=True, exist_ok=True)
    (directory / image).write_bytes(pgm)
    path = directory / "map.yaml"
    path.write_text(text)
    return path


def test_wrapper_reads_a_real_shaped_map(tmp_path):
    assert digest_map_files(_write_map(tmp_path)) == BASE_DIGEST
    assert digest_map_files(str(_write_map(tmp_path))) == BASE_DIGEST


def test_wrapper_ignores_comments_key_order_and_the_image_name(tmp_path):
    text = (
        "# saved by hand\n"
        "free_thresh:   0.196\n"
        "occupied_thresh: 0.65\n"
        "negate: 0\n"
        "origin:\n  - -4.698\n  - -7.82\n  - 0.0\n"
        "resolution: 0.05\n"
        "description: the lab\n"
        "image: images/renamed.pgm\n"
    )
    assert digest_map_files(_write_map(tmp_path, text, "images/renamed.pgm")) == BASE_DIGEST


def test_wrapper_follows_an_absolute_image_path(tmp_path):
    image = tmp_path / "elsewhere.pgm"
    image.write_bytes(PGM)
    directory = tmp_path / "maps"
    directory.mkdir()
    path = directory / "map.yaml"
    path.write_text(MAP_YAML.replace("image: map.pgm", f"image: {image}"))
    assert digest_map_files(path) == BASE_DIGEST


def test_wrapper_sees_a_changed_pixel(tmp_path):
    pgm = bytes.fromhex(VECTORS["changed_pixel"]["pgm_hex"])
    assert digest_map_files(_write_map(tmp_path, pgm=pgm)) == VECTORS["changed_pixel"]["digest"]


@pytest.mark.parametrize(
    "text",
    [
        MAP_YAML + "resolution: 0.1\n",
        MAP_YAML + "resolution: 0.05\n",
        MAP_YAML + "image: other.pgm\n",
        MAP_YAML + "description: a\ndescription: b\n",
        MAP_YAML + "extra:\n  key: 1\n  key: 2\n",
        "base: &base {resolution: 0.05}\n<<: *base\n" + MAP_YAML.replace("resolution: 0.05\n", ""),
    ],
    ids=["conflict", "same_value", "image", "unrelated", "nested", "merge"],
)
def test_wrapper_rejects_repeated_keys(tmp_path, text):
    with pytest.raises(ValueError):
        digest_map_files(_write_map(tmp_path, text))


@pytest.mark.parametrize(
    "text",
    [
        "resolution: [",
        "\t- bad",
        "a: b: c",
        "- a\n- b\n",
        "just text\n",
        "",
        "# only a comment\n",
        "5\n",
        "null\n",
        "!!python/object/apply:os.system [true]\n",
        "!!map [a, b]\n",
        "? [a, b]\n: c\n",
        b"\xff\xfe\x00bad",
    ],
    ids=repr,
)
def test_wrapper_rejects_malformed_yaml(text):
    with pytest.raises(ValueError):
        parse_map_yaml(text)


@pytest.mark.parametrize(
    "line",
    [
        "resolution: .nan",
        "resolution: .inf",
        "resolution: -.inf",
        "origin: [.nan, 0, 0]",
        "origin: [0, .inf, 0]",
        "occupied_thresh: .nan",
        "free_thresh: .nan",
        "resolution: true",
        "resolution: '0.05'",
        "resolution: 5e-2",
        "negate: false",
        "negate: 0.0",
        "mode: ~",
    ],
)
def test_wrapper_rejects_bad_values(tmp_path, line):
    key = line.partition(":")[0]
    kept = [item for item in MAP_YAML.splitlines() if not item.startswith(f"{key}:")]
    with pytest.raises(ValueError):
        digest_map_files(_write_map(tmp_path, "\n".join([*kept, line]) + "\n"))


@pytest.mark.parametrize("line", ["", "image: ''", "image: 5", "image: [a]", "image: ~"])
def test_wrapper_needs_the_name_of_an_image(tmp_path, line):
    kept = [item for item in MAP_YAML.splitlines() if not item.startswith("image:")]
    with pytest.raises(ValueError):
        digest_map_files(_write_map(tmp_path, "\n".join([*kept, line]) + "\n"))


def test_wrapper_reports_a_missing_file_as_oserror(tmp_path):
    with pytest.raises(OSError):
        digest_map_files(tmp_path / "absent.yaml")
    path = tmp_path / "map.yaml"
    path.write_text(MAP_YAML)
    with pytest.raises(OSError):
        digest_map_files(path)
