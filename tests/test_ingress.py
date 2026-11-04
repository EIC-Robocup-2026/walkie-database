"""The checks on an observation, and the slot that keeps the newest one."""

import array
import math

import numpy as np
import pytest
from memory_helpers import MAP, STREAM, block_of_points, cloud, cloud_fields, detection, header_at, jpeg_bytes, observation
from sensor_msgs.msg import CompressedImage, PointField

from walkie_database.ingress import (
    MAX_CLOUD_POINTS,
    MAX_CROP_BYTES,
    MAX_CROP_SIDE,
    MIN_CLOUD_POINTS,
    ObservationSlot,
    check_observation,
    jpeg_size,
)
from walkie_database.object_context import Rejected

NOW = 1000.0
MODELS = {"clip_vit_b16": 512}


def check(msg, now=NOW):
    check_observation(msg, models=MODELS, map_frame=MAP, now=now)


def refused(msg, now=NOW) -> Rejected:
    with pytest.raises(Rejected) as caught:
        check(msg, now)
    return caught.value


def invalid(msg, match: str) -> None:
    error = refused(msg)
    assert error.code == "INVALID"
    assert match in str(error)


def unit_vector(length: int = 512) -> list[float]:
    return [1.0] + [0.0] * (length - 1)


def one(points=None, **fields):
    msg = observation(NOW, [points])
    detection_msg = msg.detections[0]
    for name, value in fields.items():
        setattr(detection_msg, name, value)
    return msg


# What passes -----------------------------------------------------------------


def test_a_full_observation_passes():
    msg = observation(NOW)
    msg.detections = [
        detection(
            msg.header,
            points=block_of_points(),
            embedding_model="clip_vit_b16",
            embedding=unit_vector(),
            crop=CompressedImage(header=msg.header, format="jpeg", data=array.array("B", jpeg_bytes())),
            color_shares=[1.0] + [0.0] * 11,
        )
    ]
    check(msg)


def test_a_successful_empty_frame_passes():
    check(observation(NOW))


def test_a_detection_with_no_points_or_crop_or_vector_passes():
    check(observation(NOW, [None]))


@pytest.mark.parametrize("count", [MIN_CLOUD_POINTS, MAX_CLOUD_POINTS])
def test_the_point_limits_are_allowed(count):
    check(one(np.full((count, 3), 1.0)))


# The array -------------------------------------------------------------------


@pytest.mark.parametrize("stream", ["", "not-a-uuid", STREAM.upper(), STREAM.replace("-", "")])
def test_a_stream_id_must_be_a_canonical_uuid(stream):
    invalid(observation(NOW, stream=stream), "stream_id")


@pytest.mark.parametrize(
    "frame, match",
    [("", "frame_id"), ("/head_camera_optical", "frame_id"), (MAP, "optical frame")],
)
def test_an_array_must_name_an_optical_frame(frame, match):
    msg = observation(NOW)
    msg.header.frame_id = frame
    msg.camera_info.header.frame_id = frame
    invalid(msg, match)


def test_a_zero_stamp_is_refused_because_tf_reads_it_as_the_latest():
    msg = observation(NOW)
    msg.header.stamp.sec = 0
    msg.header.stamp.nanosec = 0
    msg.camera_info.header = msg.header
    invalid(msg, "header.stamp")


@pytest.mark.parametrize("sec, nanosec", [(-1, 0), (1000, 1_000_000_000)])
def test_an_impossible_stamp_is_refused(sec, nanosec):
    msg = observation(NOW)
    msg.header.stamp.sec = sec
    msg.header.stamp.nanosec = nanosec
    msg.camera_info.header = msg.header
    invalid(msg, "header.stamp")


def test_camera_info_must_share_the_header():
    msg = observation(NOW)
    msg.camera_info.header = header_at(NOW - 0.01)
    invalid(msg, "camera_info.header")


def test_camera_info_must_have_a_size_and_a_focal_length():
    msg = observation(NOW)
    msg.camera_info.width = 0
    invalid(msg, "image size")
    msg = observation(NOW)
    msg.camera_info.k[4] = 0.0
    invalid(msg, "focal length")
    msg = observation(NOW)
    msg.camera_info.k[0] = math.nan
    invalid(msg, "focal length")


@pytest.mark.parametrize("classes", [(), ("",), ("cup", "cup")])
def test_classes_must_be_unique_names(classes):
    invalid(observation(NOW, classes=classes), "classes")


# One detection ---------------------------------------------------------------


def test_a_label_must_be_in_classes():
    invalid(one(label="teapot"), "not in classes")


@pytest.mark.parametrize("field", ["score", "view_quality"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -0.1, 1.5])
def test_score_and_quality_must_be_in_the_unit_range(field, value):
    invalid(one(**{field: value}), field)


def test_a_detection_error_names_its_index():
    msg = observation(NOW, [None, None])
    msg.detections[1].label = "teapot"
    invalid(msg, "Detection 1:")


def test_a_box_must_be_finite_upright_and_not_negative():
    for change, match in (
        (lambda d: setattr(d.bbox, "size_x", math.nan), "bbox"),
        (lambda d: setattr(d.bbox, "size_y", -1.0), "bbox"),
        (lambda d: setattr(d.bbox.center, "theta", 0.1), "theta"),
    ):
        msg = observation(NOW, [None])
        change(msg.detections[0])
        invalid(msg, match)


# Vectors ---------------------------------------------------------------------


def test_a_fake_model_is_refused_because_the_store_has_no_table_for_it():
    invalid(one(embedding_model="fake_clip", embedding=unit_vector()), "no vector table")


def test_a_model_named_with_no_vector_and_the_reverse_are_refused():
    invalid(one(embedding_model="clip_vit_b16"), "both set or both empty")
    invalid(one(embedding=unit_vector()), "both set or both empty")


def test_a_vector_of_the_wrong_length_is_refused():
    invalid(one(embedding_model="clip_vit_b16", embedding=unit_vector(511)), "511")


def test_a_vector_with_nan_is_refused():
    vector = unit_vector()
    vector[3] = math.nan
    invalid(one(embedding_model="clip_vit_b16", embedding=vector), "not finite")


def test_a_vector_that_is_not_unit_length_is_refused():
    invalid(one(embedding_model="clip_vit_b16", embedding=[0.5] + [0.0] * 511), "unit length")


# Clouds ----------------------------------------------------------------------


def with_cloud(change) -> object:
    msg = observation(NOW, [block_of_points()])
    change(msg.detections[0].points)
    return msg


def test_a_cloud_from_another_time_or_frame_is_refused():
    invalid(with_cloud(lambda c: setattr(c, "header", header_at(NOW - 0.1))), "points.header")
    invalid(with_cloud(lambda c: setattr(c, "header", header_at(NOW, "other"))), "points.header")


def test_clouds_must_have_exactly_the_x_y_z_layout():
    def swapped(c):
        fields = cloud_fields()
        fields[0], fields[1] = fields[1], fields[0]
        c.fields = fields

    def as_doubles(c):
        c.fields = [
            PointField(name=f.name, offset=f.offset, datatype=PointField.FLOAT64, count=1)
            for f in cloud_fields()
        ]

    invalid(with_cloud(swapped), "fields")
    invalid(with_cloud(as_doubles), "fields")
    invalid(with_cloud(lambda c: setattr(c, "fields", cloud_fields()[:2])), "fields")


@pytest.mark.parametrize(
    "field, value, match",
    [
        ("is_bigendian", True, "little-endian"),
        ("is_dense", False, "dense"),
        ("height", 2, "height 1"),
        ("point_step", 16, "point_step"),
        ("row_step", 12, "row_step"),
    ],
)
def test_a_cloud_with_another_layout_is_refused(field, value, match):
    invalid(with_cloud(lambda c: setattr(c, field, value)), match)


def test_a_cloud_whose_data_does_not_fit_its_width_is_refused():
    invalid(with_cloud(lambda c: setattr(c, "data", array.array("B", bytes(36)))), "bytes for")


@pytest.mark.parametrize("count", [1, MIN_CLOUD_POINTS - 1, MAX_CLOUD_POINTS + 1])
def test_a_cloud_outside_the_point_limits_is_refused(count):
    invalid(one(np.full((count, 3), 1.0)), "points must be empty or hold")


@pytest.mark.parametrize(
    "bad", [(math.nan, 0.0, 1.0), (0.0, math.inf, 1.0), (0.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 0.0, math.nan)]
)
def test_a_point_that_is_not_finite_or_behind_the_camera_is_refused(bad):
    points = block_of_points()
    points[5] = bad
    invalid(one(points), "in front of the camera")


# Crops -----------------------------------------------------------------------


def crop_of(data: bytes, *, format: str = "jpeg", header=None) -> object:
    msg = observation(NOW, [None])
    msg.detections[0].crop = CompressedImage(
        header=msg.header if header is None else header, format=format, data=array.array("B", data)
    )
    return msg


def test_a_crop_must_share_the_header():
    invalid(crop_of(jpeg_bytes(), header=header_at(NOW - 0.1)), "crop.header")


def test_a_crop_must_be_a_jpeg():
    invalid(crop_of(jpeg_bytes(), format="png"), "JPEG")
    invalid(crop_of(b"\x89PNG\r\n\x1a\n"), "JPEG")


@pytest.mark.parametrize(
    "width, height", [(MAX_CROP_SIDE + 1, 10), (10, MAX_CROP_SIDE + 1), (0, 10), (10, 0)]
)
def test_a_crop_size_is_checked_from_its_header(width, height):
    invalid(crop_of(jpeg_bytes(width, height)), "pixels")


def test_the_longest_allowed_crop_side_passes():
    check(crop_of(jpeg_bytes(MAX_CROP_SIDE, MAX_CROP_SIDE)))


def test_a_crop_with_no_frame_header_is_refused():
    invalid(crop_of(b"\xff\xd8\xff\xd9\x00\x00"), "frame header")


def test_a_crop_over_the_byte_limit_is_refused():
    invalid(crop_of(jpeg_bytes() + bytes(MAX_CROP_BYTES)), "bytes")


def test_jpeg_size_skips_other_segments_and_fill_bytes():
    assert jpeg_size(jpeg_bytes(33, 44)) == (33, 44)
    assert jpeg_size(b"\xff\xd8\xff\xff\xe0\x00\x02" + jpeg_bytes(33, 44)[8:]) == (33, 44)
    assert jpeg_size(b"\xff\xd8\xff\xc4\x00\x02\xff\xc2\x00\x11\x08\x00\x02\x00\x03") == (3, 2)


@pytest.mark.parametrize("data", [b"", b"\xff\xd8", b"\xff\xd8\x00\x00\x00\x00", b"\xff\xd8\xff\xe0\x00\x01", b"\xff\xd8\xff\xc0\x00\x11\x08\x00"])
def test_jpeg_size_gives_none_for_a_broken_header(data):
    assert jpeg_size(data) is None


# Colour shares ---------------------------------------------------------------


@pytest.mark.parametrize(
    "shares, match",
    [
        ([0.5, 0.5], "12 values"),
        ([1.5, -0.5] + [0.0] * 10, "not negative"),
        ([math.nan] + [0.0] * 11, "not negative"),
        ([0.5] + [0.0] * 11, "sum to 1"),
    ],
)
def test_colour_shares_must_be_twelve_shares_of_one(shares, match):
    invalid(one(color_shares=shares), match)


# Time ------------------------------------------------------------------------


def test_an_array_at_the_age_limit_passes_and_past_it_is_stale():
    check(observation(NOW - 1.0))
    error = refused(observation(NOW - 1.001))
    assert error.code == "STALE"
    assert "old" in str(error)


def test_an_array_from_just_ahead_of_the_clock_passes_and_further_is_stale():
    check(observation(NOW + 0.004))
    error = refused(observation(NOW + 0.006))
    assert error.code == "STALE"
    assert "ahead" in str(error)


def test_a_malformed_array_is_invalid_even_when_it_is_also_stale():
    assert refused(observation(NOW - 100.0, classes=())).code == "INVALID"


# The slot --------------------------------------------------------------------


def slot() -> ObservationSlot:
    return ObservationSlot(MODELS, MAP)


def test_the_slot_keeps_only_the_newest_valid_array():
    kept = slot()
    first, second = observation(NOW), observation(NOW + 0.001)
    assert kept.receive(first, NOW) is None
    assert kept.latest is first
    assert kept.receive(second, NOW) is None
    assert kept.latest is second
    assert (kept.received, kept.rejected) == (2, 0)


def test_a_refused_array_is_counted_and_leaves_the_slot_alone():
    kept = slot()
    good = observation(NOW)
    kept.receive(good, NOW)
    assert "stream_id" in kept.receive(observation(NOW, stream=""), NOW)
    assert "old" in kept.receive(observation(NOW - 5.0), NOW)
    assert kept.latest is good
    assert (kept.received, kept.rejected) == (3, 2)


def test_an_array_before_the_first_clock_sample_is_refused():
    kept = slot()
    assert "clock" in kept.receive(observation(NOW), None)
    assert kept.latest is None
    assert (kept.received, kept.rejected) == (1, 1)
