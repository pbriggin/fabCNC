"""Helpers for keeping canvas geometry and segment metadata in sync."""


def store_shape_snapshot(shape_name, new_points, payload, shapes, breaks, types,
                         preserve_missing_metadata=False):
    """Store a shape event, preserving existing segmentation on legacy moves.

    Older canvas clients may send geometry without ``segmentBreaks`` or
    ``segmentTypes``. For move/update events, those missing fields must not
    replace the segmentation already associated with the shape.
    """
    shapes[shape_name] = [tuple(point) for point in new_points]

    if 'segmentBreaks' in payload:
        breaks[shape_name] = list(payload.get('segmentBreaks') or [0])
    elif not preserve_missing_metadata or shape_name not in breaks:
        breaks[shape_name] = [0]

    if 'segmentTypes' in payload:
        types[shape_name] = list(payload.get('segmentTypes') or [])
    elif not preserve_missing_metadata or shape_name not in types:
        types[shape_name] = []
