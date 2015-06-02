# -*- mode: python; coding: utf-8 -*-
# Copyright 2014-2015 Peter Williams <peter@newton.cx> and collaborators.
# Licensed under the MIT License.

"""numutil - NumPy and generic numerical utilities.

Functions:

dfsmooth          - Convolve data series in a pandas DataFrame with a window.
fits_recarray_to_data_frame
                  - Convert a FITS data table to a Pandas DataFrame
make_step_lcont   - Return a step function that is left-continuous.
make_step_rcont   - Return a step function that is right-continuous.
make_tophat_ee    - Return a tophat function operating on an exclusive/exclusive range.
make_tophat_ei    - Return a tophat function operating on an exclusive/inclusive range.
make_tophat_ie    - Return a tophat function operating on an inclusive/exclusive range.
make_tophat_ii    - Return a tophat function operating on an inclusive/inclusive range.
reduce_data_frame - Reduce rows of a DataFrame in chunks
reduce_data_frame_evenly_with_gaps
                  - Reduce DataFrame rows in approximately even-sized chunks.
rms               - Calculate the square root of the mean of the squares of x.
parallel_newton   - Parallelized invocation of `scipy.optimize.newton`.
parallel_quad     - Parallelized invocation of `scipy.integrate.quad`.
slice_around_gaps - Slice an ordered array into chunks separated by gaps.
slice_evenly_with_gaps
                  - Generate approximately same-sized slices of an ordered array.
unit_tophat_ee    - Tophat function on (0,1).
unit_tophat_ei    - Tophat function on (0,1].
unit_tophat_ie    - Tophat function on [0,1).
unit_tophat_ii    - Tophat function on [0,1].
usmooth           - Convolve data series with a window, with uncertainties.
weighted_mean     - Compute a weighted mean from values and their uncertainties.
weighted_variance - Estimate the variance of a weighted sampled.

Decorators:

broadcastize - Make a Python function automatically broadcast arguments.

"""
from __future__ import absolute_import, division, print_function, unicode_literals

__all__ = (b'''broadcastize dfsmooth fits_recarray_to_data_frame make_step_lcont
           make_step_rcont make_tophat_ee make_tophat_ei make_tophat_ie
           make_tophat_ii parallel_newton parallel_quad rms unit_tophat_ee
           unit_tophat_ei unit_tophat_ie unit_tophat_ii usmooth
           weighted_variance''').split ()

import functools
import numpy as np

from .method_decorator import method_decorator


def _broadcastize_spec_to_scalar_filter (s):
    if s is None:
        # This return value is independent of the inputs altogether.
        return lambda x: x
    if s == 0:
        # This return value has the same shape as the input(s). If we
        # promoted to a 1-element vector, we need to demote.
        return np.asscalar
    if s == 1:
        # This return value is a larger vector of the input(s). If we promoted
        # from a scalar, we drop the final axis. We asarray() the result for
        # convenience/robustness.
        return lambda x: np.asarray (x)[...,0]

    raise ValueError ('unrecognized @broadcastize ret_spec value %r' % s)


class _Broadcaster (method_decorator):
    # _BroadcasterDecorator must set self._n_arr and _scalar_ret_filter on creation.

    def fixup (self, newobj):
        # This function is used by the method_decorator superclass.
        newobj._n_arr = object.__getattribute__ (self, '_n_arr')
        newobj._scalar_ret_filter = object.__getattribute__ (self, '_scalar_ret_filter')

    def __call__ (self, *args, **kwargs):
        n_arr = object.__getattribute__ (self, '_n_arr')

        if len (args) < n_arr:
            raise TypeError ('expected at least %d arguments, got %d'
                             % (n_arr, len (args)))

        bc_raw = np.broadcast_arrays (*args[:n_arr])
        bc_1d = tuple (np.atleast_1d (a) for a in bc_raw)
        rest = args[n_arr:]
        result = super (_Broadcaster, self).__call__ (*(bc_1d + rest), **kwargs)

        if bc_raw[0].ndim == 0:
            # Inputs were all scalars. We need to filter the output(s) to
            # remove extra axes.
            scalar_ret_filter = object.__getattribute__ (self, '_scalar_ret_filter')
            result = scalar_ret_filter (result)

        return result


class _BroadcasterDecorator (object):
    """Decorator to make functions automatically work on vectorized arguments.

    @broadcastize (3) # myfunc's first 3 arguments should be arrays.
    def myfunc (arr1, arr2, arr3, non_vec_arg1, non_vec_arg2):
        ...

    This decorator makes it so that the child function's arguments are assured
    to be Numpy arrays of at least 1 dimension, and all having the same shape.
    The child can then perform vectorized computations without having to
    special-case scalar-vs.-vector possibilities or worry about manual
    broadcasting. If the inputs to the function are indeed all scalars, the output
    is converted back to scalar upon return.

    Therefore, for the caller, the function appears to have magic broadcasting
    rules equivalent to a Numpy ufunc. Meanwhile the implementer can get
    broadcasting behavior without having to special case the actual inputs.

    """
    def __init__ (self, n_arr, ret_spec=0):
        """Decorator for making a auto-broadcasting function. Arguments:

        n_arr - The number of array arguments accepted by the decorated function.
                These arguments come at the beginning of the argument list.

        """
        self._n_arr = int (n_arr)
        if self._n_arr < 1:
            raise ValueError ('broadcastiz\'ed function must take at least 1 '
                              'array argument')

        if isinstance (ret_spec, tuple):
            filters = tuple (_broadcastize_spec_to_scalar_filter (s) for s in ret_spec)
            self._scalar_ret_filter = lambda r: tuple (f (v) for f, v in zip (filters, r))
        else:
            self._scalar_ret_filter = _broadcastize_spec_to_scalar_filter (ret_spec)


    def __call__ (self, subfunc):
        b = _Broadcaster (subfunc)
        b._n_arr = self._n_arr
        b._scalar_ret_filter = self._scalar_ret_filter
        return b

broadcastize = _BroadcasterDecorator


# Very misc.

def fits_recarray_to_data_frame (recarray):
    """Convert a FITS data table, stored as a Numpy record array, into a Pandas
    DataFrame object.

    FITS data are big-endian, whereas nowadays almost everything is
    little-endian. This seems to be an issue for Pandas DataFrames, where
    df[['col1', 'col2']] triggers an assertion for me if the underlying data
    are not native-byte-ordered. While we're at it, we lower-case the column
    names.

    """
    from pandas import DataFrame

    def normalize (col):
        n = col.name
        if recarray[n].dtype.isnative:
            return (n.lower (), recarray[n])
        return (n.lower (), recarray[n].byteswap (True).newbyteorder ())

    return DataFrame (dict (normalize (c) for c in recarray.columns))


# Chunked averaging of data tables

def slice_around_gaps (values, maxgap):
    """Given an ordered array of values, generate a set of slices that traverse
    all of the values. Within each slice, no gap between adjacent values is
    larger than `maxgap`. In other words, these slices break the array into
    chunks separated by gaps of size larger than maxgap.

    """
    if not (maxgap > 0):
        # above test catches NaNs, other weird cases
        raise ValueError ('maxgap must be positive; got %r' % maxgap)

    values = np.asarray (values)
    delta = values[1:] - values[:-1]

    if np.any (delta < 0):
        raise ValueError ('values must be in nondecreasing order')

    whgap = np.where (delta > maxgap)[0] + 1
    prev_idx = None

    for gap_idx in whgap:
        yield slice (prev_idx, gap_idx)
        prev_idx = gap_idx

    yield slice (prev_idx, None)


def slice_evenly_with_gaps (values, target_len, maxgap):
    """Given an ordered array of values, generate a set of slices that traverse
    all of the values. Each slice contains about `target_len` items. However,
    no slice contains a gap larger than `maxgap`, so a slice may contain only
    a single item (if it is surrounded on both sides by a large gap). If a
    non-gapped run of values does not divide evenly into `target_len`, the
    algorithm errs on the side of making the slices contain more than
    `target_len` items, rather than fewer. It also attempts to keep the slice
    size uniform within each non-gapped run.

    """
    if not (target_len > 0):
        raise ValueError ('target_len must be positive; got %r' % target_len)

    values = np.asarray (values)
    l = values.size

    for gapslice in slice_around_gaps (values, maxgap):
        start, stop, ignored_stride = gapslice.indices (l)
        num_elements = stop - start
        nsegments = int (np.floor (float (num_elements) / target_len))
        nsegments = max (nsegments, 1)
        nsegments = min (nsegments, num_elements)
        segment_len = num_elements / nsegments
        offset = 0.
        prev = start

        for _ in xrange (nsegments):
            offset += segment_len
            next = start + int (round (offset))
            if next > prev:
                yield slice (prev, next)
            prev = next


def reduce_data_frame (df, chunk_slicers,
                       avg_cols=(),
                       uavg_cols=(),
                       minmax_cols=(),
                       nchunk_colname='nchunk',
                       uncert_prefix='u',
                       min_points_per_chunk=3):
    """"Reduce" a DataFrame by collapsing rows in grouped chunks. Returns another
    DataFrame with similar columns but fewer rows.

    `chunk_slicers` is an iterable that returns values that are used to slice
    `df` with its `iloc()` method. An example value might be the generator
    returned from `slice_evenly_with_gaps`.

       avg_cols - Reduced by taking the mean
      uavg_cols - Reduced by taking a weighted mean
    minmax_cols - Reduced by reporting the min and max values in each chunk.

    """
    subds = [df.iloc[idx] for idx in chunk_slicers]
    subds = [sd for sd in subds if sd.shape[0] >= min_points_per_chunk]

    chunked = df.__class__ ({nchunk_colname: np.zeros (len (subds), dtype=np.int)})

    # Some future-proofing: allow possibility of different ways of mapping
    # from a column giving a value to a column giving its uncertainty.

    uncert_col_name = lambda c: uncert_prefix + c

    for i, subd in enumerate (subds):
        label = chunked.index[i]
        chunked.loc[label,nchunk_colname] = subd.shape[0]

        for col in avg_cols:
            chunked.loc[label,col] = subd[col].mean ()

        for col in uavg_cols:
            ucol = uncert_col_name (col)
            v, u = weighted_mean (subd[col], subd[ucol])
            chunked.loc[label,col] = v
            chunked.loc[label,ucol] = u

        for col in minmax_cols:
            chunked.loc[label, 'min_'+col] = subd[col].min ()
            chunked.loc[label, 'max_'+col] = subd[col].max ()

    return chunked


def reduce_data_frame_evenly_with_gaps (df, valcol, target_len, maxgap, **kwargs):
    return reduce_data_frame (df,
                              slice_evenly_with_gaps (df[valcol], target_len, maxgap),
                              **kwargs)


# Smooth a timeseries with uncertainties

def usmooth (window, uncerts, *data, **kwargs):
    """Smooth data series according to a window, weighting based on uncertainties.
    Arguments:

    window  - the window
    uncerts - an array of uncertainties used to weight the smoothing
    *data   - the data series, same size as `uncerts`
    k=None  - Only keep every `k`th point of the results; if k is None,
              it is set to window.size.

    Returns: (s_uncerts, s_data[0], s_data[1], ...) - the smoothed uncertainties
    and data series.

    Example:

        u, x, y = numutil.usmooth (np.hamming (7), u, x, y)

    """
    window = np.asarray (window)
    uncerts = np.asarray (uncerts)

    # Hacky keyword argument handling because you can't write "def foo (*args,
    # k=0)".

    k = kwargs.pop ('k', None)

    if len (kwargs):
        raise TypeError ("smooth() got an unexpected keyword argument '%s'"
                         % kwargs.keys ()[0])

    # Done with kwargs futzing.

    if k is None:
        k = window.size

    conv = lambda q, r: np.convolve (q, r, mode='valid')

    if uncerts is None:
        w = np.ones_like (x)
    else:
        w = uncerts ** -2

    cw = conv (w, window)
    cu = np.sqrt (conv (w, window**2)) / cw
    result = [cu] + [conv (w * np.asarray (x), window) / cw for x in data]

    if k != 1:
        result = [x[::k] for x in result]
    return result


def dfsmooth (window, df, ucol, k=None):
    """Smooth a Pandas DataFrame according to a window, weighting based on
    uncertainties. Arguments:

    window  - the window.
    df      - the data frame.
    ucol    - the name of the column in `df` that contains the uncertainties
              to weight by.
    k=None  - Only keep every `k`th point of the results; if k is None,
              it is set to window.size.

    Returns: a smoothed data frame.

    The returned data frame has a default integer index.

    Example:

        sdata = numutil.dfsmooth (np.hamming (7), data, 'u_temp')

    """
    import pandas as pd

    if k is None:
        k = window.size

    conv = lambda q, r: np.convolve (q, r, mode='valid')
    w = df[ucol] ** -2
    invcw = 1. / conv (w, window)

    # XXX: we're not smoothing the index.

    res = {}

    for col in df.columns:
        if col == ucol:
            res[col] = np.sqrt (conv (w, window**2)) * invcw
        else:
            res[col] = conv (w * df[col], window) * invcw

    res = pd.DataFrame (res)
    return res[::k]


# Parallelized versions of various routines that don't operate vectorially
# even though sometimes it'd be nice to pretend that they do.

def parallel_newton (func, x0, fprime=None, par_args=(), simple_args=(), tol=1.48e-8,
                     maxiter=50, parallel=True, **kwargs):
    """A parallelized version of `scipy.optimize.newton`.

    Arguments:

    func        - The function to search for zeros, called as f(x, [*par_args...], [*simple_args...])
    x0          - The initial point for the zero search.
    fprime      - (Optional) The first derivative of `func`, called the same way.
    par_args    - Tuple of additional parallelized arguments.
    simple_args - Tuple of additional arguments passed identically to every invocation.
    tol         - The allowable error of the zero value.
    maxiter     - Maximum number of iterations.
    parallel    - Controls parallelization; default uses all available cores.
                  See `pwkit.parallel.make_parallel_helper`.
    **kwargs    - Passed to `scipy.optimize.newton`.

    Returns: locations of zeros.

    Finds zeros in parallel. The values `x0`, `tol`, `maxiter`, and the items
    of `par_args` should all be numeric, and may be N-dimensional Numpy
    arrays. They are all broadcast to a common shape, and one zero-finding run
    is performed for each element in the resulting array. The return value is
    an array of zero locations having the same shape as the common broadcast
    of the parameters named above.

    The `simple_args` are passed to each function identically for each
    integration. They do not need to be Pickle-able.

    Example:

    >>> parallel_newton (lambda x, a: x - 2 * a, 2,
                         par_args=(np.arange (6),))
    <<< array([  0.,   2.,   4.,   6.,   8.,  10.])

    >>> parallel_newton (lambda x: np.sin (x), np.arange (6))
    <<< array([  0.00000000e+00,   3.65526589e-26,   3.14159265e+00,
                 3.14159265e+00,   3.14159265e+00,   6.28318531e+00])

    """
    from scipy.optimize import newton

    from .parallel import make_parallel_helper
    phelp = make_parallel_helper (parallel)

    if not isinstance (par_args, tuple):
        raise ValueError ('par_args must be a tuple')

    if not isinstance (simple_args, tuple):
        raise ValueError ('simple_args must be a tuple')

    bc_raw = np.broadcast_arrays (x0, tol, maxiter, *par_args)
    bc_1d = tuple (np.atleast_1d (a) for a in bc_raw)

    def gen_var_args ():
        for i in xrange (bc_1d[0].size):
            yield tuple (x.flat[i] for x in bc_1d)

    def helper (i, _, var_args):
        x0, tol, maxiter = var_args[:3]
        args = var_args[3:] + simple_args
        return newton (func, x0, fprime=fprime, args=args, tol=tol,
                       maxiter=maxiter, **kwargs)

    with phelp.get_ppmap () as ppmap:
        result = np.asarray (ppmap (helper, None, gen_var_args ()))

    if bc_raw[0].ndim == 0:
        return np.asscalar (result)
    return result


def parallel_quad (func, a, b, par_args=(), simple_args=(), parallel=True, **kwargs):
    """A parallelized version of `scipy.integrate.quad`.

    Arguments:

    func        - The function to integrate, called as f(x, [*par_args...], [*simple_args...])
    a           - The lower limit(s) of integration.
    b           - The upper limits(s) of integration.
    par_args    - Tuple of additional parallelized arguments.
    simple_args - Tuple of additional arguments passed identically to every invocation.
    parallel    - Controls parallelization; default uses all available cores.
                  See `pwkit.parallel.make_parallel_helper`.
    **kwargs    - Passed to `scipy.integrate.quad`. Don't set 'full_output' to True.

    Returns: integrals and errors, see below.

    Computes many integrals in parallel. The values `a`, `b`, and the items of
    `par_args` should all be numeric, and may be N-dimensional Numpy arrays.
    They are all broadcast to a common shape, and one integral is performed
    for each element in the resulting array. If this common shape is (X,Y,Z),
    the return value has shape (2,X,Y,Z), where the subarray [0,...] contains
    the computed integrals and the subarray [1,...] contains the absolute
    error estimates. If `a`, `b`, and the items in `par_args` are all scalars,
    the return value has shape (2,).

    The `simple_args` are passed to each integrand function identically for each
    integration. They do not need to be Pickle-able.

    Example:

    >>> parallel_quad (lambda x, u, v, q: u * x + v,
                       0, # a
                       [3, 4], # b
                       (np.arange (6).reshape ((3,2)), np.arange (3).reshape ((3,1))), # par_args
                       ('hello',),)

    Computes six integrals and returns an array of shape (2,3,2). The
    functions that are evaluated are

      [[ 0*x + 0, 1*x + 0 ],
       [ 2*x + 1, 3*x + 1 ],
       [ 4*x + 2, 5*x + 2 ]]

    and the bounds of the integrals are

      [[ (0, 3), (0, 4) ],
       [ (0, 3), (0, 4) ],
       [ (0, 3), (0, 4) ]]

    In all cases the unused fourth parameter 'q' is 'hello'.

    """
    from scipy.integrate import quad

    from .parallel import make_parallel_helper
    phelp = make_parallel_helper (parallel)

    if not isinstance (par_args, tuple):
        raise ValueError ('par_args must be a tuple')

    if not isinstance (simple_args, tuple):
        raise ValueError ('simple_args must be a tuple')

    bc_raw = np.broadcast_arrays (a, b, *par_args)
    bc_1d = tuple (np.atleast_1d (a) for a in bc_raw)

    def gen_var_args ():
        for i in xrange (bc_1d[0].size):
            yield tuple (x.flat[i] for x in bc_1d)

    def helper (i, _, var_args):
        a, b = var_args[:2]
        return quad (func, a, b, var_args[2:] + simple_args, **kwargs)

    with phelp.get_ppmap () as ppmap:
        result_list = ppmap (helper, None, gen_var_args ())

    if bc_raw[0].ndim == 0:
        return np.asarray (result_list[0])

    result_arr = np.empty ((2,) + bc_raw[0].shape)
    for i in xrange (bc_1d[0].size):
        result_arr[0].flat[i], result_arr[1].flat[i] = result_list[i]
    return result_arr


# Some miscellaneous numerical tools

def rms (x):
    """Return the square root of the mean of the squares of ``x``."""
    return np.sqrt (np.square (x).mean ())


def weighted_mean (values, uncerts, **kwargs):
    values = np.asarray (values)
    uncerts = np.asarray (uncerts)
    weights = uncerts ** -2
    wt_mean, wt_sum = np.average (values, weights=weights, returned=True, **kwargs)
    return wt_mean, wt_sum ** -0.5


def weighted_variance (x, weights):
    """Return the variance of a weighted sample.

    The weighted sample mean is calculated and subtracted off, so the returned
    variance is upweighted by ``n / (n - 1)``. If the sample mean is known to
    be zero, you should just compute ``np.average (x**2, weights=weights)``.

    """
    n = len (x)
    if n < 3:
        raise ValueError ('cannot calculate meaningful variance of fewer '
                          'than three samples')
    wt_mean = np.average (x, weights=weights)
    return np.average (np.square (x - wt_mean), weights=weights) * n / (n - 1)


# Tophat functions -- numpy doesn't have anything built-in (that I know of)
# that does this in a convenient way that I'd like. These are useful for
# defining functions in a piecewise-ish way, although also pay attention to
# the existence of np.piecewise!
#
# We're careful with inclusivity/exclusivity of the bounds since that can be
# important.

def unit_tophat_ee (x):
    """Tophat function on the unit interval, left-exclusive and right-exclusive.
    Returns 1 if 0 < x < 1, 0 otherwise.

    """
    x = np.asarray (x)
    x1 = np.atleast_1d (x)
    r = ((0 < x1) & (x1 < 1)).astype (x.dtype)
    if x.ndim == 0:
        return np.asscalar (r)
    return r


def unit_tophat_ei (x):
    """Tophat function on the unit interval, left-exclusive and right-inclusive.
    Returns 1 if 0 < x <= 1, 0 otherwise.

    """
    x = np.asarray (x)
    x1 = np.atleast_1d (x)
    r = ((0 < x1) & (x1 <= 1)).astype (x.dtype)
    if x.ndim == 0:
        return np.asscalar (r)
    return r


def unit_tophat_ie (x):
    """Tophat function on the unit interval, left-inclusive and right-exclusive.
    Returns 1 if 0 <= x < 1, 0 otherwise.

    """
    x = np.asarray (x)
    x1 = np.atleast_1d (x)
    r = ((0 <= x1) & (x1 < 1)).astype (x.dtype)
    if x.ndim == 0:
        return np.asscalar (r)
    return r


def unit_tophat_ii (x):
    """Tophat function on the unit interval, left-inclusive and right-inclusive.
    Returns 1 if 0 <= x <= 1, 0 otherwise.

    """
    x = np.asarray (x)
    x1 = np.atleast_1d (x)
    r = ((0 <= x1) & (x1 <= 1)).astype (x.dtype)
    if x.ndim == 0:
        return np.asscalar (r)
    return r


def make_tophat_ee (lower, upper):
    """Return a ufunc-like tophat function on the defined range, left-exclusive
    and right-exclusive. Returns 1 if lower < x < upper, 0 otherwise.

    """
    if not np.isfinite (lower):
        raise ValueError ('"lower" argument must be finite number; got %r' % lower)
    if not np.isfinite (upper):
        raise ValueError ('"upper" argument must be finite number; got %r' % upper)

    def range_tophat_ee (x):
        x = np.asarray (x)
        x1 = np.atleast_1d (x)
        r = ((lower < x1) & (x1 < upper)).astype (x.dtype)
        if x.ndim == 0:
            return np.asscalar (r)
        return r

    range_tophat_ee.__doc__ = ('Ranged tophat function, left-exclusive and '
                               'right-exclusive. Returns 1 if %g < x < %g, '
                               '0 otherwise.') % (lower, upper)
    return range_tophat_ee


def make_tophat_ei (lower, upper):
    """Return a ufunc-like tophat function on the defined range, left-exclusive
    and right-inclusive. Returns 1 if lower < x <= upper, 0 otherwise.

    """
    if not np.isfinite (lower):
        raise ValueError ('"lower" argument must be finite number; got %r' % lower)
    if not np.isfinite (upper):
        raise ValueError ('"upper" argument must be finite number; got %r' % upper)

    def range_tophat_ei (x):
        x = np.asarray (x)
        x1 = np.atleast_1d (x)
        r = ((lower < x1) & (x1 <= upper)).astype (x.dtype)
        if x.ndim == 0:
            return np.asscalar (r)
        return r

    range_tophat_ei.__doc__ = ('Ranged tophat function, left-exclusive and '
                               'right-inclusive. Returns 1 if %g < x <= %g, '
                               '0 otherwise.') % (lower, upper)
    return range_tophat_ei


def make_tophat_ie (lower, upper):
    """Return a ufunc-like tophat function on the defined range, left-inclusive
    and right-exclusive. Returns 1 if lower <= x < upper, 0 otherwise.

    """
    if not np.isfinite (lower):
        raise ValueError ('"lower" argument must be finite number; got %r' % lower)
    if not np.isfinite (upper):
        raise ValueError ('"upper" argument must be finite number; got %r' % upper)

    def range_tophat_ie (x):
        x = np.asarray (x)
        x1 = np.atleast_1d (x)
        r = ((lower <= x1) & (x1 < upper)).astype (x.dtype)
        if x.ndim == 0:
            return np.asscalar (r)
        return r

    range_tophat_ie.__doc__ = ('Ranged tophat function, left-inclusive and '
                               'right-exclusive. Returns 1 if %g <= x < %g, '
                               '0 otherwise.') % (lower, upper)
    return range_tophat_ie


def make_tophat_ii (lower, upper):
    """Return a ufunc-like tophat function on the defined range, left-inclusive
    and right-inclusive. Returns 1 if lower < x < upper, 0 otherwise.

    """
    if not np.isfinite (lower):
        raise ValueError ('"lower" argument must be finite number; got %r' % lower)
    if not np.isfinite (upper):
        raise ValueError ('"upper" argument must be finite number; got %r' % upper)

    def range_tophat_ii (x):
        x = np.asarray (x)
        x1 = np.atleast_1d (x)
        r = ((lower <= x1) & (x1 <= upper)).astype (x.dtype)
        if x.ndim == 0:
            return np.asscalar (r)
        return r

    range_tophat_ii.__doc__ = ('Ranged tophat function, left-inclusive and '
                               'right-inclusive. Returns 1 if %g <= x <= %g, '
                               '0 otherwise.') % (lower, upper)
    return range_tophat_ii


# Step functions

def make_step_lcont (transition):
    """Return a ufunc-like step function that is left-continuous. Returns 1 if
    x > transition, 0 otherwise.

    """
    if not np.isfinite (transition):
        raise ValueError ('"transition" argument must be finite number; got %r' % transition)

    def step_lcont (x):
        x = np.asarray (x)
        x1 = np.atleast_1d (x)
        r = (x1 > transition).astype (x.dtype)
        if x.ndim == 0:
            return np.asscalar (r)
        return r

    step_lcont.__doc__ = ('Left-continuous step function. Returns 1 if x > %g, '
                          '0 otherwise.') % (transition,)
    return step_lcont


def make_step_rcont (transition):
    """Return a ufunc-like step function that is right-continuous. Returns 1 if
    x >= transition, 0 otherwise.

    """
    if not np.isfinite (transition):
        raise ValueError ('"transition" argument must be finite number; got %r' % transition)

    def step_rcont (x):
        x = np.asarray (x)
        x1 = np.atleast_1d (x)
        r = (x1 >= transition).astype (x.dtype)
        if x.ndim == 0:
            return np.asscalar (r)
        return r

    step_rcont.__doc__ = ('Right-continuous step function. Returns 1 if x >= '
                          '%g, 0 otherwise.') % (transition,)
    return step_rcont
