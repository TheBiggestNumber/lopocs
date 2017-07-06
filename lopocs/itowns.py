# -*- coding: utf-8 -*-
import traceback
from struct import Struct
from collections import namedtuple
from math import pow, log

import numpy as np

from flask import make_response, abort

from .utils import read_uncompressed_patch, boundingbox_to_diagonal
from .database import Session


the_bbox = namedtuple('bbox', ['xmin', 'ymin', 'zmin', 'xmax', 'ymax', 'zmax'])
binfloat = Struct('I')
binchar = Struct('B')

# structured array type for colors
cdt = np.dtype([('Red', np.uint8), ('Green', np.uint8), ('Blue', np.uint8), ('Alpha', np.uint8)])
# structured array type for positions
pdt = np.dtype([('X', np.float32), ('Y', np.float32), ('Z', np.float32)])


SKIP_LOD_POINT_LIMIT = 60000
NODE_POINT_LIMIT = 20000

POINT_QUERY = """
select pc_union(points) from (
    select
        pc_range({session.column}, {start}, {count}) as points
    from (
        select points
        from {session.table}
        where pc_boundingdiagonalgeometry({session.column}) &&&
            st_geomfromtext('linestringz ({diag})', {session.srsid})
    ) _
) _
"""

HIERARCHY_QUERY = """
select sum(pc_numpoints(points))
from (
    select
        pc_filterbetween(
                pc_filterbetween(
                    pc_filterbetween(
                        pc_range({session.column}, {start}, {count})
                        , 'z', {z1}, {z2}
                    ), 'y', {y1}, {y2}
                ), 'x', {x1}, {x2}
        ) as points
    from (
        select points
        from {session.table}
        where pc_boundingdiagonalgeometry({session.column}) &&&
            st_geomfromtext('linestringz ({diag})', {session.srsid})
    ) _
) _
"""


def prepare_session_lod_threshold(session):
    # for low lod patch_count = pointcount since we select 1 point per patch
    patch_count = session.lopocstable.nbpoints / session.patch_size
    if patch_count < SKIP_LOD_POINT_LIMIT:
        session.psize_threshold1 = -1
    else:
        session.psize_threshold1 = int(round(log(patch_count / SKIP_LOD_POINT_LIMIT, 8)))

    if patch_count < NODE_POINT_LIMIT:
        session.psize_threshold2 = -1
    else:
        session.psize_threshold2 = int(round(log(patch_count / NODE_POINT_LIMIT, 8)))


def ItownsRead(table, column, bbox_encoded, isleaf, last_modified):

    session = Session(table, column)

    bbox_encoded = bbox_encoded.strip('r.')
    lod = len(bbox_encoded)
    box = decode_bbox(session, bbox_encoded)
    stored_patches = session.lopocstable.filter_stored_output()
    schema = stored_patches['point_schema']
    pcid = stored_patches['pcid']
    scales = stored_patches['scales']
    offsets = stored_patches['offsets']

    prepare_session_lod_threshold(session)

    try:
        tile = get_points(
            session,
            box,
            lod,
            offsets,
            pcid,
            scales,
            schema,
            isleaf
        )
    except TypeError:
        print(traceback.print_exc())
        return abort(404)

    # build flask response
    response = make_response(tile)
    response.headers['content-type'] = 'application/octet-stream'
    response.headers['Last-Modified'] = last_modified
    return response


def ItownsHrc(table, column, bbox_encoded, last_modified):
    """
    Request for hierarchy
    [x1][n1][x11][n11][x12][n12]...[x111][n111]

    """
    session = Session(table, column)
    prepare_session_lod_threshold(session)

    bbox_encoded = bbox_encoded.strip('r.')
    lod = len(bbox_encoded)
    box = decode_bbox(session, bbox_encoded)

    patch_size = session.patch_size

    npoints = get_numpoints(session, box, lod, patch_size)
    output = octree(session, 0, 2, box, npoints, lod, patch_size)

    response = make_response(output)
    response.headers['content-type'] = 'application/octet-stream'
    response.headers['Last-Modified'] = last_modified
    return response


def compute_psize(session, lod):
    # If there's too many patch that would be selected: skip this level
    if lod <= session.psize_threshold1:
        return 0

    # If we expect to receive a lot of patches, we only return 1 point per patch
    if lod <= session.psize_threshold2:
        return 1

    # As soon as the patch count lowers, we want to return more and more points
    # because a constant point cound per patch would lead to "shallow" nodes,
    # with very few points.
    power = lod - ((session.psize_threshold2 + 1) if session.psize_threshold2 >= 0 else 0)
    return int(pow(8, power))


def get_numpoints(session, box, lod, patch_size):
    diag = boundingbox_to_diagonal([
        box.xmin, box.ymin, box.zmin, box.xmax, box.ymax, box.zmax
    ])

    psize = compute_psize(session, lod)

    if psize is 0:
        # special value...
        return -1

    start = 1 + sum([compute_psize(session, l) for l in range(lod)])
    count = min(psize, patch_size - start)

    sql = HIERARCHY_QUERY.format(
        x1=box.xmin, x2=box.xmax,
        y1=box.ymin, y2=box.ymax,
        z1=box.zmin, z2=box.zmax,
        **locals())

    return session.query(sql)[0][0] or 0


def octree(session, depth, depth_max, box, npoints, lod, patch_size, name='', buffer=None):
    psize = compute_psize(session, lod)

    if buffer is None:
        buffer = {}

    # root
    bitarray = ['0'] * 8

    estimate_total = NODE_POINT_LIMIT

    npoints = 0 if npoints < 0 else npoints

    if psize:
        # estimate the number of patches
        npatches = npoints / psize

        # how many points would a isleaf=1 request return for this bbox
        points_used_in_previous_lod = npatches * sum(compute_psize(session, l) for l in range(lod))

        estimate_total = npatches * patch_size - points_used_in_previous_lod

    # can we stop subviding?
    if estimate_total < NODE_POINT_LIMIT:
        buffer[name] = [bitarray, int(estimate_total)]
    else:
        for child in range(8):
            cbox = get_child(box, child)
            cnpoints = get_numpoints(session, cbox, lod + 1, patch_size)

            if cnpoints != 0:
                bitarray[child] = '1'
                if depth < depth_max:
                    octree(session, depth + 1, depth_max, cbox, cnpoints, lod + 1, patch_size,
                           name + str(child), buffer)

        buffer[name] = [bitarray, npoints]

    if not depth:
        sorted_keys = sorted(buffer.keys(), key=lambda x: (len(x), x))
        # print .hrc result
        for k in sorted(buffer.keys()):
            if len(k) > 0:
                children_count = sum(map(lambda x: int(x), buffer[k][0]))
                if children_count:
                    print('{}├── \033[30;34m{}\033[0m {} psize={} -> {}'.format(
                        '│   ' * (len(k) - 1), k, buffer[k][1],
                        compute_psize(session, len(k)), ''.join(buffer[k][0])[::-1]))
                else:
                    print('{}├── \033[30;34m{}\033[0m {} psize={}'.format(
                        '│   ' * (len(k) - 1), k, buffer[k][1],
                        compute_psize(session, len(k))))
            else:
                print('\033[30;34m{}\033[0m {}'.format('X', buffer[k][1]))

        output = b''.join([
            binchar.pack(int(''.join(buffer[key][0])[::-1], 2)) + binfloat.pack(buffer[key][1])
            for key in sorted_keys
        ])
        return output


def get_child(parent, numchild):
    half_size = (
        (parent.xmax - parent.xmin) / 2,
        (parent.ymax - parent.ymin) / 2,
        (parent.zmax - parent.zmin) / 2
    )
    if numchild == 0:
        child = the_bbox(
            parent.xmin,
            parent.ymin,
            parent.zmin,
            parent.xmin + half_size[0],
            parent.ymin + half_size[1],
            parent.zmin + half_size[2]
        )
    elif numchild == 1:
        child = the_bbox(
            parent.xmin,
            parent.ymin,
            parent.zmin + half_size[2],
            parent.xmin + half_size[0],
            parent.ymin + half_size[1],
            parent.zmax
        )
    elif numchild == 2:
        child = the_bbox(
            parent.xmin,
            parent.ymin + half_size[1],
            parent.zmin,
            parent.xmin + half_size[0],
            parent.ymax,
            parent.zmin + half_size[2]
        )
    elif numchild == 3:
        child = the_bbox(
            parent.xmin,
            parent.ymin + half_size[1],
            parent.zmin + half_size[2],
            parent.xmin + half_size[0],
            parent.ymax,
            parent.zmax
        )
    elif numchild == 4:
        child = the_bbox(
            parent.xmin + half_size[0],
            parent.ymin,
            parent.zmin,
            parent.xmax,
            parent.ymin + half_size[1],
            parent.zmin + half_size[2]
        )
    elif numchild == 5:
        child = the_bbox(
            parent.xmin + half_size[0],
            parent.ymin,
            parent.zmin + half_size[2],
            parent.xmax,
            parent.ymin + half_size[1],
            parent.zmax
        )
    elif numchild == 6:
        child = the_bbox(
            parent.xmin + half_size[0],
            parent.ymin + half_size[1],
            parent.zmin,
            parent.xmax,
            parent.ymax,
            parent.zmin + half_size[2]
        )
    elif numchild == 7:
        child = the_bbox(
            parent.xmin + half_size[0],
            parent.ymin + half_size[1],
            parent.zmin + half_size[2],
            parent.xmax,
            parent.ymax,
            parent.zmax
        )
    return child


def decode_bbox(session, bbox):
    """
    returns a r0000.bin
    """
    root = the_bbox(
        session.boundingbox['xmin'],
        session.boundingbox['ymin'],
        session.boundingbox['zmin'],
        session.boundingbox['xmax'],
        session.boundingbox['ymax'],
        session.boundingbox['zmax']
    )
    if not bbox:
        return root

    for numchild in [int(l) for l in bbox]:
        root = get_child(root, numchild)

    return root


def classification_to_rgb(points):
    """
    map LAS Classification to RGB colors.
    See LAS spec for codes :
    http://www.asprs.org/wp-content/uploads/2010/12/asprs_las_format_v11.pdf

    :param points: points as a structured numpy array
    :returns: numpy.record with dtype [('Red', 'u1'), ('Green', 'u1'), ('Blue', 'u1')])
    """
    # building (brown)
    building_mask = (points['Classification'] == 6).astype(np.int)
    red = building_mask * 186
    green = building_mask * 79
    blue = building_mask * 63
    # high vegetation (green)
    veget_mask = (points['Classification'] == 5).astype(np.int)
    red += veget_mask * 140
    green += veget_mask * 156
    blue += veget_mask * 8
    # medium vegetation
    veget_mask = (points['Classification'] == 4).astype(np.int)
    red += veget_mask * 171
    green += veget_mask * 200
    blue += veget_mask * 116
    # low vegetation
    veget_mask = (points['Classification'] == 3).astype(np.int)
    red += veget_mask * 192
    green += veget_mask * 213
    blue += veget_mask * 160
    # water (blue)
    water_mask = (points['Classification'] == 9).astype(np.int)
    red += water_mask * 141
    green += water_mask * 179
    blue += water_mask * 198
    # ground (light brown)
    grd_mask = (points['Classification'] == 2).astype(np.int)
    red += grd_mask * 226
    green += grd_mask * 230
    blue += grd_mask * 229
    # Unclassified (grey)
    grd_mask = (points['Classification'] == 1).astype(np.int)
    red += grd_mask * 176
    green += grd_mask * 185
    blue += grd_mask * 182

    alpha = np.ones(points.shape)

    rgb_reduced = np.c_[red, green, blue, alpha]
    rgb = np.array(np.core.records.fromarrays(rgb_reduced.T, dtype=cdt))
    return rgb


def filter_on_xyz(points, box):
    """
    returns a new array with points outside the
    box removed
    """
    return points[
        (points['X'] > box.xmin) &
        (points['X'] < box.xmax) &
        (points['Y'] > box.ymin) &
        (points['Y'] < box.ymax) &
        (points['Z'] > box.zmin) &
        (points['Z'] < box.zmax)
    ]


def box_reduced(box, scales, offsets):
    """
    returns the bbox with scales/offsets applied
    """
    return the_bbox(
        (box.xmin - offsets[0]) / scales[0],
        (box.ymin - offsets[1]) / scales[1],
        (box.zmin - offsets[2]) / scales[2],
        (box.xmax - offsets[0]) / scales[0],
        (box.ymax - offsets[1]) / scales[1],
        (box.zmax - offsets[2]) / scales[2]
    )


def get_points(session, box, lod, offsets, pcid, scales, schema, isleaf):
    sql = sql_query(session, box, pcid, lod, isleaf)

    pcpatch_wkb = session.query(sql)[0][0]
    points, _ = read_uncompressed_patch(pcpatch_wkb, schema)

    # randomizing points here (ie: after patches have been merged) allow the client
    # to display a fraction of the points and get a meaningful representation of the
    # total.
    # Without sorting/shuffling, displaying 30% of the points would result in
    # displaying 30% of the patches.
    np.random.shuffle(points)

    # remove points outside the bounding box
    points = filter_on_xyz(points, box_reduced(box, scales, offsets))
    npoints = len(points)

    fields = points.dtype.fields.keys()

    if 'Red' in fields:
        if max(points['Red']) > 255:
            # normalize
            rgb_reduced = np.c_[points['Red'] % 255,
                                points['Green'] % 255,
                                points['Blue'] % 255,
                                np.ones(npoints) * 255]
            rgb = np.array(np.core.records.fromarrays(rgb_reduced.T, dtype=cdt))
        else:
            rgb = points[['Red', 'Green', 'Blue']].astype(cdt)
    elif 'Classification' in fields:
        rgb = classification_to_rgb(points)
    else:
        # No colors
        # FIXME: compute color gradient based on elevation
        rgb_reduced = np.zeros((npoints, 3), dtype=int)
        rgb = np.array(np.core.records.fromarrays(rgb_reduced.T, dtype=cdt))

    quantized_points_r = np.c_[
        (points['X'] * scales[0] + offsets[0]) - box.xmin,
        (points['Y'] * scales[1] + offsets[1]) - box.ymin,
        (points['Z'] * scales[2] + offsets[2]) - box.zmin
    ]

    # Compute the min/max of offseted/scaled values
    realmin = [
        (np.min(points['X']) * scales[0] + offsets[0]) - box.xmin,
        (np.min(points['Y']) * scales[1] + offsets[1]) - box.ymin,
        (np.min(points['Z']) * scales[2] + offsets[2]) - box.zmin
    ]
    realmax = [
        (np.max(points['X']) * scales[0] + offsets[0]) - box.xmin,
        (np.max(points['Y']) * scales[1] + offsets[1]) - box.ymin,
        (np.max(points['Z']) * scales[2] + offsets[2]) - box.zmin
    ]

    quantized_points = np.array(np.core.records.fromarrays(quantized_points_r.T, dtype=pdt))
    header = np.array(
        [
            realmin[0], realmin[1], realmin[2],
            realmax[0], realmax[1], realmax[2]
        ], dtype='float32')

    buffer = header.tostring() + quantized_points.tostring() + rgb.tostring()
    return buffer


def sql_query(session, box, pcid, lod, isleaf):
    diag = boundingbox_to_diagonal([
        box.xmin, box.ymin, box.zmin, box.xmax, box.ymax, box.zmax
    ])

    maxppp = session.lopocstable.max_points_per_patch
    patch_size = session.patch_size

    psize = compute_psize(session, lod)
    start = 1 + sum([compute_psize(session, l) for l in range(lod)])
    count = min(psize, patch_size - start)

    if isleaf:
        # we want all points left
        count = patch_size - start

    sql = POINT_QUERY.format(
        x1=box.xmin, x2=box.xmax,
        y1=box.ymin, y2=box.ymax,
        z1=box.zmin, z2=box.zmax,
        **locals())
    return sql
