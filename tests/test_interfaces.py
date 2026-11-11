"""The frozen wire contract: fields, defaults, hashes, imports and build order.

A failure here means a type changed. A type change breaks every consumer built
from the old definition, so change these pins only with a deliberate contract
change (docs/setup/08-message-package.md).
"""

import array
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import walkie_database_msgs
import walkie_database_msgs.msg as dbmsg
import walkie_database_msgs.srv as dbsrv
import walkie_vision_msgs
import yaml
from ament_index_python.packages import get_package_share_directory
from rclpy.serialization import deserialize_message, serialize_message
from rosidl_parser import definition
from sensor_msgs.msg import CompressedImage, PointCloud2, PointField
from walkie_vision_msgs.msg import ObjectDetection, ObjectDetectionArray

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "ros2_ws" / "install"
VISION_CLONE = REPO / "ros2_ws" / "src" / "walkie-vision"

# The hashes at 64403e9, before the object interfaces were added. They must
# never change: walkie-agent-ros calls these types today.
LEGACY_HASHES = {
    "msg/Fact": "RIHS01_35927b177a3d418329c6970fa61d5a0fbb19cd116c334f07ce7035b09c8b7093",
    "msg/ModelInfo": "RIHS01_e4c418c7ab5eca1b81ca88d58548b18b0169c05080f2e3496ca47e3b122fba25",
    "msg/ModelVector": "RIHS01_19764498518d10b86792ea8e6d1701679092216998ab9b19e8a72a4ec060cda3",
    "msg/PersonMatch": "RIHS01_aea48b94e8c3ce0d6e4593928abad80be91cd8f56560d8f392f42a6851993d48",
    "msg/PersonMatches": "RIHS01_89c7ea12022094624f67b47f75481742c89afd4fd6286dadf5b8e93b09aff692",
    "msg/RememberedObject": "RIHS01_40376756b8d68df796166c286bc1dd3adf4fa6c3c237f67c69ee13ae492feb92",
    "srv/EndSession": "RIHS01_ac7bd5cb614bfc25a507b632f923a8166cda5515fe5c1f8d688bf5331a287712",
    "srv/FindObjects": "RIHS01_3b36625705647aad92c4e50a061d108bfed2b0a1b257c1a683089c12604179d9",
    "srv/MatchPeople": "RIHS01_5725bd0dc1e9c917e1a7510ba5ee9175105ea918f784e8f601b5ea2851477a65",
    "srv/RememberPerson": "RIHS01_fb4e4a28826bdb11db2bf0773abeca720b41688dbdfccf081f4acf56a9cadd9d",
    "srv/StartSession": "RIHS01_8e9cc30307daa540a1efdc7a497af45b3da0e7c131610719b60248144d4ce8c7",
}

# The object contract, ADR-0019. The services that nest ObjectDetectionArray
# also change when walkie-vision changes that type.
NEW_HASHES = {
    "msg/Event": "RIHS01_302db38e45529ebe44152b12affbb77c5e63c7a47c6e7d933d422adc5cdc2749",
    "msg/LocalizedDetection": "RIHS01_c8bc1db1fc6f8dafd0a4332850cd7d0fa0dc812b4db93417bf10ea15c16b209c",
    "msg/ObjectIdChange": "RIHS01_baabbca8a6a9853fabe544fc4ce56db9a372b1da9f6261abb5b57314264a0376",
    "msg/ObjectMatch": "RIHS01_6983bf151a64140a63d80eea6f7969a1a4af44b505970fc2dc4f36ce56c8968d",
    "msg/ObjectQuery": "RIHS01_623ea8198a36c6c53312f8da90316dd2498eb52cd6afde61b143a05a365695fb",
    "msg/ObjectSummary": "RIHS01_e40dc062105773393f45c7ce17b57e9419ed25401582ce6ca9c183edc15c8c4f",
    "msg/ObjectUpdates": "RIHS01_56fe4dbf592ea7c4f45d78a35e0fce9c6be0ebe9f0449bd69e6bb98b71611d96",
    "msg/OperationStatus": "RIHS01_255f5ff2e82284f826a1f38ae95b31492dc22c36d538bd0470ecb1cc40130a80",
    "msg/RoomSummary": "RIHS01_ce09427aa7083af4f848627349ef4b28e3717d732420707a52185966e62337b2",
    "msg/SearchPrior": "RIHS01_980aed97e08a34beb677b97823cec4e56a6c4e9c7ad49a83212319dd8ce777fd",
    "msg/Surface": "RIHS01_87dbee6356f38d190227e56fdcb3c4fc349d74e05f3fd70ee84a3cba53ab000d",
    "srv/ArenaReset": "RIHS01_8db7e1b95ac6e648c3a93675cc4c30a1fd666d7c53de56171508722cd6d25e38",
    "srv/DescribeObject": "RIHS01_a60fdbe1ee9c6dbf621ab83fb24762d9f5e7a8ea2ff3d2cb51899d2e374c2d07",
    "srv/FindEvents": "RIHS01_ff31d728c13c3b429c3e0cc1e8ca3c9fdb8b3a49f35334f1d7f6cc7a500891d5",
    "srv/FindNear": "RIHS01_5aaca8492fbef10de104e3c965506cedc1fbd15c0bc8fe2bc272b359e9a50ba0",
    "srv/GetObject": "RIHS01_075fd1459a1ce58accda1f1db7510070f4eab7149d317b32cb7ffbe4047cbcd7",
    "srv/GetRoomContents": "RIHS01_d82344c5f07921b8208e527dcfed403adf1318ac0c17e335f4e315207697a14d",
    "srv/GetStoreStatus": "RIHS01_668cf5150a7623ba4c6e34b706554301ef6de361c82dc9c488d821f67bbc3f25",
    "srv/ListRooms": "RIHS01_a80b5d9858dd7e2b1a5e3e35ba78c6b15aa12efa2ba5ef1dcc6659c8371fe594",
    "srv/LocalizeObjects": "RIHS01_e8676aa40115a6b5d14c66b5bb50340cfc8e8d3e8c79fa1a62c3200d1d22eb28",
    "srv/RememberEvent": "RIHS01_8f148ea74cae8424add35a064fd1cec0b311090e73691e1f3386faa2690c4fb5",
    "srv/ReportSighting": "RIHS01_412497ec8d8ed55be0909ce359749f876cbbd4b7dfdbdfd0486aabd2578b2684",
    "srv/SearchObjects": "RIHS01_fd1f5a5fa21123ab562637ea117069f29eefebea92ec550693df97910e8aaf8e",
    "srv/SetObjectState": "RIHS01_fb7330b9f4f87acfc383555de096983f7daab9cc2ef492e785223be51caaebda",
    "srv/SubmitDetections": "RIHS01_b517e9f16d523a696de0a6b259a2c807e7b09498e0e587019ad33ce80ab1cf86",
}

# The types of walkie_vision_msgs at the pin that this package's services nest.
VISION_HASHES = {
    "msg/ObjectDetection": "RIHS01_6c81c3c4cb5323cfb2fb8704979706965b54c32d63245a2f35c59eec7e7a4416",
    "msg/ObjectDetectionArray": "RIHS01_785d36c74d98e32def95c792b51b867bde9cc69b730dec3b8b00e4b5adb5f51b",
}

_STATUS = "walkie_database_msgs/OperationStatus"
_SUMMARY = "walkie_database_msgs/ObjectSummary"
_ARRAY = "walkie_vision_msgs/ObjectDetectionArray"
_MATCHES = "sequence<walkie_database_msgs/ObjectMatch>"
_WRITE = [
    ("request_id", "string"),
    ("task_id", "string"),
    ("cause", "string"),
    ("map_id", "string"),
]

# Field order is part of the wire format, so these are ordered lists.
NEW_FIELDS = {
    dbmsg.OperationStatus: [("code", "uint8"), ("message", "string")],
    dbmsg.ObjectSummary: [
        ("id", "int64"),
        ("map_id", "string"),
        ("revision", "uint64"),
        ("key", "string"),
        ("label", "string"),
        ("labels", "sequence<string>"),
        ("official_name", "string"),
        ("category", "string"),
        ("layer", "string"),
        ("state", "string"),
        ("source", "string"),
        ("room_id", "int64"),
        ("room", "string"),
        ("place", "string"),
        ("place_anchor_id", "int64"),
        ("attributes", "sequence<walkie_database_msgs/Fact>"),
        ("caption", "string"),
        ("printed_text", "string"),
        ("caption_model", "string"),
        ("prompt_version", "string"),
        ("p_exist", "float"),
        ("p_now", "float"),
        ("has_last_verified", "boolean"),
        ("last_verified", "builtin_interfaces/Time"),
        ("last_verified_age_s", "double"),
        ("first_seen", "builtin_interfaces/Time"),
        ("last_seen", "builtin_interfaces/Time"),
        ("n_detections", "uint32"),
        ("geometry_valid", "boolean"),
        ("pose", "geometry_msgs/PoseStamped"),
        ("size", "geometry_msgs/Vector3"),
        ("has_weight", "boolean"),
        ("weight_g", "float"),
        ("in_baseline", "boolean"),
        ("depth_seen", "boolean"),
        ("merged_into", "int64"),
        ("split_from", "int64"),
    ],
    dbmsg.ObjectQuery: [
        ("map_id", "string"),
        ("room", "string"),
        ("states", "sequence<string>"),
        ("layer", "string"),
        ("thing", "string"),
        ("category", "string"),
        ("attributes", "sequence<walkie_database_msgs/Fact>"),
        ("relation", "string"),
        ("reference_id", "int64"),
        ("reference_key", "string"),
        ("near_distance_m", "float"),
        ("superlative", "string"),
        ("reference_frame", "string"),
    ],
    dbmsg.ObjectMatch: [
        ("object", _SUMMARY),
        ("score", "float"),
        ("has_similarity", "boolean"),
        ("similarity", "float"),
        ("matched_attributes", "sequence<string>"),
        ("unknown_attributes", "sequence<string>"),
        ("mismatched_attributes", "sequence<string>"),
        ("measure_unknown", "boolean"),
    ],
    dbmsg.SearchPrior: [
        ("official_name", "string"),
        ("category", "string"),
        ("place", "string"),
        ("room", "string"),
    ],
    dbmsg.Surface: [
        ("key", "string"),
        ("anchor_id", "int64"),
        ("map_id", "string"),
        ("z", "double"),
        ("outline", "geometry_msgs/Polygon"),
        ("geometry_valid", "boolean"),
    ],
    dbmsg.RoomSummary: [
        ("id", "int64"),
        ("map_id", "string"),
        ("name", "string"),
        ("description", "string"),
        ("floor", "int32"),
        ("outline", "geometry_msgs/Polygon"),
        ("has_stand_pose", "boolean"),
        ("stand_pose", "geometry_msgs/PoseStamped"),
    ],
    dbmsg.ObjectIdChange: [("from_id", "int64"), ("to_id", "int64")],
    dbmsg.ObjectUpdates: [
        ("header", "std_msgs/Header"),
        ("map_id", "string"),
        ("store_id", "string"),
        ("sequence", "uint64"),
        ("added", "sequence<int64>"),
        ("updated", "sequence<int64>"),
        ("removed", "sequence<int64>"),
        ("merged", "sequence<walkie_database_msgs/ObjectIdChange>"),
        ("split", "sequence<walkie_database_msgs/ObjectIdChange>"),
    ],
    dbmsg.LocalizedDetection: [
        ("detection_index", "uint32"),
        ("status", _STATUS),
        ("centroid", "geometry_msgs/PointStamped"),
        ("points_used", "uint32"),
    ],
    dbmsg.Event: [
        ("id", "int64"),
        ("stamp", "builtin_interfaces/Time"),
        ("kind", "string"),
        ("text", "string"),
        ("map_id", "string"),
        ("has_robot_pose", "boolean"),
        ("robot_pose", "geometry_msgs/PoseStamped"),
        ("room_id", "int64"),
        ("object_id", "int64"),
        ("person_id", "int64"),
        ("session_id", "int64"),
        ("task_id", "string"),
        ("cause", "string"),
    ],
    dbsrv.LocalizeObjects.Request: [("map_id", "string"), ("objects", _ARRAY)],
    dbsrv.LocalizeObjects.Response: [
        ("status", _STATUS),
        ("results", "sequence<walkie_database_msgs/LocalizedDetection>"),
    ],
    dbsrv.SubmitDetections.Request: [*_WRITE, ("objects", _ARRAY)],
    dbsrv.SubmitDetections.Response: [("status", _STATUS), ("queued", "boolean")],
    dbsrv.SearchObjects.Request: [
        ("query", "walkie_database_msgs/ObjectQuery"),
        ("use_embedding", "boolean"),
        ("vector", "walkie_database_msgs/ModelVector"),
        ("text", "string"),
        ("max_results", "uint32"),
    ],
    dbsrv.SearchObjects.Response: [
        ("status", _STATUS),
        ("matches", _MATCHES),
        ("priors", "sequence<walkie_database_msgs/SearchPrior>"),
        ("lexical_only", "boolean"),
        ("truncated", "boolean"),
    ],
    dbsrv.GetObject.Request: [
        ("map_id", "string"),
        ("object_id", "int64"),
        ("include_cloud", "boolean"),
    ],
    dbsrv.GetObject.Response: [
        ("status", _STATUS),
        ("requested_id", "int64"),
        ("merged", "boolean"),
        ("object", _SUMMARY),
        ("surfaces", "sequence<walkie_database_msgs/Surface>"),
        ("cloud", "sensor_msgs/PointCloud2"),
    ],
    dbsrv.FindNear.Request: [
        ("query", "walkie_database_msgs/ObjectQuery"),
        ("max_results", "uint32"),
    ],
    dbsrv.FindNear.Response: [
        ("status", _STATUS),
        ("matches", _MATCHES),
        ("truncated", "boolean"),
    ],
    dbsrv.ListRooms.Request: [("map_id", "string")],
    dbsrv.ListRooms.Response: [
        ("status", _STATUS),
        ("rooms", "sequence<walkie_database_msgs/RoomSummary>"),
    ],
    dbsrv.GetRoomContents.Request: [
        ("query", "walkie_database_msgs/ObjectQuery"),
        ("max_results", "uint32"),
    ],
    dbsrv.GetRoomContents.Response: [
        ("status", _STATUS),
        ("room", "walkie_database_msgs/RoomSummary"),
        ("objects", _MATCHES),
        ("truncated", "boolean"),
    ],
    dbsrv.SetObjectState.Request: [
        *_WRITE,
        ("object_id", "int64"),
        ("expected_revision", "uint64"),
        ("operation", "string"),
        ("gripper_frame", "string"),
        ("has_pose", "boolean"),
        ("pose", "geometry_msgs/PoseStamped"),
        ("surface_key", "string"),
    ],
    dbsrv.SetObjectState.Response: [("status", _STATUS), ("object", _SUMMARY)],
    dbsrv.ReportSighting.Request: [
        *_WRITE,
        ("object_id", "int64"),
        ("seen", "boolean"),
        ("objects", _ARRAY),
        ("detection_index", "uint32"),
    ],
    dbsrv.ReportSighting.Response: [
        ("status", _STATUS),
        ("applied", "boolean"),
        ("object", _SUMMARY),
    ],
    dbsrv.ArenaReset.Request: _WRITE,
    dbsrv.ArenaReset.Response: [("status", _STATUS), ("objects_changed", "uint32")],
    dbsrv.RememberEvent.Request: [
        *_WRITE,
        ("kind", "string"),
        ("text", "string"),
        ("object_id", "int64"),
        ("person_id", "int64"),
        ("session_id", "int64"),
    ],
    dbsrv.RememberEvent.Response: [("status", _STATUS), ("event", "walkie_database_msgs/Event")],
    dbsrv.FindEvents.Request: [
        ("map_id", "string"),
        ("text", "string"),
        ("has_since", "boolean"),
        ("since", "builtin_interfaces/Time"),
        ("room", "string"),
        ("max_results", "uint32"),
    ],
    dbsrv.FindEvents.Response: [
        ("status", _STATUS),
        ("events", "sequence<walkie_database_msgs/Event>"),
        ("truncated", "boolean"),
    ],
    dbsrv.DescribeObject.Request: [*_WRITE, ("object_id", "int64")],
    dbsrv.DescribeObject.Response: [
        ("status", _STATUS),
        ("resolved_id", "int64"),
        ("queued", "boolean"),
    ],
    dbsrv.GetStoreStatus.Response: [
        ("status", _STATUS),
        ("map_id", "string"),
        ("map_hash", "string"),
        ("baseline_rev", "string"),
        ("store_id", "string"),
        ("clock_kind", "string"),
        ("schema_version", "uint32"),
        ("map_valid", "boolean"),
        ("ingest_enabled", "boolean"),
        ("captions_enabled", "boolean"),
        ("object_embedding_models", "sequence<string>"),
        ("arrays_received", "uint32"),
        ("arrays_rejected", "uint32"),
    ],
}


def fields_of(cls) -> list[tuple[str, str]]:
    return list(cls.get_fields_and_field_types().items())


def described_hashes(package: str, path: str) -> dict[str, str]:
    described = Path(get_package_share_directory(package)) / f"{path}.json"
    hashes = json.loads(described.read_text())["type_hashes"]
    return {h["type_name"]: h["hash_string"] for h in hashes}


def filled_value(slot_type, seed: int):
    """A value of this field's type that differs from its default."""
    if isinstance(slot_type, definition.UnboundedSequence):
        return [filled_value(slot_type.value_type, seed + i) for i in range(2)]
    if isinstance(slot_type, definition.Array):
        return [filled_value(slot_type.value_type, seed + i) for i in range(slot_type.size)]
    if isinstance(slot_type, definition.UnboundedString):
        return f"value-{seed}"
    if isinstance(slot_type, definition.NamespacedType):
        return filled(_message_class(slot_type)(), seed + 1)
    if isinstance(slot_type, definition.BasicType):
        if slot_type.typename == "boolean":
            return True
        if slot_type.typename in ("float", "double"):
            return seed + 0.5
        return seed % 100 + 1
    raise AssertionError(f"This test does not know how to fill {slot_type}")


def _message_class(slot_type: definition.NamespacedType):
    package, kind = slot_type.namespaces
    module = __import__(f"{package}.{kind}", fromlist=[slot_type.name])
    return getattr(module, slot_type.name)


def filled(message, seed: int = 1):
    """Set every field, so that a field the wire loses shows up as a difference."""
    names = message.get_fields_and_field_types()
    for i, (name, slot_type) in enumerate(zip(names, message.SLOT_TYPES, strict=True)):
        setattr(message, name, filled_value(slot_type, seed + i))
    return message


def test_every_new_interface_file_has_pins():
    package = REPO / "ros2_ws" / "src" / "walkie_database_msgs"
    files = {
        f"{kind}/{path.stem}"
        for kind in ("msg", "srv")
        for path in (package / kind).glob(f"*.{kind}")
    }

    assert files == set(LEGACY_HASHES) | set(NEW_HASHES)
    # Each .msg and .srv is built: a file CMake does not list has no hash.
    cmake = (package / "CMakeLists.txt").read_text()
    for name in files:
        assert f'"{name}.{name.split("/")[0]}"' in cmake


@pytest.mark.parametrize("path, rihs", LEGACY_HASHES.items())
def test_legacy_type_hashes_are_unchanged(path, rihs):
    assert described_hashes("walkie_database_msgs", path)[f"walkie_database_msgs/{path}"] == rihs


@pytest.mark.parametrize("path, rihs", NEW_HASHES.items())
def test_new_type_hashes_are_frozen(path, rihs):
    assert described_hashes("walkie_database_msgs", path)[f"walkie_database_msgs/{path}"] == rihs


@pytest.mark.parametrize("path, rihs", VISION_HASHES.items())
def test_pinned_vision_type_hashes(path, rihs):
    assert described_hashes("walkie_vision_msgs", path)[f"walkie_vision_msgs/{path}"] == rihs


def test_remembered_object_fields_are_unchanged():
    assert fields_of(dbmsg.RememberedObject) == [
        ("id", "int64"),
        ("label", "string"),
        ("caption", "string"),
        ("map_id", "string"),
        ("frame_id", "string"),
        ("position", "geometry_msgs/Point"),
        ("orientation", "geometry_msgs/Quaternion"),
        ("size", "geometry_msgs/Vector3"),
        ("room", "string"),
        ("n_detections", "uint32"),
        ("last_seen", "builtin_interfaces/Time"),
    ]


def test_model_vector_fields_are_unchanged():
    assert fields_of(dbmsg.ModelVector) == [("model", "string"), ("embedding", "sequence<float>")]


def test_find_objects_fields_are_unchanged():
    assert fields_of(dbsrv.FindObjects.Request) == [
        ("map_id", "string"),
        ("label", "string"),
        ("max_results", "uint32"),
    ]
    assert fields_of(dbsrv.FindObjects.Response) == [
        ("success", "boolean"),
        ("message", "string"),
        ("objects", "sequence<walkie_database_msgs/RememberedObject>"),
    ]


@pytest.mark.parametrize("cls", NEW_FIELDS, ids=lambda cls: cls.__name__)
def test_new_fields_are_frozen(cls):
    assert fields_of(cls) == NEW_FIELDS[cls]


def test_get_store_status_request_is_empty():
    assert dbsrv.GetStoreStatus.Request.get_fields_and_field_types() == {}


def test_operation_status_codes():
    codes = {
        name: getattr(dbmsg.OperationStatus, name)
        for name in (
            "OK",
            "INVALID",
            "NOT_FOUND",
            "STALE",
            "TF_UNAVAILABLE",
            "WRONG_MAP",
            "UNSUPPORTED",
            "BUSY",
            "NOT_READY",
            "INTERNAL",
            "CONFLICT",
        )
    }

    assert list(codes.values()) == list(range(11))


def test_defaults():
    query = dbmsg.ObjectQuery()
    assert query.near_distance_m == pytest.approx(0.6)
    assert query.reference_frame == "base_footprint"
    assert query.states == []
    assert dbsrv.SearchObjects.Request().max_results == 20
    assert dbsrv.FindNear.Request().max_results == 20
    assert dbsrv.GetRoomContents.Request().max_results == 100
    assert dbsrv.FindEvents.Request().max_results == 20
    assert dbsrv.GetObject.Request().include_cloud is False
    # A status nobody assigned must not read as a success by accident, so
    # servers always assign it. Its default is still the zero code.
    assert dbmsg.OperationStatus().code == dbmsg.OperationStatus.OK


@pytest.mark.parametrize("cls", NEW_FIELDS, ids=lambda cls: cls.__name__)
def test_new_types_survive_serialization(cls):
    message = filled(cls())
    assert message != cls()

    assert deserialize_message(serialize_message(message), cls) == message


def test_a_nested_observation_survives_serialization():
    # The vision types are the part another repository builds, so this checks
    # real values through the nested type instead of only the filler above.
    objects = ObjectDetectionArray(
        classes=["cup"],
        stream_id="3d3c1f4e-7a55-4b1e-8d11-2b8f1a0a9c10",
        detections=[
            ObjectDetection(
                label="cup",
                score=0.75,
                track_id=7,
                embedding_model="clip_vit_b16",
                embedding=array.array("f", [1.0] + [0.0] * 511),
                points=PointCloud2(
                    width=1,
                    height=1,
                    fields=[PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1)],
                    point_step=12,
                    row_step=12,
                    data=bytes(range(12)),
                    is_dense=True,
                ),
                crop=CompressedImage(format="jpeg", data=b"\xff\xd8\xff\xd9"),
                view_quality=0.5,
                color_shares=array.array("f", [1.0] + [0.0] * 11),
            )
        ],
    )
    request = dbsrv.LocalizeObjects.Request(map_id="arena", objects=objects)

    decoded = deserialize_message(serialize_message(request), dbsrv.LocalizeObjects.Request)

    assert decoded == request
    detection = decoded.objects.detections[0]
    assert decoded.objects.stream_id == "3d3c1f4e-7a55-4b1e-8d11-2b8f1a0a9c10"
    assert detection.track_id == 7
    assert list(detection.embedding)[:2] == [1.0, 0.0]
    assert len(detection.embedding) == 512
    assert bytes(detection.points.data) == bytes(range(12))
    assert bytes(detection.crop.data) == b"\xff\xd8\xff\xd9"
    assert len(detection.color_shares) == 12


def test_both_message_packages_import_from_this_worktrees_build():
    # A main checkout's overlay on the path would pass every other test.
    assert Path(walkie_database_msgs.__file__).is_relative_to(INSTALL / "walkie_database_msgs")
    assert Path(walkie_vision_msgs.__file__).is_relative_to(INSTALL / "walkie_vision_msgs")


def test_vision_messages_are_built_from_the_imported_clone():
    share = Path(get_package_share_directory("walkie_vision_msgs"))

    assert share.is_relative_to(INSTALL)
    # --symlink-install links the installed file to the source colcon built.
    assert (share / "package.xml").resolve().is_relative_to(VISION_CLONE)


def test_the_imported_clone_is_at_the_pin():
    pin = yaml.safe_load((REPO / "ros2_ws.repos").read_text())["repositories"][
        "ros2_ws/src/walkie-vision"
    ]["version"]
    git = shutil.which("git") or shutil.which("git", path="/usr/bin:/bin")

    head = subprocess.run(
        [git, "rev-parse", "HEAD"], cwd=VISION_CLONE, text=True, capture_output=True, check=True
    )

    assert head.stdout.strip() == pin


def test_colcon_builds_vision_before_the_database(tmp_path):
    defaults = tmp_path / "defaults.yaml"
    defaults.write_text("{}\n")

    listed = subprocess.run(
        [
            "colcon",
            "list",
            "--names-only",
            "--topological-order",
            "--packages-select",
            "walkie_database_msgs",
            "walkie_vision_msgs",
        ],
        cwd=REPO / "ros2_ws",
        env={**os.environ, "COLCON_DEFAULTS_FILE": str(defaults)},
        text=True,
        capture_output=True,
        check=True,
    )

    assert listed.stdout.split() == ["walkie_vision_msgs", "walkie_database_msgs"]


def test_build_task_selects_exactly_the_two_message_packages():
    tasks = tomllib.loads((REPO / "pixi.toml").read_text())["tasks"]

    assert "--packages-select walkie_vision_msgs walkie_database_msgs " in tasks["build-msgs"]["cmd"]


def test_the_message_package_declares_every_package_it_nests():
    package = REPO / "ros2_ws" / "src" / "walkie_database_msgs"
    manifest = (package / "package.xml").read_text()
    cmake = (package / "CMakeLists.txt").read_text()
    needed = {"builtin_interfaces", "geometry_msgs", "sensor_msgs", "std_msgs", "walkie_vision_msgs"}

    for name in needed:
        assert f"<depend>{name}</depend>" in manifest
        assert f"find_package({name} REQUIRED)" in cmake
    assert f"DEPENDENCIES {' '.join(sorted(needed))}" in cmake
