from __future__ import absolute_import

import sys
import warnings

from collections import defaultdict

import numpy as np

from ..dimension import dimension_name
from ..util import isscalar, unique_iterator
from .interface import DataError, Interface
from .multipath import MultiInterface
from .pandas import PandasInterface


class SpatialPandasInterface(MultiInterface):

    types = ()

    datatype = 'spatialpandas'

    multi = True

    @classmethod
    def loaded(cls):
        return 'spatialpandas' in sys.modules

    @classmethod
    def applies(cls, obj):
        if not cls.loaded():
            return False
        from spatialpandas import GeoDataFrame, GeoSeries
        return isinstance(obj, (GeoDataFrame, GeoSeries))

    @classmethod
    def geo_column(cls, data):
        from spatialpandas import GeoSeries
        col = 'geometry'
        if col in data and isinstance(data[col], GeoSeries):
            return col
        cols = [c for c in data.columns if isinstance(data[c], GeoSeries)]
        if not cols:
            raise ValueError('No geometry column found in spatialpandas.GeoDataFrame, '
                             'use the PandasInterface instead.')
        return cols[0]

    @classmethod
    def init(cls, eltype, data, kdims, vdims):
        import pandas as pd
        from spatialpandas import GeoDataFrame, GeoSeries

        # Handle conversion from geopandas
        if 'geopandas' in sys.modules:
            import geopandas as gpd
            if isinstance(data, gpd.GeoSeries):
                data = data.to_frame()
            if isinstance(data, gpd.GeoDataFrame):
                data = GeoDataFrame(data)

        if kdims is None:
            kdims = eltype.kdims

        if vdims is None:
            vdims = eltype.vdims

        if isinstance(data, GeoSeries):
            data = data.to_frame()

        if isinstance(data, list):
            if 'shapely' in sys.modules:
                # Handle conversion from shapely geometries
                from shapely.geometry.base import BaseGeometry
                if not data:
                    pass
                elif all(isinstance(d, BaseGeometry) for d in data):
                    data = GeoSeries(data).to_frame()
                elif all(isinstance(d, dict) and 'geometry' in d and isinstance(d['geometry'], BaseGeometry)
                         for d in data):
                    new_data = {col: [] for col in data[0]}
                    for d in data:
                        for col, val in d.items():
                            new_data[col] = val
                    new_data['geometry'] = GeoSeries(new_data['geometry'])
                    data = GeoDataFrame(new_data)
            if isinstance(data, list):
                new_data = []
                types = []
                xname, yname = (kd.name for kd in kdims[:2])
                for d in data:
                    types.append(type(d))
                    if isinstance(d, dict):
                        new_data.append(d)
                        continue
                    new_el = eltype(d, kdims, vdims)
                    new_dict = {}
                    for d in new_el.dimensions():
                        if d in (xname, yname):
                            scalar = False
                        else:
                            scalar = new_el.interface.isscalar(new_el, d)
                        new_dict[d.name] = new_el.dimension_values(d, not scalar)
                    new_data.append(new_dict)
                if len(set(types)) > 1:
                    raise DataError('Mixed types not supported')
                columns = [d.name for d in kdims+vdims if d not in (xname, yname)]
                geom = cls.geom_type(eltype)
                data = to_spatialpandas(new_data, xname, yname, columns, geom)
        elif not isinstance(data, GeoDataFrame):
            raise ValueError("SpatialPandasInterface only support spatialpandas DataFrames.")
        elif 'geometry' not in data:
            cls.geo_column(data)

        index_names = data.index.names if isinstance(data, pd.DataFrame) else [data.index.name]
        if index_names == [None]:
            index_names = ['index']

        for kd in kdims+vdims:
            kd = dimension_name(kd)
            if kd in data.columns:
                continue
            if any(kd == ('index' if name is None else name)
                   for name in index_names):
                data = data.reset_index()
                break

        return data, {'kdims': kdims, 'vdims': vdims}, {}

    @classmethod
    def validate(cls, dataset, vdims=True):
        dim_types = 'key' if vdims else 'all'
        geom_dims = cls.geom_dims(dataset)
        if len(geom_dims) != 2:
            raise DataError('Expected %s instance to declare two key '
                            'dimensions corresponding to the geometry '
                            'coordinates but %d dimensions were found '
                            'which did not refer to any columns.'
                            % (type(dataset).__name__, len(geom_dims)), cls)
        not_found = [d.name for d in dataset.dimensions(dim_types)
                     if d not in geom_dims and d.name not in dataset.data]
        if not_found:
            raise DataError("Supplied data does not contain specified "
                             "dimensions, the following dimensions were "
                             "not found: %s" % repr(not_found), cls)


    @classmethod
    def dtype(cls, dataset, dimension):
        name = dataset.get_dimension(dimension, strict=True).name
        if name not in dataset.data:
            return np.dtype('float') # Geometry dimension
        return dataset.data[name].dtype


    @classmethod
    def has_holes(cls, dataset):
        from spatialpandas.geometry import (
            MultiPolygonDtype, PolygonDtype, Polygon, MultiPolygon
        )
        col = cls.geo_column(dataset.data)
        series = dataset.data[col]
        if isinstance(series.dtype, (MultiPolygonDtype, PolygonDtype)):
            return False
        for geom in series:
            if isinstance(geom, Polygon) and geom.interiors:
                return True
            elif isinstance(geom, MultiPolygon):
                for g in geom:
                    if isinstance(g, Polygon) and g.interiors:
                        return True
        return False

    @classmethod
    def holes(cls, dataset):
        holes = []
        if not len(dataset.data):
            return holes
        col = cls.geo_column(dataset.data)
        series = dataset.data[col]
        return [geom_to_holes(geom) for geom in series]

    @classmethod
    def select(cls, dataset, selection_mask=None, **selection):
        if cls.geom_dims(dataset):
            df = cls.shape_mask(dataset, selection)
        else:
            df = dataset.data
        if not selection:
            return df
        elif selection_mask is None:
            selection_mask = cls.select_mask(dataset, selection)
        indexed = cls.indexed(dataset, selection)
        df = df.iloc[selection_mask]
        if indexed and len(df) == 1 and len(dataset.vdims) == 1:
            return df[dataset.vdims[0].name].iloc[0]
        return df

    @classmethod
    def shape_mask(cls, dataset, selection):
        xdim, ydim = cls.geom_dims(dataset)
        xsel = selection.pop(xdim.name, None)
        ysel = selection.pop(ydim.name, None)
        if xsel is None and ysel is None:
            return dataset.data

        from shapely.geometry import box

        if xsel is None:
            x0, x1 = cls.range(dataset, xdim)
        elif isinstance(xsel, slice):
            x0, x1 = xsel.start, xsel.stop
        elif isinstance(xsel, tuple):
            x0, x1 = xsel
        else:
            raise ValueError("Only slicing is supported on geometries, %s "
                             "selection is of type %s."
                             % (xdim, type(xsel).__name__))

        if ysel is None:
            y0, y1 = cls.range(dataset, ydim)
        elif isinstance(ysel, slice):
            y0, y1 = ysel.start, ysel.stop
        elif isinstance(ysel, tuple):
            y0, y1 = ysel
        else:
            raise ValueError("Only slicing is supported on geometries, %s "
                             "selection is of type %s."
                             % (ydim, type(ysel).__name__))

        bounds = box(x0, y0, x1, y1)
        col = cls.geo_column(dataset.data)
        df = dataset.data.copy()
        df[col] = df[col].intersection(bounds)
        return df[df[col].area > 0]

    @classmethod
    def select_mask(cls, dataset, selection):
        mask = np.ones(len(dataset.data), dtype=np.bool)
        for dim, k in selection.items():
            if isinstance(k, tuple):
                k = slice(*k)
            arr = dataset.data[dim].values
            if isinstance(k, slice):
                with warnings.catch_warnings():
                    warnings.filterwarnings('ignore', r'invalid value encountered')
                    if k.start is not None:
                        mask &= k.start <= arr
                    if k.stop is not None:
                        mask &= arr < k.stop
            elif isinstance(k, (set, list)):
                iter_slcs = []
                for ik in k:
                    with warnings.catch_warnings():
                        warnings.filterwarnings('ignore', r'invalid value encountered')
                        iter_slcs.append(arr == ik)
                mask &= np.logical_or.reduce(iter_slcs)
            elif callable(k):
                mask &= k(arr)
            else:
                index_mask = arr == k
                if dataset.ndims == 1 and np.sum(index_mask) == 0:
                    data_index = np.argmin(np.abs(arr - k))
                    mask = np.zeros(len(dataset), dtype=np.bool)
                    mask[data_index] = True
                else:
                    mask &= index_mask
        return mask

    @classmethod
    def geom_dims(cls, dataset):
        return [d for d in dataset.kdims + dataset.vdims
                if d.name not in dataset.data]

    @classmethod
    def dimension_type(cls, dataset, dim):
        col = cls.geo_column(dataset.data)
        arr = geom_to_array(dataset.data[col].iloc[0])
        ds = dataset.clone(arr, datatype=cls.subtypes, vdims=[])
        return ds.interface.dimension_type(ds, dim)

    @classmethod
    def isscalar(cls, dataset, dim):
        """
        Tests if dimension is scalar in each subpath.
        """
        idx = dataset.get_dimension_index(dim)
        return idx not in [0, 1]

    @classmethod
    def range(cls, dataset, dim):
        dim = dataset.get_dimension(dim)
        geom_dims = cls.geom_dims(dataset)
        if dim in geom_dims:
            col = cls.geo_column(dataset.data)
            idx = geom_dims.index(dim)
            bounds = dataset.data[col].total_bounds
            if idx == 0:
                return (bounds[0], bounds[2])
            else:
                return (bounds[1], bounds[3])
        else:
            vals = dataset.data[dim.name]
            return vals.min(), vals.max()

    @classmethod
    def groupby(cls, dataset, dimensions, container_type, group_type, **kwargs):
        geo_dims = cls.geom_dims(dataset)
        if any(d in geo_dims for d in dimensions):
            raise DataError("SpatialPandasInterface does not allow grouping "
                            "by geometry dimension.", cls)
        return PandasInterface.groupby(dataset, dimensions, container_type, group_type, **kwargs)

    @classmethod
    def aggregate(cls, columns, dimensions, function, **kwargs):
        raise NotImplementedError

    @classmethod
    def sample(cls, columns, samples=[]):
        raise NotImplementedError

    @classmethod
    def reindex(cls, dataset, kdims=None, vdims=None):
        return dataset.data

    @classmethod
    def shape(cls, dataset):
        return (cls.length(dataset), len(dataset.dimensions()))

    @classmethod
    def length(cls, dataset):
        from spatialpandas.geometry import MultiPointDtype
        col_name = cls.geo_column(dataset.data)
        column = dataset.data[col_name]
        if not isinstance(column.dtype, MultiPointDtype):
            return PandasInterface.length(dataset)
        length = 0
        for geom in column:
            length += (len(geom.buffer_values)//2)
        return length

    @classmethod
    def nonzero(cls, dataset):
        return bool(cls.length(dataset))

    @classmethod
    def redim(cls, dataset, dimensions):
        return PandasInterface.redim(dataset, dimensions)

    @classmethod
    def add_dimension(cls, dataset, dimension, dim_pos, values, vdim):
        data = dataset.data.copy()
        geom_col = cls.geo_column(dataset.data)
        if dim_pos >= list(data.columns).index(geom_col):
            dim_pos -= 1
        if dimension.name not in data:
            data.insert(dim_pos, dimension.name, values)
        return data

    @classmethod
    def iloc(cls, dataset, index):
        from spatialpandas import GeoSeries
        from spatialpandas.geometry import MultiPointDtype
        rows, cols = index
        geom_dims = cls.geom_dims(dataset)
        geom_col = cls.geo_column(dataset.data)
        scalar = False
        columns = list(dataset.data.columns)
        if isinstance(cols, slice):
            cols = [d.name for d in dataset.dimensions()][cols]
        elif np.isscalar(cols):
            scalar = np.isscalar(rows)
            cols = [dataset.get_dimension(cols).name]
        else:
            cols = [dataset.get_dimension(d).name for d in index[1]]
        if not all(d in cols for d in geom_dims):
            raise DataError("Cannot index a dimension which is part of the "
                            "geometry column of a spatialpandas DataFrame.", cls)
        cols = list(unique_iterator([
            columns.index(geom_col) if c in geom_dims else columns.index(c) for c in cols
        ]))

        if np.isscalar(rows):
            rows = [rows]

        if isinstance(dataset.data[geom_col].dtype, MultiPointDtype):
            geoms = dataset.data[geom_col]
            count = 0
            new_geoms = []
            for geom in geoms:
                length = int(len(geom.buffer_values)/2)
                if np.isscalar(rows):
                    if (count+length) > rows >= count:
                        data = geom.buffer_values[rows-count]
                        new_geoms.append(type(geom)(data))
                        break
                elif isinstance(rows, slice):
                    if rows.start is not None and rows.start > (count+length):
                        continue
                    elif rows.stop is not None and rows.stop < count:
                        break
                    start = None if rows.start is None else max(rows.start - count, 0)*2
                    stop = None if rows.stop is None else min(rows.stop - count, length)*2
                    if rows.step is not None:
                        dataset.param.warning(".iloc step slicing currently not supported for"
                                              "the multi-tabular data format.")
                    slc = slice(start, stop)
                    new_geoms.append(type(geom)(geom.buffer_values[slc]))
                else:
                    sub_rows = [v for r in rows for v in ((r-count)*2, (r-count)*2+1)
                                if 0 <= (r-count) < (count+length)]
                    new_geoms.append(type(geom)(geom.buffer_values[np.array(sub_rows, dtype=int)]))
                count += length

            new = dataset.data.copy()
            new[geom_col] = GeoSeries(new_geoms)
            return new

        if scalar:
            return dataset.data.iloc[rows[0], cols[0]]
        return dataset.data.iloc[rows, cols]

    @classmethod
    def values(cls, dataset, dimension, expanded=True, flat=True, compute=True, keep_index=False):
        dimension = dataset.get_dimension(dimension)
        geom_dims = dataset.interface.geom_dims(dataset)
        data = dataset.data
        if (dimension not in geom_dims and not expanded) or keep_index:
            data = data[dimension.name]
            return data if keep_index else data.values
        elif not len(data):
            return np.array([])

        index = geom_dims.index(dimension)
        col = cls.geo_column(dataset.data)
        return geom_array_to_array(data[col].values, index, expanded)

    @classmethod
    def split(cls, dataset, start, end, datatype, **kwargs):
        objs = []
        xdim, ydim = dataset.kdims[:2]
        if not len(dataset.data):
            return []
        row = dataset.data.iloc[0]
        col = cls.geo_column(dataset.data)
        arr = geom_to_array(row[col])
        d = {(xdim.name, ydim.name): arr}
        d.update({vd.name: row[vd.name] for vd in dataset.vdims})
        ds = dataset.clone(d, datatype=['dictionary'])
        for i, row in dataset.data.iterrows():
            geom = row[col]
            arr = geom_to_array(geom)
            d = {xdim.name: arr[:, 0], ydim.name: arr[:, 1]}
            d.update({vd.name: row[vd.name] for vd in dataset.vdims})
            ds.data = d
            if datatype == 'array':
                obj = ds.array(**kwargs)
            elif datatype == 'dataframe':
                obj = ds.dframe(**kwargs)
            elif datatype == 'columns':
                obj = ds.columns(**kwargs)
            elif datatype is None:
                obj = ds.clone()
            else:
                raise ValueError("%s datatype not support" % datatype)
            objs.append(obj)
        return objs


def geom_to_array(geom, index=None, multi=False):
    """Converts spatialpandas geometry to an array.

    Args:
        geom: spatialpandas geometry
        index: The column index to return
        multi: Whether to concatenate multiple arrays or not

    Returns:
        Array or list of arrays.
    """
    from spatialpandas.geometry import (
        Point, Polygon, Line, Ring, MultiPolygon, MultiPoint
    )
    if isinstance(geom, Point):
        if index is None:
            return np.array([geom.x, geom.y])
        arrays = [np.array([geom.y if index else geom.x])]
    elif isinstance(geom, (Polygon, Line, Ring)):
        exterior = geom.data[0] if isinstance(geom, Polygon) else geom.data
        arr = np.array(exterior.as_py()).reshape(-1, 2)
        arrays = [arr if index is None else arr[:, index]]
    elif isinstance(geom, MultiPoint):
        if index is None:
            arrays = [np.array(geom.buffer_values).reshape(-1, 2)]
        else:
            arrays = [np.array(geom.buffer_values[index::2])]
    else:
        arrays = []
        for g in geom.data:
            exterior = g[0] if isinstance(geom, MultiPolygon) else g
            arr = np.array(exterior.as_py()).reshape(-1, 2)
            arrays.append(arr if index is None else arr[:, index])
            arrays.append([[np.nan, np.nan]] if index is None else [np.nan])
        arrays = arrays[:-1]

    if multi:
        return arrays
    elif len(arrays) == 1:
        return arrays[0]
    else:
        return np.concatenate(arrays)


def geom_to_holes(geom):
    """Extracts holes from spatialpandas Polygon geometries.

    Args:
        geom: spatialpandas geometry

    Returns:
        List of arrays representing holes
    """
    from spatialpandas.geometry import Polygon, MultiPolygon
    if isinstance(geom, Polygon):
        holes = []
        for i, hole in enumerate(geom.data):
            if i == 0:
                continue
            holes.append(np.array(hole.as_py()).reshape(-1, 2))
        return [holes]
    elif isinstance(geom, MultiPolygon):
        holes = []
        for poly in geom.data:
            poly_holes = []
            for i, hole in enumerate(poly):
                if i == 0:
                    continue
                arr = np.array(hole.as_py()).reshape(-1, 2)
                poly_holes.append(arr)
            holes.append(poly_holes)
        return holes
    elif 'Multi' in type(geom).__name__:
        return [[]]*len(geom)
    else:
        return [[]]


def geom_array_to_array(geom_array, index, expand=False):
    """Converts spatialpandas extension arrays to a flattened array.

    Args:
        geom: spatialpandas geometry
        index: The column index to return

    Returns:
        Flattened array
    """
    from spatialpandas.geometry import PointArray, MultiPointArray
    if isinstance(geom_array, PointArray):
        return geom_array.y if index else geom_array.x
    arrays = []
    multi_point = isinstance(geom_array, MultiPointArray)
    for geom in geom_array:
        array = geom_to_array(geom, index, multi=expand)
        if expand:
            arrays.extend(array)
            if not multi_point:
                arrays.append([np.nan])
        else:
            arrays.append(array)
    if expand:
        if not multi_point:
            arrays = arrays[:-1]
        return np.concatenate(arrays) if arrays else np.array([])
    else:
        return arrays


def to_spatialpandas(data, xdim, ydim, columns=[], geom='point'):
    """Converts list of dictionary format geometries to spatialpandas line geometries.

    Args:
        data: List of dictionaries representing individual geometries
        xdim: Name of x-coordinates column
        ydim: Name of y-coordinates column
        ring: Whether the data represents a closed ring

    Returns:
        A spatialpandas.GeoDataFrame version of the data
    """
    from spatialpandas import GeoSeries, GeoDataFrame
    from spatialpandas.geometry import (
        Point, Line, Polygon, Ring, MultiPolygon, MultiLine, LineArray, PolygonArray, MultiPoint,
        PointArray
    )
    poly = any('holes' in d for d in data)
    if poly:
        single_type, multi_type, array_type = Polygon, MultiPolygon, PolygonArray
    elif geom == 'Polygon':
        single_type, multi_type, array_type = Ring, MultiLine, LineArray
    elif geom == 'Line':
        single_type, multi_type, array_type = Line, MultiLine, LineArray
    else:
        single_type, multi_type, array_type = Point, MultiPoint, PointArray

    lines = defaultdict(list)
    for path in data:
        path = dict(path)
        if xdim not in path or ydim not in path:
            raise DataError('Could not find geometry dimensions')
        xs, ys = path.pop(xdim), path.pop(ydim)
        xscalar, yscalar = isscalar(xs), isscalar(ys)
        if xscalar and yscalar:
            xs, ys = np.array([xs]), np.array([ys])
        elif xscalar:
            xs = np.full_like(ys, xs)
        elif yscalar:
            ys = np.full_like(xs, ys)
        geom = np.column_stack([xs, ys])
        splits = np.where(np.isnan(geom[:, :2].astype('float')).sum(axis=1))[0]
        paths = np.split(geom, splits+1) if len(splits) else [geom]
        parts = []
        for i, p in enumerate(paths):
            if i != (len(paths)-1):
                p = p[:-1]
            if len(p) < (3 if poly else 2):
                continue
            holes = path.pop('holes', None)
            for c, v in path.items():
                lines[c].append(v)
            if poly:
                parts.append([])
                subparts = parts[-1]
            else:
                subparts = parts
            subparts.append(p[:, :2])
            if poly:
                subparts += [np.array(h) for h in holes[i]]

        if single_type is Point:
            geom_type = multi_type if sum(len(p) for p in parts) > 1 else single_type
            if geom_type is single_type:
                parts = parts[0].flatten()
            else:
                parts = np.concatenate([p.flatten() for p in parts])
        elif len(parts) > 1:
            geom_type = multi_type
            parts = [[p.flatten() for p in sp] if poly else sp.flatten() for sp in parts]
        else:
            geom_type = single_type
            parts = [p.flatten() for p in parts[0]] if poly else parts[0].flatten()
        lines['geometry'].append(geom_type(parts))

    if lines:
        lines['geometry'] = GeoSeries(lines['geometry'])
    else:
        lines['geometry'] = GeoSeries(array_type([]))
    return GeoDataFrame(lines, columns=['geometry']+columns)


Interface.register(SpatialPandasInterface)