#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import bisect
import collections
import io
import json
import os
import re
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time
import traceback
from timeit import default_timer as timer
from typing import Any, AnyStr, BinaryIO, Dict, IO, List, Optional, Set, Tuple, Union

import fuse

# Can't do this dynamically with importlib.import_module and using supportedCompressions
# because then the static checkers like mypy and pylint won't recognize the modules!
try:
    import indexed_bzip2
except ImportError:
    pass
try:
    import indexed_gzip
except ImportError:
    pass
try:
    import indexed_zstd
except ImportError:
    pass
try:
    import lzmaffi
except ImportError:
    pass


__version__ = '0.7.0'


# Defining lambdas does not yet check the names of entities used inside the lambda!
CompressionInfo = collections.namedtuple( 'CompressionInfo', [ 'suffixes', 'moduleName', 'checkHeader', 'open' ] )
supportedCompressions = {
    'bz2' : CompressionInfo( [ 'bz2', 'bzip2' ], 'indexed_bzip2',
                             lambda x : ( x.read(4)[:3] == b'BZh'
                                          and x.read(6) == (0x314159265359).to_bytes(6, 'big') ),
                             lambda x : indexed_bzip2.IndexedBzip2File( x.fileno() ) ),
    'gz'  : CompressionInfo( [ 'gz', 'gzip' ], 'indexed_gzip' ,
                             lambda x : x.read(2) == b'\x1F\x8B',
                             lambda x : indexed_gzip.IndexedGzipFile( fileobj = x ) ),
    'xz'  : CompressionInfo( [ 'xz' ], 'lzmaffi',
                             lambda x : x.read(6) == b"\xFD7zXZ\x00",
                             lambda x : lzmaffi.open( x ) ),
    'zst' : CompressionInfo( [ 'zst', 'zstd' ], 'indexed_zstd',
                             lambda x : x.read(4) == (0xFD2FB528).to_bytes(4, 'little'),
                             lambda x : indexed_zstd.IndexedZstdFile( x.fileno() ) ),
}


printDebug = 1

zstdMultiFrameCompressorBashFunction = """
function createMultiFrameZstd()
{
    local file frameSize fileSize offset frameFile
    # Detect being piped into
    if [ -t 0 ]; then
        file=$1
        frameSize=$2
        if [[ ! -f "$file" ]]; then echo "Could not find file '$file'." 1>&2; return 1; fi
        fileSize=$( stat -c %s -- "$file" )
    else
        if [ -t 1 ]; then echo 'You should pipe the output to somewhere!' 1>&2; return 1; fi
        echo 'Will compress from stdin...' 1>&2
        frameSize=$1
    fi
    if [[ ! $frameSize =~ ^[0-9]+$ ]]; then
        echo "Frame size '$frameSize' is not a valid number." 1>&2
    fi

    # Create a temporary file. I avoid simply piping to zstd
    # because it wouldn't store the uncompressed size.
    if [[ -d --tmpdir=/dev/shm ]]; then frameFile=$( mktemp --tmpdir=/dev/shm ); fi
    if [[ -z $frameFile ]]; then frameFile=$( mktemp ); fi
    if [[ -z $frameFile ]]; then
        echo "Could not create a temporary file for the frames." 1>2
        return 1
    fi
    trap "'rm' -- '$frameFile'" EXIT

    if [ -t 0 ]; then
        > "$file.zst"
        for (( offset = 0; offset < fileSize; offset += frameSize )); do
            dd if="$file" of="$frameFile" bs=$(( 1024*1024 )) \
               iflag=skip_bytes,count_bytes skip="$offset" count="$frameSize" 2>/dev/null
            zstd -c -q -f --no-progress -- "$frameFile" >> "$file.zst"
        done
    else
        while true; do
            dd of="$frameFile" bs=$(( 1024*1024 )) \
               iflag=count_bytes count="$frameSize" 2>/dev/null
            # pipe is finished when reading it yields no further data
            if [[ ! -s "$frameFile" ]]; then break; fi
            zstd -c -q -f --no-progress -- "$frameFile"
        done
    fi
}

createMultiFrameZstd <path-to-file-to-compress> $(( 4*1024*1024 ))
lbzip2 -cd well-compressed-file.bz2 | createMultiFrameZstd $(( 4*1024*1024 )) > recompressed.zst
"""


class RatarmountError(Exception):
    """Base exception for ratarmount module."""
class IndexNotOpenError(RatarmountError):
    """Exception for operations executed on a closed index database."""
class InvalidIndexError(RatarmountError):
    """Exception for indexes being invalid, outdated, or created with different arguments."""
class CompressionError(RatarmountError):
    """Exception for trying to open files with unsupported compression or unavailable decompression module."""

def overrides( parentClass ):
    """Simple decorator that checks that a method with the same name exists in the parent class"""
    def overrider( method ):
        assert method.__name__ in dir( parentClass )
        assert callable( getattr( parentClass, method.__name__ ) )
        return method
    return overrider


class ProgressBar:
    """Simple progress bar which keeps track of changes and prints the progress and a time estimate."""
    def __init__( self, maxValue : float ):
        self.maxValue        : float = maxValue
        self.lastUpdateTime  : float = time.time()
        self.lastUpdateValue : float = 0
        self.updateInterval  : float = 2 # seconds
        self.creationTime    : float = time.time()

    def update( self, value : float ) -> None:
        if self.lastUpdateTime is not None and ( time.time() - self.lastUpdateTime ) < self.updateInterval:
            return

        # Use whole interval since start to estimate time
        eta1 = int( ( time.time() - self.creationTime ) / value * ( self.maxValue - value ) )
        # Use only a shorter window interval to estimate time.
        # Accounts better for higher speeds in beginning, e.g., caused by caching effects.
        # However, this estimate might vary a lot while the other one stabilizes after some time!
        eta2 = int( ( time.time() - self.lastUpdateTime )
                    / ( value - self.lastUpdateValue )
                    * ( self.maxValue - value ) )
        print( "Currently at position {} of {} ({:.2f}%). "
               "Estimated time remaining with current rate: {} min {} s, with average rate: {} min {} s."
               .format( value, self.maxValue, value / self.maxValue * 100.,
                        eta2 // 60, eta2 % 60,
                        eta1 // 60, eta1 % 60 ),
               flush = True )

        self.lastUpdateTime = time.time()
        self.lastUpdateValue = value

class StenciledFile(io.BufferedIOBase):
    """A file abstraction layer giving a stenciled view to an underlying file."""

    def __init__(self, fileobj: IO, stencils: List[Tuple[int, int]]) -> None:
        """
        stencils: A list tuples specifying the offset and length of the underlying file to use.
                  The order of these tuples will be kept.
                  The offset must be non-negative and the size must be positive.

        Examples:
            stencil = [(5,7)]
                Makes a new 7B sized virtual file starting at offset 5 of fileobj.
            stencil = [(0,3),(5,3)]
                Make a new 6B sized virtual file containing bytes [0,1,2,5,6,7] of fileobj.
            stencil = [(0,3),(0,3)]
                Make a 6B size file containing the first 3B of fileobj twice concatenated together.
        """

        self.fileobj = fileobj
        self.offsets = [ x[0] for x in stencils ]
        self.sizes   = [ x[1] for x in stencils ]
        self.offset  = 0

        # Calculate cumulative sizes
        self.cumsizes = [ 0 ]
        for offset, size in stencils:
            assert offset >= 0
            assert size > 0
            self.cumsizes.append( self.cumsizes[-1] + size )

        # Seek to the first stencil offset in the underlying file so that "read" will work out-of-the-box
        self.seek( 0 )

    def _findStencil( self, offset: int ) -> int:
        """
        Return index to stencil where offset belongs to. E.g., for stencils [(3,5),(8,2)], offsets 0 to
        and including 4 will still be inside stencil (3,5), i.e., index 0 will be returned. For offset 6,
        index 1 would be returned because it now is in the second contiguous region / stencil.
        """
        # bisect_left( value ) gives an index for a lower range: value < x for all x in list[0:i]
        # Because value >= 0 and list starts with 0 we can therefore be sure that the returned i>0
        # Consider the stencils [(11,2),(22,2),(33,2)] -> cumsizes [0,2,4,6]. Seek to offset 2 should seek to 22.
        assert offset >= 0
        i = bisect.bisect_left( self.cumsizes, offset + 1 ) - 1
        assert i >= 0
        return i

    @overrides(io.BufferedIOBase)
    def close(self) -> None:
        # Don't close the object given to us
        #self.fileobj.close()
        pass

    @overrides(io.BufferedIOBase)
    def fileno(self) -> int:
        return self.fileobj.fileno()

    @overrides(io.BufferedIOBase)
    def seekable(self) -> bool:
        return self.fileobj.seekable()

    @overrides(io.BufferedIOBase)
    def readable(self) -> bool:
        return self.fileobj.readable()

    @overrides(io.BufferedIOBase)
    def writable(self) -> bool:
        return False

    @overrides(io.BufferedIOBase)
    def read(self, size: int=-1) -> bytes:
        if size == -1:
            size = self.cumsizes[-1] - self.offset

        # This loop works in a kind of leapfrog fashion. On each even loop iteration it seeks to the next stencil
        # and on each odd iteration it reads the data and increments the offset inside the stencil!
        result = b''
        i = self._findStencil( self.offset )
        while size > 0 and i < len( self.sizes ):
            # Read as much as requested or as much as the current contiguous region / stencil still contains
            readableSize = min( size, self.sizes[i] - ( self.offset - self.cumsizes[i] ) )
            if readableSize == 0:
                # Go to next stencil
                i += 1
                if i >= len( self.offsets ):
                    break
                self.fileobj.seek( self.offsets[i] )
            else:
                # Actually read data
                tmp = self.fileobj.read( readableSize )
                self.offset += len( tmp )
                result += tmp
                size -= readableSize
                # Now, either size is 0 or readableSize will be 0 in the next iteration

        return result

    @overrides(io.BufferedIOBase)
    def seek(self, offset: int, whence: int=io.SEEK_SET) -> int:
        if whence == io.SEEK_CUR:
            self.offset += offset
        elif whence == io.SEEK_END:
            self.offset = self.cumsizes[-1] + offset
        elif whence == io.SEEK_SET:
            self.offset = offset

        if self.offset < 0:
            raise ValueError("Trying to seek before the start of the file!")
        if self.offset >= self.cumsizes[-1]:
            return self.offset

        i = self._findStencil( self.offset )
        offsetInsideStencil = self.offset - self.cumsizes[i]
        assert offsetInsideStencil >= 0
        assert offsetInsideStencil < self.sizes[i]
        self.fileobj.seek( self.offsets[i] + offsetInsideStencil, io.SEEK_SET )

        return self.offset

    @overrides(io.BufferedIOBase)
    def tell(self) -> int:
        return self.offset

# Names must be identical to the SQLite column headers!
FileInfo = collections.namedtuple( "FileInfo",
                                   "offsetheader offset size mtime mode type linkname uid gid istar issparse" )

class SQLiteIndexedTar:
    """
    This class reads once through the whole TAR archive and stores TAR file offsets
    for all contained files in an index to support fast seeking to a given file.
    """

    # Version 0.1.0:
    #   - Initial version
    # Version 0.2.0:
    #   - Add sparse support and 'offsetheader' and 'issparse' columns to the SQLite database
    #   - Add TAR file size metadata in order to quickly check whether the TAR changed
    #   - Add 'offsetheader' to the primary key of the 'files' table so that files which were
    #     updated in the TAR can still be accessed if necessary.
    __version__ = '0.2.0'

    def __init__(
        self,
        tarFileName                : Optional[str]       = None,
        fileObject                 : Optional[BinaryIO]  = None,
        writeIndex                 : bool                = False,
        clearIndexCache            : bool                = False,
        indexFileName              : Optional[str]       = None,
        indexFolders               : Optional[List[str]] = None,
        recursive                  : bool                = False,
        gzipSeekPointSpacing       : int                 = 4*1024*1024,
        encoding                   : str                 = tarfile.ENCODING,
        stripRecursiveTarExtension : bool                = False,
        ignoreZeros                : bool                = False,
        verifyModificationTime     : bool                = False,
    ) -> None:
        """
        tarFileName : Path to the TAR file to be opened. If not specified, a fileObject must be specified.
                      If only a fileObject is given, the created index can't be cached (efficiently).
        fileObject : A io.IOBase derived object. If not specified, tarFileName will be opened.
                     If it is an instance of IndexedBzip2File, IndexedGzipFile, or IndexedZstdFile, then the offset
                     loading and storing from and to the SQLite database is managed automatically by this class.
        encoding : Will be forwarded to tarfile. Specifies how filenames inside the TAR are encoded.
        ignoreZeros : Will be forwarded to tarfile. Specifies to not only skip zero blocks but also blocks with
                      invalid data. Setting this to true can lead to some problems but is required to correctly
                      read concatenated tars.
        stripRecursiveTarExtension : If true and if recursive is also true, then a <file>.tar inside the current
                                     tar will be mounted at <file>/ instead of <file>.tar/.
        """
        # stores which parent folders were last tried to add to database and therefore do exist
        self.parentFolderCache: List[Tuple[str, str]] = []
        self.mountRecursively = recursive
        self.sqlConnection: Optional[sqlite3.Connection] = None
        self.encoding = encoding
        self.stripRecursiveTarExtension = stripRecursiveTarExtension
        self.ignoreZeros = ignoreZeros
        self.verifyModificationTime = verifyModificationTime
        self.gzipSeekPointSpacing = gzipSeekPointSpacing

        if not tarFileName:
            if not fileObject:
                raise ValueError( "At least one of tarFileName and fileObject arguments should be set!" )
            self.tarFileName = '<file object>'
            self.createIndex( fileObject )
            # return here because we can't find a save location without any identifying name
            return

        self.tarFileName = os.path.abspath( tarFileName )
        if not fileObject:
            fileObject = open( self.tarFileName, 'rb' )

        # rawFileObject : Only set when opening a compressed file and only kept to keep the
        #                 compressed file handle from being closed by the garbage collector.
        # tarFileObject : File object to the uncompressed (or decompressed) TAR file to read actual data out of.
        # compression   : Stores what kind of compression the originally specified TAR file uses.
        # isTar         : Can be false for the degenerated case of only a bz2 or gz file not containing a TAR
        self.tarFileObject, self.rawFileObject, self.compression, self.isTar = \
            SQLiteIndexedTar._openCompressedFile( fileObject, gzipSeekPointSpacing, encoding )

        if self.compression == 'xz':
            try:
                if len( self.tarFileObject.block_boundaries ) <= 1:
                    print( "[Warning] The specified file '{}'".format( self.tarFileName ) )
                    print( "[Warning] is compressed using xz but only contains one xz block. This makes it " )
                    print( "[Warning] impossible to use true seeking! Please (re)compress your TAR using pixz" )
                    print( "[Warning] (see https://github.com/vasi/pixz) in order for ratarmount to do be able " )
                    print( "[Warning] to do fast seeking to requested files." )
                    print( "[Warning] As it is, each file access will decompress the whole TAR from the beginning!" )
                    print()
            except:
                pass

        # will be used for storing indexes if current path is read-only
        possibleIndexFilePaths = [ self.tarFileName + ".index.sqlite" ]
        indexPathAsName = self.tarFileName.replace( "/", "_" ) + ".index.sqlite"
        if indexFileName:
            # A given index file name takes precedence and there should be no implicit fallback
            possibleIndexFilePaths = [ os.path.abspath( os.path.expanduser( indexFileName ) ) ]
        elif indexFolders:
            # An empty path is to be interpreted as the default path right besides the TAR
            if '' not in indexFolders:
                possibleIndexFilePaths = []
            for folder in indexFolders:
                if folder:
                    indexPath = os.path.join( folder, indexPathAsName )
                    possibleIndexFilePaths.append( os.path.abspath( os.path.expanduser( indexPath ) ) )

        self.indexFileName = None
        if clearIndexCache:
            for indexPath in possibleIndexFilePaths:
                if os.path.isfile( indexPath ):
                    os.remove( indexPath )

        # Try to find an already existing index
        for indexPath in possibleIndexFilePaths:
            if self._tryLoadIndex( indexPath ):
                self.indexFileName = indexPath
                break
        if self.indexIsLoaded():
            self._loadOrStoreCompressionOffsets()
            return

        # Find a suitable (writable) location for the index database
        if writeIndex:
            for indexPath in possibleIndexFilePaths:
                if self._pathIsWritable( indexPath ) and self._pathCanBeUsedForSqlite( indexPath ):
                    self.indexFileName = indexPath
                    break

        if not self.indexFileName:
            raise InvalidIndexError( "Could not find any existing index or writable location for an index in "
                                     + str( possibleIndexFilePaths ) )


        self.createIndex( self.tarFileObject )
        self._loadOrStoreCompressionOffsets() # store
        if self.sqlConnection:
            self._storeMetadata( self.sqlConnection )

        if printDebug >= 1 and writeIndex:
            # The 0-time is legacy for the automated tests
            print( "Writing out TAR index to", self.indexFileName, "took 0s",
                   "and is sized", os.stat( self.indexFileName ).st_size, "B" )

    def _storeMetadata( self, connection: sqlite3.Connection ) -> None:
        self._storeVersionsMetadata( connection )

        metadataTable = """
            /* empty table whose sole existence specifies that we finished iterating the tar */
            CREATE TABLE "metadata" (
                "key"      VARCHAR(65535) NOT NULL, /* e.g. "tarsize" */
                "value"    VARCHAR(65535) NOT NULL  /* e.g. size in bytes as integer */
            );
        """

        connection.executescript( metadataTable )

        # All of these require the generic "metadata" table.
        self._storeTarMetadata( connection, self.tarFileName )
        self._storeArgumentsMetadata( connection )
        connection.commit()

    @staticmethod
    def _storeVersionsMetadata( connection: sqlite3.Connection ) -> None:
        versionsTable = """
            /* This table's sole existence specifies that we finished iterating the tar for older ratarmount versions */
            CREATE TABLE "versions" (
                "name"     VARCHAR(65535) NOT NULL, /* which component the version belongs to */
                "version"  VARCHAR(65535) NOT NULL, /* free form version string */
                /* Semantic Versioning 2.0.0 (semver.org) parts if they can be specified:
                 *   MAJOR version when you make incompatible API changes,
                 *   MINOR version when you add functionality in a backwards compatible manner, and
                 *   PATCH version when you make backwards compatible bug fixes. */
                "major"    INTEGER,
                "minor"    INTEGER,
                "patch"    INTEGER
            );
        """
        try:
            connection.executescript( versionsTable )
        except Exception as exception:
            if printDebug >= 2:
                print( exception )
            print( "[Warning] There was an error when adding metadata information. Index loading might not work." )

        try:
            def makeVersionRow(
                versionName: str,
                version: str
            ) -> Tuple[str, str, Optional[str], Optional[str], Optional[str]]:
                versionNumbers = [ re.sub( '[^0-9]', '', x ) for x in version.split( '.' ) ]
                return ( versionName,
                         version,
                         versionNumbers[0] if len( versionNumbers ) > 0 else None,
                         versionNumbers[1] if len( versionNumbers ) > 1 else None,
                         versionNumbers[2] if len( versionNumbers ) > 2 else None, )

            versions = [ makeVersionRow( 'ratarmount', __version__ ),
                         makeVersionRow( 'index', SQLiteIndexedTar.__version__ ) ]

            for _, cinfo in supportedCompressions.items():
                if cinfo.moduleName in globals():
                    versions += [ makeVersionRow( cinfo.moduleName, globals()[cinfo.moduleName].__version__ ) ]

            connection.executemany( 'INSERT OR REPLACE INTO "versions" VALUES (?,?,?,?,?)', versions )

            # TODO Add more metadata, e.g., for recursive argument! Move to _addMetadata method
            # @see https://github.com/mxmlnkn/ratarmount/issues/37
        except Exception as exception:
            print( "[Warning] There was an error when adding version information." )
            if printDebug >= 3:
                print( exception )

    @staticmethod
    def _storeTarMetadata( connection: sqlite3.Connection, tarPath: AnyStr ) -> None:
        """Adds some consistency meta information to recognize the need to update the cached TAR index"""
        try:
            tarStats = os.stat( tarPath )
            serializedTarStats = json.dumps( { attr : getattr( tarStats, attr )
                                               for attr in dir( tarStats ) if attr.startswith( 'st_' ) } )
            connection.execute( 'INSERT INTO "metadata" VALUES (?,?)', ( "tarstats", serializedTarStats ) )
        except Exception as exception:
            if printDebug >= 2:
                print( exception )
            print( "[Warning] There was an error when adding file metadata." )
            print( "[Warning] Automatic detection of changed TAR files during index loading might not work." )

    def _storeArgumentsMetadata( self, connection: sqlite3.Connection ) -> None:
        argumentsToSave = [
            'mountRecursively',
            'gzipSeekPointSpacing',
            'encoding',
            'stripRecursiveTarExtension',
            'ignoreZeros'
        ]

        argumentsMetadata = json.dumps( { argument : getattr( self, argument ) for argument in argumentsToSave } )

        try:
            connection.execute( 'INSERT INTO "metadata" VALUES (?,?)', ( "arguments", argumentsMetadata ) )
        except Exception as exception:
            if printDebug >= 2:
                print( exception )
            print( "[Warning] There was an error when adding argument metadata." )
            print( "[Warning] Automatic detection of changed arguments files during index loading might not work." )

    @staticmethod
    def _pathIsWritable( path: AnyStr ) -> bool:
        try:
            folder = os.path.dirname( path )
            if folder:
                os.makedirs( folder, exist_ok = True )

            f = open( path, 'wb' )
            f.write( b'\0' * 1024 * 1024 )
            f.close()
            os.remove( path )

            return True

        except IOError:
            if printDebug >= 2:
                traceback.print_exc()
                print( "Could not create file:", path )

        return False

    @staticmethod
    def _pathCanBeUsedForSqlite( path: AnyStr ) -> bool:
        try:
            folder = os.path.dirname( path )
            if folder:
                os.makedirs( folder, exist_ok = True )

            connection = SQLiteIndexedTar._openSqlDb( path )
            connection.executescript( 'CREATE TABLE "files" ( "path" VARCHAR(65535) NOT NULL );' )
            connection.commit()
            connection.close()
            os.remove( path )

            return True

        except sqlite3.OperationalError:
            if printDebug >= 2:
                traceback.print_exc()
                print( "Could not create SQLite database at:", path )

        return False

    @staticmethod
    def _openSqlDb( path: AnyStr ) -> sqlite3.Connection:
        sqlConnection = sqlite3.connect( path )
        sqlConnection.row_factory = sqlite3.Row
        sqlConnection.executescript( """
            PRAGMA LOCKING_MODE = EXCLUSIVE;
            PRAGMA TEMP_STORE = MEMORY;
            PRAGMA JOURNAL_MODE = OFF;
            PRAGMA SYNCHRONOUS = OFF;
        """ )
        return sqlConnection

    def _updateProgressBar( self, progressBar, fileobj: Any ) -> None:
        try:
            if hasattr( fileobj, 'tell_compressed' ) and self.compression == 'bz2':
                # Note that because bz2 works on a bitstream the tell_compressed returns the offset in bits
                progressBar.update( fileobj.tell_compressed() // 8 )
            elif hasattr( fileobj, 'tell_compressed' ):
                progressBar.update( fileobj.tell_compressed() )
            elif hasattr( fileobj, 'fileobj' ):
                progressBar.update( fileobj.fileobj().tell() )
            elif self.rawFileObject and hasattr( self.rawFileObject, 'tell' ):
                progressBar.update( self.rawFileObject.tell() )
            else:
                progressBar.update( fileobj.tell() )
        except:
            pass

    def createIndex(
        self,
        fileObject  : Any,
        progressBar : Any = None,
        pathPrefix  : str = '',
        streamOffset: int = 0
    ) -> None:
        if printDebug >= 1:
            print( "Creating offset dictionary for",
                   "<file object>" if self.tarFileName is None else self.tarFileName, "..." )
        t0 = timer()

        # 1. If no SQL connection was given (by recursive call), open a new database file
        openedConnection = False
        if not self.indexIsLoaded() or not self.sqlConnection:
            if printDebug >= 1:
                print( "Creating new SQLite index database at", self.indexFileName )

            createTables = """
                CREATE TABLE "files" (
                    "path"          VARCHAR(65535) NOT NULL,
                    "name"          VARCHAR(65535) NOT NULL,
                    "offsetheader"  INTEGER,  /* seek offset from TAR file where these file's contents resides */
                    "offset"        INTEGER,  /* seek offset from TAR file where these file's contents resides */
                    "size"          INTEGER,
                    "mtime"         INTEGER,
                    "mode"          INTEGER,
                    "type"          INTEGER,
                    "linkname"      VARCHAR(65535),
                    "uid"           INTEGER,
                    "gid"           INTEGER,
                    /* True for valid TAR files. Internally used to determine where to mount recursive TAR files. */
                    "istar"         BOOL   ,
                    "issparse"      BOOL   ,  /* for sparse files the file size refers to the expanded size! */
                    /* See SQL benchmarks for decision on the primary key.
                     * See also https://www.sqlite.org/optoverview.html
                     * (path,name) tuples might appear multiple times in a TAR if it got updated.
                     * In order to also be able to show older versions, we need to add
                     * the offsetheader column to the primary key. */
                    PRIMARY KEY (path,name,offsetheader)
                );
                /* "A table created using CREATE TABLE AS has no PRIMARY KEY and no constraints of any kind"
                 * Therefore, it will not be sorted and inserting will be faster! */
                CREATE TABLE "filestmp" AS SELECT * FROM "files" WHERE 0;
                CREATE TABLE "parentfolders" (
                    "path"     VARCHAR(65535) NOT NULL,
                    "name"     VARCHAR(65535) NOT NULL,
                    PRIMARY KEY (path,name)
                );
            """

            openedConnection = True
            self.sqlConnection = self._openSqlDb( self.indexFileName if self.indexFileName else ':memory:' )
            tables = self.sqlConnection.execute( 'SELECT name FROM sqlite_master WHERE type = "table";' )
            if { "files", "filestmp", "parentfolders" }.intersection( { t[0] for t in tables } ):
                raise InvalidIndexError( "The index file {} already seems to contain a table. "
                                         "Please specify --recreate-index.".format( self.indexFileName ) )
            self.sqlConnection.executescript( createTables )

        # 2. Open TAR file reader
        loadedTarFile: Any = [] # Feign an empty TAR file if anything goes wrong
        try:
            streamed = bool( self.compression )
            # r: uses seeks to skip to the next file inside the TAR while r| doesn't do any seeks.
            # r| might be slower but for compressed files we have to go over all the data once anyways
            # and I had problems with seeks at this stage. Maybe they are gone now after the bz2 bugfix though.
            # Note that with ignore_zeros = True, no invalid header issues or similar will be raised even for
            # non TAR files!?
            # Using r: is buggy because the bz2 readers tell() is buggy and returns the number of total decoded bytes.
            # In seeking mode, tell() is used to initialize offset and offset_data so those are also bugged then!
            # This has been fixed starting from indexed_bzip2 1.1.2.
            if self.isTar:
                loadedTarFile = tarfile.open( fileobj      = fileObject,
                                              mode         = 'r|' if streamed else 'r:',
                                              ignore_zeros = self.ignoreZeros,
                                              encoding     = self.encoding )
        except tarfile.ReadError:
            pass

        if progressBar is None:
            progressBar = ProgressBar( os.fstat( fileObject.fileno() ).st_size )

        # 3. Iterate over files inside TAR and add them to the database
        try:
            for tarInfo in loadedTarFile:
                loadedTarFile.members = []
                globalOffset = streamOffset + tarInfo.offset_data
                globalOffsetHeader = streamOffset + tarInfo.offset
                self._updateProgressBar( progressBar, fileObject )

                mode = (
                    tarInfo.mode
                    | ( stat.S_IFDIR if tarInfo.isdir()  else 0 )
                    | ( stat.S_IFREG if tarInfo.isfile() else 0 )
                    | ( stat.S_IFLNK if tarInfo.issym()  else 0 )
                    | ( stat.S_IFCHR if tarInfo.ischr()  else 0 )
                    | ( stat.S_IFIFO if tarInfo.isfifo() else 0 )
                )

                # Add a leading '/' as a convention where '/' represents the TAR root folder
                # Partly, done because fusepy specifies paths in a mounted directory like this
                # os.normpath does not delete duplicate '/' at beginning of string!
                fullPath = pathPrefix + "/" + os.path.normpath( tarInfo.name ).lstrip( '/' )

                # 4. Open contained TARs for recursive mounting
                tarExtension = '.tar'
                isTar = False
                if self.mountRecursively and tarInfo.isfile() and tarInfo.name.endswith( tarExtension ):
                    if (
                        self.stripRecursiveTarExtension
                        and len( tarExtension ) > 0
                        and fullPath.endswith( tarExtension )
                    ):
                        fullPath = fullPath[:-len( tarExtension )]

                    oldPos = fileObject.tell()
                    fileObject.seek( globalOffset )
                    oldPrintName = self.tarFileName

                    try:
                        self.tarFileName = tarInfo.name.lstrip( '/' ) # This is for output of the recursive call
                        # StenciledFile's tell returns the offset inside the file chunk instead of the global one,
                        # so we have to always communicate the offset of this chunk to the recursive call no matter
                        # whether tarfile has streaming access or seeking access!
                        tarFileObject = StenciledFile( fileObject, [ ( globalOffset, tarInfo.size ) ] )
                        self.createIndex( tarFileObject, progressBar, fullPath, globalOffset )

                        # if the TAR file contents could be read, we need to adjust the actual
                        # TAR file's metadata to be a directory instead of a file
                        mode = (
                            ( mode & 0o777 )
                            | stat.S_IFDIR
                            | ( stat.S_IXUSR if mode & stat.S_IRUSR != 0 else 0 )
                            | ( stat.S_IXGRP if mode & stat.S_IRGRP != 0 else 0 )
                            | ( stat.S_IXOTH if mode & stat.S_IROTH != 0 else 0 )
                        )
                        isTar = True

                    except tarfile.ReadError:
                        pass

                    self.tarFileName = oldPrintName
                    del tarFileObject
                    fileObject.seek( oldPos )

                path, name = fullPath.rsplit( "/", 1 )
                fileInfo = (
                    path               , # 0
                    name               , # 1
                    globalOffsetHeader , # 2
                    globalOffset       , # 3
                    tarInfo.size       , # 4
                    tarInfo.mtime      , # 5
                    mode               , # 6
                    tarInfo.type       , # 7
                    tarInfo.linkname   , # 8
                    tarInfo.uid        , # 9
                    tarInfo.gid        , # 10
                    isTar              , # 11
                    tarInfo.issparse() , # 12
                )
                self._setFileInfo( fileInfo )
        except tarfile.ReadError as e:
            if 'unexpected end of data' in str( e ):
                print( "[Warning] The TAR file is incomplete. Ratarmount will work but some files might be cut off. "
                       "If the TAR file size changes, ratarmount will recreate the index during the next mounting." )

        # Everything below should not be done in a recursive call of createIndex
        if streamOffset > 0:
            t1 = timer()
            if printDebug >= 1:
                print( "Creating offset dictionary for",
                       "<file object>" if self.tarFileName is None else self.tarFileName,
                       "took {:.2f}s".format( t1 - t0 ) )
            return

        # If no file is in the TAR, then it most likely indicates a possibly compressed non TAR file.
        # In that case add that itself to the file index. This won't work when called recursively,
        # so check stream offset.
        fileCount = self.sqlConnection.execute( 'SELECT COUNT(*) FROM "files";' ).fetchone()[0]
        if fileCount == 0:
            tarInfo = os.fstat( fileObject.fileno() )
            fname = os.path.basename( self.tarFileName )
            for suffix in [ '.gz', '.bz2', '.bzip2', '.gzip', '.xz', '.zst', '.zstd' ]:
                if fname.lower().endswith( suffix ) and len( fname ) > len( suffix ):
                    fname = fname[:-len( suffix )]
                    break

            # If the file object is actually an IndexedBzip2File or such, we can't directly use the file size
            # from os.stat and instead have to gather it from seek. Unfortunately, indexed_gzip does not support
            # io.SEEK_END even though it could as it has the index ...
            while fileObject.read( 1024*1024 ):
                self._updateProgressBar( progressBar, fileObject )
            fileSize = fileObject.tell()

            fileInfo = (
                ""                 , # 0 path
                fname              , # 1
                None               , # 2 header offset
                0                  , # 3 data offset
                fileSize           , # 4
                tarInfo.st_mtime   , # 5
                tarInfo.st_mode    , # 6
                None               , # 7 TAR file type. Don't care because it curerntly is unused and overlaps with mode
                None               , # 8 linkname
                tarInfo.st_uid     , # 9
                tarInfo.st_gid     , # 10
                False              , # 11 isTar
                False              , # 12 isSparse, don't care if it is actually sparse or not because it is not in TAR
            )
            self._setFileInfo( fileInfo )

        # All the code below is for database finalizing which should not be done in a recursive call of createIndex!
        if not openedConnection:
            return

        # 5. Resort by (path,name). This one-time resort is faster than resorting on each INSERT (cache spill)
        if printDebug >= 2:
            print( "Resorting files by path ..." )

        cleanupDatabase = """
            INSERT OR REPLACE INTO "files" SELECT * FROM "filestmp" ORDER BY "path","name",rowid;
            DROP TABLE "filestmp";
            INSERT OR IGNORE INTO "files"
                /* path name offsetheader offset size mtime mode type linkname uid gid istar issparse */
                SELECT path,name,0,0,1,0,{},{},"",0,0,0,0
                FROM "parentfolders" ORDER BY "path","name";
            DROP TABLE "parentfolders";
        """.format( int( 0o555 | stat.S_IFDIR ), int( tarfile.DIRTYPE ) )
        self.sqlConnection.executescript( cleanupDatabase )

        self.sqlConnection.commit()

        t1 = timer()
        if printDebug >= 1:
            print( "Creating offset dictionary for",
                   "<file object>" if self.tarFileName is None else self.tarFileName,
                   "took {:.2f}s".format( t1 - t0 ) )

    @staticmethod
    def _rowToFileInfo( row: Dict[str, Any] ) -> FileInfo:
        return FileInfo(
            offset       = row['offset'],
            offsetheader = row['offsetheader'] if 'offsetheader' in row.keys() else 0,
            size         = row['size'],
            mtime        = row['mtime'],
            mode         = row['mode'],
            type         = row['type'],
            linkname     = row['linkname'],
            uid          = row['uid'],
            gid          = row['gid'],
            istar        = row['istar'],
            issparse     = row['issparse'] if 'issparse' in row.keys() else False
        )

    def getFileInfo(
        self,
        fullPath     : str,
        listDir      : bool = False,
        listVersions : bool = False,
        fileVersion  : int  = 0
    ) -> Optional[Union[FileInfo, Dict[str, FileInfo]]]:
        """
        This is the heart of this class' public interface!

        path    : full path to file where '/' denotes TAR's root, e.g., '/', or '/foo'
        listDir : if True, return a dictionary for the given directory path: { fileName : FileInfo, ... }
                  if False, return simple FileInfo to given path (directory or file)
        fileVersion : If the TAR contains the same file path multiple times, by default only the last one is shown.
                      But with this argument other versions can be queried. Version 1 is the oldest one.
                      Version 0 translates to the most recent one for compatibility with tar --occurrence=<number>.
                      Version -1 translates to the second most recent, and so on.
                      For listDir=True, the file version makes no sense and is ignored!
                      So, even if a folder was overwritten by a file, which is already not well supported by tar,
                      then listDir for that path will still list all contents of the overwritten folder or folders,
                      no matter the specified version. The file system layer has to take care that a directory
                      listing is not even requeted in the first place if it is not a directory.
                      FUSE already does this by calling getattr for all parent folders in the specified path first.

        If path does not exist, always return None

        If listVersions is true, then return metadata for all versions of a file possibly appearing more than once
        in the TAR as a directory dictionary. listDir will then be ignored!
        """
        # @todo cache last listDir as most often a stat over all entries will soon follow

        if not isinstance( fileVersion, int ):
            raise TypeError( "The specified file version must be an integer!" )
        if not self.sqlConnection:
            raise IndexNotOpenError( "This method can not be called without an opened index database!" )

        # also strips trailing '/' except for a single '/' and leading '/'
        fullPath = '/' + os.path.normpath( fullPath ).lstrip( '/' )

        if listVersions:
            path, name = fullPath.rsplit( '/', 1 )
            rows = self.sqlConnection.execute(
                'SELECT * FROM "files" WHERE "path" == (?) AND "name" == (?) ORDER BY "offsetheader" ASC',
                ( path, name )
            )
            result = { str( version + 1 ) : self._rowToFileInfo( row ) for version, row in enumerate( rows ) }
            return result

        if listDir:
            # For listing directory entries the file version can't be applied meaningfully at this abstraction layer.
            # E.g., should it affect the file version of the directory to list, or should it work on the listed files
            # instead and if so how exactly if there aren't the same versions for all files available, ...?
            # Or, are folders assumed to be overwritten by a new folder entry in a TAR or should they be union mounted?
            # If they should be union mounted, like is the case now, then the folder version only makes sense for
            # its attributes.
            rows = self.sqlConnection.execute( 'SELECT * FROM "files" WHERE "path" == (?)',
                                               ( fullPath.rstrip( '/' ), ) )
            directory = {}
            gotResults = False
            for row in rows:
                gotResults = True
                if row['name']:
                    directory[row['name']] = self._rowToFileInfo( row )
            return directory if gotResults else None

        path, name = fullPath.rsplit( '/', 1 )
        row = self.sqlConnection.execute(
            'SELECT * FROM "files" WHERE "path" == (?) AND "name" == (?) ORDER BY "offsetheader" {} LIMIT 1 OFFSET (?)'
            .format( 'DESC' if fileVersion is None or fileVersion <= 0 else 'ASC' ),
            ( path, name, 0 if fileVersion is None else fileVersion - 1 if fileVersion > 0 else fileVersion )
        ).fetchone()
        return self._rowToFileInfo( row ) if row else None

    def isDir( self, path: str ) -> bool:
        return isinstance( self.getFileInfo( path, listDir = True ), dict )

    def _tryAddParentFolders( self, path: str ) -> None:
        # Add parent folders if they do not exist.
        # E.g.: path = '/a/b/c' -> paths = [('', 'a'), ('/a', 'b'), ('/a/b', 'c')]
        # Without the parentFolderCache, the additional INSERT statements increase the creation time
        # from 8.5s to 12s, so almost 50% slowdown for the 8MiB test TAR!
        pathParts = path.split( "/" )
        paths = [ p
                  for p in (
                    ( "/".join( pathParts[:i] ), pathParts[i] )
                    for i in range( 1, len( pathParts ) )
                  )
                  if p not in self.parentFolderCache ]
        if not paths:
            return

        self.parentFolderCache += paths
        # Assuming files in the TAR are sorted by hierarchy, the maximum parent folder cache size
        # gives the maximum cacheable file nesting depth. High numbers lead to higher memory usage and lookup times.
        if len( self.parentFolderCache ) > 16:
            self.parentFolderCache = self.parentFolderCache[-8:]

        if not self.sqlConnection:
            raise IndexNotOpenError( "This method can not be called without an opened index database!" )
        self.sqlConnection.executemany( 'INSERT OR IGNORE INTO "parentfolders" VALUES (?,?)',
                                        [ ( p[0], p[1] ) for p in paths ] )

    def _setFileInfo( self, row: tuple ) -> None:
        if not self.sqlConnection:
            raise IndexNotOpenError( "This method can not be called without an opened index database!" )

        try:
            self.sqlConnection.execute( 'INSERT OR REPLACE INTO "files" VALUES (' +
                                        ','.join( '?' * len( row ) ) + ');', row )
        except UnicodeEncodeError:
            print( "[Warning] Problem caused by file name encoding when trying to insert this row:", row )
            print( "[Warning] The file name will now be stored with the bad character being escaped" )
            print( "[Warning] instead of being correctly interpreted." )
            print( "[Warning] Please specify a suitable file name encoding using, e.g., --encoding iso-8859-1!" )
            print( "[Warning] A list of possible encodings can be found here:" )
            print( "[Warning] https://docs.python.org/3/library/codecs.html#standard-encodings" )

            checkedRow = []
            for x in list( row ): # check strings
                if isinstance( x, str ):
                    try:
                        x.encode()
                        checkedRow += [ x ]
                    except UnicodeEncodeError:
                        checkedRow += [ x.encode( self.encoding, 'surrogateescape' ) \
                                         .decode( self.encoding, 'backslashreplace' ) ]
                else:
                    checkedRow += [ x ]

            self.sqlConnection.execute( 'INSERT OR REPLACE INTO "files" VALUES (' +
                                        ','.join( '?' * len( row ) ) + ');', tuple( checkedRow ) )
            print( "[Warning] The escaped inserted row is now:", row )
            print()

        self._tryAddParentFolders( row[0] )

    def setFileInfo( self, fullPath: str, fileInfo: FileInfo ) -> None:
        """
        fullPath : the full path to the file with leading slash (/) for which to set the file info
        """
        if not self.sqlConnection:
            raise IndexNotOpenError( "This method can not be called without an opened index database!" )
        assert fullPath[0] == "/"

        # os.normpath does not delete duplicate '/' at beginning of string!
        path, name = fullPath.rsplit( "/", 1 )
        row = (
            path,
            name,
            fileInfo.offsetheader,
            fileInfo.offset,
            fileInfo.size,
            fileInfo.mtime,
            fileInfo.mode,
            fileInfo.type,
            fileInfo.linkname,
            fileInfo.uid,
            fileInfo.gid,
            fileInfo.istar,
            fileInfo.issparse,
        )
        self._setFileInfo( row )

    def indexIsLoaded( self ) -> bool:
        """Returns true if the SQLite database has been opened for reading and a "files" table exists."""
        if not self.sqlConnection:
            return False

        try:
            self.sqlConnection.execute( 'SELECT * FROM "files" WHERE 0 == 1;' )
        except sqlite3.OperationalError:
            self.sqlConnection = None
            return False

        return True

    def loadIndex( self, indexFileName: AnyStr ) -> None:
        """Loads the given index SQLite database and checks it for validity."""
        if self.indexIsLoaded():
            return

        t0 = time.time()
        self.sqlConnection = self._openSqlDb( indexFileName )
        tables = [ x[0] for x in self.sqlConnection.execute( 'SELECT name FROM sqlite_master WHERE type="table"' ) ]
        versions = None
        try:
            rows = self.sqlConnection.execute( 'SELECT * FROM versions;' )
            versions = {}
            for row in rows:
                versions[row[0]] = ( row[2], row[3], row[4] )
        except sqlite3.OperationalError:
            pass

        try:
            # Check indexes created with bugged bz2 decoder (bug existed when I did not store versions yet)
            if 'bzip2blocks' in tables and 'versions' not in tables:
                raise InvalidIndexError(
                    "The indexes created with version 0.3.0 through 0.3.3 for bzip2 compressed archives "
                    "are very likely to be wrong because of a bzip2 decoder bug.\n"
                    "Please delete the index or call ratarmount with the --recreate-index option!" )

            # Check for empty or incomplete indexes. Pretty safe to rebuild the index for these as they
            # are so invalid, noone should miss them. So, recreate index by default for these cases.
            if 'files' not in tables:
                raise InvalidIndexError( "SQLite index is empty" )

            if 'filestmp' in tables or 'parentfolders' in tables:
                raise InvalidIndexError( "SQLite index is incomplete" )

            # Check for pre-sparse support indexes
            if (
                'versions' not in tables
                or 'index' not in versions
                or len(versions['index']) < 2
                or versions['index'][1] < 2
            ):
                print( "[Warning] The found outdated index does not contain any sparse file information." )
                print( "[Warning] The index will also miss data about multiple versions of a file." )
                print( "[Warning] Please recreate the index if you have problems with those." )

            if 'metadata' in tables:
                values = dict( self.sqlConnection.execute( 'SELECT * FROM metadata;' ) )
                if 'tarstats' in values:
                    values = json.loads( values['tarstats'] )
                tarStats = os.stat( self.tarFileName )

                if (
                    hasattr( tarStats, "st_size" )
                    and 'st_size' in values
                    and tarStats.st_size != values['st_size']
                ):
                    raise InvalidIndexError( "TAR file for this SQLite index has changed size from",
                                             values['st_size'], "to", tarStats.st_size)

                if (
                    self.verifyModificationTime
                    and hasattr( tarStats, "st_mtime" )
                    and 'st_mtime' in values
                    and tarStats.st_mtime != values['st_mtime']
                ):
                    raise InvalidIndexError( "The modification date for the TAR file", values['st_mtime'],
                                             "to this SQLite index has changed (" + str( tarStats.st_mtime ) + ")" )

                # Check arguments used to create the found index. These are only warnings and not forcing a rebuild
                # by default. ToDo: Add --force options?
                if 'arguments' in values:
                    indexArgs = json.loads( values['arguments'] )
                    argumentsToCheck = [
                        'mountRecursively',
                        'gzipSeekPointSpacing',
                        'encoding',
                        'stripRecursiveTarExtension',
                        'ignoreZeros'
                    ]
                    differingArgs = []
                    for arg in argumentsToCheck:
                        if (
                            arg in indexArgs
                            and hasattr( self, arg )
                            and indexArgs[arg] != getattr( self, arg )
                        ):
                            differingArgs.append( ( indexArgs[arg], getattr( self, arg ) ) )
                    if differingArgs:
                        print( "[Warning] The arguments used for creating the found index differ from the arguments " )
                        print( "[Warning] given for mounting the archive now. In order to apply these changes, " )
                        print( "[Warning] recreate the index using the --recreate-index option!" )

        except Exception as e:
            # indexIsLoaded checks self.sqlConnection, so close it before returning because it was found to be faulty
            try:
                self.sqlConnection.close()
            except sqlite3.Error:
                pass
            self.sqlConnection = None

            raise e

        if printDebug >= 1:
            # Legacy output for automated tests
            print( "Loading offset dictionary from", indexFileName, "took {:.2f}s".format( time.time() - t0 ) )

    def _tryLoadIndex( self, indexFileName: AnyStr ) -> bool:
        """calls loadIndex if index is not loaded already and provides extensive error handling"""

        if self.indexIsLoaded():
            return True

        if not os.path.isfile( indexFileName ):
            return False

        try:
            self.loadIndex( indexFileName )
        except Exception as exception:
            if printDebug >= 3:
                traceback.print_exc()

            print( "[Warning] Could not load file:", indexFileName )
            print( "[Info] Exception:", exception )
            print( "[Info] Some likely reasons for not being able to load the index file:" )
            print( "[Info]   - The index file has incorrect read permissions" )
            print( "[Info]   - The index file is incomplete because ratarmount was killed during index creation" )
            print( "[Info]   - The index file was detected to contain errors because of known bugs of older versions" )
            print( "[Info]   - The index file got corrupted because of:" )
            print( "[Info]     - The program exited while it was still writing the index because of:" )
            print( "[Info]       - the user sent SIGINT to force the program to quit" )
            print( "[Info]       - an internal error occured while writing the index" )
            print( "[Info]       - the disk filled up while writing the index" )
            print( "[Info]     - Rare lowlevel corruptions caused by hardware failure" )

            print( "[Info] This might force a time-costly index recreation, so if it happens often\n"
                   "       and mounting is slow, try to find out why loading fails repeatedly,\n"
                   "       e.g., by opening an issue on the public github page." )

            try:
                os.remove( indexFileName )
            except OSError:
                print( "[Warning] Failed to remove corrupted old cached index file:", indexFileName )

        if printDebug >= 3 and self.indexIsLoaded():
            print( "Loaded index", indexFileName )

        return self.indexIsLoaded()

    @staticmethod
    def _detectCompression( fileobj : BinaryIO ) -> Optional[str]:
        if not isinstance( fileobj, io.IOBase ) or not fileobj.seekable():
            return None

        oldOffset = fileobj.tell()
        for compressionId, compression in supportedCompressions.items():
            # The header check is a necessary condition not a sufficient condition.
            # Especially for gzip, which only has 2 magic bytes, false positives might happen.
            # Therefore, only use the magic bytes based check if the module could not be found
            # in order to still be able to print pinpoint error messages.
            matches = compression.checkHeader(fileobj)
            fileobj.seek( oldOffset )
            if not matches:
                continue

            if compression.moduleName not in globals() and matches:
                return compressionId

            try:
                compressedFileobj = compression.open( fileobj )
                # Reading 1B from a single-frame zst file might require decompressing it fully in order
                # to get uncompressed file size! Avoid that. The magic bytes should suffice mostly.
                # TODO: Make indexed_zstd not require the uncompressed size for the read call.
                if compressionId != 'zst':
                    compressedFileobj.read( 1 )
                compressedFileobj.close()
                fileobj.seek( oldOffset )
                return compressionId
            except:
                fileobj.seek( oldOffset )

        return None

    @staticmethod
    def _detectTar( fileobj : BinaryIO, encoding : str ) -> bool:
        if not isinstance( fileobj, io.IOBase ) or not fileobj.seekable():
            return False

        oldOffset = fileobj.tell()
        isTar = False
        try:
            with tarfile.open( fileobj = fileobj, mode = 'r:', encoding = encoding ):
                isTar = True
        except ( tarfile.ReadError, tarfile.CompressionError ):
            pass

        fileobj.seek( oldOffset )
        return isTar

    @staticmethod
    def _openCompressedFile( fileobj: BinaryIO, gzipSeekPointSpacing: int, encoding: str ) -> Any:
        """
        Opens a file possibly undoing the compression.
        Returns (tar_file_obj, raw_file_obj, compression, isTar).
        raw_file_obj will be none if compression is None.
        """
        compression = SQLiteIndexedTar._detectCompression( fileobj )
        if compression not in supportedCompressions:
            return fileobj, None, compression, SQLiteIndexedTar._detectTar( fileobj, encoding )

        cinfo = supportedCompressions[compression]
        if cinfo.moduleName not in globals():
            raise CompressionError( "Can't open a {} compressed file '{}' without {} module!"
                                    .format( compression, fileobj.name, cinfo.moduleName ) )

        if compression == 'gz':
            # drop_handles keeps a file handle opening as is required to call tell() during decoding
            tar_file = indexed_gzip.IndexedGzipFile( fileobj = fileobj,
                                                     drop_handles = False,
                                                     spacing = gzipSeekPointSpacing )
        else:
            tar_file = cinfo.open( fileobj )

        return tar_file, fileobj, compression, SQLiteIndexedTar._detectTar( tar_file, encoding )

    def _loadOrStoreCompressionOffsets( self ):
        # This should be called after the TAR file index is complete (loaded or created).
        # If the TAR file index was created, then tarfile has iterated over the whole file once
        # and therefore completed the implicit compression offset creation.
        if not self.sqlConnection:
            raise IndexNotOpenError( "This method can not be called without an opened index database!" )
        db = self.sqlConnection
        fileObject = self.tarFileObject

        if (
            hasattr( fileObject, 'set_block_offsets' )
            and hasattr( fileObject, 'block_offsets' )
            and self.compression in [ 'bz2', 'zst' ]
        ):
            if self.compression == 'bz2':
                table_name = 'bzip2blocks'
            elif self.compression == 'zst':
                table_name = 'zstdblocks'

            try:
                offsets = dict( db.execute( 'SELECT blockoffset,dataoffset FROM {};'.format( table_name ) ) )
                fileObject.set_block_offsets( offsets )
            except:
                if printDebug >= 2:
                    print( "[Info] Could not load {} block offset data. Will create it from scratch."
                           .format( self.compression ) )

                tables = [ x[0] for x in db.execute( 'SELECT name FROM sqlite_master WHERE type="table";' ) ]
                if table_name in tables:
                    db.execute( 'DROP TABLE {}'.format( table_name ) )
                db.execute( 'CREATE TABLE {} ( blockoffset INTEGER PRIMARY KEY, dataoffset INTEGER )'
                            .format( table_name ) )
                db.executemany( 'INSERT INTO {} VALUES (?,?)'.format( table_name ),
                                fileObject.block_offsets().items() )
                db.commit()
            return

        if (
            hasattr( fileObject, 'import_index' )
            and hasattr( fileObject, 'export_index' )
            and self.compression == 'gz'
        ):
            # indexed_gzip index only has a file based API, so we need to write all the index data from the SQL
            # database out into a temporary file. For that, let's first try to use the same location as the SQLite
            # database because it should have sufficient writing rights and free disk space.
            gzindex = None
            for tmpDir in [ os.path.dirname( self.indexFileName ), None ]:
                # Try to export data from SQLite database. Note that no error checking against the existence of
                # gzipindex table is done because the exported data itself might also be wrong and we can't check
                # against this. Therefore, collate all error checking by catching exceptions.
                try:
                    gzindex = tempfile.mkstemp( dir = tmpDir )[1]
                    with open( gzindex, 'wb' ) as file:
                        file.write( db.execute( 'SELECT data FROM gzipindex' ).fetchone()[0] )
                except:
                    try:
                        if gzindex:
                            os.remove( gzindex )
                    except:
                        pass
                    gzindex = None

            try:
                fileObject.import_index( filename = gzindex )
                return
            except:
                pass

            try:
                if gzindex:
                    os.remove( gzindex )
            except:
                pass

            # Store the offsets into a temporary file and then into the SQLite database
            if printDebug >= 2:
                print( "[Info] Could not load GZip Block offset data. Will create it from scratch." )

            # Transparently force index to be built if not already done so. build_full_index was buggy for me.
            # Seeking from end not supported, so we have to read the whole data in in a loop
            while fileObject.read( 1024*1024 ):
                pass

            # The created index can unfortunately be pretty large and tmp might actually run out of memory!
            # Therefore, try different paths, starting with the location where the index resides.
            gzindex = None
            for tmpDir in [ os.path.dirname( self.indexFileName ), None ]:
                gzindex = tempfile.mkstemp( dir = tmpDir )[1]
                try:
                    fileObject.export_index( filename = gzindex )
                except indexed_gzip.ZranError:
                    try:
                        os.remove( gzindex )
                    except:
                        pass
                    gzindex = None

            if not gzindex or not os.path.isfile( gzindex ):
                print( "[Warning] The GZip index required for seeking could not be stored in a temporary file!" )
                print( "[Info] This might happen when you are out of space in your temporary file and at the" )
                print( "[Info] the index file location. The gzipindex size takes roughly 32kiB per 4MiB of" )
                print( "[Info] uncompressed(!) bytes (0.8% of the uncompressed data) by default." )
                raise RuntimeError( "Could not initialize the GZip seek cache." )
            if printDebug >= 2:
                print( "Exported GZip index size:", os.stat( gzindex ).st_size )

            # Store contents of temporary file into the SQLite database
            tables = [ x[0] for x in db.execute( 'SELECT name FROM sqlite_master WHERE type="table"' ) ]
            if 'gzipindex' in tables:
                db.execute( 'DROP TABLE gzipindex' )
            db.execute( 'CREATE TABLE gzipindex ( data BLOB )' )
            with open( gzindex, 'rb' ) as file:
                db.execute( 'INSERT INTO gzipindex VALUES (?)', ( file.read(), ) )
            db.commit()
            os.remove( gzindex )
            return

        # Note that for xz seeking, loading and storing block indexes is unnecessary because it has an index included!
        if self.compression in [ None, 'xz' ]:
            return

        assert False, ( "Could not load or store block offsets for {} probably because adding support was forgotten!"
                        .format( self.compression ) )

class TarMount( fuse.Operations ):
    """
    This class implements the fusepy interface in order to create a mounted file system view
    to a TAR archive.
    Tasks of this class:
       - Changes all file permissions to read-only
       - Manage possibly multiple SQLiteIndexedTar objects and folder paths to be union mounted together
       - Forward access to mounted folders to the respective system calls
       - Resolve hard links returned by SQLiteIndexedTar
       - Get actual file contents either by directly reading from the TAR or by using StenciledFile and tarfile
       - Provide hidden folders as an interface to get older versions of updated files
    """

    __slots__ = (
        'mountSources',
        'rootFileInfo',
        'mountPoint',
        'mountPointFd',
        'mountPointWasCreated',
        'encoding',
    )

    def __init__(
        self,
        pathToMount : Union[str, List[str]],
        mountPoint  : str,
        encoding    : str = tarfile.ENCODING,
        **sqliteIndexedTarOptions
    ) -> None:
        self.encoding = encoding

        if not isinstance( pathToMount, list ):
            try:
                os.fspath( pathToMount )
                pathToMount = [ pathToMount ]
            except:
                pass

        self.mountSources: List[Any] = [
            SQLiteIndexedTar( tarFile,
                              writeIndex = True,
                              encoding = self.encoding,
                              **sqliteIndexedTarOptions )
            if not os.path.isdir( tarFile ) else os.path.realpath( tarFile )
            for tarFile in pathToMount
        ]

        # make the mount point read only and executable if readable, i.e., allow directory listing
        tarStats = os.stat( pathToMount[0] )
        # clear higher bits like S_IFREG and set the directory bit instead
        mountMode = (
            ( tarStats.st_mode & 0o777 )
            | stat.S_IFDIR
            | ( stat.S_IXUSR if tarStats.st_mode & stat.S_IRUSR != 0 else 0 )
            | ( stat.S_IXGRP if tarStats.st_mode & stat.S_IRGRP != 0 else 0 )
            | ( stat.S_IXOTH if tarStats.st_mode & stat.S_IROTH != 0 else 0 )
        )

        self.rootFileInfo = FileInfo(
            offset       = None             ,
            offsetheader = None             ,
            size         = tarStats.st_size ,
            mtime        = tarStats.st_mtime,
            mode         = mountMode        ,
            type         = tarfile.DIRTYPE  ,
            linkname     = ""               ,
            uid          = tarStats.st_uid  ,
            gid          = tarStats.st_gid  ,
            istar        = True             ,
            issparse     = False
        )

        # Create mount point if it does not exist
        self.mountPointWasCreated = False
        if mountPoint and not os.path.exists( mountPoint ):
            os.mkdir( mountPoint )
            self.mountPointWasCreated = True
        self.mountPoint = os.path.realpath( mountPoint )
        self.mountPointFd = os.open( self.mountPoint, os.O_RDONLY )

    def __del__( self ) -> None:
        try:
            if self.mountPointWasCreated:
                os.rmdir( self.mountPoint )
        except:
            pass

        try:
            os.close( self.mountPointFd )
        except:
            pass

    @staticmethod
    def _getFileInfoFromRealFile( filePath: AnyStr ) -> FileInfo:
        stats = os.lstat( filePath )
        return FileInfo(
            offset       = None          ,
            offsetheader = None          ,
            size         = stats.st_size ,
            mtime        = stats.st_mtime,
            mode         = stats.st_mode ,
            type         = None          , # I think this is completely unused and mostly contained in mode
            linkname     = os.readlink( filePath ) if os.path.islink( filePath ) else None,
            uid          = stats.st_uid  ,
            gid          = stats.st_gid  ,
            istar        = False         ,
            issparse     = False
        )

    def _getUnionMountFileInfo(
        self,
        filePath    : str,
        fileVersion : int = 0
    ) -> Optional[Tuple[FileInfo, Optional[str]]]:
        """Returns the file info from the last (most recent) mount source in mountSources,
           which contains that file and that mountSource itself. mountSource might be None
           if it is the root folder."""

        if filePath == '/':
            return self.rootFileInfo, None

        # We need to keep the sign of the fileVersion in order to forward it to SQLiteIndexedTar.
        # When the requested version can't be found in a mount source, increment negative specified versions
        # by the amount of versions in that mount source or decrement the initially positive version.
        if fileVersion <= 0:
            for mountSource in reversed( self.mountSources ):
                if isinstance( mountSource, str ):
                    realFilePath = os.path.join( mountSource, filePath.lstrip( os.path.sep ) )
                    if os.path.lexists( realFilePath ):
                        if fileVersion == 0:
                            return self._getFileInfoFromRealFile( realFilePath ), \
                                   os.path.join( mountSource, filePath.lstrip( os.path.sep ) )
                        fileVersion += 1

                else:
                    fileInfo = mountSource.getFileInfo( filePath, listDir = False, fileVersion = fileVersion )
                    if isinstance( fileInfo, FileInfo ):
                        return fileInfo, mountSource
                    fileVersion += len( mountSource.getFileInfo( filePath, listVersions = True ) )

                if fileVersion > 0:
                    return None

            return None

        # fileVersion >= 1
        for mountSource in self.mountSources:
            if isinstance( mountSource, str ):
                realFilePath = os.path.join( mountSource, filePath.lstrip( os.path.sep ) )
                if os.path.lexists( realFilePath ):
                    if fileVersion == 1:
                        return self._getFileInfoFromRealFile( realFilePath ), \
                               os.path.join( mountSource, filePath.lstrip( os.path.sep ) )
                    fileVersion -= 1

            else:
                fileInfo = mountSource.getFileInfo( filePath, listDir = False, fileVersion = fileVersion )
                if isinstance( fileInfo, FileInfo ):
                    return fileInfo, mountSource
                fileVersion -= len( mountSource.getFileInfo( filePath, listVersions = True ) )

            if fileVersion < 1:
                return None

        return None

    def _decodeVersionsPathAPI( self, filePath: str ) -> Optional[Tuple[str, bool, Optional[int]]]:
        """
        Do a loop over the parent path parts to resolve possible versions in parent folders.
        Note that multiple versions of a folder always are union mounted. So, for the path to a file
        inside those folders the exact version of a parent folder can simply be removed for lookup.
        Therefore, translate something like: /foo.version/3/bar.version/2/mimi.version/1 into
        /foo/bar/mimi.version/1
        This is possibly time-costly but requesting a different version from the most recent should
        be a rare occurence and FUSE also checks all parent parts before accessing a file so it
        might only slow down access by roughly factor 2.
        """

        # @todo make it work for files ending with '.versions'.
        # Currently, this feature would be hidden by those files. But, I think this should be quite rare.
        # I could allow arbitrary amounts of dots like '....versions' but then it wouldn't be discernible
        # for ...versions whether the versions of ..versions or .versions file was requested. I could add
        # a rule for the decision, like ...versions shows the versions of .versions and ....versions for
        # ..versions, however, all of this might require an awful lot of file existence checking.
        # My first idea was to use hidden subfolders for each file like path/to/file/.versions/1 but FUSE
        # checks the parents in a path that they are directories first, so getattr or readdir is not even
        # called for path/to/file/.versions if path/to/file is not a directory.
        # Another alternative might be one hidden folder at the root for a parallel file tree, like
        # /.versions/path/to/file/3 but that runs into similar problems when trying to specify the file
        # version or if a .versions root directory exists.

        filePathParts = filePath.lstrip( '/' ).split( '/' )
        filePath = ''
        pathIsSpecialVersionsFolder = False
        fileVersion = None # Not valid if None or parentIsVersions is True
        for part in filePathParts:
            # Skip over the exact version specified
            if pathIsSpecialVersionsFolder:
                try:
                    fileVersion = int( part )
                    assert str( fileVersion ) == part
                except:
                    return None
                pathIsSpecialVersionsFolder = False
                continue

            # Simply append normal existing folders
            tmpFilePath = '/'.join( [ filePath, part ] )
            if self._getUnionMountFileInfo( tmpFilePath ):
                filePath = tmpFilePath
                fileVersion = 0
                continue

            # If current path does not exist, check if it is a special versions path
            if part.endswith( '.versions' ) and len( part ) > len( '.versions' ):
                pathIsSpecialVersionsFolder = True
                filePath = tmpFilePath[:-len( '.versions' )]
                continue

            # Parent path does not exist and is not a versions path, so any subpaths also won't exist either
            return None

        return filePath, pathIsSpecialVersionsFolder, ( None if pathIsSpecialVersionsFolder else fileVersion )

    def _getFileInfo( self, filePath: str ) -> Tuple[FileInfo, Optional[str]]:
        """Wrapper for _getUnionMountFileInfo, which also resolves special file version specifications in the path."""
        result = self._getUnionMountFileInfo( filePath )
        if result:
            return result

        # If no file was found, check if a special .versions folder to an existing file/folder was queried.
        versionsInfo = self._decodeVersionsPathAPI( filePath )
        if not versionsInfo:
            raise fuse.FuseOSError( fuse.errno.ENOENT )
        filePath, pathIsSpecialVersionsFolder, fileVersion = versionsInfo

        # 2.) Check if the request was for the special .versions folder and return its contents or stats
        # At this point, filePath is assured to actually exist!
        if pathIsSpecialVersionsFolder:
            pathInfo = self._getUnionMountFileInfo( filePath )
            assert pathInfo
            parentFileInfo, mountSource = pathInfo
            return FileInfo(
                offset       = None                ,
                offsetheader = None                ,
                size         = 0                   ,
                mtime        = parentFileInfo.mtime,
                mode         = 0o777 | stat.S_IFDIR,
                type         = tarfile.DIRTYPE     ,
                linkname     = ""                  ,
                uid          = parentFileInfo.uid  ,
                gid          = parentFileInfo.gid  ,
                istar        = False               ,
                issparse     = False
            ), mountSource

        # 3.) At this point the request is for an actual version of a file or folder
        if fileVersion is None:
            print( "[Error] fileVersion should not be None!" )
            raise fuse.FuseOSError( fuse.errno.ENOENT )
        result = self._getUnionMountFileInfo( filePath, fileVersion = fileVersion )
        if result:
            return result

        raise fuse.FuseOSError( fuse.errno.ENOENT )

    def _getUnionMountListDir( self, folderPath: str ) -> Optional[Set[str]]:
        """
        Returns the set of all folder contents over all mount sources or None if the path was found in none of them.
        """

        files: Set[str] = set()
        folderExists = False

        for mountSource in self.mountSources:
            if isinstance( mountSource, str ):
                realFolderPath = os.path.join( mountSource, folderPath.lstrip( os.path.sep ) )
                if os.path.isdir( realFolderPath ):
                    files = files.union( os.listdir( realFolderPath ) )
                    folderExists = True
            else:
                result = mountSource.getFileInfo( folderPath, listDir = True )
                if isinstance( result, dict ):
                    files = files.union( result.keys() )
                    folderExists = True

        return files if folderExists else None

    @overrides( fuse.Operations )
    def init( self, connection ) -> None:
        os.fchdir( self.mountPointFd )
        for i in range( len( self.mountSources ) ):
            if self.mountSources[i] == self.mountPoint:
                self.mountSources[i] = '.'

    @overrides( fuse.Operations )
    def getattr( self, path: str, fh = None ) -> Dict[str, Any]:
        fileInfo, _ = self._getFileInfo( path )

        # Dereference hard links
        if not stat.S_ISREG( fileInfo.mode ) and not stat.S_ISLNK( fileInfo.mode ) and fileInfo.linkname:
            targetLink = '/' + fileInfo.linkname.lstrip( '/' )
            if targetLink != path:
                return self.getattr( targetLink, fh )

        # dictionary keys: https://pubs.opengroup.org/onlinepubs/007904875/basedefs/sys/stat.h.html
        statDict = { "st_" + key : getattr( fileInfo, key ) for key in ( 'size', 'mtime', 'mode', 'uid', 'gid' ) }
        # signal that everything was mounted read-only
        statDict['st_mode'] &= ~( stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH )
        statDict['st_mtime'] = int( statDict['st_mtime'] )
        statDict['st_nlink'] = 1  # TODO: this is wrong for files with hardlinks

        # du by default sums disk usage (the number of blocks used by a file)
        # instead of file size directly. Tar files are usually a series of 512B
        # blocks, so we report a 1-block header + ceil(filesize / 512).
        statDict['st_blksize'] = 512
        statDict['st_blocks'] = 1 + ((fileInfo.size + 511) // 512)

        return statDict

    @overrides( fuse.Operations )
    def readdir( self, path: str, fh ):
        # we only need to return these special directories. FUSE automatically expands these and will not ask
        # for paths like /../foo/./../bar, so we don't need to worry about cleaning such paths
        yield '.'
        yield '..'

        files = self._getUnionMountListDir( path )
        if files is not None:
            for key in files:
                yield key
            return

        # If no folder was found, check whether the special .versions folder was requested
        result = self._decodeVersionsPathAPI( path )
        if not result:
            return
        path, pathIsSpecialVersionsFolder, fileVersion = result

        if not pathIsSpecialVersionsFolder:
            files = self._getUnionMountListDir( path )
            if files is not None:
                for key in files:
                    yield key
            return

        # Print all available versions of the file at filePath as the contents of the special '.versions' folder
        version = 0
        for mountSource in self.mountSources:
            if isinstance( mountSource, str ):
                realPath = os.path.join( mountSource, path.lstrip( os.path.sep ) )
                if os.path.lexists( realPath ):
                    version += 1
                    yield str( version )
            else:
                fileVersions = mountSource.getFileInfo( path, listVersions = True )
                if isinstance( fileVersions, dict ):
                    for x in fileVersions.keys():
                        version += 1
                        yield str( version )

    @overrides( fuse.Operations )
    def readlink( self, path: str ) -> str:
        fileInfo, _ = self._getFileInfo( path )
        return fileInfo.linkname

    @overrides( fuse.Operations )
    def read( self, path: str, size: int, offset: int, fh ) -> bytes:
        fileInfo, mountSource = self._getFileInfo( path )
        # mountSource may only be None for the root folder. However, this read method
        # should  only ever be called on files, so mountSource shoult never be None.
        if mountSource is None:
            print( "[Error] Trying to read data from non-file root folder!" )
            raise fuse.FuseOSError( fuse.errno.EIO )

        # Dereference hard links
        if not stat.S_ISREG( fileInfo.mode ) and not stat.S_ISLNK( fileInfo.mode ) and fileInfo.linkname:
            targetLink = '/' + fileInfo.linkname.lstrip( '/' )
            if targetLink != path:
                return self.read( targetLink, size, offset, fh )

        # Handle access to underlying non-empty folder
        if isinstance( mountSource, str ):
            f = open( mountSource, 'rb' )
            f.seek( offset )
            return f.read( size )

        if fileInfo.issparse:
            # The TAR file format is very simple. It's just a concatenation of TAR blocks. There is not even a
            # global header, only the TAR block headers. That's why we can simpley cut out the TAR block for
            # the sparse file using StenciledFile and then use tarfile on it to expand the sparse file correctly.
            tarBlockSize = fileInfo.offset - fileInfo.offsetheader + fileInfo.size
            tarSubFile = StenciledFile( mountSource.tarFileObject, [ ( fileInfo.offsetheader, tarBlockSize ) ] )
            tmpTarFile = tarfile.open( fileobj = tarSubFile, mode = 'r:', encoding = self.encoding )
            tmpFileObject = tmpTarFile.extractfile( next( iter( tmpTarFile ) ) )
            tmpFileObject.seek( offset, os.SEEK_SET )
            result = tmpFileObject.read( size )
            tmpTarFile.close()
            return result

        try:
            mountSource.tarFileObject.seek( fileInfo.offset + offset, os.SEEK_SET )
            return mountSource.tarFileObject.read( size )
        except RuntimeError as exception:
            traceback.print_exc()
            print( "Caught exception when trying to read data from underlying TAR file! Returning errno.EIO." )
            raise fuse.FuseOSError( fuse.errno.EIO ) from exception


class TarFileType:
    """
    Similar to argparse.FileType but raises an exception if it is not a valid TAR file.
    """

    def __init__( self, encoding: str = tarfile.ENCODING ) -> None:
        self.encoding = encoding

    def __call__( self, tarFile: str ) -> Tuple[str, Optional[str]]:
        if not os.path.exists( tarFile ):
            raise argparse.ArgumentTypeError( "File '{}' does not exist!".format( tarFile ) )

        with open( tarFile, 'rb' ) as fileobj:
            compression = SQLiteIndexedTar._detectCompression( fileobj )

            try:
                # Determining if there are many frames in zstd is O(1) with is_multiframe
                if (
                    compression != 'zst'
                    or supportedCompressions[compression].moduleName not in globals()
                ):
                    raise Exception() # early exit because we catch it ourself anyways

                zstdFile = supportedCompressions[compression].open( fileobj )

                if not zstdFile.is_multiframe():
                    print( "[Warning] The specified file '{}'".format( tarFile ) )
                    print( "[Warning] is compressed using zstd but only contains one zstd frame. This makes it " )
                    print( "[Warning] impossible to use true seeking! Please (re)compress your TAR using multiple " )
                    print( "[Warning] frames in order for ratarmount to do be able to do fast seeking to requested " )
                    print( "[Warning] files. Else, each file access will decompress the whole TAR from the beginning!" )
                    print( "[Warning] Here is a simple bash script demonstrating how to do this:" )
                    print( zstdMultiFrameCompressorBashFunction )
                    print( "[Warning] The zstd command line tool does not support an option to do this automatically." )
                    print( "[Warning] See https://github.com/facebook/zstd/issues/2121" )
                    print()
            except:
                pass

            if compression not in supportedCompressions:
                if SQLiteIndexedTar._detectTar( fileobj, self.encoding ):
                    return tarFile, compression
                raise argparse.ArgumentTypeError( "Archive '{}' can't be opened!\n".format( tarFile ) )

        cinfo = supportedCompressions[compression]
        if cinfo.moduleName not in globals():
            raise argparse.ArgumentTypeError(
                "Can't open a {} compressed TAR file '{}' without {} module!"
                .format( compression, fileobj.name, cinfo.moduleName )
            )

        return tarFile, compression

class CustomFormatter( argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter ):
    pass

def parseArgs( rawArgs: Optional[List[str]] = None ):
    parser = argparse.ArgumentParser(
        formatter_class = CustomFormatter,
        description = '''\
With ratarmount, you can:
  - Mount a (compressed) TAR file to a folder for read-only access
  - Mount a compressed file to `<mountpoint>/<filename>`
  - Bind mount a folder to another folder for read-only access
  - Union mount a list of TARs, compressed files, and folders to a mount point
    for read-only access
''',
        epilog = '''\
# Metadata Index Cache

In order to reduce the mounting time, the created index for random access
to files inside the tar will be saved to one of these locations. These
locations are checked in order and the first, which works sufficiently, will
be used. This is the default location order:

  1. <path to tar>.index.sqlite
  2. ~/.ratarmount/<path to tar: '/' -> '_'>.index.sqlite
     E.g., ~/.ratarmount/_media_cdrom_programm.tar.index.sqlite

This list of fallback folders can be overwritten using the `--index-folders`
option. Furthermore, an explicitly named index file may be specified using
the `--index-file` option. If `--index-file` is used, then the fallback
folders, including the default ones, will be ignored!

# Bind Mounting

The mount sources can be TARs and/or folders.  Because of that, ratarmount
can also be used to bind mount folders read-only to another path similar to
`bindfs` and `mount --bind`. So, for:

    ratarmount folder mountpoint

all files in `folder` will now be visible in mountpoint.

# Union Mounting

If multiple mount sources are specified, the sources on the right side will be
added to or update existing files from a mount source left of it. For example:

    ratarmount folder1 folder2 mountpoint

will make both, the files from folder1 and folder2, visible in mountpoint.
If a file exists in both multiple source, then the file from the rightmost
mount source will be used, which in the above example would be `folder2`.

If you want to update / overwrite a folder with the contents of a given TAR,
you can specify the folder both as a mount source and as the mount point:

    ratarmount folder file.tar folder

The FUSE option -o nonempty will be automatically added if such a usage is
detected. If you instead want to update a TAR with a folder, you only have to
swap the two mount sources:

    ratarmount file.tar folder folder

# File versions

If a file exists multiple times in a TAR or in multiple mount sources, then
the hidden versions can be accessed through special <file>.versions folders.
For example, consider:

    ratarmount folder updated.tar mountpoint

and the file `foo` exists both in the folder and as two different versions
in `updated.tar`. Then, you can list all three versions using:

    ls -la mountpoint/foo.versions/
        dr-xr-xr-x 2 user group     0 Apr 25 21:41 .
        dr-x------ 2 user group 10240 Apr 26 15:59 ..
        -r-x------ 2 user group   123 Apr 25 21:41 1
        -r-x------ 2 user group   256 Apr 25 21:53 2
        -r-x------ 2 user group  1024 Apr 25 22:13 3

In this example, the oldest version has only 123 bytes while the newest and
by default shown version has 1024 bytes. So, in order to look at the oldest
version, you can simply do:
    cat mountpoint/foo.versions/1

# Compressed non-TAR files

If you want a compressed file not containing a TAR, e.g., `foo.bz2`, then
you can also use ratarmount for that. The uncompressed view will then be
mounted to `<mountpoint>/foo` and you will be able to leverage ratarmount's
seeking capabilities when opening that file.
''' )

    parser.add_argument(
        '-f', '--foreground', action='store_true', default = False,
        help = 'Keeps the python program in foreground so it can print debug '
               'output when the mounted path is accessed.' )

    parser.add_argument(
        '-d', '--debug', type = int, default = 1,
        help = 'Sets the debugging level. Higher means more output. Currently, 3 is the highest.' )

    parser.add_argument(
        '-c', '--recreate-index', action='store_true', default = False,
        help = 'If specified, pre-existing .index files will be deleted and newly created.' )

    parser.add_argument(
        '-r', '--recursive', action='store_true', default = False,
        help = 'Mount TAR archives inside the mounted TAR recursively. '
               'Note that this only has an effect when creating an index. '
               'If an index already exists, then this option will be effectively ignored. '
               'Recreate the index if you want change the recursive mounting policy anyways.' )

    # Considerations for the default value:
    #   - seek times for the bz2 backend are between 0.01s and 0.1s
    #   - seek times for the gzip backend are roughly 1/10th compared to bz2 at a default spacing of 4MiB
    #     -> we could do a spacing of 40MiB (however the comparison are for another test archive, so it might not apply)
    #   - ungziping firefox 66 inflates the compressed size of 66MiB to 184MiB (~3 times more) and takes 1.4s on my PC
    #     -> to have a response time of 0.1s, it would require a spacing < 13MiB
    #   - the gzip index takes roughly 32kiB per seek point
    #   - the bzip2 index takes roughly 16B per 100-900kiB of compressed data
    #     -> for the gzip index to have the same space efficiency assuming a compression ratio of only 1,
    #        the spacing would have to be 1800MiB at which point it would become almost useless
    parser.add_argument(
        '-gs', '--gzip-seek-point-spacing', type = float, default = 16,
        help =
        'This only is applied when the index is first created or recreated with the -c option. '
        'The spacing given in MiB specifies the seek point distance in the uncompressed data. '
        'A distance of 16MiB means that archives smaller than 16MiB in uncompressed size will '
        'not benefit from faster seek times. A seek point takes roughly 32kiB. '
        'So, smaller distances lead to more responsive seeking but may explode the index size!' )

    parser.add_argument(
        '-p', '--prefix', type = str, default = '',
        help = '[deprecated] Use "-o modules=subdir,subdir=<prefix>" instead. '
               'This standard way utilizes FUSE itself and will also work for other FUSE '
               'applications. So, it is preferable even if a bit more verbose.'
               'The specified path to the folder inside the TAR will be mounted to root. '
               'This can be useful when the archive as created with absolute paths. '
               'E.g., for an archive created with `tar -P cf /var/log/apt/history.log`, '
               '-p /var/log/apt/ can be specified so that the mount target directory '
               '>directly< contains history.log.' )

    parser.add_argument(
        '-e', '--encoding', type = str, default = tarfile.ENCODING,
        help = 'Specify an input encoding used for file names among others in the TAR. '
               'This must be used when, e.g., trying to open a latin1 encoded TAR on an UTF-8 system. '
               'Possible encodings: https://docs.python.org/3/library/codecs.html#standard-encodings' )

    parser.add_argument(
        '-i', '--ignore-zeros', action = 'store_true',
        help = 'Ignore zeroed blocks in archive. Normally, two consecutive 512-blocks filled with zeroes mean EOF '
               'and ratarmount stops reading after encountering them. This option instructs it to read further and '
               'is useful when reading archives created with the -A option.' )

    parser.add_argument(
        '--verify-mtime', action = 'store_true',
        help = 'By default, only the TAR file size is checked to match the one in the found existing ratarmount index. '
               'If this option is specified, then also check the modification timestamp. But beware that the mtime '
               'might change during copying or downloading without the contents changing. So, this check might cause '
               'false positives.' )

    parser.add_argument(
        '-s', '--strip-recursive-tar-extension', action = 'store_true',
        help = 'If true, then recursively mounted TARs named <file>.tar will be mounted at <file>/. '
               'This might lead to folders of the same name being overwritten, so use with care. '
               'The index needs to be (re)created to apply this option!' )

    parser.add_argument(
        '--index-file', type = str,
        help = 'Specify a path to the .index.sqlite file. Setting this will disable fallback index folders.' )

    parser.add_argument(
        '--index-folders', default = "," + os.path.join( "~", ".ratarmount" ),
        help = 'Specify one or multiple paths for storing .index.sqlite files. Paths will be tested for suitability '
               'in the given order. An empty path will be interpreted as the location in which the TAR resides. '
               'If the argument begins with a bracket "[", then it will be interpreted as a JSON-formatted list. '
               'If the argument contains a comma ",", it will be interpreted as a comma-separated list of folders. '
               'Else, the whole string will be interpreted as one folder path. Examples: '
               '--index-folders ",~/.foo" will try to save besides the TAR and if that does not work, in ~/.foo. '
               '--index-folders \'["~/.ratarmount", "foo,9000"]\' will never try to save besides the TAR. '
               '--index-folder ~/.ratarmount will only test ~/.ratarmount as a storage location and nothing else. '
               'Instead, it will first try ~/.ratarmount and the folder "foo,9000". ' )

    parser.add_argument(
        '-o', '--fuse', type = str, default = '',
        help = 'Comma separated FUSE options. See "man mount.fuse" for help. '
               'Example: --fuse "allow_other,entry_timeout=2.8,gid=0". ' )

    parser.add_argument(
        '-v', '--version', action='store_true', help = 'Print version string.' )

    parser.add_argument(
        'mount_source', nargs = '+',
        help = 'The path to the TAR archive to be mounted. '
               'If multiple archives and/or folders are specified, then they will be mounted as if the arguments '
               'coming first were updated with the contents of the archives or folders specified thereafter, '
               'i.e., the list of TARs and folders will be union mounted.' )
    parser.add_argument(
        'mount_point', nargs = '?',
        help = 'The path to a folder to mount the TAR contents into. '
               'If no mount path is specified, the TAR will be mounted to a folder of the same name '
               'but without a file extension.' )

    args = parser.parse_args( rawArgs )

    args.gzipSeekPointSpacing = args.gzip_seek_point_spacing * 1024 * 1024

    # This is a hack but because we have two positional arguments (and want that reflected in the auto-generated help),
    # all positional arguments, including the mountpath will be parsed into the tarfilepaths namespace and we have to
    # manually separate them depending on the type.
    if os.path.isdir( args.mount_source[-1] ) or not os.path.exists( args.mount_source[-1] ):
        args.mount_point = args.mount_source[-1]
        args.mount_source = args.mount_source[:-1]
    if not args.mount_source:
        print( "[Error] You must at least specify one path to a valid TAR file or union mount source directory!" )
        sys.exit( 1 )

    # Manually check that all specified TARs and folders exist
    args.mount_source = [ TarFileType( encoding = args.encoding )( tarFile )[0]
                          if not os.path.isdir( tarFile ) else os.path.realpath( tarFile )
                          for tarFile in args.mount_source ]

    # Automatically generate a default mount path
    if not args.mount_point:
        tarPath = args.mount_source[0]
        for compression in [ s for c in supportedCompressions.values() for s in c.suffixes ]:
            if not compression:
                continue
            doubleExtension = '.tar.' + compression
            if tarPath[-len( doubleExtension ):].lower() == doubleExtension.lower():
                args.mount_point = tarPath[:-len( doubleExtension )]
                break
        if not args.mount_point:
            args.mount_point = os.path.splitext( tarPath )[0]
    args.mount_point = os.path.abspath( args.mount_point )

    # Preprocess the --index-folders list as a string argument
    if args.index_folders:
        if args.index_folders[0] == '[':
            args.index_folders = json.loads( args.index_folders )
        elif ',' in args.index_folders:
            args.index_folders = args.index_folders.split( ',' )

    return args

def cli( rawArgs : Optional[List[str]] = None ) -> None:
    """Command line interface for ratarmount. Call with args = [ '--help' ] for a description."""

    # The first argument, is the path to the script and should be ignored
    tmpArgs = sys.argv[1:] if rawArgs is None else rawArgs
    if '--version' in tmpArgs or '-v' in tmpArgs:
        print( "ratarmount", __version__ )
        return

    # tmpArgs are only for the manual parsing. In general, rawArgs is None, meaning it reads sys.argv,
    # and maybe sometimes contains arguments when used programmatically. In that case the first argument
    # should not be the path to the script!
    args = parseArgs( rawArgs )

    # Convert the comma separated list of key[=value] options into a dictionary for fusepy
    fusekwargs = dict( [ option.split( '=', 1 ) if '=' in option else ( option, True )
                       for option in args.fuse.split( ',' ) ] ) if args.fuse else {}
    if args.prefix:
        fusekwargs['modules'] = 'subdir'
        fusekwargs['subdir'] = args.prefix

    if args.mount_point in args.mount_source:
        fusekwargs['nonempty'] = True

    global printDebug
    printDebug = args.debug

    fuseOperationsObject = TarMount(
        pathToMount                = args.mount_source,
        clearIndexCache            = args.recreate_index,
        recursive                  = args.recursive,
        gzipSeekPointSpacing       = args.gzipSeekPointSpacing,
        mountPoint                 = args.mount_point,
        encoding                   = args.encoding,
        ignoreZeros                = args.ignore_zeros,
        verifyModificationTime     = args.verify_mtime,
        stripRecursiveTarExtension = args.strip_recursive_tar_extension,
        indexFileName              = args.index_file,
        indexFolders               = args.index_folders,
    )

    fuse.FUSE( operations = fuseOperationsObject,
               mountpoint = args.mount_point,
               foreground = args.foreground,
               nothreads  = True, # Can't access SQLite database connection object from multiple threads
               **fusekwargs )

if __name__ == '__main__':
    cli( sys.argv[1:] )
