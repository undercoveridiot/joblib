"""
Utilities for fast persistence of big data, with optional compression.
"""

# Author: Gael Varoquaux <gael dot varoquaux at normalesup dot org>
# Copyright (c) 2009 Gael Varoquaux
# License: BSD Style, 3 clauses.

import pickle
import traceback
import sys
import os
import zlib
import warnings

from ._compat import _basestring

from io import BytesIO

if sys.version_info[0] >= 3:
    Unpickler = pickle._Unpickler
    Pickler = pickle._Pickler
    xrange = range

else:
    Unpickler = pickle.Unpickler
    Pickler = pickle.Pickler

_MEGA = 2 ** 20
_MAX_LEN = len(hex(2 ** 64).rstrip('L'))

# To detect file types
_ZFILE_PREFIX = b'ZF'
_CHUNK_SIZE = 64 * 1024

###############################################################################
# Compressed file with Zlib


def _read_magic(file_handle):
    """ Utility to check the magic signature of a file identifying it as a
        Zfile
    """
    magic = file_handle.read(len(_ZFILE_PREFIX))
    # Pickling needs file-handles at the beginning of the file
    file_handle.seek(0)
    return magic


def read_zfile(file_handle, write_buf=None):
    """Read the z-file and return the content as a string

    Z-files are raw data compressed with zlib used internally by joblib
    for persistence. Backward compatibility is not guaranteed. Do not
    use for external purposes.
    """
    file_handle.seek(0)
    assert _read_magic(file_handle) == _ZFILE_PREFIX, \
        "File does not have the right magic"
    length = file_handle.read(len(_ZFILE_PREFIX) + _MAX_LEN)
    length = length[len(_ZFILE_PREFIX):]
    length = int(length, 16)
    decompresser = zlib.decompressobj()
    idx = 0
    if write_buf is None:
        write_buf = bytearray()
    while True:
        zchunk = file_handle.read(_CHUNK_SIZE)
        if not zchunk:
            break
        chunk = decompresser.decompress(zchunk)
        # Write chunks at a time into the buffer
        write_buf[idx:idx+len(chunk)] = chunk
        idx += len(chunk)
    write_buf[idx:] = decompresser.flush()  # Read the remainder

    assert len(write_buf) == length, (
        "Incorrect data length while decompressing %s."
        "The file could be corrupted." % file_handle)
    return write_buf


def write_zfile(file_handle, data, compress=1):
    """Write the data in the given file as a Z-file.

    Z-files are raw data compressed with zlib used internally by joblib
    for persistence. Backward compatibility is not guaranteed. Do not
    use for external purposes.
    """
    compresser = zlib.compressobj(compress)
    file_handle.write(_ZFILE_PREFIX)
    if hasattr(data, 'nbytes'):
        data_length = data.nbytes
    else:
        data_length = len(data)
    length = hex(data_length).encode('ascii')
    # If python 2.x, we need to remove the trailing 'L'
    # in the hex representation
    length = length.rstrip(b'L')
    # Store the length of the data
    file_handle.write(length.ljust(_MAX_LEN))

    if hasattr(data, 'flat'):
        # Numpy ndarray, flatten it out
        data = data.flat

    for i in xrange(data_length//_CHUNK_SIZE+1):
        chunk = data[i*_CHUNK_SIZE:(i+1)*_CHUNK_SIZE]
        file_handle.write(compresser.compress(chunk))

    tail = compresser.flush()
    if tail:  # Write the remainder
        file_handle.write(tail)


###############################################################################
# Utility objects for persistence.

class NDArrayWrapper(object):
    """ An object to be persisted instead of numpy arrays.

        The only thing this object does, is to carry the filename in which
        the array has been persisted, and the array subclass.
    """
    def __init__(self, filename, subclass):
        "Store the useful information for later"
        self.filename = filename
        self.subclass = subclass

    def read(self, unpickler):
        "Reconstruct the array"
        filename = os.path.join(unpickler._dirname, self.filename)
        # Load the array from the disk
        if unpickler.np.__version__ >= '1.3':
            array = unpickler.np.load(filename,
                                      mmap_mode=unpickler.mmap_mode)
        else:
            # Numpy does not have mmap_mode before 1.3
            array = unpickler.np.load(filename)
        # Reconstruct subclasses. This does not work with old
        # versions of numpy
        if (hasattr(array, '__array_prepare__')
                and not self.subclass in (unpickler.np.ndarray,
                                          unpickler.np.memmap)):
            # We need to reconstruct another subclass
            new_array = unpickler.np.core.multiarray._reconstruct(
                self.subclass, (0,), 'b')
            new_array.__array_prepare__(array)
            array = new_array
        return array

    #def __reduce__(self):
    #    return None


class ZNDArrayWrapper(NDArrayWrapper):
    """An object to be persisted instead of numpy arrays.

    This object stores the Zfile filename in which
    the data array has been persisted, and the meta information to
    retrieve it.

    The reason that we store the raw buffer data of the array and
    the meta information, rather than array representation routine
    (tostring) is that it enables us to use completely the strided
    model to avoid memory copies (a and a.T store as fast). In
    addition saving the heavy information separately can avoid
    creating large temporary buffers when unpickling data with
    large arrays.
    """
    def __init__(self, filename, init_args, state):
        "Store the useful information for later"
        self.filename = filename
        self.state = state
        self.init_args = init_args

    def read(self, unpickler):
        "Reconstruct the array from the meta-information and the z-file"
        # We do not support Numpy < 1.7 with Python 3
        if sys.version_info[0] == 3 and unpickler.np.__version__ < '1.7':
            raise NotImplementedError('Loading compressed files with Numpy'
                                      ' < 1.7 not supported in Python3')
        # Here we a simply reproducing the unpickling mechanism for numpy
        # arrays
        filename = os.path.join(unpickler._dirname, self.filename)
        array = unpickler.np.core.multiarray._reconstruct(*self.init_args)
        # First we construct an empty array with the original properties
        # This requires Numpy >= 1.7 on Python3 since before that arrays
        # initialized by __setstate__ with an empty string do not own their
        # data??
        array.__setstate__(self.state)
        # Then we resize it back to its original size
        array.resize(self.init_args[1])

        if sys.version_info[0] == 2:
            # Python2 buffer objects can be written directly into
            read_zfile(open(filename, 'rb'), array.data)
        else:
            # Memoryview objects do not support assignments to ndim > 1
            # (as of Python 3.3), so write into a bytearray instead
            # and eat the cost of a memory copy back into a memoryview
            array.data = memoryview(read_zfile(open(filename, 'rb')))

        return array


###############################################################################
# Pickler classes

class NumpyPickler(Pickler):
    """A pickler to persist of big data efficiently.

        The main features of this object are:

         * persistence of numpy arrays in separate .npy files, for which
           I/O is fast.

         * optional compression using Zlib, with a special care on avoid
           temporaries.
    """

    def __init__(self, filename, compress=0, cache_size=10):
        self._filename = filename
        self._filenames = [filename, ]
        self.cache_size = cache_size
        self.compress = compress
        if not self.compress:
            self.file = open(filename, 'wb')
        else:
            self.file = BytesIO()
        # Count the number of npy files that we have created:
        self._npy_counter = 0
        Pickler.__init__(self, self.file,
                         protocol=pickle.HIGHEST_PROTOCOL)
        # delayed import of numpy, to avoid tight coupling
        try:
            import numpy as np
        except ImportError:
            np = None
        self.np = np

    def _write_array(self, array, filename):
        if not self.compress:
            self.np.save(filename, array)
            container = NDArrayWrapper(os.path.basename(filename),
                                       type(array))
        else:
            filename += '.z'
            # Efficient compressed storage:
            # The meta data is stored in the container, and the core
            # numerics in a z-file
            init_args = (type(array), array.shape, 'b')
            # This requires numpy >= 1.7 on Python3:
            # Use the state to create an empty array object and resize
            # it after the data is read from the file
            state = (1, (0,), array.dtype,
                     array.flags.f_contiguous and not array.flags.c_contiguous,
                     '')

            zfile = open(filename, 'wb')
            write_zfile(zfile, array,
                        compress=self.compress)
            zfile.close()
            container = ZNDArrayWrapper(os.path.basename(filename),
                                        init_args, state)
        return container, filename

    def save(self, obj):
        """ Subclass the save method, to save ndarray subclasses in npy
            files, rather than pickling them. Of course, this is a
            total abuse of the Pickler class.
        """
        if self.np is not None and type(obj) in (self.np.ndarray,
                                                 self.np.matrix,
                                                 self.np.memmap):
            size = obj.size * obj.itemsize
            if self.compress and size < self.cache_size * _MEGA:
                # When compressing, as we are not writing directly to the
                # disk, it is more efficient to use standard pickling
                if type(obj) is self.np.memmap:
                    # Pickling doesn't work with memmaped arrays
                    obj = self.np.asarray(obj)
                return Pickler.save(self, obj)
            self._npy_counter += 1
            try:
                filename = '%s_%02i.npy' % (self._filename,
                                            self._npy_counter)
                # This converts the array in a container
                obj, filename = self._write_array(obj, filename)
                self._filenames.append(filename)
            except:
                self._npy_counter -= 1
                # XXX: We should have a logging mechanism
                print('Failed to save %s to .npy file:\n%s' % (
                    type(obj),
                    traceback.format_exc()))
        return Pickler.save(self, obj)

    def close(self):
        if self.compress:
            zfile = open(self._filename, 'wb')
            write_zfile(zfile,
                        self.file.getvalue(), self.compress)
            zfile.close()


class NumpyUnpickler(Unpickler):
    """A subclass of the Unpickler to unpickle our numpy pickles.
    """
    dispatch = Unpickler.dispatch.copy()

    def __init__(self, filename, file_handle, mmap_mode=None):
        self._filename = os.path.basename(filename)
        self._dirname = os.path.dirname(filename)
        self.mmap_mode = mmap_mode
        self.file_handle = self._open_pickle(file_handle)
        Unpickler.__init__(self, self.file_handle)
        try:
            import numpy as np
        except ImportError:
            np = None
        self.np = np

    def _open_pickle(self, file_handle):
        return file_handle

    def load_build(self):
        """ This method is called to set the state of a newly created
            object.

            We capture it to replace our place-holder objects,
            NDArrayWrapper, by the array we are interested in. We
            replace them directly in the stack of pickler.
        """
        Unpickler.load_build(self)
        if isinstance(self.stack[-1], NDArrayWrapper):
            if self.np is None:
                raise ImportError("Trying to unpickle an ndarray, "
                                  "but numpy didn't import correctly")
            nd_array_wrapper = self.stack.pop()
            array = nd_array_wrapper.read(self)
            self.stack.append(array)

    # Be careful to register our new method.
    if sys.version_info[0] >= 3:
        dispatch[pickle.BUILD[0]] = load_build
    else:
        dispatch[pickle.BUILD] = load_build


class ZipNumpyUnpickler(NumpyUnpickler):
    """A subclass of our Unpickler to unpickle on the fly from
    compressed storage."""

    def __init__(self, filename, file_handle):
        NumpyUnpickler.__init__(self, filename,
                                file_handle,
                                mmap_mode=None)

    def _open_pickle(self, file_handle):
        return BytesIO(read_zfile(file_handle))


###############################################################################
# Utility functions

def dump(value, filename, compress=0, cache_size=100):
    """Fast persistence of an arbitrary Python object into a files, with
    dedicated storage for numpy arrays.

    Parameters
    -----------
    value: any Python object
        The object to store to disk
    filename: string
        The name of the file in which it is to be stored
    compress: integer for 0 to 9, optional
        Optional compression level for the data. 0 is no compression.
        Higher means more compression, but also slower read and
        write times. Using a value of 3 is often a good compromise.
        See the notes for more details.
    cache_size: positive number, optional
        Fixes the order of magnitude (in megabytes) of the cache used
        for in-memory compression. Note that this is just an order of
        magnitude estimate and that for big arrays, the code will go
        over this value at dump and at load time.

    Returns
    -------
    filenames: list of strings
        The list of file names in which the data is stored. If
        compress is false, each array is stored in a different file.

    See Also
    --------
    joblib.load : corresponding loader

    Notes
    -----
    Memmapping on load cannot be used for compressed files. Thus
    using compression can significantly slow down loading. In
    addition, compressed files take extra extra memory during
    dump and load.
    """
    if compress is True:
        # By default, if compress is enabled, we want to be using 3 by
        # default
        compress = 3
    if not isinstance(filename, _basestring):
        # People keep inverting arguments, and the resulting error is
        # incomprehensible
        raise ValueError(
            'Second argument should be a filename, %s (type %s) was given'
            % (filename, type(filename))
        )
    try:
        pickler = NumpyPickler(filename, compress=compress,
                               cache_size=cache_size)
        pickler.dump(value)
        pickler.close()
    finally:
        if 'pickler' in locals() and hasattr(pickler, 'file'):
            pickler.file.flush()
            pickler.file.close()
    return pickler._filenames


def load(filename, mmap_mode=None):
    """Reconstruct a Python object from a file persisted with joblib.load.

    Parameters
    -----------
    filename: string
        The name of the file from which to load the object
    mmap_mode: {None, 'r+', 'r', 'w+', 'c'}, optional
        If not None, the arrays are memory-mapped from the disk. This
        mode has no effect for compressed files. Note that in this
        case the reconstructed object might not longer match exactly
        the originally pickled object.

    Returns
    -------
    result: any Python object
        The object stored in the file.

    See Also
    --------
    joblib.dump : function to save an object

    Notes
    -----

    This function can load numpy array files saved separately during the
    dump. If the mmap_mode argument is given, it is passed to np.load and
    arrays are loaded as memmaps. As a consequence, the reconstructed
    object might not match the original pickled object. Note that if the
    file was saved with compression, the arrays cannot be memmaped.
    """
    file_handle = open(filename, 'rb')
    # We are careful to open the file handle early and keep it open to
    # avoid race-conditions on renames. That said, if data are stored in
    # companion files, moving the directory will create a race when
    # joblib tries to access the companion files.
    if _read_magic(file_handle) == _ZFILE_PREFIX:
        if mmap_mode is not None:
            warnings.warn('file "%(filename)s" appears to be a zip, '
                          'ignoring mmap_mode "%(mmap_mode)s" flag passed'
                          % locals(), Warning, stacklevel=2)
        unpickler = ZipNumpyUnpickler(filename, file_handle=file_handle)
    else:
        unpickler = NumpyUnpickler(filename,
                                   file_handle=file_handle,
                                   mmap_mode=mmap_mode)

    try:
        obj = unpickler.load()
    finally:
        if hasattr(unpickler, 'file_handle'):
            unpickler.file_handle.close()
    return obj
